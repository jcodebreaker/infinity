"""Shared constants, state, and small dataclasses for the sessions routes.

Leaf module: imports no sibling ``_sessions`` module. Holds the mutable
caches/registries/task-sets and immutable config the helpers and router share,
so a single instance is imported by reference across the package.
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from dataclasses import dataclass
from typing import Any

import cachetools
import httpx
from pydantic import TypeAdapter

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.db.workspace_cache import WorkspaceScopedCache, WorkspaceScopedSet
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.server.host_registry import HostRegistry
from omnigent.server.schemas import (
    BackgroundTaskInfo,
    McpServerStartup,
    SandboxStatus,
    ServerStreamEvent,
)
from omnigent.spec.types import (
    StateUpdate,
)

# Pinned to the historical module path so log records keep landing on the
# ``omnigent.server.routes.sessions`` logger after the split into this package.
_logger = logging.getLogger("omnigent.server.routes.sessions")


_INTERRUPT_TYPE: str = "interrupt"


_APPROVAL_TYPE: str = "approval"


_MCP_ELICITATION_TYPE: str = "mcp_elicitation"


_COMPACT_TYPE: str = "compact"


_SLASH_COMMAND_TYPE: str = "slash_command"


_STOP_SESSION_TYPE: str = "stop_session"


_RETRY_SESSION_TYPE: str = "retry_session"


_EXTERNAL_ASSISTANT_MESSAGE_TYPE: str = "external_assistant_message"


_EXTERNAL_CONVERSATION_ITEM_TYPE: str = "external_conversation_item"


_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE: str = "external_output_text_delta"


_EXTERNAL_TOOL_OUTPUT_DELTA_TYPE: str = "external_tool_output_delta"


_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE: str = "external_output_reasoning_delta"


_EXTERNAL_SESSION_INTERRUPTED_TYPE: str = "external_session_interrupted"


_EXTERNAL_SESSION_SUPERSEDED_TYPE: str = "external_session_superseded"
# Transient /btw side-chat overlay: the claude-native forwarder scrapes a
# settled ``/btw`` exchange from the pane and posts it here to be broadcast
# (never persisted) so the web UI shows the ephemeral overlay.
_EXTERNAL_BTW_SIDECHAT_TYPE: str = "external_btw_sidechat"
# Transient /btw overlay dismiss: the web UI posts this when the reader closes
# the side-chat overlay (Escape / ✕), and the server forwards an Escape to the
# pane so the terminal's own ``/btw`` overlay closes in lockstep.
_EXTERNAL_BTW_DISMISS_TYPE: str = "external_btw_dismiss"


_EXTERNAL_ELICITATION_RESOLVED_TYPE: str = "external_elicitation_resolved"


_EXTERNAL_SESSION_STATUS_TYPE: str = "external_session_status"


_SUBAGENT_STATUS_TYPE: str = "subagent.status"


_EXTERNAL_SESSION_STATUS_VALUES: frozenset[str] = frozenset(
    {"idle", "running", "waiting", "failed"}
)


_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT: int = 1000


_EXTERNAL_COMPACTION_STATUS_TYPE: str = "external_compaction_status"


_EXTERNAL_COMPACTION_STATUS_VALUES: frozenset[str] = frozenset(
    {"in_progress", "completed", "failed"}
)


_EXTERNAL_MCP_STARTUP_TYPE: str = "external_mcp_startup"


_EXTERNAL_MCP_STARTUP_STATUS_VALUES: frozenset[str] = frozenset(
    {"starting", "ready", "failed", "cancelled"}
)


_EXTERNAL_SESSION_USAGE_TYPE: str = "external_session_usage"


_EXTERNAL_MODEL_CHANGE_TYPE: str = "external_model_change"


_EXTERNAL_SESSION_TITLE_TYPE: str = "external_session_title"


_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE: str = "external_reasoning_effort_change"


_LAST_CONTEXT_TOKENS_LABEL_KEY: str = "omnigent.last_context_tokens"


_LAST_CONTEXT_WINDOW_LABEL_KEY: str = "omnigent.last_context_window"


_LAST_TASK_ERROR_CODE_LABEL_KEY: str = "omnigent.last_task_error_code"


_LAST_TASK_ERROR_MESSAGE_LABEL_KEY: str = "omnigent.last_task_error_message"


_LAST_TASK_ERROR_AGENT_NAME_LABEL_KEY: str = "omnigent.last_task_error_agent_name"


# Optional structured failure fields (present when the runner classified the
# failure — see ``omnigent.runner.launch_failure``), persisted so a reload
# renders the same clear failure card instead of only the raw code + message.
_LAST_TASK_ERROR_TITLE_LABEL_KEY: str = "omnigent.last_task_error_title"


_LAST_TASK_ERROR_CAUSE_LABEL_KEY: str = "omnigent.last_task_error_cause"


_LAST_TASK_ERROR_REMEDIATION_LABEL_KEY: str = "omnigent.last_task_error_remediation"


_LABEL_VALUE_MAX_LEN: int = LABEL_VALUE_MAX_LEN


_CLAUDE_NATIVE_WRAPPER_LABEL_KEY = "omnigent.wrapper"


_CLAUDE_NATIVE_UI_LABEL_KEY = "omnigent.ui"


_CLAUDE_NATIVE_UI_LABEL_VALUE = "terminal"


_HOST_BOUND_RUNNER_CONNECT_GRACE_S = 10.0


_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S = 30.0


# How long a message waits for a host whose row still reads online to redial this
# engine (a few of the host's reconnect backoffs, capped at 3 s) before the host is
# treated as gone and relaunched.
_HOST_RECONNECT_GRACE_S = 15.0


_HOST_RUNNER_STATUS_TIMEOUT_S = 3.0


_MANAGED_RESUMABLE_TUNNEL_STALE_S = 30.0


_RUNNER_CONVICTION_POLL_S = 0.25


_HOST_LAUNCH_RESULT_TIMEOUT_S = 10.0


# custom-lint: disable-next=workspace-scoped-cache -- server-minted action_id
_browser_action_registry: dict[str, asyncio.Future[dict[str, Any]]] = {}  # -> parked Future


# custom-lint: disable-next=workspace-scoped-cache -- server-minted action_id
_browser_action_owners: dict[str, str] = {}  # -> issuing session_id (result POST must match)


# custom-lint: disable-next=workspace-scoped-cache -- server-minted action_id
_browser_action_claims: dict[str, str] = {}


# custom-lint: disable-next=workspace-scoped-cache -- server-minted action_id
_browser_action_claim_events: dict[str, asyncio.Event] = {}


# The Electron relay claims before doing browser work, so this only budgets
# event delivery plus the claim round-trip. Keep it short enough that a generic
# stream subscriber cannot recreate the old 30-second no-renderer stall.
_BROWSER_ACTION_CLAIM_GRACE_S = 2.0


_BROWSER_ACTION_AWAIT_S = 30.0


_BROWSER_ACTION_NO_RENDERER_RESULT: dict[str, Any] = {"error": "no browser renderer is connected"}


_BROWSER_ACTION_TIMEOUT_RESULT: dict[str, Any] = {
    "error": "browser action timed out — is the session open in the Omnigent desktop app?"
}


_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S = 300.0


_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES = 1024


# How long a severed elicitation's card survives before it is cleared, giving a
# hook retry time to re-park the same id. Wide enough to absorb a slow re-POST
# (a lapsed token costs a re-mint round trip) so a still-blocked prompt is not
# flipped to "Resolved elsewhere" between polls; a hook that died for real just
# leaves the card up this much longer.
_HARNESS_ELICITATION_REPARK_GRACE_S = 30.0


# How long an archive defers tearing down the session's runner, giving an Undo's
# unarchive time to land first. When the teardown fires it re-reads the persisted
# archived flag and skips if the session was unarchived, so any Undo whose
# unarchive PERSISTS before this fires keeps the runner — across replicas, since
# the guard is the shared row, not an in-memory timer.
#
# MUST stay above the client Undo pill's total lifetime, which the pill caps at
# ARCHIVE_UNDO_MAX_LIFETIME_MS (5s) in web/src/shell/archiveUndoToast.tsx. The
# pill merges successive archives, so without that cap it could linger past this
# grace and offer an Undo AFTER the teardown already ran — and the re-check
# can't un-stop a runner. The cap keeps the pill's Undo window inside this grace.
_ARCHIVE_STOP_UNDO_GRACE_S = 8.0


_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_[a-z]+_[0-9a-f]{32}$")


_EVALUATE_HOOK_ELICITATION_ID_RE = re.compile(r"^elicit_evaluate_[0-9a-f]{32}$")


_RACE_TASK_REAP_TIMEOUT_S = 5.0


_SESSION_STREAM_HEARTBEAT_INTERVAL_S = 15.0


_SNAPSHOT_RUNNER_TIMEOUT_S = 2.0


_RUNNER_RELAY_READY_TIMEOUT_S = 5.0


_RUNNER_FORWARD_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=10.0)


_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(ITEM_TYPE_TO_DATA_CLS.keys()) | {
    _INTERRUPT_TYPE,
    _APPROVAL_TYPE,
    _MCP_ELICITATION_TYPE,
    _COMPACT_TYPE,
    _STOP_SESSION_TYPE,
    _RETRY_SESSION_TYPE,
    _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
    _EXTERNAL_CONVERSATION_ITEM_TYPE,
    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
    _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
    _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
    _EXTERNAL_SESSION_INTERRUPTED_TYPE,
    _EXTERNAL_SESSION_SUPERSEDED_TYPE,
    _EXTERNAL_BTW_SIDECHAT_TYPE,
    _EXTERNAL_BTW_DISMISS_TYPE,
    _EXTERNAL_ELICITATION_RESOLVED_TYPE,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _SUBAGENT_STATUS_TYPE,
    _EXTERNAL_SESSION_USAGE_TYPE,
    _EXTERNAL_COMPACTION_STATUS_TYPE,
    _EXTERNAL_MCP_STARTUP_TYPE,
    _EXTERNAL_MODEL_CHANGE_TYPE,
    _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
    _EXTERNAL_SESSION_TITLE_TYPE,
}


_SERVER_STREAM_EVENT_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)


# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_WATCHER_TASKS: set[asyncio.Task[None]] = set()


_session_status_cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


@dataclass
class _RunnerStatusProbeBackoff:
    """
    Skip window for a session's runner status probe after slow probes.

    :param skip_until: Monotonic time before which the probe is skipped.
    :param failures: Consecutive slow or failed probes; sets the next window.
    :param runner_id: Runner the slow probes were against, e.g.
        ``"runner_0123456789abcdef"``; a rebind to another runner discards
        the window.
    """

    skip_until: float
    failures: int
    runner_id: str | None


_runner_status_probe_backoff: WorkspaceScopedCache[str, _RunnerStatusProbeBackoff] = (
    WorkspaceScopedCache()
)

# The one runner status probe in flight per session; concurrent snapshots await it.
_runner_status_probe_inflight: WorkspaceScopedCache[str, asyncio.Task[str | None]] = (
    WorkspaceScopedCache()
)


_session_active_response_cache: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


_session_background_task_count_cache: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()


# Per-shell detail behind the tally above, kept sticky in lockstep with it (see
# ``_publish_status``) so a reload/reconnect can restore it. Absent when the
# count cache is absent, or when a runner reported only the count with no detail.
_session_background_tasks_cache: WorkspaceScopedCache[str, list[BackgroundTaskInfo]] = (
    WorkspaceScopedCache()
)


_read_last_seen: WorkspaceScopedCache[str, dict[str, int]] = WorkspaceScopedCache()


_read_explicit_unread: WorkspaceScopedCache[str, set[str]] = WorkspaceScopedCache()


_interrupt_fenced_sessions: WorkspaceScopedSet[str] = WorkspaceScopedSet()


_intentional_stop_sessions: WorkspaceScopedSet[str] = WorkspaceScopedSet()


_TERMINAL_RESPONSE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.completed",
        "response.failed",
        "response.cancelled",
        "response.incomplete",
    }
)


_FENCE_EXEMPT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "response.elicitation_request",
        "response.elicitation_resolved",
    }
)


_SESSION_UPDATES_RESCAN_INTERVAL_S: float = 4.0


_SESSION_UPDATES_HEARTBEAT_INTERVAL_S: float = 30.0


_SESSION_UPDATES_MAX_WATCHED: int = 500


_SHARED_DISCOVERY_KEY = "__all__"


_session_terminal_pending_cache: WorkspaceScopedCache[str, bool] = WorkspaceScopedCache()


_session_sandbox_status_cache: WorkspaceScopedCache[str, SandboxStatus] = WorkspaceScopedCache()


_session_mcp_startup_cache: WorkspaceScopedCache[str, dict[str, McpServerStartup]] = (
    WorkspaceScopedCache()
)


# Strong references to fire-and-forget catalog prefetches, so a task cannot be
# garbage-collected mid-flight. Entries remove themselves when they finish.
# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_catalog_prefetch_tasks: set[asyncio.Task[None]] = set()


@dataclass
class _MirroredToolCall:
    """
    Tool identity of a forwarder-mirrored ``function_call``.

    Cached by ``call_id`` so a later ``function_call_output`` (which
    carries only ``call_id`` + ``output``) can recover the tool it
    belongs to and correlate it to a parked permission prompt. See
    :data:`_recent_mirrored_tool_calls`.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :param tool_input: Parsed tool arguments, e.g.
        ``{"command": "ls"}``; ``{}`` when the arguments were absent or
        not a JSON object.
    """

    tool_name: str
    tool_input: dict[str, Any]


# Workspace-scoped: a native call_id can be derived from the conversation id,
# which collides across workspaces for an imported session.
_recent_mirrored_tool_calls: WorkspaceScopedCache[str, _MirroredToolCall] = WorkspaceScopedCache(
    lambda: cachetools.LRUCache(maxsize=2048)
)


@dataclass(frozen=True)
class _PendingPolicyAskWrites:
    """Policy writes deferred until a relay-path tool-call ASK is approved.

    The relay / non-native tool-call gate (:func:`_evaluate_tool_call_policy`)
    parks an ASK as a runner-owned elicitation and returns ``pending`` — it
    cannot apply the deciding policy's ``state_updates`` / ``set_labels``
    inline because the approval happens later, off that request. They are
    stashed here keyed by elicitation id and applied when the matching
    ``approval`` event resolves with ``accept`` (POLICIES.md §7.2: a denied
    ASK leaves no trace). Without this, e.g. a cost-budget soft checkpoint is
    never recorded server-side, so it re-prompts on every subsequent tool
    call. The native-harness path (:func:`_hold_native_ask_gate`) parks
    server-side and applies these inline, so it does not need this.

    :param state_updates: Deferred :class:`StateUpdate` ops to apply on
        approve, or ``None``.
    :param set_labels: Deferred label writes to apply on approve, or ``None``.
    :param from_mcp: ``True`` when created by the ``/mcp`` endpoint's
        first-call ASK path. The MCP retry path applies writes
        itself, so the events handler skips write application for
        these entries to avoid double-applying non-idempotent ops
        (e.g. ``INCREMENT`` state updates for cost-budget counters).
    :param reviewed_arguments: Original MCP arguments shown for approval.
    :param transformed_arguments: Policy transform stored with that approval.
    """

    state_updates: list[StateUpdate] | None
    set_labels: dict[str, str] | None
    from_mcp: bool = False
    reviewed_arguments: dict[str, Any] | None = None
    transformed_arguments: dict[str, Any] | None = None


# Workspace-scoped: keyed by a harness elicitation id, which can be
# deterministic and collide across workspaces for an imported session.
_pending_policy_ask_writes: WorkspaceScopedCache[str, _PendingPolicyAskWrites] = (
    WorkspaceScopedCache(lambda: cachetools.LRUCache(maxsize=512))
)


_TURN_ACTOR_LABEL = "omnigent.turn_actor"


# Sessions whose in-flight turn's assistant output a PHASE_LLM_RESPONSE
# policy denied, mapped to the deny reason. Set by the policy-evaluate
# route when it returns the DENY (the harness only errors the turn AFTER
# the denied text already streamed and filled the relay's persistence
# buffer), consumed by the relay's terminal text flush so the buffered
# text persists as the deny sentinel instead of the denied content.
# A plain dict, NOT an evicting cache: this is an enforcement decision,
# and a silent eviction would downgrade a DENY into normal persistence.
# Leak-safety comes from lifetime, not bounding — writes are gated on an
# active relay for the session (routes_hooks), and the entry is popped at
# every consume point, on each new turn, and when the relay task ends
# (the relay's done-callback), so an entry can never outlive its relay.
_llm_response_denied_turns: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


# custom-lint: disable-next=workspace-scoped-cache -- lock; collision only serializes
_native_ask_gate_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)

# Serializes native transcript mirrors per conversation so a retried mirror sees
# the first attempt's commit before it touches the pending-input queue.
# custom-lint: disable-next=workspace-scoped-cache -- lock; collision only serializes
_native_mirror_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


# custom-lint: disable-next=workspace-scoped-cache -- lock; collision only serializes
_policy_evaluation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


@dataclass
class _RelayHandle:
    """
    Active SSE relay task plus the runner it streams from.

    :param runner_id: Runner id the task is bound to, e.g.
        ``"runner_abc123"``. Used to detect rebinds to a
        different runner so the stale task can be replaced.
    :param task: The relay coroutine task.
    :param ready: Event set after the relay observes the runner
        stream's ready heartbeat, proving the runner-side
        no-replay subscription is registered.
    """

    runner_id: str
    task: asyncio.Task[None]
    ready: asyncio.Event


_runner_relay_tasks: WorkspaceScopedCache[str, _RelayHandle] = WorkspaceScopedCache()


# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_deferred_elicitation_clear_tasks: set[asyncio.Task[None]] = set()


_MODEL_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_native_popup_forward_tasks: set[asyncio.Task[None]] = set()


_SUBAGENT_FORWARD_RECONNECT_WAIT_S = 5.0


# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_managed_launch_tasks: set[asyncio.Task[None]] = set()


_RUNNER_SESSION_INIT_TIMEOUT_S = 10.0


_STOP_RUNNER_RESULT_TIMEOUT_S = 10.0


_DENY_SENTINEL_PREFIX = "[Denied by policy: "


_MAX_TERMINAL_LAUNCH_ARGS = 256


_MAX_TERMINAL_LAUNCH_ARG_LEN = 4096


COST_CONTROL_OVERRIDE_VALUES = frozenset({"on", "off"})


# Per-session subagent-routing switch. Two-state: only ``"on"`` routes
# spawns, and ``"off"`` / absent both read as Default. Creates that start
# on Smart Routing are stamped ``"on"``, so absent is never an inherit.
SUBAGENT_ROUTING_OVERRIDE_VALUES = frozenset({"on", "off"})


_CHILD_PREVIEW_LIMIT = 150


_UI_ADDED_AGENT_TITLE_PREFIX = "ui"


_UPLOAD_READ_CHUNK_BYTES: int = 1024 * 1024


# Runner router for the native-terminal approval popup, set once at app startup
# (see :func:`set_server_runner_router`). The tool-policy ASK gate forwards a
# ``cost_approval_popup`` control event to the bound runner from a parked-gate
# background task that carries no FastAPI request / route closure, so it reads
# the router from this module-level global via :func:`get_server_runner_router`.
_server_runner_router: RunnerRouter | None = None


def set_server_runner_router(runner_router: RunnerRouter | None) -> None:
    """Stash the runner router for the native-terminal approval popup.

    Called once from ``create_app`` so the tool-policy ASK gate
    (``_spawn_native_approval_popup_forward``) can reach the bound runner from
    background contexts that do not carry the request / route closure.

    :param runner_router: The session runner router, or ``None`` in in-process
        setups.
    :returns: None.
    """
    global _server_runner_router
    _server_runner_router = runner_router


def get_server_runner_router() -> RunnerRouter | None:
    """Return the runner router stashed by :func:`set_server_runner_router`."""
    return _server_runner_router


# Live host-tunnel registry, set once at app startup (see
# :func:`set_server_host_registry`), for code paths that carry no FastAPI
# request (e.g. the routing backend).
_server_host_registry: HostRegistry | None = None


def set_server_host_registry(host_registry: HostRegistry | None) -> None:
    """Stash the live host registry for request-less code paths.

    Called once from ``create_app`` so background contexts that do not carry
    the request / route closure can reach a session's host connection.

    :param host_registry: The live host-tunnel registry, or ``None`` in
        setups without host tunnels.
    :returns: None.
    """
    global _server_host_registry
    _server_host_registry = host_registry


def get_server_host_registry() -> HostRegistry | None:
    """Return the registry stashed by :func:`set_server_host_registry`."""
    return _server_host_registry


__all__ = [
    "COST_CONTROL_OVERRIDE_VALUES",
    "SUBAGENT_ROUTING_OVERRIDE_VALUES",
    "_ALLOWED_EVENT_TYPES",
    "_APPROVAL_TYPE",
    "_BROWSER_ACTION_AWAIT_S",
    "_BROWSER_ACTION_CLAIM_GRACE_S",
    "_BROWSER_ACTION_NO_RENDERER_RESULT",
    "_BROWSER_ACTION_TIMEOUT_RESULT",
    "_CHILD_PREVIEW_LIMIT",
    "_CLAUDE_NATIVE_UI_LABEL_KEY",
    "_CLAUDE_NATIVE_UI_LABEL_VALUE",
    "_CLAUDE_NATIVE_WRAPPER_LABEL_KEY",
    "_COMPACT_TYPE",
    "_DENY_SENTINEL_PREFIX",
    "_EVALUATE_HOOK_ELICITATION_ID_RE",
    "_EXTERNAL_ASSISTANT_MESSAGE_TYPE",
    "_EXTERNAL_BTW_DISMISS_TYPE",
    "_EXTERNAL_BTW_SIDECHAT_TYPE",
    "_EXTERNAL_COMPACTION_STATUS_TYPE",
    "_EXTERNAL_COMPACTION_STATUS_VALUES",
    "_EXTERNAL_CONVERSATION_ITEM_TYPE",
    "_EXTERNAL_ELICITATION_RESOLVED_TYPE",
    "_EXTERNAL_MCP_STARTUP_STATUS_VALUES",
    "_EXTERNAL_MCP_STARTUP_TYPE",
    "_EXTERNAL_MODEL_CHANGE_TYPE",
    "_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE",
    "_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE",
    "_EXTERNAL_REASONING_EFFORT_CHANGE_TYPE",
    "_EXTERNAL_SESSION_INTERRUPTED_TYPE",
    "_EXTERNAL_SESSION_STATUS_TYPE",
    "_EXTERNAL_SESSION_STATUS_VALUES",
    "_EXTERNAL_SESSION_SUPERSEDED_TYPE",
    "_EXTERNAL_SESSION_TITLE_TYPE",
    "_EXTERNAL_SESSION_USAGE_TYPE",
    "_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT",
    "_EXTERNAL_TOOL_OUTPUT_DELTA_TYPE",
    "_FENCE_EXEMPT_EVENT_TYPES",
    "_HARNESS_ELICITATION_REPARK_GRACE_S",
    "_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES",
    "_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S",
    "_HOOK_ELICITATION_ID_RE",
    "_HOST_BOUND_RUNNER_CONNECT_GRACE_S",
    "_HOST_LAUNCH_RESULT_TIMEOUT_S",
    "_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S",
    "_HOST_RUNNER_STATUS_TIMEOUT_S",
    "_INTERRUPT_TYPE",
    "_LABEL_VALUE_MAX_LEN",
    "_LAST_CONTEXT_TOKENS_LABEL_KEY",
    "_LAST_CONTEXT_WINDOW_LABEL_KEY",
    "_LAST_TASK_ERROR_CAUSE_LABEL_KEY",
    "_LAST_TASK_ERROR_CODE_LABEL_KEY",
    "_LAST_TASK_ERROR_MESSAGE_LABEL_KEY",
    "_LAST_TASK_ERROR_REMEDIATION_LABEL_KEY",
    "_LAST_TASK_ERROR_TITLE_LABEL_KEY",
    "_MANAGED_RESUMABLE_TUNNEL_STALE_S",
    "_MAX_TERMINAL_LAUNCH_ARGS",
    "_MAX_TERMINAL_LAUNCH_ARG_LEN",
    "_MCP_ELICITATION_TYPE",
    "_MODEL_TOKEN_KEYS",
    "_RACE_TASK_REAP_TIMEOUT_S",
    "_RETRY_SESSION_TYPE",
    "_RUNNER_CONVICTION_POLL_S",
    "_RUNNER_FORWARD_TIMEOUT",
    "_RUNNER_RELAY_READY_TIMEOUT_S",
    "_RUNNER_SESSION_INIT_TIMEOUT_S",
    "_SERVER_STREAM_EVENT_ADAPTER",
    "_SESSION_STREAM_HEARTBEAT_INTERVAL_S",
    "_SESSION_UPDATES_HEARTBEAT_INTERVAL_S",
    "_SESSION_UPDATES_MAX_WATCHED",
    "_SESSION_UPDATES_RESCAN_INTERVAL_S",
    "_SHARED_DISCOVERY_KEY",
    "_SLASH_COMMAND_TYPE",
    "_SNAPSHOT_RUNNER_TIMEOUT_S",
    "_STOP_RUNNER_RESULT_TIMEOUT_S",
    "_STOP_SESSION_TYPE",
    "_SUBAGENT_FORWARD_RECONNECT_WAIT_S",
    "_SUBAGENT_STATUS_TYPE",
    "_TERMINAL_RESPONSE_EVENT_TYPES",
    "_TURN_ACTOR_LABEL",
    "_UI_ADDED_AGENT_TITLE_PREFIX",
    "_UPLOAD_READ_CHUNK_BYTES",
    "_WATCHER_TASKS",
    "_MirroredToolCall",
    "_PendingPolicyAskWrites",
    "_RelayHandle",
    "_RunnerStatusProbeBackoff",
    "_browser_action_claim_events",
    "_browser_action_claims",
    "_browser_action_owners",
    "_browser_action_registry",
    "_catalog_prefetch_tasks",
    "_deferred_elicitation_clear_tasks",
    "_intentional_stop_sessions",
    "_interrupt_fenced_sessions",
    "_llm_response_denied_turns",
    "_logger",
    "_managed_launch_tasks",
    "_native_ask_gate_locks",
    "_native_popup_forward_tasks",
    "_pending_policy_ask_writes",
    "_read_explicit_unread",
    "_read_last_seen",
    "_recent_mirrored_tool_calls",
    "_runner_relay_tasks",
    "_runner_status_probe_backoff",
    "_runner_status_probe_inflight",
    "_server_host_registry",
    "_server_runner_router",
    "_session_active_response_cache",
    "_session_background_task_count_cache",
    "_session_background_tasks_cache",
    "_session_mcp_startup_cache",
    "_session_sandbox_status_cache",
    "_session_status_cache",
    "_session_terminal_pending_cache",
    "get_server_host_registry",
    "get_server_runner_router",
    "set_server_host_registry",
    "set_server_runner_router",
]


# --- Facade-delegating proxies for runtime bindings patched on the router ---
#
# Tests (and callers) patch these on the historical ``sessions`` facade module,
# e.g. ``monkeypatch.setattr("...routes.sessions.get_agent_cache", fake)``. The
# sibling ``_sessions`` modules resolve the names in their OWN namespace, so a
# facade-level patch would miss them. These proxies resolve the facade attribute
# lazily on every access, so a patch on the facade is honoured everywhere the
# siblings import the name from here. Deliberately NOT in ``__all__`` or the
# facade's explicit imports, preserving its real runtime bindings.


def _sessions_facade() -> Any:
    """Return the historical ``sessions`` router module (resolved lazily)."""
    import omnigent.server.routes.sessions as facade

    return facade


class _FacadeCallable:
    """Callable that forwards to the same-named attribute on the facade."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return getattr(_sessions_facade(), self._name)(*args, **kwargs)


class _FacadeAttrProxy:
    """Attribute-access proxy forwarding to the facade's same-named object."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(getattr(_sessions_facade(), self._name), attr)


class _FacadeValue:
    """Comparison proxy for a plain value read off the facade lazily."""

    def __init__(self, name: str) -> None:
        self._name = name

    def _value(self) -> Any:
        return getattr(_sessions_facade(), self._name)

    def __eq__(self, other: object) -> bool:
        return bool(self._value() == other)

    def __ne__(self, other: object) -> bool:
        return bool(self._value() != other)

    def __hash__(self) -> int:
        return hash(self._value())

    def __str__(self) -> str:
        return str(self._value())


get_agent_cache = _FacadeCallable("get_agent_cache")
build_policy_engine = _FacadeCallable("build_policy_engine")
get_caps = _FacadeCallable("get_caps")
session_stream = _FacadeAttrProxy("session_stream")
user_session_stream = _FacadeAttrProxy("user_session_stream")
_ELICITATION_MODE = _FacadeValue("_ELICITATION_MODE")
