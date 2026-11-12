"""The host's indexer: keep the index in step with the files area, and answer from it.

The files area is ``your_files/``, each Goal's ``files/`` and ``memory/`` (the journal and the
working list copy) under the workspace (uploads, what the Muse saves, what it writes there).
:meth:`Indexer.scan` is the reconcile: new or changed files are queued, vanished ones are
dropped; :meth:`Indexer.add` is the event path for one file (an upload, a saved artifact).
:meth:`Indexer.process_next` runs one file through its passes:

``queued -> text_ready``: parse, cut into passages with page numbers, store them (keyword-
searchable at once).
``text_ready -> ready``: embed the passages through the model proxy (keyword-only, with the reason,
when the owner has no embedding connection), render PDF page thumbnails, settle.

The index also holds the ``conversation`` source (:meth:`Indexer.index_turns`): one passage per
completed turn, written by the runner when a compaction retires it; its only pass is the embedding.

Everything blocks; the runtime (:mod:`omnigent.superchat.knowledge.runtime`) runs it in one thread.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from omnigent.superchat.knowledge.chunking import chunk_pages
from omnigent.superchat.knowledge.db import (
    EMBED_EMBEDDED,
    EMBED_KEYWORD,
    KIND_TURN,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_READY,
    STATE_TEXT_READY,
    ChunkRow,
    FileRow,
    KnowledgeDb,
    TurnPassage,
    file_id_for,
    search_text,
)
from omnigent.superchat.knowledge.embedder import (
    REASON_ERROR,
    REASON_NO_CONNECTION,
    Embedder,
    EmbeddingUnavailable,
)
from omnigent.superchat.knowledge.fsio import read_bytes, sha256_of
from omnigent.superchat.knowledge.limits import INGEST_TIMEOUT_S, UPLOADS_PREFIX
from omnigent.superchat.knowledge.markdown import (
    parse_page_spec,
    select_pages,
    split_pages,
    to_markdown,
)
from omnigent.superchat.knowledge.parse import INDEXABLE_KINDS, parse_file
from omnigent.superchat.knowledge.pdf import render_page, render_thumbnails
from omnigent.superchat.knowledge.refs import journal_refs
from omnigent.superchat.knowledge.rerank import Reranker, RerankUnavailable
from omnigent.superchat.knowledge.tabular import (
    TABULAR_KINDS,
    manifest_pages,
    profile_table,
    schema_cards,
)

_logger = logging.getLogger(__name__)

#: A file larger than this is not indexed (it shows as failed, with the reason).
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ATTEMPTS = 3
#: Longest the background loop holds back for an explicit ingest before it carries on (the same
#: bound the relay waits for the ingest itself).
INGEST_YIELD_MAX_SECONDS = INGEST_TIMEOUT_S
#: How a kind's file is read. A file read by an older version of its kind's reader is read again.
PARSER_VERSIONS = {"pdf": 1, "txt": 1, "md": 1, "csv": 2, "xlsx": 2}
_BACKOFF_SECONDS = 30
DEFAULT_K = 8
MAX_K = 25
#: Longest passage returned to the model, in characters.
_PASSAGE_CHARS = 1600
#: How many fused candidates the LLM rerank reads.
RERANK_CANDIDATES = 20
#: A file whose Markdown is at most this long (about 8k tokens) travels whole with the message.
INLINE_MAX_CHARS = 32_000
#: Default and ceiling of what ``files_get`` / ``files_multi_get`` return, in characters.
GET_DEFAULT_CHARS = 24_000
GET_MAX_CHARS = 60_000
#: Said with a table file's manifest, in place of its rows.
TABULAR_NOTE = (
    "This is the table's schema, not its rows. Compute numbers with code (pandas, duckdb) in "
    "the host, and edit it with the sheet tools."
)
MULTI_GET_MAX_FILES = 20
#: Kinds by file extension (lower case, with the dot).
_EXTENSION_KIND = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".md": "md",
    ".markdown": "md",
    ".csv": "csv",
    ".xlsx": "xlsx",
}
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})
#: Journal day files: their passages carry the references of their lines.
JOURNAL_PREFIX = "memory/journal/"


def kind_of(name: str) -> str | None:
    """The indexable kind of a file name, or ``None`` when it is not one we read."""
    kind = _EXTENSION_KIND.get(PurePosixPath(name).suffix.lower())
    return kind if kind in INDEXABLE_KINDS else None


def thumbnail_ref(file_id: str, page: int) -> str:
    """The relative reference a search hit carries for a page thumbnail."""
    return f"files/{file_id}/pages/{page}/thumbnail"


def collection_of(path: str) -> str:
    """The folder of a workspace path: ``your_files``, ``memory``, ``goals/<id>`` or ``other``."""
    parts = PurePosixPath(path).parts
    if parts[:1] == ("your_files",):
        return "your_files"
    if parts[:1] == ("memory",):
        return "memory"
    if len(parts) > 2 and parts[0] == "goals" and parts[2] == "files":
        return f"goals/{parts[1]}"
    return "other"


class AmbiguousFile(Exception):
    """A plain file name that matches more than one indexed file."""

    def __init__(self, ref: str, candidates: list[str]) -> None:
        super().__init__(ref)
        self.ref = ref
        self.candidates = candidates


class Indexer:
    """Indexes the files area of one workspace; see the module docstring."""

    def __init__(
        self,
        home: Path,
        workspace: Path,
        embedder: Embedder,
        *,
        reranker: Reranker | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        :param home: The index directory (``~/.superchat/knowledge``): the database and thumbnails.
        :param workspace: The workspace root the indexed paths are relative to (``~/workspace``).
        :param embedder: Makes the vectors (or says why it cannot).
        :param reranker: Reorders the best hybrid hits with a model (``None``: fused order).
        """
        self.db = KnowledgeDb(home)
        self.reranker = reranker
        self._md = Path(home) / "md"
        self._work = threading.RLock()  # one pass at a time: the service thread and ingest
        # Explicit ingests waiting for (or holding) the work lock. Python locks are not FIFO, so a
        # background loop that re-takes the lock after every file could starve an ingest; the loop
        # waits on this between files instead (an attachment the person is waiting on goes first).
        self._ingest_waiting = 0
        self._ingest_cv = threading.Condition()
        self.workspace = workspace
        self.embedder = embedder
        self._thumbs = Path(home) / "thumbs"
        self._clock = clock
        #: Set by :meth:`shutdown` to end a wait early.
        self.stop_event = threading.Event()
        #: Called when a pass left work for the background loop (the service wakes its thread).
        self.on_pending: Callable[[], None] | None = None
        self._requeue_outdated()

    def _requeue_outdated(self) -> None:
        """Send searchable files read by an older reader of their kind back to be read again.

        Only ready files: a failed one stays failed (its reason is shown, the person retries) and
        one still waiting is read by the current reader anyway.
        """
        for row in self.db.files():
            if row.state == STATE_READY and row.parser_version < PARSER_VERSIONS.get(row.kind, 1):
                self.db.update_file(row.file_id, state=STATE_QUEUED, attempts=0, last_error=None)

    def shutdown(self) -> None:
        """Wake every wait on an ingest so the service can stop at once."""
        self.stop_event.set()
        with self._ingest_cv:
            self._ingest_cv.notify_all()

    # ------------------------------------------------------------------ the files area

    def _areas(self) -> Iterator[Path]:
        yield self.workspace / "your_files"
        yield self.workspace / "memory"
        goals = self.workspace / "goals"
        try:
            for goal in sorted(goals.iterdir()):
                yield goal / "files"
        except OSError:
            return

    def _walk(self) -> Iterator[tuple[str, Path]]:
        """``(relative path, absolute path)`` of every indexable file in the files area."""
        for area in self._areas():
            for directory, dirs, names in os.walk(area):
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d not in _SKIPPED_DIRS
                )
                for name in sorted(names):
                    if name.startswith(".") or kind_of(name) is None:
                        continue
                    absolute = Path(directory) / name
                    try:
                        relative = absolute.relative_to(self.workspace).as_posix()
                    except ValueError:
                        continue
                    yield relative, absolute

    def relative(self, path: str | Path) -> str | None:
        """*path* (absolute or workspace-relative) as a workspace-relative POSIX path, or ``None``
        when it lies outside the workspace."""
        given = Path(path)
        absolute = given if given.is_absolute() else self.workspace / given
        try:
            return absolute.resolve().relative_to(self.workspace.resolve()).as_posix()
        except (ValueError, OSError):
            return None

    def absolute(self, relative: str) -> Path:
        """The file's absolute path."""
        return self.workspace / relative

    def scan(self) -> dict[str, int]:
        """Reconcile the index with the disk: queue new and changed files, drop vanished ones.

        :returns: Counts ``{"added", "changed", "removed"}``.
        """
        counts = {"added": 0, "changed": 0, "removed": 0}
        known = {row.path: row for row in self.db.files()}
        seen: set[str] = set()
        for relative, absolute in self._walk():
            seen.add(relative)
            outcome = self._sync(relative, absolute, known.get(relative), explicit=False)
            if outcome:
                counts[outcome] += 1
        for relative, row in known.items():
            if relative in seen:
                continue
            absolute = self.absolute(relative)
            if row.explicit and absolute.is_file():
                if self._sync(relative, absolute, row, explicit=True) == "changed":
                    counts["changed"] += 1
                continue
            self.remove(relative)
            counts["removed"] += 1
        return counts

    def add(self, path: str | Path, *, explicit: bool = False) -> bool:
        """Queue one file now (an upload or a saved artifact). ``False`` when it is not indexable.

        :param explicit: Keep it indexed even outside the files area (a saved artifact).
        """
        relative = self.relative(path)
        if relative is None or kind_of(relative) is None:
            return False
        absolute = self.absolute(relative)
        if not absolute.is_file():
            return False
        self._sync(relative, absolute, self.db.file_by_path(relative), explicit=explicit)
        return True

    def remove(self, path: str | Path) -> None:
        """Forget a file and its thumbnails."""
        relative = self.relative(path) or str(path)
        fid = file_id_for(relative)
        self.db.delete_file(fid)
        self._drop_thumbs(fid)
        self.markdown_path(fid).unlink(missing_ok=True)

    def _sync(
        self, relative: str, absolute: Path, row: FileRow | None, *, explicit: bool
    ) -> str | None:
        try:
            stat = absolute.stat()
        except OSError:
            return None
        unchanged = (
            row is not None and row.size == stat.st_size and row.mtime_ns == stat.st_mtime_ns
        )
        if unchanged:
            if explicit and not row.explicit:
                self.db.update_file(row.file_id, explicit=1)
            if row.sha256 is None and row.state == STATE_READY:
                with contextlib.suppress(OSError):  # indexed before hashes were kept
                    self.db.update_file(row.file_id, sha256=sha256_of(absolute))
            return None
        kind = kind_of(relative)
        assert kind is not None
        self._drop_thumbs(file_id_for(relative))
        queued = self.db.upsert_file(
            path=relative,
            name=PurePosixPath(relative).name,
            kind=kind,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            explicit=explicit,
        )
        if stat.st_size > MAX_FILE_BYTES:
            self.db.update_file(
                queued.file_id,
                state=STATE_FAILED,
                last_error=f"File is too large to search ({MAX_FILE_BYTES >> 20} MB max)",
            )
        return "added" if row is None else "changed"

    # ------------------------------------------------------------------ the pass

    def _yield_to_ingest(self, timeout: float = INGEST_YIELD_MAX_SECONDS) -> None:
        """Hold the background loop back while an explicit ingest is waiting or running."""
        deadline = time.monotonic() + timeout
        with self._ingest_cv:
            while self._ingest_waiting > 0 and not self.stop_event.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._ingest_cv.wait(timeout=left)

    @contextlib.contextmanager
    def _ingest_priority(self) -> Iterator[None]:
        """Mark an explicit ingest as waiting; the background loop gives way until it is done."""
        with self._ingest_cv:
            self._ingest_waiting += 1
        try:
            yield
        finally:
            with self._ingest_cv:
                self._ingest_waiting -= 1
                self._ingest_cv.notify_all()

    def process_next(self) -> bool:
        """Run the next due file one step further. ``False`` when nothing is due.

        An explicit :meth:`ingest` goes first: the loop waits for it between files.
        """
        self._yield_to_ingest()
        row = self.db.next_due(self._clock())
        if row is None:
            return False
        try:
            self.process(row, background=True)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the indexer
            _logger.warning("indexing %s failed (%s)", row.path, type(exc).__name__)
            self._record_failure(row, exc)
        return True

    def process(
        self,
        row: FileRow,
        *,
        retry: bool = True,
        background: bool = False,
        finish: bool = True,
    ) -> None:
        """Run the pass(es) a file is due. Raises on failure; the caller records it.

        The text pass and the finishing pass (embeddings, thumbnails) each take the work lock on
        their own, so an urgent ingest can slip in between them. *background* is the service
        loop: it yields there to a waiting ingest. *finish* ``False`` stops after the text pass
        (the file is keyword-searchable, the loop does the rest).
        """
        if row.kind == KIND_TURN:  # a chat's conversation passages: stored, only embedding is left
            with self._work:
                current = self.db.get_file(row.file_id)
                if current is not None and current.state == STATE_TEXT_READY:
                    self._finish(current, None, retry=retry)
            return
        with self._work:
            absolute = self.absolute(row.path)
            if not absolute.is_file():
                self.remove(row.path)
                return
            current = self.db.get_file(row.file_id)
            if current is None:
                return
            row = current
            if row.state == STATE_QUEUED:
                # A table is read as a stream; no pass keeps the bytes of a CSV in memory.
                data = None if row.kind in TABULAR_KINDS else read_bytes(absolute)
                row = self._text_pass(row, absolute, data)
        if row.state != STATE_TEXT_READY or not finish:
            return
        if background:
            self._yield_to_ingest()
        with self._work:
            current = self.db.get_file(row.file_id)
            if current is None or current.state != STATE_TEXT_READY:
                return  # another pass finished it while this one stood aside
            absolute = self.absolute(current.path)
            data = read_bytes(absolute) if current.kind == "pdf" else None
            self._finish(current, data, retry=retry)

    def _text_pass(self, row: FileRow, absolute: Path, data: bytes | None) -> FileRow:
        if row.kind in TABULAR_KINDS:
            # A table is never read as rows of text: the index keeps its schema cards, the
            # Markdown version is the manifest (columns, types, row count, a few rows).
            profile = profile_table(row.kind, absolute, name=row.name)
            pages = manifest_pages(profile, row.name)
            chunks = schema_cards(profile, row.name)
            markdown = to_markdown(pages, kind="sheet")
            digest = sha256_of(absolute)
        else:
            assert data is not None
            pages, chunk_kind = parse_file(row.kind, data)
            chunks = chunk_pages(pages, kind=chunk_kind)
            markdown = to_markdown(pages, kind=chunk_kind)
            digest = hashlib.sha256(data).hexdigest()
        self._write_markdown(row.file_id, markdown)
        stored = self.db.replace_chunks(row.file_id, chunks)
        if row.path.startswith(JOURNAL_PREFIX):
            found = {c.id: refs for c in stored if (refs := journal_refs(c.text))}
            self.db.set_refs(found)
        self.db.update_file(
            row.file_id,
            state=STATE_TEXT_READY,
            page_count=len(pages),
            text_pages=sum(1 for page in pages if page.has_text),
            chunk_count=len(stored),
            sha256=digest,
            parser_version=PARSER_VERSIONS.get(row.kind, 1),
            last_error=None,
        )
        updated = self.db.get_file(row.file_id)
        assert updated is not None
        return updated

    def _finish(self, row: FileRow, data: bytes | None, *, retry: bool = True) -> None:
        status, reason = self._embed(row, self.db.chunks_for(row.file_id), retry=retry)
        if row.kind == "pdf" and data is not None:
            self._write_thumbs(row.file_id, data)
        self.db.update_file(
            row.file_id,
            state=STATE_READY,
            embed_status=status,
            embed_reason=reason,
            last_error=None,
            attempts=0,
        )

    def _embed(
        self, row: FileRow, chunks: Sequence[ChunkRow], *, retry: bool = True
    ) -> tuple[str, str | None]:
        """Embed the passages that have no current vector; ``(status, reason)``."""
        if not chunks:
            return EMBED_KEYWORD, None
        try:
            plan = self.embedder.plan()
        except EmbeddingUnavailable as exc:
            return EMBED_KEYWORD, exc.reason
        todo = [c for c in chunks if c.embed_tag != plan.tag]
        if not todo:
            return EMBED_EMBEDDED, None
        try:
            result = self.embedder.embed([search_text(c.heading_path, c.text) for c in todo])
        except EmbeddingUnavailable as exc:
            if retry and exc.reason == REASON_ERROR and row.attempts + 1 < MAX_ATTEMPTS:
                raise  # a provider hiccup: retry the file rather than settle for keywords
            return EMBED_KEYWORD, exc.reason
        dims = len(result.vectors[0])
        stored = self.db.set_vectors(
            {c.id: v for c, v in zip(todo, result.vectors, strict=True)}, result.tag, dims
        )
        if not stored:
            return EMBED_KEYWORD, REASON_NO_CONNECTION
        return EMBED_EMBEDDED, None

    def _record_failure(self, row: FileRow, error: Exception) -> None:
        """Count a failed attempt: retry later with a growing delay, then give up."""
        attempts = row.attempts + 1
        message = (str(error) or type(error).__name__)[:300]
        if attempts >= MAX_ATTEMPTS:
            self.db.update_file(
                row.file_id, state=STATE_FAILED, attempts=attempts, last_error=message
            )
            return
        self.db.update_file(
            row.file_id,
            attempts=attempts,
            last_error=message,
            next_attempt_at=self._clock() + _BACKOFF_SECONDS * 4 ** (attempts - 1),
        )

    def reindex(self) -> int:
        """Send files that are keyword-only or embedded with another model back for embedding.

        Conversation passages go back too; the count is of files only.
        """
        try:
            tag = self.embedder.plan().tag
        except EmbeddingUnavailable:
            return 0
        count = 0
        for row in self.db.files(turns=True):
            if row.state != STATE_READY or not row.chunk_count:
                continue
            stale = row.embed_status != EMBED_EMBEDDED or any(
                c.embed_tag != tag for c in self.db.chunks_for(row.file_id)
            )
            if stale:
                self.db.update_file(row.file_id, state=STATE_TEXT_READY, attempts=0)
                count += row.kind != KIND_TURN
        return count

    def maybe_reindex(self) -> int:
        """Re-embed once when the embedding model changes (a key was added, or the model moved).

        Files that failed to embed are not retried here until the model tag changes again.
        """
        try:
            tag = self.embedder.plan().tag
        except EmbeddingUnavailable:
            return 0
        if self.db.meta("reindexed_tag") == tag:
            return 0
        count = self.reindex()
        self.db.set_meta("reindexed_tag", tag)
        return count

    # ------------------------------------------------------------------ thumbnails

    def _thumb_path(self, file_id: str, page: int) -> Path:
        return self._thumbs / file_id / f"{page:05d}.webp"

    def _write_thumbs(self, file_id: str, data: bytes) -> None:
        directory = self._thumbs / file_id
        directory.mkdir(parents=True, exist_ok=True)
        for page, webp in render_thumbnails(data):
            self._thumb_path(file_id, page).write_bytes(webp)

    def _drop_thumbs(self, file_id: str) -> None:
        directory = self._thumbs / file_id
        if not directory.is_dir():
            return
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()

    def thumbnail(self, file_id: str, page: int) -> bytes | None:
        """A page's stored 256 px WebP, or ``None``."""
        row = self.db.get_file(file_id)
        if row is None or row.kind != "pdf" or page < 1 or page > row.page_count:
            return None
        try:
            return self._thumb_path(file_id, page).read_bytes()
        except OSError:
            return None

    # ------------------------------------------------------------------ reads

    def status(self) -> dict[str, Any]:
        """Index state per file, and whether searches can use embeddings."""
        reason: str | None = None
        try:
            self.embedder.plan()
        except EmbeddingUnavailable as exc:
            reason = exc.reason
        return {
            "files": [
                {**_file_view(row), "abs_path": str(self.absolute(row.path))}
                for row in self.db.files()
            ],
            "embeddings": {"available": reason is None, "reason": reason},
            "collections": self._collections(),
            "pending": self.db.pending_count(),
        }

    def _collections(self) -> list[dict[str, Any]]:
        """The person's folders and what is indexed in each."""
        names = ["your_files"]
        if (self.workspace / "memory").is_dir():
            names.append("memory")
        goals = self.workspace / "goals"
        with contextlib.suppress(OSError):
            names += [f"goals/{g.name}" for g in sorted(goals.iterdir()) if (g / "files").is_dir()]
        rows = self.db.files()
        return [
            {
                "name": name,
                "files": sum(1 for r in rows if collection_of(r.path) == name),
                "searchable": sum(
                    1 for r in rows if collection_of(r.path) == name and r.state == STATE_READY
                ),
            }
            for name in names
        ]

    def search(
        self,
        query: str,
        *,
        mode: str = "hybrid",
        file_ids: Sequence[str] | None = None,
        k: int = DEFAULT_K,
        rerank: bool = False,
    ) -> dict[str, Any]:
        """Passages for *query*.

        :param mode: ``keyword`` (BM25), ``vector`` (nearest passages) or ``hybrid`` (both,
            fused by reciprocal rank). Hybrid falls back to BM25 when the owner cannot embed;
            vector then answers ``reason: no_embeddings`` and nothing else.
        :param rerank: Reorder the best :data:`RERANK_CANDIDATES` hits with the model; the result
            says ``rerank: llm`` or ``rerank: fused`` (with ``rerank_reason``) either way.
        """
        limit = max(1, min(int(k), MAX_K))
        vector, reason = None, None
        if mode in ("vector", "hybrid"):
            try:
                vector = self.embedder.embed([query]).vectors[0]
            except EmbeddingUnavailable as exc:
                reason = exc.reason
        out: dict[str, Any] = {
            "query": query,
            "files_indexing": self.db.pending_count(),
            # Which files the answer may be missing, so the model can say so honestly.
            "files_indexing_names": self.db.pending_names(),
        }
        if mode == "vector" and vector is None:
            return {**out, "mode": "vector", "reason": "no_embeddings", "detail": reason,
                    "results": []}  # fmt: skip
        out_mode = "keyword" if vector is None else mode
        fetch = max(limit, RERANK_CANDIDATES) if rerank else limit
        found = self.db.search(
            query,
            limit=fetch,
            file_ids=file_ids,
            query_vector=vector,
            keyword=mode != "vector",
        )
        results = self._hits(found)
        out["mode"] = out_mode
        out["keyword_only_reason"] = reason if out_mode == "keyword" else None
        if rerank:
            results, verdict = self.rerank_hits(query, results)
            out.update(verdict)
        out["results"] = results[:limit]
        return out

    # ------------------------------------------------------------------ conversation passages

    def index_turns(
        self, passages: Sequence[TurnPassage], *, cursor: tuple[str, str] | None = None
    ) -> int:
        """Store turn passages (keyword-searchable now) and wake the loop to embed them.

        Under the work lock, so an embedding pass of the same chat cannot mark it done over a
        passage written while it ran.
        """
        with self._work:
            written = self.db.index_turns(passages, cursor=cursor)
        if written and self.on_pending is not None:
            self.on_pending()
        return written

    def delete_chats(self, chat_ids: Sequence[str], *, meta_keys: Sequence[str] = ()) -> int:
        """Remove every conversation passage of these chats (and the meta keys *meta_keys*)."""
        with self._work:
            return self.db.delete_chats(chat_ids, meta_keys=meta_keys)

    def rerank_hits(
        self, query: str, hits: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """*hits* in the model's order, or as they are with the reason it could not."""
        if len(hits) < 2:
            return hits, {"rerank": "fused", "rerank_reason": "too_few_results"}
        try:
            if self.reranker is None:
                raise RerankUnavailable("no_connection")
            order = self.reranker.rerank(query, [h["passage"] for h in hits])
        except RerankUnavailable as exc:
            return hits, {"rerank": "fused", "rerank_reason": exc.reason}
        seen = set(order)
        order = [*order, *(i for i in range(len(hits)) if i not in seen)]
        ranked = [dict(hits[i]) for i in order]
        for place, hit in enumerate(ranked):
            hit["score"] = round(1.0 - place / len(ranked), 4)
        return ranked, {"rerank": "llm", "rerank_reason": None}

    def _hits(self, found: Sequence[tuple[ChunkRow, float]]) -> list[dict[str, Any]]:
        files = {row.file_id: row for row in self.db.files()}
        results = []
        for chunk, score in found:
            row = files.get(chunk.file_id)
            if row is None:
                continue
            results.append(
                {
                    "file_id": row.file_id,
                    "path": row.path,
                    "abs_path": str(self.absolute(row.path)),
                    "file_name": row.name,
                    "page": chunk.page_start,
                    "page_end": chunk.page_end,
                    "score": round(score, 4),
                    "heading": chunk.heading_path,
                    "passage": chunk.text[:_PASSAGE_CHARS],
                    "thumbnail_url": thumbnail_ref(row.file_id, chunk.page_start)
                    if row.kind == "pdf"
                    and self._thumb_path(row.file_id, chunk.page_start).exists()
                    else None,
                }
            )
        return results

    # ------------------------------------------------------------------ Markdown

    def markdown_path(self, file_id: str) -> Path:
        """Where a file's Markdown version lives (``~/.superchat/knowledge/md/<file_id>.md``)."""
        return self._md / f"{file_id}.md"

    def _write_markdown(self, file_id: str, text: str) -> None:
        self._md.mkdir(parents=True, exist_ok=True)
        target = self.markdown_path(file_id)
        scratch = target.with_suffix(".md.tmp")
        scratch.write_text(text, encoding="utf-8")
        scratch.replace(target)

    def _markdown(self, row: FileRow) -> str | None:
        try:
            return self.markdown_path(row.file_id).read_text(encoding="utf-8")
        except OSError:
            return None

    def ingest(
        self,
        path: str | Path,
        *,
        inline: bool = False,
        within: str | None = None,
        finish: bool = True,
    ) -> dict[str, Any]:
        """Read one file now and return what it came to.

        The explicit operation an attachment goes through: convert to Markdown, cut passages,
        embed (when the owner can), index. Idempotent: an unchanged, already indexed file answers
        at once, without waiting for the background indexer (a size and mtime check, no lock).

        :param inline: Also return the Markdown when it is short enough to travel with the message.
        :param within: Only read a file under this workspace folder (``your_files/uploads/``).
            The check is on the resolved path, so ``..`` and a symlink out of the folder fail it.
        :param finish: ``False`` returns once the text is read (a turn needs the text, not the
            embeddings); the background loop finishes the file.
        :raises ValueError: the file cannot be read as an indexable kind, or reading failed.
        :raises FileNotFoundError: there is no such file.
        """
        relative = self.relative(path)
        if relative is None or kind_of(relative) is None:
            raise ValueError("This kind of file can't be read")
        if within is not None and not relative.startswith(within.rstrip("/") + "/"):
            raise ValueError("Only an uploaded file can be read here")
        absolute = self.absolute(relative)
        if not absolute.is_file():
            raise FileNotFoundError(relative)
        row = self._current_unlocked(relative, absolute, finish)
        if row is None:
            with self._ingest_priority():
                row = self._ingest_locked(relative, absolute, finish)
        if row is None or row.state not in (STATE_READY, STATE_TEXT_READY):
            raise ValueError((row.last_error if row else None) or "The file could not be read")
        if row.state != STATE_READY and self.on_pending is not None:
            self.on_pending()
        return self._ingest_answer(row, inline)

    def _current_unlocked(self, relative: str, absolute: Path, finish: bool) -> FileRow | None:
        """The row when the file is already read and unchanged on disk; no lock is taken."""
        row = self.db.file_by_path(relative)
        if row is None:
            return None
        ready = STATE_READY if finish else None
        if row.state != STATE_READY and not (ready is None and row.state == STATE_TEXT_READY):
            return None
        try:
            stat = absolute.stat()
        except OSError:
            return None
        if stat.st_size != row.size or stat.st_mtime_ns != row.mtime_ns:
            return None
        if row.size > MAX_FILE_BYTES or self._markdown(row) is None:
            return None
        return row

    def _ingest_locked(self, relative: str, absolute: Path, finish: bool) -> FileRow | None:
        with self._work:
            self._sync(relative, absolute, self.db.file_by_path(relative), explicit=False)
            row = self.db.file_by_path(relative)
            assert row is not None
            if row.size > MAX_FILE_BYTES:
                raise ValueError(f"File is too large to read ({MAX_FILE_BYTES >> 20} MB max)")
            if row.state == STATE_FAILED or (
                row.state == STATE_READY and self._markdown(row) is None
            ):
                # A retry by the person, or a file indexed before Markdown was kept: read it again.
                self.db.update_file(row.file_id, state=STATE_QUEUED, attempts=0, last_error=None)
                row = self.db.get_file(row.file_id)
                assert row is not None
        if row.state != STATE_READY:
            try:
                self.process(row, retry=False, finish=finish)
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                message = (str(exc) or type(exc).__name__)[:300]
                self.db.update_file(row.file_id, state=STATE_FAILED, last_error=message)
        return self.db.file_by_path(relative)

    def _ingest_answer(self, row: FileRow, inline: bool) -> dict[str, Any]:
        text = self._markdown(row) or ""
        out: dict[str, Any] = {
            "file_id": row.file_id,
            "name": row.name,
            "path": row.path,
            "pages": row.page_count,
            "pages_without_text": max(row.page_count - row.text_pages, 0)
            if row.kind == "pdf"
            else 0,
            "chars": len(text),
            "kind": row.kind,
            "markdown_path": str(self.markdown_path(row.file_id)),
            "search": "hybrid" if row.embed_status == EMBED_EMBEDDED else "keyword",
        }
        if inline and len(text) <= INLINE_MAX_CHARS:
            out["markdown"] = text
        return out

    def find_upload(self, sha256: str) -> dict[str, Any] | None:
        """A searchable upload whose bytes have this SHA-256, or ``None``.

        The stored hash is only a lead: the file is hashed again from disk, so a file changed
        since it was indexed, or a link swapped in, never answers for bytes it no longer holds.
        """
        for row in self.db.ready_files_with_hash(sha256.lower()):
            if not row.path.startswith(UPLOADS_PREFIX):
                continue
            try:
                if sha256_of(self.absolute(row.path)) != sha256.lower():
                    continue
            except OSError:
                continue
            return {
                "file_id": row.file_id,
                "name": row.name,
                "path": row.path,
                "pages": row.page_count,
            }
        return None

    def get(
        self, row: FileRow, *, pages: object = None, max_chars: int | None = None
    ) -> dict[str, Any] | None:
        """A file's Markdown (all of it, or only *pages*), cut to *max_chars*; ``None`` unread."""
        text = self._markdown(row)
        if text is None:
            return None
        limit = max(1000, min(max_chars or GET_DEFAULT_CHARS, GET_MAX_CHARS))
        selected: list[int] | None = None
        if pages is not None:
            selected = parse_page_spec(pages, row.page_count)
            if not selected:
                raise ValueError(f"{row.name} has pages 1 to {max(row.page_count, 1)}")
            text = select_pages(text, selected)
        total = len(text)
        out: dict[str, Any] = {
            "file_id": row.file_id,
            "path": row.path,
            "name": row.name,
            "pages": row.page_count,
            "chars": total,
            "truncated": total > limit,
        }
        if total > limit:
            cut = text.rfind("\n", 0, limit)
            text = text[: cut if cut > limit // 2 else limit]
            shown = list(split_pages(text))
            out["note"] = (
                f"Cut at {len(text)} of {total} characters"
                + (f" (through page {shown[-1]})" if shown else "")
                + "; ask for the rest with pages."
            )
        out["markdown"] = text
        if row.kind in TABULAR_KINDS:
            out["note"] = f"{out['note']} {TABULAR_NOTE}" if "note" in out else TABULAR_NOTE
        return out

    def multi_get(self, pattern: str, *, max_chars: int | None = None) -> dict[str, Any]:
        """The Markdown of every file whose path matches *pattern* (a glob, or comma-separated).

        The character budget is shared out evenly; at most :data:`MULTI_GET_MAX_FILES` files.
        """
        patterns = [p.strip().lower() for p in pattern.split(",") if p.strip()]
        named = [
            row
            for row in self.db.files()
            if any(
                fnmatch.fnmatchcase(row.path.lower(), p)
                or fnmatch.fnmatchcase(row.name.lower(), p)
                for p in patterns
            )
        ]
        rows = [row for row in named if row.state == STATE_READY]
        # Matched files that are not readable yet are reported, never silently left out.
        not_ready = [
            {"path": row.path, "state": _file_view(row)["state"], "error": row.last_error}
            for row in named
            if row.state != STATE_READY
        ]
        matched = len(rows)
        rows = rows[:MULTI_GET_MAX_FILES]
        budget = max(1000, min(max_chars or GET_DEFAULT_CHARS, GET_MAX_CHARS))
        each = max(1000, budget // max(len(rows), 1))
        files = [got for row in rows if (got := self.get(row, max_chars=each)) is not None]
        return {
            "pattern": pattern,
            "matched": matched,
            "returned": len(files),
            "files": files,
            **(
                {
                    "not_ready": not_ready,
                    "note": f"{len(not_ready)} matching file(s) are still being indexed or "
                    "could not be read, so they are not included; try again shortly.",
                }
                if not_ready
                else {}
            ),
        }

    def resolve(self, file_id: str | None, path: str | None) -> FileRow | None:
        """The file named by id, or by workspace path or plain file name (either may be given).

        :raises AmbiguousFile: when a plain name matches several files.
        """
        if file_id:
            row = self.db.get_file(file_id)
            return None if row is not None and row.kind == KIND_TURN else row
        ref = (path or "").strip()
        if not ref:
            return None
        relative = self.relative(ref)
        row = self.db.file_by_path(relative) if relative else None
        if row is not None and row.kind != KIND_TURN:
            return row
        wanted = ref.replace("\\", "/").strip("/").lower()
        matches = [
            f
            for f in self.db.files()
            if f.path.lower() == wanted
            or f.path.lower().endswith("/" + wanted)
            or f.name.lower() == wanted
        ]
        if len(matches) > 1:
            raise AmbiguousFile(ref, [m.path for m in matches])
        return matches[0] if matches else None

    def page(self, row: FileRow, page: int) -> dict[str, Any] | None:
        """One page: its text and, for a PDF, its 1024 px image; ``None`` when out of range."""
        if page < 1 or page > max(row.page_count, 1) or row.state == STATE_FAILED:
            return None
        text = "\n".join(c.text for c in self.db.page_chunks(row.file_id, page))
        image: bytes | None = None
        if row.kind == "pdf":
            try:
                image = render_page(self.absolute(row.path).read_bytes(), page)
            except OSError:
                image = None
        return {
            "file_id": row.file_id,
            "path": row.path,
            "name": row.name,
            "page": page,
            "page_count": row.page_count,
            "text": text,
            "image_base64": base64.b64encode(image).decode() if image else None,
            "image_mime": "image/webp" if image else None,
        }


def _file_view(row: FileRow) -> dict[str, Any]:
    state = (
        "failed"
        if row.state == STATE_FAILED
        else "searchable"
        if row.state == STATE_READY
        else "indexing"
    )
    return {
        "file_id": row.file_id,
        "path": row.path,
        "name": row.name,
        "kind": row.kind,
        "collection": collection_of(row.path),
        "state": state,
        "search": "hybrid" if row.embed_status == EMBED_EMBEDDED else "keyword",
        "keyword_only_reason": row.embed_reason,
        "pages": row.page_count,
        "pages_without_text": max(row.page_count - row.text_pages, 0) if row.kind == "pdf" else 0,
        "chunks": row.chunk_count,
        "error": row.last_error if row.state == STATE_FAILED else None,
    }
