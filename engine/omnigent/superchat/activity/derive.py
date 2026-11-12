"""Activity Feed derivation for ``superside-chat`` sessions.

An **Activity** is never stored: it is derived on read from existing
conversations and items — a Super Chat turn that
included at least one tool call, or a whole Sub-agent session. See
``docs/glossary.md`` (Activity, Activity Feed, Step) and
``docs/helpers.md``.

Reuses ``_project_activity_item`` from ``omnigent.tools.builtins.spawn``
for step/detail projection rather than re-implementing it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.context.labels import (
    ADHOC_HELPER_LABEL_KEY,
    ARTIFACT_CHAT_LABEL_KEY,
    PAUSED_LABEL_KEY,
    ROUND_LABEL_KEY,
    SCHEDULED_HELPER_LABEL_KEY,
    is_artifact_chat,
)
from omnigent.entities import Conversation, ConversationItem, MessageData
from omnigent.entities.conversation import is_system_notice
from omnigent.stores.conversation_store import ConversationStore
from omnigent.superchat.activity.titles import (
    ACTIVITY_TITLE_INSTRUCTIONS,
    clean_generated_label,
    is_generic_title,
    scheduled_display_title,
    scrub_internal_words,
    split_generated_label,
    strip_fire_suffix,
    strip_reply_filler,
    tidy_request_title,
)
from omnigent.superchat.family.tree import (  # noqa: F401  (re-exported: the family reads live in core)
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    is_helper_live,
    last_assistant_text,
    list_chat_family,
    list_chat_roots,
    message_text,
    resolve_super_chat_id,
    sub_agent_status,
)
from omnigent.superchat.feature import RECORDED_RESPONSE_PREFIX
from omnigent.superchat.rounds.record import NO_TURN, RELAY_LOST, RESET, helper_titled
from omnigent.superchat.titles import STRUCTURED_TEXT, one_line_summary, truncate
from omnigent.superchat.transcript.blocks import turn_activity_id
from omnigent.superchat.work_tools import is_work_tool
from omnigent.tools.builtins.spawn import _project_activity_item
from omnigent.util.session_lifecycle import title_without_closed_marker

if TYPE_CHECKING:
    from omnigent.server.background_session_titles import BackgroundSessionTitleCoordinator

_logger = logging.getLogger(__name__)

KIND_TURN = "turn"
KIND_SUB_AGENT = "sub_agent"

#: What an Activity is, for the icon beside it: ``kind`` says how it is stored
#: (and keys its id); ``source`` says where the work came from.
SOURCE_TURN = "turn"
SOURCE_BACKGROUND = "background"
SOURCE_SCHEDULED = "scheduled"
#: Quiet upkeep (daily note, Dreaming, People): grouped into one row per local day.
SOURCE_HOUSEKEEPING = "housekeeping"
HOUSEKEEPING_PREFIX = "housekeeping"
HOUSEKEEPING_TITLE = "Kept your notes up to date"
#: A turn in a file's studio (ADR 0024): one row per request, grouped under the file by the web.
SOURCE_STUDIO = "studio"

#: The file's name for a studio's group id (``None`` when unknown), filled in by the artifacts
#: capability: the activity read has the conversation store only.
FILE_NAMES: dict[str, Callable[[str], str | None]] = {}
#: The Round that created a studio's file (``None`` when unknown or made outside a Round),
#: filled in by the artifacts capability: the web hangs the file's edits under that request.
FILE_ROUNDS: dict[str, Callable[[str], str | None]] = {}
#: ``(conversation id, its item ids oldest first) -> the Round each item is in``, filled in by
#: the rounds capability (``rounds.read.item_rounds``): a turn's row carries its Round.
ROUND_IDS: dict[str, Callable[[str, list[str]], list[str | None]]] = {}
#: ``(conversation id, round ids) -> {round id: its folded view}`` (``rounds.fold.fold``), filled
#: in by the rounds capability: a row in a recorded Round reads that view (ADR 0029).
ROUND_VIEWS: dict[str, Callable[[str, list[str]], dict[str, dict[str, Any]]]] = {}
#: A Round's status, and one of its Helpers', as an Activity status.
_ROUND_STATUS = {
    "idle": STATUS_IN_PROGRESS,
    "running": STATUS_IN_PROGRESS,
    "done": STATUS_DONE,
    "failed": STATUS_FAILED,
    "stopped": STATUS_CANCELLED,
}
#: A conversation turn is shown only when it did real work beyond a quick answer.
_MIN_TURN_STEPS = 3
_MIN_TURN_SECONDS = 60

#: Env var overriding the per-chat Activity Feed items-scan limit below.
ITEMS_SCAN_LIMIT_ENV = "OMNIGENT_ACTIVITY_ITEMS_SCAN_LIMIT"

# A feed covering more items than this per chat only sees the newest
# _ITEMS_SCAN_LIMIT (oldest history is what gets truncated, never the
# current turn) — generous enough for ordinary use while keeping one
# page-fetch's cost bounded.
_ITEMS_SCAN_LIMIT = 1000


def _resolve_items_scan_limit() -> int:
    """Resolve the per-chat Activity Feed items-scan limit.

    Reads :data:`ITEMS_SCAN_LIMIT_ENV`, default :data:`_ITEMS_SCAN_LIMIT`. A
    missing, non-integer, or non-positive value falls back to the default.
    """
    raw = os.environ.get(ITEMS_SCAN_LIMIT_ENV)
    if raw is None:
        return _ITEMS_SCAN_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _ITEMS_SCAN_LIMIT
    return value if value > 0 else _ITEMS_SCAN_LIMIT


_DEFAULT_LIMIT = 20
#: Seconds a row may start after its chat's last write (a Round's record lags its message).
_LAST_WRITE_SLACK = 60
_MAX_LIMIT = 100
_OUTCOME_MAX_CHARS = 140
_TITLE_MAX_CHARS = 80
_TITLE_PROMPT_REQUEST_CHARS = 700
_TITLE_PROMPT_STEPS = 6
_TITLE_PROMPT_RESULT_CHARS = 600
#: A feed read starts at most this many model-written titles (newest first); the
#: rest follow on later reads, so an old, untitled history fills in gradually.
_TITLE_JOBS_PER_READ = 8


@dataclass(frozen=True)
class Step:
    """One action within an Activity, in plain language.

    :param item_id: The underlying ``function_call`` item's id.
    :param title: Plain-language description, e.g. ``"Searched memory for
        'deadline'"``.
    :param created_at: Unix epoch timestamp of the call.
    :param tool: The tool name invoked, e.g. ``"memory_search"``.
    :param detail: The call's arguments and matching output, capped —
        populated only when the Activity is read in full (not on the
        list/feed view).
    :param kind: What kind of step it is (:func:`step_kind`), or ``None``.
    :param snippet: A line of what it found, when its title has one (the brief's first line).
    :param digest: For a step of a recorded Round, its arguments in a few words, as the strip
        shows it (the Round view's trail).
    :param helper_id: For a step of a recorded Round, the Helper that made it, else ``None``.
    :param ok: For a finished step of a recorded Round, whether its output read as success.
    """

    item_id: str
    title: str
    created_at: int
    tool: str | None = None
    detail: dict[str, Any] | None = None
    kind: str | None = None
    snippet: str | None = None
    digest: str | None = None
    helper_id: str | None = None
    ok: bool | None = None


@dataclass(frozen=True)
class Activity:
    """One unit of multi-step work: a chat turn or a Sub-agent session.

    :param id: Opaque id, e.g. ``"turn:conv_abc:resp_xyz"`` or
        ``"sub_agent:conv_child"``.
    :param kind: ``"turn"`` or ``"sub_agent"``.
    :param chat_id: The conversation this Activity happened in (the Super
        Chat for a turn; the sub-agent's own conversation for a sub-agent
        Activity).
    :param source: Where the work came from: :data:`SOURCE_TURN`,
        :data:`SOURCE_BACKGROUND`, :data:`SOURCE_SCHEDULED`,
        :data:`SOURCE_HOUSEKEEPING`.
    :param title: Short task title: model-written once generated, else the
        request tidied (see :mod:`omnigent.superchat.activity.titles`).
    :param outcome: One-line outcome, or ``None`` when nothing to report yet.
    :param summary: A short one-line gist of the outcome for the feed row, or
        ``None`` while in progress (the live Step stands in) or when there is none.
    :param title_prompt: Set only while a model-written title or result summary is still
        missing: the text to write them from. Never serialized.
    :param stored_summary: The model-written result summary, once stored. Never serialized.
    :param status: One of :data:`STATUS_IN_PROGRESS`, :data:`STATUS_DONE`,
        :data:`STATUS_FAILED`, :data:`STATUS_CANCELLED`.
    :param started_at: Unix epoch timestamp of the first item.
    :param finished_at: Unix epoch timestamp of the last item, or ``None``
        while still in progress.
    :param steps: The Activity's Steps, chronological.
    :param parent_chat_id: For a Sub-agent, the chat that started it (the Super Chat, or
        another Sub-agent when it is a sub-worker); ``None`` for a turn.
    :param artifact: For a studio turn (:data:`SOURCE_STUDIO`), the file it changed:
        ``{"group_id": ..., "name": ...}`` plus ``"round_id"`` (the Round that created the
        file) when known; ``None`` otherwise.
    :param summary_line: What the work did, in one line under the reply (ADR 0027), e.g.
        ``"Created 2 files, searched the web, and 12 more steps"``; ``None`` with no steps.
    :param paused: For a Helper, whether it is paused (its ``superchat.paused`` label is set);
        ``None`` for a turn.
    :param title_round_id: For a row named by its Round's ask, that Round: the model-written
        title is stored once per Round, under this id.
    :param round_id: The Round the work is in (ADR 0029): a turn's, from the record; a Helper's,
        the Round it was started in. ``None`` outside any Round.
    """

    id: str
    kind: str
    chat_id: str
    title: str
    outcome: str | None
    status: str
    started_at: int
    finished_at: int | None
    steps: list[Step] = field(default_factory=list)
    source: str = SOURCE_TURN
    summary: str | None = None
    title_prompt: str | None = None
    stored_summary: str | None = None
    parent_chat_id: str | None = None
    artifact: dict[str, str] | None = None
    summary_line: str | None = None
    paused: bool | None = None
    round_id: str | None = None
    title_round_id: str | None = None


#: The families of tools a step belongs to (:func:`step_family`).
FAMILY_WEB, FAMILY_FILES, FAMILY_DECKS, FAMILY_SHELL, FAMILY_BROWSER = (
    "web",
    "files",
    "decks",
    "shell",
    "browser",
)
#: The kinds of step the summary line names (:func:`step_kind`), in the order it names them.
KIND_FILE, KIND_WEB, KIND_BRIEF, KIND_RECALL, KIND_READ, KIND_HELPER, KIND_COMMAND, KIND_CHECK = (
    "file",
    "web",
    "brief",
    "recall",
    "read",
    "helper",
    "command",
    "check",
)
#: Calls that show something rather than do work: never counted as steps of the line.
_SHOWN = frozenset({"render_card", "ask_clarification", "suggest_follow_ups"})
#: At most this many kinds are named; the rest are "N more steps".
_NAMED_KINDS = 2
_RECALLS = frozenset({"recall", "recall_get", "memory_search"})
_PHRASES = {
    KIND_WEB: "searched the web",
    KIND_BRIEF: "checked what we already have",
    KIND_RECALL: "recalled earlier work",
    KIND_READ: "read your files",
}


def step_kind(tool_name: str) -> str | None:
    """The kind of step a tool call is, for the summary line; ``None`` for any other step."""
    base = tool_name.rsplit("__", 1)[-1]
    if base in ("artifact_save", "deck_export"):
        return KIND_FILE
    if base in ("web_search", "web_fetch") or base.startswith("browser_"):
        return KIND_WEB
    if base == "context_brief":
        return KIND_BRIEF
    if base in _RECALLS:
        return KIND_RECALL
    if base.startswith("files_"):
        return KIND_READ
    if base == "start_helper":
        return KIND_HELPER
    if base.startswith("sys_os_"):
        return KIND_COMMAND
    if base.endswith("_check"):
        return KIND_CHECK
    return None


def _counted(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(n=count)


def _kind_phrase(kind: str, count: int) -> str:
    if kind == KIND_FILE:
        return _counted(count, "created 1 file", "created {n} files")
    if kind == KIND_HELPER:
        return _counted(count, "started a helper", "started {n} helpers")
    if kind == KIND_COMMAND:
        return _counted(count, "ran a command", "ran {n} commands")
    return _PHRASES.get(kind, "checked the work")


def summary_line(calls: list[tuple[str, str | None]]) -> str | None:
    """One line of what the work did, from its calls ``(tool, the file it names)``, e.g.
    ``"Created 2 files, searched the web, and 12 more steps"``.

    Files are counted once per name saved; the other kinds by call. At most two kinds are named
    (files first, then in the order they happened); every other step is "N more steps".
    """
    steps = [(tool, file) for tool, file in calls if tool.rsplit("__", 1)[-1] not in _SHOWN]
    if not steps:
        return None
    files: set[str] = set()
    counts: dict[str, int] = {}
    for index, (tool, file) in enumerate(steps):
        kind = step_kind(tool)
        if kind is None:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        if kind == KIND_FILE:
            files.add(file or f"#{index}")
    order = sorted(counts, key=lambda kind: kind != KIND_FILE)[:_NAMED_KINDS]
    phrases = [_kind_phrase(k, len(files) if k == KIND_FILE else counts[k]) for k in order]
    rest = len(steps) - sum(counts[k] for k in order)
    if not phrases:
        return _counted(rest, "1 step", "{n} steps")
    if rest:
        phrases.append(_counted(rest, "1 more step", "{n} more steps"))
    if len(phrases) == 1:
        line = phrases[0]
    elif len(phrases) == 2:
        line = f"{phrases[0]} and {phrases[1]}"
    else:
        line = f"{', '.join(phrases[:-1])}, and {phrases[-1]}"
    return line[0].upper() + line[1:]


def step_family(tool_name: str) -> str | None:
    """The family of tools a call belongs to, for the web's icons: ``web``, ``files``,
    ``decks``, ``shell`` or ``browser``; ``None`` for any other."""
    base = tool_name.rsplit("__", 1)[-1]
    if base in ("web_search", "web_fetch"):
        return FAMILY_WEB
    if base.startswith("browser_"):
        return FAMILY_BROWSER
    if base.startswith("deck_"):
        return FAMILY_DECKS
    if base.startswith("sys_os_"):
        return FAMILY_SHELL
    if base.startswith(("files_", "artifact_")):
        return FAMILY_FILES
    return None


def step_snippet(tool_name: str, output: str | None) -> str | None:
    """A line of what a step found: the brief's first line (its count of what exists)."""
    if tool_name.rsplit("__", 1)[-1] != "context_brief" or not output:
        return None
    return next(
        (
            truncate(line.strip(), _OUTCOME_MAX_CHARS)
            for line in output.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ),
        None,
    )


def step_title_for_call(tool_name: str, raw_arguments: str, output: str | None = None) -> str:
    """Deterministic, plain-language title for one tool call.

    A small set of named templates covers the tools called out in the
    plan; everything else falls back to ``"Used <tool in words>"`` (``"Used web search"``),
    never the raw tool identifier. No LLM call.

    :param tool_name: The invoked tool's name, e.g. ``"memory_search"``.
    :param raw_arguments: The call's JSON-encoded arguments string.
    :param output: The call's output, when a title names what it read (``artifact_outline``).
    :returns: A one-line, user-facing description.
    """
    args = _args_dict(raw_arguments)
    if tool_name == "context_brief":
        return "Checked what we already have"
    if tool_name == "artifact_outline":
        name = _args_dict(output or "").get("name")
        return (
            f"Reviewed {name}" if isinstance(name, str) and name.strip() else "Reviewed the file"
        )
    if tool_name == "load_skill":
        return "Looked up the deck layouts" if args.get("name") == "decks" else "Looked up a guide"
    if tool_name == "memory_search":
        query = args.get("query") or args.get("q")
        return f"Searched memory for '{query}'" if query else "Searched memory"
    if tool_name == "session_history":
        action = args.get("action")
        if action == "read":
            return "Read earlier messages"
        if action == "list_chats":
            return "Listed chats"
        return "Looked at earlier messages"
    if tool_name == "sys_session_create":
        title = args.get("title")
        return f"Launched a sub-agent '{title}'" if title else "Launched a sub-agent"
    if tool_name == "sys_session_send":
        title = args.get("title")
        return (
            f"Sent a message to sub-agent '{title}'" if title else "Sent a message to a sub-agent"
        )
    if tool_name in ("report_progress", "raise_flag"):
        text = " ".join(str(args.get("text") or "").split())
        if tool_name == "raise_flag":
            return f"Flagged: {text}" if text else "Flagged something to check"
        return text or "Finished a step"
    if tool_name == "artifact_save":
        title = args.get("title")
        name = str(args.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
        shown = title if isinstance(title, str) and title.strip() else name
        return f"Saved '{shown.strip()}'" if shown.strip() else "Saved a file"
    if tool_name == "artifact_patch":
        patches = args.get("patches")
        count = len(patches) if isinstance(patches, list) else 0
        return f"Made {count} edits to the file" if count > 1 else "Edited the file"
    words = tool_name.removeprefix("sys_").replace("_", " ").strip()
    return f"Used {words}" if words else "Used a tool"


def _args_dict(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_user_message(items: list[ConversationItem]) -> ConversationItem | None:
    for item in items:
        if item.type == "message" and getattr(item.data, "role", None) == "user":
            if message_text(item):
                return item
    return None


def _person_request(item: ConversationItem | None) -> str | None:
    """The person's own words in a user message, or ``None`` for a system-started turn
    (a Result wake) or no message."""
    if item is None or not isinstance(item.data, MessageData) or is_system_notice(item.data):
        return None
    return message_text(item)


def _first_error_message(items: list[ConversationItem]) -> str | None:
    for item in items:
        if item.type == "error":
            return item.data.message  # type: ignore[union-attr]
    return None


def _group_items_by_response(items: list[ConversationItem]) -> list[list[ConversationItem]]:
    """Group a chronological item list into per-turn (``response_id``) runs."""
    groups: dict[str, list[ConversationItem]] = {}
    order: list[str] = []
    for item in items:
        if item.response_id not in groups:
            groups[item.response_id] = []
            order.append(item.response_id)
        groups[item.response_id].append(item)
    return [groups[response_id] for response_id in order]


def _step_detail(
    call: ConversationItem,
    output: ConversationItem | None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"call": _project_activity_item(call)}
    if output is not None:
        detail["result"] = _project_activity_item(output)
    return detail


def _steps_for_calls(
    calls: list[ConversationItem],
    outputs_by_call_id: dict[str, ConversationItem],
    *,
    include_detail: bool,
    live: bool = False,
) -> list[Step]:
    """Steps for ``calls``; a ``live`` Activity's latest Step carries its call (the
    arguments name what it is doing right now, e.g. the query being searched)."""
    steps: list[Step] = []
    for index, call in enumerate(calls):
        name = call.data.name  # type: ignore[union-attr]
        arguments = call.data.arguments  # type: ignore[union-attr]
        call_id = call.data.call_id  # type: ignore[union-attr]
        result = outputs_by_call_id.get(call_id)
        output = result.data.output if result is not None else None  # type: ignore[union-attr]
        steps.append(
            Step(
                item_id=call.id,
                title=step_title_for_call(name, arguments, output),
                created_at=call.created_at,
                tool=name,
                kind=step_kind(name),
                snippet=step_snippet(name, output),
                detail=(
                    _step_detail(call, outputs_by_call_id.get(call_id))
                    if include_detail
                    else {"call": _project_activity_item(call)}
                    if live and index == len(calls) - 1
                    else None
                ),
            )
        )
    return steps


def _turn_status(
    conversation: Conversation,
    group: list[ConversationItem],
    *,
    is_latest_group: bool,
) -> str:
    """Map a turn's items (+ live session status) to an Activity status.

    An ``error`` item in the turn is Failed; an interrupted assistant
    message (the engine's own durable partial-response marker) is
    Cancelled; the latest turn still running/waiting on the live session
    is In Progress; everything else is Done.
    """
    if any(item.type == "error" for item in group):
        return STATUS_FAILED
    if any(item.type == "message" and getattr(item.data, "interrupted", False) for item in group):
        return STATUS_CANCELLED
    if is_latest_group and conversation.live_status in ("running", "waiting"):
        return STATUS_IN_PROGRESS
    if is_latest_group and conversation.live_status == "failed":
        return STATUS_FAILED
    return STATUS_DONE


def _turn_outcome(group: list[ConversationItem], *, status: str) -> str | None:
    if status == STATUS_FAILED:
        return _first_error_message(group) or "Could not complete the request"
    if status == STATUS_CANCELLED:
        return "Cancelled before finishing"
    return _reply_outcome(last_assistant_text(group))


def _reply_outcome(reply: str | None) -> str | None:
    """A reply as the one-line outcome, its acknowledgement opener ("Perfect.") dropped."""
    opened = strip_reply_filler(reply)
    return truncate(opened, _OUTCOME_MAX_CHARS) if opened else None


def _status_summary(
    status: str,
    group: list[ConversationItem],
    outcome: str | None,
    stored_summary: str | None = None,
) -> str | None:
    """The feed row's one-line gist: the model-written result summary, else the answer's
    first sentence; nothing while working."""
    if status == STATUS_IN_PROGRESS:
        return None
    if status == STATUS_DONE:
        return stored_summary or one_line_summary(last_assistant_text(group))
    return outcome


def _turn_title(conversation: Conversation, request: str | None, reply: str | None = None) -> str:
    # Until a model-written title exists, the person's request names the work
    # (tidied); a system-started turn (a Result wake) is named by what it said,
    # then by the chat's title.
    tidied = tidy_request_title(request or _plain_reply(reply))
    if tidied:
        return tidied
    if conversation.title:
        return truncate(conversation.title, _TITLE_MAX_CHARS)
    return "Activity"


def _plain_reply(reply: str | None) -> str | None:
    return None if not reply or STRUCTURED_TEXT.match(reply) else reply


def _turn_title_prompt(request: str, steps: list[Step], result: str | None = None) -> str:
    """What a title is written from: the request, the first few things done for it and,
    once finished, the result to summarise."""
    done = "; ".join(step.title for step in steps[:_TITLE_PROMPT_STEPS])
    prompt = f"Request to name:\n{truncate(request, _TITLE_PROMPT_REQUEST_CHARS)}"
    if done:
        prompt = f"{prompt}\n\nWhat was done for it: {done}"
    if result:
        prompt = f"{prompt}\n\nResult:\n{truncate(result, _TITLE_PROMPT_RESULT_CHARS)}"
    return prompt


def _summarisable_result(reply: str | None) -> str | None:
    """The reply as a Result worth summarising: plain words, not a code block or JSON."""
    return _plain_reply(strip_reply_filler(reply))


def _turn_activity(
    conversation: Conversation,
    group: list[ConversationItem],
    *,
    request: str | None,
    is_latest_group: bool,
    include_step_detail: bool,
    stored_title: str | None,
    stored_summary: str | None = None,
) -> Activity | None:
    """Build the Activity for one turn's items, or ``None`` if it had no tool calls."""
    calls = [item for item in group if item.type == "function_call"]
    if not calls:
        return None
    outputs_by_call_id = {
        item.data.call_id: item  # type: ignore[union-attr]
        for item in group
        if item.type == "function_call_output"
    }
    status = _turn_status(conversation, group, is_latest_group=is_latest_group)
    steps = _steps_for_calls(
        calls,
        outputs_by_call_id,
        include_detail=include_step_detail,
        live=status == STATUS_IN_PROGRESS,
    )
    # A system-started turn has no request of the person's: the reply names the work.
    reply = last_assistant_text(group)
    user_request = request or _plain_reply(reply)
    outcome = _turn_outcome(group, status=status)
    result = _summarisable_result(reply) if status == STATUS_DONE else None
    needs_label = not stored_title or (result is not None and not stored_summary)
    return Activity(
        id=turn_activity_id(conversation.id, group[0].response_id),
        kind=KIND_TURN,
        chat_id=conversation.id,
        title=stored_title or _turn_title(conversation, request, reply),
        outcome=outcome,
        status=status,
        started_at=group[0].created_at,
        finished_at=None if status == STATUS_IN_PROGRESS else group[-1].created_at,
        steps=steps,
        source=SOURCE_TURN,
        summary=_status_summary(status, group, outcome, stored_summary),
        title_prompt=(
            _turn_title_prompt(user_request, steps, result)
            if user_request and needs_label
            else None
        ),
        stored_summary=stored_summary,
        summary_line=_calls_line(calls),
    )


def _ask_title(ask: str) -> str:
    """A Round's ask as a row title: its first line, tidied, else clipped."""
    line = next((line.strip() for line in ask.splitlines() if line.strip()), ask)
    return tidy_request_title(line) or truncate(line, _TITLE_MAX_CHARS)


def _round_steps(
    items: list[ConversationItem], view: Mapping[str, Any], *, include_detail: bool
) -> list[Step]:
    """A Round's steps: its view's trail and, while it runs, its live tool last, as the strip
    shows them. A step's call and result come from the stored items only for a full read."""
    now = view["now"]
    trail = [*view["trail"], *([now] if now else [])]
    calls, outputs = {}, {}
    for item in items:
        if item.type in ("function_call", "function_call_output"):
            by_type = calls if item.type == "function_call" else outputs
            by_type[item.data.call_id] = item  # type: ignore[union-attr]
    steps = []
    for index, entry in enumerate(trail):
        name, call_id = str(entry.get("name") or ""), entry.get("call_id")
        call = calls.get(call_id) if call_id else None
        live = entry is now
        detail = None
        if call is not None and include_detail:
            detail = _step_detail(call, outputs.get(call_id))
        elif call is not None and live:
            detail = {"call": _project_activity_item(call)}
        steps.append(
            Step(
                item_id=call_id or f"step-{index}",
                title=step_title_for_call(name, "{}"),
                created_at=int(entry.get("t") or entry.get("since") or 0),
                tool=name or None,
                kind=step_kind(name),
                detail=detail,
                digest=entry.get("digest"),
                helper_id=entry.get("helper_id"),
                ok=None if live else bool(entry.get("ok", True)),
            )
        )
    return steps


def _failed_outcome(reason: str | None) -> str:
    """A failed Round's outcome: the error its turn ended with, never the engine's own reason
    (a lost relay, a reset, a Round no turn came for)."""
    if reason and reason not in (RELAY_LOST, RESET, NO_TURN):
        return truncate(" ".join(reason.split()), _OUTCOME_MAX_CHARS)
    return "Could not complete the request"


def _round_activity(
    conversation: Conversation,
    items: list[ConversationItem],
    round_id: str,
    view: Mapping[str, Any],
    *,
    include_step_detail: bool,
    stored_title: str | None,
) -> Activity | None:
    """One Round's row, read from its folded *view*, as the strip reads it: the ask names it,
    and its status, outcome and steps (:func:`_round_steps`) are the Round's own. ``None`` when
    the Round called no tool (a quick answer)."""
    steps = _round_steps(items, view, include_detail=include_step_detail)
    if not steps:
        return None
    status = _ROUND_STATUS.get(view["status"], STATUS_IN_PROGRESS)
    said = [words for words in view["words"] if words.strip()]
    if status == STATUS_FAILED:
        outcome = _failed_outcome(view["reason"])
    elif status == STATUS_CANCELLED:
        outcome = view["stop_line"] or "Stopped"
    elif status == STATUS_DONE:
        outcome = (one_line_summary(said[-1]) if said else None) or "Done"
    else:
        outcome = None
    settled_at = view["settled_at"]
    return Activity(
        id=turn_activity_id(conversation.id, items[0].response_id),
        kind=KIND_TURN,
        chat_id=conversation.id,
        title=stored_title or _ask_title(view["ask"]),
        outcome=outcome,
        status=status,
        started_at=int(view["started_at"]),
        finished_at=None
        if status == STATUS_IN_PROGRESS or settled_at is None
        else int(settled_at),
        steps=steps,
        source=SOURCE_TURN,
        summary=outcome,
        title_prompt=None if stored_title else _turn_title_prompt(view["ask"], steps),
        summary_line=summary_line([(step.tool or "", step.digest) for step in steps]),
        round_id=round_id,
        title_round_id=round_id,
    )


#: ``(conversation id, round id) -> its view`` (``None``: not shown), read once per feed read:
#: every Helper of a Round reads the Round its parent folded.
_Views = dict[tuple[str, str], dict[str, Any] | None]


def _round_views(
    conversation_id: str, round_ids: Iterable[str | None], seen: _Views | None = None
) -> dict[str, Any]:
    """The folded view of each recorded Round a person opened (:data:`ROUND_VIEWS`); empty
    without the rounds capability or when its read fails (a row then reads as before). The
    views already in *seen* are not read again, and the ones read are added to it."""
    seen = {} if seen is None else seen
    wanted = list(dict.fromkeys(r for r in round_ids if r))
    missing = [r for r in wanted if (conversation_id, r) not in seen]
    for read in ROUND_VIEWS.values() if missing else ():
        try:
            views = read(conversation_id, missing)
        except Exception:
            _logger.warning("round views of %s not read", conversation_id, exc_info=True)
            return {}
        for r in missing:
            view = views.get(r)
            seen[conversation_id, r] = view if view and view["ask"] and not view["hidden"] else None
        break
    return {r: v for r in wanted if (v := seen.get((conversation_id, r))) is not None}


def _calls_line(calls: list[ConversationItem]) -> str | None:
    named = []
    for call in calls:
        args = _args_dict(call.data.arguments)  # type: ignore[union-attr]
        path = str(args.get("path") or args.get("name") or "").rstrip("/")
        named.append((call.data.name, path.rsplit("/", 1)[-1] or None))  # type: ignore[union-attr]
    return summary_line(named)


def _split_sub_agent_title(conversation: Conversation) -> tuple[str, str]:
    """Best-effort ``(agent, display_title)`` split of a sub-agent's title.

    Named sub-agents (``sys_session_send`` / ``sys_session_close``) persist
    ``"<agent>:<title>"``; ``sys_session_create`` children may carry any
    title. Both are handled leniently (no raise) since an Activity read
    must never fail on an odd title.
    """
    title = title_without_closed_marker(conversation.title)
    if title and ":" in title:
        agent, _, rest = title.partition(":")
        return agent, rest
    return (conversation.sub_agent_name or "sub-agent"), (title or "")


def _sub_agent_source(conversation: Conversation) -> str:
    """Where a Helper came from: a scheduled run (an automation) or background."""
    if ADHOC_HELPER_LABEL_KEY in conversation.labels:
        return SOURCE_HOUSEKEEPING
    if SCHEDULED_HELPER_LABEL_KEY not in conversation.labels:
        return SOURCE_BACKGROUND
    return SOURCE_SCHEDULED


def _sub_agent_title(
    conversation: Conversation,
    display_title: str,
    task: str | None,
    source: str,
) -> tuple[str, str | None]:
    """``(title, title_prompt)`` for a Helper: its own words about the work, never an id.

    A model-written ``task_summary`` wins; a scheduled Helper shows its task's name
    (never the engine's collision suffix); a spawner-chosen title is kept. Otherwise the
    delegated task is tidied for now and, with ``title_prompt`` set, named by the model.
    """
    if conversation.task_summary:
        return strip_fire_suffix(scrub_internal_words(conversation.task_summary)), None
    if source != SOURCE_BACKGROUND:
        named = scheduled_display_title(display_title)
        if named:
            return named, None
    elif not is_generic_title(display_title):
        return truncate(strip_fire_suffix(display_title), _TITLE_MAX_CHARS), None
    return tidy_request_title(task) or (
        "Working on something" if source == SOURCE_BACKGROUND else "Scheduled work"
    ), (f"Request to name:\n{truncate(task, _TITLE_PROMPT_REQUEST_CHARS)}" if task else None)


def _sub_agent_activity(
    conversation: Conversation,
    items: list[ConversationItem],
    *,
    include_step_detail: bool,
    stored_summary: str | None = None,
    helper: Mapping[str, Any] | None = None,
) -> Activity:
    """A Helper's row; one started in a recorded Round reads its status, title and result from
    the Round's view (*helper*), as the strip does, else from its session. The view's title is
    the one the Helper was started with until its model-written name (``helper.titled``)."""
    _agent, display_title = _split_sub_agent_title(conversation)
    status = (
        _ROUND_STATUS.get(helper["status"], STATUS_IN_PROGRESS)
        if helper is not None
        else sub_agent_status(conversation, items)
    )
    source = _sub_agent_source(conversation)
    named = helper["title"] if helper is not None else None
    if named and not is_generic_title(named):
        title, title_prompt = truncate(named, _TITLE_MAX_CHARS), None
    else:
        title, title_prompt = _sub_agent_title(
            conversation, display_title, _person_request(_first_user_message(items)), source
        )
    calls = [item for item in items if item.type == "function_call"]
    outputs_by_call_id = {
        item.data.call_id: item  # type: ignore[union-attr]
        for item in items
        if item.type == "function_call_output"
    }
    result = _summarisable_result(last_assistant_text(items)) if status == STATUS_DONE else None
    task = _person_request(_first_user_message(items))
    if result and task and (title_prompt is not None or not stored_summary):
        title_prompt = _turn_title_prompt(task, [], result)
    if status == STATUS_FAILED:
        outcome = _first_error_message(items) or (
            "Could not get started"
            if conversation.live_status is None
            else "Could not complete the task"
        )
    elif status == STATUS_CANCELLED:
        outcome = "Cancelled before finishing"
    elif helper is not None and helper["result"]:
        outcome = _reply_outcome(helper["result"])
    else:
        outcome = _reply_outcome(last_assistant_text(items))
    started_at = items[0].created_at if items else conversation.created_at
    finished_at = items[-1].created_at if items and status != STATUS_IN_PROGRESS else None
    if helper is not None and status != STATUS_IN_PROGRESS and helper["finished_at"]:
        finished_at = int(helper["finished_at"])
    return Activity(
        id=f"{KIND_SUB_AGENT}:{conversation.id}",
        kind=KIND_SUB_AGENT,
        chat_id=conversation.id,
        title=title,
        outcome=outcome,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        steps=_steps_for_calls(
            calls,
            outputs_by_call_id,
            include_detail=include_step_detail,
            live=status == STATUS_IN_PROGRESS,
        ),
        source=source,
        summary=_status_summary(status, items, outcome, stored_summary),
        title_prompt=title_prompt,
        stored_summary=stored_summary,
        parent_chat_id=conversation.parent_conversation_id,
        summary_line=_calls_line(calls),
        paused=bool((conversation.labels or {}).get(PAUSED_LABEL_KEY)),
        round_id=(conversation.labels or {}).get(ROUND_LABEL_KEY) or None,
    )


# ── Store-backed orchestration ─────────────────────────────────────────


def _activities_for_conversation(
    conv_store: ConversationStore,
    conversation: Conversation,
    *,
    include_step_detail: bool,
    views_seen: _Views | None = None,
) -> list[Activity]:
    # Newest-first, bounded, then reversed to chronological order: for a
    # conversation with more than _ITEMS_SCAN_LIMIT items, an ascending scan
    # would return only the OLDEST items and silently miss the current turn
    # — the newest turns and the live/in-progress status must always be
    # covered, so the truncation (if any) falls on the oldest history
    # instead.
    items = list(
        reversed(
            conv_store.list_items(
                conversation.id, limit=_resolve_items_scan_limit(), order="desc"
            ).data
        )
    )
    studio = is_artifact_chat(conversation.labels)
    if conversation.kind == "sub_agent" and not studio:
        _title, summary = conv_store.get_activity_labels([conversation.id]).get(
            conversation.id, (None, None)
        )
        round_id = (conversation.labels or {}).get(ROUND_LABEL_KEY) or None
        view = _round_views(
            conversation.root_conversation_id or conversation.id, [round_id], views_seen
        ).get(round_id or "")
        return [
            _sub_agent_activity(
                conversation,
                items,
                include_step_detail=include_step_detail,
                stored_summary=summary,
                helper=view["helpers"].get(conversation.id) if view else None,
            )
        ]
    # What the engine recorded outside a turn (a picked-up topic) is no run of its own.
    groups = [
        group
        for group in _group_items_by_response(items)
        if not group[0].response_id.startswith(RECORDED_RESPONSE_PREFIX)
    ]
    rounds = _item_rounds(conversation.id, items) if not studio else {}
    views = _round_views(conversation.id, rounds.values(), views_seen)
    stored_labels = conv_store.get_activity_labels(
        [group[0].response_id for group in groups] + list(views)
    )
    activities: list[Activity] = []
    # A recorded Round is one row read from its view, whatever turns it took.
    in_round: dict[str, list[ConversationItem]] = {}
    for group in groups:
        round_id = rounds.get(group[0].id)
        if round_id in views:
            in_round.setdefault(round_id, []).extend(group)
    for round_id, round_items in in_round.items():
        row = _round_activity(
            conversation,
            round_items,
            round_id,
            views[round_id],
            include_step_detail=include_step_detail,
            stored_title=stored_labels.get(round_id, (None, None))[0],
        )
        if row is not None:
            activities.append(row)
    # The user message that started a turn is usually stored outside its
    # response group, so carry the latest one forward.
    request: str | None = None
    for index, group in enumerate(groups):
        first = _first_user_message(group)
        if first is not None:
            # A studio's runtime lines (versions saved elsewhere) never start a turn there.
            asked = _person_request(first)
            if asked is not None or not studio:
                request = asked
        round_id = rounds.get(group[0].id)
        if round_id in views:
            continue
        activity = _turn_activity(
            conversation,
            group,
            request=request,
            is_latest_group=(index == len(groups) - 1),
            include_step_detail=include_step_detail,
            stored_title=stored_labels.get(group[0].response_id, (None, None))[0],
            stored_summary=stored_labels.get(group[0].response_id, (None, None))[1],
        )
        if activity is not None:
            activities.append(replace(activity, round_id=round_id))
    activities.sort(key=lambda activity: activity.started_at)
    if studio:
        artifact = _studio_file(conversation)
        return [replace(a, source=SOURCE_STUDIO, artifact=artifact) for a in activities]
    return activities


def _item_rounds(conversation_id: str, items: list[ConversationItem]) -> dict[str, str | None]:
    """``item id -> the Round it is in`` (:data:`ROUND_IDS`); empty without the rounds
    capability or when its read fails (a row is never lost for its Round)."""
    ids = [item.id for item in items]
    for read in ROUND_IDS.values():
        try:
            return dict(zip(ids, read(conversation_id, ids), strict=True))
        except Exception:
            _logger.warning("rounds of %s not read", conversation_id, exc_info=True)
    return {}


def _studio_file(conversation: Conversation) -> dict[str, str]:
    """The file a studio edits: its group id and name (the group id when no name is known),
    and the Round that created it when known."""
    group_id = conversation.labels.get(ARTIFACT_CHAT_LABEL_KEY, "")
    name: str | None = None
    for namer in FILE_NAMES.values():
        try:
            name = namer(group_id)
        except Exception:  # noqa: BLE001 - a name is a label, never a reason to fail a read
            name = None
        if name:
            break
    file = {"group_id": group_id, "name": name or group_id}
    for rounder in FILE_ROUNDS.values():
        try:
            round_id = rounder(group_id)
        except Exception:  # noqa: BLE001 - grouping only, never a reason to fail a read
            round_id = None
        if round_id:
            file["round_id"] = round_id
            break
    return file


def _zone(tz: str | None) -> tzinfo:
    try:
        return ZoneInfo(tz) if tz else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def local_day(epoch_seconds: int, tz: str | None = None) -> str:
    """Calendar day (``YYYY-MM-DD``) of ``epoch_seconds`` in the person's time zone."""
    return datetime.fromtimestamp(epoch_seconds, tz=_zone(tz)).date().isoformat()


def _did_real_work(activity: Activity) -> bool:
    """Whether a conversation turn is worth a row: it did work, not just answered in chat."""
    if len(activity.steps) >= _MIN_TURN_STEPS:
        return True
    # One search, page read, or file step is still work the person may want to trace.
    if any(step.tool and is_work_tool(step.tool) for step in activity.steps):
        return True
    end = activity.finished_at if activity.finished_at is not None else activity.started_at
    return end - activity.started_at > _MIN_TURN_SECONDS


def _housekeeping_row(day: str, runs: list[Activity]) -> Activity:
    """One row for a local day's quiet upkeep runs; its Steps are the runs themselves."""
    runs = sorted(runs, key=lambda run: run.started_at)
    live = any(run.status == STATUS_IN_PROGRESS for run in runs)
    count = len(runs)
    return Activity(
        id=f"{KIND_SUB_AGENT}:{HOUSEKEEPING_PREFIX}:{day}",
        kind=KIND_SUB_AGENT,
        chat_id=runs[-1].chat_id,
        title=HOUSEKEEPING_TITLE,
        outcome=None,
        status=STATUS_IN_PROGRESS if live else STATUS_DONE,
        started_at=runs[-1].started_at,
        finished_at=None if live else max(run.finished_at or run.started_at for run in runs),
        steps=[
            Step(
                item_id=run.id,
                title=scrub_internal_words(strip_fire_suffix(run.title)) or "Updated your notes",
                created_at=run.started_at,
            )
            for run in runs
        ],
        source=SOURCE_HOUSEKEEPING,
        summary=f"{count} update{'s' if count != 1 else ''}",
    )


def _feed(activities: list[Activity], tz: str | None) -> list[Activity]:
    """The feed's rows: work not visible in chat, upkeep grouped into one row per local day."""
    rows: list[Activity] = []
    upkeep: dict[str, list[Activity]] = {}
    for activity in activities:
        if activity.source == SOURCE_HOUSEKEEPING:
            upkeep.setdefault(local_day(activity.started_at, tz), []).append(activity)
        elif (
            activity.kind != KIND_TURN
            or activity.source == SOURCE_STUDIO
            or _did_real_work(activity)
        ):
            rows.append(activity)
    rows.extend(_housekeeping_row(day, runs) for day, runs in upkeep.items())
    rows.sort(key=lambda activity: activity.started_at, reverse=True)
    return rows


def list_activities(
    conv_store: ConversationStore,
    session_id: str,
    *,
    before: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    tz: str | None = None,
) -> list[Activity]:
    """The user's Activity Feed: every Activity under one Super Chat's family.

    :param conv_store: Store to query.
    :param session_id: The Super Chat id.
    :param before: When set, only Activities that started strictly before
        this epoch timestamp.
    :param limit: Maximum Activities to return, newest-first (clamped to
        :data:`_MAX_LIMIT`).
    :param tz: The person's IANA time zone; upkeep runs are grouped by their local day.
    :returns: Activities newest-first; ``[]`` when ``session_id`` doesn't
        resolve to a ``superside-chat`` Super Chat.
    """
    super_chat_id = resolve_super_chat_id(conv_store, session_id)
    if super_chat_id is None:
        return []
    bounded_limit = max(1, min(limit, _MAX_LIMIT))
    # Newest chats first: once the page is full, a chat last written before its oldest row
    # has no row on it and is never read, so a read costs the page, not the whole history.
    # A file's studio (ADR 0024) is never one row or a Helper: each of its turns is a row.
    chats = sorted(
        list_chat_family(conv_store, super_chat_id),
        key=lambda chat: chat.updated_at,
        reverse=True,
    )
    activities: list[Activity] = []
    seen: _Views = {}
    page: list[Activity] = []
    for conversation in chats:
        if len(page) == bounded_limit and _off_page(conversation, page[-1].started_at, tz):
            continue
        activities.extend(
            _activities_for_conversation(
                conv_store, conversation, include_step_detail=False, views_seen=seen
            )
        )
        page = _page(activities, tz, before, bounded_limit)
    return page


def _page(
    activities: list[Activity], tz: str | None, before: int | None, limit: int
) -> list[Activity]:
    """The feed's newest *limit* rows started before *before*."""
    rows = _feed(activities, tz)
    if before is not None:
        rows = [row for row in rows if row.started_at < before]
    return rows[:limit]


def _off_page(conversation: Conversation, oldest: int, tz: str | None) -> bool:
    """Whether none of *conversation*'s rows can be on a full page whose oldest row started at
    *oldest*: every row of a chat starts by its last write, give or take
    :data:`_LAST_WRITE_SLACK`. An upkeep run joins its local day's row, so one is left out only
    before that row's day."""
    last = conversation.updated_at + _LAST_WRITE_SLACK
    if last >= oldest:
        return False
    studio = is_artifact_chat(conversation.labels)
    if conversation.kind == "sub_agent" and not studio:
        if _sub_agent_source(conversation) == SOURCE_HOUSEKEEPING:
            return local_day(last, tz) < local_day(oldest, tz)
    return True


def activities_missing_titles(activities: list[Activity]) -> list[Activity]:
    """The Activities still showing a fallback title, newest first, bounded per read."""
    return [activity for activity in activities if activity.title_prompt][:_TITLE_JOBS_PER_READ]


def schedule_missing_titles(
    conv_store: ConversationStore,
    coordinator: BackgroundSessionTitleCoordinator,
    activities: list[Activity],
    conversations: Mapping[str, Conversation],
) -> None:
    """Start model-written titles for Activities from :func:`activities_missing_titles`.

    Never waits: the feed answers now with the tidied request, and the title
    appears on a later read once the cheap model has written it (a turn's is
    stored by response id, a Helper's as its ``task_summary``). Must run on the
    event loop (the attempts are loop tasks); ``conversations`` is read beforehand.

    :param conv_store: Store the titles are saved to.
    :param coordinator: The app's background title coordinator.
    :param activities: The Activities to title.
    :param conversations: Their chats by id (an Activity with no chat here is skipped).
    """
    for activity in activities:
        conversation = conversations.get(activity.chat_id)
        if conversation is None or activity.title_prompt is None:
            continue
        chat_id = activity.chat_id
        # A result the model gave no summary for keeps the deterministic gist, so a
        # finished row is never summarised twice.
        fallback = activity.summary if activity.status == STATUS_DONE else None
        is_turn = activity.kind == KIND_TURN
        key = label_key = activity.id.rsplit(":", 1)[-1] if is_turn else chat_id
        # A Round's row is titled once per Round, under the Round's id.
        key = label_key = activity.title_round_id or key

        def save(
            label: str,
            chat_id: str = chat_id,
            label_key: str = label_key,
            fallback: str | None = fallback,
            is_turn: bool = is_turn,
            has_title: bool = bool(conversation.task_summary),
        ) -> object:
            title, summary = split_generated_label(label)
            if not is_turn and not has_title:
                titled = conv_store.set_task_summary(chat_id, title)
                if titled is not None:
                    helper_titled(titled)
            return conv_store.set_activity_title(chat_id, label_key, title, summary or fallback)

        coordinator.schedule_activity_title(
            key=key,
            conversation=conversation,
            prompt=activity.title_prompt,
            instructions=ACTIVITY_TITLE_INSTRUCTIONS,
            clean=clean_generated_label,
            save=save,
            turn_started_at=activity.started_at,
        )


def get_activity(
    conv_store: ConversationStore,
    session_id: str,
    activity_id: str,
    tz: str | None = None,
) -> Activity | None:
    """One Activity, in full (steps include capped call/result detail).

    Scoped to ``session_id``'s Super Chat family — an id for an Activity
    outside that family returns ``None``, matching the owner-scoping the
    route layer already enforces on ``session_id`` itself.

    :param conv_store: Store to query.
    :param session_id: The Super Chat id.
    :param activity_id: An id previously returned by :func:`list_activities`.
    :returns: The :class:`Activity` with full step detail, or ``None``.
    """
    super_chat_id = resolve_super_chat_id(conv_store, session_id)
    if super_chat_id is None:
        return None
    kind, _, rest = activity_id.partition(":")
    if kind == KIND_SUB_AGENT and rest.startswith(f"{HOUSEKEEPING_PREFIX}:"):
        for row in list_activities(conv_store, session_id, limit=_MAX_LIMIT, tz=tz):
            if row.id == activity_id:
                return row
        return None
    if kind == KIND_SUB_AGENT:
        chat_id = rest
    elif kind == KIND_TURN:
        chat_id, _, _response_id = rest.partition(":")
    else:
        return None
    family = {chat.id: chat for chat in list_chat_family(conv_store, super_chat_id)}
    conversation = family.get(chat_id)
    if conversation is None or (kind == KIND_SUB_AGENT and is_artifact_chat(conversation.labels)):
        return None
    for activity in _activities_for_conversation(
        conv_store, conversation, include_step_detail=True
    ):
        if activity.id == activity_id:
            return activity
    return None


def iso_date(epoch_seconds: int, tz: str | None = None) -> str:
    """Calendar-day string for grouping Activities by day.

    :param epoch_seconds: Unix epoch timestamp.
    :param tz: The person's IANA time zone (UTC when unset or unknown).
    :returns: ``"YYYY-MM-DD"`` in that zone.
    """
    return local_day(epoch_seconds, tz)


def step_to_dict(step: Step) -> dict[str, Any]:
    """JSON-safe projection of one Step."""
    payload: dict[str, Any] = {
        "item_id": step.item_id,
        "title": step.title,
        "created_at": step.created_at,
    }
    if step.tool is not None:
        payload["tool"] = step.tool
    if step.detail is not None:
        payload["detail"] = step.detail
    if step.kind is not None:
        payload["kind"] = step.kind
    for key in ("snippet", "digest", "helper_id", "ok"):
        if getattr(step, key) is not None:
            payload[key] = getattr(step, key)
    return payload


def step_families(steps: list[Step]) -> list[str]:
    """The distinct families of tools the steps used (:func:`step_family`), first used first."""
    found = (step_family(step.tool) for step in steps if step.tool)
    return list(dict.fromkeys(family for family in found if family))


def activity_to_dict(activity: Activity, tz: str | None = None) -> dict[str, Any]:
    """JSON-safe projection of one Activity, for either route."""
    return {
        "id": activity.id,
        "kind": activity.kind,
        "chat_id": activity.chat_id,
        "source": activity.source,
        "title": activity.title,
        "outcome": activity.outcome,
        "summary": activity.summary,
        "status": activity.status,
        "started_at": activity.started_at,
        "finished_at": activity.finished_at,
        "date": iso_date(activity.started_at, tz),
        "steps": [step_to_dict(step) for step in activity.steps],
        "parent_chat_id": activity.parent_chat_id,
        # A studio turn's file (ADR 0024); absent for every other Activity.
        **({"artifact": activity.artifact} if activity.artifact is not None else {}),
        # ADR 0027: display is the engine's.
        "summary_line": activity.summary_line,
        "families": step_families(activity.steps),
        # A Helper's pause (ADR 0027); absent for a turn.
        **({"paused": activity.paused} if activity.paused is not None else {}),
        "round_id": activity.round_id,
    }
