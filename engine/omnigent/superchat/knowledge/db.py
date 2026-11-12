"""The host's file index: one SQLite file with the files, their passages and both indexes.

Layout (all in ``<home>/.superchat/knowledge/index.sqlite3``, on the host's persistent home
volume):

* ``files``: one row per indexed file, keyed by its workspace-relative path, with the pass state
  (``queued -> text_ready -> ready`` or ``failed``) and what was seen on disk (size, mtime).
* ``chunks``: the passages (page range, heading path, text), the source of truth.
* ``chunks_fts``: an FTS5 table over the passages (BM25), kept in step with ``chunks``.
* ``chunks_vec``: a sqlite-vec ``vec0`` table of their embeddings (cosine), created when the first
  vector arrives, rebuilt when the embedding model changes.
* ``turns``: the ``conversation`` source, one passage per completed turn of the person's chats. A
  chat keeps one row in ``files`` (kind ``turn``, path ``conversation/<chat id>``) that owns its
  passages, so they share the FTS and vector tables and the embedding pass; such a row is never a
  file (left out of :meth:`KnowledgeDb.files`, the pending counts and file searches).
* ``refs``: the references a passage carries (``files``, ``artifacts``, ``project``, ``helpers``;
  see :mod:`omnigent.superchat.knowledge.refs`), from a journal line's trailing comment
  or a turn's tool calls, so a search can filter on them.

``search`` fuses the two rankings with reciprocal rank fusion, or returns BM25 alone when the
caller
has no query vector. Nothing here calls a model; the vectors are handed in.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
import threading
import time
from collections.abc import Hashable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.superchat.knowledge.chunking import Chunk

STATE_QUEUED = "queued"
STATE_TEXT_READY = "text_ready"
STATE_READY = "ready"
STATE_FAILED = "failed"
#: States the worker still has work for.
PENDING_STATES = (STATE_QUEUED, STATE_TEXT_READY)

#: Kind of a chat's row in ``files`` and of its passages: the ``conversation`` source.
KIND_TURN = "turn"
CONVERSATION = "conversation"

EMBED_EMBEDDED = "embedded"
EMBED_KEYWORD = "keyword"

#: Candidates each ranking contributes to the fusion, per requested result.
_CANDIDATE_FACTOR = 10
_MIN_CANDIDATES = 50
#: Reciprocal-rank-fusion constant (the usual 60).
_RRF_K = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    explicit INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    embed_status TEXT NOT NULL DEFAULT 'keyword',
    embed_reason TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    text_pages INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    sha256 TEXT,
    parser_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS files_state ON files (state, next_attempt_at);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL REFERENCES files (file_id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    kind TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    text TEXT NOT NULL,
    embed_tag TEXT
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks (file_id, page_start, ord);
CREATE TABLE IF NOT EXISTS turns (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks (id) ON DELETE CASCADE,
    turn_item_id TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    main_id TEXT NOT NULL,
    date TEXT NOT NULL,
    origin TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    person_text TEXT NOT NULL DEFAULT '',
    last_item_id TEXT NOT NULL DEFAULT '',
    at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS turns_main ON turns (main_id);
CREATE TABLE IF NOT EXISTS refs (
    chunk_id INTEGER NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (chunk_id, key, value)
);
CREATE INDEX IF NOT EXISTS refs_value ON refs (key, value);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body, tokenize = 'unicode61 remove_diacritics 2'
);
"""

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)

#: Words that say nothing about which passage answers (English and French), plus the words a
#: person uses to talk *to* the search ("find", "cite the file and page").
_STOPWORDS = frozenset(
    """
    a about above after again all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from
    further had has have having he her here hers him his how i if in into is it its just me
    more most my no nor not now of off on once only or other our ours out over own same she
    should so some such than that the their them then there these they this those through to
    too under until up us very was we were what when where which while who whom why will
    with would you your yours please tell show give get got look looking find found use
    using used check cite citing le la les un une des du de d l et ou en au aux ce ces cet
    cette dans sur sous par pour avec sans que qui quoi dont est sont etre ete suis es ai as
    avons avez ont mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs je tu il
    elle nous vous ils elles se y ne pas plus quel quelle quels quelles comment combien
    quand cherche trouve trouver donne montre file files fichier fichiers page pages search
    document documents
    """.split()  # noqa: SIM905
)
_PREFIX_MIN = 5  # a term this long also matches its longer forms (budget -> budgets, budgeting)


def file_id_for(path: str) -> str:
    """The stable id of a workspace-relative path: 32 hex chars (the shape tools accept)."""
    return hashlib.sha256(path.encode()).hexdigest()[:32]


def turn_path(chat_id: str) -> str:
    """The ``files`` path of a chat's conversation passages (never a workspace file)."""
    return f"{CONVERSATION}/{chat_id}"


def search_text(heading_path: str, text: str) -> str:
    """What BM25 and the embedding see for a passage: its headings, then its text."""
    if heading_path and not text.startswith(heading_path.rsplit(" > ", 1)[-1]):
        return f"{heading_path}\n{text}"
    return text


def query_terms(query: str) -> list[str]:
    """The words of *query*, lowercased and once each, filler words out (all of them when only
    filler is left)."""
    words = list(dict.fromkeys(_TOKEN.findall(query.lower())))
    # A query made only of filler ("the file") still searches, rather than finding nothing.
    return [w for w in words if w not in _STOPWORDS] or words


_query_terms = query_terms


def fts_query(query: str) -> str | None:
    """An FTS5 MATCH expression, or ``None`` for a query with no words.

    Natural language in, OR of terms out: filler words are dropped, every term is a quoted
    phrase (so FTS operators in the query are plain text) and longer terms also match their
    longer and plural forms. BM25 then ranks passages by how many, and how rare, the terms are.
    """
    parts = []
    for word in query_terms(query):
        stem = word[:-1] if len(word) > _PREFIX_MIN and word.endswith("s") else word
        parts.append(f'"{stem}"*' if len(stem) >= _PREFIX_MIN else f'"{word}"')
    return " OR ".join(parts) or None


def fuse(lists: Sequence[Sequence[Hashable]]) -> list[tuple[Any, float]]:
    """Reciprocal rank fusion: every item of *lists*, best first, with a score in ``[0, 1]`` (an
    item on top of every list is 1). An item found by several lists rises above one found once.
    """
    ranks: dict[Any, float] = {}
    best = len(lists) / (_RRF_K + 1)
    for ids in lists:
        for rank, item in enumerate(ids, start=1):
            ranks[item] = ranks.get(item, 0.0) + 1.0 / (_RRF_K + rank)
    ordered = sorted(ranks.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(item, score / best) for item, score in ordered]


def vector_bytes(vector: Sequence[float]) -> bytes:
    """A float32 vector in the byte layout sqlite-vec reads."""
    return struct.pack(f"{len(vector)}f", *vector)


@dataclass(frozen=True)
class FileRow:
    """One indexed file."""

    file_id: str
    path: str
    name: str
    kind: str
    size: int
    mtime_ns: int
    explicit: bool
    state: str
    embed_status: str
    embed_reason: str | None
    page_count: int
    text_pages: int
    chunk_count: int
    attempts: int
    last_error: str | None
    next_attempt_at: float
    updated_at: float
    sha256: str | None = None
    parser_version: int = 1


@dataclass(frozen=True)
class ChunkRow:
    """One passage."""

    id: int
    file_id: str
    ord: int
    page_start: int
    page_end: int
    kind: str
    heading_path: str
    text: str
    embed_tag: str | None


@dataclass(frozen=True)
class TurnPassage:
    """One completed turn of a chat, as the ``conversation`` source stores it.

    :param chat_id: The chat (the main Conversation) the turn ran in.
    :param turn_item_id: The person's message item id (what ``session_history`` reads).
    :param date: The turn's date, ``YYYY-MM-DD``.
    :param origin: ``main``.
    :param project: The Project folder the turn ran in, or empty.
    :param person_text: The person's words, clipped (hint snippets quote only these).
    :param text: The passage: the person's text, the final reply and the tool digests.
    :param main_id: The main Conversation of the chat (deleting it removes the chat's passages).
    :param refs: The turn's references, ``{"files": [...], "artifacts": [...], ...}``.
    :param last_item_id: The turn's last item (a runtime notice's turn after it counts as its
        own), so a checkpoint anchor finds the turn it ends.
    :param at: When the turn started (epoch seconds; 0 unknown): orders a chat's turns.
    """

    chat_id: str
    turn_item_id: str
    date: str
    origin: str
    project: str
    person_text: str
    text: str
    main_id: str
    refs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    last_item_id: str = ""
    at: float = 0.0


_TURN_FIELDS = ("chat_id", "turn_item_id", "date", "origin", "project", "person_text")

_FILE_COLUMNS = (
    "file_id, path, name, kind, size, mtime_ns, explicit, state, embed_status, "
    "embed_reason, page_count, text_pages, chunk_count, attempts, last_error, "
    "next_attempt_at, updated_at, sha256, parser_version"
)
_CHUNK_COLUMNS = "id, file_id, ord, page_start, page_end, kind, heading_path, text, embed_tag"


def _file(row: sqlite3.Row | tuple[Any, ...]) -> FileRow:
    values = list(row)
    values[6] = bool(values[6])
    return FileRow(*values)


def _chunk(row: sqlite3.Row | tuple[Any, ...]) -> ChunkRow:
    return ChunkRow(*row)


def _load_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension; ``False`` when this Python cannot (keyword-only then)."""
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.Error):
        return False
    return True


class KnowledgeDb:
    """The index file. Safe to share between threads: each thread gets its own connection."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "index.sqlite3"
        self._local = threading.local()
        self._write = threading.RLock()
        with self._connection() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add the columns an older index file lacks, then the index that needs them."""
        have = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        if "sha256" not in have:
            conn.execute("ALTER TABLE files ADD COLUMN sha256 TEXT")
        if "parser_version" not in have:
            # Files read by an older version count as version 1: only a kind whose reader has
            # changed since (see ``indexer.PARSER_VERSIONS``) is read again.
            conn.execute("ALTER TABLE files ADD COLUMN parser_version INTEGER NOT NULL DEFAULT 1")
        conn.execute("CREATE INDEX IF NOT EXISTS files_sha256 ON files (sha256)")
        turns = {row[1] for row in conn.execute("PRAGMA table_info(turns)")}
        if "last_item_id" not in turns:
            # Passages stored before have neither: the next write of the turn fills them in.
            conn.execute("ALTER TABLE turns ADD COLUMN last_item_id TEXT NOT NULL DEFAULT ''")
        if "at" not in turns:
            conn.execute("ALTER TABLE turns ADD COLUMN at REAL NOT NULL DEFAULT 0")

    # ------------------------------------------------------------------ plumbing

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = None
        self._local.vec = _load_vec(conn)
        return conn

    @contextmanager
    def _connection(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._open()
        yield conn

    @contextmanager
    def _tx(self) -> Any:
        """A write transaction (one writer at a time inside this process)."""
        with self._write, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @property
    def vectors_supported(self) -> bool:
        """Whether the sqlite-vec extension loaded (otherwise every search is keyword-only)."""
        with self._connection():
            return bool(getattr(self._local, "vec", False))

    def close(self) -> None:
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------ meta

    def meta(self, key: str) -> str | None:
        """A stored setting."""
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        """Store (or, with ``None``, remove) a setting."""
        with self._tx() as conn:
            self._set_meta(conn, key, value)

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str | None) -> None:
        if value is None:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def vector_tag(self) -> str | None:
        """The embedding model tag the stored vectors were made with."""
        return self.meta("vec_tag")

    def vector_dims(self) -> int | None:
        """The size of the stored vectors, or ``None`` when there are none."""
        value = self.meta("vec_dims")
        return int(value) if value else None

    # ------------------------------------------------------------------ files

    def get_file(self, file_id: str) -> FileRow | None:
        """One file by id."""
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE file_id = ?", (file_id,)
            ).fetchone()
        return _file(row) if row else None

    def file_by_path(self, path: str) -> FileRow | None:
        """One file by its workspace-relative path."""
        return self.get_file(file_id_for(path))

    def files(self, *, turns: bool = False) -> list[FileRow]:
        """Every file, by path. The chats' rows of conversation passages only with *turns*."""
        where = "" if turns else f"WHERE kind != '{KIND_TURN}' "
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM files {where}ORDER BY path"
            ).fetchall()
        return [_file(r) for r in rows]

    def upsert_file(
        self,
        *,
        path: str,
        name: str,
        kind: str,
        size: int,
        mtime_ns: int,
        explicit: bool = False,
    ) -> FileRow:
        """Record a file seen on disk and queue it. Its old passages go; a new state starts."""
        fid = file_id_for(path)
        now = time.time()
        with self._tx() as conn:
            self._delete_chunks(conn, fid)
            conn.execute(
                "INSERT INTO files (file_id, path, name, kind, size, mtime_ns, explicit, state, "
                "updated_at, parser_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT (file_id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
                "size = excluded.size, mtime_ns = excluded.mtime_ns, "
                "explicit = MAX(files.explicit, excluded.explicit), state = excluded.state, "
                "embed_status = 'keyword', embed_reason = NULL, page_count = 0, text_pages = 0, "
                "chunk_count = 0, attempts = 0, last_error = NULL, next_attempt_at = 0, "
                "sha256 = NULL, parser_version = 0, updated_at = excluded.updated_at",
                (fid, path, name, kind, size, mtime_ns, int(explicit), STATE_QUEUED, now),
            )
        row = self.get_file(fid)
        assert row is not None
        return row

    def ready_files_with_hash(self, sha256: str) -> list[FileRow]:
        """Files whose bytes had this SHA-256 when they were read and that are searchable now."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE sha256 = ? AND state = ? ORDER BY path",
                (sha256, STATE_READY),
            ).fetchall()
        return [_file(r) for r in rows]

    def update_file(self, file_id: str, **fields: Any) -> None:
        """Set columns of one file (``updated_at`` is always refreshed)."""
        fields["updated_at"] = time.time()
        names = ", ".join(f"{name} = ?" for name in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE files SET {names} WHERE file_id = ?", (*fields.values(), file_id)
            )

    def delete_file(self, file_id: str) -> None:
        """Forget a file: its row, passages and both index entries."""
        with self._tx() as conn:
            self._delete_chunks(conn, file_id)
            conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))

    def next_due(self, now: float | None = None) -> FileRow | None:
        """The oldest file with work to do whose retry delay has passed."""
        moment = time.time() if now is None else now
        marks = ", ".join("?" for _ in PENDING_STATES)
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE state IN ({marks}) "
                "AND next_attempt_at <= ? ORDER BY updated_at LIMIT 1",
                (*PENDING_STATES, moment),
            ).fetchone()
        return _file(row) if row else None

    def pending_count(self) -> int:
        """How many files still have a pass to run."""
        marks = ", ".join("?" for _ in PENDING_STATES)
        with self._connection() as conn:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM files WHERE state IN ({marks}) "
                    f"AND kind != '{KIND_TURN}'",
                    PENDING_STATES,
                ).fetchone()[0]
            )

    def pending_names(self, limit: int = 10) -> list[str]:
        """File names that still have a pass to run (path order), at most *limit*."""
        marks = ", ".join("?" for _ in PENDING_STATES)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT name FROM files WHERE state IN ({marks}) AND kind != '{KIND_TURN}' "
                "ORDER BY path LIMIT ?",
                (*PENDING_STATES, limit),
            ).fetchall()
        return [str(r[0]) for r in rows]

    # ------------------------------------------------------------------ conversation passages

    def index_turns(
        self, passages: Iterable[TurnPassage], *, cursor: tuple[str, str] | None = None
    ) -> int:
        """Store turn passages, keyword-searchable at once; idempotent on ``turn_item_id``.

        A turn already stored with the same text is left as it is (an empty project, and its
        last item and start, are filled in); one whose text changed (stored before its reply
        was) is replaced. The chat's row goes back to ``text_ready`` so the background pass
        embeds what is new. *cursor* is a ``(meta key, value)`` saved in the same transaction
        (backfill progress).

        :returns: How many passages were written.
        """
        written: set[str] = set()
        count = 0
        now = time.time()
        with self._tx() as conn:
            for p in passages:
                old = conn.execute(
                    "SELECT t.chunk_id, c.text, t.project, t.last_item_id, t.at FROM turns t "
                    "JOIN chunks c ON c.id = t.chunk_id WHERE t.turn_item_id = ?",
                    (p.turn_item_id,),
                ).fetchone()
                if old is not None and old[1] == p.text:
                    if p.last_item_id and (old[3], old[4]) != (p.last_item_id, p.at):
                        conn.execute(
                            "UPDATE turns SET last_item_id = ?, at = ? WHERE chunk_id = ?",
                            (p.last_item_id, p.at, old[0]),
                        )
                    if p.project and not old[2]:
                        conn.execute(
                            "UPDATE turns SET project = ? WHERE chunk_id = ?", (p.project, old[0])
                        )
                        self._write_refs(conn, old[0], {"project": [p.project]})
                    continue
                if old is not None:
                    self._drop_chunk_ids(conn, [old[0]])
                fid = file_id_for(turn_path(p.chat_id))
                conn.execute(
                    "INSERT INTO files (file_id, path, name, kind, state, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (file_id) DO NOTHING",
                    (fid, turn_path(p.chat_id), p.chat_id, KIND_TURN, STATE_TEXT_READY, now),
                )
                ord_ = conn.execute(
                    "SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE file_id = ?", (fid,)
                ).fetchone()[0]
                chunk_id = conn.execute(
                    "INSERT INTO chunks (file_id, ord, page_start, page_end, kind, heading_path, "
                    "text) VALUES (?, ?, 1, 1, ?, '', ?)",
                    (fid, ord_, KIND_TURN, p.text),
                ).lastrowid
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, body) VALUES (?, ?)", (chunk_id, p.text)
                )
                project = p.project or (old[2] if old is not None else "")
                conn.execute(
                    "INSERT INTO turns (chunk_id, turn_item_id, chat_id, main_id, date, origin, "
                    "project, person_text, last_item_id, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk_id,
                        p.turn_item_id,
                        p.chat_id,
                        p.main_id,
                        p.date,
                        p.origin,
                        project,
                        p.person_text,
                        p.last_item_id,
                        p.at,
                    ),
                )
                self._write_refs(
                    conn, chunk_id, {**p.refs, "project": [project] if project else []}
                )
                written.add(fid)
                count += 1
            for fid in written:
                conn.execute(
                    "UPDATE files SET state = ?, attempts = 0, next_attempt_at = 0, "
                    "last_error = NULL, updated_at = ?, "
                    "chunk_count = (SELECT COUNT(*) FROM chunks WHERE file_id = ?) "
                    "WHERE file_id = ?",
                    (STATE_TEXT_READY, now, fid, fid),
                )
            if cursor is not None:
                self._set_meta(conn, *cursor)
        return count

    def turn_meta(self, chunk_ids: Sequence[int]) -> dict[int, dict[str, str]]:
        """``chunk id -> {chat_id, turn_item_id, date, origin, project, person_text}``."""
        if not chunk_ids:
            return {}
        marks = ", ".join("?" for _ in chunk_ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT chunk_id, {', '.join(_TURN_FIELDS)} FROM turns "
                f"WHERE chunk_id IN ({marks})",
                list(chunk_ids),
            ).fetchall()
        return {r[0]: dict(zip(_TURN_FIELDS, r[1:], strict=True)) for r in rows}

    def turn_chunks(self, turn_item_ids: Sequence[str]) -> dict[str, int]:
        """``turn item id -> chunk id`` of the stored turns among *turn_item_ids*."""
        if not turn_item_ids:
            return {}
        marks = ", ".join("?" for _ in turn_item_ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT turn_item_id, chunk_id FROM turns WHERE turn_item_id IN ({marks})",
                list(turn_item_ids),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def turn_ending(self, chat_id: str, item_id: str) -> int | None:
        """The chunk id of *chat_id*'s stored turn that starts or ends at *item_id*, if any."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT chunk_id FROM turns WHERE chat_id = ? AND (last_item_id = ? OR "
                "turn_item_id = ?) ORDER BY chunk_id DESC LIMIT 1",
                (chat_id, item_id, item_id),
            ).fetchone()
        return int(row[0]) if row else None

    def turn_order(self, chunk_ids: Sequence[int]) -> dict[int, tuple[str, float, int]]:
        """``chunk id -> (date, start, chunk id)``: sorts a chat's turns in the order they ran (a
        passage stored without its start falls back to its day, then the order it was stored)."""
        if not chunk_ids:
            return {}
        marks = ", ".join("?" for _ in chunk_ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT chunk_id, date, at FROM turns WHERE chunk_id IN ({marks})",
                list(chunk_ids),
            ).fetchall()
        return {r[0]: (str(r[1]), float(r[2] or 0), int(r[0])) for r in rows}

    def chats_of(self, main_id: str) -> list[str]:
        """The chats with passages that belong to the main Conversation *main_id*."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT chat_id FROM turns WHERE main_id = ? ORDER BY chat_id", (main_id,)
            ).fetchall()
        return [str(r[0]) for r in rows]

    # ------------------------------------------------------------------ references

    @staticmethod
    def _write_refs(
        conn: sqlite3.Connection, chunk_id: int, refs: Mapping[str, Sequence[str]]
    ) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO refs (chunk_id, key, value) VALUES (?, ?, ?)",
            [(chunk_id, key, value) for key, values in refs.items() for value in values],
        )

    def set_refs(self, refs: Mapping[int, Mapping[str, Sequence[str]]]) -> None:
        """Store passages' references: ``chunk id -> {key: [values]}``."""
        with self._tx() as conn:
            for chunk_id, found in refs.items():
                self._write_refs(conn, chunk_id, found)

    def refs_of(self, chunk_ids: Sequence[int]) -> dict[int, dict[str, list[str]]]:
        """``chunk id -> {key: [values]}`` for the passages that carry references."""
        if not chunk_ids:
            return {}
        marks = ", ".join("?" for _ in chunk_ids)
        out: dict[int, dict[str, list[str]]] = {}
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT chunk_id, key, value FROM refs WHERE chunk_id IN ({marks}) "
                "ORDER BY rowid",
                list(chunk_ids),
            ).fetchall()
        for chunk_id, key, value in rows:
            out.setdefault(chunk_id, {}).setdefault(key, []).append(value)
        return out

    def chunks_with_ref(self, key: str, value: str) -> set[int]:
        """The passages that carry the reference ``key:value``."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT chunk_id FROM refs WHERE key = ? AND value = ?", (key, value)
            ).fetchall()
        return {r[0] for r in rows}

    def delete_chats(self, chat_ids: Iterable[str], *, meta_keys: Sequence[str] = ()) -> int:
        """Remove every passage of these chats, and the meta keys *meta_keys* (their progress).

        :returns: How many passages were removed.
        """
        removed = 0
        with self._tx() as conn:
            for chat_id in dict.fromkeys(chat_ids):
                fid = file_id_for(turn_path(chat_id))
                removed += conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE file_id = ?", (fid,)
                ).fetchone()[0]
                self._delete_chunks(conn, fid)
                conn.execute("DELETE FROM files WHERE file_id = ?", (fid,))
            for key in meta_keys:
                self._set_meta(conn, key, None)
        return removed

    # ------------------------------------------------------------------ passages

    def _delete_chunks(self, conn: sqlite3.Connection, file_id: str) -> None:
        ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))]
        self._drop_chunk_ids(conn, ids)

    def _drop_chunk_ids(self, conn: sqlite3.Connection, ids: Sequence[int]) -> None:
        if not ids:
            return
        marks = ", ".join("?" for _ in ids)
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", list(ids))
        if self._has_vec_table(conn) and self.vectors_supported:
            conn.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", list(ids))
        conn.execute(f"DELETE FROM chunks WHERE id IN ({marks})", list(ids))

    @staticmethod
    def _has_vec_table(conn: sqlite3.Connection) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec' AND type = 'table'"
            ).fetchone()
            is not None
        )

    def replace_chunks(self, file_id: str, chunks: Iterable[Chunk]) -> list[ChunkRow]:
        """Store a file's passages (keyword-searchable at once) in place of any older ones."""
        with self._tx() as conn:
            self._delete_chunks(conn, file_id)
            for ord_, chunk in enumerate(chunks):
                cursor = conn.execute(
                    "INSERT INTO chunks (file_id, ord, page_start, page_end, kind, heading_path, "
                    "text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        file_id,
                        ord_,
                        chunk.page_start,
                        chunk.page_end,
                        chunk.kind,
                        chunk.heading_path,
                        chunk.text,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (rowid, body) VALUES (?, ?)",
                    (cursor.lastrowid, search_text(chunk.heading_path, chunk.text)),
                )
        return self.chunks_for(file_id)

    def chunks_for(self, file_id: str) -> list[ChunkRow]:
        """A file's passages in reading order."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE file_id = ? ORDER BY ord", (file_id,)
            ).fetchall()
        return [_chunk(r) for r in rows]

    def page_chunks(self, file_id: str, page: int) -> list[ChunkRow]:
        """The passages that start on one page."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE file_id = ? AND page_start = ? "
                "ORDER BY ord",
                (file_id, page),
            ).fetchall()
        return [_chunk(r) for r in rows]

    # ------------------------------------------------------------------ vectors

    def set_vectors(self, vectors: dict[int, Sequence[float]], tag: str, dims: int) -> bool:
        """Store embeddings (``chunk id -> vector``) made with the model *tag*.

        A different tag or size than what is stored drops every older vector first (they are
        not comparable); the caller re-embeds the other files (:meth:`stale_files`).

        :returns: ``False`` when sqlite-vec is not available (nothing stored).
        """
        if not self.vectors_supported:
            return False
        with self._tx() as conn:
            if self.vector_tag() != tag or self.vector_dims() != dims:
                self._reset_vectors(conn, dims)
                self._set_meta(conn, "vec_tag", tag)
                self._set_meta(conn, "vec_dims", str(dims))
            for chunk_id, vector in vectors.items():
                conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (chunk_id,))
                conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (chunk_id, vector_bytes(vector)),
                )
                conn.execute("UPDATE chunks SET embed_tag = ? WHERE id = ?", (tag, chunk_id))
        return True

    def _reset_vectors(self, conn: sqlite3.Connection, dims: int) -> None:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        conn.execute(
            f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{int(dims)}] "
            "distance_metric=cosine)"
        )
        conn.execute("UPDATE chunks SET embed_tag = NULL")

    def stale_files(self, tag: str | None) -> list[FileRow]:
        """Ready files whose passages lack a vector of the current model (*tag*)."""
        if tag is None:
            return []
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_FILE_COLUMNS} FROM files f WHERE state = ? AND EXISTS "
                "(SELECT 1 FROM chunks c WHERE c.file_id = f.file_id "
                "AND COALESCE(c.embed_tag, '') != ?)",
                (STATE_READY, tag),
            ).fetchall()
        return [_file(r) for r in rows]

    # ------------------------------------------------------------------ search

    def search(
        self,
        query: str,
        *,
        limit: int,
        file_ids: Sequence[str] | None = None,
        query_vector: Sequence[float] | None = None,
        keyword: bool = True,
        turns: bool = False,
    ) -> list[tuple[ChunkRow, float]]:
        """Best passages, best first, with a score in ``[0, 1]`` (the top hit of a list is 1).

        With *query_vector* the BM25 and vector rankings are fused (reciprocal rank fusion);
        without it BM25 alone ranks; ``keyword=False`` with a vector ranks by vector alone.
        *file_ids* restricts the search to those files. Conversation passages are searched only
        with ``turns=True`` (and then nothing else): a file search never returns one.
        """
        lists = self.rankings(
            query,
            limit=limit,
            file_ids=file_ids,
            query_vector=query_vector,
            keyword=keyword,
            turns=turns,
        )
        ordered = fuse(lists)[:limit]
        chunks = self.chunks_by_ids([chunk_id for chunk_id, _ in ordered])
        return [(chunks[i], score) for i, score in ordered if i in chunks]

    def rankings(
        self,
        query: str,
        *,
        limit: int,
        file_ids: Sequence[str] | None = None,
        query_vector: Sequence[float] | None = None,
        keyword: bool = True,
        turns: bool = False,
    ) -> list[list[int]]:
        """The BM25 and vector rankings of passage ids for *query*, before :func:`fuse` (see
        :meth:`search`)."""
        wanted = set(file_ids) if file_ids is not None else None
        if wanted is not None and not wanted:
            return []
        candidates = max(limit * _CANDIDATE_FACTOR, _MIN_CANDIDATES)
        lists: list[list[int]] = [self._bm25(query, candidates, wanted, turns)] if keyword else []
        if query_vector is not None and self.vector_dims() == len(query_vector):
            lists.append(self._nearest(query_vector, candidates, wanted, turns))
        return lists

    def _bm25(self, query: str, limit: int, wanted: set[str] | None, turns: bool) -> list[int]:
        match = fts_query(query)
        if match is None:
            return []
        sql = (
            "SELECT chunks_fts.rowid FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
            f"WHERE chunks_fts MATCH ? AND c.kind {'=' if turns else '!='} ?"
        )
        params: list[Any] = [match, KIND_TURN]
        if wanted is not None:
            sql += f" AND c.file_id IN ({', '.join('?' for _ in wanted)})"
            params.extend(sorted(wanted))
        sql += " ORDER BY chunks_fts.rank LIMIT ?"
        params.append(limit)
        with self._connection() as conn:
            try:
                return [r[0] for r in conn.execute(sql, params)]
            except sqlite3.OperationalError:
                return []

    def _nearest(
        self, vector: Sequence[float], limit: int, wanted: set[str] | None, turns: bool
    ) -> list[int]:
        if not self.vectors_supported:
            return []
        with self._connection() as conn:
            if not self._has_vec_table(conn):
                return []
            # vec0 has no row filter: ask for more neighbours, then keep the ones that count
            # (file passages or conversation passages, and only *wanted* files).
            k = min(limit * 8, 4096)
            rows = conn.execute(
                "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (vector_bytes(vector), k),
            ).fetchall()
            ids = [r[0] for r in rows]
            if not ids:
                return ids
            marks = ", ".join("?" for _ in ids)
            sql = (
                f"SELECT id FROM chunks WHERE id IN ({marks}) AND kind {'=' if turns else '!='} ?"
            )
            params: list[Any] = [*ids, KIND_TURN]
            if wanted is not None:
                sql += f" AND file_id IN ({', '.join('?' for _ in wanted)})"
                params.extend(sorted(wanted))
            keep = {r[0] for r in conn.execute(sql, params)}
        return [i for i in ids if i in keep][:limit]

    def chunks_by_ids(self, ids: Sequence[int]) -> dict[int, ChunkRow]:
        """Passages by id."""
        if not ids:
            return {}
        marks = ", ".join("?" for _ in ids)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE id IN ({marks})", list(ids)
            ).fetchall()
        return {r[0]: _chunk(r) for r in rows}
