"""What a Super Chat turn or a new Helper's Brief is prefixed with: the one hook.

Both the runner's per-turn path (``runner/app.py``) and the sub-agent launch paths
(``runner/tool_dispatch.py``, which keep only the profile's preference lines on the brief's
subject) read the person's Memory Profile from the server here, and the
runner's per-turn path also reads the local-time line. Failures never fail the turn: a missing
profile means "no profile".
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx

from omnigent.memory.service import PROFILE_BLOCK_FOOTER, PROFILE_BLOCK_HEADER
from omnigent.superchat.artifacts.hint import DELTA_HEADER as FILES_DELTA_HEADER
from omnigent.superchat.artifacts.hint import HEADER as ARTIFACT_HINT_HEADER
from omnigent.superchat.artifacts.hint import fetch_artifact_hint
from omnigent.superchat.artifacts.studio_turn import (
    BRIEF_OPEN,
    FILE_LINE_OPEN,
    SELECTED_CLOSE,
    SELECTED_OPEN,
    studio_turn_blocks,
)
from omnigent.superchat.artifacts.writeback import NOTE_LINE as EDIT_NOTE_LINE
from omnigent.superchat.decks.outline import HEADER as OUTLINE_HEADER
from omnigent.superchat.knowledge.attachment_context import OPEN as ATTACHMENT_OPEN
from omnigent.superchat.knowledge.attachment_context import REMINDER as ATTACHMENT_REMINDER
from omnigent.superchat.local_time import LOCAL_TIME_OPEN, fetch_timezone, local_time_block
from omnigent.superchat.projects.block import PROJECTS_OPEN, projects_block
from omnigent.superchat.projects.choice import MOVED_NOTE
from omnigent.superchat.recall_hint.hint import FOOTER as HINT_FOOTER
from omnigent.superchat.recall_hint.hint import HEADER as HINT_HEADER
from omnigent.superchat.subagents import RESULT_REMINDER, result_notice_reminder

if TYPE_CHECKING:
    from omnigent.superchat.feature import Feature, MessageBlock

_logger = logging.getLogger(__name__)

#: Longest a turn's prefix waits on its reads (ADR 0026: the first word under two seconds). A
#: read still out is left out of this turn; it never holds the model call.
PREFIX_BUDGET_SECONDS = 0.5
#: Longest the prefix waits when it carries the person's own material: the text of files the
#: message attaches (why such a message gets a turn of its own, ``Feature.needs_own_turn``), or
#: the file a studio works on.
OWN_MATERIAL_BUDGET_SECONDS = 5.0

#: Feature notes (hand edits written back, "the person moved this chat") read too late for their
#: turn, by Conversation: delivered with the next turn's, since reading them consumed them.
_late_feature_blocks: dict[str, asyncio.Task[list[str]]] = {}
#: The person's timezone as last read, for a turn whose read is late.
_known_timezone: dict[str, str | None] = {}

#: Sent in place of a block the warm client already holds, see :func:`turn_prefix_blocks`.
PROFILE_UNCHANGED_LINE = "Standing memory unchanged since the last message."
PROJECTS_UNCHANGED_LINE = "Projects unchanged since the last message."
STUDIO_FILE_UNCHANGED_LINE = "The file is unchanged since the last message."
#: Sent in place of a block not read within the budget, so the model knows it is missing and
#: how to get it, instead of acting as if there were no memory or no Projects.
PROFILE_LATE_LINE = (
    "Standing memory was not read in time for this message; memory_search looks up what you need."
)
PROJECTS_LATE_LINE = (
    "The Project list was not read in time for this message; list ~/workspace/projects/ to see it."
)


IDLE_GAP_OPEN, IDLE_GAP_CLOSE = "It has been ", " since the last message."
#: The line naming the model and harness a Super Chat turn runs on, so the model can say so.
#: Per turn, not in the instructions: a model change then keeps the warm client.
SETUP_OPEN, SETUP_CLOSE = (
    "[Setup: you run on the model ",
    " harness. Earlier messages may describe another setup: go by this one.]",
)
#: The line a turn gets when the model or harness changed since the Conversation's last turn.
SETUP_CHANGED_OPEN, SETUP_CHANGED_CLOSE = (
    "[Since the last message the setup changed: ",
    ". Earlier messages may describe the old setup; go by the current one.]",
)


def setup_line(model: str, harness_label: str) -> str:
    """One line naming the setup, e.g. ``[Setup: you run on the model X through the pi ...]``."""
    return f"{SETUP_OPEN}{model} through the {harness_label}{SETUP_CLOSE}"


def setup_changed_line(before: str, now: str) -> str:
    """One line saying the setup changed, e.g. ``... was pi with claude-opus-5-5, now ...``."""
    return f"{SETUP_CHANGED_OPEN}was {before}, now {now}{SETUP_CHANGED_CLOSE}"


def idle_gap_line(idle_seconds: float) -> str:
    """One plain line naming how long the person was away, e.g. ``It has been 3 days ...``."""
    hours = int(idle_seconds // 3600)
    days = hours // 24
    if days >= 2:
        span = f"{days} days"
    elif hours >= 1:
        span = f"{hours} hour" + ("" if hours == 1 else "s")
    else:
        span = "less than an hour"
    return f"{IDLE_GAP_OPEN}{span}{IDLE_GAP_CLOSE}"


def _delta_block(
    name: str,
    block: str,
    unchanged_line: str,
    last_sent: Mapping[str, str] | None,
    sent_now: dict[str, str] | None,
) -> str:
    """*block*, or *unchanged_line* when the warm client already got this exact block.

    Records the block's hash in *sent_now* either way: the client holds it after this turn.
    """
    digest = hashlib.sha256(block.encode("utf-8")).hexdigest()
    if sent_now is not None:
        sent_now[name] = digest
    if last_sent is not None and last_sent.get(name) == digest:
        return unchanged_line
    return block


def files_block(
    hint: str, last_sent: Mapping[str, str] | None, sent_now: dict[str, str] | None
) -> str | None:
    """The file handles on a harness session's first turn; after that only the changed lines.

    A line changes when its file is new, has a new version or a new state (open, being
    edited), so a warm session hears of exactly those; nothing changed means no block. What
    this turn's handles are is recorded in *sent_now* (the lines, not a hash: the next turn
    compares them line by line).
    """
    lines = [line for line in hint.split("\n")[1:] if line.startswith("- ")]
    if sent_now is not None:
        sent_now["files"] = "\n".join(lines)
    if last_sent is None or "files" not in last_sent:
        return hint
    before = set(last_sent["files"].split("\n"))
    changed = [line for line in lines if line not in before]
    return "\n".join([FILES_DELTA_HEADER, *changed]) if changed else None


async def fetch_memory_profile(
    server_client: httpx.AsyncClient | None, conversation_id: str | None
) -> str | None:
    """The chat owner's delimiter-wrapped Memory Profile block, or ``None``.

    Read from the server (``GET /v1/sessions/{id}/memory/profile``): the memory store lives
    server-side. Any failure, or an empty profile, means "no profile".
    """
    if server_client is None or not conversation_id:
        return None
    try:
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}/memory/profile", timeout=10.0
        )
        if resp.status_code != 200:
            _logger.warning(
                "Memory Profile fetch failed for %s; proceeding without it: status=%s",
                conversation_id,
                resp.status_code,
                extra={"session_id": conversation_id},
            )
            profile = None
        else:
            profile = resp.json().get("profile")
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning(
            "Memory Profile fetch failed for %s; proceeding without it: %s",
            conversation_id,
            type(exc).__name__,
            extra={"session_id": conversation_id},
        )
        return None
    return profile if isinstance(profile, str) and profile else None


async def fetch_local_time_line(server_client: httpx.AsyncClient | None) -> str:
    """The person's current local date/time line (owner preference zone; UTC when unknown).

    The zone is remembered for a turn whose read is late (:func:`late_local_time_line`).
    """
    zone = await fetch_timezone(server_client)
    _known_timezone["zone"] = zone
    return local_time_block(zone)


def late_local_time_line() -> str:
    """The local time line in the zone last read (UTC before any read)."""
    return local_time_block(_known_timezone.get("zone"))


async def _within(
    budget: float, conversation_id: str, parts: Mapping[str, Awaitable[object]]
) -> dict[str, object]:
    """The *parts* that finished within *budget* seconds, by name, started together.

    A part still running is left running (its result is not used) and logged by name, so a
    slow read shows which one it was.
    """
    tasks = {name: asyncio.ensure_future(part) for name, part in parts.items()}
    if not tasks:
        return {}
    await asyncio.wait(tasks.values(), timeout=budget)
    late = [name for name, task in tasks.items() if not task.done()]
    if late:
        _logger.info(
            "prefix for %s left out what was not read within %.1fs: %s",
            conversation_id,
            budget,
            ", ".join(late),
            extra={"session_id": conversation_id},
        )
    done: dict[str, object] = {}
    for name, task in tasks.items():
        if task.done() and not task.cancelled() and task.exception() is None:
            done[name] = task.result()
    return done


def _carried_feature_blocks(conversation_id: str) -> list[str]:
    """The feature notes an earlier turn read too late, when they have arrived since."""
    task = _late_feature_blocks.get(conversation_id)
    if task is None or not task.done():
        return []
    del _late_feature_blocks[conversation_id]
    if task.cancelled() or task.exception() is not None:
        return []
    return task.result()


def _merge_carried(carried: Sequence[str], fresh: Sequence[str]) -> list[str]:
    """*fresh* after the *carried* blocks a fresh one does not supersede (same first line)."""
    heads = {block.split("\n", 1)[0] for block in fresh}
    return [b for b in carried if b.split("\n", 1)[0] not in heads] + list(fresh)


def _safe_projects_block(workspace: Path | None) -> str | None:
    """The Project list block; a filesystem or parsing surprise means "no block"."""
    try:
        return projects_block(workspace)
    except Exception:  # the list is a convenience, never a reason to fail a turn
        _logger.warning("Project list failed; proceeding without it", exc_info=True)
        return None


async def _feature_prefix_blocks(
    server_client: httpx.AsyncClient | None, conversation_id: str
) -> list[str]:
    """Every feature's ``turn_prefix`` blocks, in feature order. Never fails a turn."""
    from omnigent.superchat.features import FEATURES

    async def _blocks(feature: Feature) -> list[str]:
        assert feature.turn_prefix is not None
        try:
            return await feature.turn_prefix(server_client, conversation_id)
        except Exception:  # a convenience, never a reason to fail a turn
            _logger.warning(
                "%s turn prefix failed; proceeding without it", feature.name, exc_info=True
            )
            return []

    # Independent reads, fetched together; gather keeps the feature order.
    fetched = await asyncio.gather(*(_blocks(f) for f in FEATURES if f.turn_prefix is not None))
    return [block for blocks in fetched for block in blocks]


def message_needs_own_turn(texts: Sequence[str]) -> bool:
    """Whether any feature needs this message to start a turn of its own (``needs_own_turn``)."""
    from omnigent.superchat.features import FEATURES

    return any(
        feature.needs_own_turn(text)
        for feature in FEATURES
        if feature.needs_own_turn is not None
        for text in texts
    )


async def _message_prefix_blocks(
    server_client: httpx.AsyncClient | None,
    conversation_id: str,
    texts: Sequence[str],
    helper: bool = False,
) -> list[MessageBlock]:
    """Every feature's ``message_prefix`` blocks for the turn's own message, fetched together and
    kept in feature order. A Helper's turn (*helper*) skips the Conversation-only ones
    (``Feature.conversation_only``). Never fails a turn."""
    from omnigent.superchat.features import FEATURES

    async def _blocks(feature: Feature) -> list[MessageBlock]:
        assert feature.message_prefix is not None
        try:
            return await feature.message_prefix(server_client, conversation_id, texts)
        except Exception:  # a convenience, never a reason to fail a turn
            _logger.warning(
                "%s message prefix failed; proceeding without it", feature.name, exc_info=True
            )
            return []

    fetched = await asyncio.gather(
        *(
            _blocks(f)
            for f in FEATURES
            if f.message_prefix is not None and not (helper and f.conversation_only)
        )
    )
    return [block for blocks in fetched for block in blocks]


async def turn_prefix_blocks(
    server_client: httpx.AsyncClient | None,
    conversation_id: str,
    workspace: Path | None = None,
    message_texts: Sequence[str] = (),
    idle_gap_seconds: float | None = None,
    last_sent: Mapping[str, str] | None = None,
    sent_now: dict[str, str] | None = None,
    studio: bool = False,
    helper: bool = False,
) -> list[MessageBlock]:
    """The blocks to prepend to a Super Chat turn, in application order.

    Each block is prepended in turn, so the last one ends up first: ``[message blocks (the files
    it attaches), memory profile (when any), Projects (when any), feature notes such as hand edits
    (when any), local time]`` renders as local time, then the feature notes, then the Projects,
    then the profile, then the message blocks, then the person's message. A message block that is
    a content block (an attached image) is not text: it goes with the message itself. Nothing from
    recall: earlier work reaches the model through the ``context_brief`` tool (ADR 0027).

    :param workspace: The session's current working directory (marks the open Project).
    :param message_texts: Text parts of the turn's own message; features build blocks from them
        (``Feature.message_prefix``) that go first, so they end up closest to the message.
    :param idle_gap_seconds: How long the person was away, when this is the first message back
        from an idle break; adds one line next to the local time.
    :param last_sent: Hashes (by ``profile`` / ``projects``) of the blocks the current warm
        client last received, and the file handle lines (``files``). A block with the same hash
        goes as one short line instead; the file handles go only as their changed lines.
        ``None`` (a cold start, or anything unproven) sends every block in full.
    :param sent_now: Filled with the hashes of the profile and Projects blocks this turn
        carries, for the caller to remember once the turn completes.
    :param helper: The session is a Helper: no Conversation-only message blocks (tasks) and no
        memory profile (its brief carries the preference lines that apply). A file's studio
        drops the Conversation-only blocks the same way.
    :param studio: The session is a file's studio (ADR 0024): its turn gets the memory profile,
        the file line and outline (again only once the version changed), the elements the
        message selected and this file's write-back notes only (no Projects, no feature notes,
        no Conversation file handles).

    The reads share one budget (:data:`PREFIX_BUDGET_SECONDS`, longer for the person's own
    material): a block not read in time is left out of this turn, the profile and Projects with
    one line saying so, and feature notes read late go with the next turn's.
    """
    # Every block is an independent read: fetch them together (ADR 0026: the prefix is paid on
    # every turn), within one budget, then assemble them in the documented order. Imported
    # first so the budget never pays for an import.
    from omnigent.superchat.features import FEATURES  # noqa: F401

    budget = (
        OWN_MATERIAL_BUDGET_SECONDS
        if studio or (message_texts and message_needs_own_turn(message_texts))
        else PREFIX_BUDGET_SECONDS
    )
    if studio:
        got = await _within(
            budget,
            conversation_id,
            {
                # The studio is not the Conversation: no Conversation-only blocks (tasks).
                "message": _message_prefix_blocks(
                    server_client, conversation_id, message_texts, helper=True
                )
                if message_texts
                else _no_blocks(),
                "profile": fetch_memory_profile(server_client, conversation_id),
                "studio": studio_turn_blocks(server_client, conversation_id, message_texts),
            },
        )
        message_blocks = cast("list[MessageBlock]", got.get("message", []))
        profile = cast(str | None, got.get("profile"))
        file_blocks, turn_blocks = cast(tuple[list[str], list[str]], got.get("studio", ([], [])))
        blocks: list[MessageBlock] = [*message_blocks, *_reminders(message_texts)]
        if profile:
            blocks.append(
                _delta_block("profile", profile, PROFILE_UNCHANGED_LINE, last_sent, sent_now)
            )
        if file_blocks:
            # The file line and outline again only once the version changed (ADR 0031).
            file_block = "\n\n".join(file_blocks)
            blocks.append(
                _delta_block("file", file_block, STUDIO_FILE_UNCHANGED_LINE, last_sent, sent_now)
            )
        blocks.extend(turn_blocks)
        return blocks
    carried = _carried_feature_blocks(conversation_id)
    features = asyncio.ensure_future(_feature_prefix_blocks(server_client, conversation_id))
    got = await _within(
        budget,
        conversation_id,
        {
            "message": _message_prefix_blocks(
                server_client, conversation_id, message_texts, helper
            )
            if message_texts
            else _no_blocks(),
            # A Helper gets the preference lines on its subject in its brief, not the profile.
            **(
                {} if helper else {"profile": fetch_memory_profile(server_client, conversation_id)}
            ),
            "projects": asyncio.to_thread(_safe_projects_block, workspace),
            # The Conversation's file handles; a Helper names its files in its own work.
            **({} if helper else {"files": fetch_artifact_hint(server_client, conversation_id)}),
            "features": features,
            "local_time": fetch_local_time_line(server_client),
        },
    )
    if not features.done():
        _late_feature_blocks[conversation_id] = features
    message_blocks = cast("list[MessageBlock]", got.get("message", []))
    profile = cast(str | None, got.get("profile"))
    projects = cast(str | None, got.get("projects"))
    feature_blocks = _merge_carried(carried, cast(list[str], got.get("features", [])))
    local_time = cast(str | None, got.get("local_time")) or late_local_time_line()
    blocks: list[MessageBlock] = [*message_blocks, *_reminders(message_texts)]
    if profile:
        blocks.append(
            _delta_block("profile", profile, PROFILE_UNCHANGED_LINE, last_sent, sent_now)
        )
    elif not helper and "profile" not in got:
        blocks.append(PROFILE_LATE_LINE)
    if projects is not None:
        blocks.append(
            _delta_block("projects", projects, PROJECTS_UNCHANGED_LINE, last_sent, sent_now)
        )
    elif "projects" not in got:
        blocks.append(PROJECTS_LATE_LINE)
    blocks.extend(feature_blocks)
    hint = cast(list[str], got.get("files", []))
    if hint:
        files = files_block(hint[0], last_sent, sent_now)
        if files:
            blocks.append(files)
    elif "files" not in got and last_sent is not None and "files" in last_sent:
        # Not read in time: this session still holds what it was sent, so keep comparing to it.
        if sent_now is not None:
            sent_now["files"] = last_sent["files"]
    if idle_gap_seconds is not None:
        blocks.append(idle_gap_line(idle_gap_seconds))
    blocks.append(local_time)
    return blocks


async def _no_blocks() -> list[str]:
    return []


def _reminders(texts: Sequence[str]) -> list[str]:
    """The reminder a wake carrying a Result gets, next to the notice; never stored."""
    reminder = result_notice_reminder(texts)
    return [reminder] if reminder else []


#: The blocks that span lines, as (how one starts, what ends it). Projects end at the ``]`` that
#: closes the block, the one followed by the blank line before the next block.
_FENCED_BLOCKS = (
    (PROFILE_BLOCK_HEADER, PROFILE_BLOCK_FOOTER),
    # The recall hint block an older turn carried (before ADR 0027).
    (HINT_HEADER, "\n" + HINT_FOOTER),
    (ATTACHMENT_OPEN, ATTACHMENT_REMINDER),
    (PROJECTS_OPEN, "]\n\n"),
    # The file handles' lines run to the blank line before the next block, as do their changes.
    (ARTIFACT_HINT_HEADER, "\n\n"),
    (FILES_DELTA_HEADER, "\n\n"),
    (SELECTED_OPEN, SELECTED_CLOSE),
    # A studio deck's slide outline (ADR 0024), likewise.
    (OUTLINE_HEADER, "\n\n"),
)
_MOVED_OPEN, _MOVED_CLOSE = MOVED_NOTE.split("{}")


def _injected_line(line: str) -> bool:
    """Whether *line* is one of the one-line blocks :func:`turn_prefix_blocks` writes."""
    line = line.strip()
    return (
        (line.startswith(LOCAL_TIME_OPEN) and line.endswith("]"))
        # The attachment block's closing line is left alone by the transcript's own cut of the
        # block (:func:`omnigent.context.attachments.strip_legacy_attachment_context`).
        or line
        in (
            PROFILE_UNCHANGED_LINE,
            PROJECTS_UNCHANGED_LINE,
            STUDIO_FILE_UNCHANGED_LINE,
            PROFILE_LATE_LINE,
            PROJECTS_LATE_LINE,
            ATTACHMENT_REMINDER,
        )
        or (line.startswith(IDLE_GAP_OPEN) and line.endswith(IDLE_GAP_CLOSE))
        or (line.startswith(SETUP_OPEN) and line.endswith(SETUP_CLOSE))
        or (line.startswith(SETUP_CHANGED_OPEN) and line.endswith(SETUP_CHANGED_CLOSE))
        or (line.startswith(_MOVED_OPEN) and line.endswith(_MOVED_CLOSE))
        or line.startswith((FILE_LINE_OPEN, BRIEF_OPEN, RESULT_REMINDER))
        or EDIT_NOTE_LINE.fullmatch(line) is not None
    )


def person_words(text: str) -> str:
    """*text* without the blocks :func:`turn_prefix_blocks` put in front of it: the person's words.

    The blocks go to the harness only, but a harness that keeps its own transcript can hand the
    prefixed message back, and a new Helper's Brief carries the Memory Profile. A reader that
    wants what the person said (the ``conversation`` index) cuts them here, by the markers their
    producers write. Only a leading run of blocks is cut, so the person's own text is never
    touched; a block that never ends was cut off with nothing after it, so nothing is left.
    """
    return split_prefix(text)[1]


def split_prefix(text: str) -> tuple[list[str], str]:
    """*text* as ``(the leading blocks turn_prefix_blocks put in front of it, each whole; the
    person's words after them)``, cut by the same markers as :func:`person_words`."""
    blocks: list[str] = []
    rest = text.lstrip()
    while rest:
        for opener, closer in _FENCED_BLOCKS:
            if rest.startswith(opener):
                end = rest.find(closer, len(opener))
                cut = len(rest) if end < 0 else end + len(closer)
                blocks.append(rest[:cut].strip())
                rest = rest[cut:].lstrip()
                break
        else:
            line, _, after = rest.partition("\n")
            if not _injected_line(line):
                return blocks, rest.rstrip()
            blocks.append(line.strip())
            rest = after.lstrip()
    return blocks, ""
