"""What feeds ``memory/``: a stored checkpoint, a settled Round, or a rebuild from the chats.

Also what feeds the index's ``conversation`` source: the passages of the turns a checkpoint
retires (:func:`index_retired_turns`), the turns from before that (:func:`backfill_turns`), and
their removal with a deleted Conversation (:func:`reconcile`). A turn of the current epoch is
still in context, so it is not a passage until the compaction that retires it.

Every entry point here swallows its failures (logged): the files are a copy, and writing them must
never fail or delay a turn. The compaction items stay the only source; a rebuild reads them and
never reads the files back (see :mod:`omnigent.runner.memory.files`).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from omnigent.context.rollover import CHECKPOINT_HEADERS, is_placeholder_summary, is_reset_item
from omnigent.entities import CompactionData
from omnigent.runner.memory import files
from omnigent.superchat.feature import is_helper
from omnigent.superchat.knowledge.db import TurnPassage
from omnigent.superchat.knowledge.runtime import KnowledgeRuntime, default_workspace
from omnigent.superchat.knowledge.turns import Turn, passage_of, split_turns
from omnigent.superchat.rollover import (
    ItemsPageFetchError,
    _get_items_page,
    fetch_latest_checkpoint,
)

_logger = logging.getLogger(__name__)

_COMPACTION_PAGE = "100"
_MAX_PAGES = 200

#: One backfill run reads at most this many pages (the route's maximum each) and writes at most
#: this many turns, so an old Conversation is indexed over a few turns, none of them slowed.
_BACKFILL_PAGE = "1000"
_BACKFILL_PAGES = 5
_BACKFILL_TURNS = 300
#: Index meta key: the last item a chat's backfill read.
#: Versioned: bumped when what a passage holds changes, so the backfill writes it once again.
CURSOR_KEY = "turns_cursor.v3:"


async def _snapshot(server_client: Any, chat_id: str) -> dict[str, Any]:
    """The chat's ``GET /v1/sessions/{id}`` body, or ``{}`` when it cannot be read."""
    try:
        resp = await server_client.get(f"/v1/sessions/{chat_id}", timeout=10.0)
        body = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError, RuntimeError):
        return {}
    return body if isinstance(body, dict) else {}


async def _gone(server_client: Any, chat_id: str) -> bool:
    """Whether the chat no longer exists (the server answered 404). Unsure counts as present."""
    try:
        resp = await server_client.get(f"/v1/sessions/{chat_id}", timeout=10.0)
    except (httpx.HTTPError, RuntimeError):
        return False
    return resp.status_code == 404


async def record_checkpoint(
    chat_id: str,
    labels: dict[str, str] | None,
    item: CompactionData,
    *,
    item_id: str | None = None,
    date: str | None = None,
    write_state: bool = True,
    workspace: Path | None = None,
) -> bool:
    """Journal a stored checkpoint of the Conversation and write its ``STATE.md``.

    A line already in a day file is not added again. With *item_id* the checkpoint is marked
    written (``.last_checkpoint``), so the memory sync never writes it twice. A Helper writes
    nothing. Never raises.

    :param date: The day its lines fall on when they name none (default today).
    :param write_state: ``False`` for a checkpoint a newer one replaces in ``STATE.md``.
    :returns: ``True`` unless a write failed.
    """
    try:
        if is_helper(labels):
            return True
        root = workspace or default_workspace()
        origin = files.Origin("main", chat_id)
        entries = files.checkpoint_lines(
            origin,
            item.journal_lines,
            item.evicted_entries,
            default_date=date or files.today(),
            refs=item.journal_refs,
        )
        ok = await asyncio.to_thread(files.append_lines, root, entries, skip_present=True)
        if write_state:
            state = files.state_text_of(item.summary)
            ok = ok and await asyncio.to_thread(files.write_state, root, state)
        await asyncio.to_thread(files.write_owner, root, chat_id, keep_existing=True)
        if ok and item_id is not None:
            await asyncio.to_thread(files.write_last_checkpoint, root, item_id)
        return bool(ok)
    except Exception:  # a copy must never fail the turn
        _logger.warning("memory: could not record the checkpoint of %s", chat_id, exc_info=True)
        return False


def _strings(raw: Any) -> list[str]:
    return [str(v) for v in raw if isinstance(v, str) and v] if isinstance(raw, list) else []


async def record_round(
    chat_id: str, entry: dict[str, Any], *, workspace: Path | None = None
) -> bool:
    """Journal the one entry of a settled Round (ADR 0029), once per Round. Never raises.

    *entry* is what the engine built from the Round view
    (:func:`omnigent.superchat.rounds.record.journal_entry`): ``round_id``, ``date``, ``ask``,
    ``outcome``, ``stopped``, ``files``, ``helpers``, ``folder``, ``seconds``, ``checked`` and
    ``refs``. A
    second settlement of the same Round finds its mark in the day file and writes nothing.

    :returns: Whether the entry was written now.
    """
    try:
        round_id, date = entry.get("round_id"), entry.get("date")
        if not isinstance(round_id, str) or not round_id or not isinstance(date, str):
            return False
        raw_refs = entry.get("refs") if isinstance(entry.get("refs"), dict) else {}
        line = files.round_line(
            files.Origin("main", chat_id),
            round_id=round_id,
            date=date,
            ask=str(entry.get("ask") or ""),
            outcome=str(entry.get("outcome") or ""),
            stopped=entry.get("stopped") is True,
            files=_strings(entry.get("files")),
            helpers=_strings(entry.get("helpers")),
            folder=str(entry.get("folder") or ""),
            seconds=float(entry.get("seconds") or 0),
            checked=entry.get("checked") if isinstance(entry.get("checked"), bool) else None,
            refs={key: _strings(values) for key, values in raw_refs.items()},
        )
        root = workspace or default_workspace()
        return await asyncio.to_thread(files.append_once, root, line, files.round_mark(round_id))
    except Exception:  # a copy must never fail the turn
        _logger.warning("memory: could not journal a Round of %s", chat_id, exc_info=True)
        return False


async def record_hand_edits(
    server_client: Any,
    chat_id: str,
    edits: list[dict[str, Any]],
    *,
    workspace: Path | None = None,
) -> None:
    """Journal one line per hand-edited artifact version delivered to a turn. Never raises.

    *edits* are ``pending-edits`` rows; only ``origin == "manual"`` ones are the person's (a live
    update's refresh is not). A Helper writes nothing.
    """
    try:
        manual = [e for e in edits if e.get("origin") == "manual"]
        if not manual:
            return
        body = await _snapshot(server_client, chat_id)
        labels = body.get("labels") if isinstance(body.get("labels"), dict) else None
        # Stored labels never mark a Helper or a studio; their parent does.
        if is_helper(labels) or body.get("parent_session_id"):
            return
        origin = files.Origin("main", chat_id)
        entries = [
            files.hand_edit_line(
                origin,
                name=str(edit["name"]),
                version=int(edit["version"]),
                artifact_id=str(edit["id"]),
                summary=str(edit.get("edit_summary") or ""),
                date=_item_date(edit),
            )
            for edit in manual
        ]
        await asyncio.to_thread(files.append_lines, workspace or default_workspace(), entries)
    except Exception:  # a copy must never fail the turn
        _logger.warning("memory: could not record the hand edits in %s", chat_id, exc_info=True)


async def record_daily_note(note: dict[str, Any], *, workspace: Path | None = None) -> None:
    """Project a daily note (the ``daily_note_update`` response) into its day file. Never raises.

    The note stays in the ``daily_notes`` table; the file holds a copy of its sections.
    """
    try:
        date, sections = note.get("date"), note.get("sections")
        if isinstance(date, str) and isinstance(sections, dict):
            await asyncio.to_thread(
                files.write_day_sections, workspace or default_workspace(), date, sections
            )
    except Exception:  # a copy must never fail the tool call
        _logger.warning("memory: could not project the daily note", exc_info=True)


async def _compaction_items(server_client: Any, chat_id: str) -> list[dict[str, Any]]:
    """Every ``compaction`` item of a chat, oldest first."""
    params = {"limit": _COMPACTION_PAGE, "order": "asc", "type": "compaction"}
    found: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        page = await _get_items_page(server_client, chat_id, params)
        data = [it for it in page.get("data", []) if it.get("type") == "compaction"]
        found.extend(data)
        if not page.get("data") or not page.get("has_more", False):
            break
        params = {**params, "after": page["data"][-1]["id"]}
    return found


def _item_date(item: dict[str, Any]) -> str:
    created = item.get("created_at")
    if isinstance(created, int):
        return files.today(datetime.fromtimestamp(created, UTC))
    return files.today()


def _reset_entry(item: dict[str, Any], main_id: str) -> tuple[str, str]:
    return files.format_line(_item_date(item), files.RESET_TEXT, files.Origin("main", main_id))


def _is_checkpoint(item: dict[str, Any]) -> bool:
    summary = item.get("summary")
    return isinstance(summary, str) and any(summary.startswith(h) for h in CHECKPOINT_HEADERS)


async def rebuild(server_client: Any, chat_id: str, *, workspace: Path | None = None) -> None:
    """Regenerate ``memory/journal/*.md`` and ``STATE.md`` from stored checkpoints, no model call.

    Reads the compaction items of the Conversation. A line already in a day file is not added
    again, so running it twice gives the same files. Round entries come from the Round record,
    not from the checkpoints, and are not replayed. Never raises.
    """
    try:
        root = workspace or default_workspace()
        main_id = chat_id
        origin = files.Origin("main", main_id)
        entries: list[tuple[str, str]] = []
        state: str | None = None
        reset_id: str | None = None
        last_id: Any = None
        for item in await _compaction_items(server_client, main_id):
            last_id = item.get("id")
            entries += files.checkpoint_lines(
                origin,
                item.get("journal_lines"),
                item.get("evicted_entries"),
                default_date=_item_date(item),
                refs=item.get("journal_refs"),
            )
            if is_reset_item(item):
                entries.append(_reset_entry(item, main_id))
                state, reset_id = files.reset_state_text(_item_date(item)), item.get("id")
            elif not is_placeholder_summary(item.get("summary")) and _is_checkpoint(item):
                state = files.state_text_of(item.get("summary"))
        notes = await _daily_notes(server_client)
        # Made only after the reads worked, so a failed read is retried on a later turn.
        files.memory_dir(root).mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(files.append_lines, root, entries, skip_present=True)
        if state:
            await asyncio.to_thread(files.write_state, root, state)
        for note in notes:
            await record_daily_note(note, workspace=root)
        if isinstance(reset_id, str):
            await asyncio.to_thread(files.write_last_reset, root, reset_id)
        if isinstance(last_id, str):
            # The memory sync writes only checkpoints stored after this one.
            await asyncio.to_thread(files.write_last_checkpoint, root, last_id)
        await asyncio.to_thread(files.write_owner, root, main_id)
    except Exception:  # a copy must never fail the turn
        _logger.warning("memory: could not rebuild from %s", chat_id, exc_info=True)


async def _daily_notes(server_client: Any) -> list[dict[str, Any]]:
    """The owner's stored daily notes (the newest year), or ``[]`` when they cannot be read."""
    try:
        resp = await server_client.get("/v1/me/daily-notes", params={"limit": "366"}, timeout=30.0)
        rows = resp.json().get("daily_notes", []) if resp.status_code == 200 else []
    except (httpx.HTTPError, ValueError, RuntimeError, AttributeError):
        return []
    return [row for row in rows if isinstance(row, dict)]


#: Most compaction items one memory sync reads back to the last one written.
_LATEST_PAGES = 5


async def _apply_latest(server_client: Any, main_id: str, root: Path) -> None:
    """Write the checkpoints stored since the last one written to the files, oldest first: a
    reset (a journal line, then an empty working list), or a checkpoint (its journal lines;
    ``STATE.md`` from the newest). The engine's fold stores them without this host, and two can
    land between syncs (ADR 0031). With no mark yet, only the newest is written (``rebuild``
    wrote the rest)."""
    marker = files.last_checkpoint_of(root)
    params = {"limit": "20", "order": "desc", "type": "compaction"}
    newer: list[dict[str, Any]] = []
    for _ in range(_LATEST_PAGES):
        page = await _get_items_page(server_client, main_id, params)
        data = [it for it in page.get("data", []) if isinstance(it, dict)]
        for item in data:
            if item.get("id") == marker:
                break
            newer.append(item)
            if marker is None:
                break
        else:
            if data and page.get("has_more", False):
                params = {**params, "after": str(data[-1].get("id"))}
                continue
        break
    for index, item in enumerate(reversed(newer)):
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        newest = index == len(newer) - 1
        if is_reset_item(item):
            if files.last_reset_of(root) != item_id:
                ok = await asyncio.to_thread(
                    files.append_lines, root, [_reset_entry(item, main_id)], skip_present=True
                )
                if newest:
                    state = files.reset_state_text(_item_date(item))
                    ok = ok and await asyncio.to_thread(files.write_state, root, state)
                if not ok:
                    return
                await asyncio.to_thread(files.write_last_reset, root, item_id)
            await asyncio.to_thread(files.write_last_checkpoint, root, item_id)
        elif _is_checkpoint(item) and not is_placeholder_summary(item.get("summary")):
            data = CompactionData.model_validate(
                {k: item.get(k) for k in CompactionData.model_fields if item.get(k) is not None}
            )
            if not await record_checkpoint(
                main_id,
                None,
                data,
                item_id=item_id,
                date=_item_date(item),
                write_state=newest,
                workspace=root,
            ):
                return
        else:
            await asyncio.to_thread(files.write_last_checkpoint, root, item_id)


async def reconcile(
    server_client: Any,
    chat_id: str,
    labels: dict[str, str] | None,
    *,
    workspace: Path | None = None,
    runtime: KnowledgeRuntime | None = None,
) -> None:
    """Bring ``memory/`` in line with the Conversation this turn belongs to. Never raises.

    * No folder (new or lost volume): rebuild it.
    * A folder owned by another main Conversation that was deleted: remove it, rebuild. While
      the other one still exists (or its state is unknown) the folder is left alone. The deleted
      Conversation's passages leave the index with it.
    * An unmarked folder (made before the marker): adopt it.
    * A reset, or a checkpoint the engine's fold stored, the files have not seen: write it once.

    """
    try:
        main_id = chat_id
        if is_helper(labels):
            return
        root = workspace or default_workspace()
        if not files.memory_dir(root).exists():
            await rebuild(server_client, chat_id, workspace=root)
            return
        owner = files.owner_of(root)
        if owner is not None and owner != main_id:
            if not await _gone(server_client, owner):
                return
            if await asyncio.to_thread(files.wipe, root):
                await _forget_conversation(server_client, owner, runtime)
                await rebuild_if_missing(server_client, chat_id, workspace=root)
            return
        if owner is None:
            await asyncio.to_thread(files.write_owner, root, main_id)
        await _apply_latest(server_client, main_id, root)
    except Exception:  # a copy must never fail the turn
        _logger.warning("memory: could not reconcile for %s", chat_id, exc_info=True)


async def rebuild_if_missing(
    server_client: Any, chat_id: str, *, workspace: Path | None = None
) -> None:
    """Run :func:`rebuild` when the host has no ``memory/`` folder (new or lost volume)."""
    root = workspace or default_workspace()
    if not files.memory_dir(root).exists():
        await rebuild(server_client, chat_id, workspace=root)


# ── the index's conversation source ──────────────────────────────────────


def _knowledge(server_client: Any, runtime: KnowledgeRuntime | None) -> KnowledgeRuntime | None:
    """The host's index service, or ``None`` outside a managed host (nothing to write)."""
    if runtime is not None:
        return runtime
    if os.environ.get("IS_SANDBOX") != "1":
        return None
    from omnigent.superchat.knowledge import runtime as service

    base_url = getattr(server_client, "base_url", None)
    service.configure(str(base_url) if base_url else None)
    found = service.get_runtime()
    found.start()
    return found


def _passages(
    turns: list[Turn], origin: files.Origin, main_id: str, project: str = ""
) -> list[TurnPassage]:
    """The passages of *turns*; a turn a runtime notice started is never one, and its items count
    as the previous passage's (so an anchor at its end still finds a turn)."""
    out: list[TurnPassage] = []
    for t in turns:
        if t.notice:
            if out:
                out[-1] = replace(out[-1], last_item_id=t.last_item_id)
            continue
        out.append(
            passage_of(
                t, chat_id=origin.chat_id, main_id=main_id, origin=origin.kind, project=project
            )
        )
    return out


async def index_retired_turns(
    server_client: Any,
    chat_id: str,
    labels: dict[str, str] | None,
    window: list[dict[str, Any]],
    *,
    project: str = "",
    runtime: KnowledgeRuntime | None = None,
) -> None:
    """Write the turns a stored checkpoint retired to the index, in one batch. Never raises.

    *window* is the record the checkpoint summarized (the items after the previous anchor, up to
    its own), already read by the rollover: nothing is read again. Called once the compaction
    item is stored. *project* is the
    Project folder the chat runs in. A Helper's turns, steers and runtime notices are never
    passages (:mod:`.turns`).
    """
    try:
        found = _knowledge(server_client, runtime)
        if is_helper(labels) or found is None:
            return
        origin = files.Origin("main", chat_id)
        passages = _passages(split_turns(window), origin, chat_id, project)
        if passages:
            await asyncio.to_thread(found.indexer.index_turns, passages)
    except Exception:  # indexing must never fail the turn
        _logger.warning("recall: could not index the retired turns of %s", chat_id, exc_info=True)


def _backfill_step(
    items: list[dict[str, Any]], more: bool, room: int, ends: set[str]
) -> tuple[list[Turn], str | None, bool]:
    """What one page of the record gives: ``(turns to write, new cursor, reached the end)``.

    *ends* are the chat's latest checkpoint anchor and the checkpoint item itself: the record
    up to the first of them is retired, every turn in it complete. Before it, the page's last
    turn may go on in the next page, so it is left for the next read; a single turn longer than
    a page is taken whole. Past it is the current epoch, which only its compaction indexes.
    ``None`` as the cursor means no progress.
    """
    end = next((i for i, it in enumerate(items) if it.get("id") in ends), None)
    if end is not None:
        items = items[: end + 1]
        turns = split_turns(items)
        keep, cut, done = turns, len(items), True
    elif not more:
        return [], None, True  # the cursor is at or past the anchor: nothing retired is left
    else:
        turns = split_turns(items)
        keep, cut, done = turns[:-1], (turns[-1].first if turns else len(items)), False
        if not keep and cut == 0:
            keep, cut = turns, len(items)
    if len(keep) > room:
        keep, cut, done = keep[:room], turns[room].first, False
    cursor = items[cut - 1].get("id") if cut > 0 else None
    return keep, cursor if isinstance(cursor, str) else None, done


async def _backfill_chat(
    server_client: Any,
    found: KnowledgeRuntime,
    origin: files.Origin,
    main_id: str,
    budget: dict[str, int],
    project: str = "",
) -> bool:
    """Index a chat's retired turns (up to its latest checkpoint's anchor) from its saved cursor
    while *budget* lasts. ``True`` once none is left; a chat never compacted has none."""
    db, key = found.indexer.db, f"{CURSOR_KEY}{origin.chat_id}"
    checkpoint = await fetch_latest_checkpoint(
        server_client, origin.chat_id, skip_placeholders=True
    )
    if checkpoint is None:
        return True
    ends = {e for e in (checkpoint.get("last_item_id"), checkpoint.get("id")) if e}
    while budget["pages"] > 0 and budget["turns"] > 0:
        cursor = await asyncio.to_thread(db.meta, key)
        if cursor in ends:
            return True
        params = {"limit": _BACKFILL_PAGE, "order": "asc", **({"after": cursor} if cursor else {})}
        try:
            page = await _get_items_page(server_client, origin.chat_id, params)
        except ItemsPageFetchError as exc:
            if exc.status_code == 400 and cursor:
                # A cursor item that is gone: read again from the start (writes are idempotent).
                await asyncio.to_thread(db.set_meta, key, None)
            raise
        budget["pages"] -= 1
        items = [it for it in page.get("data", []) if isinstance(it, dict)]
        more = bool(items) and bool(page.get("has_more", False))
        turns, moved, ended = _backfill_step(items, more, budget["turns"], ends)
        budget["turns"] -= len(turns)
        passages = _passages(turns, origin, main_id, project)
        await asyncio.to_thread(
            found.indexer.index_turns, passages, cursor=(key, moved) if moved else None
        )
        if ended:
            return True
    return False


async def backfill_turns(
    server_client: Any,
    chat_id: str,
    labels: dict[str, str] | None,
    *,
    project: str = "",
    runtime: KnowledgeRuntime | None = None,
) -> None:
    """Index the turns of a Conversation up to its latest checkpoint's anchor. Never raises.

    Reads the record from the saved cursor to the anchor (every retired turn, none of the
    current epoch), at most :data:`_BACKFILL_PAGES` pages and :data:`_BACKFILL_TURNS` turns per
    run. Progress (the last item read) is saved with the passages, so a restart resumes where it
    stopped and never writes a turn twice. A cursor already at the anchor costs one checkpoint
    read. A fold moves the anchor without a history load (ADR 0031), so the turns it folded
    arrive here at the next turn; the ones a new session leaves out arrive with its history load.
    """
    try:
        main_id = chat_id
        found = _knowledge(server_client, runtime)
        if is_helper(labels) or found is None:
            return
        budget = {"pages": _BACKFILL_PAGES, "turns": _BACKFILL_TURNS}
        origin = files.Origin("main", main_id)
        await _backfill_chat(server_client, found, origin, main_id, budget, project)
    except Exception:  # indexing must never fail the turn
        _logger.warning("recall: could not backfill the turns of %s", chat_id, exc_info=True)


async def _forget_conversation(
    server_client: Any, main_id: str, runtime: KnowledgeRuntime | None
) -> None:
    """Remove a deleted Conversation's passages from the index."""
    try:
        found = _knowledge(server_client, runtime)
        if found is None:
            return
        chats = [main_id, *await asyncio.to_thread(found.indexer.db.chats_of, main_id)]
        keys = [
            *(f"{CURSOR_KEY}{chat}" for chat in chats),
        ]
        await asyncio.to_thread(found.indexer.delete_chats, chats, meta_keys=keys)
    except Exception:  # a copy must never fail the turn
        _logger.warning("recall: could not remove the turns of %s", main_id, exc_info=True)
