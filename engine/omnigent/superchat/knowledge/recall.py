"""Recall: one search over the person's files, journal, conversation turns and claims.

ADR 0016, contract C2; ``docs/context.md`` "Recall". The ``recall`` tool (the
pull) and the hint both call :func:`recall_search`. Each sub-query runs BM25 and vectors over
the host's index (keyword-only when the owner cannot embed); claims about the person come
from the engine through the caller's *claims* search. Every ranking of every sub-query is fused
with the index's reciprocal rank fusion, so a passage that answers two sub-queries rises. A
conversation hit then loses half its score every 90 days (never all of it), and near-duplicates
collapse into the newest, with the older dates in ``earlier``. Filters (``project``, ``file``,
``chat``, ``since``, ``until``) match the passages' metadata (C1, C5) and dates; with a Project,
file or chat and no query, the matching passages come back newest first.

Every hit is dated and carries one read id for ``recall_get``: ``i_<turn item id>`` (a turn),
``ep_<passage id>`` (a journal passage), ``f_<file id>@<page>`` (a file page), ``c_<claim id>``
(a claim).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from omnigent.memory.service import (
    _RECENCY_BOOST_HALFLIFE_SECONDS,
    _SAME_TEXT_JACCARD_THRESHOLD,
    _jaccard,
)
from omnigent.superchat.knowledge.db import (
    ChunkRow,
    FileRow,
    _query_terms,
    file_id_for,
    fuse,
    turn_path,
)
from omnigent.superchat.knowledge.embedder import EmbeddingUnavailable
from omnigent.superchat.knowledge.indexer import AmbiguousFile, Indexer

_logger = logging.getLogger(__name__)

SOURCES = frozenset({"files", "journal", "conversation", "memory"})
MAX_QUERIES = 3
#: Fused candidates looked at before recency, filters and collapse cut them to the limit.
_POOL = 50
_FILTERED_POOL = 200
#: Claims asked of the engine per sub-query (its route's ceiling is 20).
_CLAIMS_PER_QUERY = 10
_TITLE_CHARS = 80
_SNIPPET_CHARS = 1600
_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})")
_JOURNAL_LINE_DATE = re.compile(r"^\s*[-*]\s*(\d{4}-\d{2}-\d{2})")
#: A journal line's trailing comment (chat id and refs): metadata, not what happened.
_COMMENT = re.compile(r"\s*<!--.*?-->\s*$")

#: ``(query, limit) -> claims, best first`` (the ``memory_search`` result rows).
ClaimSearch = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class RecallHit:
    """One dated pointer, with the ids to read it in full."""

    source: str
    date: str
    title: str
    snippet: str
    score: float
    project: str = ""
    earlier: list[str] = field(default_factory=list)
    #: The id ``recall_get`` reads it by.
    id: str = ""
    chat_id: str | None = None
    turn_item_id: str | None = None
    file_id: str | None = None
    page: int | None = None
    path: str | None = None
    line: int | None = None
    claim_id: str | None = None
    #: What a journal line or a turn touched: ``files``, ``artifacts`` (C5).
    refs: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Filters:
    """What the passages must match: Project slug, file path or name, chat id, ISO dates."""

    project: str | None = None
    file: str | None = None
    chat: str | None = None
    since: str | None = None
    until: str | None = None

    @property
    def by_object(self) -> bool:
        return bool(self.project or self.file or self.chat)

    @property
    def any(self) -> bool:
        return self.by_object or bool(self.since or self.until)


async def recall_search(
    queries: list[str],
    *,
    sources: set[str],
    recency: bool = True,
    limit: int = 8,
    exclude_chat_after: dict[str, str] | None = None,
    project: str | None = None,
    file: str | None = None,
    chat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    indexer: Indexer | None = None,
    claims: ClaimSearch | None = None,
) -> list[RecallHit]:
    """The best hits for one to three sub-queries over *sources*, best first.

    :param queries: One to three sub-queries, each a different angle; none with a Project, file
        or chat filter lists the matching passages newest first.
    :param sources: A subset of :data:`SOURCES`; ``journal`` is the ``memory/`` folder.
    :param recency: Let conversation hits lose rank with age (off for questions about the past).
    :param limit: Most hits returned.
    :param exclude_chat_after: Chat id -> item id after which that chat's turns are left out
        (the current epoch): the first or last item of a stored turn, as a checkpoint's anchor
        (the last item it retired) is. An empty id leaves the whole chat out (no checkpoint yet);
        an item that starts or ends no stored turn leaves the chat whole.
    :param project: Only what belongs to this Project (folder slug).
    :param file: Only this file, and the journal lines and turns that touched it (newest first).
    :param chat: Only this chat's turns.
    :param since: Only hits dated on or after this ISO date.
    :param until: Only hits dated on or before this ISO date.
    :param indexer: The host's indexer (default: the runner's service).
    :param claims: How to search claims in the engine; without it ``memory`` finds nothing.
    """
    hits, _ = await recall_with_reason(
        queries,
        sources=sources,
        recency=recency,
        limit=limit,
        exclude_chat_after=exclude_chat_after,
        project=project,
        file=file,
        chat=chat,
        since=since,
        until=until,
        indexer=indexer,
        claims=claims,
    )
    return hits


async def recall_with_reason(
    queries: list[str],
    *,
    sources: set[str],
    recency: bool = True,
    limit: int = 8,
    exclude_chat_after: dict[str, str] | None = None,
    project: str | None = None,
    file: str | None = None,
    chat: str | None = None,
    since: str | None = None,
    until: str | None = None,
    indexer: Indexer | None = None,
    claims: ClaimSearch | None = None,
) -> tuple[list[RecallHit], str | None]:
    """:func:`recall_search`, plus why the search was keyword-only (``None``: it was not)."""
    filters = Filters(project or None, file or None, chat or None, since or None, until or None)
    wanted = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
    if len(wanted) > MAX_QUERIES or not (wanted or filters.by_object):
        raise ValueError("recall takes one to three queries, or a project, file or chat")
    if not sources or not set(sources) <= SOURCES:
        raise ValueError(f"sources must be some of {', '.join(sorted(SOURCES))}")
    if indexer is None:
        from omnigent.superchat.knowledge.runtime import get_runtime

        indexer = get_runtime().indexer
    # Claims carry no Project, file or chat: an object filter leaves them out.
    fetch_claims = claims if "memory" in sources and wanted and not filters.by_object else None
    local, found_claims = await asyncio.gather(
        asyncio.to_thread(_passage_rankings, indexer, wanted, set(sources), filters),
        _claim_rankings(fetch_claims, wanted),
    )
    lists, allowed, reason = local
    claim_rows: dict[str, dict[str, Any]] = {}
    for ranking in found_claims:
        lists.append([("claim", c["claim_id"]) for c in ranking])
        claim_rows.update({c["claim_id"]: c for c in ranking})
    pool = _FILTERED_POOL if filters.any else _POOL
    fused = (
        fuse(lists)[:pool] if wanted else [(("passage", i), 1.0) for i in sorted(allowed or ())]
    )
    hits = await asyncio.to_thread(
        _hits, indexer, fused, allowed, claim_rows, wanted, exclude_chat_after or {}
    )
    hits = [h for h in hits if _in_period(h.date, filters)]
    if recency and wanted:
        now = time.time()
        hits = [
            replace(h, score=h.score * recency_factor(h.date, now))
            if h.source == "conversation"
            else h
            for h in hits
        ]
    hits.sort(key=lambda h: -h.score)
    hits = collapse(hits)
    if not wanted or filters.file:  # what happened to an object reads as a timeline
        hits.sort(key=lambda h: h.date, reverse=True)
    return [replace(h, score=round(h.score, 4)) for h in hits[: max(1, limit)]], reason


def recency_factor(date: str, now: float) -> float:
    """How much of a conversation hit's score is left at its age: half every 90 days (the
    claims' half-life), never zero."""
    try:
        moment = datetime.fromisoformat(date)
    except ValueError:
        return 1.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    age = max(now - moment.timestamp(), 0.0)
    return max(0.5 ** (age / _RECENCY_BOOST_HALFLIFE_SECONDS), 1e-9)


def collapse(hits: list[RecallHit]) -> list[RecallHit]:
    """Near-duplicates (the claims dedupe's word-overlap test) become one hit: the newest, at the
    best place any of them had, with the older dates in ``earlier``."""
    kept: list[RecallHit] = []
    for hit in hits:
        twin = next(
            (
                i
                for i, k in enumerate(kept)
                if _jaccard(k.snippet, hit.snippet) >= _SAME_TEXT_JACCARD_THRESHOLD
            ),
            None,
        )
        if twin is None:
            kept.append(hit)
            continue
        first = kept[twin]
        newer, older = (hit, first) if hit.date > first.date else (first, hit)
        dates = {older.date, *older.earlier, *newer.earlier} - {newer.date}
        kept[twin] = replace(
            newer, score=max(first.score, hit.score), earlier=sorted(dates, reverse=True)
        )
    return kept


def same_file(ref: str, wanted: str) -> bool:
    """Whether a workspace path is the file a filter names (its path, or its plain name)."""
    ref, wanted = ref.strip("/"), wanted.strip().strip("/")
    return ref == wanted or ("/" not in wanted and PurePosixPath(ref).name == wanted)


# ---------------------------------------------------------------------- rankings


def _passage_rankings(
    indexer: Indexer, queries: list[str], sources: set[str], filters: Filters
) -> tuple[list[list[Any]], set[int] | None, str | None]:
    """BM25 and vector rankings of every sub-query over the index's sources, and the passages a
    Project, file or chat filter allows (``None``: all of them)."""
    db = indexer.db
    rows = [r for r in db.files() if ("journal" if _is_journal(r) else "files") in sources]
    # ``None``: every file (both file sources) or every chat's passages.
    ids = None if {"files", "journal"} <= sources else [r.file_id for r in rows]
    turn_ids: list[str] | None = None
    allowed = _allowed(indexer, rows, sources, filters) if filters.by_object else None
    if allowed is not None:
        owners = {c.file_id for c in db.chunks_by_ids(sorted(allowed)).values()}
        ids = [r.file_id for r in rows if r.file_id in owners]
        turn_ids = sorted(owners - set(ids))
    if not queries:
        return [], allowed, None
    search_files = bool(rows) and (ids is None or bool(ids))
    vectors: list[Any] = [None] * len(queries)
    reason = None
    try:
        vectors = indexer.embedder.embed(list(queries)).vectors
    except EmbeddingUnavailable as exc:
        reason = exc.reason
    lists: list[list[Any]] = []
    pool = _FILTERED_POOL if filters.any else _POOL
    for query, vector in zip(queries, vectors, strict=True):
        found = (
            db.rankings(query, limit=pool, file_ids=ids, query_vector=vector)
            if search_files
            else []
        )
        if "conversation" in sources:
            found += db.rankings(
                query, limit=pool, file_ids=turn_ids, query_vector=vector, turns=True
            )
        lists += [[("passage", chunk_id) for chunk_id in ranking] for ranking in found]
    return lists, allowed, reason


def _is_journal(row: FileRow) -> bool:
    return row.path.startswith("memory/")


def _allowed(
    indexer: Indexer, rows: list[FileRow], sources: set[str], filters: Filters
) -> set[int]:
    """The passages that match the Project, file and chat filters: a file by its folder and name;
    a journal passage or a turn by its references (C5) and, for a turn, its chat."""
    db = indexer.db
    allowed: set[int] = set()
    if not filters.chat:
        for row in rows:
            if _is_journal(row):
                continue
            if filters.project and not row.path.startswith(f"projects/{filters.project}/"):
                continue
            if filters.file and not same_file(row.path, filters.file):
                continue
            allowed.update(c.id for c in db.chunks_for(row.file_id))
    wanted: list[set[int]] = []
    if filters.project:
        wanted.append(db.chunks_with_ref("project", filters.project))
    if filters.file:
        wanted.append(db.chunks_with_ref("files", _file_path(indexer, filters.file)))
    if filters.chat:
        wanted.append({c.id for c in db.chunks_for(file_id_for(turn_path(filters.chat)))})
    by_refs = set.intersection(*wanted)
    turns = db.turn_meta(sorted(by_refs))
    journals = {r.file_id for r in rows if _is_journal(r)}
    for chunk in db.chunks_by_ids(sorted(by_refs)).values():
        if (chunk.id in turns and "conversation" in sources) or chunk.file_id in journals:
            allowed.add(chunk.id)
    return allowed


def _file_path(indexer: Indexer, file: str) -> str:
    """The workspace path a ``file`` filter names (a plain name of an indexed file resolves)."""
    try:
        row = indexer.resolve(None, file)
    except AmbiguousFile:
        row = None
    return row.path if row is not None else file.strip().strip("/")


def _in_period(date: str, filters: Filters) -> bool:
    day = date[:10]
    return (not filters.since or day >= filters.since[:10]) and (
        not filters.until or day <= filters.until[:10]
    )


async def _claim_rankings(
    claims: ClaimSearch | None, queries: list[str]
) -> list[list[dict[str, Any]]]:
    """Each sub-query's claims, best first; a failed search finds nothing."""
    if claims is None:
        return []

    async def one(query: str) -> list[dict[str, Any]]:
        try:
            rows = await claims(query, _CLAIMS_PER_QUERY)
        except Exception:  # claims are one source; the others still answer
            _logger.warning("recall: claim search failed", exc_info=True)
            return []
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("claim_id"), str)]

    return list(await asyncio.gather(*(one(q) for q in queries)))


# ---------------------------------------------------------------------- hits


def _hits(
    indexer: Indexer,
    fused: list[tuple[Any, float]],
    allowed: set[int] | None,
    claim_rows: dict[str, dict[str, Any]],
    queries: list[str],
    exclude: dict[str, str],
) -> list[RecallHit]:
    db = indexer.db
    keys = [key for (kind, key), _ in fused if kind == "passage"]
    chunks = db.chunks_by_ids([k for k in keys if allowed is None or k in allowed])
    turns = db.turn_meta(sorted(chunks))
    owners = {c.file_id for c in chunks.values() if c.id not in turns}
    rows = {fid: row for fid in owners if (row := db.get_file(fid))}
    refs = db.refs_of(sorted(chunks))
    cutoffs = _cutoffs(db, exclude)
    order = db.turn_order([c for c, t in turns.items() if t["chat_id"] in cutoffs])
    terms = {t for q in queries for t in _query_terms(q)}
    journals: dict[str, list[str]] = {}
    out: list[RecallHit] = []
    for (kind, key), score in fused:
        if kind == "claim":
            out.append(_claim_hit(claim_rows[key], score))
            continue
        chunk = chunks.get(key)
        if chunk is None:
            continue
        ref = refs.get(chunk.id, {})
        turn = turns.get(chunk.id)
        if turn is not None:
            cutoff = cutoffs.get(turn["chat_id"])
            if cutoff is None or order[chunk.id] <= cutoff:
                out.append(_turn_hit(turn, chunk.text, score, ref))
            continue
        row = rows.get(chunk.file_id)
        if row is None:
            continue
        if row.path.startswith("memory/"):
            out.append(_journal_hit(indexer, row, chunk, score, terms, ref, journals))
            continue
        out.append(
            RecallHit(
                source="files",
                date=_mtime_date(row),
                title=row.name,
                snippet=chunk.text[:_SNIPPET_CHARS],
                score=score,
                id=f"f_{row.file_id}@{chunk.page_start}",
                file_id=row.file_id,
                page=chunk.page_start,
                path=row.path,
            )
        )
    return out


def _cutoffs(db: Any, exclude: dict[str, str]) -> dict[str, tuple[str, float, int]]:
    """Chat id -> the order (``turn_order``) of the turn the cutoff item starts or ends; that
    chat's turns ordered after it are left out. An empty item id leaves out every turn."""
    found = {chat: db.turn_ending(chat, item) for chat, item in exclude.items() if item}
    order = db.turn_order([c for c in found.values() if c is not None])
    cutoffs = {chat: order[c] for chat, c in found.items() if c in order}
    cutoffs.update({chat: ("", 0.0, 0) for chat, item in exclude.items() if not item})
    return cutoffs


def _object_refs(ref: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: ref[k] for k in ("files", "artifacts") if ref.get(k)}


def _turn_hit(
    turn: dict[str, Any], text: str, score: float, ref: dict[str, list[str]]
) -> RecallHit:
    person = " ".join(str(turn.get("person_text") or "").split())
    return RecallHit(
        source="conversation",
        date=str(turn["date"])[:10],
        title=person[:_TITLE_CHARS],
        snippet=text[:_SNIPPET_CHARS],
        score=score,
        project=str(turn.get("project") or next(iter(ref.get("project", [])), "")),
        id=f"i_{turn['turn_item_id']}",
        chat_id=turn["chat_id"],
        turn_item_id=turn["turn_item_id"],
        refs=_object_refs(ref),
    )


def _claim_hit(claim: dict[str, Any], score: float) -> RecallHit:
    seen = claim.get("last_confirmed") or claim.get("first_seen")
    date = (
        datetime.fromtimestamp(float(seen), UTC).date().isoformat()
        if isinstance(seen, (int, float))
        else ""
    )
    return RecallHit(
        source="memory",
        date=date,
        title=str(claim.get("kind") or "claim"),
        snippet=str(claim.get("text") or ""),
        score=score,
        id=f"c_{claim['claim_id']}",
        claim_id=claim["claim_id"],
    )


def journal_line(
    indexer: Indexer,
    row: FileRow,
    chunk: ChunkRow,
    terms: set[str],
    cache: dict[str, list[str]] | None = None,
) -> tuple[str, int | None, str]:
    """A ``memory/`` passage's line that best matches *terms*: ``(text, line number, date)``."""
    lines = [
        ln.strip()
        for ln in chunk.text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    best = max(
        lines,
        key=lambda ln: (sum(t in ln.lower() for t in terms), bool(_JOURNAL_LINE_DATE.match(ln))),
        default="",
    )
    cache = {} if cache is None else cache
    if row.path not in cache:
        try:
            cache[row.path] = indexer.absolute(row.path).read_text(encoding="utf-8").splitlines()
        except OSError:
            cache[row.path] = []
    number = next(
        (i for i, ln in enumerate(cache[row.path], 1) if best and ln.strip() == best), None
    )
    dated = _JOURNAL_LINE_DATE.match(best) or _DAY.search(Path(row.path).name)
    return best, number, dated.group(1) if dated else _mtime_date(row)


def _journal_hit(
    indexer: Indexer,
    row: FileRow,
    chunk: ChunkRow,
    score: float,
    terms: set[str],
    ref: dict[str, list[str]],
    cache: dict[str, list[str]],
) -> RecallHit:
    best, number, date = journal_line(indexer, row, chunk, terms, cache)
    return RecallHit(
        source="journal",
        date=date,
        title=row.path,
        snippet=_COMMENT.sub("", best) or chunk.text[:_SNIPPET_CHARS],
        score=score,
        project=next(iter(ref.get("project", [])), ""),
        id=f"ep_{chunk.id}",
        path=row.path,
        line=number,
        refs=_object_refs(ref),
    )


def _mtime_date(row: FileRow) -> str:
    return datetime.fromtimestamp(row.mtime_ns / 1e9, UTC).date().isoformat()
