"""The single registration seam for a Super Chat primitive.

One folder per primitive under ``omnigent/superchat/<name>/`` exposes a :class:`Feature`; the
three framework seams iterate :data:`omnigent.superchat.features.FEATURES` instead of growing a
per-primitive branch each:

* ``tools/manager.py`` registers every feature's label-gated tool defs.
* ``runner/tool_dispatch.py`` routes a feature tool name to its handler.
* ``server/app.py`` calls :func:`install_features` (stores + routers) and runs the jobs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from omnigent.context.labels import SUBAGENT_LABEL_KEY, is_superside_chat

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def is_super_chat(labels: Mapping[str, str] | None) -> bool:
    """True for the top-level Super Chat session itself (never a Helper)."""
    return is_superside_chat(labels) and SUBAGENT_LABEL_KEY not in (labels or {})


def is_helper(labels: Mapping[str, str] | None) -> bool:
    """True for a Super Chat's Helper (sub-agent session)."""
    return is_superside_chat(labels) and SUBAGENT_LABEL_KEY in (labels or {})


@dataclass(frozen=True)
class ToolManagerCtx:
    """What a feature's ``tools`` factory may need from the ToolManager (kept minimal)."""

    spec: Any = None


@dataclass(frozen=True)
class SpawnRequest:
    """One sub-agent launch a handler asks the runner for (the generic create path).

    :param agent: The declared Sub-agent Type to start.
    :param task: The brief (the child's first message).
    :param title: Instance label, or ``None`` to leave naming to the engine.
    :param model: The category: ``light``, ``builder`` or ``foundation``.
    :param reasoning_effort: ``low`` / ``medium`` / ``high``, or ``None`` for the default.
    :param file_ids: Uploaded file ids to hand to the child.
    :param workspace: Absolute folder the child works in, set before its brief is posted;
        ``None`` keeps the folder it inherits from the caller.
    :param project_name: Card name of the Project ``workspace`` is, if it is one.
    """

    agent: str
    task: str
    title: str | None = None
    model: str = "builder"
    reasoning_effort: str | None = None
    file_ids: tuple[str, ...] = ()
    workspace: str | None = None
    project_name: str | None = None


@dataclass(frozen=True)
class SpawnResult:
    """Outcome of a :class:`SpawnRequest`: the child's id, or why it was refused.

    :param child_id: The started child session (``None`` on refusal).
    :param error: The runner's refusal reason, without a tool-name prefix.
    """

    child_id: str | None = None
    error: str = ""


@dataclass(frozen=True)
class SubAgentHost:
    """What the runner offers a handler that starts sub-agents (built in ``tool_dispatch``).

    :param declared_types: Names of the Sub-agent Types the calling session declares.
    :param helper_roles: ``{type name: "worker" | "subworker"}`` for Types that declare a Helper
        role in their ``params.helper_type``; Types without one keep their literal name.
    :param child_titles: Titles of the caller's existing child sessions.
    :param spawn: Starts one child through the generic sub-agent create path.
    :param send: Passes a message to one of the caller's own children whose turn is running
        (``child id, text``); it reads it at its next step. The child itself refuses it when
        no turn is running by the time it arrives (``{"error": "not_running"}``), so a send
        never starts a second task. Returns the generic ``sys_session_send`` output.
    :param running: ``(child id, title)`` of the caller's children whose turn is running.
    """

    declared_types: tuple[str, ...]
    child_titles: Callable[[], Awaitable[list[str]]]
    spawn: Callable[[SpawnRequest], Awaitable[SpawnResult]]
    helper_roles: Mapping[str, str] = field(default_factory=dict)
    send: Callable[[str, str], Awaitable[str]] | None = None
    running: Callable[[], Awaitable[list[tuple[str, str]]]] | None = None


@dataclass(frozen=True)
class HandlerCtx:
    """What a runner-side feature handler needs about the call.

    :param tool_name: The tool being called.
    :param server_client: HTTP client pointed at the Omnigent server (``None`` outside a runner).
    :param conversation_id: The calling session.
    :param labels: The calling session's labels, when the dispatcher has them.
    :param sub_agents: The runner's sub-agent launcher (``None`` outside a runner).
    :param turn_said: Whether the running turn has streamed any text before this call
        (``None`` when the session has no running turn).
    """

    tool_name: str
    server_client: httpx.AsyncClient | None
    conversation_id: str | None
    labels: Mapping[str, str] | None = None
    sub_agents: SubAgentHost | None = None
    turn_said: bool | None = None


@dataclass(frozen=True)
class InstallDeps:
    """Server objects a feature's ``install`` may wire its store and router with."""

    scheduled_task_store: Any
    conversation_store: Any
    agent_store: Any
    permission_store: Any
    auth_provider: Any
    agent_cache: Any = None
    runner_router: Any = None
    objective_store: Any = None

    @property
    def storage_location(self) -> str | None:
        """The SQLAlchemy URI of the server's database, where a feature keeps its tables."""
        return getattr(self.scheduled_task_store, "storage_location", None)


class BackgroundJob(Protocol):
    """A feature's background worker: started with the app, stopped on shutdown."""

    def start(self) -> Any:
        """Start the job (may be sync or return an awaitable)."""

    def shutdown(self) -> Any:
        """Stop the job (may be sync or return an awaitable)."""


#: Top-level ``/mcp/execute`` body key the engine's relays set. The runner lets a relay op through
#: its granted-tool gate only for a call that carries it; a model's tool call never does (the
#: engine builds that body itself), so an injected model cannot reach a relay op by name.
RELAY_MARK = "_omnigent_relay"

Handler = Callable[[HandlerCtx, dict[str, Any]], Awaitable[str]]
ResultListener = Callable[[HandlerCtx, str], None]
#: ``(server_client, conversation_id) -> blocks`` prepended to a Super Chat turn (never raises).
TurnPrefix = Callable[["httpx.AsyncClient | None", str], Awaitable[list[str]]]
#: One block a feature builds from the turn's own message: text goes in front of the message, a
#: content block (an attached image as ``input_image``) goes with the message itself.
MessageBlock = str | dict[str, Any]
#: ``(server_client, conversation_id, texts of the turn's own message) -> blocks`` for the
#: harness body only, closest to the message (never stored; never raises).
MessagePrefix = Callable[
    ["httpx.AsyncClient | None", str, Sequence[str]], Awaitable[list[MessageBlock]]
]
#: ``(conversation_id) -> None``: a runner turn of that session ended (never raises, never awaits).
TurnEnd = Callable[[str], None]


@dataclass(frozen=True)
class Feature:
    """One Super Chat primitive's registration surface.

    :param name: Folder / feature name, e.g. ``"vault"``.
    :param tools: ``(labels, ctx) -> tool defs`` (label-gated: Super Chat vs Helper).
    :param handlers: ``tool_name -> async handler`` run on the runner.
    :param relay_ops: Handler names only the engine's relays call (through the runner's
        ``/mcp/execute`` with :data:`RELAY_MARK`): never offered to a model, and allowed through
        the runner's granted-tool gate only for a marked relay call in a Super Chat session.
    :param on_result: Sees every feature handler's ``(ctx, output)`` after it ran (any feature's
        tool), so a feature can follow another's results without importing it.
    :param turn_prefix: Blocks this feature prepends to a Super Chat turn (e.g. one-shot notes),
        read by ``prompt_prefix`` in feature order; a failure never fails the turn.
    :param message_prefix: Blocks this feature builds from the turn's own message (the files it
        attaches), prepended for the model only: they sit closest to the message and are never
        part of what is stored as the person's words.
    :param conversation_only: ``message_prefix`` is for the Conversation itself: a Helper's turn
        and a file's studio never call it (ADR 0026: no call whose result is thrown away).
    :param on_turn_end: Told when a runner turn ends (the turn's ``response.completed``).
    :param needs_own_turn: Whether a message's text needs a turn of its own to be read whole,
        e.g. because it attaches a file whose ``message_prefix`` context only a turn start
        adds; such a message is never steered into a running turn.
    :param install: Wires stores + routers onto the FastAPI app.
    :param jobs: Returns background jobs (objects with ``start()`` / ``shutdown()``).
    """

    name: str
    tools: Callable[[Mapping[str, str] | None, ToolManagerCtx], list[Tool]]
    handlers: Mapping[str, Handler] = field(default_factory=dict)
    relay_ops: frozenset[str] = frozenset()
    on_result: ResultListener | None = None
    turn_prefix: TurnPrefix | None = None
    message_prefix: MessagePrefix | None = None
    conversation_only: bool = False
    on_turn_end: TurnEnd | None = None
    needs_own_turn: Callable[[str], bool] | None = None
    install: Callable[[FastAPI, InstallDeps], None] | None = None
    jobs: Callable[[FastAPI], list[BackgroundJob]] | None = None


#: The ``response_id`` prefix of items the engine records in a Conversation outside any turn (a
#: picked-up topic's card, ADR 0035): part of the record, never an Activity of their own.
RECORDED_RESPONSE_PREFIX = "recorded_"


#: Where a Project's folder is, registered by the projects capability so another capability
#: (``start_helper``) can ask core instead of importing it.
_project_resolver: Callable[[str], tuple[Path, str] | None] | None = None


def register_project_resolver(resolve: Callable[[str], tuple[Path, str] | None]) -> None:
    """Set the function that maps a Project slug to ``(folder, name)``."""
    global _project_resolver
    _project_resolver = resolve


def resolve_project(slug: str) -> tuple[Path, str] | None:
    """``(folder, name)`` of Project ``slug``, or ``None`` when there is none (or no resolver)."""
    return _project_resolver(slug) if _project_resolver is not None else None


#: The owner's current topics as brief lines, registered by the topics capability so the
#: quiet-moment pass (``daily_notes``) can list them without importing it (ADR 0035).
_topics_brief: Callable[[str], str] | None = None


def register_topics_brief(render: Callable[[str], str]) -> None:
    """Set the function that renders an owner's current topics for the quiet brief."""
    global _topics_brief
    _topics_brief = render


def topics_brief(owner: str) -> str | None:
    """The owner's current topics as brief lines (``""`` when they have none), or ``None`` when
    no topics capability is registered."""
    return _topics_brief(owner) if _topics_brief is not None else None


#: ``feature name -> hook(conversation_id, messages)``: adds a feature's fields to one page of
#: transcript messages in place (blocking, called off the event loop), so the transcript shows
#: them without importing the feature, e.g. the outbox's ``proactive`` label.
TRANSCRIPT_FIELDS: dict[str, Callable[[str, list[dict[str, Any]]], None]] = {}

#: The person's mutes, registered by the outbox capability (ADR 0036) so the Lookout can read
#: them through core instead of importing it.
_mutes_reader: Callable[[str], list[dict[str, str]]] | None = None


def register_mutes_reader(read: Callable[[str], list[dict[str, str]]]) -> None:
    """Set the function that lists an owner's mutes."""
    global _mutes_reader
    _mutes_reader = read


def list_mutes(owner: str) -> list[dict[str, str]]:
    """The owner's mutes from *Not useful* (``{topic_key, sender?, term?}``, oldest first;
    ``owner`` is the user id, ``"local"`` when auth is off). Blocking; empty without an outbox.
    """
    return _mutes_reader(owner) if _mutes_reader is not None else []


#: The person's proactive messages, registered by the outbox capability (ADR 0036) so the rhythm
#: (ADR 0038) reads them through core: ``(owner, since_ms, until_ms) -> [{item_id, kind,
#: topic_key, created_at_ms}]`` oldest first, and ``(owner, item_id) -> that dict | None``.
_proactive_list: Callable[[str, int, int], list[dict[str, Any]]] | None = None
_proactive_get: Callable[[str, str], dict[str, Any] | None] | None = None


def register_proactive_reader(
    list_between: Callable[[str, int, int], list[dict[str, Any]]],
    get: Callable[[str, str], dict[str, Any] | None],
) -> None:
    """Set the functions that read an owner's proactive messages."""
    global _proactive_list, _proactive_get
    _proactive_list, _proactive_get = list_between, get


def proactive_items(owner: str, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
    """The owner's proactive messages created in ``[since_ms, until_ms)``, oldest first
    (``{item_id, kind, topic_key, created_at_ms}``). Blocking; empty without an outbox."""
    return _proactive_list(owner, since_ms, until_ms) if _proactive_list is not None else []


def proactive_item(owner: str, item_id: str) -> dict[str, Any] | None:
    """The owner's proactive message *item_id*, or ``None`` when it is not one (blocking)."""
    return _proactive_get(owner, item_id) if _proactive_get is not None else None


#: Whether the person has not yet seen a proactive message (it is after their ``web`` cursor),
#: registered by the outbox capability: ``(owner, item_id) -> unseen``.
_proactive_unseen: Callable[[str, str], bool] | None = None


def register_proactive_unseen(unseen: Callable[[str, str], bool]) -> None:
    """Set the function that tells whether the owner has not seen proactive message *item_id*."""
    global _proactive_unseen
    _proactive_unseen = unseen


def proactive_unseen(owner: str, item_id: str) -> bool:
    """Whether *item_id* is one of the owner's proactive messages they have not seen yet
    (blocking). ``False`` without an outbox or for any other item."""
    return _proactive_unseen(owner, item_id) if _proactive_unseen is not None else False


#: ``feature name -> listener(owner, topic_key, at, item_id)``: the person touched a topic in the
#: Conversation (picked it up, or the Conversation updated the topic it works on); *item_id* is
#: the proactive message the pick came from, when it came from one. Called by the topics
#: capability (blocking, off the event loop) so the rhythm hears it without importing topics.
ON_TOPIC_TOUCHED: dict[str, Callable[[str, str, int, str | None], None]] = {}


def topic_touched(owner: str, topic_key: str, at: int, item_id: str | None = None) -> None:
    """Tell every :data:`ON_TOPIC_TOUCHED` listener; a listener's failure is logged only."""
    for name, listener in ON_TOPIC_TOUCHED.items():
        try:
            listener(owner, topic_key, at, item_id)
        except Exception:  # bookkeeping never fails the person's action
            _logger.warning("%s topic touch failed", name, exc_info=True)


#: Writes the outbox row of a proactive message engine code posted itself (ADR 0038, "One run,
#: posted by code"), registered by the outbox capability so the Lookout needs no import of it.
_proactive_recorder: Callable[..., None] | None = None


def register_proactive_recorder(record: Callable[..., None]) -> None:
    """Set the function that writes a code-posted proactive message's outbox row."""
    global _proactive_recorder
    _proactive_recorder = record


def record_proactive(
    *,
    owner: str,
    conversation_id: str,
    item_id: str,
    response_id: str,
    text: str,
    kind: str,
    urgency: str,
    topic_key: str | None = None,
    source: Mapping[str, str] | None = None,
) -> bool:
    """Blocking: write the outbox row of the assistant message *item_id* that engine code posted
    (its label, title and line). Call it before the message is published, so a client that
    refetches on ``message.done`` sees the label. ``False`` without an outbox."""
    if _proactive_recorder is None:
        return False
    _proactive_recorder(
        owner=owner,
        conversation_id=conversation_id,
        item_id=item_id,
        response_id=response_id,
        text=text,
        kind=kind,
        urgency=urgency,
        topic_key=topic_key,
        source=dict(source or {}),
    )
    return True


#: Links an assistant message engine code posted to the file its run saved (ADR 0021's message
#: link, ``made``; ADR 0038), registered by the artifacts capability so the poster needs no import
#: of it: ``(session_id, item_id, artifact_id) -> linked``.
_file_linker: Callable[[str, str, str], bool] | None = None


def register_file_linker(link: Callable[[str, str, str], bool]) -> None:
    """Set the function that links a code-posted message to the file its run made."""
    global _file_linker
    _file_linker = link


def link_made_file(session_id: str, item_id: str, artifact_id: str) -> bool:
    """Blocking: show the file *artifact_id* as a card on the assistant message *item_id* of
    the Conversation *session_id*. Call it before the message is published. ``False`` without
    the artifacts capability or when the file is not that Conversation's."""
    return _file_linker(session_id, item_id, artifact_id) if _file_linker is not None else False
