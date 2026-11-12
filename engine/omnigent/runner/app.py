"""Runner FastAPI app — spawns harness subprocesses and dispatches to them.

Per ``designs/RUNNER.md`` §1, the runner owns harness subprocesses.
It resolves the harness type + spawn-env from the agent spec (either
via a spec_resolver callback for in-process use, or via
GET /v1/agents/{id}/contents for out-of-process use).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import functools
import hashlib
import itertools
import json
import logging
import math
import mimetypes
import os
import re
import time
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeAlias, cast, overload

if TYPE_CHECKING:
    from omnigent.llms.client import Client as LLMClient
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.runner.policy import PolicyVerdict
    from omnigent.terminals.registry import TerminalRegistry

import httpcore
import httpx
import psutil
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

# The product composition root: importing it fills the tool manager and policy hooks (ADR 0019).
import omnigent.superchat.features  # noqa: F401
from omnigent.context.labels import (
    ADHOC_HELPER_LABEL_KEY,
    ARTIFACT_CHAT_LABEL_KEY,
    BACKGROUND_LABELS,
    CONTEXT_MODE_LABEL,
    PAUSED_LABEL_KEY,
    ROLLOVER_KEEP_TOKENS_LABEL,
    SCHEDULED_HELPER_LABEL_KEY,
    SUBAGENT_DISPATCH_ID_LABEL_KEY,
    SUBAGENT_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
    is_artifact_chat,
    is_superside_chat,
)
from omnigent.debug_logging import (
    debug_event,
    phase_scope,
    runner_primary_session_id,
    set_current_session_id,
)
from omnigent.entities.session_resources import (
    DEFAULT_ENVIRONMENT_ID,
    resolve_terminal_entry_by_resource_id,
    session_resource_view_to_dict,
    terminal_resource_id,
)
from omnigent.errors import ErrorCode, ErrorPhase, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_capabilities import InstructionDelivery
from omnigent.harness_plugins import (
    harness_capabilities,
    load_object,
    model_env_keys,
    spawn_env_builders,
)
from omnigent.harnesses.native_attachments import (
    framework_notice_block,
    has_unresolved_file_id,
    resolve_file_id_block,
)
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.policies.types import FAIL_CLOSED_PHASES
from omnigent.process_logging import process_log_reference
from omnigent.runner import pending_approvals
from omnigent.runner.mcp_execution_registry import (
    PAUSED_NOTE,
    STOPPED_RESULT,
    McpExecutionConflict,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager
from omnigent.runner.resource_registry import (
    SessionResourceRegistry,
    TerminalExitEvent,
    trim_terminal_output,
)
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    parse_runner_session_init_envelope,
)
from omnigent.runner.spec_entries import (
    ResolvedSpec,
    _is_spec_local_native_python_tool,
    _resolve_sub_agent_spec_entry,
    _resolved_spec_workdir,
    _resolved_workdir_for_spec,
    _rewrap_like,
    _unwrap_resolved_spec,
)
from omnigent.runner.subagent_routing import (
    PLAIN_SESSION,
    SessionRoutingClass,
    forget_session_routing_class,
    remember_session_routing_class,
    routing_class_from_snapshot,
    session_routing_class,
)
from omnigent.runner.tool_starts import ToolStarts
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.runtime.prompt import (
    build_instructions,
    build_instructions_nullable,
    raw_author_instructions,
)
from omnigent.spec.skill_sources import resolve_session_skills
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.superchat.artifacts.studio import STUDIO_TYPE
from omnigent.superchat.feature import RECORDED_RESPONSE_PREFIX, is_helper, is_super_chat
from omnigent.superchat.features import notify_turn_end
from omnigent.superchat.helpers.notices import take_waiting_progress
from omnigent.superchat.helpers.saved_files import (
    collect_saved_files,
    forget_saved_files,
    format_saved_files,
)
from omnigent.superchat.prompt_prefix import (
    message_needs_own_turn,
    setup_changed_line,
    setup_line,
    turn_prefix_blocks,
)

#: How a harness is named to the model when it says which setup it runs on.
_HARNESS_LABELS = {"pi": "pi", "claude-sdk": "Claude Agent SDK"}
#: A turn's end events; each may carry the turn's usage (``response.usage.context_tokens``).
_TURN_END_EVENTS = frozenset(
    {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}
)
from omnigent.superchat.subagents import (
    STUDIO_QUEUED_NOTE,
    STUDIO_WARM_SECONDS,
    StudioRunners,
    format_subagent_wake_notice_with_result,
    result_notice_reminder,
)
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.ws_common import WS_CLOSE_TERMINAL_NOT_FOUND
from omnigent.tools.browser_backend import local_browser_backend_enabled
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

# Allow process termination and forwarder cleanup to finish before DELETE proceeds.
_SESSION_INIT_CANCEL_TIMEOUT_S = 20.0

_SESSION_METADATA_PARAMS: dict[str, str] = {
    "include_items": "false",
    "include_liveness": "false",
    "include_usage": "false",
}


_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS = 1.0


def turn_timing(
    started: float, marks: Mapping[str, float], first_token: float
) -> tuple[float, float, float, float]:
    """A Super Chat turn's wait in four chunks: setup, prefix, client, first token (seconds).

    *started* is the turn's start and *first_token* its first streamed token (monotonic);
    *marks* holds ``prefix`` (setup ends), ``prefixed`` (the prefix is built) and ``client``
    (the harness holds a Claude CLI and asks to make the model call). A missing mark takes the
    previous one's time, so its chunk reads zero and the four always sum to the whole wait.
    """
    prefix = marks.get("prefix", started)
    prefixed = max(marks.get("prefixed", prefix), prefix)
    client = min(max(marks.get("client", prefixed), prefixed), first_token)
    return prefix - started, prefixed - prefix, client - prefixed, first_token - client


async def _session_labels_for_runner_spawn(
    *,
    server_client: httpx.AsyncClient,
    session_id: str,
) -> dict[str, str] | None:
    """
    Fetch session labels for harness spawn-env construction.

    :param server_client: Omnigent server client used to fetch the session
        labels endpoint.
    :param session_id: Omnigent session/conversation id, e.g.
        ``"conv_abc123"``.
    :returns: String label mapping, or ``None`` on lookup failure.
        ``None`` is distinct from a confirmed-empty ``{}`` (a session that
        really carries no labels) — callers that cache this result must
        never cache ``None``, or a transient failure permanently poisons
        the cache as "no labels" (e.g. reading a side chat / superside-chat
        session as a plain one for the runner's lifetime).
    """
    path = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/labels"
    try:
        resp = await server_client.get(
            path,
            timeout=_SESSION_LABEL_LOOKUP_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        _logger.debug(
            "Timed out resolving session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
            extra={"session_id": session_id},
        )
        return None
    except httpx.HTTPError as exc:
        _logger.warning(
            "Failed to resolve session labels; session=%s error=%s",
            session_id,
            type(exc).__name__,
            extra={"session_id": session_id},
        )
        return None
    if resp.status_code != 200:
        _logger.warning(
            "Failed to resolve session labels; session=%s status=%s",
            session_id,
            resp.status_code,
            extra={"session_id": session_id},
        )
        return None
    try:
        labels = resp.json().get("labels")
    except ValueError:
        # A 200 with a non-JSON body (e.g. an empty response from the
        # Databricks Apps proxy when the server event loop is starved,
        # or an HTML login page on an auth edge) must not abort the
        # turn. Labels are a best-effort spawn hint; recover by using
        # the session id, exactly as the timeout / non-200 paths do.
        _logger.warning(
            "Session labels response was not valid JSON; session=%s status=%s",
            session_id,
            resp.status_code,
            extra={"session_id": session_id},
        )
        return None
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def _forward_harness_response(resp: httpx.Response) -> Response:
    """Relay a non-streaming harness response through FastAPI."""
    if resp.status_code in _NO_BODY_STATUS_CODES:
        return Response(status_code=resp.status_code)
    content_type = resp.headers.get("content-type", "")
    if not resp.content:
        return Response(content=b"", status_code=resp.status_code, media_type=content_type or None)
    if "application/json" in content_type.lower():
        try:
            return JSONResponse(status_code=resp.status_code, content=resp.json())
        except ValueError:
            pass
    return Response(
        content=resp.content, status_code=resp.status_code, media_type=content_type or None
    )


def _warn_unresolved_sub_agent(session_id: str | None, sub_agent_name: str) -> None:
    """
    Log that a sub-agent name did not resolve to a declared child spec.

    Every spec-swap site is guarded by ``if sub_spec is not None`` with no
    ``else`` and falls back to the already-resolved PARENT spec — so a
    renamed/removed sub-agent or stale session metadata silently boots the
    child as a parent clone (parent prompt, tools, harness, workdir). The
    create route now rejects an undeclared name up front, but stale rows
    and post-create bundle edits can still reach these sites; a loud log
    makes the fallback diagnosable instead of invisible.

    :param session_id: The session whose turn is resolving the spec.
    :param sub_agent_name: The name that failed to resolve in the parent
        spec tree.
    """
    _logger.warning(
        "Sub-agent %r for session %s did not resolve in the parent spec; "
        "falling back to the parent spec (child runs with the parent's "
        "prompt, tools and harness). Likely a renamed/removed sub-agent or "
        "stale session metadata.",
        sub_agent_name,
        session_id,
    )


# Servers before 0.3.0 cannot serialize the runner's "waiting" status.
# Unknown versions also downgrade to "running" so old servers never return 500.
_WAITING_STATUS_MIN_SERVER_VERSION = "0.3.0"
# Cached server version from the /api/version probe; ``None`` until a probe
# succeeds. A failed probe stays ``None`` and is retried on the next
# session-create — the GET is cheap and self-heals a transient failure.
_server_version: str | None = None


def _version_supports_waiting_status(server_version: str) -> bool:
    """
    Whether *server_version* can serialize ``session.status: "waiting"``.

    :param server_version: The server's reported version, e.g. ``"0.2.0"`` or
        ``"0.3.0.dev0"``.
    :returns: ``True`` iff the server's PEP 440 release tuple is ``>= 0.3.0``
        (the release that added "waiting" to the session-status model).
    """
    from packaging.version import InvalidVersion, Version

    try:
        return (
            Version(server_version).release >= Version(_WAITING_STATUS_MIN_SERVER_VERSION).release
        )
    except InvalidVersion:
        _logger.warning(
            "server version %r is not PEP 440; treating waiting status support as unknown",
            server_version,
            extra={"session_id": runner_primary_session_id()},
        )
        return False


async def _get_server_version(server_client: httpx.AsyncClient) -> str | None:
    """
    Resolve the server's version via a one-time ``GET /api/version`` probe.

    Memoized once it succeeds: later calls return the cached version. A failed
    probe returns ``None`` and is retried on the next call, so callers fail safe
    (treat an unknown version as not supporting newer behavior).

    :param server_client: The runner's httpx client pointed at the server.
    :returns: The server's reported version (e.g. ``"0.2.0"``), or ``None`` when
        the probe has not yet succeeded.
    """
    global _server_version
    if _server_version is not None:
        return _server_version
    try:
        resp = await server_client.get("/api/version")
        resp.raise_for_status()
        _server_version = resp.json()["version"]
        _logger.info(
            "resolved server version: %s",
            _server_version,
            extra={"session_id": runner_primary_session_id()},
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully; never 500 an old server
        _logger.warning(
            "could not probe server /api/version (%s); treating as unknown",
            exc,
            extra={"session_id": runner_primary_session_id()},
        )
    return _server_version


def _client_safe_error_detail(exc: BaseException, *, context: str) -> str:
    """
    Log *exc* in full and return a generic detail string safe for clients.

    Raw exception text (``str(exc)``) can embed absolute paths, internal
    hostnames, PIDs, and other server-side state. The runner is reached via
    the AP server proxy and its error bodies are relayed to the caller, so
    the cause is logged here for operators while the HTTP response carries
    only this fixed string. The structured ``error`` code that accompanies
    the detail already names the failure category for the caller.

    The runner's own log path is named so the reader can go read the cause
    instead of hunting for it; it is home-relative (``~/…``) so it points
    somewhere without leaking the account name.

    :param exc: The caught exception, e.g. a ``RuntimeError`` from a harness
        spawn or an ``InvalidPath`` from path validation.
    :param context: Short operator-facing label for the failing operation,
        e.g. ``"harness spawn"``. Appears only in the server log.
    :returns: A non-sensitive string safe to return to clients, e.g.
        ``"Request failed on the runner; see the runner log for details:
        ~/.omnigent/logs/runner/runner-conv_ab12.log"``.
    """
    _logger.warning(
        "%s failed: %s",
        context,
        exc,
        exc_info=exc,
        extra={"session_id": runner_primary_session_id()},
    )
    log_reference = process_log_reference("runner")
    return f"Request failed on the runner; see the runner log for details: {log_reference}"


_SpecEntry: TypeAlias = AgentSpec | ResolvedSpec
SpecResolver: TypeAlias = Callable[[str, str | None], Awaitable[_SpecEntry | None]]
_ResourceType: TypeAlias = Literal["environment", "terminal", "file"]


@overload
def _unwrap_spec_entry(entry: None) -> None: ...


@overload
def _unwrap_spec_entry(entry: _SpecEntry) -> AgentSpec: ...


def _unwrap_spec_entry(entry: _SpecEntry | None) -> AgentSpec | None:
    """Return the agent spec from a runner app cache entry."""
    return entry.spec if isinstance(entry, ResolvedSpec) else entry


_NO_BODY_STATUS_CODES = {204, 304}
_SUBAGENT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_SUBAGENT_LIVE_STATUSES = frozenset({"launching", "running", "waiting"})
# Bound how long a sub-agent dispatch can wait for a start acknowledgment.
# A timeout reports uncertain launch status, not proof that the process is dead.
_SUBAGENT_LAUNCH_TIMEOUT_S_ENV = "OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S"
_DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S = 180.0
# Interval for the background sweep in the runner entrypoint.
SUBAGENT_LAUNCH_REAP_INTERVAL_S = 30.0


def resolve_subagent_launch_timeout_s() -> float:
    """
    Resolve the sub-agent launch liveness budget in seconds.

    Values ``<= 0`` disable the reaper. A non-numeric override is rejected
    with a warning and falls back to the default.

    :returns: The budget in seconds, e.g. ``180.0``.
    """
    raw = os.environ.get(_SUBAGENT_LAUNCH_TIMEOUT_S_ENV, "").strip()
    if not raw:
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        value = None
    # Non-finite values (nan/inf) would silently disable reaping without the
    # explicit ``<= 0`` "disabled" intent — reject them like non-numeric input.
    if value is None or not math.isfinite(value):
        _logger.warning(
            "Invalid %s=%r; using default %ss",
            _SUBAGENT_LAUNCH_TIMEOUT_S_ENV,
            raw,
            _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S,
        )
        return _DEFAULT_SUBAGENT_LAUNCH_TIMEOUT_S
    return value


_SUBAGENT_DELIVERY_DELIVERED = "delivered"
_SUBAGENT_DELIVERY_ALREADY_DELIVERED = "already_delivered"
_SUBAGENT_DELIVERY_UNTRACKED = "untracked"
_SUBAGENT_DELIVERY_MISSING_WORK_ENTRY = "missing_work_entry"
_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX = "missing_parent_inbox"
# A delayed terminal op (an interrupt's grace-timer cancel) whose originating
# dispatch has since been replaced by a newer send on the reused child session.
# Dropped without touching the newer dispatch so an old timer never cancels it.
_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH = "superseded_dispatch"
# Runner-owned labels on a child session that make sub-agent result delivery
# durable across a runner restart. The dispatch id is stamped when a turn is
# sent to the child; the delivered id is the receipt the parent's
# ``sys_read_inbox`` drain writes once it has consumed that turn's result.
SUBAGENT_DELIVERED_ID_LABEL_KEY = "omnigent.subagent.delivered_id"
# The parent turn (its response id) that dispatched the child: children started in one turn
# are a group, and their parent is woken once, when the last of them settles.
SUBAGENT_GROUP_LABEL_KEY = "omnigent.subagent.group"
# Read budget for runner→server POSTs that can PARK behind a human-approval
# ASK gate: policy evaluation (``_evaluate_policy_via_omnigent``) and sub-agent
# wake-notice delivery (``_deliver_subagent_wake_post``). Both are gated at the
# recipient's REQUEST/LLM/TOOL phase, which can hold for the deciding policy's
# ``ask_timeout`` (default one day). Held at one day (86400s) — matching that
# default — so the POST WAITS for the real verdict instead of severing the
# parked gate at a short read timeout. A 30s cut previously fail-closed to DENY
# (and the wake POST retried into duplicate approval cards). Fast connect (30s)
# so an unreachable server still fails out promptly into the caller's
# fail-open/retry path. Guarded by tests/test_ask_timeout_infinite.py.
_ASK_GATE_DELIVERY_READ_TIMEOUT_S: float = 86400.0
_ASK_GATE_DELIVERY_TIMEOUT = httpx.Timeout(_ASK_GATE_DELIVERY_READ_TIMEOUT_S, connect=30.0)

# Transport errors that mean the harness channel is dead (subprocess killed,
# connection reset, timeout). A dead channel can never resolve the harness's
# parked policy future, so we signal recovery instead of log-and-swallow.
_DEAD_HARNESS_CHANNEL_ERRORS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.StreamClosed,
    httpx.ConnectError,
    httpx.TimeoutException,
    httpcore.ReadError,
    httpcore.ConnectError,
    httpcore.TimeoutException,
)

# Not in the retryable-harness-error allowlist — desync is terminal, not transient.
_RUNNER_TURN_CONTEXT_DESYNC_CODE = "runner_turn_context_desync"
# Bounded retry budget for the sub-agent wake POST. The wake is the sole
# delivery signal for the last child of a fan-out, and Omnigent routinely
# returns a transient 503 RUNNER_UNAVAILABLE while the parent's runner tunnel
# is reconnecting, so a single attempt can strand the parent silently.
_WAKE_POST_MAX_ATTEMPTS = 3
_WAKE_POST_RETRY_BASE_DELAY_S = 0.5
_WAKE_POST_RETRY_MAX_DELAY_S = 4.0
# Delays before each stranded-wake re-attempt round after a tunnel reconnect.
# The reconnect callback fires BEFORE the new tunnel connection is established,
# and the server 503s an injected parent event while the parent's runner is
# still offline — so pace the rounds to outlast a slow handshake instead of
# spending the bounded per-POST retries against a not-yet-open tunnel. The
# final long round covers a server that is reachable but slow to become ready;
# rounds cost nothing once no parent is stranded (the loop exits early).
_STRANDED_WAKE_RETRY_DELAYS_S = (2.0, 5.0, 10.0, 30.0)
# 4xx statuses that are transient and worth retrying (mirrors the forwarder's
# classification): everything else in 4xx is a permanent client-side rejection.
_WAKE_POST_TRANSIENT_4XX = frozenset({408, 409, 425, 429})

# Cadence for ``session.heartbeat`` keepalive events on the runner's
# ``GET /v1/sessions/{id}/stream`` endpoint. Between turns the event
# queue is idle — without periodic bytes, an intermediate proxy (e.g.
# the Databricks Apps ingress) can drop the long-lived HTTP connection.
# Matches the AP-side ``_SESSION_STREAM_HEARTBEAT_INTERVAL_S``.
_SESSION_STREAM_HEARTBEAT_S = 15.0

# Lazy singleton LLM client for the runner process. Created on first use so
# the runner does not import llms at startup (imports are expensive and the
# /v1/summarize endpoint is optional). The concrete type is imported only
# during type checking to keep the runtime import graph lazy.
_runner_llm_client: LLMClient | None = None


def _get_runner_llm_client() -> LLMClient:
    """Return the runner-process LLM client, creating it on first use.

    The client is constructed from the runner process's environment
    variables, which include the Databricks credentials set up by the
    runner entry point. This is intentionally separate from the AP
    server's ``_get_llm_client()`` — the runner may have different
    (or more) credentials than the Omnigent server.

    :returns: A ``llms.Client`` instance bound to this runner process.
    """
    global _runner_llm_client
    if _runner_llm_client is None:
        from omnigent.llms import Client as LLMClient

        _runner_llm_client = LLMClient()
    return _runner_llm_client


# Marker the runner stamps on action_required SSE events it intends
# to dispatch locally. See designs/RUNNER_MCP.md §Explicit dispatch
# marker.
_RUNNER_DISPATCHED_FIELD = "omnigent_runner_dispatched"
#: Longest a turn waits on the session's harness pre-launch; past it the pre-launch is
#: stopped and the turn launches the CLI itself, so a hung one never costs more than this.
_PRELAUNCH_WAIT_SECONDS = 3.0
#: A turn's setup (before the prefix) slower than this logs where its time went.
_SLOW_SETUP_SECONDS = 0.5
#: Terminal frames of a harness turn: a tool call still open when one arrives never returned.
_TURN_END_EVENT_TYPES = frozenset(
    {"response.completed", "response.failed", "response.incomplete", "response.cancelled"}
)


def _encode_sse_event(event: Mapping[str, object]) -> bytes:
    """Re-encode an SSE event as a single ``data:`` frame."""
    import json as _json

    return f"data: {_json.dumps(event)}\n\n".encode()


class _PausedStop(Exception):
    """A paused Helper's pause expired before its native tool call was evaluated."""


async def _evaluate_policy_via_omnigent(
    *,
    server_client: httpx.AsyncClient,
    harness_client: httpx.AsyncClient,
    conversation_id: str,
    evaluation_id: str,
    phase: str,
    data: dict[str, Any],
    on_delivery_failure: Callable[[str], Awaitable[None]] | None = None,
    hold: Callable[[str], Awaitable[bool]] | None = None,
) -> None:
    """
    Proxy a policy evaluation request from the harness to the Omnigent server.

    Called by the runner's ``proxy_stream`` when it intercepts a
    ``policy_evaluation.requested`` SSE event from the harness. Posts
    the evaluation request to the Omnigent server's
    ``POST /sessions/{id}/policies/evaluate`` endpoint, then delivers
    the verdict back to the harness as a ``policy_verdict`` inbound
    event.

    On failure (AP unreachable, non-200, malformed response) the default
    verdict is phase-aware:

    - ``PHASE_LLM_REQUEST`` / ``PHASE_LLM_RESPONSE`` fail OPEN
      (``POLICY_ACTION_ALLOW``) so a transient Omnigent outage does not
      hang the turn — these gates are advisory.
    - ``PHASE_TOOL_CALL`` fails CLOSED (``POLICY_ACTION_DENY``). For
      connector-native MCP tools the harness ``can_use_tool`` callback
      (which consumes this verdict) is the *only* enforcement point — the
      call is never re-checked server-side — so a policy that cannot be
      evaluated must not let the tool through.
    - ``PHASE_TOOL_RESULT`` fails OPEN: by the result phase the tool has
      already executed, so denying would only block an already-incurred
      side effect.

    :param server_client: HTTP client pointed at the Omnigent server.
    :param harness_client: HTTP client pointed at the harness subprocess.
    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param evaluation_id: Unique correlation id from the harness,
        e.g. ``"poleval_abc123"``.
    :param phase: Proto-style phase string, e.g.
        ``"PHASE_LLM_REQUEST"``.
    :param data: Event data dict for the policy engine.
    :param on_delivery_failure: Called with *conversation_id* when the verdict
        cannot be delivered after retry; wired by callers to cancel the wedged turn.
    :param hold: The paused-Helper hold (ADR 0027), awaited before a native tool call is
        evaluated; ``False`` (the pause expired) denies the call.
    """
    # Default verdict on error / non-200 / timeout. Phase-aware: TOOL_CALL
    # fails CLOSED (this round-trip is the authoritative gate for
    # connector-native tools), while advisory LLM phases and TOOL_RESULT
    # (the tool already ran) fail OPEN so a transient outage never hangs
    # the turn.
    _fail_closed = phase in FAIL_CLOSED_PHASES
    _default_action = "POLICY_ACTION_DENY" if _fail_closed else "POLICY_ACTION_ALLOW"
    verdict_action = _default_action
    verdict_reason: str | None = (
        f"Omnigent policy evaluation unavailable; failing closed for {phase}."
        if _fail_closed
        else None
    )
    verdict_data: _JsonObject | None = None
    # A paused Helper's native tool call waits here, at its tool boundary.
    stopped = phase == "PHASE_TOOL_CALL" and hold is not None and not await hold(conversation_id)
    if stopped:
        verdict_action, verdict_reason = "POLICY_ACTION_DENY", "Stopped while paused."

    try:
        if stopped:
            raise _PausedStop
        ap_resp = await server_client.post(
            f"/v1/sessions/{conversation_id}/policies/evaluate",
            json={
                "event": {
                    "type": phase,
                    "data": data,
                },
            },
            # A TOOL_CALL/LLM_REQUEST/REQUEST ASK parks server-side in
            # ``_hold_native_ask_gate`` until a human resolves it (up to the
            # deciding policy's ``ask_timeout``, default one day). A 30s read
            # budget here severed that long-poll after 30s — the server saw an
            # UPSTREAM DISCONNECT and failed the gate closed (DENY), so the
            # main (claude-sdk) agent's approval card auto-resolved while
            # native sub-agents (whose hooks already wait the full day) parked
            # correctly. Hold the read budget at one day to match the native
            # hooks' ``_EVALUATE_POLICY_TIMEOUT_S``; the server's ``ask_timeout``
            # remains the single real cap. Fast connect so an unreachable
            # server still fails out promptly into the fail-open path below.
            timeout=_ASK_GATE_DELIVERY_TIMEOUT,
        )
        if ap_resp.status_code == 200:
            result = ap_resp.json()
            # A well-formed 200 carries "result"; a malformed body that
            # omits it falls back to _default_action — i.e. DENY on a
            # tool-call phase. That's deliberate: a 200 we can't read is
            # an unevaluable verdict, which fails closed like any other.
            verdict_action = result.get("result", _default_action)
            verdict_reason = result.get("reason")
            verdict_data = result.get("data")
        else:
            _logger.warning(
                "AP policy evaluate returned %d for %s; defaulting to %s",
                ap_resp.status_code,
                evaluation_id,
                _default_action,
                extra={"session_id": conversation_id},
            )
    except _PausedStop:
        pass
    except Exception:
        _logger.warning(
            "AP policy evaluate failed for %s; defaulting to %s",
            evaluation_id,
            _default_action,
            exc_info=True,
            extra={"session_id": conversation_id},
        )

    # Post the verdict back to the harness as a policy_verdict event.
    verdict_body: dict[str, Any] = {
        "type": "policy_verdict",
        "evaluation_id": evaluation_id,
        "action": verdict_action,
    }
    if verdict_reason is not None:
        verdict_body["reason"] = verdict_reason
    if verdict_data is not None:
        verdict_body["data"] = verdict_data

    # Retry once on dead-channel / timeout / non-2xx; any unacknowledged verdict
    # eventually calls on_delivery_failure to cancel the wedged turn. Track the
    # failure mode so the wrap-up log attributes the cause instead of lumping
    # every mode into one unattributed record.
    failure_reason = "unexpected"
    for _attempt in range(2):
        try:
            resp = await harness_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json=verdict_body,
                timeout=30.0,
            )
        except _DEAD_HARNESS_CHANNEL_ERRORS as exc:
            failure_reason = "dead_channel"
            _logger.warning(
                "Policy verdict %s delivery hit a dead harness channel (attempt %d/2): %s",
                evaluation_id,
                _attempt + 1,
                exc,
                extra={"session_id": conversation_id},
            )
            continue
        except Exception:
            failure_reason = "unexpected"
            _logger.warning(
                "Failed to deliver policy verdict %s to harness (unexpected error)",
                evaluation_id,
                exc_info=True,
                extra={"session_id": conversation_id},
            )
            break
        if 200 <= resp.status_code < 300:
            return
        failure_reason = f"http_{resp.status_code}"
        _logger.warning(
            "Policy verdict %s delivery got HTTP %d — harness did not accept it (attempt %d/2)",
            evaluation_id,
            resp.status_code,
            _attempt + 1,
            extra={"session_id": conversation_id},
        )

    if failure_reason == "dead_channel":
        # The harness channel died before the verdict could land — an upstream
        # disconnect/teardown consequence whose primary failure (the harness
        # death) is surfaced by stream teardown, not an Omnigent defect. Log at
        # WARNING with a structured reason; the desync recovery still runs.
        _logger.warning(
            "Policy verdict %s undeliverable after retry: harness channel is dead "
            "(upstream disconnect/teardown); signaling desync for %s",
            evaluation_id,
            conversation_id,
            extra={
                "session_id": conversation_id,
                "delivery_failure_reason": "verdict_delivery_channel_dead",
            },
        )
    else:
        # A live harness refused the verdict (non-2xx) or delivery failed in an
        # unforeseen way — potentially a real protocol defect, kept at ERROR.
        _logger.error(
            "Policy verdict %s delivery unacknowledged (%s) after retry; signaling desync for %s",
            evaluation_id,
            failure_reason,
            conversation_id,
            extra={
                "session_id": conversation_id,
                "delivery_failure_reason": failure_reason,
            },
        )
    if on_delivery_failure is not None:
        await on_delivery_failure(conversation_id)


def _response_body_preview(resp: object, *, limit: int = 500) -> str:
    """
    Return a short response-body preview for diagnostics.

    Some runner tests use lightweight response fakes that expose
    ``content`` and ``status_code`` but not HTTPX's convenience
    ``text`` property. Logging should not make those fakes diverge from
    production behavior.

    :param resp: Response-like object, e.g. ``httpx.Response``.
    :param limit: Maximum number of characters to include.
    :returns: Decoded response text preview.
    """
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text[:limit]
    content = getattr(resp, "content", b"")
    if isinstance(content, bytes):
        return content[:limit].decode("utf-8", errors="replace")
    if isinstance(content, str):
        return content[:limit]
    return ""


@dataclasses.dataclass
@dataclasses.dataclass(frozen=True)
class _SessionSnapshot:
    """One ``GET /v1/sessions/{id}`` projected for all runner readers.

    The single source registration, workspace resolution, and spec
    resolution share instead of each fetching. See
    :func:`_session_snapshot` for the single-flight loader.

    :param ok: ``True`` only when the fetch returned HTTP 200.
    :param status_code: The fetch's HTTP status, or ``None`` on a
        transport error before any response, e.g. ``200`` / ``404``.
    :param created_at: Server creation time (UNIX seconds), or the
        runner's wall clock when the fetch failed / omitted it.
    :param workspace: Server-stored workspace path, or ``None``.
    :param agent_id: Bound agent id, or ``None`` when not yet bound /
        the fetch failed, e.g. ``"ag_abc123"``.
    :param sub_agent_name: For sub-agent sessions, the dispatched
        sub-agent's name, e.g. ``"claude_code"`` — used to swap the
        parent spec to the child's sub-spec so the child's harness
        (e.g. ``pi``) is resolved instead of the parent's.
        ``None`` for top-level sessions. Projected from the server
        snapshot so the identity survives a runner reconnect / spec-cache
        eviction (the in-memory ``_session_sub_agent_names`` map does not).
    :param parent_session_id: For sub-agent sessions, the parent
        conversation's id, e.g. ``"conv_parent987"``. ``None`` for
        top-level sessions. Lets ``_ensure_subagent_work_entry`` rebuild a lost
        work entry when the in-memory map was wiped (reconnect / restart) or
        never populated (a ``sys_session_create`` child).
    :param agent_name: Human-readable bound agent name, e.g.
        ``"researcher"``. Used as the sub-agent label when rebuilding a
        work entry for a child the server did not record a ``sub_agent_name``
        for. ``None`` when unbound / the fetch failed.
    """

    ok: bool
    status_code: int | None
    created_at: float
    workspace: str | None
    agent_id: str | None
    sub_agent_name: str | None = None
    parent_session_id: str | None = None
    agent_name: str | None = None


@dataclasses.dataclass(frozen=True)
class _SessionInitContext:
    """Metadata source selected before shared session initialization runs."""

    envelope: RunnerSessionInitEnvelope | None

    @property
    def labels(self) -> Mapping[str, str] | None:
        """Return server-supplied labels, or ``None`` on the legacy path."""
        return self.envelope.snapshot.labels if self.envelope is not None else None

    @property
    def routing_class(self) -> SessionRoutingClass:
        """Return the session's Smart Routing class.

        The legacy path carries no snapshot, so it reads as plain — a
        session whose routing state cannot be established must not pay any
        routing-path cost.
        """
        if self.envelope is None:
            return PLAIN_SESSION
        snapshot = self.envelope.snapshot
        return routing_class_from_snapshot(
            cost_control_mode=snapshot.cost_control_mode_override,
            harness_override=snapshot.harness_override,
            labels=snapshot.labels,
        )


# Language constant the omnigent YAML translator stamps on callable-backed
# tools (omnigent/spec/omnigent.py:OMNIGENT_TOOL_LANGUAGE). Duplicated rather
# than imported to avoid pulling the heavy translator module in for one
# string — same rationale as omnigent/tools/local_callable.py.
_OMNIGENT_CALLABLE_LANGUAGE = "omnigent-python-callable"


def _looks_like_file_path(path: str) -> bool:
    """
    Return whether *path* is a filesystem path rather than a dotted import.

    File-based local tools are discovered as ``tools/python/foo.py`` /
    ``tools/typescript/foo.ts`` — always carrying a path separator and a
    source extension (see :func:`omnigent.spec.parser._discover_local_tools`).
    Callable-backed tools store a dotted import path (``pkg.mod.func``) in the
    same field — no separator, no source extension. This structural test is
    the primary guard so a rename of the callable-tool *language* string can
    never reintroduce the workdir-mangling bug.

    :param path: A :class:`LocalToolInfo` ``path`` value.
    :returns: ``True`` when *path* is a file path safe to resolve onto the
        workdir; ``False`` for dotted import paths.
    """
    return "/" in path or os.sep in path or path.endswith((".py", ".ts"))


def _spec_with_workdir_paths(
    spec: AgentSpec | None,
    workdir: Path | None,
) -> AgentSpec | None:
    if workdir is None or spec is None:
        return spec
    local_tools = getattr(spec, "local_tools", None)
    if not local_tools:
        return spec
    resolved_tools: list[LocalToolInfo] = []
    changed = False
    for info in local_tools:
        path = getattr(info, "path", None)
        # Only resolve genuine file paths onto the workdir. Callable-backed
        # tools store a dotted import path (``pkg.mod.func``) in the same
        # field; joining that to the workdir corrupts it, the import fails,
        # the tool never registers, and any tool_call policy narrowed to it
        # can never fire. The structural file-vs-dotted check is the primary
        # guard; the language check is belt-and-suspenders.
        if (
            path
            and getattr(info, "language", None) != _OMNIGENT_CALLABLE_LANGUAGE
            and _looks_like_file_path(path)
            and not Path(path).is_absolute()
        ):
            resolved_tools.append(dataclasses.replace(info, path=str((workdir / path).resolve())))
            changed = True
        else:
            resolved_tools.append(info)
    if not changed:
        return spec
    return dataclasses.replace(spec, local_tools=resolved_tools)


@dataclasses.dataclass
class TurnDispatch:
    """
    Runner-side dispatch context for a single turn.

    Carries metadata the runner needs for harness resolution,
    MCP schema injection, and system prompt — separated from
    the harness message body so no field-stripping is needed.

    :param agent_id: Agent identifier for spec resolution,
        e.g. ``"ag_abc123"``.
    :param harness: Harness type, e.g. ``"claude-sdk"``.
    :param has_mcp_servers: Whether to inject MCP tool schemas.
    :param instructions: System prompt for the LLM.
    :param agent_version: Spec version for invalidation.
    :param spawn_env: Harness subprocess environment overrides.
    :param client_side_tool_names: Names of request-supplied
        client-side tools for this turn (e.g. ``{"Read", "Glob"}``).
        These are executed by the caller, not the runner, so the
        proxy_stream relays their ``action_required`` events upstream
        to tunnel rather than dispatching them locally.
    """

    agent_id: str | None = None
    harness: str | None = None
    has_mcp_servers: bool = False
    instructions: str | None = None
    agent_version: int | None = None
    spawn_env: dict[str, str] | None = None
    client_side_tool_names: frozenset[str] = frozenset()


@dataclasses.dataclass
class InstructionComposition:
    """Runner-local, never-serialized view of this turn's instruction state.

    Computed once inside ``_stream_message_to_harness`` (the point where the
    background and direct-stream dispatch paths converge) and consumed
    in-process by the single delivery-gap warn check. Never attached to
    ``TurnDispatch``, ``MessageEvent``, ``CreateResponseRequest``, or
    ``ExecutorConfig`` — the wire shape is unchanged from today.

    :param authored_present: Whether ``AgentSpec.instructions`` is
        non-empty/non-whitespace, resolved pre-composition.
    :param composed: The meaningful composed text (author + applicable
        framework instructions), or ``None`` if there is truly nothing.
    """

    authored_present: bool
    composed: str | None


def _prefix_blocks_with_tail(blocks: object, prefix: str) -> object:
    """Prepend *prefix* to the first text content block in *blocks*.

    :param blocks: A plain string, or a list of content blocks
        (``{"type": "input_text", "text": ...}`` and the like).
    :param prefix: Text to prepend, already carrying its own trailing
        separator.
    :returns: *blocks* with *prefix* prepended, never mutating the input.
    """
    if isinstance(blocks, str):
        return prefix + blocks
    if isinstance(blocks, list):
        out = list(blocks)
        for i, block in enumerate(out):
            if (
                isinstance(block, dict)
                and block.get("type") in ("input_text", "text")
                and isinstance(block.get("text"), str)
            ):
                out[i] = {**block, "text": prefix + block["text"]}
                return out
        return [{"type": "input_text", "text": prefix.rstrip("\n")}, *out]
    return blocks


_FLAT_BLOCK_TYPES = frozenset({"input_text", "text", "input_image", "input_file"})


def _prefix_content_with_tail(content: object, tail: str) -> object:
    """Prepend *tail* to a turn's outgoing *content*, matching its shape.

    Two shapes reach the harness, depending on the dispatch path: a flat
    list of content blocks for a single new message (the direct-stream
    path), or Responses-style conversation history — a list of
    ``{"type": "message", "role": ..., "content": [...]}`` items (the
    background-turn path, built from ``_session_histories``). The tail
    belongs on the LATEST user turn either way, so a history list gets its
    last user message's blocks prefixed instead of the list itself.

    :param content: A turn's outgoing content — a plain string, a list of
        content blocks, a list of history message items, or something else
        (left untouched).
    :param tail: Text to prepend, e.g. a post-compaction tail block.
    :returns: *content* with *tail* prepended, never mutating the input.
    """
    prefix = f"{tail}\n\n"
    return _edit_latest_user_content(
        content, lambda blocks: _prefix_blocks_with_tail(blocks, prefix)
    )


def _edit_latest_user_content(content: object, edit: Callable[[object], object]) -> object:
    """*content* with *edit* applied to the latest user message's content, never mutating it.

    *content* is a flat list of content blocks (a string too), edited whole, or Responses-style
    history, whose last user message is edited (see :func:`_prefix_content_with_tail`).
    """
    # History interleaves ``function_call`` / ``function_call_output`` /
    # ``reasoning`` items with messages once a turn used tools, so "history"
    # means every item is a typed Responses item and at least one is a message.
    # A flat block list (``input_text`` ...) has no ``message`` item.
    is_history = (
        isinstance(content, list)
        and bool(content)
        and all(
            isinstance(item, dict) and item.get("type") not in _FLAT_BLOCK_TYPES
            for item in content
        )
        and any(item.get("type") == "message" for item in content)
    )
    if not is_history:
        return edit(content)
    messages = cast(list[dict[str, Any]], content)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            updated = list(messages)
            updated[i] = {**messages[i], "content": edit(messages[i].get("content"))}
            return updated
    return content


def _with_parts(content: object, parts: Sequence[_JsonObject]) -> object:
    """A message's *content* as content blocks with *parts* after its own."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}, *parts]
    return [*(content if isinstance(content, list) else []), *parts]


def _apply_memory_profile_to_body(body: _JsonObject, block: str | None) -> _JsonObject:
    """Prepend *block* (a rendered Memory Profile, already delimiter-wrapped)
    to *body*'s outgoing content, or return *body* unchanged if *block* is
    falsy (memory not configured, or the profile is empty — never deliver an
    empty block). Mutates only the in-flight request for this turn — the
    persisted user item, ``_session_histories`` and
    ``_session_message_buffers`` copies of *body* are untouched, and the
    block never reaches a rollover summary (``docs/context.md``). Shares
    :func:`_prefix_content_with_tail`'s shape handling (flat
    content blocks vs. a history-list turn).

    :param body: This turn's outgoing harness body (``{"content": ...}``).
    :param block: The rendered, delimiter-wrapped Memory Profile block, or
        ``None``/empty.
    :returns: *body* with *block* prepended to its content, or *body* itself
        (never mutated) when *block* is falsy.
    """
    if not block:
        return body
    new_body = dict(body)
    new_body["content"] = _prefix_content_with_tail(body.get("content"), block)
    return new_body


def _message_texts(content: object) -> list[str]:
    """Every text part of a message's content (a string, or nested blocks/items)."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [t for part in content for t in _message_texts(part)]
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return [text]
        return _message_texts(content.get("content"))
    return []


def _prepend_turn_blocks(
    body: _JsonObject, blocks: Sequence[str | _JsonObject], conv_id: str
) -> _JsonObject:
    """Prepend each text one of *blocks* to *body*'s content in turn (the last ends up first);
    a content block (an attached image) goes after the latest user message's own blocks.

    Logs at DEBUG which blocks went in (first line of each), so a turn's prefix can be checked
    without dumping the person's profile.
    """
    parts = [block for block in blocks if isinstance(block, dict)]
    for block in blocks:
        if isinstance(block, str):
            body = _apply_memory_profile_to_body(body, block)
    if parts:
        content = _edit_latest_user_content(
            body.get("content"), lambda own: _with_parts(own, parts)
        )
        body = {**body, "content": content}
    if _logger.isEnabledFor(logging.DEBUG):
        _logger.debug(
            "turn prefix for %s: %s",
            conv_id,
            [
                (b.split("\n", 1)[0][:60], len(b)) if isinstance(b, str) else (b.get("type"), 1)
                for b in blocks
            ],
            extra={"session_id": conv_id},
        )
    return body


def _wrap_as_message_event(body: _JsonObject) -> _JsonObject:
    """
    Adapt a ``CreateResponseRequest``-shaped body into a
    :class:`MessageEvent` body for the harness's discriminated
    ``POST /v1/sessions/{id}/events`` endpoint.

    The runtime still synthesizes ``CreateResponseRequest``-shaped
    bodies internally to drive harness turns; this helper renames
    ``input`` → ``content`` and stamps the discriminator
    (``type="message"``) and role (``role="user"``) fields without
    copying every other field by name — the harness's
    :class:`MessageEvent` accepts arbitrary extras and forwards them
    onto its synthesized :class:`CreateResponseRequest`, so
    passthrough is automatic.

    :param body: The runner's incoming JSON body, e.g.
        ``{"model": "agent", "input": [...], "tools": [...]}``.
    :returns: A new dict in :class:`MessageEvent` shape, e.g.
        ``{"type": "message", "role": "user", "model": "agent",
        "content": [...], "tools": [...]}``. Does not mutate the
        input dict.
    """
    event_body = dict(body)
    event_body["type"] = "message"
    event_body["role"] = "user"
    if "input" in event_body:
        event_body["content"] = event_body.pop("input")
    return event_body


class _ContextWindowOverflow(Exception):
    """
    Raised and caught inside ``proxy_stream`` when the harness reports a
    context-window overflow, so both live and background turns end the
    same way.

    :param max_tokens: The model's context window.
    :param actual_tokens: The prompt size that overflowed.
    """

    def __init__(self, max_tokens: int, actual_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.actual_tokens = actual_tokens
        super().__init__(f"context window exceeded: {actual_tokens} > {max_tokens}")


_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "prompt is too long",
)


def _is_context_overflow_error(event: _JsonObject) -> tuple[int, int] | None:
    """
    Check if a ``response.failed`` SSE event indicates a context-window overflow.

    :param event: The parsed SSE event dict.
    :returns: ``(max_tokens, actual_tokens)`` if overflow detected, else ``None``.
    """
    if event.get("type") != "response.failed":
        return None
    error = cast(_JsonObject, event.get("error", {}))
    msg = str(error.get("message", "")).lower()
    if not any(pat in msg for pat in _CONTEXT_OVERFLOW_PATTERNS):
        return None
    actual_gt_max = re.search(r"(\d{4,})\D*>\D*(\d{4,})", msg)
    if actual_gt_max is not None:
        return int(actual_gt_max.group(2)), int(actual_gt_max.group(1))

    numbers = re.findall(r"(\d{4,})", msg)
    if len(numbers) >= 2:
        return int(numbers[-2]), int(numbers[-1])
    if len(numbers) == 1:
        return int(numbers[0]), int(numbers[0]) + 1
    return 128000, 128001


def _response_failed_payload(
    error: Mapping[str, object],
    source: str = "execution",
) -> _JsonObject:
    """Build a failure envelope with required error fields and a legacy mirror."""
    # Normalized fields go LAST: _normalize_turn_error guarantees non-empty
    # code/message, but merging it first let a raw error's own blank/missing
    # "message" (present as a key with "" or None, not merely absent) win the
    # spread and reach the client as a message-less response.failed/response.error
    # event. Any other field *error* carries (e.g. "type") still passes through.
    failure_error = {**error, **_normalize_turn_error(error)}
    return {
        "type": "response.failed",
        "source": source,
        "response": {"status": "failed", "error": failure_error},
        "error": failure_error,
    }


def _response_failed_event(
    error: Mapping[str, object],
    source: str = "execution",
) -> bytes:
    """
    Encode one ``response.failed`` SSE frame.

    Keep a top-level ``error`` mirror for older tests/debuggers that
    inspected the legacy runner proxy shape directly.

    :param error: Error payload to place under ``response.error``,
        e.g. ``{"code": "connection_error", "message": "dropped"}``.
    :param source: Where the fault originated -- ``"llm"`` for
        inference/context errors, ``"harness"`` for Claude Code/harness
        process failures, ``"execution"`` for runner configuration
        or infrastructure failures.  Forwarded as-is to the AP server
        so it can persist the right ``ErrorData.source``.
    :returns: UTF-8 encoded SSE frame bytes.
    """
    payload = json.dumps(_response_failed_payload(error, source=source))
    return f"event: response.failed\ndata: {payload}\n\n".encode()


async def _resolve_forwarded_message_content(
    content: list[_JsonObject],
    *,
    session_id: str,
    server_client: httpx.AsyncClient,
) -> list[_JsonObject]:
    """Resolve server-uploaded ``file_id`` blocks inside the runner.

    Remote Omnigent servers can forward session messages with raw file IDs
    because their file store is not available to the out-of-process
    runner. The runner can still fetch bytes through the session-scoped
    file resource endpoint and inline them before handing content to a
    harness. Blocks already resolved by the server pass through.
    """
    if not any(isinstance(block, dict) and has_unresolved_file_id(block) for block in content):
        return content

    resolved: list[_JsonObject] = []
    changed = False
    for block in content:
        result = None
        if isinstance(block, dict) and has_unresolved_file_id(block):
            result = await resolve_file_id_block(
                block, session_id=session_id, client=server_client
            )
        if result is None:
            resolved.append(block)
        else:
            new_block, notice = result
            resolved.append(new_block)
            if notice is not None:
                resolved.append(framework_notice_block(notice))
            changed = True

    return resolved if changed else content


def _inject_mcp_schemas(
    event_body: _JsonObject,
    mcp_schemas: list[_JsonObject],
) -> None:
    """Append *mcp_schemas* to ``event_body["tools"]`` in place.

    Preserves any existing tools (builtins / client-side from the AP
    server) and adds MCP schemas after them. No-op when *mcp_schemas*
    is empty. See ``designs/RUNNER_MCP.md`` §Schema injection.

    Skips schemas already present by name: the per-session tool cache
    also folds in MCP schemas, and codex rejects duplicate tool names.
    """
    if not mcp_schemas:
        return
    existing = cast(list[_JsonObject], event_body.get("tools") or [])
    existing_names = {t.get("name") for t in existing if t.get("name")}
    new_schemas = [s for s in mcp_schemas if s.get("name") not in existing_names]
    event_body["tools"] = list(existing) + new_schemas


def _schema_tool_name(schema: _JsonObject) -> str | None:
    """
    Extract a tool's function name from its OpenAI-format schema.

    :param schema: A tool schema dict in nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``.
    :returns: The tool name (e.g. ``"Read"``), or ``None`` when the
        schema is malformed / missing the ``function.name`` field.
    """
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    return None


def _merge_request_client_tools(
    spec_tools: list[_JsonObject],
    client_tools: list[_JsonObject],
) -> list[_JsonObject]:
    """
    Append request-supplied client-side tools to the spec tool schemas.

    The runner-native session path assembles the harness tool list from
    the agent spec's builtin + MCP schemas only. Client-side tools the
    caller registers on the event (``request.tools`` — e.g. a REPL's
    ``Read`` / ``Write`` / ``Glob``) must also reach non-native harnesses
    so the model can emit them. The resulting call is not in
    ``_ALL_LOCAL_TOOLS``, so ``dispatch_tool_locally`` relays the
    ``action_required`` event upstream and it tunnels back to the caller.
    Without this merge the schemas never reach the executor and the model
    cannot invoke client tools at all.

    Builtins win on a name clash: a request tool must not shadow a
    policy-enforced server-side builtin of the same name.

    :param spec_tools: Spec-derived builtin + MCP tool schemas, each in
        nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "load_skill", ...}}``.
    :param client_tools: Request-supplied client-side tool schemas in the
        same nested OpenAI format, e.g.
        ``{"type": "function", "function": {"name": "Read", ...}}``.
    :returns: ``spec_tools`` followed by the named client tools whose names
        don't collide with a spec tool. Non-dict and nameless client
        entries are dropped. A fresh list; inputs are not mutated. Empty
        when both inputs are empty.
    """
    seen: set[str] = {
        name
        for t in spec_tools
        if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
    }
    merged: list[_JsonObject] = list(spec_tools)
    for tool in client_tools:
        if not isinstance(tool, dict):
            continue
        name = _schema_tool_name(tool)
        # Drop nameless/malformed entries: the executor rejects an unnamed
        # FunctionTool, so forwarding one would only risk a hard error.
        if name is None or name in seen:
            continue
        seen.add(name)
        merged.append(tool)
    return merged


def _should_dispatch_tool_locally(
    tool_name: str,
    *,
    dispatch: TurnDispatch | None,
    is_mcp: bool,
    is_runner_builtin: bool,
    is_spec_local: bool,
) -> bool:
    """
    Decide whether the runner dispatches *tool_name* locally vs. relays it.

    Client-side (request-supplied) tools execute on the caller, so their
    ``action_required`` events must relay upstream to tunnel — dispatching
    them locally would error ``"<tool> not in local dispatch table"``. Every
    other tool keeps the prior behavior, including the ``dispatch is not
    None`` catch-all that covers spec-local / UC / spec-callable tools in
    session-native mode.

    :param tool_name: The tool the LLM called, e.g. ``"Read"`` or
        ``"sys_session_send"``.
    :param dispatch: The turn's :class:`TurnDispatch` (carries
        ``client_side_tool_names``), or ``None`` on the legacy path.
    :param is_mcp: ``True`` when *tool_name* is an MCP-server tool for
        this turn.
    :param is_runner_builtin: ``True`` when *tool_name* is a
        runner-dispatched builtin (``should_dispatch_locally(tool_name)``).
    :param is_spec_local: ``True`` when *tool_name* is a spec-declared
        local python/callable tool.
    :returns: ``True`` to dispatch locally on the runner; ``False`` to
        relay the ``action_required`` event upstream (client-side tunnel).
    """
    if dispatch is not None and tool_name in dispatch.client_side_tool_names:
        return False
    return dispatch is not None or is_mcp or is_runner_builtin or is_spec_local


@dataclasses.dataclass
class _SubagentWorkEntry:
    """
    Runner-local state for one asynchronous ``sys_session_send`` dispatch.

    :param parent_session_id: Parent session id that invoked
        ``sys_session_send``, e.g. ``"conv_parent123"``.
    :param child_session_id: Child session id used as the work handle,
        e.g. ``"conv_child456"``.
    :param work_id: Unique id for this dispatch to the child session,
        e.g. ``"subagent_a1b2c3"``.
    :param agent: Sub-agent name from the parent spec, e.g.
        ``"researcher"``.
    :param title: Caller-provided child instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional ``omnigent.wrapper`` label from the
        child session.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param status: Current work status, e.g. ``"launching"`` or
        ``"running"``.
    :param output: Terminal child output or error text. ``None``
        while the work is still running.
    :param created_at: Unix timestamp when the dispatch was registered.
    :param completed_at: Unix timestamp when the dispatch reached a
        terminal status, or ``None`` while running.
    :param delivered: Whether the terminal payload has been pushed to
        the parent's inbox.
    :param cancellation_confirmed: Whether a native terminal edge confirmed
        an abort, rather than an interrupt merely being requested.
    :param superside_chat: Whether the dispatching parent is a
        superside-chat session (``omnigent.context.mode=superside-chat``),
        set at registration time from the dispatch's own labels. Gates the
        Result-in-wake behavior (slice S3): see ``wake_inlined``.
    :param wake_inlined: Whether this entry's Result was already delivered
        verbatim in its wake notice — set when the wake is scheduled for a
        ``superside_chat`` entry. ``sys_read_inbox`` reads this to avoid
        repeating the Result text once the item is drained.
    :param group: Response id of the parent turn that dispatched it, e.g.
        ``"resp_5bfa746a"``; ``None`` outside a parent turn. The parent is
        woken once for a group, when no member still runs.
    """

    parent_session_id: str
    child_session_id: str
    work_id: str
    agent: str
    title: str
    wrapper_label: str | None = None
    created_by: str | None = None
    status: str = "launching"
    output: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)
    completed_at: float | None = None
    delivered: bool = False
    cancellation_confirmed: bool = False
    superside_chat: bool = False
    wake_inlined: bool = False
    group: str | None = None


@dataclasses.dataclass(frozen=True)
class _SubagentDeliveryAck:
    """
    Result of attempting to deliver a terminal sub-agent payload.

    :param entry: Work entry whose delivery was attempted, or ``None``
        when the child session is not tracked in the work registry.
    :param delivered: Whether the payload is confirmed delivered to the
        parent inbox. True for both first delivery and already-delivered
        duplicate terminal reports.
    :param delivered_now: Whether this attempt pushed a new payload into
        the parent inbox.
    :param reason: Machine-readable outcome, e.g. ``"delivered"`` or
        ``"missing_parent_inbox"``.
    """

    entry: _SubagentWorkEntry | None
    delivered: bool
    delivered_now: bool
    reason: str


class MarkSubagentTerminalAndWake(Protocol):
    """Mark a sub-agent work entry terminal and wake its parent."""

    def __call__(
        self,
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
    ) -> _SubagentDeliveryAck:
        raise NotImplementedError


_subagent_work_by_child: dict[str, _SubagentWorkEntry] = {}
_subagent_work_by_parent: dict[str, set[str]] = {}
_drained_delivered_subagent_children: set[str] = set()
# Parents whose restart-recovery scan completed in this process, plus a
# per-parent lock so an init racing a sys_read_inbox drain cannot run two
# scans that both pass the registry check and queue one result twice.
_subagent_recovery_done: set[str] = set()
_subagent_recovery_locks: dict[str, asyncio.Lock] = {}

# Per-(parent, agent_type) monotonic ordinal counter for structured
# sub-agent names (e.g. "researcher-1", "researcher-2").
_subagent_ordinal_counters: dict[tuple[str, str], int] = {}


def next_subagent_ordinal(parent_session_id: str, agent_type: str) -> int:
    """Return the next ordinal for a (parent, agent_type) pair and bump the counter."""
    key = (parent_session_id, agent_type)
    ordinal = _subagent_ordinal_counters.get(key, 0) + 1
    _subagent_ordinal_counters[key] = ordinal
    return ordinal


def recover_subagent_ordinals(
    parent_session_id: str,
    agent_type: str,
    existing_children: list[dict[str, object]],
) -> None:
    """Set the ordinal high-water mark from existing children after a runner restart."""
    import re

    key = (parent_session_id, agent_type)
    if key in _subagent_ordinal_counters:
        return
    pattern = re.compile(rf"^{re.escape(agent_type)}-(\d+)$")
    max_ordinal = 0
    for child in existing_children:
        session_name = child.get("session_name")
        if isinstance(session_name, str):
            m = pattern.match(session_name)
            if m:
                max_ordinal = max(max_ordinal, int(m.group(1)))
    _subagent_ordinal_counters[key] = max_ordinal


def new_subagent_work_id() -> str:
    """
    Mint the id of one sub-agent dispatch, e.g. ``"subagent_a1b2c3d4e5f6"``.

    :returns: A fresh dispatch id.
    """
    return f"subagent_{uuid.uuid4().hex[:12]}"


# Per-child locks serializing the classify+register step of an in-flight
# sub-agent send (see ``tool_dispatch._send_to_in_flight_child``), so two
# concurrent sends to one child can't install divergent work entries. Co-located
# with the work registries so it is torn down alongside them — otherwise a
# long-lived runner would accumulate one lock per steered child forever.
_in_flight_send_locks: dict[str, asyncio.Lock] = {}


def in_flight_send_lock(child_session_id: str) -> asyncio.Lock:
    """
    Return (creating on first use) the per-child in-flight-send lock.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The lock guarding that child's in-flight-send bookkeeping.
    """
    lock = _in_flight_send_locks.get(child_session_id)
    if lock is None:
        lock = asyncio.Lock()
        _in_flight_send_locks[child_session_id] = lock
    return lock


def register_subagent_work(
    *,
    parent_session_id: str,
    child_session_id: str,
    agent: str,
    title: str,
    wrapper_label: str | None = None,
    created_by: str | None = None,
    work_id: str | None = None,
    superside_chat: bool = False,
    group: str | None = None,
) -> _SubagentWorkEntry:
    """
    Register one running sub-agent dispatch.

    Re-registering the same child replaces the prior entry so a
    repeated send to an existing child represents the latest turn.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :param child_session_id: Child session id, e.g.
        ``"conv_child456"``.
    :param agent: Sub-agent name, e.g. ``"researcher"``.
    :param title: Sub-agent instance title, e.g. ``"auth"``.
    :param wrapper_label: Optional child ``omnigent.wrapper``
        label, e.g. ``"claude-code-native-ui"``.
    :param created_by: Human actor that dispatched this child turn, if
        known from the parent turn context.
    :param work_id: Dispatch id already stamped on the child session,
        e.g. ``"subagent_a1b2c3d4e5f6"``; ``None`` mints a new one.
    :param superside_chat: Whether the dispatching parent is a
        superside-chat session — see :class:`_SubagentWorkEntry`.
    :param group: The dispatching parent turn — see :class:`_SubagentWorkEntry`.
    :returns: The registered work entry.
    """
    prior = _subagent_work_by_child.get(child_session_id)
    if prior is not None:
        children = _subagent_work_by_parent.get(prior.parent_session_id)
        if children is not None:
            children.discard(child_session_id)
            if not children:
                _subagent_work_by_parent.pop(prior.parent_session_id, None)

    entry = _SubagentWorkEntry(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        work_id=work_id or new_subagent_work_id(),
        agent=agent,
        title=title,
        wrapper_label=wrapper_label,
        created_by=created_by,
        superside_chat=superside_chat,
        group=group,
    )
    _drained_delivered_subagent_children.discard(child_session_id)
    _subagent_work_by_child[child_session_id] = entry
    _subagent_work_by_parent.setdefault(parent_session_id, set()).add(child_session_id)
    return entry


def get_subagent_work(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Return registered sub-agent work by child session id.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The work entry, or ``None`` if the child is not tracked.
    """
    return _subagent_work_by_child.get(child_session_id)


def mark_subagent_work_started(child_session_id: str) -> _SubagentWorkEntry | None:
    """
    Promote a sub-agent dispatch from launch bookkeeping to real execution.

    ``sys_session_send`` creates the child session and registers work before
    the child harness has proven it started. The first child
    ``session.status:running`` / ``waiting`` edge is that proof.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :returns: The updated work entry, or ``None`` if the child is untracked.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return None
    if entry.status in {"launching", "waiting"}:
        entry.status = "running"
    return entry


def unregister_subagent_work(
    child_session_id: str,
    *,
    work_id: str | None = None,
    remember_drained_delivery: bool = False,
) -> None:
    """
    Remove sub-agent work tracking for a child session.

    Used when the child-message POST fails before a handle has been
    returned to the LLM.

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param work_id: Optional dispatch id guard. When provided, the
        current registry entry is removed only if it still belongs to
        that dispatch.
    :param remember_drained_delivery: Whether to remember a delivered
        entry as drained so duplicate terminal status reports for the
        same child are acknowledged as already delivered.
    :returns: None.
    """
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        return
    if work_id is not None and entry.work_id != work_id:
        return
    if remember_drained_delivery and entry.delivered:
        _drained_delivered_subagent_children.add(child_session_id)
    _subagent_work_by_child.pop(child_session_id, None)
    _in_flight_send_locks.pop(child_session_id, None)
    children = _subagent_work_by_parent.get(entry.parent_session_id)
    if children is None:
        return
    children.discard(child_session_id)
    if not children:
        _subagent_work_by_parent.pop(entry.parent_session_id, None)


def _subagent_children(session_id: str) -> list[str]:
    """Ids of the sub-agent sessions ``session_id`` started (still tracked)."""
    return [w.child_session_id for w in list_subagent_work(session_id)]


def unregister_subagent_work_for_session(session_id: str) -> None:
    """
    Remove sub-agent work associated with a deleted session.

    A deleted session can be either the child work handle itself or
    the parent that owns several child handles. Both indexes are
    cleaned so runner-local state cannot outlive the session tree.

    :param session_id: Session id being deleted, e.g.
        ``"conv_parent123"`` or ``"conv_child456"``.
    :returns: None.
    """
    forget_saved_files(session_id, _subagent_children)
    unregister_subagent_work(session_id)
    _drained_delivered_subagent_children.discard(session_id)
    _in_flight_send_locks.pop(session_id, None)
    for child_id in list(_subagent_work_by_parent.get(session_id, set())):
        _subagent_work_by_child.pop(child_id, None)
        _drained_delivered_subagent_children.discard(child_id)
        _in_flight_send_locks.pop(child_id, None)
    _subagent_work_by_parent.pop(session_id, None)


def list_subagent_work(parent_session_id: str) -> list[_SubagentWorkEntry]:
    """
    List sub-agent work registered by a parent session.

    :param parent_session_id: Parent session id, e.g.
        ``"conv_parent123"``.
    :returns: Work entries ordered by creation time.
    """
    child_ids = _subagent_work_by_parent.get(parent_session_id, set())
    entries = [
        entry
        for child_id in child_ids
        if (entry := _subagent_work_by_child.get(child_id)) is not None
    ]
    return sorted(entries, key=lambda entry: entry.created_at)


def subagent_group(parent_session_id: str) -> str | None:
    """The group a child dispatched now joins: the parent's running turn, if one runs."""
    return _live_response_id_ref.get(parent_session_id)


def subagent_group_waits(entry: _SubagentWorkEntry) -> bool:
    """Whether another child of ``entry``'s group still runs, so its parent is not woken yet."""
    return entry.group is not None and any(
        other.group == entry.group
        and other.child_session_id != entry.child_session_id
        and other.status in _SUBAGENT_LIVE_STATUSES
        for other in list_subagent_work(entry.parent_session_id)
    )


def undelivered_subagent_dispatch_id(labels: Mapping[str, object]) -> str | None:
    """
    Return the dispatch id of a child turn whose result the parent never drained.

    :param labels: Child session labels, e.g.
        ``{"omnigent.subagent.dispatch_id": "subagent_a1b2c3d4e5f6"}``.
    :returns: The dispatch id when the delivered-id receipt is missing or
        names an earlier turn; ``None`` for a drained turn, for a child
        created before dispatch ids were stamped, or for an ad-hoc Helper (its
        Result is never the parent's to collect; code posts it, ADR 0038).
    """
    if ADHOC_HELPER_LABEL_KEY in labels:
        return None
    dispatch_id = labels.get(SUBAGENT_DISPATCH_ID_LABEL_KEY)
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return None
    if labels.get(SUBAGENT_DELIVERED_ID_LABEL_KEY) == dispatch_id:
        return None
    return dispatch_id


#: How long an ``if_running`` sender waits for the running turn to offer its message to the
#: harness (the offer is a stdin or RPC write, answered at once); past it the message is refused.
_OFFER_VERDICT_TIMEOUT_S = 10.0

#: Buffered-body marker of an ``if_running`` message: it is only for the running turn and
#: never runs as a turn of its own.
_ONLY_RUNNING_TURN = "_only_running_turn"


def _is_steerable(content: object) -> bool:
    """Whether a harness steering this message in takes all of it: plain text that no feature
    needs a turn of its own for (``Feature.needs_own_turn``, e.g. an attached file)."""
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") in ("input_text", "text")
        for block in content
    ):
        texts = [str(block.get("text") or "") for block in content]
    else:
        return False
    return not message_needs_own_turn(texts)


class _SubagentRecoveryReadError(Exception):
    """A sessions API read needed by restart recovery returned a non-200."""


async def _get_recovery_page(
    server_client: httpx.AsyncClient, path: str, params: dict[str, str]
) -> Any:
    """
    Read one page of a sessions API listing for restart recovery.

    :param server_client: HTTP client connected to the Omnigent server.
    :param path: Sessions API path, e.g. ``"/v1/sessions/conv_p/child_sessions"``.
    :param params: Query parameters, e.g. ``{"limit": "1000"}``.
    :returns: The decoded JSON page.
    :raises _SubagentRecoveryReadError: When the server returns a non-200.
    """
    response = await server_client.get(path, params=params, timeout=10.0)
    if response.status_code != 200:
        raise _SubagentRecoveryReadError(f"{path} returned {response.status_code}")
    return response.json()


async def _list_child_sessions(
    server_client: httpx.AsyncClient, parent_id: str
) -> list[_JsonObject]:
    """
    Return every child-session summary of a parent, following pagination.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session id, e.g. ``"conv_parent123"``.
    :returns: Child summaries as returned by the sessions API.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    children: list[_JsonObject] = []
    params: dict[str, str] = {"limit": "1000"}
    while True:
        page = await _get_recovery_page(
            server_client, f"/v1/sessions/{parent_id}/child_sessions", params
        )
        children.extend(page.get("data", []))
        if not page.get("has_more") or not page.get("last_id"):
            return children
        params["after"] = page["last_id"]


async def _fetch_latest_assistant_text(
    server_client: httpx.AsyncClient, session_id: str
) -> str | None:
    """
    Return the newest assistant message text of a session, reading newest first.

    :param server_client: HTTP client connected to the Omnigent server.
    :param session_id: Session to read, e.g. ``"conv_child456"``.
    :returns: Joined text blocks of the newest assistant message (empty when
        that message carries no text, matching live delivery), or ``None``
        when the transcript holds no assistant message.
    :raises _SubagentRecoveryReadError: When a page read fails.
    """
    params: dict[str, str] = {"limit": "100", "order": "desc"}
    while True:
        page = await _get_recovery_page(server_client, f"/v1/sessions/{session_id}/items", params)
        for item in page.get("data", []):
            if item.get("type") != "message" or item.get("role") != "assistant":
                continue
            return "\n".join(
                block["text"]
                for block in item.get("content", [])
                if block.get("type") in {"output_text", "text"} and block.get("text")
            )
        if not page.get("has_more") or not page.get("last_id"):
            return None
        params["after"] = page["last_id"]


async def _recover_subagent_results_from_server(
    *,
    server_client: httpx.AsyncClient,
    parent_id: str,
    schedule_wake: Callable[[_SubagentWorkEntry], None],
) -> None:
    """
    Re-queue terminal child results whose delivery receipt is missing.

    A child turn is stamped with a dispatch id when it is sent, and the
    parent's ``sys_read_inbox`` drain writes that id back as the delivered
    id. A terminal child whose two ids differ was never drained, so its
    result is rebuilt from the child transcript and queued again under the
    same dispatch id, letting the eventual drain close the loop.

    :param server_client: HTTP client connected to the Omnigent server.
    :param parent_id: Parent session whose inbox was recreated, e.g.
        ``"conv_parent123"``.
    :param schedule_wake: Callback that posts the parent wake notice.
    :raises _SubagentRecoveryReadError: When a server read returns a
        non-200; the caller retries on the next drain.
    """
    # Wakes go out after the scan, once every child of a group is registered, so a recovered
    # Result waits for a sibling that still runs, as it did before the restart.
    recovered: list[_SubagentWorkEntry] = []
    for child in await _list_child_sessions(server_client, parent_id):
        child_id = child.get("id")
        status = child.get("current_task_status")
        if not isinstance(child_id, str) or not isinstance(status, str):
            continue
        error = child.get("last_task_error")
        interrupted = status == "in_progress" or (
            status == "failed"
            and isinstance(error, dict)
            and error.get("code") in {"runner_disconnected", "runner_failed_to_start"}
        )
        if status not in _SUBAGENT_TERMINAL_STATUSES and not interrupted:
            continue
        existing = get_subagent_work(child_id)
        if (existing is not None and existing.status != "waiting") or (
            child_id in _drained_delivered_subagent_children
        ):
            continue
        labels = child.get("labels")
        dispatch_id = undelivered_subagent_dispatch_id(labels if isinstance(labels, dict) else {})
        if dispatch_id is None or (existing is not None and existing.work_id != dispatch_id):
            continue
        output: str | None = None
        if status == "failed":
            error = child.get("last_task_error")
            message = error.get("message") if isinstance(error, dict) else None
            output = message if isinstance(message, str) else None
        elif not interrupted:
            output = await _fetch_latest_assistant_text(server_client, child_id)
        # A forwarded completion or newer dispatch may arrive during the history read.
        if (
            get_subagent_work(child_id) is not existing
            or (existing is not None and existing.status != "waiting")
            or child_id in _drained_delivered_subagent_children
        ):
            continue
        labels = labels if isinstance(labels, dict) else {}
        group = labels.get(SUBAGENT_GROUP_LABEL_KEY)
        entry = existing or register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=child_id,
            agent=str(child.get("tool") or child.get("agent_name") or "sub-agent"),
            title=str(child.get("session_name") or ""),
            work_id=dispatch_id,
            # The child inherited its parent's mode, so its Result is inlined as before.
            superside_chat=is_superside_chat(labels),
            group=group if isinstance(group, str) and group else None,
        )
        if interrupted:
            # This dispatch already existed; a local launch timeout cannot judge it.
            entry.status = "waiting"
            continue
        ack = mark_subagent_work_terminal(child_id, status=status, output=output)
        if ack.delivered_now:
            recovered.append(entry)
    for entry in recovered:
        schedule_wake(entry)


def mark_subagent_work_terminal(
    child_session_id: str,
    *,
    status: str,
    output: str | None,
    only_if_work_id: str | None = None,
) -> _SubagentDeliveryAck:
    """
    Mark a sub-agent dispatch terminal and notify the parent inbox.

    A delivered terminal status is final: it is not overturned by a later
    report for the same child (the ``failed``-over-``completed`` edge-race
    exception aside). Distinguishing a confirmed completion from a bare
    quiescence idle is the caller's job (the events route only reports
    ``completed`` for a confirmed turn-end or a legacy harness).

    :param child_session_id: Child session id, e.g. ``"conv_child456"``.
    :param status: Terminal status: ``"completed"``, ``"failed"``, or
        ``"cancelled"``.
    :param output: Child output or error text. ``None`` means the
        completion had no assistant text to deliver.
        If an earlier terminal report could not be delivered, a later
        report for the same child replaces the undelivered status and
        output before retrying parent inbox delivery.
    :param only_if_work_id: When set, apply this report only if the current
        entry is still that dispatch. A delayed op (an interrupt's grace-timer
        cancel) bound to the dispatch it was raised for is dropped when a newer
        send has replaced the entry, so an old timer never cancels new work.
    :returns: Delivery acknowledgement for this terminal report.
    :raises ValueError: If ``status`` is not terminal.
    """
    if status not in _SUBAGENT_TERMINAL_STATUSES:
        raise ValueError(
            f"sub-agent terminal status must be one of "
            f"{sorted(_SUBAGENT_TERMINAL_STATUSES)}; got {status!r}"
        )
    entry = _subagent_work_by_child.get(child_session_id)
    if entry is None:
        if child_session_id in _drained_delivered_subagent_children:
            return _SubagentDeliveryAck(
                entry=None,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        return _SubagentDeliveryAck(
            entry=None,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_UNTRACKED,
        )
    if only_if_work_id is not None and entry.work_id != only_if_work_id:
        # A delayed op raised for an earlier dispatch: a newer send replaced the
        # entry on this reused child session. Drop it untouched so an old
        # interrupt's grace timer can never cancel the new dispatch.
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH,
        )
    if entry.status in _SUBAGENT_TERMINAL_STATUSES:
        # ``failed`` outranks ``completed``: a quiescence-derived ``completed``
        # (the watcher's ``idle`` edge) can be recorded — and delivered — before
        # the turn's real ``failed`` edge lands. The failure must replace it and
        # be re-delivered, or the parent is left believing the turn succeeded
        # and the error text is silently dropped. A parent may act on the false
        # success before the re-delivery arrives — that window is inherent to
        # the edge race; re-delivery is the mitigation, not a prevention.
        if status == "failed" and entry.status == "completed":
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
            entry.delivered = False
            return _deliver_subagent_completion(entry)
        if entry.delivered:
            return _SubagentDeliveryAck(
                entry=entry,
                delivered=True,
                delivered_now=False,
                reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
            )
        # A late stop_session-driven "cancelled" must not downgrade an
        # already-recorded "completed"/"failed" still awaiting delivery, and a
        # trailing quiescence "completed" must not launder a recorded "failed".
        keep_recorded = (status == "cancelled" and entry.status != "cancelled") or (
            status == "completed" and entry.status == "failed"
        )
        if not keep_recorded:
            entry.status = status
            entry.output = output
            entry.completed_at = time.time()
        return _deliver_subagent_completion(entry)
    entry.status = status
    entry.output = output
    entry.completed_at = time.time()
    return _deliver_subagent_completion(entry)


def _deliver_subagent_completion(entry: _SubagentWorkEntry) -> _SubagentDeliveryAck:
    """
    Push a terminal sub-agent payload into the parent session inbox.

    :param entry: Terminal sub-agent work entry to deliver.
    :returns: Delivery acknowledgement describing whether the payload is
        confirmed in the parent inbox.
    """
    if entry.delivered:
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=True,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_ALREADY_DELIVERED,
        )
    inbox = _session_inboxes_ref.get(entry.parent_session_id)
    if inbox is None:
        _logger.warning(
            "Sub-agent work completed but parent inbox is missing; parent=%s child=%s",
            entry.parent_session_id,
            entry.child_session_id,
        )
        return _SubagentDeliveryAck(
            entry=entry,
            delivered=False,
            delivered_now=False,
            reason=_SUBAGENT_DELIVERY_MISSING_PARENT_INBOX,
        )
    output = entry.output
    if output is None:
        # Only a completion needs the explicit no-output marker; a cancelled
        # dispatch legitimately has nothing to report.
        output = (
            "[System: sub-agent completed with no output]" if entry.status == "completed" else ""
        )
    inbox.put_nowait(
        {
            "type": "sub_agent",
            "work_id": entry.work_id,
            "task_id": entry.child_session_id,
            "handle_id": entry.child_session_id,
            "conversation_id": entry.child_session_id,
            "tool_name": entry.agent,
            "agent": entry.agent,
            "title": entry.title,
            "status": entry.status,
            "output": output,
        }
    )
    entry.delivered = True
    return _SubagentDeliveryAck(
        entry=entry,
        delivered=True,
        delivered_now=True,
        reason=_SUBAGENT_DELIVERY_DELIVERED,
    )


def reap_stalled_subagent_launches(
    *,
    now: float | None = None,
    timeout_s: float | None = None,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
) -> list[_SubagentWorkEntry]:
    """
    Fail sub-agent dispatches stuck in ``launching`` beyond the liveness budget.

    A dispatch with no running/waiting/terminal status acknowledgment can
    otherwise remain pending forever. Missing acknowledgment does not prove
    that the child process never started. Each reaped entry is
    marked ``failed`` and its failure is delivered to the parent inbox through
    ``mark_terminal``.

    :param now: Clock override for tests, e.g. ``time.time()``.
    :param timeout_s: Budget override for tests; defaults to
        :func:`resolve_subagent_launch_timeout_s`.
    :param mark_terminal: Terminal-delivery callback. Production passes the
        app's ``mark_subagent_terminal_and_wake`` seam so the reaped failure
        also schedules the parent wake POST — the sole signal that rouses an
        idle parent to drain its inbox. Defaults to the inbox-only
        :func:`mark_subagent_work_terminal`.
    :returns: The entries that were failed by this sweep.
    """
    budget = resolve_subagent_launch_timeout_s() if timeout_s is None else timeout_s
    if budget <= 0:
        return []
    deliver = mark_subagent_work_terminal if mark_terminal is None else mark_terminal
    current = time.time() if now is None else now
    reaped: list[_SubagentWorkEntry] = []
    for entry in list(_subagent_work_by_child.values()):
        if entry.status != "launching":
            continue
        if current - entry.created_at < budget:
            continue
        _logger.warning(
            "Sub-agent dispatch stuck in launching for %.0fs; failing it: parent=%s child=%s",
            current - entry.created_at,
            entry.parent_session_id,
            entry.child_session_id,
        )
        deliver(
            entry.child_session_id,
            status="failed",
            output=(
                f"Error: no start acknowledgment for sub-agent {entry.agent!r} "
                f"title {entry.title!r} within {budget:.0f}s of dispatch. "
                "The child may still be running; inspect its session before retrying."
            ),
        )
        reaped.append(entry)
    return reaped


async def run_subagent_launch_reaper(
    *,
    interval_s: float = SUBAGENT_LAUNCH_REAP_INTERVAL_S,
    mark_terminal: MarkSubagentTerminalAndWake | None = None,
    reconcile_pending: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """
    Periodically sweep for sub-agent dispatches wedged in ``launching``.

    Runs until cancelled; started by the runner entrypoint alongside the
    process manager. Sweep errors are logged and never end the loop.

    :param interval_s: Seconds between sweeps, e.g. ``30.0``.
    :param mark_terminal: Terminal-delivery callback forwarded to each sweep;
        the entrypoint passes the app's wake-scheduling seam so a reaped
        failure wakes the parent, not just its inbox.
    :param reconcile_pending: Refresh recovered work awaiting remote completion.
    :returns: None.
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            reap_stalled_subagent_launches(mark_terminal=mark_terminal)
            if reconcile_pending is not None:
                await reconcile_pending()
        except Exception:
            _logger.warning("sub-agent launch reaper sweep failed", exc_info=True)


async def _wake_retry_sleep(seconds: float) -> None:
    """
    Sleep between sub-agent wake-POST retries.

    Indirection point so tests can stub the backoff without clobbering the
    process-wide ``asyncio.sleep`` (the ``no-global-asyncio-patch`` lint
    hook bans patching the module singleton).

    :param seconds: Seconds to wait before the next retry, e.g. ``0.5``.
    :returns: None.
    """
    await asyncio.sleep(seconds)


def _wake_post_is_retryable(exc: httpx.HTTPError) -> bool:
    """
    Return whether a failed wake POST should be retried.

    Transport-level failures (connect/read errors, timeouts) are always
    retryable. A non-2xx response surfaces as :class:`httpx.HTTPStatusError`:
    5xx statuses are transient (notably the 503 ``RUNNER_UNAVAILABLE`` that
    Omnigent returns while the parent's runner tunnel is reconnecting), as
    are a few 4xx codes; every other 4xx is a permanent client-side rejection
    that retrying cannot fix.

    :param exc: HTTP error raised by the wake POST or ``raise_for_status``,
        e.g. an ``httpx.HTTPStatusError`` wrapping a 503 response.
    :returns: ``True`` if a bounded retry is worthwhile, else ``False``.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        # Transport failure — the POST may never have reached Omnigent.
        return True
    status_code = exc.response.status_code
    if status_code >= 500:
        return True
    return status_code in _WAKE_POST_TRANSIENT_4XX


async def _deliver_subagent_wake_post(
    server_client: httpx.AsyncClient,
    parent_id: str,
    notice: str,
    *,
    created_by: str | None = None,
    helper_id: str | None = None,
) -> bool:
    """
    POST a sub-agent wake notice with a bounded retry on transient failure.

    httpx does not raise on a non-2xx response, so a real 503
    ``RUNNER_UNAVAILABLE`` JSON response (routine while the parent's runner
    tunnel reconnects) would otherwise be treated as a successful delivery.
    This calls ``raise_for_status`` to turn any non-2xx into a failure and
    retries transient failures up to :data:`_WAKE_POST_MAX_ATTEMPTS` with
    exponential backoff, because the wake is the sole delivery signal for
    the last child of a fan-out. Permanent 4xx rejections stop immediately.

    :param server_client: Omnigent HTTP client for the runner subprocess.
    :param parent_id: Parent session to wake, e.g. ``"conv_parent123"``.
    :param notice: The ``[System: ...]`` notice text to inject.
    :param created_by: Human actor that dispatched the completed child
        turn, if known.
    :param helper_id: The child the wake is about; the notice joins its Round (ADR 0029).
    :returns: ``True`` if a 2xx was confirmed, ``False`` if every attempt
        failed (transport error, timeout, or non-2xx response).
    """
    attribution_created_by = created_by
    for attempt in range(1, _WAKE_POST_MAX_ATTEMPTS + 1):
        try:
            resp = await server_client.post(
                f"/v1/sessions/{parent_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "is_system_notice": True,
                        "content": [{"type": "input_text", "text": notice}],
                    },
                    **(
                        {"created_by": attribution_created_by}
                        if attribution_created_by is not None
                        else {}
                    ),
                    **({"context": {"helper_id": helper_id}} if helper_id else {}),
                },
                # The server gates this injected wake at the parent's REQUEST
                # phase, which can PARK on a human ASK (e.g. session_cost_budget)
                # for up to the deciding policy's ``ask_timeout`` (default one
                # day). A 30s read budget severed that park after 30s → the
                # TimeoutError below retried → each retry re-posted the notice
                # and parked ANOTHER gate → duplicate approval cards, and the
                # gate never cleanly blocked. Hold the read budget at one day so
                # this POST waits for the real verdict (one held connection, one
                # card); fast connect so an unreachable parent runner still
                # fails out into the bounded retry below.
                timeout=_ASK_GATE_DELIVERY_TIMEOUT,
            )
            # Treat a non-2xx RESPONSE (e.g. a genuine 503 JSONResponse) as a
            # failure — httpx does not raise on status by itself.
            resp.raise_for_status()
            return True
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if (
                attribution_created_by is not None
                and isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code == 403
            ):
                _logger.debug(
                    "Sub-agent wake POST attribution rejected for parent=%s; "
                    "retrying without actor",
                    parent_id,
                    extra={"session_id": runner_primary_session_id()},
                )
                attribution_created_by = None
                continue
            last_attempt = attempt >= _WAKE_POST_MAX_ATTEMPTS
            retryable = isinstance(exc, asyncio.TimeoutError) or _wake_post_is_retryable(exc)
            _logger.debug(
                "Sub-agent wake POST attempt %d/%d for parent=%s failed (retryable=%s): %r",
                attempt,
                _WAKE_POST_MAX_ATTEMPTS,
                parent_id,
                retryable,
                exc,
                extra={"session_id": runner_primary_session_id()},
            )
            if last_attempt or not retryable:
                return False
            delay_s = min(
                _WAKE_POST_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)),
                _WAKE_POST_RETRY_MAX_DELAY_S,
            )
            await _wake_retry_sleep(delay_s)
    return False


def _subagent_delivery_not_confirmed_response(
    ack: _SubagentDeliveryAck,
    *,
    is_runner_known_subagent: bool,
) -> JSONResponse | None:
    """
    Build a 503 response when a known sub-agent result was not delivered.

    Top-level sessions also post terminal status but have no parent inbox, so
    an untracked status remains a no-op unless the runner knows this session
    was created as a sub-agent. For known sub-agents, Omnigent must not receive a
    2xx acknowledgement unless the terminal payload is confirmed in the
    parent's inbox — except a tracked entry whose parent is itself a
    sub-agent, which ``post_session_events`` acknowledges before calling here;
    the entry is retained and delivered only if this runner ever creates that
    parent's inbox.

    :param ack: Delivery acknowledgement returned by
        ``mark_subagent_work_terminal``.
    :param is_runner_known_subagent: Whether runner session state identifies
        the status sender as a sub-agent child.
    :returns: A 503 JSON response when delivery is not confirmed, or ``None``
        when the status can be acknowledged.
    """
    if ack.delivered:
        return None
    if ack.reason == _SUBAGENT_DELIVERY_SUPERSEDED_DISPATCH:
        # A delayed op intentionally dropped because a newer send replaced the
        # dispatch: acknowledge so the forwarder does not retry, and never touch
        # the newer dispatch. Not a delivery failure.
        return None
    if ack.entry is None and not is_runner_known_subagent:
        return None
    reason = _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY if ack.entry is None else ack.reason
    detail_by_reason = {
        _SUBAGENT_DELIVERY_MISSING_WORK_ENTRY: (
            "Sub-agent terminal status arrived, but the runner has no "
            "tracked work entry to deliver to the parent inbox."
        ),
        _SUBAGENT_DELIVERY_MISSING_PARENT_INBOX: (
            "Sub-agent terminal status arrived, but the parent inbox is missing on this runner."
        ),
    }
    detail = detail_by_reason[reason]
    return JSONResponse(
        status_code=503,
        content={
            "error": "subagent_delivery_not_confirmed",
            "reason": reason,
            "detail": detail,
        },
    )


def _format_subagent_wake_notice(*, agent: str, title: str, status: str, pending: int) -> str:
    """
    Build the framework notice that wakes a parent after a child finishes.

    :param agent: Sub-agent name from the parent spec, e.g. ``"researcher"``.
    :param title: Child instance title supplied at dispatch, e.g. ``"auth"``.
    :param status: Terminal child status, e.g. ``"completed"``, ``"failed"``,
        or ``"cancelled"``.
    :param pending: Number of undrained items in the parent inbox, e.g. ``3``.
    :returns: A ``[System: ...]`` notice string, e.g. ``"[System: sub-agent
        researcher/auth finished (completed) — 1 result waiting in inbox. Call
        sys_read_inbox to collect.]"``.
    """
    noun = "result" if pending == 1 else "results"
    return (
        f"[System: sub-agent {agent}/{title} finished ({status}) — "
        f"{pending} {noun} waiting in inbox. Call sys_read_inbox to collect.]"
    )


# Max length of a child message preview mirrored to the parent stream.
# Matches the server-side ``_latest_message_preview`` truncation so the
# live runner-pushed preview and the snapshot preview look the same.
_CHILD_PREVIEW_MAX_CHARS = 150


@dataclasses.dataclass
class _ChildParentMeta:
    """Fan-out metadata for one child sub-agent session.

    Lets the runner mirror a child's status/preview deltas onto the
    PARENT's SSE stream — the child's own relay isn't running when only
    the parent is viewed, and the runner runs the child turn (affinity).

    :param parent_id: Parent session id whose stream receives the deltas.
    :param title: Child title ``"{tool}:{session_name}"`` — carried in
        status deltas so even a cold update has a display name.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    :param last_busy: Last busy value fanned out, used to coalesce
        duplicate status deltas. ``None`` until first publish.
    :param last_task_status: Last child-rail task status fanned out, e.g.
        ``"completed"``. Tracked separately so ``idle`` → ``failed`` emits
        even though both states are non-busy.
    :param last_error: Last child failure detail fanned out, used to emit a
        new parent update when only the error changes, and to clear stale
        errors on a later running/waiting edge.
    """

    parent_id: str
    title: str
    tool: str
    session_name: str
    last_busy: bool | None = None
    last_task_status: str | None = None
    last_error: tuple[str, str] | None = None


# child_session_id -> :class:`_ChildParentMeta`. Populated at spawn (see
# tool_dispatch._execute_subagent_tool), dropped when the child ends.
_child_session_parents: dict[str, _ChildParentMeta] = {}


def register_child_session(
    child_session_id: str,
    *,
    parent_session_id: str,
    title: str,
    tool: str,
    session_name: str,
) -> None:
    """
    Record a child→parent mapping for SSE status/preview fan-out.

    :param child_session_id: Child session id, e.g. ``"conv_child123"``.
    :param parent_session_id: Parent session id whose stream should
        receive the child's deltas, e.g. ``"conv_parent987"``.
    :param title: Child title, ``"{tool}:{session_name}"``.
    :param tool: Sub-agent type, e.g. ``"researcher"``.
    :param session_name: Sub-agent instance name, e.g. ``"auth"``.
    """
    _child_session_parents[child_session_id] = _ChildParentMeta(
        parent_id=parent_session_id,
        title=title,
        tool=tool,
        session_name=session_name,
    )


def unregister_child_session(child_session_id: str) -> None:
    """
    Drop a child→parent mapping when the child session ends.

    :param child_session_id: Child session id to forget.
    """
    _child_session_parents.pop(child_session_id, None)


def session_status_event(
    status: str, blocked_on: str | None = None, *, paused: bool = False
) -> dict[str, object]:
    """A ``session.status`` event: ``blocked_on`` when the turn waits on something, and
    ``paused: true`` while a paused Helper is held (its ``superchat.paused`` label is set), so a
    client reads the flag, never the note's words."""
    event: dict[str, object] = {"type": "session.status", "status": status}
    if blocked_on is not None:
        event["blocked_on"] = blocked_on
    if paused:
        event["paused"] = True
    return event


def _session_status_to_task_status(status: object) -> str | None:
    """
    Map a ``session.status`` value to a child summary ``current_task_status``.

    The two vocabularies differ (session status vs. task status); this
    keeps the child rail's status text roughly in sync as ``busy`` flips.

    :param status: A ``session.status`` value, e.g. ``"running"``.
    :returns: ``"launching"`` / ``"in_progress"`` / ``"completed"`` /
        ``"failed"`` / ``"cancelled"``, or ``None`` for an unrecognized status (caller
        omits the field).
    """
    if status == "launching":
        return "launching"
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status in ("failed", "cancelled"):
        return str(status)
    return None


def _normalize_turn_error(error: Mapping[str, object]) -> dict[str, str]:
    """
    Coerce a turn-failure ``error`` dict into a ``{code, message}`` shape.

    The ``error`` dicts passed to :func:`_on_proxy_stream_end` vary by
    call site: most carry ``{"message": "..."}`` (and sometimes
    ``"type"``), but a few carry only ``{"status": <http status>}``.
    The wire ``SessionStatusEvent.error`` field (``ErrorDetail``)
    requires both ``code`` and ``message``, so this normalizes every
    shape into one the schema accepts, never raising on a missing key.
    The result is what gets published on the ``failed`` status event
    and ultimately rendered as the REPL's terminal error line.

    A harness ``response.failed`` error already names its failure in ``code``;
    that code is kept so the failed status edge and the persisted error item
    describe one failure the same way (the web UI de-duplicates them by code
    and message). ``type`` is the legacy spelling; ``runner_error`` is the
    fallback for setup failures that carry neither.

    :param error: Raw error dict from a ``_on_proxy_stream_end`` call,
        e.g. ``{"message": "turn setup failed: ..."}``,
        ``{"code": "agent_startup_pending", "message": "..."}`` or
        ``{"status": 502}``.
    :returns: A dict with ``code`` and ``message`` string keys, e.g.
        ``{"code": "runner_error", "message": "turn setup failed: ..."}``.
        Falls back to a generic message when none is present.
    """
    raw_message = error.get("message")
    if isinstance(raw_message, str) and raw_message.strip():
        message = raw_message
    elif "status" in error:
        message = f"turn failed (status {error['status']})"
    else:
        message = "turn failed"
    code = "runner_error"
    for key in ("code", "type"):
        raw_code = error.get(key)
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
            break
    return {"code": code, "message": message}


def _harness_error_response_error(response: object) -> dict[str, str]:
    """
    Convert a non-streaming harness error response into a turn failure.

    For example, ``{"error": "harness_spawn_failed", "detail": "See runner log"}``
    becomes ``{"message": "harness_spawn_failed: See runner log"}``. Missing or
    malformed bodies fall back to a short raw-body preview or a generic message.

    :param response: Response returned instead of a ``StreamingResponse``.
    :returns: An error dict suitable for :func:`_on_proxy_stream_end`.
    """
    text = ""
    # A stub response without a body, a body that is not bytes, or bytes
    # that are not UTF-8 all fall back to the generic message below.
    with contextlib.suppress(UnicodeDecodeError, AttributeError, TypeError):
        text = bytes(cast(Any, response).body).decode("utf-8")
    payload: object = None
    with contextlib.suppress(ValueError):
        payload = json.loads(text)
    if isinstance(payload, dict):
        raw_detail = payload.get("detail")
        raw_code = payload.get("error")
        detail = raw_detail.strip() if isinstance(raw_detail, str) else ""
        code = raw_code.strip() if isinstance(raw_code, str) else ""
        if code and detail:
            return {"message": f"{code}: {detail}"}
        if detail:
            return {"message": detail}
        if code:
            return {"message": code}
    return {"message": text.strip()[:200] or "harness returned error response"}


def _truncate_child_preview(text: str) -> str:
    """
    Truncate a child message preview to the cap with an ellipsis.

    Matches the server-side ``_latest_message_preview`` truncation so the
    live runner-pushed preview and the snapshot preview look the same.

    :param text: The child's latest assistant reply text.
    :returns: ``text`` truncated to :data:`_CHILD_PREVIEW_MAX_CHARS` with
        a trailing ellipsis when longer, else ``text`` unchanged.
    """
    if len(text) > _CHILD_PREVIEW_MAX_CHARS:
        return text[:_CHILD_PREVIEW_MAX_CHARS].rstrip() + "…"
    return text


# Per-session timer registry. Keyed by session_id → {timer_id → Task}.
_session_timers: dict[str, dict[str, asyncio.Task[None]]] = {}


def _has_live_async_tasks(
    session_async_tasks: Mapping[
        str,
        Mapping[str, tuple[asyncio.Task[object], asyncio.Event]],
    ],
) -> bool:
    """Return whether an async-tool registry contains unfinished work."""
    return any(
        not task.done()
        for handles in session_async_tasks.values()
        for task, _cancel_event in handles.values()
    )


def register_timer(
    session_id: str,
    timer_id: str,
    task: asyncio.Task[None],
) -> None:
    """
    Register an active timer task for a session.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer identifier, e.g. ``"timer_a1b2..."``.
    :param task: The asyncio.Task running the timer loop.
    """
    _session_timers.setdefault(session_id, {})[timer_id] = task


def unregister_timer(session_id: str, timer_id: str) -> None:
    """
    Remove a timer from the registry on completion or cancel.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer to remove.
    """
    timers = _session_timers.get(session_id)
    if timers is not None:
        timers.pop(timer_id, None)


def cancel_timer(session_id: str, timer_id: str) -> bool:
    """
    Cancel a timer by ID.

    :param session_id: Session the timer belongs to.
    :param timer_id: Timer to cancel.
    :returns: True if found and cancelled, False otherwise.
    """
    timers = _session_timers.get(session_id)
    if timers is None:
        return False
    task = timers.pop(timer_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


# Module-level ref to _session_agent_ids. Populated inside
# create_runner_app; read by tool_dispatch._execute_subagent_tool.
_session_agent_ids_ref: dict[str, str] = {}

# Module-level ref to _session_histories. Populated inside
# create_runner_app; used by tests to inspect in-memory history.
_session_histories_ref: dict[str, list[_JsonObject]] = {}

# Module-level ref to _session_event_queues. Populated inside
# create_runner_app; used by tests to inspect the queue an SSE
# subscriber would have read (events published synchronously by
# ``_publish_event`` are visible by the time the producer's await
# call returns, so tests don't need to subscribe to the HTTP
# ``/stream`` endpoint just to assert on emitted events).
_session_event_queues_ref: dict[str, asyncio.Queue[_JsonObject | None]] = {}

# Module-level ref to _session_inboxes. Populated inside create_runner_app;
# used by the sub-agent work registry to deliver completions to the parent.
_session_inboxes_ref: dict[str, asyncio.Queue[_JsonObject]] = {}

# Module-level ref to _live_response_id (conversation id -> its running turn's
# response id), so a sub-agent dispatch can name the parent turn it joins.
_live_response_id_ref: dict[str, str] = {}


def get_session_agent_id(session_id: str) -> str | None:
    """
    Return the durable agent_id for a session.

    :param session_id: Session/conversation ID, e.g.
        ``"conv_abc123"``.
    :returns: The agent_id, or ``None`` if not found.
    """
    return _session_agent_ids_ref.get(session_id)


# Repeated invocations share a filesystem scan; installed skills become
# resolvable after at most one minute without restarting the runner.
_SESSION_SKILLS_CACHE_TTL_SECONDS = 60.0
_SESSION_INIT_ENVELOPE_TTL_SECONDS = 60.0


class _BodyRequest:
    """Minimal stand-in for a Starlette ``Request`` exposing only ``json()``.

    Lets internal callers reuse a route handler that consumes the request
    solely for its JSON body (e.g. ``create_session_terminal``) without
    constructing a real ASGI ``Request``. Not a general Request substitute.
    """

    def __init__(self, body: _JsonObject) -> None:
        self._body = body

    async def json(self) -> _JsonObject:
        return self._body


#: Bare (unprefixed) id patterns recognized as OpenAI's own naming, for
#: :func:`resolve_model_provider_family`. Deliberately narrower than "has no
#: '/' prefix" — a third-party provider's own bare id (Groq, Gemini, xAI,
#: ...) must not fall through to this and get treated as OpenAI.
_OPENAI_BARE_ID_PREFIXES: tuple[str, ...] = (
    "gpt",
    "chatgpt",
    "o1",
    "o3",
    "o4",
    "text-embedding",
    "davinci",
    "curie",
    "babbage",
    "ada",
    "dall-e",
    "whisper",
    "tts",
    "omni-moderation",
)


def resolve_model_provider_family(model: str) -> str | None:
    """Known-provider family for a rollover-summarizer *model* id, or ``None``.

    Recognizes Anthropic ids by the ``"claude"`` token appearing anywhere
    (case-insensitive) — covering the plain ``"claude-*"`` form, the
    ``"anthropic/*"`` provider-prefixed form, Bedrock-style ids
    (``"us.anthropic.claude-..."``, ``"anthropic.claude-..."``), and
    Databricks-hosted Claude (``"databricks-claude-..."``) — plus any
    explicit ``"anthropic/"`` prefix. Recognizes OpenAI ids by an explicit
    ``"openai/"`` prefix or OpenAI's own bare-id naming
    (:data:`_OPENAI_BARE_ID_PREFIXES`: gpt/o-series/dall-e/whisper/...).

    A model id matching neither — including a bare id from some OTHER
    provider (Groq, Gemini, xAI, a self-hosted model) — is NOT guessed as
    OpenAI: this connection-resolution subsystem only ever has an Anthropic
    or an OpenAI slot to offer, so callers must treat ``None`` as "don't
    know which of the two (if either) this is" and refuse to pick one
    rather than silently handing a third provider's call OpenAI credentials.

    :param model: LLM model string in any vocabulary, e.g.
        ``"claude-haiku-4-5"``, ``"us.anthropic.claude-3-5-sonnet..."``,
        ``"databricks-claude-opus-4-8"``, ``"gpt-4o"``, ``"o3-mini"``.
    :returns: ``"anthropic"``, ``"openai"``, or ``None`` when unrecognized.
    """
    lower = model.lower()
    if "claude" in lower or lower.startswith("anthropic/"):
        return "anthropic"
    if lower.startswith("openai/") or lower.startswith(_OPENAI_BARE_ID_PREFIXES):
        return "openai"
    return None


def normalize_anthropic_base_url(base_url: str) -> str:
    """Append the missing API version to a bare ``api.anthropic.com`` base URL.

    Provider config (``~/.omnigent/config.yaml``) stores the Claude SDK form
    of the Anthropic base URL, which has no ``/v1`` — the LLM client's
    Anthropic adapter needs the versioned form. Matched by parsed host
    (case-insensitive), not a literal string, so this also normalizes a
    trailing slash or any casing of the host; a base URL that already names
    a path (a version, or a proxy's own route) is left alone, and any other
    host (a self-hosted gateway, a proxy under a different domain) is
    returned unchanged.

    :param base_url: The configured Anthropic base URL, e.g.
        ``"https://api.anthropic.com"``, ``"HTTPS://API.ANTHROPIC.COM/"``.
    :returns: *base_url* with ``/v1`` appended when it names the bare
        Anthropic host with no path; otherwise *base_url* unchanged.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(base_url)
    if parts.hostname != "api.anthropic.com":
        return base_url
    if parts.path not in ("", "/"):
        return base_url
    return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))


def _notify_file_written(relative_path: str, env: Any) -> None:
    """Tell the file search a file was written through the runner (an upload), if it runs here."""
    from omnigent.superchat.knowledge.runtime import notify_written

    try:
        root = getattr(env, "cwd", None)
        notify_written(Path(root) / relative_path if root else relative_path)
    except Exception:
        _logger.debug("knowledge: write notification failed", exc_info=True)


def create_runner_app(
    *,
    process_manager: HarnessProcessManager | None = None,
    spec_resolver: SpecResolver | None = None,
    server_client: httpx.AsyncClient,
    terminal_registry: TerminalRegistry | None = None,
    resource_registry: SessionResourceRegistry | None = None,
    runner_workspace: Path | None = None,
    per_session_workspace: bool = True,
    mcp_manager: RunnerMcpManager | None = None,
    auth_token: str | None = None,
) -> FastAPI:
    """Build a fresh runner FastAPI app.

    :param process_manager: Pre-started HarnessProcessManager.
        ``None`` → scaffold mode (501 stubs).
    :param spec_resolver: Async callback ``(agent_id) -> AgentSpec | None``.
        For in-process: wraps the server's agent cache.
        For out-of-process: wraps HTTP fetch to GET /v1/agents/{id}/contents.
        ``None`` → runner falls back to body-supplied hints (test path).
    :param server_client: httpx.AsyncClient pointed at the AP
        server's public API. Used by the runner for
        elicitation/approval forwarding.
        In-process: pointed at the Omnigent ASGI app.
        Out-of-process: pointed at the server's HTTP URL.
    :param terminal_registry: TerminalRegistry instance for
        runner-local terminal tool dispatch (Phase 2).
        ``None`` → terminal tools relay upstream.
    :param runner_workspace: Optional local workspace path passed
        by the CLI when the runner owns filesystem tools for a
        remote app server session.
    :param per_session_workspace: ``True`` (default) isolates each
        session under a subdirectory of *runner_workspace*.
        Single-user CLI runners pass ``False`` so the agent sees the
        project root. No effect when *runner_workspace* is ``None``.
    :param mcp_manager: Optional :class:`RunnerMcpManager` owning
        this runner's MCP pool. ``None`` skips MCP injection
        (test path).
    :param auth_token: Optional bearer token that callers must
        present in the ``Authorization`` header.  When set, every
        request except ``GET /health`` is rejected with 401 if
        the token is missing or wrong.  ``None``
        disables auth (in-process / test path).
    """
    import hmac

    app = FastAPI(title="omnigent-runner")

    from omnigent.runner.logging_context import RunnerLogContextMiddleware

    app.add_middleware(RunnerLogContextMiddleware)
    mcp_execution_registry = McpExecutionRegistry()
    app.state.mcp_execution_registry = mcp_execution_registry

    # Set as soon as SIGINT/SIGTERM is handled, so a required terminal that
    # dies with the runner's process group is not reported as a crash.
    _shutting_down = asyncio.Event()
    app.state.shutting_down = _shutting_down

    from omnigent.runtime import telemetry

    telemetry.instrument_fastapi_app(app)

    if auth_token is not None:
        _expected_token = auth_token

        @app.middleware("http")
        async def _runner_auth_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            if request.url.path == "/health":
                return await call_next(request)
            client = request.scope.get("client")
            if client is not None and client[0] == "tunnel":
                return await call_next(request)
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                provided = auth_header[7:]
            else:
                provided = ""
            if not provided or not hmac.compare_digest(provided, _expected_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or missing runner auth token"},
                )
            return await call_next(request)

    if terminal_registry is not None:
        from omnigent.runtime import _globals as _rt_globals

        _rt_globals._terminal_registry = terminal_registry

    _version_cache: dict[str, int] = {}  # conversation_id → last seen agent_version
    _spec_cache: dict[str, _SpecEntry] = {}  # agent_id → cached AgentSpec for terminal tools
    _resp_to_conv: dict[str, str] = {}  # harness response_id → conversation_id
    _live_response_id = _live_response_id_ref
    app.state.live_response_id = _live_response_id
    # conv_id -> the response id of the turn that has streamed text. A tool call reaches its
    # handler through /mcp/execute, not this turn's stream, so the turn's words are read here.
    _turn_said: dict[str, str] = {}
    _session_start_cache: dict[str, float] = {}  # session_id → registered start time
    _session_spec_cache: dict[str, _SpecEntry | None] = {}  # session_id → session AgentSpec
    # session_id → the harness the session actually runs, when it differs from
    # the spec's. Smart Routing pins a routed child's harness on the
    # conversation and forwards it as ``harness_override``; without this record
    # every spec-derived read (native-vs-SDK checks above all) still answers
    # with the harness the spec declared, which a routed session is not on.
    _session_harness_overrides: dict[str, str] = {}
    _session_snapshot_cache: dict[str, _SessionSnapshot] = {}  # session_id → snapshot
    _session_snapshot_locks: dict[str, asyncio.Lock] = {}  # session_id → snapshot fetch lock
    _session_spec_locks: dict[str, asyncio.Lock] = {}  # session_id → spec resolution lock
    _session_init_tasks: dict[
        tuple[str, str, str | None, str | None], asyncio.Task[JSONResponse]
    ] = {}
    _recovery_turn_ids: dict[str, set[str]] = {}
    # session_id → id of the persisted user item a history-resume recovery
    # turn (started by session init) is answering. The server's forward of
    # that same item (``persisted_item_id``) is then already answered and is
    # dropped instead of buffered into a second turn — the double reply after
    # a cold start, when the tunnel-reconnect init races the message forward.
    _recovery_resumed_item_ids: dict[str, str] = {}
    _session_init_envelopes: dict[str, tuple[float, RunnerSessionInitEnvelope]] = {}
    # session_id → canonical reasoning effort, seeded from the session-init
    # snapshot and updated by ``effort_change``. In-process harnesses learn the
    # effort only from the forwarded turn body, which is built field by field.
    _session_reasoning_effort: dict[str, str] = {}
    _session_skills_cache: dict[str, tuple[float, list[SkillSpec]]] = {}
    # session_id → the session's rollover-mode label, once resolved. Durable
    # for the runner process's lifetime (unlike _session_init_envelopes' 60s
    # TTL): rollover mode is set once at session creation and never changes,
    # so tool-call-time gating (mcp_execute) never re-fetches it. Empty dict
    # means "resolved, not rollover" — distinct from "not yet resolved".
    _session_rollover_labels_cache: dict[str, dict[str, str]] = {}
    _session_workspace_cache: dict[str, str | None] = {}  # session_id → workspace path
    _session_agent_ids = _session_agent_ids_ref  # shared with module-level get_session_agent_id
    _session_sub_agent_names: dict[str, str] = {}
    # Tracks whether the sub-agent was successfully resolved on session create.
    # True = child spec is cached; False = parent kept as fallback after miss.
    # Absent = session has no sub_agent_name or create has not completed.
    _session_sub_agent_resolved: dict[str, bool] = {}
    # Per-conversation set of (harness, InstructionDelivery) pairs already
    # warned about. Keyed by conversation so a session that switches harnesses
    # warns again for the new pair rather than inheriting the old one's silence.
    _instruction_delivery_warned: dict[str, set[tuple[str, InstructionDelivery]]] = {}
    _session_tool_schemas: dict[str, list[_JsonObject]] = {}  # session_id → cached tool schemas
    _session_mcp_spec_hash: dict[str, str] = {}  # session_id → last MCP spec hash

    # Process-lifetime monotonic fill-generation counter per session id.
    # Incremented (never popped) at delete_session so a fill parked across a
    # delete→recreate of the same id sees a changed generation and discards its
    # result instead of publishing stale data into the new session.
    _session_cache_generations: dict[str, int] = {}

    def _session_cache_generation(session_id: str) -> int:
        """Return (and materialize) the generation a cache fill starts under."""
        return _session_cache_generations.setdefault(session_id, 0)

    def _session_cache_generation_is_current(session_id: str, generation: int) -> bool:
        """True only when the captured generation still matches — fill may write."""
        return _session_cache_generations.get(session_id, 0) == generation

    _active_turns: dict[str, asyncio.Task[None] | None] = {}
    app.state.active_turns = _active_turns
    # Conversations whose claude-sdk `/compact` published an up-front
    # `response.compaction.in_progress`. Used to (a) swallow the executor's own
    # later `in_progress` so the web shows a single spinner, and (b) publish a
    # `failed` if the turn produced no compaction, so the spinner is never
    # stranded. Discarded on `response.compaction.completed` (real compaction),
    # else cleared by `_on_proxy_stream_end` — the single turn-end convergence
    # point reached on every exit path (clean end, setup error, cancel).
    _sdk_compact_inprogress: set[str] = set()
    app.state.sdk_compact_inprogress = _sdk_compact_inprogress
    _native_pane_status: dict[str, str] = {}
    app.state.native_pane_status = _native_pane_status
    _session_message_buffers: dict[str, list[dict[str, Any]]] = {}
    app.state.session_message_buffers = _session_message_buffers
    # ``if_running`` messages offered to a running turn, by injection id: (session, answer).
    # The answer is True once the turn takes or offers it for its next step, False if refused.
    _offer_answers: dict[str, tuple[str, asyncio.Future[bool]]] = {}
    _author_attribution_sessions: set[str] = set()
    _ingest_next_seq: dict[str, int] = {}
    _ingest_now_serving: dict[str, int] = {}
    _ingest_cond: dict[str, asyncio.Condition] = {}
    _interrupted_sessions: set[str] = set()
    app.state.interrupted_sessions = _interrupted_sessions
    # Desynced conversations; cleared when a fresh turn binds.
    _desynced_sessions: set[str] = set()
    app.state.desynced_sessions = _desynced_sessions
    # Monotonic epoch stamped at each turn bind; lets recovery detect a replacement that ran
    # and finished during a teardown await (slot empty, but epoch advanced).
    _turn_epoch_seq = itertools.count(1)
    _turn_bind_epoch: dict[str, int] = {}
    app.state.turn_bind_epoch = _turn_bind_epoch
    # Epoch at which desync recovery claimed the terminal token; competing sites skip their
    # ``idle`` only if the epoch still matches.
    _desync_terminalized: dict[str, int] = {}
    app.state.desync_terminalized = _desync_terminalized
    _background_tasks: set[asyncio.Task[Any]] = set()
    # One watcher per session for a pending Databricks sign-in (see _start_sign_in_watch).
    _subagent_recovery_tasks: dict[str, asyncio.Task[None]] = {}
    _subagent_wake_pending: set[str] = set()
    _last_rewake_notice: dict[str, str] = {}
    # Session -> the Stop line the server sent with an interrupt (ADR 0029), written into the
    # history with the cancellation marker.
    _stop_notes: dict[str, str] = {}
    # Parents whose wake POST exhausted its bounded retries while their inbox
    # still held a sub-agent result (typically: the server was down when the
    # child finished). The catch-up scan re-attempts these on tunnel reconnect.
    _stranded_wake_parents: set[str] = set()
    # Single-flight holder for the paced stranded-wake retry loop, so
    # back-to-back reconnects don't stack concurrent retry loops.
    _stranded_wake_retry_task: list[asyncio.Task[None]] = []

    _session_histories = _session_histories_ref
    # superside-chat: sessions whose warm-client drop (a manual /compact) was deferred because
    # a turn was active when it finished (see _drop_superside_chat_warm_client), applied by
    # _consume_superside_chat_pending_refresh at the next turn.
    _superside_chat_pending_refresh: set[str] = set()
    # conv_id -> context fill the session's latest turn reported (hard-limit check).
    _superside_chat_last_context_tokens: dict[str, int] = {}
    # conv_id -> the view start this runner last wrote (VIEW_START_LABEL): the label is written,
    # and the turns before it indexed, only when it moves (see _assembled_history).
    _view_starts: dict[str, str] = {}
    # conv_id -> what the session's current warm client was last sent of the Memory Profile,
    # Projects and file handle blocks (hashes or lines, plus the prompt and subprocess it was
    # sent under); and what the turn in flight carries, kept once the turn completes (see
    # _apply_turn_prefix_blocks).
    _superside_chat_prefix_sent: dict[str, dict[str, str]] = {}
    _superside_chat_prefix_pending: dict[str, dict[str, str]] = {}
    # conv_id -> the setup ("<harness> with <model>") its last turn ran on, so a turn after a
    # switch says what changed instead of leaving the model to trust its earlier answers.
    _superside_chat_setup: dict[str, str] = {}
    # A file's studio sessions on this host (ADR 0024): the turn cap, and when each harness goes
    # cold (released by the sweeper below once untouched for ten minutes).
    _studio_runners = StudioRunners()
    _studio_sweeper: list[asyncio.Task[None]] = []
    app.state.studio_runners = _studio_runners
    app.state.superside_chat_pending_refresh = _superside_chat_pending_refresh
    _last_server_item_id: dict[str, str] = {}
    # conv_id -> ``(type, role, id)`` of the record's newest item a turn wrote, kept only while
    # the record ends in items the engine recorded outside any turn (a picked-up topic's card,
    # ``RECORDED_RESPONSE_PREFIX``): recovery decides on that item, never on the recorded ones,
    # so a card landing after an unanswered message or mid-turn still resumes it. ``None``: no
    # turn wrote anything yet.
    _record_turn_tail: dict[str, tuple[Any, Any, Any] | None] = {}
    # conv_id -> the harness subprocess that took the session's last completed turn. The runner's
    # history is that subprocess's context; once it is gone (crash, relaunch, reconnect) the next
    # turn assembles the history from the record instead (ADR 0031: only the load starts one).
    _history_process: dict[str, str | None] = {}
    _session_event_queues = _session_event_queues_ref
    app.state.session_event_queues = _session_event_queues
    _session_inboxes = _session_inboxes_ref
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]] = {}

    def _has_active_work() -> bool:
        if _active_turns:
            return True
        if _has_live_async_tasks(_session_async_tasks):
            return True
        for timers in _session_timers.values():
            for timer_task in timers.values():
                if not timer_task.done():
                    return True
        if pending_approvals.has_any_pending():
            return True
        session_ids = set(_session_start_cache) | set(_session_agent_ids)
        return process_manager is not None and any(
            process_manager.has_active_turn(session_id) for session_id in session_ids
        )

    app.state.has_active_work = _has_active_work

    def _drain_session_streams() -> None:
        for queue in list(_session_event_queues.values()):
            queue.put_nowait(None)

    app.state.drain_session_streams = _drain_session_streams

    def _publish_event(session_id: str, event: Mapping[str, object]) -> None:
        event_body = cast(_JsonObject, event)
        queue = _session_event_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            _session_event_queues[session_id] = queue
        queue.put_nowait(event_body)
        if event_body.get("type") == "session.status":
            _status_value = event_body.get("status")
            if isinstance(_status_value, str):
                _native_pane_status[session_id] = _status_value
        _fan_out_child_delta_to_parent(session_id, event_body)

    def _child_preview_from_status(
        session_id: str,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> str | None:
        if latest_assistant_text is not None:
            reply_source = latest_assistant_text
        elif allow_history_preview_fallback:
            reply_source = _extract_last_assistant_text(session_id)
        else:
            return None
        reply = reply_source.strip()
        if not reply:
            return None
        return _truncate_child_preview(reply)

    def _child_status_body(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        include_error: bool = False,
    ) -> _JsonObject:
        busy = status in ("running", "waiting")
        child: _JsonObject = {
            "id": session_id,
            "title": meta.title,
            "tool": meta.tool,
            "session_name": meta.session_name,
            "busy": busy,
            "current_task_status": _session_status_to_task_status(status),
        }
        if include_error:
            child["last_task_error"] = error
        return child

    def _child_error_from_status_event(
        status: str | None,
        event: _JsonObject,
    ) -> dict[str, str] | None:
        if status != "failed":
            return None
        raw_error = event.get("error")
        if not isinstance(raw_error, dict):
            return None
        raw_code = raw_error.get("code")
        raw_message = raw_error.get("message")
        if not isinstance(raw_code, str) or not isinstance(raw_message, str):
            return None
        if not raw_code or not raw_message:
            return None
        return {"code": raw_code, "message": raw_message}

    def _build_child_status_update(
        session_id: str,
        meta: _ChildParentMeta,
        status: str | None,
        *,
        error: dict[str, str] | None = None,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> _JsonObject | None:
        if status in ("running", "waiting"):
            mark_subagent_work_started(session_id)
        # A trailing pane-idle edge must not turn a failed or aborted task
        # into success. A new running/waiting edge clears the terminal outcome.
        if status == "idle" and meta.last_task_status in ("failed", "cancelled"):
            return None
        busy = status in ("running", "waiting")
        task_status = _session_status_to_task_status(status)
        error_signature = (error["code"], error["message"]) if error is not None else None
        include_error = status in ("running", "waiting") or error is not None
        if (
            meta.last_busy == busy
            and meta.last_task_status == task_status
            and meta.last_error == error_signature
        ):
            return None
        meta.last_busy = busy
        meta.last_task_status = task_status
        meta.last_error = error_signature
        child = _child_status_body(
            session_id,
            meta,
            status,
            error=error,
            include_error=include_error,
        )
        if not busy:
            preview = _child_preview_from_status(
                session_id,
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if preview is not None:
                child["last_message_preview"] = preview
        return {
            "type": "session.child_session.updated",
            "conversation_id": meta.parent_id,
            "child_session_id": session_id,
            "child": child,
        }

    def _fan_out_child_delta_to_parent(
        session_id: str,
        event: _JsonObject,
        *,
        latest_assistant_text: str | None = None,
        allow_history_preview_fallback: bool = True,
    ) -> None:
        meta = _child_session_parents.get(session_id)
        if meta is None:
            return
        evt_type = event.get("type")
        if evt_type == "session.status":
            raw_status = event.get("status")
            status = raw_status if isinstance(raw_status, str) else None
            child_update = _build_child_status_update(
                session_id,
                meta,
                status,
                error=_child_error_from_status_event(status, event),
                latest_assistant_text=latest_assistant_text,
                allow_history_preview_fallback=allow_history_preview_fallback,
            )
            if child_update is not None:
                _publish_event(meta.parent_id, child_update)

    if resource_registry is None:
        resource_registry = SessionResourceRegistry(
            terminal_registry=terminal_registry,
            runner_workspace=runner_workspace,
            per_session_workspace=per_session_workspace,
        )
    app.state.session_resource_registry = resource_registry

    def _publish_terminal_activity(session_id: str, terminal_id: str) -> None:
        if process_manager is not None:
            process_manager.note_activity(session_id)
        _publish_event(
            session_id,
            {
                "type": "session.terminal.activity",
                "session_id": session_id,
                "terminal_id": terminal_id,
            },
        )

    resource_registry.set_terminal_activity_publisher(_publish_terminal_activity)

    def _publish_session_status(
        session_id: str,
        status: str,
        blocked_on: str | None = None,
        *,
        paused: bool = False,
    ) -> None:
        _publish_event(session_id, session_status_event(status, blocked_on, paused=paused))

    resource_registry.set_session_status_publisher(_publish_session_status)

    def _live_terminal_pane_snapshot(conv_id: str) -> str | None:
        """Return the first captured pane text among the conversation's terminals.

        Serves failures that strike while terminals are still alive (e.g. a
        harness stream drop): the required-terminal *exit* diagnostics never
        fire, yet what is on the pane right now is often the most actionable
        context available (a CLI parked at a trust prompt, an auth error, ...).
        """
        registry = resource_registry.terminal_registry if resource_registry else None
        if registry is None:
            return None
        for entry in registry.list_for_conversation(conv_id):
            try:
                pane = trim_terminal_output(entry.instance.last_pane_text())
            except Exception:
                _logger.exception(
                    "Failed to read terminal pane diagnostics for %s",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                continue
            if pane:
                return pane
        return None

    def _harness_stream_failure_message(conv_id: str, exc: BaseException) -> str:
        """Compose the user-facing message for a harness stream failure.

        Leads with the real transport cause (which otherwise reaches only the
        runner log) and attaches the live pane snapshot as a ``Last captured
        terminal output:`` block, which the web UI renders as diagnostics.
        """
        # httpx raises several transport errors with no message at all
        # (``ReadError()``), which left the whole diagnostic as the bare
        # sentence. Fall back to the exception type so the message always names
        # which transport failure ended the stream.
        cause = str(exc).strip() or type(exc).__name__
        message = f"Harness stream connection error: {cause}"
        pane = _live_terminal_pane_snapshot(conv_id)
        if pane:
            message = f"{message}\n\nLast captured terminal output:\n{pane}"
        return message

    def _publish_terminal_exit(event: TerminalExitEvent) -> None:
        _publish_event(
            event.session_id,
            {
                "type": "session.resource.deleted",
                "resource_id": event.terminal_id,
                "resource_type": "terminal",
                "session_id": event.session_id,
            },
        )

    resource_registry.set_terminal_exit_publisher(_publish_terminal_exit)

    from omnigent.runtime.filesystem_registry import (
        FilesystemRegistry,
        create_filesystem_registry,
        detect_git_root,
    )

    if runner_workspace is not None:
        filesystem_registry = create_filesystem_registry(watch_path=runner_workspace)
        filesystem_registry.start()
    else:
        filesystem_registry = None
    app.state.filesystem_registry = filesystem_registry

    _session_fs_registries: dict[str, FilesystemRegistry] = {}
    # Roots whose search registry stays warm; bounded so distinct generated
    # workspaces cannot accumulate for the runner's lifetime.
    _search_fs_registries: OrderedDict[str, FilesystemRegistry] = OrderedDict()
    _search_registry_cache_size = 8

    def _search_registry_for_root(root: Path) -> FilesystemRegistry:
        """Registry rooted at *root*, the tree a search actually walks.

        The session registry watches the session's stored workspace (or the
        runner's), which is not necessarily the environment root the search
        walks — runner-managed sessions get a generated per-session workspace
        no other registry covers. The repository root is re-detected on every
        call, so a repository created or removed mid-session — including one
        nested at the workspace root inside an outer repository — is read on
        the next search. The registry is never started: startup
        runs ``git update-index`` inside the repository, and a repository at a
        generated workspace root may be the agent's own. Search needs only
        the anchored index read.

        :param root: Absolute directory the search walks.
        :returns: A registry whose workspace root is *root*.
        """
        key = str(root)
        registry = _search_fs_registries.get(key)
        if registry is None or registry.git_root != detect_git_root(root):
            registry = create_filesystem_registry(watch_path=root)
            _search_fs_registries[key] = registry
            while len(_search_fs_registries) > _search_registry_cache_size:
                _search_fs_registries.popitem(last=False)
        _search_fs_registries.move_to_end(key)
        return registry

    async def _session_snapshot(session_id: str) -> _SessionSnapshot:
        cached = _session_snapshot_cache.get(session_id)
        if cached is not None:
            return cached
        lock = _session_snapshot_locks.setdefault(session_id, asyncio.Lock())
        generation = _session_cache_generation(session_id)
        async with lock:
            cached = _session_snapshot_cache.get(session_id)
            if cached is not None:
                return cached
            status_code: int | None = None
            created_at: float | None = None
            workspace: str | None = None
            agent_id: str | None = None
            sub_agent_name: str | None = None
            parent_session_id: str | None = None
            agent_name: str | None = None
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS
                )
                status_code = resp.status_code
                if resp.status_code == 200:
                    body = resp.json()
                    raw_created = body.get("created_at")
                    if raw_created is not None:
                        created_at = float(raw_created)
                    workspace = body.get("workspace")
                    raw_agent_id = body.get("agent_id")
                    if isinstance(raw_agent_id, str) and raw_agent_id:
                        agent_id = raw_agent_id
                    raw_sub_agent = body.get("sub_agent_name")
                    if isinstance(raw_sub_agent, str) and raw_sub_agent:
                        sub_agent_name = raw_sub_agent
                    raw_parent = body.get("parent_session_id")
                    if isinstance(raw_parent, str) and raw_parent:
                        parent_session_id = raw_parent
                    raw_agent_name = body.get("agent_name")
                    if isinstance(raw_agent_name, str) and raw_agent_name:
                        agent_name = raw_agent_name
            except Exception:  # noqa: BLE001 — best-effort; created_at falls back to wall time
                pass
            snapshot = _SessionSnapshot(
                ok=status_code == 200,
                status_code=status_code,
                created_at=created_at if created_at is not None else time.time(),
                workspace=workspace,
                agent_id=agent_id,
                sub_agent_name=sub_agent_name,
                parent_session_id=parent_session_id,
                agent_name=agent_name,
            )
            if snapshot.ok and snapshot.agent_id is not None:
                if _session_cache_generation_is_current(session_id, generation):
                    _session_snapshot_cache[session_id] = snapshot
            return snapshot

    async def _session_workspace_value(session_id: str) -> str | None:
        if session_id not in _session_workspace_cache:
            generation = _session_cache_generation(session_id)
            snapshot = await _session_snapshot(session_id)
            # A failed fetch carries no workspace. Memoizing its ``None``
            # would pin the session to the global workspace for its lifetime.
            if not snapshot.ok:
                return None
            if _session_cache_generation_is_current(session_id, generation):
                _session_workspace_cache[session_id] = snapshot.workspace
        return _session_workspace_cache.get(session_id)

    async def _fetch_session_model_override(session_id: str) -> str | None:
        """One-shot uncached read of the persisted ``/model`` override.

        Legacy (no-envelope) init only — current servers ship the override in
        the init envelope. Deliberately NOT cached in ``_SessionSnapshot``:
        ``model_override`` is mutable (a ``/model`` switch changes it), so a
        value stored in the long-lived identity cache would go stale and reseed
        the old model on a later re-init, forcing a needless respawn. Each init
        re-reads it fresh.
        """
        try:
            resp = await server_client.get(
                f"/v1/sessions/{session_id}", params=_SESSION_METADATA_PARAMS
            )
            if resp.status_code == 200:
                raw = resp.json().get("model_override")
                if isinstance(raw, str) and raw:
                    return raw
            else:
                _logger.warning(
                    "legacy model_override fallback for %s: session GET returned "
                    "HTTP %s; a model-pinned first turn may respawn",
                    session_id,
                    resp.status_code,
                )
        except Exception:
            _logger.warning(
                "legacy model_override fallback for %s failed; a model-pinned "
                "first turn may respawn",
                session_id,
                exc_info=True,
            )
        return None

    async def _session_runtime_cwd(session_id: str) -> Path | None:
        workspace = await _session_workspace_value(session_id)
        if workspace and workspace.strip():
            return Path(workspace.strip()).expanduser().resolve()
        return runner_workspace.resolve() if runner_workspace is not None else None

    async def _load_legacy_session_init_context() -> _SessionInitContext:
        await _get_server_version(server_client)
        return _SessionInitContext(envelope=None)

    def _load_envelope_session_init_context(
        envelope: RunnerSessionInitEnvelope,
        *,
        session_id: str,
        agent_id: str,
    ) -> _SessionInitContext:
        from omnigent.runner.session_init_protocol import validate_runner_inference_config

        if envelope.session_id != session_id or envelope.agent_id != agent_id:
            raise ValueError("session initialization envelope identity mismatch")
        validate_runner_inference_config(envelope.snapshot.inference_config)

        global _server_version
        _server_version = envelope.server_version
        snapshot = envelope.snapshot
        _session_snapshot_cache[session_id] = _SessionSnapshot(
            ok=True,
            status_code=200,
            created_at=float(snapshot.created_at),
            workspace=snapshot.workspace,
            agent_id=agent_id,
            sub_agent_name=envelope.sub_agent_name,
            parent_session_id=snapshot.parent_session_id,
        )
        _session_start_cache[session_id] = float(snapshot.created_at)
        _session_workspace_cache[session_id] = snapshot.workspace
        if envelope.sub_agent_name:
            _session_sub_agent_names[session_id] = envelope.sub_agent_name
        if snapshot.reasoning_effort:
            _session_reasoning_effort[session_id] = snapshot.reasoning_effort
        _session_init_envelopes[session_id] = (time.monotonic(), envelope)
        return _SessionInitContext(envelope=envelope)

    def _fresh_session_init_envelope(session_id: str) -> RunnerSessionInitEnvelope | None:
        cached = _session_init_envelopes.get(session_id)
        if cached is None:
            return None
        cached_at, envelope = cached
        if time.monotonic() - cached_at <= _SESSION_INIT_ENVELOPE_TTL_SECONDS:
            return envelope
        _session_init_envelopes.pop(session_id, None)
        return None

    async def _load_session_init_context(
        body: _JsonObject,
        *,
        session_id: str,
        agent_id: str,
    ) -> _SessionInitContext:
        envelope = parse_runner_session_init_envelope(body)
        if envelope is None:
            if os.environ.get("OMNIGENT_INFERENCE_CONFIG"):
                from omnigent.runner.session_init_protocol import validate_runner_inference_config

                validate_runner_inference_config(None)
            return await _load_legacy_session_init_context()
        body_sub_agent = body.get("sub_agent_name")
        if envelope.sub_agent_name != (
            body_sub_agent if isinstance(body_sub_agent, str) else None
        ):
            raise ValueError("session initialization envelope sub-agent mismatch")
        return _load_envelope_session_init_context(
            envelope,
            session_id=session_id,
            agent_id=agent_id,
        )

    async def _resolve_session_fs_registry(
        session_id: str,
    ) -> FilesystemRegistry | None:
        if session_id in _session_fs_registries:
            return _session_fs_registries[session_id]

        session_workspace = await _session_workspace_value(session_id)
        if session_workspace is None:
            return filesystem_registry

        session_ws_path = Path(session_workspace).resolve()
        runner_ws_resolved = runner_workspace.resolve() if runner_workspace is not None else None
        if runner_ws_resolved is not None and session_ws_path == runner_ws_resolved:
            return filesystem_registry

        registry = create_filesystem_registry(watch_path=session_ws_path)
        registry.start()
        _session_fs_registries[session_id] = registry
        return registry

    from omnigent.entities.environment_filesystem import (
        FilesystemEntry,
        ResourceError,
    )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(ValueError)
    async def _handle_value_error(
        request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_input",
                    "message": str(exc),
                },
            },
        )

    @app.exception_handler(ResourceError)
    async def _handle_resource_error(
        request: Request,
        exc: ResourceError,
    ) -> JSONResponse:
        del request
        from omnigent.entities.environment_filesystem import (
            DirectoryNotEmpty,
            FilesystemPathNotFound,
            FileTooLarge,
            InvalidPath,
            PathUnreachable,
            UnsupportedMediaType,
        )

        status = 500
        error: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, FilesystemPathNotFound):
            status = 404
        elif isinstance(exc, PathUnreachable):
            # 403, not 400: the path is well-formed, the caller just may not
            # see it. Carries the reachable roots so a UI can say what IS
            # available without a second round trip.
            status = 403
            error["reachable_roots"] = exc.reachable_roots
        elif isinstance(exc, InvalidPath):
            status = 400
        elif isinstance(exc, DirectoryNotEmpty):
            status = 409
        elif isinstance(exc, FileTooLarge):
            status = 413
        elif isinstance(exc, UnsupportedMediaType):
            status = 415
        return JSONResponse(
            status_code=status,
            content={"error": error},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _initialize_session(body: _JsonObject) -> JSONResponse:
        from omnigent.runner.session_init_protocol import RunnerInferenceConfigMismatch

        raw_id = body.get("session_id")
        set_current_session_id(raw_id if isinstance(raw_id, str) else None)
        _init_started = time.monotonic()
        _logger.info(
            "Runner session initialization started",
            extra=debug_event("runner_session_init_started", stage="session_init"),
        )
        if process_manager is None:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=501,
                    error_code="not_implemented",
                ),
            )
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner POST /v1/sessions needs a HarnessProcessManager."),
                },
            )
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not session_id or not agent_id:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=400,
                    error_code="invalid_request",
                ),
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": ("'session_id' and 'agent_id' required."),
                },
            )
        session_id = cast(str, session_id)
        agent_id = cast(str, agent_id)
        initial_turn_epoch = _turn_bind_epoch.get(session_id)
        initial_native_activity = resource_registry.session_activity_epoch(session_id)
        initially_active = session_id in _active_turns or resource_registry.session_turn_is_active(
            session_id
        )

        # Captured before init's first await: the legacy (no-envelope) context
        # load below probes the server's version over the network, so a reset
        # landing anywhere in init — including that probe — must fence the
        # memoizing writes at the end of init.
        spec_cache_generation = _session_cache_generation(session_id)

        try:
            init_context = await _load_session_init_context(
                body,
                session_id=session_id,
                agent_id=agent_id,
            )
        except RunnerInferenceConfigMismatch:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "inference_config_mismatch",
                    "detail": (
                        "This runner has a different saved provider configuration; "
                        "launch a new runner."
                    ),
                },
            )
        except ValueError:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=400,
                    error_code="invalid_request",
                ),
            )
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Invalid session initialization envelope.",
                },
            )

        # Stamp the session's Smart Routing class before anything reads it: the
        # spawn env is rebuilt on every harness respawn, long after this
        # envelope is gone, and the class decides whether the session gets the
        # spawn-routing endpoint at all.
        _routing_class = init_context.routing_class
        remember_session_routing_class(session_id, _routing_class)
        if init_context.envelope is not None:
            _note_session_harness_override(
                session_id, init_context.envelope.snapshot.harness_override
            )

        spec: AgentSpec | None = None
        spec_entry: _SpecEntry | None = None
        if spec_resolver is not None:
            try:
                spec_entry = await spec_resolver(agent_id, session_id)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                _logger.error(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        status_code=503,
                        error_code="spec_resolver_failed",
                    ),
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        # The session's persisted /model override; the recovery turns started at the end of
        # init carry it so they run on the same model as every later turn.
        _model_override: str | None = None
        if spec_entry is not None:
            spec = _unwrap_spec_entry(spec_entry)
            raw_sub_agent_name = body.get("sub_agent_name")
            _sa_name_assign = cast(str | None, raw_sub_agent_name)
            # A sub-agent's bundle assets live under its own directory; keeping
            # the parent's workdir would load the parent's skills and local
            # tools into the child.
            if _sa_name_assign:
                _sub_entry = _resolve_sub_agent_spec_entry(spec_entry, _sa_name_assign)
                if _sub_entry is None:
                    _warn_unresolved_sub_agent(session_id, _sa_name_assign)
                    _session_sub_agent_resolved[session_id] = False
                else:
                    spec_entry = _sub_entry
                    spec = _unwrap_resolved_spec(_sub_entry)
                    _session_sub_agent_resolved[session_id] = True
            # The session's override outranks the spec: resolving from the spec
            # alone made init spawn a harness the turns never ask for, evicting
            # the override's live subprocess (entries are keyed by conversation).
            raw_harness = (
                _session_harness_overrides.get(session_id)
                or spec.executor.config.get("harness")
                or spec.executor.type
            )
            harness_name = canonicalize_harness(raw_harness) or raw_harness

            _start_verdict = await _evaluate_agent_start_gate(spec, harness_name)
            if _start_verdict is not None:
                if _start_verdict.action in ("deny", "ask"):
                    _logger.error(
                        "Runner session initialization failed",
                        extra=debug_event(
                            "runner_session_init_failed",
                            stage="session_init",
                            status_code=403,
                            error_code="agent_start_denied",
                        ),
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "agent_start_denied",
                            "detail": _start_verdict.deny_text or "Agent start denied by policy",
                        },
                    )
                if _start_verdict.data is not None:
                    _apply_sandbox_override_from_verdict(spec, _start_verdict.data)

            await _ensure_session_subagent_router(
                session_id,
                harness_name,
                server_client=server_client,
                routing_class=_routing_class,
            )
            # Seed the initial spawn with the persisted /model override so a
            # model-pinned session's first turn doesn't force a wasteful
            # model-switch respawn. Current servers ship the override in the
            # init envelope (read fresh each init); legacy (no-envelope) servers
            # fall back to a one-shot uncached GET. Both sources are read fresh
            # so a later /model switch can't reseed a stale model. Native
            # harnesses no-op (_build_spawn_env_from_spec guards on env=None).
            _model_override = (
                init_context.envelope.snapshot.model_override
                if init_context.envelope is not None
                else await _fetch_session_model_override(session_id)
            )
            try:
                spawn_env = _build_spawn_env_from_spec(
                    spec,
                    raw_harness,
                    workdir=_resolved_spec_workdir(spec_entry),
                    cwd=await _session_runtime_cwd(session_id),
                    session_id=session_id,
                    model_override=_model_override,
                )
            except OmnigentError as exc:
                # The relay also needs the failure when init precedes the first turn.
                _publish_turn_status(
                    session_id, "failed", error={"code": exc.code, "message": str(exc)}
                )
                raise
            # An agent-cache reset acknowledged while init awaited retired
            # this entry; skip the memoization so the reset wins. Init still
            # runs on what it resolved — the next spec read re-resolves.
            if _session_cache_generation_is_current(session_id, spec_cache_generation):
                _session_spec_cache[session_id] = spec_entry
        else:
            if spec_resolver is not None:
                # spec_resolver was configured but returned no spec for this
                # agent_id. Return a clear 400 rather than silently proceeding
                # with the test-only harness and leaving the session in a
                # broken/unrunnable state.
                _logger.error(
                    "Runner session initialization failed",
                    extra=debug_event(
                        "runner_session_init_failed",
                        stage="session_init",
                        status_code=400,
                        error_code="no_agent_spec",
                    ),
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "no_agent_spec",
                        "detail": (
                            f"No agent spec found for agent_id={agent_id!r}. "
                            "Ensure the agent is registered before creating a session."
                        ),
                    },
                )
            harness_name = "runner-test-default"
            spawn_env = None

        try:
            harness_client = await process_manager.get_client(
                session_id,
                harness_name,
                env=spawn_env,
            )
        except RuntimeError as exc:
            _logger.error(
                "Runner session initialization failed",
                extra=debug_event(
                    "runner_session_init_failed",
                    stage="session_init",
                    status_code=503,
                    error_code="harness_spawn_failed",
                ),
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _session_start_cache.setdefault(session_id, time.time())
        if harness_name in ("claude-sdk", "pi") and spec is not None:
            _harness_prelaunches[session_id] = asyncio.create_task(
                _prelaunch_harness(
                    session_id, harness_client, harness_name, _model_override, agent_id
                ),
                name=f"harness-prelaunch-{session_id}",
            )
        # The same reset that retires the spec entry also retires this
        # binding, and later resets read it to decide which agent's shared
        # ``_spec_cache`` entry to drop; reinstating a superseded binding
        # would misdirect them at the old agent. Same fence as the spec-cache
        # write above — a fenced-out reader falls back to the fresh session
        # snapshot instead.
        if _session_cache_generation_is_current(session_id, spec_cache_generation):
            _session_agent_ids[session_id] = agent_id
        else:
            # The reset that fenced this binding ran before init registered
            # the harness, so it could not release the subprocess spawned
            # above with the superseded spec's environment baked in. With
            # the binding left absent, the next turn's prior-binding teardown
            # would skip release too, and a new agent sharing the harness and
            # model would silently reuse the stale process. Release it now so
            # the next turn respawns from the freshly resolved spec.
            _logger.info(
                "session init raced an agent-cache reset; releasing the "
                "harness spawned from the superseded spec",
                extra={"session_id": session_id},
            )
            # Conditional on idleness (the reaper's guard): a consumer that
            # touched or is mid-turn on the shared entry after this cutoff is
            # never torn down out from under it; the entry init itself just
            # registered predates the cutoff and is released.
            await process_manager.release(session_id, only_if_idle_cutoff=time.monotonic())
        if session_id not in _session_event_queues:
            _session_event_queues[session_id] = asyncio.Queue()
        if session_id not in _session_inboxes:
            _session_inboxes[session_id] = asyncio.Queue()
        # A fresh queue can mean a fresh runner process rather than a fresh
        # session: re-queue results the previous process never drained. Start
        # the durable server scan now, but overlap it with terminal creation;
        # sys_read_inbox uses the same locked helper if a turn races the scan.
        _deliver_retained_subagent_results(session_id)
        _subagent_recovery_task = _start_subagent_recovery(session_id)
        if session_id not in _session_async_tasks:
            _session_async_tasks[session_id] = {}
        raw_sub_agent_name = body.get("sub_agent_name")
        _sa_name = cast(str | None, raw_sub_agent_name)
        if _sa_name:
            _session_sub_agent_names[session_id] = _sa_name

        # Preserve the initialization contract: undrained child results are
        # recovered before POST /sessions returns. The scan no longer delays
        # native terminal registration because it ran concurrently above.
        await asyncio.shield(_subagent_recovery_task)

        # Crash recovery (Step 8.5 Scenario A): if the session
        # has existing history, check whether the last item
        # indicates an incomplete turn that needs restarting.
        #
        # Skip the recovery-turn check when the server set
        # suppress_recovery_turn=True in the init envelope.  That flag means
        # the server is about to forward the triggering message immediately
        # after this handshake completes.  If the message was already
        # persisted to DB before the init call (invariant I1), the history
        # load would see it and start a redundant recovery turn; the
        # subsequent forward would then find _active_turns occupied, buffer
        # the message, and re-process it once the recovery turn finishes —
        # causing the first message to be silently ignored (sandbox
        # wake) or processed twice (managed relaunch).
        _suppress_recovery = (
            init_context.envelope is not None and init_context.envelope.suppress_recovery_turn
        )
        recovery_id = (
            init_context.envelope.recovery_id
            if init_context.envelope is not None
            and init_context.envelope.resume_interrupted_turn
            and not _suppress_recovery
            else None
        )
        history = await _load_history_as_input(session_id)
        execution_seen = (
            initially_active
            or _turn_bind_epoch.get(session_id) != initial_turn_epoch
            or resource_registry.session_activity_epoch(session_id) != initial_native_activity
        )
        recovery_turn = "none"
        if history and not execution_seen and session_id not in _active_turns:
            _session_histories[session_id] = history
            resumed_item_id = _last_server_item_id.get(session_id)
            if session_id in _record_turn_tail:
                last_type, last_role, resumed_item_id = _record_turn_tail[session_id] or (
                    None,
                    None,
                    None,
                )
            else:
                last = history[-1]
                last_type = last.get("type")
                last_role = last.get("role")
            needs_turn = (
                (last_type == "message" and last_role == "user")
                or last_type == "function_call"
                or last_type == "function_call_output"
            )
            if (
                needs_turn
                and recovery_id is None
                and not _suppress_recovery
                and session_id not in _active_turns
            ):
                recovery_turn = "history_resume"
                if last_type == "message" and last_role == "user" and resumed_item_id:
                    _recovery_resumed_item_ids[session_id] = resumed_item_id
                _begin_turn_slot(session_id)
                _publish_turn_status(session_id, "running")
                msg_body = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
                    # Recovery has no live server dispatch carrying renderer state.
                    "browser_renderer_available": False,
                    **({"model_override": _model_override} if _model_override else {}),
                }
                _turn_task = asyncio.create_task(
                    _run_turn_bg(msg_body, session_id),
                    name=f"turn-recover-{session_id}",
                )
                _active_turns[session_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

        if recovery_id is not None and recovery_id not in _recovery_turn_ids.get(
            session_id, set()
        ):
            # Active execution, including a newer message, takes precedence over
            # automatic continuation. Initialization alone cannot consume it.
            if (
                not execution_seen
                and session_id not in _active_turns
                and not resource_registry.session_turn_is_active(session_id)
            ):
                recovery_turn = "recovery_prompt"
                _begin_turn_slot(session_id)
                _publish_turn_status(session_id, "running")
                recovery_body: _JsonObject = {
                    "agent_id": agent_id,
                    "model": body.get("model", agent_id),
                    "browser_renderer_available": False,
                    **({"model_override": _model_override} if _model_override else {}),
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Your runner was interrupted while this task was active. "
                                "Continue the existing task from its current state. "
                                "Check any interrupted operation's outcome before repeating it."
                            ),
                        }
                    ],
                }
                _session_histories.setdefault(session_id, []).append(
                    {"type": "message", "role": "user", "content": recovery_body["content"]}
                )
                recovery_task = asyncio.create_task(
                    _run_turn_bg(recovery_body, session_id), name=f"turn-recover-{session_id}"
                )
                _active_turns[session_id] = recovery_task
                recovery_task.add_done_callback(_background_tasks.discard)
                _background_tasks.add(recovery_task)
            _recovery_turn_ids.setdefault(session_id, set()).add(recovery_id)

        status = "running" if session_id in _active_turns else "idle"
        _note_cold_start_init(_init_started)
        # The recovery decision and its inputs, so a turn that restarted after a
        # reconnect can be attributed to the history heuristic, the server's
        # continuation request, or neither.
        _logger.info(
            "Runner session initialization finished",
            extra=debug_event(
                "runner_session_initialized",
                session_id=session_id,
                stage="session_init",
                status_code=201,
                harness=harness_name,
                status=status,
                recovery_turn=recovery_turn,
                recovery_id=recovery_id,
                resume_interrupted_turn=(
                    init_context.envelope is not None
                    and init_context.envelope.resume_interrupted_turn
                ),
                suppress_recovery_turn=_suppress_recovery,
                execution_seen=execution_seen,
                history_len=len(history),
                last_item_type=history[-1].get("type") if history else None,
            ),
        )
        return JSONResponse(
            status_code=201,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(_session_start_cache[session_id]),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
                "session_init_protocol_version": (
                    init_context.envelope.protocol_version
                    if init_context.envelope is not None
                    else None
                ),
                **(
                    {"inference_config_verified": True}
                    if init_context.envelope is not None
                    and init_context.envelope.snapshot.inference_config is not None
                    else {}
                ),
            },
        )

    @app.post("/v1/sessions")
    async def create_session(request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Session initialization body must be a JSON object.",
                },
            )
        session_id = body.get("session_id")
        agent_id = body.get("agent_id")
        if not isinstance(session_id, str) or not isinstance(agent_id, str):
            return await _initialize_session(body)
        sub_agent_name = body.get("sub_agent_name")
        try:
            envelope = parse_runner_session_init_envelope(body)
        except ValueError:
            return await _initialize_session(body)
        key = (
            session_id,
            agent_id,
            sub_agent_name if isinstance(sub_agent_name, str) else None,
            envelope.recovery_id if envelope is not None else None,
        )
        task = _session_init_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                _initialize_session(body),
                name=f"session-init-{session_id}",
            )
            _session_init_tasks[key] = task

            def _drop_completed_init(done: asyncio.Task[JSONResponse]) -> None:
                if _session_init_tasks.get(key) is done:
                    _session_init_tasks.pop(key, None)

            task.add_done_callback(_drop_completed_init)
        response = await asyncio.shield(task)
        return JSONResponse(
            status_code=response.status_code,
            content=json.loads(bytes(response.body)),
        )

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream_session(session_id: str) -> StreamingResponse:
        async def _event_generator() -> AsyncIterator[bytes]:
            queue = _session_event_queues.get(session_id)
            if queue is None:
                queue = asyncio.Queue()
                _session_event_queues[session_id] = queue
            heartbeat_frame = b'data: {"type": "session.heartbeat"}\n\n'
            yield heartbeat_frame
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SESSION_STREAM_HEARTBEAT_S
                    )
                except asyncio.TimeoutError:
                    yield heartbeat_frame
                    continue
                if event is None:
                    break
                frame = "data: " + json.dumps(event) + "\n\n"
                try:
                    yield frame.encode("utf-8")
                except (GeneratorExit, asyncio.CancelledError):
                    queue.put_nowait(event)
                    return
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": ("Runner GET /v1/sessions/{id} needs a HarnessProcessManager."),
                },
            )
        # A live session's subprocess can be legitimately unregistered — a
        # fence-fired release after init raced a reset, or an agent-switch
        # teardown awaiting its next-turn respawn — so a missing entry alone
        # is not "no such session": the start cache tracks every session this
        # runner initialized until it is deleted.
        if not process_manager.has_session(session_id) and session_id not in _session_start_cache:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "not_found",
                    "detail": (f"No session '{session_id}' on this runner."),
                },
            )
        has_turn = session_id in _active_turns or process_manager.has_active_turn(session_id)
        status = "running" if has_turn else "idle"
        # A failed setup has no active turn; retain its failure in server status probes.
        if not has_turn and _native_pane_status.get(session_id) == "failed":
            status = "failed"
        agent_id = _session_agent_ids.get(session_id)
        if agent_id is None:
            # An agent-cache reset retires the binding while the session
            # stays live, so a registered session can transiently lack it.
            # The server snapshot is the authoritative binding; serve it
            # rather than failing the read (the next turn re-memoizes).
            snapshot = await _session_snapshot(session_id)
            if snapshot.ok and snapshot.agent_id is not None:
                _logger.info(
                    "session agent binding absent from cache; serving the server snapshot binding",
                    extra={"session_id": session_id},
                )
                agent_id = snapshot.agent_id
        if agent_id is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but agent_id missing from cache."
                    ),
                },
            )
        created_at = _session_start_cache.get(session_id)
        if created_at is None:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "detail": (
                        f"Session '{session_id}' registered but start_time missing from cache."
                    ),
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": session_id,
                "agent_id": agent_id,
                "status": status,
                "created_at": int(created_at),
                "title": None,
                "labels": {},
                "runner_id": None,
                "reasoning_effort": None,
                "items": [],
                "permission_level": None,
            },
        )

    @app.get("/v1/sessions/{session_id}/pending-approvals")
    async def get_pending_approvals(session_id: str) -> dict[str, list[str]]:
        """The approvals this runner is still waiting on for *session_id*."""
        return {"elicitation_ids": pending_approvals.parked_ids(session_id)}

    @app.get("/v1/sessions/{session_id}/buffered")
    async def get_buffered_messages(session_id: str) -> JSONResponse:
        """The messages waiting behind the running turn (live state; nothing is persisted).

        :returns: ``{"item_ids": [...]}``: the persisted user messages waiting for the next
            turn, oldest first. One offered to the running turn is not waiting.
        """
        return JSONResponse(
            {
                "item_ids": [
                    body["persisted_item_id"]
                    for body in _waiting(session_id)
                    if isinstance(body.get("persisted_item_id"), str)
                ]
            }
        )

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> JSONResponse:
        _drop_message_buffer(session_id)
        # Stop initialization before it can recreate resources during teardown.
        init_tasks = [
            task
            for key, task in list(_session_init_tasks.items())
            if key[0] == session_id and not task.done()
        ]
        for init_task in init_tasks:
            init_task.cancel()
        if init_tasks:
            # Bound cleanup time; log init failures so resource teardown still runs.
            _finished, pending = await asyncio.wait(
                set(init_tasks), timeout=_SESSION_INIT_CANCEL_TIMEOUT_S
            )
            if pending:
                _logger.warning(
                    "Cancelled session init for %s did not finish within %.0fs",
                    session_id,
                    _SESSION_INIT_CANCEL_TIMEOUT_S,
                )
            for done_task in _finished:
                if not done_task.cancelled() and done_task.exception() is not None:
                    _logger.warning(
                        "Session init for %s failed while being cancelled: %r",
                        session_id,
                        done_task.exception(),
                    )
        await _cancel_subagent_recovery(session_id)
        turn_task = _active_turns.pop(session_id, None)
        if turn_task is not None and isinstance(turn_task, asyncio.Task):
            turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn_task
        await mcp_execution_registry.cancel_session(session_id)
        pending_approvals.cancel_session(session_id, "session_closed")
        _drop_message_buffer(session_id)
        _live_response_id.pop(session_id, None)
        _turn_said.pop(session_id, None)
        # Clear all desync/turn state so a recreated same-id session starts clean.
        _turn_bind_epoch.pop(session_id, None)
        _recovery_turn_ids.pop(session_id, None)
        _recovery_resumed_item_ids.pop(session_id, None)
        _desync_terminalized.pop(session_id, None)
        _desynced_sessions.discard(session_id)
        _native_pane_status.pop(session_id, None)
        _ingest_next_seq.pop(session_id, None)
        _ingest_now_serving.pop(session_id, None)
        _ingest_cond.pop(session_id, None)
        _interrupted_sessions.discard(session_id)

        if process_manager is not None:
            await process_manager.forward_cancel(session_id)

        queue = _session_event_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(None)

        if process_manager is not None:
            await process_manager.release(session_id)

        await resource_registry.cleanup_session(session_id)

        # The SDK harnesses' router is started here, so this is its only
        # teardown: without it the session leaks an HTTP server, its thread,
        # and a live bearer token on disk.
        from omnigent.runner.subagent_routing import shutdown_session_router

        await asyncio.to_thread(shutdown_session_router, session_id)
        forget_session_routing_class(session_id)

        from omnigent.runner.tool_dispatch import forget_spawn_family

        forget_spawn_family(session_id)

        # Increment before clearing caches so in-flight fills see a changed
        # generation and discard rather than repopulating entries for a dead
        # (or reborn) session.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1

        _session_spec_cache.pop(session_id, None)
        _session_harness_overrides.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_start_cache.pop(session_id, None)
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_snapshot_locks.pop(session_id, None)
        _session_init_envelopes.pop(session_id, None)
        _session_reasoning_effort.pop(session_id, None)
        _session_spec_locks.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _instruction_delivery_warned.pop(session_id, None)
        _session_sub_agent_resolved.pop(session_id, None)
        _session_histories.pop(session_id, None)
        _history_process.pop(session_id, None)
        _view_starts.pop(session_id, None)
        _forget_superside_chat_prefix(session_id)
        _last_server_item_id.pop(session_id, None)
        _session_event_queues.pop(session_id, None)
        _session_inboxes.pop(session_id, None)
        _subagent_recovery_done.discard(session_id)
        _subagent_recovery_locks.pop(session_id, None)
        _subagent_wake_pending.discard(session_id)
        _stranded_wake_parents.discard(session_id)
        _last_rewake_notice.pop(session_id, None)
        _stop_notes.pop(session_id, None)
        _session_sub_agent_names.pop(session_id, None)
        unregister_child_session(session_id)
        unregister_subagent_work_for_session(session_id)
        if filesystem_registry is not None:
            filesystem_registry.unregister_conversation(session_id)
        for _task, evt in _session_async_tasks.pop(session_id, {}).values():
            evt.set()
        for _tmr in _session_timers.pop(session_id, {}).values():
            _tmr.cancel()
        _version_cache.pop(session_id, None)
        stale_resp_ids = [rid for rid, cid in _resp_to_conv.items() if cid == session_id]
        for rid in stale_resp_ids:
            _resp_to_conv.pop(rid, None)

        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.deleted",
                "deleted": True,
            },
        )

    async def _load_history_as_input(
        session_id: str,
        drop_item_id: str | None = None,
        keep_tokens: int | None = None,
    ) -> list[_JsonObject]:
        """Rebuild a session's harness input from its record.

        Reads from the latest checkpoint's anchor on, not from item 1 (see
        ``iter_items_from_checkpoint``); the record before it stays reachable
        through ``session_history``. A superside-chat session gets one assembled
        block of state plus its newest messages (``_assembled_history``, ADR 0031):
        a restart, a relaunch, a harness switch or an idle return bridges and never
        summarizes. Its tail is trimmed to the kept-tail budget, or to *keep_tokens*
        (the hard-limit caller, which schedules its own rollover). Any other session
        replays the items since the checkpoint, trimmed only to *keep_tokens*.
        """
        from omnigent.context.labels import is_superside_chat
        from omnigent.context.rollover import resolve_keep_tokens
        from omnigent.superchat.rollover import (
            ItemsPageFetchError,
            iter_items_from_checkpoint,
        )

        rollover_labels = await _rollover_labels_for_session(session_id)
        superside = is_superside_chat(rollover_labels)
        if superside and keep_tokens is None:
            keep_tokens = resolve_keep_tokens(rollover_labels)
        all_items: list[_JsonObject] = []
        turn_tail: tuple[Any, Any, Any] | None = None
        _record_turn_tail.pop(session_id, None)
        try:
            async for page_items in iter_items_from_checkpoint(
                server_client,
                session_id,
                skip_placeholders=superside,
            ):
                all_items.extend(page_items)
                last_id = page_items[-1].get("id")
                if last_id:
                    _last_server_item_id[session_id] = last_id
                for raw in page_items:
                    response_id = raw.get("response_id")
                    if not (
                        isinstance(response_id, str)
                        and response_id.startswith(RECORDED_RESPONSE_PREFIX)
                    ):
                        turn_tail = (raw.get("type"), raw.get("role"), raw.get("id"))
                last_response = page_items[-1].get("response_id")
                if isinstance(last_response, str) and last_response.startswith(
                    RECORDED_RESPONSE_PREFIX
                ):
                    _record_turn_tail[session_id] = turn_tail
                else:
                    _record_turn_tail.pop(session_id, None)
        except (httpx.HTTPError, ValueError, ItemsPageFetchError):
            _logger.warning(
                "History load failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )

        if drop_item_id is not None:
            all_items = [it for it in all_items if it.get("id") != drop_item_id]
        if superside:
            converted = await _assembled_history(session_id, all_items, cast(int, keep_tokens))
        else:
            converted = _convert_raw_items_to_input(all_items, keep_tokens=keep_tokens)
        # Items are persisted pre-resolution, so reloaded history can still
        # carry raw file_id blocks (the runner has no file/artifact stores).
        # Resolve them the same way current-turn intake does.
        for item in converted:
            content = item.get("content")
            if item.get("type") == "message" and isinstance(content, list):
                item["content"] = await _resolve_forwarded_message_content(
                    content,
                    session_id=session_id,
                    server_client=server_client,
                )
        return converted

    async def _assembled_history(
        session_id: str, items: list[_JsonObject], keep_tokens: int
    ) -> list[_JsonObject]:
        """A new session's input: one assembled block, then the newest messages as they are.

        The block (``assemble_block``) holds the checkpoint's working list once, a line per
        settled turn left out of the tail (Rounds, hidden ones included, else the transcript),
        the person's words from those turns and the tail itself in the second person. From the
        tail's last answer on, the items stay raw, so the history still ends where the record
        does (the message this turn answers, an interrupted call) and the recovery check and the
        harness prompt builders read it as before. Code only: no model call, nothing awaited but
        the Round read. Writes the view start and schedules indexing of the turns before it.
        """
        from omnigent.context.rollover import (
            assemble_block,
            clip_notices,
            is_reset_item,
            select_recent,
            split_at_latest_compaction,
            strip_checkpoint_header,
            transcript_turn_lines,
        )
        from omnigent.models.model_fallbacks import ROLLOVER_TOKEN_COUNT_MODEL

        remaining, c = split_at_latest_compaction(items)
        kept = (
            select_recent(remaining, keep_tokens=keep_tokens, model=ROLLOVER_TOKEN_COUNT_MODEL)
            or remaining
        )
        retired = remaining[: len(remaining) - len(kept)]
        # The tail's last answer and what follows it stay raw items; before it is the block's.
        cut = next(
            (
                i
                for i in range(len(kept) - 1, -1, -1)
                if kept[i].get("type") == "message" and kept[i].get("role") == "assistant"
            ),
            0,
        )
        shown, raw = clip_notices(kept)[:cut], kept[cut:]
        _note_view_start(session_id, retired, c)
        lines = transcript_turn_lines(retired)
        covered = {line.key for line in lines}
        rounds = await _round_turn_lines(session_id, remaining, retired)
        if rounds is not None:
            # A Round's line stands for its turn; a turn no Round read covers keeps its own.
            round_lines, round_ids = rounds
            lines = [*round_lines, *(line for line in lines if line.key not in round_ids)]
            covered |= round_ids
        summary = c.get("summary") if c is not None and not is_reset_item(c) else None
        since = next((it.get("created_at") for it in remaining if it.get("created_at")), None)
        block = assemble_block(
            working_list=strip_checkpoint_header(summary) if isinstance(summary, str) else None,
            turn_lines=lines,
            retired=retired,
            shown=shown,
            covered=covered,
            since=since,
            model=ROLLOVER_TOKEN_COUNT_MODEL,
        )
        converted = _convert_raw_items_to_input(raw)
        if block is None:
            return converted
        _logger.info(
            "assembled history for %s: %d chars, %d items retired, %d turn lines, %d raw items",
            session_id,
            len(block),
            len(retired),
            len(lines),
            len(converted),
            extra={"session_id": session_id},
        )
        opening: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": block}],
        }
        return [opening, *converted]

    async def _round_turn_lines(
        session_id: str, since_anchor: list[_JsonObject], retired: list[_JsonObject]
    ) -> tuple[list[Any], set[str]] | None:
        """The Conversation's settled Rounds a new session does not see in full, as turn lines:
        the Rounds of *retired* turns, and the hidden Rounds (scheduled and system runs) since
        the checkpoint. ``(lines, ids of the person's messages they stand for)``; ``None`` for a
        Helper or the studio (no Rounds of their own) or when the Rounds cannot be read."""
        from omnigent.context.rollover import TurnLine, turn_line
        from omnigent.superchat.rounds.record import journal_entry

        snapshot = await _session_snapshot(session_id)
        if not snapshot.ok or snapshot.parent_session_id:
            return None
        base = f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/rounds"
        try:
            resp = await server_client.get(base, params={"limit": "50"}, timeout=10.0)
            rows = resp.json().get("data") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            rows = None
        if not isinstance(rows, list):
            return None
        known = {str(it.get("id")) for it in since_anchor if it.get("id")}
        mine = {str(it.get("id")) for it in retired if it.get("id")}
        since = next((it.get("created_at") for it in since_anchor if it.get("created_at")), 0)
        wanted = [
            str(row["round_id"])
            for row in rows
            if isinstance(row, dict)
            and row.get("round_id")
            and row.get("settled_at") is not None
            and (
                str(row["round_id"]) in mine
                or (str(row["round_id"]) not in known and (row.get("started_at") or 0) >= since)
            )
        ]

        async def view(round_id: str) -> dict[str, Any] | None:
            try:
                got = await server_client.get(
                    f"{base}/{urllib.parse.quote(round_id, safe='')}", timeout=10.0
                )
                body = got.json() if got.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None
            return body if isinstance(body, dict) else None

        lines: list[Any] = []
        ids: set[str] = set()
        for round_id, body in zip(wanted, await asyncio.gather(*map(view, wanted)), strict=True):
            if body is None:
                continue
            hidden = bool(body.get("hidden"))
            try:
                entry = journal_entry(round_id, {**body, "hidden": False})
            except (KeyError, TypeError, ValueError):
                entry = None
            if entry is None:
                continue
            at = body.get("started_at") or body.get("settled_at") or 0
            text = turn_line(
                at,
                entry["ask"],
                entry["outcome"],
                files=entry["files"],
                stopped=entry["stopped"],
                scheduled=hidden,
            )
            lines.append(TurnLine(float(at), round_id, text))
            if not hidden:
                ids.add(round_id)
        return lines, ids

    def _note_view_start(
        session_id: str, retired: list[_JsonObject], checkpoint: _JsonObject | None
    ) -> None:
        """Write where this session's view starts (``VIEW_START_LABEL``: the last item before
        it) and index the turns before it for recall, both in the background and only when the
        view start moved. recall then finds a turn a restart or a handoff left out."""
        from omnigent.context.rollover import VIEW_START_LABEL

        after = str(
            (retired[-1].get("id") if retired else (checkpoint or {}).get("last_item_id")) or ""
        )
        if _view_starts.get(session_id) == after:
            return
        _view_starts[session_id] = after

        async def note() -> None:
            from omnigent.runner.memory.sync import index_retired_turns
            from omnigent.superchat.projects.card import project_slug_from_workspace

            try:
                await server_client.patch(
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                    json={"labels": {VIEW_START_LABEL: after}},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                _logger.warning(
                    "view start not written for %s",
                    session_id,
                    exc_info=True,
                    extra={"session_id": session_id},
                )
            if not retired:
                return
            labels = await _rollover_labels_for_session(session_id)
            lineage = await _memory_labels(session_id, labels)
            workspace = await _session_workspace_value(session_id)
            await index_retired_turns(
                server_client,
                session_id,
                lineage,
                retired,
                project=project_slug_from_workspace(workspace) or "",
            )

        task = asyncio.create_task(note(), name=f"view-start-{session_id}")
        task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(task)

    def _convert_raw_items_to_input(
        items: list[_JsonObject],
        keep_tokens: int | None = None,
    ) -> list[_JsonObject]:
        from omnigent.context.rollover import (
            clip_notices,
            select_recent,
            split_at_latest_compaction,
        )
        from omnigent.models.model_fallbacks import ROLLOVER_TOKEN_COUNT_MODEL

        # Items after the checkpoint's anchor, including any turn that ran
        # while the rollover was summarizing (see split_at_latest_compaction).
        remaining, c = split_at_latest_compaction(items)
        if keep_tokens is not None:
            # Whole newest turns that fit; the latest user message is always in.
            kept = (
                select_recent(remaining, keep_tokens=keep_tokens, model=ROLLOVER_TOKEN_COUNT_MODEL)
                or remaining
            )
            remaining = kept
        compaction_idx = None if c is None else items.index(c)
        # Notices replay as their outcome line plus a few hundred characters (ADR 0031), as
        # select_recent counted them; the message this turn answers stays whole.
        _compacted = cast(list[_JsonObject], (c or {}).get("compacted_messages") or [])
        _clipped = clip_notices([*_compacted, *remaining])
        _compacted, remaining = _clipped[: len(_compacted)], _clipped[len(_compacted) :]

        result: list[_JsonObject] = []
        if c is not None:
            if _compacted:
                result.extend(_compacted)
            else:
                result.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "[Automatically generated summary of prior "
                                    "conversation context.]\n\n"
                                    "Please provide a summary of our conversation so far."
                                ),
                            }
                        ],
                    }
                )
                result.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": c.get("summary", ""),
                            }
                        ],
                    }
                )

        _skipped_types: list[str] = []
        for item in remaining:
            item_type = item.get("type")
            if item_type not in (
                "message",
                "function_call",
                "function_call_output",
                "error",
            ):
                _skipped_types.append(str(item_type))
            if item_type == "message":
                result.append(
                    {
                        "type": "message",
                        "role": item.get("role", "user"),
                        "content": item.get("content", []),
                    }
                )
            elif item_type == "function_call":
                result.append(
                    {
                        "type": "function_call",
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
            elif item_type == "function_call_output":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id"),
                        "output": item.get("output"),
                    }
                )
            elif item_type == "error":
                error_message = item.get("message")
                code = item.get("code")
                source = item.get("source")
                result.append(
                    {
                        "type": "error",
                        "source": source if isinstance(source, str) and source else "execution",
                        "code": code if isinstance(code, str) and code else "error",
                        "message": (
                            error_message
                            if isinstance(error_message, str) and error_message
                            else "unknown error"
                        ),
                    }
                )
        if _skipped_types:
            _logger.warning(
                "_convert_raw_items_to_input: skipped %d items with types: %s",
                len(_skipped_types),
                _skipped_types,
                extra={"session_id": runner_primary_session_id()},
            )
        _logger.info(
            "_convert_raw_items_to_input: %d raw items → %d converted (compaction_idx=%s)",
            len(items),
            len(result),
            compaction_idx,
            extra={"session_id": runner_primary_session_id()},
        )
        return result

    def _extract_last_assistant_text(session_id: str) -> str:
        history = _session_histories.get(session_id, [])
        for item in reversed(history):
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text") or block.get("input_text")
                            if text:
                                parts.append(str(text))
                        elif isinstance(block, str):
                            parts.append(block)
                    return "\n".join(parts) if parts else ""
        return ""

    async def _handle_harness_compaction(
        conv: str,
        event: _JsonObject,
    ) -> None:
        summary = cast(str, event.get("summary", ""))
        token_count = cast(int, event.get("total_tokens") or 0)
        model = cast(str | None, event.get("summary_model"))
        last_item_id = _last_server_item_id.get(conv)

        if not last_item_id:
            _logger.warning(
                "Skipping harness compaction persist for %s: no "
                "server-side last_item_id available",
                conv,
                extra={"session_id": conv},
            )
            return

        compacted_messages = cast(list[_JsonObject] | None, event.get("compacted_messages"))
        compaction_event: _JsonObject = {
            "type": "compaction",
            "summary": summary,
            "last_item_id": last_item_id,
            "model": model,
            "token_count": token_count,
        }
        if compacted_messages:
            compaction_event["compacted_messages"] = compacted_messages
        try:
            await server_client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "compaction",
                    "data": compaction_event,
                },
                timeout=10.0,
            )
        except (httpx.HTTPError, RuntimeError):
            _logger.warning(
                "Failed to persist harness compaction item for %s",
                conv,
                exc_info=True,
                extra={"session_id": conv},
            )

        if compacted_messages:
            _session_histories[conv] = compacted_messages
        else:
            _session_histories[conv] = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "[Automatically generated summary of prior "
                                "conversation context.]\n\n"
                                "Please provide a summary of our conversation so far."
                            ),
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": summary,
                        }
                    ],
                },
            ]

    _CANCELLATION_TOOL_OUTPUT = "[Cancelled — tool execution was interrupted.]"
    _CANCELLATION_MARKER_TEXT = (
        "[System: interrupted]\n"
        "The user interrupted and abandoned their previous request (the user "
        "message immediately before this one). Do not resume or act on that "
        "interrupted request unless the user asks for it again; treat the next "
        "user message as the current instruction. The preceding assistant "
        "message may be incomplete."
    )

    def _append_cancellation_items(conv_id: str, *, turn_interrupted: bool = True) -> None:
        """Close the stopped turn's dangling calls and add the marker the next turn reads.

        The marker carries the Stop line when the server sent one; with *turn_interrupted*
        ``False`` (no turn was running) it is that line alone, and nothing without one.
        """
        note = _stop_notes.pop(conv_id, None)
        if not turn_interrupted and note is None:
            return
        history = _session_histories.get(conv_id, [])

        call_ids_with_output: set[str] = set()
        dangling_calls: list[_JsonObject] = []
        for item in history:
            itype = item.get("type")
            if itype == "function_call":
                cid = item.get("call_id")
                if cid:
                    dangling_calls.append(item)
            elif itype == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    call_ids_with_output.add(cast(str, cid))

        items_to_persist: list[_JsonObject] = []
        synthetic_items: list[_JsonObject] = []
        cached_spec_entry = _session_spec_cache.get(conv_id)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        agent_name = cached_spec.name if cached_spec else "unknown"
        for fc in dangling_calls:
            call_id = fc["call_id"]
            if call_id not in call_ids_with_output:
                fc_for_db = dict(fc)
                fc_for_db.setdefault("agent", agent_name)
                items_to_persist.append(fc_for_db)
                synthetic_output: _JsonObject = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _CANCELLATION_TOOL_OUTPUT,
                }
                synthetic_items.append(synthetic_output)
                items_to_persist.append(synthetic_output)

        if not turn_interrupted:
            marker_text = f"[System: stopped]\n{note}"
        else:
            marker_text = _CANCELLATION_MARKER_TEXT + (f"\n{note}" if note else "")
        marker: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": marker_text,
                }
            ],
        }
        synthetic_items.append(marker)
        items_to_persist.append(marker)

        _session_histories.setdefault(conv_id, []).extend(synthetic_items)

        loop = asyncio.get_running_loop()
        _task = loop.create_task(
            _persist_cancellation_items(conv_id, items_to_persist),
            name=f"persist-cancel-{conv_id}",
        )
        _task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_task)

    async def _persist_cancellation_items(
        conv_id: str,
        items: list[_JsonObject],
    ) -> None:
        import uuid as _uuid

        response_id = f"cancel_{_uuid.uuid4().hex}"
        for item in items:
            item_type = item.get("type", "message")
            item_data = {k: v for k, v in item.items() if k != "type"}
            try:
                await server_client.post(
                    f"/v1/sessions/{conv_id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": item_type,
                            "item_data": item_data,
                            "response_id": response_id,
                        },
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Failed to persist cancellation item for %s: %s",
                    conv_id,
                    item_type,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )

    async def _recover_sub_agent_name(conv_id: str) -> str | None:
        cached = _session_sub_agent_names.get(conv_id)
        if cached:
            return cached
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        name = snapshot.sub_agent_name if snapshot is not None else None
        if name:
            _session_sub_agent_names[conv_id] = name
        return name

    async def _ensure_subagent_work_entry(conv_id: str) -> _SubagentWorkEntry | None:
        existing = get_subagent_work(conv_id)
        if existing is not None:
            return existing
        if conv_id in _drained_delivered_subagent_children:
            return None
        try:
            snapshot = await _session_snapshot(conv_id)
        except Exception:  # noqa: BLE001 — best-effort recovery
            return None
        parent_id = snapshot.parent_session_id
        if not parent_id or parent_id == conv_id:
            return None
        agent = snapshot.sub_agent_name or snapshot.agent_name or "sub-agent"
        return register_subagent_work(
            parent_session_id=parent_id,
            child_session_id=conv_id,
            agent=agent,
            title=snapshot.sub_agent_name or "",
        )

    async def _parent_is_nested_subagent(entry: _SubagentWorkEntry) -> bool:
        """
        Return whether an undelivered result's parent is itself a sub-agent.

        A sub-agent parent that runs elsewhere never has an inbox here, so
        retrying its children's terminal status every 30 s buys nothing. The
        status is acknowledged and the entry kept, so a sub-agent parent that
        does run here later still receives it when its inbox is created
        (``_deliver_retained_subagent_results``). An unreadable parent
        snapshot reads as a top-level parent, so the retry contract still
        covers a parent that lives elsewhere or is re-initializing after a
        restart.

        :param entry: Terminal work entry whose parent inbox was missing.
        :returns: ``True`` when the parent's snapshot names its own parent.
        """
        snapshot = await _session_snapshot(entry.parent_session_id)
        return snapshot.ok and snapshot.parent_session_id is not None

    def _deliver_retained_subagent_results(parent_id: str) -> None:
        """
        Hand over results acknowledged while ``parent_id`` had no inbox here.

        A terminal child whose sub-agent parent had no inbox here is
        acknowledged with its entry kept undelivered. If that parent later
        runs on this runner, creating its inbox delivers those entries and
        wakes it, as the forwarder's pending retry used to the moment the inbox
        appeared. A parent that never runs here keeps the entry undelivered.
        Idempotent: delivered entries are skipped.

        :param parent_id: Parent whose inbox now exists, e.g. ``"conv_parent123"``.
        :returns: None.
        """
        for entry in list_subagent_work(parent_id):
            if entry.status not in _SUBAGENT_TERMINAL_STATUSES or entry.delivered:
                continue
            if _deliver_subagent_completion(entry).delivered_now:
                _schedule_subagent_wake(entry)

    async def _run_subagent_recovery(parent_id: str) -> None:
        """
        Re-queue terminal child results lost with a runner process restart.

        The parent inbox is a process-local queue, so a result queued before
        a restart but not yet drained would otherwise vanish. Runs once per
        parent per process; pending recovered work is refreshed by the periodic
        sweep. A failed server read is retried before the next ``sys_read_inbox``
        drain. The inbox is created here when missing: after a reconnect the
        server can dispatch a pending message before it re-initializes the session,
        and that turn's drain
        must still see the recovered results. Results acknowledged while this
        parent had no inbox here are handed over first, on every call.

        :param parent_id: Parent session whose inbox was recreated, e.g.
            ``"conv_parent123"``.
        :returns: None.
        """
        _session_inboxes.setdefault(parent_id, asyncio.Queue())
        _deliver_retained_subagent_results(parent_id)
        if parent_id in _subagent_recovery_done:
            return
        lock = _subagent_recovery_locks.setdefault(parent_id, asyncio.Lock())
        async with lock:
            if parent_id in _subagent_recovery_done:
                return
            try:
                await _recover_subagent_results_from_server(
                    server_client=server_client,
                    parent_id=parent_id,
                    schedule_wake=_schedule_subagent_wake,
                )
            except (httpx.HTTPError, _SubagentRecoveryReadError, ValueError):
                _logger.warning(
                    "Failed to recover undrained sub-agent results for %s",
                    parent_id,
                    exc_info=True,
                    extra={"session_id": parent_id},
                )
                return
            _subagent_recovery_done.add(parent_id)

    def _start_subagent_recovery(parent_id: str) -> asyncio.Task[None]:
        """Return the session-owned single-flight restart recovery task."""
        task = _subagent_recovery_tasks.get(parent_id)
        if task is not None and not task.done():
            return task
        _subagent_recovery_tasks.pop(parent_id, None)
        task = asyncio.create_task(
            _run_subagent_recovery(parent_id),
            name=f"subagent-recovery:{parent_id}",
        )
        _subagent_recovery_tasks[parent_id] = task
        _background_tasks.add(task)

        def _drop_completed_recovery(done: asyncio.Task[None]) -> None:
            _background_tasks.discard(done)
            if _subagent_recovery_tasks.get(parent_id) is done:
                _subagent_recovery_tasks.pop(parent_id, None)

        task.add_done_callback(_drop_completed_recovery)
        return task

    async def _cancel_subagent_recovery(parent_id: str) -> None:
        """Stop recovery before deleting its session-local inbox and markers."""
        task = _subagent_recovery_tasks.pop(parent_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _logger.warning(
                "Sub-agent recovery failed while deleting session %s",
                parent_id,
                exc_info=True,
                extra={"session_id": parent_id},
            )

    async def _recover_undrained_subagent_results(parent_id: str) -> None:
        """Await the session-owned single-flight restart recovery task."""
        await asyncio.shield(_start_subagent_recovery(parent_id))

    app.state.recover_undrained_subagent_results = _recover_undrained_subagent_results

    async def _reconcile_pending_subagent_results() -> None:
        """Refresh only recovered work with no local execution or completion edge."""
        parents = {
            entry.parent_session_id
            for entry in list(_subagent_work_by_child.values())
            if entry.status == "waiting"
        }
        for parent_id in parents:
            if not any(entry.status == "waiting" for entry in list_subagent_work(parent_id)):
                continue
            _subagent_recovery_done.discard(parent_id)
            await _recover_undrained_subagent_results(parent_id)

    app.state.reconcile_pending_subagent_results = _reconcile_pending_subagent_results

    def _note_session_harness_override(conv_id: str, harness_override: str | None) -> None:
        """Record the harness a session was forwarded, so reads match the run.

        A routed child arrives with ``harness_override`` naming a harness its
        (sub-)agent spec never declared — Smart Routing picks it on the first
        message. The ``"auto"`` sentinel is not a harness, so it is ignored.

        :param conv_id: Session/conversation id, e.g. ``"conv_child456"``.
        :param harness_override: The forwarded override, e.g. ``"pi"``.
        :returns: None.
        """
        if not harness_override or harness_override == "auto":
            return
        _session_harness_overrides[conv_id] = harness_override

    def _session_harness_name(conv_id: str) -> str | None:
        # The override wins: a routed session runs the harness the server
        # pinned, not the one its spec declares.
        override = _session_harness_overrides.get(conv_id)
        if override is not None:
            return canonicalize_harness(override) or override
        spec = _session_spec_cache.get(conv_id)
        if spec is None:
            return None
        h = spec.executor.config.get("harness") or spec.executor.type
        return canonicalize_harness(h) or h

    def _publish_turn_status(
        conv_id: str,
        status: str,
        error: Mapping[str, object] | None = None,
        *,
        source_error: Mapping[str, object] | None = None,
        response_id: str | None = None,
    ) -> None:
        if status == "waiting" and not (
            _server_version is not None and _version_supports_waiting_status(_server_version)
        ):
            status = "running"
        event: _JsonObject = {"type": "session.status", "status": status}
        turn = _turn_bind_epoch.get(conv_id)
        if turn is not None:
            # Lets a sender tell the end of the turn its message joined from an earlier one.
            event["turn"] = turn
        if error is not None:
            event["error"] = error
        if response_id is not None:
            # Name the turn this edge closes. The web UI already rendered the
            # response's own terminal error; the id lets it recognise this edge
            # as the same failure instead of adding a second card.
            event["response_id"] = response_id
        if status == "failed":
            harness = _session_harness_name(conv_id)
            source = source_error if source_error is not None else (error or {})
            dimensions: dict[str, str] = {}
            for key in ("code", "type", "status"):
                value = source.get(key)
                if (isinstance(value, int) and not isinstance(value, bool)) or (
                    isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,128}", value)
                ):
                    dimensions[f"source_{key}"] = str(value)
            # Canonical broken-turn signal: every failed turn shown in the UI
            # funnels through here, so log once at ERROR for the dashboard.
            _logger.error(
                "turn surfaced to UI as failed for %s (harness=%s): %s",
                conv_id,
                harness,
                error,
                extra=debug_event(
                    "runner_turn_failed",
                    session_id=conv_id,
                    harness=harness,
                    **dimensions,
                ),
            )
        _publish_event(conv_id, event)

    def _queue_changed(conv_id: str, body: Mapping[str, object]) -> None:
        """Tell clients *body* started or stopped waiting behind the turn (a refetch ping)."""
        item_id = body.get("persisted_item_id")
        if isinstance(item_id, str) and item_id:
            _publish_event(
                conv_id, {"type": "session.input.delivery", "data": {"item_id": item_id}}
            )

    def _waiting(conv_id: str) -> list[dict[str, Any]]:
        """The buffered messages waiting for the next turn: not (or no longer) offered."""
        buffered = _session_message_buffers.get(conv_id) or []
        return [m for m in buffered if not m.get("injection_id")]

    def _drop_message_buffer(conv_id: str) -> None:
        """Drop every message buffered behind *conv_id*'s turn (a stop or a delete)."""
        for body in _session_message_buffers.pop(conv_id, None) or []:
            _answer_offer(body.get("injection_id"), taken=False)
            _queue_changed(conv_id, body)

    def _remove_buffered(conv_id: str, injection_id: object) -> list[dict[str, Any]]:
        """Take the buffered messages forwarded as *injection_id* out of *conv_id*'s buffer."""
        buf = _session_message_buffers.get(conv_id) or []
        taken = [m for m in buf if m.get("injection_id") == injection_id]
        if taken:
            _session_message_buffers[conv_id] = [
                m for m in buf if m.get("injection_id") != injection_id
            ]
        return taken

    def _answer_offer(injection_id: object, *, taken: bool) -> None:
        """Answer the wait of an ``if_running`` message for the running turn's verdict."""
        if not isinstance(injection_id, str):
            return
        waiting = _offer_answers.pop(injection_id, None)
        if waiting is not None and not waiting[1].done():
            waiting[1].set_result(taken)

    async def _offer_verdict(injection_id: str, offer: asyncio.Future[bool]) -> bool:
        """The running turn's verdict on an ``if_running`` message; none in time is a no."""
        try:
            return await asyncio.wait_for(asyncio.shield(offer), _OFFER_VERDICT_TIMEOUT_S)
        except asyncio.TimeoutError:
            _answer_offer(injection_id, taken=False)
            return False

    def _answer_offers_of(conv_id: str) -> None:
        """The turn of *conv_id* ended: whatever it never answered was not taken."""
        for injection_id, (owner, _) in list(_offer_answers.items()):
            if owner == conv_id:
                _answer_offer(injection_id, taken=False)

    async def _teardown_session_terminals(conv_id: str) -> None:
        from omnigent.runner.tool_dispatch import _publish_terminal_deleted_event

        terminal_registry = resource_registry.terminal_registry
        if terminal_registry is None:
            return
        terminals = [
            (entry.terminal_name, entry.session_key)
            for entry in terminal_registry.list_for_conversation(conv_id)
        ]
        for terminal_name, session_key in terminals:
            terminal_id = terminal_resource_id(terminal_name, session_key)
            try:
                await resource_registry.close_terminal(conv_id, terminal_id)
            except (RuntimeError, OSError):
                _logger.warning(
                    "Failed to close terminal %s for session %s during stop",
                    terminal_id,
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )
            _publish_terminal_deleted_event(
                conversation_id=conv_id,
                terminal_name=terminal_name,
                session_key=session_key,
                publish_event=_publish_event,
            )

    def _is_sdk_compact_body(body: dict[str, Any]) -> bool:
        """Return whether a buffered body is the synthesized claude-sdk ``/compact``.

        The compact control is dispatched as a resumed turn whose sole content is
        the literal ``/compact`` slash command. The continuation drain uses this
        to dispatch a buffered ``/compact`` as its OWN turn (never coalesced
        behind a later message), so the SDK still sees it as the turn prompt and
        runs native compaction.
        """
        content = body.get("content")
        if not isinstance(content, list) or len(content) != 1:
            return False
        part = content[0]
        return (
            isinstance(part, dict)
            and part.get("type") == "input_text"
            and part.get("text") == "/compact"
        )

    async def _memory_labels(conv_id: str, labels: Mapping[str, str] | None) -> dict[str, str]:
        """*labels* with the sub-agent mark :func:`_tool_labels_for_session` derives from the
        parent. A session that cannot be read is marked too: memory is written only for a
        session known to be the Conversation (the next checkpoint heals a skipped copy)."""
        lineage = {**dict(labels or {}), **await _tool_labels_for_session(conv_id)}
        if not (await _session_snapshot(conv_id)).ok:
            lineage[SUBAGENT_LABEL_KEY] = "1"
        return lineage

    _memory_rebuilds: set[str] = set()

    def _schedule_memory_sync(conv_id: str, labels: Mapping[str, str] | None) -> None:
        """Heal ``memory/`` in the background: missing folder, new owner, a reset not yet seen.

        Then a bounded step of indexing the Conversation's turns up to its checkpoint's anchor
        (the ``conversation`` source), with the Project the chat runs in.
        """
        from omnigent.runner.memory.sync import backfill_turns, reconcile
        from omnigent.superchat.projects.card import project_slug_from_workspace

        if _memory_rebuilds:
            return
        _memory_rebuilds.add(conv_id)

        async def _run() -> None:
            try:
                # Stored labels never mark a Helper or a studio: the parent does (one rule for
                # tools and memory), so neither writes the Conversation's STATE.md or journal.
                lineage = await _memory_labels(conv_id, labels)
                await reconcile(server_client, conv_id, lineage)
                workspace = await _session_workspace_value(conv_id)
                await backfill_turns(
                    server_client,
                    conv_id,
                    lineage,
                    project=project_slug_from_workspace(workspace) or "",
                )
            finally:
                _memory_rebuilds.discard(conv_id)

        task = asyncio.create_task(_run(), name=f"memory-sync-{conv_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _handle_superside_chat_compact(conv_id: str) -> Response:
        """Compact a superside-chat session with Omnigent's own rollover.

        The Claude SDK's ``/compact`` stores a placeholder summary, not a
        checkpoint (and auto-compaction is off in this mode). The same
        :func:`~omnigent.superchat.rollover.roll_over_session` the engine's fold runs builds a
        real one here, over the runner's route to the session's model, then drops the warm
        client. It runs in the background (the summarizer outlives the server's forward
        timeout) and publishes the in-progress / completed events the client expects; returning
        200 makes the server skip its own compaction. The next turn's memory sync journals it.
        """
        from omnigent.superchat.rollover import roll_over_session

        model = await _superside_chat_session_model(conv_id)
        if model is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "superside_chat_compact_failed",
                    "detail": "Could not resolve the session's model.",
                },
            )
        labels = await _session_labels_for_runner_spawn(
            server_client=server_client, session_id=conv_id
        )
        wire, connection = _summarize_route(conv_id, model)
        _publish_event(conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id})

        async def _compact() -> None:
            rolled_over = False
            try:
                rolled_over = await roll_over_session(
                    conv_id,
                    labels=labels,
                    model=model,
                    server_client=server_client,
                    llm_client=_get_runner_llm_client(),
                    connection=connection,
                    wire=wire,
                    on_rolled_over=lambda: _drop_superside_chat_warm_client(conv_id),
                    cause="manual",
                )
            except Exception:
                _logger.warning(
                    "superside-chat /compact failed for %s",
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )
            outcome = "completed" if rolled_over else "failed"
            _publish_event(conv_id, {"type": f"response.compaction.{outcome}", "task_id": conv_id})

        task = asyncio.create_task(_compact(), name=f"superside-chat-compact-{conv_id}")
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return Response(status_code=200)

    async def _handle_claude_sdk_compact(conv_id: str) -> Response:
        """Compact a claude-sdk session by sending it the ``/compact`` command.

        The Claude SDK owns its own context window in the harness subprocess,
        so Omnigent-side transcript compaction is ineffective for it. The
        effective path is to send the literal ``/compact`` slash command to
        the live client, which runs native compaction — the same PreCompact
        path auto-compaction uses, whose ``response.compaction.completed`` the
        executor already emits. We do that by dispatching a resumed
        ``/compact`` turn: buffered behind an in-flight turn (the harness has a
        single client), started immediately otherwise. Returns 200 so the
        Omnigent server treats the control as handled and skips its own
        (transcript-only) compaction. A superside-chat session is compacted by
        Omnigent instead (``_handle_superside_chat_compact``).
        """
        if (await _rollover_labels_for_session(conv_id)).get(
            CONTEXT_MODE_LABEL
        ) == SUPERSIDE_CHAT_MODE_VALUE:
            return await _handle_superside_chat_compact(conv_id)
        compact_body: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "/compact"}],
            "conversation_id": conv_id,
        }
        # Serialize the active-turn check + slot bind through the same ingest
        # gate the message path uses. Without it, the idle branch's check and
        # its `_active_turns` bind straddle an `await` (history load), so a
        # message arriving in that window also sees the session idle and binds
        # the slot — the two turns then clobber each other's `_active_turns`
        # entry and race the single live SDK client. Under the gate one reaches
        # its bind before the other's check, so the loser buffers instead.
        _seq = _ingest_next_seq.get(conv_id, 0)
        _ingest_next_seq[conv_id] = _seq + 1
        _cond = _ingest_cond.get(conv_id)
        if _cond is None:
            _cond = asyncio.Condition()
            _ingest_cond[conv_id] = _cond
        async with _cond:
            while _ingest_now_serving.get(conv_id, 0) != _seq:
                await _cond.wait()
        try:
            # A turn is already running: buffer so /compact runs as the next turn
            # rather than racing the live one; the buffer drains via
            # _check_and_start_next_turn once the active turn ends. The buffered
            # compact's own in_progress comes from the executor when it later runs
            # — publishing an up-front spinner here would show "Compacting…" while
            # the prior turn is still working, so only the idle path does that.
            if conv_id in _active_turns:
                _session_message_buffers.setdefault(conv_id, []).append(compact_body)
                return Response(status_code=200)

            # Publish the compaction spinner up front so the UI shows "Compacting
            # conversation…" immediately, like the native handlers — the executor's
            # own in_progress fires only once the SDK PreCompact hook hits (mid-turn),
            # by which point the generic running status has shown "Working…". The
            # relay swallows the executor's later duplicate; `completed` (or the
            # turn-end `failed` fallback in _on_proxy_stream_end) clears the spinner.
            _publish_event(
                conv_id, {"type": "response.compaction.in_progress", "task_id": conv_id}
            )
            _sdk_compact_inprogress.add(conv_id)
            try:
                new_item: _JsonObject = {
                    "type": "message",
                    "role": "user",
                    "content": compact_body["content"],
                }
                if conv_id in _session_histories:
                    _session_histories[conv_id].append(new_item)
                else:
                    loaded = await _load_history_as_input(conv_id)
                    loaded.append(new_item)
                    _session_histories[conv_id] = loaded

                _begin_turn_slot(conv_id)
                _publish_turn_status(conv_id, "running")
                _turn_task = asyncio.create_task(
                    _run_turn_bg(compact_body, conv_id),
                    name=f"compact-{conv_id}",
                )
                _active_turns[conv_id] = _turn_task
                _turn_task.add_done_callback(_background_tasks.discard)
                _background_tasks.add(_turn_task)
            except Exception:
                # Never strand the spinner if the turn fails to start.
                _sdk_compact_inprogress.discard(conv_id)
                _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
                raise
            return Response(status_code=200)
        finally:
            async with _cond:
                _ingest_now_serving[conv_id] = _seq + 1
                _cond.notify_all()

    #: When each session's current turn started, until its first streamed token is logged.
    _turn_started_mono: dict[str, float] = {}
    #: The current turn's later marks, by session (see :func:`turn_timing`): ``prefix`` (setup
    #: done, the prefix starts), ``prefixed`` (the prefix is built), ``client`` (the harness
    #: holds a Claude CLI and asks to make the model call).
    _turn_marks: dict[str, dict[str, float]] = {}

    def _mark_turn(conv_id: str, mark: str) -> None:
        """Stamp *mark* on the session's current turn, once, while its first token is pending."""
        if conv_id in _turn_started_mono:
            _turn_marks.setdefault(conv_id, {}).setdefault(mark, time.monotonic())

    #: The first turn after boot (ADR 0026): runner boot and session init, in seconds, until the
    #: turn's first model output logs them with the harness's own wait; then cleared for good.
    _cold_start: dict[str, float] = {}
    _cold_start_logged: list[bool] = []

    def _note_cold_start_init(init_started: float) -> None:
        """Remember the first session init after boot, and how long the runner booted before it."""
        if _cold_start or _cold_start_logged:
            return
        with contextlib.suppress(psutil.Error, OSError):
            booted = time.time() - psutil.Process().create_time()
            _cold_start["runner"] = booted - (time.monotonic() - init_started)
        _cold_start["init"] = time.monotonic() - init_started

    def _log_cold_start(conv_id: str, harness_seconds: float) -> None:
        """Log, once per runner process, where the first turn's wait went."""
        if _cold_start_logged:
            return
        _cold_start_logged.append(True)
        _logger.info(
            "cold start for %s: runner %.1fs, session init %.1fs, harness %.1fs",
            conv_id,
            _cold_start.get("runner", 0.0),
            _cold_start.get("init", 0.0),
            harness_seconds,
            extra={"session_id": conv_id},
        )

    async def _log_first_token(conv_id: str) -> None:
        """Log, once per Conversation turn, how long the first streamed token took."""
        now = time.monotonic()
        started = _turn_started_mono.pop(conv_id, None)
        marks = _turn_marks.pop(conv_id, {})
        if started is None or not is_super_chat(await _tool_labels_for_session(conv_id)):
            return
        _logger.info(
            "first token for %s %.2fs after the turn started",
            conv_id,
            now - started,
            extra={"session_id": conv_id},
        )
        setup, prefix, client, first = turn_timing(started, marks, now)
        _logger.info(
            "turn timing for %s: setup %.2fs prefix %.2fs client %.2fs first token %.2fs",
            conv_id,
            setup,
            prefix,
            client,
            first,
            extra={"session_id": conv_id},
        )

    def _begin_turn_slot(conv_id: str) -> None:
        """Bind the ``None`` sentinel and stamp a fresh epoch for the new turn.

        Must be used instead of a bare ``_active_turns[conv] = None`` so recovery can detect
        a replacement turn that ran and finished during a teardown await.
        """
        _active_turns[conv_id] = None
        _turn_bind_epoch[conv_id] = next(_turn_epoch_seq)
        _turn_started_mono[conv_id] = time.monotonic()
        _turn_marks.pop(conv_id, None)

    def _release_live_turn_markers(conv_id: str) -> None:
        """Clear ``_live_response_id`` and the process-manager in-flight marker atomically.

        The two stores represent one fact; clearing only one leaves the idle reaper stuck.
        """
        _live_response_id.pop(conv_id, None)
        if process_manager is not None:
            process_manager.clear_in_flight(conv_id)

    def _sweep_dead_turn_slot(conv_id: str, occupant: asyncio.Task[None] | None) -> bool:
        """Remove a completed turn and all its per-turn tokens together (identity-guarded).

        Returns ``True`` if swept, ``False`` if a newer turn already owns the slot.
        """
        if _active_turns.get(conv_id) is not occupant:
            return False
        _active_turns.pop(conv_id, None)
        _release_live_turn_markers(conv_id)
        _interrupted_sessions.discard(conv_id)
        _answer_offers_of(conv_id)
        pending_approvals.cancel_session(conv_id, "turn_swept")
        return True

    def _on_proxy_stream_end(
        conv_id: str,
        *,
        error: dict[str, Any] | None = None,
        owner_response_id: str | None = None,
    ) -> None:
        # Stale-finalizer guard: when owner_response_id no longer matches the live response,
        # a newer turn has taken over — skip all conversation-state mutations.
        if owner_response_id is not None and _live_response_id.get(conv_id) != owner_response_id:
            _logger.debug(
                "proxy stream end for %s ignored: response %s superseded by %s",
                conv_id,
                owner_response_id,
                _live_response_id.get(conv_id),
                extra={"session_id": conv_id},
            )
            return
        # The turn is over: an ``if_running`` message it never answered was not taken.
        _answer_offers_of(conv_id)

        _active_turns.pop(conv_id, None)
        _release_live_turn_markers(conv_id)
        # A claude-sdk `/compact` turn that ended without emitting
        # `response.compaction.completed` produced no compaction (nothing to
        # compact, or the turn failed before/without streaming). Clear the
        # up-front spinner with `failed` here — the single turn-end convergence
        # point, reached on every exit path (clean end, setup error, cancel) — so
        # no path strands the spinner or leaks the flag into a later turn's
        # compaction signalling. A successful compaction already discarded the
        # flag on `response.compaction.completed`, making this a no-op then.
        if conv_id in _sdk_compact_inprogress:
            _sdk_compact_inprogress.discard(conv_id)
            _publish_event(conv_id, {"type": "response.compaction.failed", "task_id": conv_id})
        # Transport-loss ending desyncs harness from runner; flag for clean rebind.
        if error is not None and error.get("code") == "connection_error":
            _desynced_sessions.add(conv_id)
        # A message meant only for this turn never runs after it, so it holds nothing back.
        has_buffered = any(
            not m.get(_ONLY_RUNNING_TURN) for m in _session_message_buffers.get(conv_id) or []
        )
        was_interrupted = conv_id in _interrupted_sessions
        # Suppress terminal only if desync recovery claimed the token for THIS generation's epoch.
        _suppress_status = _desync_terminalized.get(conv_id) == _turn_bind_epoch.get(conv_id, 0)
        if _suppress_status:
            _desync_terminalized.pop(conv_id, None)
        if was_interrupted:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            if not has_buffered and not _suppress_status:
                _publish_turn_status(conv_id, "idle")
        elif error is not None:
            normalized_error = _normalize_turn_error(error)
            if not _suppress_status:
                _publish_turn_status(
                    conv_id,
                    "failed",
                    error=normalized_error,
                    source_error=error,
                    response_id=owner_response_id,
                )
        else:
            if not has_buffered and not _suppress_status:
                _publish_turn_status(
                    conv_id, "waiting" if _has_live_child_helpers(conv_id) else "idle"
                )
        if was_interrupted:
            if conv_id in _desynced_sessions and not has_buffered:
                # This turn was torn down by desync recovery (which sets the
                # interrupt marker to unwind the harness), NOT by a user
                # interrupt — and it publishes a terminal desync ``failed``. Report
                # the sub-agent FAILED so the parent wake/result matches that
                # ``failed``, rather than a contradictory ``cancelled``.
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="failed",
                    output="Error: sub-agent turn failed: runner turn-context desync.",
                )
            else:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="cancelled",
                    output=None,
                )
        elif error is not None:
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="failed",
                output=f"Error: sub-agent turn failed: {error.get('message', 'unknown')}",
            )
        elif (
            not has_buffered
            # A Helper whose turn ends while its own child Helpers still run is waiting, not
            # finished: each child's result wakes it, and only a turn ending with none live
            # delivers the result to its parent.
            and not _has_live_child_helpers(conv_id)
        ):
            _mark_subagent_terminal_and_wake(
                conv_id,
                status="completed",
                output=_extract_last_assistant_text(conv_id),
            )
        try:
            loop = asyncio.get_running_loop()
            _cont = loop.create_task(
                _check_and_start_next_turn(conv_id),
            )
            _cont.add_done_callback(_background_tasks.discard)
            _background_tasks.add(_cont)
        except RuntimeError:
            pass

    async def _cancel_active_turn(
        conv_id: str, expected_task: asyncio.Task[None] | None = None
    ) -> bool:
        turn_task = _active_turns.get(conv_id)
        if not isinstance(turn_task, asyncio.Task):
            return False
        if turn_task.done():
            # A completed generation left in the slot is a CORPSE (same class as
            # _cancel_inprocess_turn's done-task handling). This IS reachable: a
            # live task cancel-forwarded by _cancel_inprocess_turn can COMPLETE
            # during the intervening _forward_harness_interrupt await, arriving
            # here done — and it carries an _interrupted_sessions token that must
            # be cleared or it taints the next turn. Sweep it (tokens included),
            # honoring expected_task.
            if expected_task is None or turn_task is expected_task:
                _sweep_dead_turn_slot(conv_id, turn_task)
            return False
        if expected_task is not None and turn_task is not expected_task:
            return False
        turn_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn_task
        if _active_turns.get(conv_id) is turn_task:
            _on_proxy_stream_end(conv_id)
            return True
        if conv_id in _interrupted_sessions:
            _interrupted_sessions.discard(conv_id)
            _append_cancellation_items(conv_id)
            # A turn torn down by desync recovery publishes a desync `failed`, so
            # its sub-agent must be reported FAILED, not a contradictory
            # `cancelled`, for the parent wake/result.
            if conv_id in _desynced_sessions:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="failed",
                    output="Error: sub-agent turn failed: runner turn-context desync.",
                )
            else:
                _mark_subagent_terminal_and_wake(
                    conv_id,
                    status="cancelled",
                    output=None,
                )
        return True

    async def _forward_harness_interrupt(conv_id: str) -> None:
        """Best-effort POST ``{"type":"interrupt"}`` to a conversation's harness.

        Releases the harness's parked policy/tool future so its ``run_turn``
        unwinds. A dead or wedged harness logs and is swallowed — the
        runner-side floor does not depend on this succeeding.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        """
        if process_manager is None:
            return
        try:
            harness_client = await process_manager.get_client(conv_id, "any")
            await harness_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "interrupt"},
                # Bounded under the Omnigent server's 5s stop deadline.
                timeout=3.0,
            )
        except NoLiveHarnessError:
            _logger.debug(
                "Interrupt forward skipped for %s: no live harness",
                conv_id,
                extra={"session_id": conv_id},
            )
        except Exception:
            _logger.warning(
                "Interrupt forward to harness failed for %s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )

    async def _write_stop_note(conv_id: str) -> None:
        """No turn was left to stop: the Stop line still goes in the history, and the warm
        harness session is dropped so the next turn replays that history and reads it."""
        if conv_id not in _stop_notes:
            return
        _append_cancellation_items(conv_id, turn_interrupted=False)
        _forget_superside_chat_prefix(conv_id)
        await _forward_harness_interrupt(conv_id)

    async def _cancel_inprocess_turn(conv_id: str, note: str | None = None) -> None:
        """Stop *conv_id*'s turn. *note* is the Stop line, written into its history."""
        if note:
            _stop_notes[conv_id] = note
        # Distinguish "no live turn" (absent) from a stream-mode turn (present as
        # the None sentinel — driven by the AP request's consumption of
        # proxy_stream, so the runner owns no cancellable Task). Both a live Task
        # and the sentinel have a live harness turn parked on a future, so the
        # interrupt must be forwarded for either.
        # An interrupted turn's approval prompts can no longer be answered into it.
        pending_approvals.cancel_session(conv_id, "interrupted")
        # Its running tools stop too: the registry's shield is for a tunnel replacement only.
        stopped_tools = mcp_execution_registry.cancel_turn(conv_id)
        if stopped_tools:
            _logger.info(
                "interrupt cancelled %d running tool(s) for %s",
                stopped_tools,
                conv_id,
                extra={"session_id": conv_id},
            )
        if conv_id not in _active_turns:
            await _write_stop_note(conv_id)
            return
        target = _active_turns.get(conv_id)
        if isinstance(target, asyncio.Task) and target.done():
            # A done Task is a corpse, not a live turn. Leaving it wedges every
            # ``conv in _active_turns`` liveness check (the buffer gate would strand
            # later messages) — sweep it, tokens included.
            _sweep_dead_turn_slot(conv_id, target)
            await _write_stop_note(conv_id)
            return
        _interrupted_sessions.add(conv_id)
        # The executor drops its client on an interrupt: the next turn starts cold.
        _forget_superside_chat_prefix(conv_id)
        await _forward_harness_interrupt(conv_id)
        # Floor: force-cancel the runner Task when we own one. In stream mode
        # there is no Task here — ``_resync_turn_state`` owns the sentinel pop,
        # and direct interrupt/stop callers rely on the forwarded interrupt
        # ending proxy_stream.
        if isinstance(target, asyncio.Task):
            await _cancel_active_turn(conv_id, expected_task=target)

    async def _after_pause_hold(conv_id: str, call: Coroutine[Any, Any, Any]) -> Any:
        """Run a runner-dispatched tool call once a paused Helper is resumed (ADR 0027).

        An expired pause drops the call: the Stop that follows ends the turn waiting on it.
        """
        if not await mcp_execution_registry.hold(conv_id):
            call.close()
            return None
        return await call

    def _pause_session(conv_id: str, since: float | None = None) -> bool:
        """Pause *conv_id* in the registry: held at its next tool boundary, stopped after
        :data:`PAUSE_LIMIT_S` (counted from *since*, the label's time, when restored)."""
        return mcp_execution_registry.pause(
            conv_id,
            on_hold=lambda sid: _publish_session_status(sid, "running", PAUSED_NOTE, paused=True),
            on_expired=_stop_paused_helper,
            since=since,
        )

    async def _restore_pause(conv_id: str) -> None:
        """At a Helper's turn start, pause it again from its ``superchat.paused`` label (a
        runner restart forgets the in-memory pause). Never raises."""
        if mcp_execution_registry.is_paused(conv_id):
            return
        try:
            if not (await _session_snapshot(conv_id)).parent_session_id:
                return
            labels = await _session_labels_for_runner_spawn(
                server_client=server_client, session_id=conv_id
            )
            since = float((labels or {}).get(PAUSED_LABEL_KEY) or 0)
        except Exception:
            _logger.warning(
                "could not read the pause of %s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )
            return
        if since > 0:
            _pause_session(conv_id, since)

    async def _stop_paused_helper(conv_id: str) -> None:
        """A Helper paused for :data:`PAUSE_LIMIT_S` unresumed (ADR 0027): stop it as Stop does
        (the engine's interrupt, which reaches :func:`_cancel_inprocess_turn`), clear its paused
        label, and tell its Conversation in one runtime notice. Never raises.
        """
        from omnigent.superchat.helpers.notices import post_notice

        try:
            resp = await server_client.get(f"/v1/sessions/{conv_id}", timeout=10.0)
            info = resp.json() if resp.status_code == 200 else {}
            stopped = await server_client.post(
                f"/v1/sessions/{conv_id}/events", json={"type": "interrupt"}, timeout=10.0
            )
            if stopped.status_code >= 400:
                await _cancel_inprocess_turn(conv_id)
            await server_client.patch(
                f"/v1/sessions/{conv_id}",
                json={"labels": {PAUSED_LABEL_KEY: ""}},
                timeout=10.0,
            )
            parent = info.get("parent_session_id") if isinstance(info, dict) else None
            if isinstance(parent, str) and parent:
                title = str(info.get("title") or "").strip() or "A helper"
                await post_notice(
                    server_client,
                    parent,
                    f"{title} was paused for 30 minutes and stopped.",
                    if_running=False,
                    helper_id=conv_id,
                )
        except Exception:
            _logger.warning(
                "could not stop paused helper %s",
                conv_id,
                exc_info=True,
                extra={"session_id": conv_id},
            )

    async def _resync_turn_state(
        conv_id: str, reason: str, *, owner_response_id: str | None = None
    ) -> None:
        """Single ordered recovery entry for a harness↔runner desync.

        Marks the conversation desynced, clears the stale live-response marker,
        tears the wedged turn down, and either drains a buffered continuation or
        publishes one terminal desync ``failed``. Cancelling the turn unwinds
        ``run_turn``, releasing the harness's parked policy future in
        milliseconds instead of at ``_POLICY_EVAL_TIMEOUT_S``.

        Idempotent: ``_cancel_inprocess_turn`` no-ops with no turn in flight and
        ``_interrupted_sessions`` is the existing idempotency token, so a
        duplicate signal for the same wedged turn collapses to one recovery.

        Generation-ownership gate: a desync signal names the turn that produced
        it (its ``owner_response_id``). A delayed or duplicate signal from an
        OLD response must not cancel whichever newer turn is now active, so when
        ``owner_response_id`` is supplied and no longer matches the live
        response, this is a stale signal — no-op. Signals with no owning response
        (e.g. a conversation-level path) pass ``None`` and always recover.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param reason: Short machine reason for the desync, logged for ops.
        :param owner_response_id: The response id the signal belongs to; when it
            no longer matches ``_live_response_id[conv_id]`` a newer turn has
            taken over and the signal is ignored.
        """
        if owner_response_id is not None and _live_response_id.get(conv_id) != owner_response_id:
            _logger.debug(
                "resync for %s ignored: response %s superseded by %s",
                conv_id,
                owner_response_id,
                _live_response_id.get(conv_id),
                extra={"session_id": conv_id},
            )
            return
        _logger.warning(
            "resyncing turn state for %s: %s",
            conv_id,
            reason,
            extra={"session_id": conv_id},
        )
        _desynced_sessions.add(conv_id)
        # Capture entry epoch; an advance during teardown means a replacement ran.
        _entry_epoch = _turn_bind_epoch.get(conv_id, 0)
        # Release markers before any await so concurrent callers see no live turn.
        _release_live_turn_markers(conv_id)
        # Pre-claim terminal token for this epoch; authoritative decision re-made after teardown.
        if not _session_message_buffers.get(conv_id):
            _desync_terminalized[conv_id] = _entry_epoch
        # Stream-sentinel turns have no Task — pop synchronously so the gate doesn't stay stuck.
        stream_sentinel = conv_id in _active_turns and not isinstance(
            _active_turns.get(conv_id), asyncio.Task
        )
        if stream_sentinel:
            _active_turns.pop(conv_id, None)
            await _forward_harness_interrupt(conv_id)
        else:
            await _cancel_inprocess_turn(conv_id)
        # Epoch advanced → a replacement ran (covers live-slot and empty-slot after teardown).
        _continuation_ran = _turn_bind_epoch.get(conv_id, 0) != _entry_epoch
        _has_buffer = bool(_session_message_buffers.get(conv_id))
        if not _continuation_ran and not _has_buffer:
            _publish_turn_status(
                conv_id,
                "failed",
                error={
                    "code": _RUNNER_TURN_CONTEXT_DESYNC_CODE,
                    "message": (
                        "The agent turn was interrupted by a harness desync and "
                        "could not be recovered. Please send your message again."
                    ),
                },
            )
        else:
            # A continuation owns the terminal status — release our claim (compare-and-pop
            # so a nested recovery's higher-epoch claim isn't accidentally stripped).
            if _desync_terminalized.get(conv_id) == _entry_epoch:
                _desync_terminalized.pop(conv_id, None)
            # Kick a continuation if none ran; a cancelled mid-drain turn doesn't schedule one.
            if _has_buffer and not _continuation_ran:
                try:
                    loop = asyncio.get_running_loop()
                    _cont = loop.create_task(_check_and_start_next_turn(conv_id))
                    _cont.add_done_callback(_background_tasks.discard)
                    _background_tasks.add(_cont)
                except RuntimeError:
                    pass

    def _recover_failed_tool_dispatch(
        dispatch_task: asyncio.Task[object], *, conv_id: str, response_id: str
    ) -> None:
        if dispatch_task.cancelled() or dispatch_task.exception() is None:
            return
        # Recovery can cancel a turn awaiting this dispatch task. Run it
        # independently so teardown cannot await or cancel itself.
        task = asyncio.create_task(
            _resync_turn_state(conv_id, "tool_dispatch_failed", owner_response_id=response_id)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _resync_turn_state_on_delivery_failure(
        conv_id: str, response_id: str | None
    ) -> None:
        """``on_delivery_failure`` adapter binding the desync reason + owner.

        Carries the response id of the turn whose verdict delivery failed so a
        delayed or duplicate failure from an old response cannot cancel a newer
        active turn (the ownership gate in :func:`_resync_turn_state`).

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param response_id: The response id of the turn whose verdict delivery
            failed, or ``None`` if the eval fired before ``response.created``.
        """
        await _resync_turn_state(
            conv_id, "verdict_delivery_channel_dead", owner_response_id=response_id
        )

    async def _resync_turn_state_on_harness_respawn(
        conv_id: str, reason: str, replaced_response_id: str
    ) -> None:
        """``HarnessProcessManager`` respawn-hook adapter for ``_resync_turn_state``.

        A respawn while the replaced response is still bound is the deterministic respawn-desync:
        the inner generation dies with the subprocess and the slot is never cleaned.
        Recovering here collapses the window instead of waiting for the orphan backstop.

        Two gates keep it from cancelling a HEALTHY turn:

        1. ``conv_id in _active_turns`` — a respawn with no bound turn is a no-op.
        2. ``_live_response_id[conv_id] == replaced_response_id`` — the process
           manager only fires when the replaced process was mid-response, but by
           the time this runs the wedged turn may already have ended and a NEW
           turn bound under the same conversation (whose ``get_client`` triggered
           the respawn). Cancelling on the bare conversation would then clobber
           that new turn. Identity-match the replaced response so only the turn
           that actually lost its subprocess is torn down; a mismatch means the
           new turn owns the slot and must be left alone.

        :param conv_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
        :param reason: Machine reason from the process manager, e.g.
            ``"harness_respawn_model_switch"``.
        :param replaced_response_id: The in-flight response id of the process
            that was torn down, for identity-matching the bound turn.
        """
        if conv_id not in _active_turns:
            return
        # The identity match against the replaced response is enforced centrally
        # by ``_resync_turn_state``'s ownership gate — a fresh turn that took the
        # slot has a different live response id and is left alone.
        await _resync_turn_state(conv_id, reason, owner_response_id=replaced_response_id)

    # Test seams: the real signals originate in a harness verdict POST failure
    # and inside the process manager's get_client, neither scriptable in-process.
    app.state.resync_turn_state = _resync_turn_state
    app.state.resync_turn_state_on_harness_respawn = _resync_turn_state_on_harness_respawn
    app.state.on_proxy_stream_end = _on_proxy_stream_end
    # Test seam: bind a turn slot with a fresh (non-repeating) bind epoch, so a
    # test can simulate a same-id recreate binding a new lifetime's turn.
    app.state.begin_turn_slot = _begin_turn_slot
    # hasattr guard: alternate/stub process managers need not implement the hook
    # — they simply fall back to the orphan-callback backstop.
    if process_manager is not None and hasattr(process_manager, "set_respawn_hook"):
        process_manager.set_respawn_hook(_resync_turn_state_on_harness_respawn)

    async def _check_and_start_next_turn(
        session_id: str,
    ) -> None:
        _seq = _ingest_next_seq.get(session_id, 0)
        _ingest_next_seq[session_id] = _seq + 1
        _cond = _ingest_cond.get(session_id)
        if _cond is None:
            _cond = asyncio.Condition()
            _ingest_cond[session_id] = _cond
        async with _cond:
            while _ingest_now_serving.get(session_id, 0) != _seq:
                await _cond.wait()
        try:
            if session_id in _active_turns:
                return

            buf = _session_message_buffers.get(session_id)
            if not buf:
                _rewake_parent_if_inbox_stranded(session_id)
                return

            # A message meant only for the turn that ended never runs as a turn of its own.
            for _only in [m for m in buf if m.get(_ONLY_RUNNING_TURN)]:
                _remove_buffered(session_id, _only.get("injection_id"))
            buf = _session_message_buffers.get(session_id)
            if not buf:
                return

            # A buffered claude-sdk /compact must dispatch as its OWN turn: the
            # SDK runs native compaction only when /compact is the turn prompt,
            # but the default non-native drain coalesces the whole buffer and
            # dispatches only the last body — burying a /compact behind a later
            # message and silently no-opping it (the runner already returned 200,
            # so the server won't fall back). Drain one at a time whenever a
            # /compact is buffered, so each lands as its own turn.
            # Likewise one at a time when a message carries more than text, so each is a turn
            # with all of its own content and turn-start context.
            if any(_is_sdk_compact_body(b) for b in buf) or not all(
                _is_steerable(b.get("content")) for b in buf
            ):
                next_body = buf.pop(0)
                if not buf:
                    _session_message_buffers.pop(session_id, None)
                _queue_changed(session_id, next_body)
                _session_histories.setdefault(session_id, []).append(
                    {
                        "type": "message",
                        "role": next_body.get("role", "user"),
                        "content": next_body.get("content", []),
                    }
                )
            else:
                all_bodies = list(buf)
                buf.clear()
                _session_message_buffers.pop(session_id, None)

                for body in all_bodies:
                    _session_histories.setdefault(session_id, []).append(
                        {
                            "type": "message",
                            "role": body.get("role", "user"),
                            "content": body.get("content", []),
                        }
                    )
                    _queue_changed(session_id, body)
                next_body = all_bodies[-1]

            if _is_sdk_compact_body(next_body):
                # This buffered /compact now dispatches as its own turn. Mirror the
                # idle path: publish the spinner up front AND set the flag, so
                # (a) the relay swallows the executor's own duplicate in_progress
                # (single spinner), and (b) _on_proxy_stream_end publishes `failed`
                # if the turn ends without a `response.compaction.completed` (a real
                # executor path — the compaction-complete event can be None) rather
                # than stranding the spinner. The prior turn has ended, so showing
                # "Compacting…" now is accurate.
                _publish_event(
                    session_id,
                    {"type": "response.compaction.in_progress", "task_id": session_id},
                )
                _sdk_compact_inprogress.add(session_id)
            _begin_turn_slot(session_id)
            _publish_turn_status(session_id, "running")
            _turn_task = asyncio.create_task(
                _run_turn_bg(next_body, session_id),
                name=f"turn-cont-{session_id}",
            )
            _active_turns[session_id] = _turn_task
            _turn_task.add_done_callback(
                _background_tasks.discard,
            )
            _background_tasks.add(_turn_task)
        finally:
            async with _cond:
                _ingest_now_serving[session_id] = _seq + 1
                _cond.notify_all()

    app.state.check_and_start_next_turn = _check_and_start_next_turn

    async def _post_subagent_wake_notice(
        parent_id: str,
        notice: str,
        child_id: str,
        created_by: str | None,
        *,
        is_rewake: bool = False,
        inlined: Sequence[tuple[str, str]] = (),
    ) -> None:
        delivered = await _deliver_subagent_wake_post(
            server_client, parent_id, notice, created_by=created_by, helper_id=child_id
        )
        if delivered:
            # The wake carried these Results (child id, dispatch id) itself, so the
            # Conversation holds them now: write the receipt a drain would, or a restarted
            # runner's recovery posts them again.
            from omnigent.runner.tool_dispatch import _record_subagent_receipt

            for inlined_child, work_id in inlined:
                await _record_subagent_receipt(server_client, inlined_child, work_id)
                forget_saved_files(inlined_child, _subagent_children)
            forget_saved_files(child_id, _subagent_children)
            _stranded_wake_parents.discard(parent_id)
            if is_rewake:
                _last_rewake_notice[parent_id] = notice
        else:
            # Not delivered: the Results go again with the re-attempted wake.
            for inlined_child, work_id in inlined:
                if (member := get_subagent_work(inlined_child)) and member.work_id == work_id:
                    member.wake_inlined = False
            _subagent_wake_pending.discard(parent_id)
            _stranded_wake_parents.add(parent_id)
            _logger.warning(
                "Sub-agent wake POST failed for parent=%s child=%s after %d attempt(s); "
                "result remains in the parent inbox; the wake will be re-attempted "
                "after the next tunnel reconnect or parent turn",
                parent_id,
                child_id,
                _WAKE_POST_MAX_ATTEMPTS,
                extra={"session_id": runner_primary_session_id()},
            )

    def _schedule_subagent_wake(entry: _SubagentWorkEntry, *, is_rewake: bool = False) -> None:
        if entry.parent_session_id == entry.child_session_id:
            return
        inbox = _session_inboxes.get(entry.parent_session_id)
        if inbox is None:
            return
        if entry.parent_session_id in _subagent_wake_pending:
            return
        # Children started in one parent turn wake it once: the last of them to settle
        # carries every Result of the group (each one already sits in the inbox).
        if subagent_group_waits(entry):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        inlined: list[_SubagentWorkEntry] = []
        if entry.superside_chat:
            # Inline the Results themselves rather than only "N results waiting"
            # (docs/helpers.md); sys_read_inbox
            # later sees entry.wake_inlined and won't repeat them.
            inlined = [
                other
                for other in list_subagent_work(entry.parent_session_id)
                if other is not entry
                and entry.group is not None
                and other.group == entry.group
                and other.delivered
                and not other.wake_inlined
                and other.status in _SUBAGENT_TERMINAL_STATUSES
            ] + [entry]
            notices = [
                format_subagent_wake_notice_with_result(
                    agent=member.agent,
                    title=member.title,
                    status=member.status,
                    child_session_id=member.child_session_id,
                    result_text=member.output,
                    saved_files=format_saved_files(
                        collect_saved_files(member.child_session_id, _subagent_children)
                    ),
                )
                for member in inlined
            ]
            # Progress the parent was idle for rides on this wake: one line per other Helper.
            waiting = take_waiting_progress(
                entry.parent_session_id, done=[member.child_session_id for member in inlined]
            )
            notice = "\n".join([*waiting, "\n\n".join(notices)])
            for member in inlined:
                member.wake_inlined = True
        else:
            notice = _format_subagent_wake_notice(
                agent=entry.agent,
                title=entry.title,
                status=entry.status,
                pending=inbox.qsize(),
            )
        if is_rewake and notice == _last_rewake_notice.get(entry.parent_session_id):
            return
        _subagent_wake_pending.add(entry.parent_session_id)
        _wake_task = loop.create_task(
            _post_subagent_wake_notice(
                entry.parent_session_id,
                notice,
                entry.child_session_id,
                entry.created_by,
                is_rewake=is_rewake,
                inlined=[(member.child_session_id, member.work_id) for member in inlined],
            )
        )
        _wake_task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_wake_task)

    def _rewake_parent_if_inbox_stranded(parent_session_id: str) -> None:
        inbox = _session_inboxes.get(parent_session_id)
        drained = inbox is None or inbox.empty()
        if drained:
            # A drained inbox ends the stranding episode, so the recorded
            # re-wake no longer describes outstanding work; forget it or a
            # later episode's matching notice is wrongly deduped.
            _last_rewake_notice.pop(parent_session_id, None)
            _stranded_wake_parents.discard(parent_session_id)
        # A parent whose wake POST exhausted its retries has no pending flag,
        # but its inbox still holds an undelivered result — rescue it too.
        stranded_retry = parent_session_id in _stranded_wake_parents
        if parent_session_id not in _subagent_wake_pending and not stranded_retry:
            return
        _subagent_wake_pending.discard(parent_session_id)
        _stranded_wake_parents.discard(parent_session_id)
        if drained:
            return
        entries = list_subagent_work(parent_session_id)
        if not entries:
            return
        latest = max(
            entries,
            key=lambda entry: entry.completed_at if entry.completed_at is not None else 0.0,
        )
        _schedule_subagent_wake(latest, is_rewake=True)

    async def _retry_stranded_wakes_soon() -> None:
        # Paced rounds: a failed re-attempt lands the parent back in
        # _stranded_wake_parents (see _post_subagent_wake_notice), so a later
        # round picks it up once the handshake has had more time. In-flight
        # attempts are deduped by _subagent_wake_pending. Recovery is bounded:
        # a parent still failing after the last round stays stranded until the
        # NEXT tunnel reconnect or an explicit parent turn re-attempts it.
        for delay_s in _STRANDED_WAKE_RETRY_DELAYS_S:
            await _wake_retry_sleep(delay_s)
            if not _stranded_wake_parents:
                return
            for parent_id in list(_stranded_wake_parents):
                _stranded_wake_parents.discard(parent_id)
                inbox = _session_inboxes.get(parent_id)
                if inbox is None or inbox.empty():
                    continue
                entries = list_subagent_work(parent_id)
                if not entries:
                    continue
                latest = max(
                    entries,
                    key=lambda entry: (
                        entry.completed_at if entry.completed_at is not None else 0.0
                    ),
                )
                _logger.info(
                    "Re-attempting stranded sub-agent wake for parent=%s after reconnect",
                    parent_id,
                    extra={"session_id": runner_primary_session_id()},
                )
                # Deliberately not is_rewake=True: skip the _last_rewake_notice
                # dedup so the notice always re-sends after a reconnect; the
                # drained-inbox check above prevents true duplicates.
                _schedule_subagent_wake(latest)

    def _retry_stranded_wakes() -> None:
        # A wake POST that exhausted its bounded retries (the server was down
        # when the child finished) left the result in the parent's inbox with
        # nothing scheduled to re-deliver it — the wake is the sole delivery
        # signal for an idle parent. The tunnel just reconnected, so the
        # server is reachable again: re-attempt one wake per stranded parent
        # whose inbox still holds results.
        if not _stranded_wake_parents:
            return
        if _stranded_wake_retry_task and not _stranded_wake_retry_task[0].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _retry_task = loop.create_task(_retry_stranded_wakes_soon())
        _stranded_wake_retry_task[:] = [_retry_task]

        def _clear_retry_refs(task: asyncio.Task[None]) -> None:
            _background_tasks.discard(task)
            if _stranded_wake_retry_task and _stranded_wake_retry_task[0] is task:
                _stranded_wake_retry_task.clear()
            # Surface an unexpected failure instead of a GC-time warning;
            # recovery degrades to the next reconnect or explicit parent turn.
            if not task.cancelled() and task.exception() is not None:
                _logger.warning(
                    "Stranded sub-agent wake retry loop failed: %r",
                    task.exception(),
                    extra={"session_id": runner_primary_session_id()},
                )

        _retry_task.add_done_callback(_clear_retry_refs)
        _background_tasks.add(_retry_task)

    def _has_live_child_helpers(conv_id: str) -> bool:
        """Whether ``conv_id`` started sub-agents that have not reached a terminal status."""
        return any(
            (e := _subagent_work_by_child.get(c)) is not None
            and e.status in _SUBAGENT_LIVE_STATUSES
            for c in _subagent_work_by_parent.get(conv_id, set())
        )

    def _mark_subagent_terminal_and_wake(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
    ) -> _SubagentDeliveryAck:
        ack = mark_subagent_work_terminal(
            child_session_id,
            status=status,
            output=output,
            only_if_work_id=only_if_work_id,
        )
        if ack.entry is not None and ack.delivered_now:
            _schedule_subagent_wake(ack.entry)
        return ack

    # Seam for the entrypoint's launch reaper (and tests): terminal delivery
    # that also schedules the parent wake POST, not just the inbox insert.
    app.state.mark_subagent_terminal_and_wake = _mark_subagent_terminal_and_wake

    def _subagent_work_id_for_session(conv_id: str) -> str | None:
        entry = get_subagent_work(conv_id)
        return entry.work_id if entry is not None else None

    def _remember_rollover_labels(session_id: str, labels: Mapping[str, str] | None) -> None:
        """Cache just the rollover-mode label from a full label set, once known.

        Warms :data:`_session_rollover_labels_cache` for free wherever full
        session labels are already being fetched for another reason (relay
        start, codex spawn-env), so ``_rollover_labels_for_session``
        rarely needs its own round trip.
        """
        if labels is None:
            return
        # Fixed at creation: the mode, the kept-tail budget, the file a studio
        # session edits (ADR 0024), and the background run's task (a Lookout or brief
        # run is offered its own tools, e.g. ``mail_read_thread``; ADR 0039).
        _session_rollover_labels_cache[session_id] = {
            key: labels[key]
            for key in (
                CONTEXT_MODE_LABEL,
                ROLLOVER_KEEP_TOKENS_LABEL,
                ARTIFACT_CHAT_LABEL_KEY,
                *BACKGROUND_LABELS,
            )
            if key in labels
        }

    async def _rollover_labels_for_session(session_id: str) -> dict[str, str]:
        """Durable per-session lookup of just the rollover-mode label.

        Unlike ``_fresh_session_init_envelope`` (60s TTL), this is cached for
        the runner process's lifetime once resolved — rollover mode is set
        once at session creation and never changes, so tool-call-time gating
        (``mcp_execute``) pays at most one lookup per session.
        """
        cached = _session_rollover_labels_cache.get(session_id)
        if cached is not None:
            return cached
        labels = await _session_labels_for_runner_spawn(
            server_client=server_client, session_id=session_id
        )
        _remember_rollover_labels(session_id, labels)
        return _session_rollover_labels_cache.get(session_id, {})

    async def _tool_labels_for_session(session_id: str) -> dict[str, str]:
        """Labels that gate which tools a session is offered.

        The rollover labels plus a sub-agent marker for a session with a parent,
        so top-level-only tools (e.g. ``sys_scheduled_task_*``) are withheld
        from Helpers.
        """
        labels = dict(await _rollover_labels_for_session(session_id))
        snapshot = await _session_snapshot(session_id)
        if snapshot.parent_session_id:
            labels[SUBAGENT_LABEL_KEY] = "1"
        return labels

    _unscheduled_children: set[str] = set()

    async def _register_scheduled_helper_work(session_id: str) -> None:
        """Track a scheduled Helper's turn so its Result wakes the parent.

        A parent-bound scheduled fire creates the child on the server, so the
        runner never saw a ``sys_session_create``; register the work entry from
        the child's labels before its first turn.
        """
        from omnigent.context.labels import is_superside_chat

        if get_subagent_work(session_id) is not None or session_id in _unscheduled_children:
            return
        snapshot = await _session_snapshot(session_id)
        if not snapshot.parent_session_id:
            _unscheduled_children.add(session_id)
            return
        labels = await _session_labels_for_runner_spawn(
            server_client=server_client, session_id=session_id
        )
        if not labels or SCHEDULED_HELPER_LABEL_KEY not in labels:
            _unscheduled_children.add(session_id)
            return
        register_subagent_work(
            parent_session_id=snapshot.parent_session_id,
            child_session_id=session_id,
            agent=snapshot.sub_agent_name or "sub-agent",
            title=snapshot.sub_agent_name or "",
            work_id=labels.get(SUBAGENT_DISPATCH_ID_LABEL_KEY),
            superside_chat=is_superside_chat(labels),
        )

    async def _drop_superside_chat_warm_client(
        conv_id: str, *, defer_if_active: bool = True, reason: str = "a /compact"
    ) -> None:
        """Drop the warm harness client so the next turn starts from the assembled block.

        Invalidates this runner's cached history so the next dispatch
        reloads it from the server (the latest checkpoint and the record after its anchor,
        see ``_load_history_as_input``), then releases the harness
        subprocess: its shutdown runs ``ClaudeSDKExecutor.close_session``
        (``ExecutorAdapter.on_shutdown``), which is the only way this
        process reaches that method on another process's executor.

        A background ``/compact`` can finish while a DIFFERENT turn
        is live on the warm client it would drop — releasing then would
        kill that turn's in-flight connection. With *defer_if_active* (the
        default), a live ``_active_turns`` entry defers the drop instead:
        it's recorded in ``_superside_chat_pending_refresh`` and applied at
        the start of the session's next turn
        (``_consume_superside_chat_pending_refresh``). The handoff decision and the hard
        limit pass ``defer_if_active=False``: it runs synchronously at
        the start of ITS OWN turn, before that turn has dispatched
        anything, so ``_active_turns`` already holds its own slot and
        deferring would be wrong — the whole point is to release now, so
        this turn's own dispatch cold-starts.
        """
        if defer_if_active and conv_id in _active_turns:
            _superside_chat_pending_refresh.add(conv_id)
            return
        _forget_superside_chat_prefix(conv_id)
        _session_histories.pop(conv_id, None)
        # The next session's size is unknown until its first turn ends: never hand it off on the
        # old one (0 also keeps the label's stale reading out).
        _superside_chat_last_context_tokens[conv_id] = 0
        if process_manager is not None:
            _logger.info(
                "dropping the warm %s client for %s: %s",
                _session_harness_name(conv_id) or "harness",
                conv_id,
                reason,
                extra={"session_id": conv_id},
            )
            await process_manager.release(conv_id)

    def _forget_superside_chat_prefix(conv_id: str) -> None:
        """Forget what the warm client was sent: the next turn carries every block in full."""
        _superside_chat_prefix_sent.pop(conv_id, None)
        _superside_chat_prefix_pending.pop(conv_id, None)

    async def _superside_chat_hard_limit_tokens(
        conv_id: str, labels: Mapping[str, str] | None = None
    ) -> int | None:
        """The context fill of *conv_id* if it leaves no room for another turn, else ``None``.

        Uses the fill the last turn reported (recorded when its stream completed, else the
        session's ``last_context_tokens`` label) against the session's reported window.
        ``None`` when either is unknown: the turn then dispatches as usual,
        since the rollover threshold sits well below the window.
        """
        from omnigent.context.rollover import reported_context_tokens
        from omnigent.superchat.rollover import exceeds_hard_context_limit

        if labels is None:
            labels = await _session_labels_for_runner_spawn(
                server_client=server_client, session_id=conv_id
            )
        tokens = _superside_chat_last_context_tokens.get(conv_id)
        if tokens is None:
            tokens = reported_context_tokens(labels)
        return tokens if exceeds_hard_context_limit(labels, context_tokens=tokens) else None

    async def _consume_superside_chat_pending_refresh(
        conv_id: str, turn_labels: Mapping[str, str] | None = None
    ) -> int | None:
        """Apply a deferred superside-chat warm-client drop at the start of a turn.

        Called unconditionally at the top of ``_run_turn_bg_setup_and_stream``
        for every turn (cheap no-op for a session with nothing pending).

        The fold (ADR 0031) runs in the engine and never drops the warm client, so nothing
        here waits on it. At the hard limit (``_superside_chat_hard_limit_tokens``) another turn
        on the warm state would overflow the window: the turn does not wait, the warm client is
        dropped so it starts from the assembled block, and the caller loads only the newest
        whole turns after the checkpoint that fit the returned budget. Nothing is deleted from
        the record.

        Otherwise, if a ``/compact`` deferred its drop because THIS session had an
        active turn at the time, applies it now: this turn is the next one,
        so no turn is active yet.

        :param turn_labels: The session's labels, read once for the turn (fetched here when
            ``None``).
        :returns: The token budget for the turn's history after the
            checkpoint at the hard limit, else ``None``.
        """
        from omnigent.context.rollover import resolve_keep_tokens

        keep_tokens: int | None = None
        drop = conv_id in _superside_chat_pending_refresh
        drop_reason = "a /compact finished during the last turn"
        labels = await _rollover_labels_for_session(conv_id)
        if labels.get(CONTEXT_MODE_LABEL) == SUPERSIDE_CHAT_MODE_VALUE:
            if turn_labels is None:
                turn_labels = await _session_labels_for_runner_spawn(
                    server_client=server_client, session_id=conv_id
                )
            tokens = await _superside_chat_hard_limit_tokens(conv_id, turn_labels)
            if tokens is not None:
                keep_tokens = resolve_keep_tokens(turn_labels)
                drop_reason = "the context is at the hard limit"
                _logger.warning(
                    "superside-chat context at the hard limit for %s; "
                    "cold-starting from the checkpoint plus the newest whole turns",
                    conv_id,
                    extra={"session_id": conv_id},
                )
                drop = True
        if drop:
            _superside_chat_pending_refresh.discard(conv_id)
            await _drop_superside_chat_warm_client(
                conv_id, defer_if_active=False, reason=drop_reason
            )
        return keep_tokens

    async def _superside_chat_session_model(conv_id: str) -> str | None:
        """The model *conv_id*'s turns run on (the summarizer uses it), or ``None``."""
        try:
            resp = await server_client.get(f"/v1/sessions/{conv_id}", timeout=10.0)
            body = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return None
        model = body.get("model_override") or body.get("llm_model")
        return model if isinstance(model, str) and model else None

    async def _superside_chat_idle_gap(conv_id: str) -> float | None:
        """Seconds since the item before this turn's message, from the record; ``None`` on a
        miss. For the turn's gap line only: a return never replaces the session."""
        try:
            resp = await server_client.get(
                f"/v1/sessions/{conv_id}/items",
                params={"limit": "2", "order": "desc"},
                timeout=10.0,
            )
            page_items = resp.json().get("data", []) if resp.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return None
        newest = page_items[0] if page_items else {}
        if newest.get("type") == "message" and newest.get("role") == "user":
            page_items = page_items[1:]
        created_at = page_items[0].get("created_at") if page_items else None
        return time.time() - created_at if isinstance(created_at, int) else None

    async def _superside_chat_handoff(
        conv_id: str, *, labels: Mapping[str, str] | None
    ) -> float | None:
        """Keep this superside-chat session's warm client for the turn, or drop it first.

        A warm session is replaced only when it needs compacting (``handoff_reason``, ADR 0031,
        owner decision 4): its context reached T and is over twice what a new session costs. A
        drop makes no model call and never waits: the turn's own history load assembles. A
        return, a cache expiry or a model switch keeps it (``set_model`` on Claude).

        :returns: Seconds away on the first message after the idle period, for the turn's gap
            line; else ``None``.
        """
        from omnigent.context.rollover import reported_context_tokens
        from omnigent.superchat.rollover import handoff_reason, resolve_idle_refresh_seconds

        gap = await _superside_chat_idle_gap(conv_id)
        gap = gap if gap is not None and gap >= resolve_idle_refresh_seconds() else None
        if conv_id not in _session_histories:
            return gap  # nothing warm to hand off; the load assembles
        tokens = (
            _superside_chat_last_context_tokens[conv_id]
            if conv_id in _superside_chat_last_context_tokens
            else reported_context_tokens(labels)
        )
        if handoff_reason(labels, context_tokens=tokens, conversation_id=conv_id) is None:
            return gap
        _logger.info(
            "superside-chat handoff for %s: size (context %s tokens)",
            conv_id,
            tokens,
            extra={"session_id": conv_id},
        )
        # defer_if_active=False: this runs at the start of THIS turn, which already holds its
        # own _active_turns slot, so the default deferral would always (wrongly) defer.
        await _drop_superside_chat_warm_client(
            conv_id, defer_if_active=False, reason="handoff (size)"
        )
        return gap

    def _warm_client_serial(conv_id: str) -> str | None:
        """The number of *conv_id*'s live harness subprocess, or ``None`` when unknown."""
        serial = getattr(process_manager, "instance_serial", None)
        value = serial(conv_id) if callable(serial) else None
        return str(value) if isinstance(value, int) else None

    def _commit_superside_chat_prefix(conv_id: str) -> None:
        """Remember what the completed turn sent, tied to the subprocess that received it."""
        sent = _superside_chat_prefix_pending.pop(conv_id, None)
        serial = _warm_client_serial(conv_id)
        if sent is not None and serial is not None:
            _superside_chat_prefix_sent[conv_id] = {**sent, "process": serial}

    async def _apply_turn_prefix_blocks(
        body: _JsonObject,
        conv_id: str,
        message: _JsonObject | None = None,
        *,
        prompt_key: str = "",
        idle_gap_seconds: float | None = None,
        setup: str | None = None,
    ) -> _JsonObject:
        """Prepend the Super Chat per-turn blocks (Memory Profile, Projects, local time).

        *setup* is the line naming the model and harness (``prompt_prefix.setup_line``); it goes
        only when this warm client has not received it yet, the same way as the Memory Profile.

        *message* is this turn's own user message; features build blocks from its text
        (``Feature.message_prefix``) that go in front of it, for the harness only.

        Called only for ``superside-chat`` turns; the blocks come from the one
        ``superchat.prompt_prefix`` hook, fetched fresh each turn (the SDK's system prompt is
        frozen per warm client, so a per-message block is the seam that still reaches it).

        A warm client keeps every earlier turn's blocks in its transcript, so the Memory Profile
        and Projects go in full only when they changed since this client last received them;
        otherwise each is one short line. What it received is remembered only once a turn
        completes (``_commit_superside_chat_prefix``), and only while the same subprocess
        runs under the same *prompt_key* (a hash of the instructions and tool names the turn
        sends: the executor rebuilds its client when they change, and replays the persisted
        history, which holds no blocks). Taking the remembered state here means a turn that
        fails, is interrupted or is cut short leaves nothing behind, so the next turn is full.
        """
        _mark_turn(conv_id, "prefix")
        workspace = await _session_runtime_cwd(conv_id)
        texts = _message_texts(message.get("content")) if message else []
        last = _superside_chat_prefix_sent.pop(conv_id, None)
        serial = _warm_client_serial(conv_id)
        if last is not None and (
            last.get("prompt") != prompt_key or last.get("process") != serial
        ):
            last = None
        sent_now: dict[str, str] = {"prompt": prompt_key}
        tool_labels = await _tool_labels_for_session(conv_id)
        prefix_started = time.monotonic()
        blocks = await turn_prefix_blocks(
            server_client,
            conv_id,
            workspace,
            texts,
            idle_gap_seconds=idle_gap_seconds,
            last_sent=last,
            sent_now=sent_now,
            helper=is_helper(tool_labels),
            # A file's studio: the file line and its write-back instead of the chat's blocks.
            studio=ARTIFACT_CHAT_LABEL_KEY in tool_labels,
        )
        _logger.info(
            "prefix for %s %.2fs",
            conv_id,
            time.monotonic() - prefix_started,
            extra={"session_id": conv_id},
        )
        if setup is not None:
            sent_now["setup"] = setup
            if last is None or last.get("setup") != setup:
                blocks.append(setup)
        _mark_turn(conv_id, "prefixed")
        _superside_chat_prefix_pending[conv_id] = sent_now
        return _prepend_turn_blocks(body, blocks, conv_id)

    async def _run_turn_bg(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        _subagent_wake_pending.discard(conv)
        # Capture our own task so the finally floor can identity-compare before
        # clearing the slot (see below).
        _own_task = asyncio.current_task()
        # A fresh turn is binding: whatever desync the previous turn ended on is
        # resolved now. Also clear a stale publish-once token (e.g. left set by a
        # wedged stream that never reached its own _on_proxy_stream_end) so it
        # can't suppress this turn's legitimate terminal publish.
        _desynced_sessions.discard(conv)
        _desync_terminalized.pop(conv, None)
        # Locate any uncoded exception logged below in the turn phase (this task's
        # context carries it for its lifetime). Coded errors keep their own phase.
        with phase_scope(ErrorPhase.TURN):
            try:
                if await _is_studio(conv):
                    await _run_studio_turn(msg_body, conv)
                else:
                    await _run_turn_bg_setup_and_stream(msg_body, conv)
            except _ContextWindowOverflow:
                # Re-raise so the streaming-phase handler (which publishes the
                # error event) is never shadowed by the generic except below.
                raise
            except asyncio.CancelledError as exc:
                _logger.error(
                    "turn cancelled for %s: %s",
                    conv,
                    exc,
                    exc_info=True,
                    extra={"session_id": conv},
                )
                _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})
                raise
            except Exception as exc:
                _logger.error(
                    "turn setup failed for %s: %s",
                    conv,
                    exc,
                    exc_info=True,
                    extra={"session_id": conv},
                )
                _on_proxy_stream_end(conv, error={"message": f"turn setup failed: {exc}"})
            finally:
                # Permanent-wedge floor: guarantee _active_turns is never left stale,
                # however the body exits — including a BaseException that escapes
                # ``except Exception``. A setup-phase abnormal exit otherwise leaves
                # the slot set and every later message buffers forever.
                #
                # Identity compare-and-clear: only finalize when the slot STILL holds
                # THIS turn's own task. A turn that ended cleanly already popped its
                # slot via _on_proxy_stream_end, which schedules a continuation that
                # can bind a NEW turn's task under the same conv — a bare
                # ``conv in _active_turns`` check would then let this stale finally
                # clobber the newer turn (the same class of bug the ExecutorAdapter
                # identity CAS fixes). When the slot is a None sentinel or a
                # different task, this turn is already accounted for — skip.
                if _active_turns.get(conv) is _own_task and _own_task is not None:
                    _on_proxy_stream_end(conv)

    async def _is_studio(conv: str) -> bool:
        """Whether *conv* is a file's studio: its helper type first (known for every turn)."""
        if await _recover_sub_agent_name(conv) != STUDIO_TYPE:
            return False
        return ARTIFACT_CHAT_LABEL_KEY in await _rollover_labels_for_session(conv)

    async def _run_studio_turn(msg_body: _JsonObject, conv: str) -> None:
        """A file's studio turn (ADR 0024): within the host's studio cap, and keeps it warm.

        A turn past the cap waits for a slot; meanwhile the session shows it is queued.
        """
        queued: list[bool] = []

        def _on_queued() -> None:
            queued.append(True)
            _publish_session_status(conv, "running", blocked_on=STUDIO_QUEUED_NOTE)

        _ensure_studio_sweeper()
        async with _studio_runners.turn(conv, _on_queued):
            if queued:
                _publish_session_status(conv, "running")
            await _run_turn_bg_setup_and_stream(msg_body, conv)

    def _ensure_studio_sweeper() -> None:
        """Start the loop that releases a studio's harness once it has gone cold."""
        if _studio_sweeper and not _studio_sweeper[0].done():
            return
        _studio_sweeper[:] = [asyncio.create_task(_release_cold_studios())]

    async def _release_cold_studios() -> None:
        while True:
            await asyncio.sleep(60.0)
            for conv in _studio_runners.cold():
                if process_manager is None or conv in _active_turns:
                    continue
                try:
                    # Conditional, like the idle reaper: a turn that began meanwhile keeps it.
                    await process_manager.release(
                        conv, only_if_idle_cutoff=time.monotonic() - STUDIO_WARM_SECONDS
                    )
                except Exception:
                    _logger.warning("could not release cold studio %s", conv, exc_info=True)

    def _turn_reasoning(conv: str, msg_body: _JsonObject) -> _JsonObject | None:
        """Reasoning block to forward on a turn, or ``None`` when unset.

        An explicit per-event value wins; otherwise the session's remembered
        effort (session-init snapshot or a later ``effort_change``) applies, so
        an in-process harness sees the same effort a native TUI got on its argv.

        :param conv: Session/conversation id.
        :param msg_body: The dispatched message body.
        :returns: ``{"effort": "<level>"}`` or ``None``.
        """
        raw = msg_body.get("reasoning")
        if isinstance(raw, dict):
            effort = raw.get("effort")
            if isinstance(effort, str) and effort:
                return {"effort": effort}
        remembered = _session_reasoning_effort.get(conv)
        return {"effort": remembered} if remembered else None

    async def _ensure_session_tool_schemas(
        conv: str, cached_spec: AgentSpec | None, cached_spec_entry: object | None
    ) -> None:
        """Build the session's tool schemas once, and its MCP ones whenever its servers change."""
        if conv not in _session_tool_schemas:
            all_tools: list[_JsonObject] = []
            if cached_spec is not None:
                try:
                    from omnigent.tools.manager import (
                        ToolManager,
                    )

                    # Thread the session's context-mode label through so a
                    # superside-chat (or rollover) session's ToolManager
                    # registers session_history/memory_* here too — this SDK
                    # path otherwise never offers them, even though the
                    # system prompt mentions them (see
                    # ``_rollover_labels_for_session``).
                    _tmgr = ToolManager(
                        cached_spec,
                        workdir=_resolved_workdir_for_spec(cached_spec_entry, runner_workspace),
                        os_env_schema_only=True,
                        labels=await _tool_labels_for_session(conv),
                    )
                    all_tools.extend(_tmgr.get_tool_schemas())
                except (
                    ImportError,
                    ValueError,
                    RuntimeError,
                ):
                    _logger.warning(
                        "ToolManager schema build failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )
            _session_tool_schemas[conv] = all_tools

        if cached_spec and cached_spec.mcp_servers:
            from omnigent.runner.mcp_manager import compute_spec_hash

            _mcp_hash = compute_spec_hash(list(cached_spec.mcp_servers))
            if _mcp_hash != _session_mcp_spec_hash.get(conv):
                _session_mcp_proxy = ProxyMcpManager(
                    conv,
                    server_client,
                    execution_registry=mcp_execution_registry,
                )
                try:
                    mcp_result = await _session_mcp_proxy.schemas_for(
                        cached_spec,
                    )
                    _builtin_tools = [
                        t
                        for t in _session_tool_schemas.get(conv, [])
                        if not (
                            isinstance(t, dict)
                            and isinstance(t.get("name"), str)
                            and "__" in cast(str, t.get("name"))
                        )
                    ]
                    _session_tool_schemas[conv] = _builtin_tools + list(mcp_result.schemas)
                    _session_mcp_spec_hash[conv] = _mcp_hash
                except (
                    httpx.HTTPError,
                    RuntimeError,
                    ValueError,
                ):
                    _logger.warning(
                        "MCP schema resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )

    async def _superside_chat_turn_reads(
        conv: str,
    ) -> tuple[bool, dict[str, str] | None]:
        """Whether *conv* is a Super Chat, and if so its labels.

        One labels read per turn, shared by the hard-limit check, the handoff decision and the
        memory sync. A plain session pays no round trip.
        """
        mode = (await _rollover_labels_for_session(conv)).get(CONTEXT_MODE_LABEL)
        if mode != SUPERSIDE_CHAT_MODE_VALUE:
            return False, None
        labels = await _session_labels_for_runner_spawn(
            server_client=server_client, session_id=conv
        )
        return True, labels

    async def _run_turn_bg_setup_and_stream(
        msg_body: _JsonObject,
        conv: str,
    ) -> None:
        _setup_started = time.monotonic()
        _superside, _turn_labels = await _superside_chat_turn_reads(conv)
        _setup_reads = time.monotonic() - _setup_started
        # Cheap no-op for a session with no pending superside-chat rollover;
        # otherwise applies a deferred warm-client drop before anything below
        # touches the harness, and at the hard context limit returns the
        # budget this turn's history is trimmed to.
        _trim_keep_tokens = await _consume_superside_chat_pending_refresh(conv, _turn_labels)
        _dispatched_agent_id = cast(str | None, msg_body.get("agent_id"))
        _prior_agent_id = _session_agent_ids.get(conv)
        if (
            _dispatched_agent_id
            and _prior_agent_id is not None
            and _prior_agent_id != _dispatched_agent_id
        ):
            _logger.info(
                "agent switch detected for %s: %s -> %s; resetting session caches",
                conv,
                _prior_agent_id,
                _dispatched_agent_id,
                extra={"session_id": conv},
            )
            await _invalidate_session_agent_state(conv, _dispatched_agent_id)
        if _dispatched_agent_id:
            _session_agent_ids[conv] = _dispatched_agent_id

        cached_spec_entry = _session_spec_cache.get(conv)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        cached_spec_workdir = _resolved_spec_workdir(cached_spec_entry)
        if cached_spec is None and spec_resolver is not None:
            _aid = _dispatched_agent_id
            if _aid:
                try:
                    resolved = await spec_resolver(_aid, conv)
                    if isinstance(resolved, ResolvedSpec):
                        cached_spec = _unwrap_resolved_spec(resolved)
                        cached_spec_workdir = _resolved_spec_workdir(resolved)
                        _session_spec_cache[conv] = resolved
                    elif resolved is not None:
                        cached_spec = resolved
                        _session_spec_cache[conv] = resolved
                except (httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "Spec resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )
            else:
                try:
                    cached_spec = await _resolve_session_agent_spec(conv)
                    cached_spec_workdir = _resolved_spec_workdir(_session_spec_cache.get(conv))
                except (OmnigentError, httpx.HTTPError, RuntimeError):
                    _logger.warning(
                        "On-demand agent resolution failed for %s",
                        conv,
                        exc_info=True,
                        extra={"session_id": conv},
                    )

        # The resolver branches above write straight into the cache, so the
        # entry read at the top of this block can already be stale.
        cached_spec_entry = _session_spec_cache.get(conv, cached_spec_entry)

        _sa_name = await _recover_sub_agent_name(conv)
        # The child's workdir is rooted before _spec_with_workdir_paths below,
        # which joins relative local-tool paths onto whatever workdir is current.
        if _sa_name and cached_spec is not None:
            sub_entry = _resolve_sub_agent_spec_entry(cached_spec_entry, _sa_name)
            if sub_entry is None:
                # Warn unless the child was confirmed resolved (True = already cached).
                if _session_sub_agent_resolved.get(conv) is not True:
                    _warn_unresolved_sub_agent(conv, _sa_name)
            else:
                cached_spec_entry = sub_entry
                cached_spec = _unwrap_resolved_spec(sub_entry)
                cached_spec_workdir = _resolved_spec_workdir(sub_entry)
                _session_spec_cache[conv] = sub_entry

        cached_spec = _spec_with_workdir_paths(cached_spec, cached_spec_workdir)
        if cached_spec is not None:
            cached_spec_entry = _rewrap_like(cached_spec_entry, cached_spec, cached_spec_workdir)
            _session_spec_cache[conv] = cached_spec_entry

        harness_name: str | None = None
        raw_harness: str | None = None
        spawn_env: dict[str, str] | None = None
        instructions: str | None = None
        _note_session_harness_override(conv, cast(str | None, msg_body.get("harness_override")))
        if cached_spec is not None:
            # The session's recorded override outranks the spec (mirrors
            # _initialize_session): a forward may carry no per-event
            # harness_override, so resolving from the body alone would drop a
            # later turn back onto the spec's harness and evict the override
            # harness mid-session.
            raw_harness = (
                _session_harness_overrides.get(conv)
                or cast(str | None, msg_body.get("harness_override"))
                or cached_spec.executor.config.get("harness")
                or cached_spec.executor.type
            )
            harness_name = canonicalize_harness(raw_harness) or raw_harness

        # Keep the warm client or hand off: checked every turn (a live chat keeps its history
        # cached); a drop clears the cache so the load below assembles.
        _idle_gap = (
            await _superside_chat_handoff(conv, labels=_turn_labels) if _superside else None
        )
        _history_started = time.monotonic()
        if conv in _session_histories and _history_process.get(
            conv, _warm_client_serial(conv)
        ) != _warm_client_serial(conv):
            # The subprocess that held this history is gone: the new one starts cold, from the
            # record (clipped, whole Rounds), never from the runner's raw copy of the old one.
            _session_histories.pop(conv, None)
        if conv not in _session_histories:
            _session_histories[conv] = await _load_history_as_input(
                conv, keep_tokens=_trim_keep_tokens
            )
            _history = _session_histories[conv]
            if _trim_keep_tokens is not None and (
                not _history or _history[-1].get("role") != "user"
            ):
                # The trimmed turn always ends with the person's latest message.
                _history.append(
                    {"type": "message", "role": "user", "content": msg_body.get("content", [])}
                )
        _setup_history = time.monotonic() - _history_started
        _raw_per_request_instructions = cast(str | None, msg_body.get("instructions"))
        _env_started = time.monotonic()
        if cached_spec is not None:
            spawn_env = _build_spawn_env_from_spec(
                cached_spec,
                cast(str, raw_harness),
                workdir=cached_spec_workdir,
                cwd=await _session_runtime_cwd(conv),
                model_override=cast(str | None, msg_body.get("model_override")),
                session_id=conv,
            )
            _authored_bg = raw_author_instructions(cached_spec) is not None
            # Cache hit in steady state (see _rollover_labels_for_session):
            # harnesses pay one
            # lookup on their first turn.
            _bg_labels = await _rollover_labels_for_session(conv)
            instructions = build_instructions(
                cached_spec,
                _raw_per_request_instructions,
                [],
                labels=_bg_labels,
            )
            # Warn once per (conversation, harness, delivery) if the agent has
            # authored instructions but the harness can't deliver them.
            if _authored_bg and harness_name:
                _bg_caps = harness_capabilities().get(harness_name)
                _bg_delivery = (
                    _bg_caps.instruction_delivery
                    if _bg_caps is not None
                    else InstructionDelivery.UNKNOWN
                )
                _bg_warn_key = (harness_name, _bg_delivery)
                if _bg_warn_key not in _instruction_delivery_warned.get(conv, ()):
                    _instruction_delivery_warned.setdefault(conv, set()).add(_bg_warn_key)
                    if _bg_delivery in (
                        InstructionDelivery.NOT_DELIVERED,
                        InstructionDelivery.UNKNOWN,
                    ):
                        _logger.warning(
                            "agent instructions not delivered for session=%s "
                            "harness=%s delivery=%s — authored instructions "
                            "accepted but have no delivery channel on this harness.",
                            conv,
                            harness_name,
                            _bg_delivery.value,
                            extra={"session_id": conv},
                        )

        # Super Chat answers which model and harness it runs on when asked: a per-turn line, not
        # the instructions, so a model change keeps the warm client (Claude's ``set_model``) and
        # the pre-launch, built from the same instructions, is kept (see the prefix below).
        _turn_model = msg_body.get("model_override")
        _harness_label = _HARNESS_LABELS.get(harness_name, harness_name)
        _setup_now = (
            f"{_harness_label} with {_turn_model}"
            if isinstance(_turn_model, str) and _turn_model
            else None
        )
        _setup_env = time.monotonic() - _env_started
        ctx = TurnDispatch(
            agent_id=_dispatched_agent_id,
            harness=harness_name,
            spawn_env=spawn_env,
            has_mcp_servers=(
                (cached_spec is not None and bool(cached_spec.mcp_servers))
                or msg_body.get("has_mcp_servers") is True
            ),
            instructions=instructions,
        )

        harness_body: _JsonObject = {
            "type": "message",
            "role": "user",
            "model": msg_body.get("model", ""),
        }
        # Threaded onto CreateResponseRequest.context_mode (MessageEvent
        # passthrough) so the claude-sdk executor adapter can tell a
        # superside-chat turn apart from a plain one and turn off the SDK's
        # own competing features for it only (see claude_sdk_executor.py).
        _turn_context_mode = (await _rollover_labels_for_session(conv)).get(CONTEXT_MODE_LABEL)
        if _turn_context_mode:
            harness_body["context_mode"] = _turn_context_mode
        # A background job's single turn is bounded in context (the Conversation and a file's
        # studio keep the between-turn rollover only); the executor adapter enforces it.
        # The parent marks a Helper (stored labels never do); a studio keeps its rollover.
        if (
            _superside
            and is_helper({**(_turn_labels or {}), **await _tool_labels_for_session(conv)})
            and not is_artifact_chat(_turn_labels)
        ):
            from omnigent.superchat.rollover import resolve_helper_turn_context_ceiling

            harness_body["turn_context_ceiling"] = resolve_helper_turn_context_ceiling()
        # The routed model rides in-band on the forwarded message. This body is
        # built field by field (not copied), so it must be threaded explicitly:
        # the harness forwards it onto CreateResponseRequest.model_override and
        # the executor adapter into ExecutorConfig.model, which is how the
        # harness learns to switch models for this turn.
        _model_override = msg_body.get("model_override")
        if isinstance(_model_override, str) and _model_override:
            harness_body["model_override"] = _model_override
        # The Round's trace parent (ADR 0041): the harness opens this turn's span under it.
        _traceparent = msg_body.get("traceparent")
        if isinstance(_traceparent, str) and _traceparent:
            harness_body["traceparent"] = _traceparent
        # The web's stable id for this message: stamped on the turn's failure
        # so the server can settle exactly this queued entry, not the oldest.
        _stable_id = msg_body.get("stable_id")
        if isinstance(_stable_id, str) and _stable_id:
            harness_body["input_stable_id"] = _stable_id
            _logger.info(
                "_run_turn_bg: conv=%s received model_override=%s (forwarding to harness)",
                conv,
                _model_override,
                extra={"session_id": conv},
            )
        # Resolve the effort for this turn — an explicit per-event value, else
        # the session's remembered one — then deliver only what this harness can
        # accept. The persisted effort is validated at create against the union
        # vocabulary, so a value that is legal there ("none" on an
        # Anthropic-family harness) can still be foreign here, and the executors
        # reject an unsupported value by failing the turn. The native launch path
        # filters the same way (see native/orchestration.py's ``--effort``
        # guard); an unknown harness is passed through rather than dropped, since
        # a plugin harness may accept efforts this registry has never heard of.
        _reasoning = _turn_reasoning(conv, msg_body)
        if _reasoning is not None:
            from omnigent.util.reasoning_effort import efforts_for_harness, format_supported

            _effort = _reasoning["effort"]
            _supported = efforts_for_harness(harness_name)
            if _supported is None or _effort in _supported:
                harness_body["reasoning"] = _reasoning
            else:
                _logger.warning(
                    "_run_turn_bg: conv=%s dropping reasoning effort %r — harness %s accepts %s",
                    conv,
                    _effort,
                    harness_name,
                    format_supported(_supported) if _supported else "no effort override",
                )
        if _session_histories[conv]:
            harness_body["content"] = _session_histories[conv]
        else:
            harness_body["content"] = msg_body.get(
                "content",
                [],
            )
        _content = cast(list[object], harness_body.get("content", []))
        _content_summary = []
        for _ci in _content:
            if isinstance(_ci, dict):
                _ct = _ci.get("type", "?")
                if _ct == "message":
                    _blocks = cast(list[object], _ci.get("content", []))
                    _block_types = [b.get("type") for b in _blocks if isinstance(b, dict)]
                    _content_summary.append(f"msg({_ci.get('role', '?')}, blocks={_block_types})")
                else:
                    _content_summary.append(str(_ct))
        _logger.info(
            "_run_turn_bg: conv=%s history_msgs=%d content_summary=%s",
            conv,
            len(_content),
            _content_summary[:20],
            extra={"session_id": conv},
        )

        if instructions:
            harness_body["instructions"] = instructions

        _tools_started = time.monotonic()
        await _ensure_session_tool_schemas(conv, cached_spec, cached_spec_entry)
        _setup_tools = time.monotonic() - _tools_started
        _setup_total = time.monotonic() - _setup_started
        if _setup_total > _SLOW_SETUP_SECONDS:
            _logger.info(
                "turn setup for %s took %.2fs: reads %.2fs history %.2fs env %.2fs tools %.2fs",
                conv,
                _setup_total,
                _setup_reads,
                _setup_history,
                _setup_env,
                _setup_tools,
                extra={"session_id": conv},
            )

        _spec_tools = _session_tool_schemas.get(conv) or []
        # Request-driven harnesses should not advertise browser tools when no
        # renderer is subscribed. Native harnesses ignore this per-turn list
        # and keep their session-scoped relay surface; their calls still use
        # the prompt no-renderer failure below. An absent hint from an older
        # server preserves the previous advertised surface. Only the spec
        # surface is filtered; request-supplied tools remain caller-owned.
        # A local browser backend needs no renderer, so it keeps the tools.
        if (
            msg_body.get("browser_renderer_available") is False
            and not local_browser_backend_enabled()
        ):
            from omnigent.runner.tool_dispatch import strip_browser_tool_schemas

            _spec_tools = strip_browser_tool_schemas(_spec_tools)
        _client_tools = cast(list[_JsonObject], msg_body.get("tools") or [])
        merged_tools = _merge_request_client_tools(_spec_tools, _client_tools)
        if merged_tools:
            harness_body["tools"] = merged_tools
        _spec_names = {
            name
            for t in _spec_tools
            if isinstance(t, dict) and (name := _schema_tool_name(t)) is not None
        }
        ctx.client_side_tool_names = frozenset(
            name
            for t in _client_tools
            if isinstance(t, dict)
            and (name := _schema_tool_name(t)) is not None
            and name not in _spec_names
        )

        if _turn_context_mode == SUPERSIDE_CHAT_MODE_VALUE:
            # Placed after the tools are known: the key covers what the executor builds its
            # system prompt from, so a change to it (which rebuilds the client) is a cold start.
            # The model too: pi respawns on a model change inside the same harness process and
            # replays a history without the blocks (Claude keeps its client, and gets them again).
            _tool_names = sorted(
                name for t in merged_tools if (name := _schema_tool_name(t)) is not None
            )
            _prompt_key = hashlib.sha256(
                json.dumps([instructions, _tool_names, _turn_model]).encode()
            ).hexdigest()
            harness_body = await _apply_turn_prefix_blocks(
                harness_body,
                conv,
                msg_body,
                prompt_key=_prompt_key,
                idle_gap_seconds=_idle_gap,
                setup=(
                    setup_line(cast(str, _turn_model), _harness_label)
                    if _setup_now is not None
                    else None
                ),
            )
            if _setup_now is not None:
                _setup_before = _superside_chat_setup.get(conv)
                _superside_chat_setup[conv] = _setup_now
                if _setup_before is not None and _setup_before != _setup_now:
                    harness_body = _prepend_turn_blocks(
                        harness_body, [setup_changed_line(_setup_before, _setup_now)], conv
                    )
        if is_superside_chat(_turn_labels):
            # After the prefix: its reads and the sync's never compete (ADR 0026).
            _schedule_memory_sync(conv, _turn_labels)

        try:
            response = await _stream_message_to_harness(
                harness_body,
                conv,
                dispatch=ctx,
            )
        finally:
            _session_init_envelopes.pop(conv, None)
        if isinstance(response, StreamingResponse):
            await _drain_streaming_response(response, conv)
        else:
            error = _harness_error_response_error(response)
            _logger.error(
                "turn bg error for %s: %s",
                conv,
                error["message"],
                extra={"session_id": conv},
            )
            _on_proxy_stream_end(conv, error=error)

    async def _drain_streaming_response(
        response: StreamingResponse,
        session_id: str,
    ) -> None:
        try:
            async for _chunk in response.body_iterator:
                pass
        except asyncio.CancelledError:
            # Identity guard (same generation-ownership class as
            # _on_proxy_stream_end and the _run_turn_bg finally floor): the drain
            # runs INLINE in this turn's own _run_turn_bg task, so the slot should
            # still hold that task. Only clear when it does — if a newer turn has
            # taken the slot, this is a stale finalizer and must not pop the newer
            # turn's state, response id, or publish a spurious idle over it.
            # ``delete_session`` pops the slot before cancelling, so an empty
            # slot still means "no newer turn took over" — publish for it too.
            _slot = _active_turns.get(session_id)
            if _slot is None or _slot is asyncio.current_task():
                _active_turns.pop(session_id, None)
                # Clear the live response AND the in-flight marker together (B1
                # class fix): a bare pop would leak the process-manager marker and
                # the idle reaper would skip the harness forever.
                _release_live_turn_markers(session_id)
                # Publish-once guard, epoch-scoped (same token as
                # _on_proxy_stream_end): suppress this ``idle`` only if recovery
                # claimed the terminal for THIS generation's epoch.
                if _desync_terminalized.get(session_id) == _turn_bind_epoch.get(session_id, 0):
                    _desync_terminalized.pop(session_id, None)
                else:
                    _publish_turn_status(session_id, "idle")
            raise
        except (httpx.HTTPError, RuntimeError, StopAsyncIteration) as exc:
            _logger.error(
                "drain failed for %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            _on_proxy_stream_end(
                session_id,
                error={
                    "message": f"background turn drain failed: {exc}",
                },
            )

    #: The harness's agent (the Claude CLI, the Pi process) launched at session init, by session
    #: (ADR 0026: cold start is paid before the person speaks). A turn waits for it before posting, so its message never lands inside
    #: the launch.
    _harness_prelaunches: dict[str, asyncio.Task[None]] = {}

    async def _prelaunch_harness(
        conv: str,
        client: Any,
        harness_name: str,
        model_override: str | None,
        agent_id: str,
    ) -> None:
        """Post an empty first message carrying what the session's turns will send.

        The executor launches its CLI (Claude) or process (Pi) for it and answers nothing; a
        first turn whose options differ relaunches, and any failure here leaves the first turn
        to launch it as before.
        """
        try:
            entry = _session_spec_cache.get(conv)
            spec = _spec_with_workdir_paths(
                _unwrap_resolved_spec(entry), _resolved_spec_workdir(entry)
            )
            if spec is None or process_manager is None:
                return
            labels = await _rollover_labels_for_session(conv)
            await _ensure_session_tool_schemas(conv, spec, entry)
            # The tools a turn sends while the person's screen is open (a turn strips the
            # browser tools only when no renderer is subscribed): the turn that follows the
            # launch is almost always one sent from that screen, and a CLI launched with other
            # tools would be relaunched for it.
            tools = _session_tool_schemas.get(conv) or []
            body: _JsonObject = {
                "model": agent_id,
                "content": [],
                "instructions": build_instructions(spec, None, [], labels=labels),
                "tools": tools,
            }
            if context_mode := labels.get(CONTEXT_MODE_LABEL):
                body["context_mode"] = context_mode
            if model_override:
                body["model_override"] = model_override
            reasoning = _turn_reasoning(conv, {})
            if reasoning is not None:
                from omnigent.util.reasoning_effort import efforts_for_harness

                supported = efforts_for_harness(harness_name)
                if supported is None or reasoning["effort"] in supported:
                    body["reasoning"] = reasoning
            if conv in _active_turns:
                # The first turn came during init and launches it itself; an empty message now
                # would land in that turn as a steer.
                return
            started = time.monotonic()
            try:
                async with client.stream(
                    "POST",
                    f"/v1/sessions/{conv}/events",
                    json=_wrap_as_message_event(body),
                    timeout=None,
                ) as response:
                    async for _chunk in response.aiter_text():
                        pass
            except asyncio.CancelledError:
                # The first turn stopped waiting: end the harness's pre-launch turn too.
                with contextlib.suppress(Exception):
                    await client.post(
                        f"/v1/sessions/{conv}/events", json={"type": "interrupt"}, timeout=2.0
                    )
                raise
            _logger.info(
                "harness pre-launched for %s in %.1fs",
                conv,
                time.monotonic() - started,
                extra={"session_id": conv},
            )
        except Exception:
            _logger.warning(
                "harness pre-launch failed for %s; the first turn launches it",
                conv,
                exc_info=True,
                extra={"session_id": conv},
            )

    async def _await_harness_prelaunch(conv_id: str) -> None:
        """Wait for the session's harness pre-launch, if one is still running."""
        prelaunch = _harness_prelaunches.pop(conv_id, None)
        if prelaunch is None or prelaunch.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(prelaunch), _PRELAUNCH_WAIT_SECONDS)
        except TimeoutError:
            # Stopping it interrupts the harness's pre-launch turn (see _prelaunch_harness),
            # so this turn's message starts a turn of its own instead of steering into it.
            prelaunch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prelaunch

    async def _stream_message_to_harness(
        body: _JsonObject,
        conv_id: str,
        dispatch: TurnDispatch | None = None,
    ) -> Response:
        await _await_harness_prelaunch(conv_id)
        manager = cast(HarnessProcessManager, process_manager)
        harness_name = dispatch.harness if dispatch else cast(str | None, body.get("harness"))
        spawn_env = (
            dispatch.spawn_env if dispatch else cast(dict[str, str] | None, body.get("spawn_env"))
        )
        _note_session_harness_override(conv_id, cast(str | None, body.get("harness_override")))
        # Shared agent-switch invalidation for both dispatch paths.
        _ds_agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        _ds_prior = _session_agent_ids.get(conv_id)
        if _ds_agent_id and _ds_prior is not None and _ds_prior != _ds_agent_id:
            await _invalidate_session_agent_state(conv_id, _ds_agent_id)
        if _ds_agent_id:
            _session_agent_ids[conv_id] = _ds_agent_id
        if not harness_name:
            _agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
            _sub_agent_name = await _recover_sub_agent_name(conv_id)
            try:
                harness_name, spawn_env = await _resolve_harness_config(
                    agent_id=_agent_id,
                    spec_resolver=spec_resolver,
                    session_id=conv_id,
                    model_override=cast(str | None, body.get("model_override")),
                    # Session-recorded override first (the note above already
                    # folded in any body value): a body without one must not
                    # drop the turn back onto the spec's harness.
                    harness_override=(
                        _session_harness_overrides.get(conv_id)
                        or cast(str | None, body.get("harness_override"))
                    ),
                    sub_agent_name=_sub_agent_name,
                    cwd=await _session_runtime_cwd(conv_id),
                )
            except (httpx.HTTPError, RuntimeError) as exc:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "spec_resolver_failed",
                        "detail": _client_safe_error_detail(exc, context="spec resolve"),
                    },
                )
        agent_version = (
            dispatch.agent_version if dispatch else cast(int | None, body.get("agent_version"))
        )
        if agent_version is not None and conv_id in _version_cache:
            if agent_version > _version_cache[conv_id]:
                await manager.release(conv_id)
        if agent_version is not None:
            _version_cache[conv_id] = agent_version

        try:
            client = await manager.get_client(conv_id, harness_name, env=spawn_env)
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "harness_spawn_failed",
                    "detail": _client_safe_error_detail(exc, context="harness spawn"),
                },
            )

        _turn_agent_id = dispatch.agent_id if dispatch else cast(str | None, body.get("agent_id"))
        _has_mcp_hint = dispatch.has_mcp_servers if dispatch else body.get("has_mcp_servers")
        _turn_spec: object | None = None
        _turn_spec_entry: object | None = None
        _turn_spec_resolved = False
        _mcp_schemas: list[_JsonObject] = []
        _mcp_tool_names: set[str] = set()
        _eager_spec_error: tuple[str, str] | None = None
        if _has_mcp_hint is True and _turn_agent_id:
            _turn_spec_entry = _spec_cache.get(_turn_agent_id)
            _turn_spec = _unwrap_resolved_spec(_turn_spec_entry)
            if _turn_spec is None:
                _session_entry = _session_spec_cache.get(conv_id)
                _turn_spec_entry = _session_entry
                _turn_spec = _unwrap_resolved_spec(_session_entry)
            if _turn_spec is None and spec_resolver is not None:
                try:
                    _resolved_turn_spec = await spec_resolver(_turn_agent_id, conv_id)
                    _turn_spec = _unwrap_resolved_spec(_resolved_turn_spec)
                except (httpx.HTTPError, RuntimeError) as exc:
                    _logger.warning(
                        "eager turn spec resolution failed for %s: %s",
                        conv_id,
                        exc,
                        exc_info=True,
                        extra={
                            "session_id": conv_id,
                            "event_name": "runner_turn_spec_resolution_failed",
                            "attributes": {"phase": "eager", "exception_type": type(exc).__name__},
                        },
                    )
                    _eager_spec_error = (
                        type(exc).__name__,
                        "Failed to resolve the agent spec for this turn.",
                    )
                else:
                    if _resolved_turn_spec is not None and _turn_spec is not None:
                        _spec_cache[_turn_agent_id] = _resolved_turn_spec
                        _turn_spec_entry = _resolved_turn_spec
            _turn_spec_resolved = True
            _turn_mcp = ProxyMcpManager(
                conv_id,
                server_client,
                execution_registry=mcp_execution_registry,
            )
            if _eager_spec_error is None and _turn_spec is not None:
                try:
                    _mcp = await _turn_mcp.schemas_for(cast(AgentSpec, _turn_spec))
                    _mcp_schemas = _mcp.schemas
                    _mcp_tool_names = _mcp.tool_names
                    for _srv, _err in _mcp.failures.items():
                        _logger.warning(
                            "runner MCP %r unavailable for this turn: %s",
                            _srv,
                            _err,
                            extra={"session_id": conv_id},
                        )
                except Exception:
                    _logger.exception(
                        "runner mcp_manager.schemas_for failed", extra={"session_id": conv_id}
                    )

        async def _resolve_turn_spec_lazy() -> tuple[object | None, tuple[str, str] | None]:
            nonlocal _turn_spec, _turn_spec_entry, _turn_spec_resolved
            if _turn_spec_resolved:
                return _turn_spec_entry or _turn_spec, None
            _turn_spec_resolved = True
            session_cached = _session_spec_cache.get(conv_id)
            if session_cached is not None:
                _turn_spec_entry = session_cached
                _turn_spec = _unwrap_resolved_spec(session_cached)
                return session_cached, None
            if not _turn_agent_id or spec_resolver is None:
                return None, None
            cached = _spec_cache.get(_turn_agent_id)
            if cached is not None:
                _turn_spec_entry = cached
                _turn_spec = _unwrap_resolved_spec(cached)
                return cached, None
            try:
                resolved = await spec_resolver(_turn_agent_id, conv_id)
            except (httpx.HTTPError, RuntimeError) as exc:
                _logger.warning(
                    "lazy turn spec resolution failed for %s: %s",
                    conv_id,
                    exc,
                    exc_info=True,
                    extra={
                        "session_id": conv_id,
                        "event_name": "runner_turn_spec_resolution_failed",
                        "attributes": {"phase": "lazy", "exception_type": type(exc).__name__},
                    },
                )
                return None, (
                    type(exc).__name__,
                    "Failed to resolve the agent spec for this turn.",
                )
            if resolved is not None:
                _spec_cache[_turn_agent_id] = resolved
                _turn_spec_entry = resolved
                _turn_spec = _unwrap_resolved_spec(resolved)
                return resolved, None
            return None, None

        async def proxy_stream() -> AsyncIterator[bytes]:
            import asyncio as _asyncio
            import json as _json

            from omnigent.runner.tool_dispatch import (
                dispatch_tool_locally,
                get_arguments,
                get_call_id,
                get_tool_name,
                is_action_required,
                should_dispatch_locally,
            )

            if _eager_spec_error is not None:
                _err_type, _err_msg = _eager_spec_error
                _fail = _response_failed_payload({"message": _err_msg, "type": _err_type})
                _publish_event(conv_id, _fail)
                _on_proxy_stream_end(
                    conv_id,
                    error={"message": _err_msg, "type": _err_type},
                )
                yield _response_failed_event({"message": _err_msg, "type": _err_type})
                return

            # Compose instructions for direct-stream turns (dispatch is None;
            # background path pre-composes).
            _instr_body = body
            if dispatch is None:
                with contextlib.suppress(OmnigentError, httpx.HTTPError, RuntimeError, ValueError):
                    _instr_entry_ds = await _resolve_session_spec_entry(conv_id)
                    _instr_spec_ds = _unwrap_resolved_spec(_instr_entry_ds)
                    if _instr_spec_ds is not None:
                        _per_req_instr = cast(str | None, body.get("instructions"))
                        _authored_ds = raw_author_instructions(_instr_spec_ds) is not None
                        _ds_labels = await _rollover_labels_for_session(conv_id)
                        _ic_ds = InstructionComposition(
                            authored_present=_authored_ds,
                            composed=build_instructions_nullable(
                                _instr_spec_ds, _per_req_instr, [], labels=_ds_labels
                            ),
                        )
                        if _ic_ds.composed is not None:
                            _instr_body = {
                                **body,
                                "instructions": build_instructions(
                                    _instr_spec_ds, _per_req_instr, [], labels=_ds_labels
                                ),
                            }
                        if _authored_ds and harness_name:
                            _ds_caps = harness_capabilities().get(harness_name)
                            _ds_delivery = (
                                _ds_caps.instruction_delivery
                                if _ds_caps is not None
                                else InstructionDelivery.UNKNOWN
                            )
                            _ds_warn_key = (harness_name, _ds_delivery)
                            _already_warned_ds = _ds_warn_key in _instruction_delivery_warned.get(
                                conv_id, ()
                            )
                            if not _already_warned_ds:
                                _instruction_delivery_warned.setdefault(conv_id, set()).add(
                                    _ds_warn_key
                                )
                                if _ds_delivery in (
                                    InstructionDelivery.NOT_DELIVERED,
                                    InstructionDelivery.UNKNOWN,
                                ):
                                    _logger.warning(
                                        "agent instructions not delivered for session=%s "
                                        "harness=%s delivery=%s — authored instructions "
                                        "accepted but have no delivery channel on this harness.",
                                        conv_id,
                                        harness_name,
                                        _ds_delivery.value,
                                        extra={"session_id": conv_id},
                                    )
            # Re-warn on every turn when session-create established a miss.
            _ds_sa = _session_sub_agent_names.get(conv_id)
            if _ds_sa and _session_sub_agent_resolved.get(conv_id) is False:
                _warn_unresolved_sub_agent(conv_id, _ds_sa)
            event_body = _wrap_as_message_event(_instr_body)
            _inject_mcp_schemas(event_body, _mcp_schemas)
            _response_id: str | None = None
            _posted_at = time.monotonic()
            try:
                async with client.stream(
                    "POST",
                    f"/v1/sessions/{conv_id}/events",
                    json=event_body,
                    timeout=None,
                ) as harness_resp:
                    if harness_resp.status_code != 200:
                        _logger.error(
                            "harness rejected turn delivery for %s with status %d",
                            conv_id,
                            harness_resp.status_code,
                            extra={
                                "session_id": conv_id,
                                "event_name": "harness_turn_rejected",
                                "attributes": {
                                    "harness": harness_name,
                                    "http_status": harness_resp.status_code,
                                },
                            },
                        )
                        _fail_status = _response_failed_payload(
                            {"status": harness_resp.status_code}, source="harness"
                        )
                        _publish_event(
                            conv_id,
                            _fail_status,
                        )
                        _on_proxy_stream_end(
                            conv_id,
                            error={"status": harness_resp.status_code},
                        )
                        yield _response_failed_event(
                            {"status": harness_resp.status_code}, source="harness"
                        )
                        return

                    _omnigent_task_id = cast(str | None, body.get("task_id"))
                    _buffer = ""
                    _dispatch_tasks: list[_asyncio.Task[object]] = []
                    _text_acc: list[str] = []
                    _stream_failed_error: _JsonObject | None = None
                    _tool_starts = ToolStarts()
                    async for chunk in harness_resp.aiter_text():
                        _buffer += chunk
                        while "\n\n" in _buffer:
                            frame, _, _buffer = _buffer.partition("\n\n")
                            raw_sse_bytes = (frame + "\n\n").encode("utf-8")

                            data_line = next(
                                (line for line in frame.splitlines() if line.startswith("data:")),
                                None,
                            )
                            if data_line is not None:
                                try:
                                    event = _json.loads(data_line[5:].strip())
                                except _json.JSONDecodeError:
                                    event = None
                            else:
                                event = None

                            _defer_publish = False
                            if event is not None:
                                if event.get("type") == "response.created":
                                    resp_obj = event.get("response") or {}
                                    _response_id = resp_obj.get("id")
                                    if _response_id and conv_id:
                                        _resp_to_conv[_response_id] = conv_id
                                        _live_response_id[conv_id] = _response_id
                                        manager.mark_in_flight(conv_id, _response_id)

                                _overflow = _is_context_overflow_error(event)
                                if _overflow is not None:
                                    raise _ContextWindowOverflow(*_overflow)

                                _evt_type = event.get("type")
                                if (
                                    not _cold_start_logged
                                    and isinstance(_evt_type, str)
                                    and _evt_type.startswith(
                                        ("response.output_", "response.reasoning")
                                    )
                                ):
                                    _log_cold_start(conv_id, time.monotonic() - _posted_at)
                                if (
                                    _evt_type == "response.compaction.in_progress"
                                    and conv_id in _sdk_compact_inprogress
                                ):
                                    # _handle_claude_sdk_compact already published an
                                    # up-front spinner; drop the executor's own duplicate
                                    # so the web renders a single compaction spinner.
                                    continue
                                if _evt_type in _TURN_END_EVENTS:
                                    # The fill the turn reported, on any end (a stopped or failed
                                    # turn too): the size handoff and the hard limit read it. The
                                    # fold runs in the engine (ADR 0031), not here.
                                    _usage = (event.get("response") or {}).get("usage")
                                    _reported = (
                                        _usage.get("context_tokens")
                                        if isinstance(_usage, dict)
                                        else None
                                    )
                                    if isinstance(_reported, int) and _reported > 0:
                                        _superside_chat_last_context_tokens[conv_id] = _reported
                                if _evt_type == "injection.offered":
                                    # Offered for the turn's next step; its outcome follows.
                                    _answer_offer(event.get("injection_id"), taken=True)
                                    continue
                                if _evt_type == "injection.consumed":
                                    _inj_id = event.get("injection_id")
                                    _answer_offer(_inj_id, taken=True)
                                    if _inj_id is not None:
                                        for _m in _remove_buffered(conv_id, _inj_id):
                                            _session_histories.setdefault(conv_id, []).append(
                                                {
                                                    "type": "message",
                                                    "role": _m.get("role", "user"),
                                                    "content": _m.get("content", []),
                                                }
                                            )
                                    continue
                                if _evt_type == "injection.refused":
                                    # The harness cannot take it into the running turn: the
                                    # buffered copy runs as the next turn, unless it was meant
                                    # only for this one.
                                    _inj_id = event.get("injection_id")
                                    _answer_offer(_inj_id, taken=False)
                                    # A refused steer can leave the client dropped (cold next).
                                    _superside_chat_prefix_pending.pop(conv_id, None)
                                    _superside_chat_prefix_sent.pop(conv_id, None)
                                    for _m in list(_session_message_buffers.get(conv_id) or []):
                                        if _m.get("injection_id") != _inj_id:
                                            continue
                                        if _m.get(_ONLY_RUNNING_TURN):
                                            _remove_buffered(conv_id, _inj_id)
                                            continue
                                        _m.pop("injection_id", None)  # no longer offered: waits
                                        _queue_changed(conv_id, _m)
                                    continue
                                if _evt_type == "response.output_text.delta":
                                    delta = event.get("delta")
                                    if delta is not None:
                                        if conv_id in _turn_started_mono:
                                            await _log_first_token(conv_id)
                                        _text_acc.append(delta)
                                        if _response_id and str(delta).strip():
                                            _turn_said[conv_id] = _response_id
                                elif _evt_type == "response.completed":
                                    _stream_failed_error = None
                                    if _text_acc:
                                        _session_histories.setdefault(conv_id, []).append(
                                            {
                                                "type": "message",
                                                "role": "assistant",
                                                "content": [
                                                    {
                                                        "type": "output_text",
                                                        "text": "".join(_text_acc),
                                                    }
                                                ],
                                            }
                                        )
                                        _text_acc.clear()
                                    _commit_superside_chat_prefix(conv_id)
                                    _history_process[conv_id] = _warm_client_serial(conv_id)
                                    notify_turn_end(conv_id)  # Feature.on_turn_end
                                elif _evt_type == "response.failed":
                                    _err = event.get("error") or (event.get("response") or {}).get(
                                        "error"
                                    )
                                    _stream_failed_error = (
                                        _err
                                        if isinstance(_err, dict)
                                        else {"message": "harness turn failed"}
                                    )
                                elif _evt_type == "response.output_item.done":
                                    _item = event.get("item")
                                    _it = _item.get("type") if isinstance(_item, dict) else None
                                    if _it in ("function_call", "function_call_output") and any(
                                        h.get("type") == _it
                                        and h.get("call_id") == _item.get("call_id")
                                        for h in (_session_histories.get(conv_id) or [])[-50:]
                                    ):
                                        # A call is announced more than once (in progress, then
                                        # completed); the history keeps it once.
                                        _it = None
                                    if _it in ("function_call", "function_call_output"):
                                        _hist = _session_histories.setdefault(conv_id, [])
                                        if _it == "function_call":
                                            if _text_acc:
                                                # The words before a call are their own message, so
                                                # the turn's last message (a Helper's Result) is
                                                # only what it said after its last call.
                                                _hist.append(
                                                    {
                                                        "type": "message",
                                                        "role": "assistant",
                                                        "content": [
                                                            {
                                                                "type": "output_text",
                                                                "text": "".join(_text_acc),
                                                            }
                                                        ],
                                                    }
                                                )
                                                _text_acc.clear()
                                            _hist.append(
                                                {
                                                    "type": "function_call",
                                                    "call_id": _item["call_id"],
                                                    "name": _item["name"],
                                                    "arguments": _item["arguments"],
                                                }
                                            )
                                        elif _it == "function_call_output":
                                            _hist.append(
                                                {
                                                    "type": "function_call_output",
                                                    "call_id": _item["call_id"],
                                                    "output": _item["output"],
                                                }
                                            )
                                elif _evt_type == "response.compaction.completed" and event.get(
                                    "summary"
                                ):
                                    # A real compaction landed; the completed event clears
                                    # the up-front spinner, so drop the pending flag and
                                    # skip the stream-end `failed` fallback.
                                    _sdk_compact_inprogress.discard(conv_id)
                                    await _handle_harness_compaction(conv_id, event)

                                if is_action_required(event):
                                    tool_name = get_tool_name(event)
                                    is_mcp = tool_name in _mcp_tool_names
                                    _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                        _session_spec_cache.get(conv_id)
                                    )
                                    _is_spec_local = _is_spec_local_native_python_tool(
                                        _spec_for_dispatch_hint,
                                        tool_name,
                                    )
                                    if (
                                        not _is_spec_local
                                        and not is_mcp
                                        and not should_dispatch_locally(tool_name)
                                    ):
                                        (
                                            _spec_for_dispatch_hint_entry,
                                            _lazy_hint_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_hint_err is None:
                                            _spec_for_dispatch_hint = _unwrap_resolved_spec(
                                                _spec_for_dispatch_hint_entry
                                            )
                                            _is_spec_local = _is_spec_local_native_python_tool(
                                                _spec_for_dispatch_hint,
                                                tool_name,
                                            )
                                    _should_dispatch = _should_dispatch_tool_locally(
                                        tool_name,
                                        dispatch=dispatch,
                                        is_mcp=is_mcp,
                                        is_runner_builtin=should_dispatch_locally(tool_name),
                                        is_spec_local=_is_spec_local,
                                    )
                                    if _should_dispatch and _response_id:
                                        _defer_publish = True
                                        (
                                            _spec_for_dispatch_entry,
                                            _lazy_err,
                                        ) = await _resolve_turn_spec_lazy()
                                        if _lazy_err is not None:
                                            _err_type, _err_msg = _lazy_err
                                            _fail = _response_failed_payload(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            _publish_event(conv_id, _fail)
                                            _on_proxy_stream_end(
                                                conv_id,
                                                error={
                                                    "message": _err_msg,
                                                    "type": _err_type,
                                                },
                                                owner_response_id=_response_id,
                                            )
                                            yield _response_failed_event(
                                                {"message": _err_msg, "type": _err_type}
                                            )
                                            return
                                        _local_tool_workdir = _resolved_workdir_for_spec(
                                            _spec_for_dispatch_entry,
                                            runner_workspace,
                                        )
                                        _spec_for_dispatch = _unwrap_resolved_spec(
                                            _spec_for_dispatch_entry
                                        )
                                        _dispatch_workspace = await _session_runtime_cwd(conv_id)
                                        event[_RUNNER_DISPATCHED_FIELD] = True
                                        raw_sse_bytes = _encode_sse_event(event)
                                        _agent_id_for_dispatch = cast(
                                            str | None, body.get("agent_id")
                                        )
                                        _dispatch_mcp = ProxyMcpManager(
                                            conv_id,
                                            server_client,
                                            publish_event=_publish_event,
                                            execution_registry=mcp_execution_registry,
                                        )
                                        _dispatch_tasks.append(
                                            _asyncio.create_task(
                                                _after_pause_hold(
                                                    conv_id,
                                                    dispatch_tool_locally(
                                                        tool_name=tool_name,
                                                        call_id=get_call_id(event),
                                                        arguments=get_arguments(event),
                                                        response_id=_response_id,
                                                        harness_client=client,
                                                        server_client=server_client,
                                                        terminal_registry=terminal_registry,
                                                        resource_registry=resource_registry,
                                                        agent_spec=_spec_for_dispatch,
                                                        conversation_id=conv_id,
                                                        task_id=_omnigent_task_id or _response_id,
                                                        agent_id=_agent_id_for_dispatch,
                                                        agent_name=cast(
                                                            str | None, body.get("model")
                                                        ),
                                                        runner_workspace=_dispatch_workspace,
                                                        local_tool_workdir=_local_tool_workdir,
                                                        mcp_manager=cast(
                                                            "RunnerMcpManager", _dispatch_mcp
                                                        ),
                                                        session_inbox=_session_inboxes.get(
                                                            conv_id
                                                        ),
                                                        session_async_tasks=_session_async_tasks.get(
                                                            conv_id
                                                        ),
                                                        publish_event=_publish_event,
                                                        filesystem_registry=filesystem_registry,
                                                        effective_harness=_session_harness_name(
                                                            conv_id
                                                        ),
                                                        labels=await _tool_labels_for_session(
                                                            conv_id
                                                        ),
                                                    ),
                                                )
                                            )
                                        )
                                        _dispatch_tasks[-1].add_done_callback(
                                            functools.partial(
                                                _recover_failed_tool_dispatch,
                                                conv_id=conv_id,
                                                response_id=_response_id,
                                            )
                                        )

                                if _evt_type == "policy_evaluation.requested":
                                    _eval_id = event.get("evaluation_id", "")
                                    _eval_phase = event.get("phase", "")
                                    if _eval_phase == "PHASE_LLM_REQUEST":
                                        # Asked right after the executor holds its CLI client.
                                        _mark_turn(conv_id, "client")
                                    _eval_data = event.get("data") or {}
                                    _dispatch_tasks.append(
                                        _asyncio.create_task(
                                            _evaluate_policy_via_omnigent(
                                                server_client=server_client,
                                                harness_client=client,
                                                conversation_id=conv_id,
                                                evaluation_id=_eval_id,
                                                phase=_eval_phase,
                                                data=_eval_data,
                                                # A dead verdict-delivery channel
                                                # parks the harness turn forever;
                                                # route it to the recovery entry,
                                                # binding THIS turn's response id
                                                # so a delayed failure can't cancel
                                                # a newer turn (ownership gate).
                                                on_delivery_failure=functools.partial(
                                                    _resync_turn_state_on_delivery_failure,
                                                    response_id=_response_id,
                                                ),
                                                hold=mcp_execution_registry.hold,
                                            )
                                        )
                                    )
                                    continue

                            if event is None:
                                yield raw_sse_bytes
                                continue
                            if event.get("type") == "response.failed":
                                _input_stable_id = body.get("input_stable_id")
                                if isinstance(_input_stable_id, str):
                                    event["input_stable_id"] = _input_stable_id
                            # A tool call is stored when it starts (ADR 0026), so the person
                            # sees the step while it runs; its completed repeat is dropped.
                            if _tool_starts.is_repeat(event):
                                continue
                            if event.get("type") in _TURN_END_EVENT_TYPES:
                                for _stopped in _tool_starts.unfinished():
                                    _publish_event(conv_id, _stopped)
                                    yield _encode_sse_event(_stopped)
                            if (_started := _tool_starts.started(event)) is not None:
                                _publish_event(conv_id, _started)
                                yield _encode_sse_event(_started)
                            if not _defer_publish and event.get("type") != "response.created":
                                _publish_event(conv_id, event)
                            if dispatch is not None and event.get(_RUNNER_DISPATCHED_FIELD):
                                pass
                            else:
                                yield raw_sse_bytes

                    # The harness stream ended, so no tool result can reach this turn: a
                    # dispatch still parked on an approval would otherwise hold it open.
                    pending_approvals.cancel_session(conv_id, "turn_ended")
                    if _dispatch_tasks:
                        await _asyncio.gather(*_dispatch_tasks, return_exceptions=True)

                    # _on_proxy_stream_end clears any claude-sdk `/compact`
                    # spinner (publishing `failed` when no compaction landed);
                    # it is the single convergence point for every turn-end path.
                    _on_proxy_stream_end(
                        conv_id, error=_stream_failed_error, owner_response_id=_response_id
                    )

            except _ContextWindowOverflow as overflow:
                _error = {
                    "code": "context_length_exceeded",
                    "message": (
                        f"Context window exceeded: {overflow.actual_tokens} tokens "
                        f"> {overflow.max_tokens} max"
                    ),
                    "type": "_ContextWindowOverflow",
                }
                _overflow_fail = _response_failed_payload(_error, source="llm")
                _publish_event(conv_id, _overflow_fail)
                _on_proxy_stream_end(conv_id, error=_error, owner_response_id=_response_id)
                yield _response_failed_event(_error, source="llm")

            except (httpx.HTTPError, RuntimeError) as exc:
                # Name the type as well as the text: the messageless httpx
                # errors otherwise log a trailing colon and nothing, so one
                # signature covered every transport cause.
                _logger.exception(
                    "proxy stream connection error for %s: %s: %s",
                    conv_id,
                    type(exc).__name__,
                    exc,
                    extra={
                        "session_id": conv_id,
                        "event_name": "harness_stream_failed",
                        "attributes": {
                            "harness": harness_name,
                            "response_id": _response_id,
                            "exception_type": type(exc).__name__,
                        },
                    },
                )
                _error = {
                    "code": "connection_error",
                    "message": _harness_stream_failure_message(conv_id, exc),
                    "type": type(exc).__name__,
                }
                _http_fail = _response_failed_payload(_error, source="harness")
                _publish_event(conv_id, _http_fail)
                # The harness is gone; a dispatch parked on an approval has no turn to return to.
                pending_approvals.cancel_session(conv_id, "harness_lost")
                _on_proxy_stream_end(conv_id, error=_error, owner_response_id=_response_id)
                yield _response_failed_event(_error, source="harness")

        return StreamingResponse(
            proxy_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/sessions/{conversation_id}/events")
    async def post_session_events(
        conversation_id: str,
        request: Request,
        stream: bool = Query(default=False),
    ) -> Response:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "detail": (
                        "Runner /v1/sessions/{conv}/events needs a HarnessProcessManager; "
                        "build with create_runner_app(process_manager=...) "
                        "after calling await mgr.start()."
                    ),
                },
            )

        body = await request.json()
        body_type = body.get("type") if isinstance(body, dict) else None
        _logger.info(
            "post_session_events: conv=%s type=%s active=%s buffer_len=%d content_types=%s "
            "model_override=%s",
            conversation_id,
            body_type,
            conversation_id in _active_turns,
            len(_session_message_buffers.get(conversation_id, [])),
            [b.get("type") for b in body.get("content", []) if isinstance(b, dict)]
            if isinstance(body, dict)
            else "N/A",
            body.get("model_override") if isinstance(body, dict) else None,
            extra={"session_id": conversation_id},
        )
        if body_type == "message" or body_type is None:
            if not isinstance(body, dict):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_request",
                        "detail": "session message body must be a JSON object",
                    },
                )
            message_body = dict(body)
            message_body["conversation_id"] = conversation_id
            await _register_scheduled_helper_work(conversation_id)
            await _restore_pause(conversation_id)

            if stream:
                # The ?stream=true direct-dispatch path bypasses
                # _run_turn_bg_setup_and_stream entirely, so it never threads
                # context_mode, the Memory Profile block, the idle-refresh
                # check, or the deferred-refresh consumer onto the turn — a
                # superside-chat turn sent this way would run with the
                # engine's own competing compaction/memory still on (ADR
                # 0001: Omnigent, not the engine, owns that). Refuse rather
                # than silently degrade (docs/context.md, ADR 0006).
                from omnigent.context.labels import is_superside_chat

                _stream_guard_labels = await _rollover_labels_for_session(conversation_id)
                if is_superside_chat(_stream_guard_labels):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": "stream_not_supported_for_superside_chat",
                            "detail": (
                                "?stream=true is not supported for superside-chat "
                                "sessions — it skips Rollover's turn-start hooks "
                                "(idle refresh, the Memory Profile block, context "
                                "mode). Send the turn without ?stream=true."
                            ),
                        },
                    )

            _seq = _ingest_next_seq.get(conversation_id, 0)
            _ingest_next_seq[conversation_id] = _seq + 1
            _cond = _ingest_cond.get(conversation_id)
            if _cond is None:
                _cond = asyncio.Condition()
                _ingest_cond[conversation_id] = _cond
            async with _cond:
                while _ingest_now_serving.get(conversation_id, 0) != _seq:
                    await _cond.wait()
            try:
                _persisted_item_id = message_body.get("persisted_item_id")
                if (
                    _persisted_item_id
                    and _recovery_resumed_item_ids.get(conversation_id) == _persisted_item_id
                ):
                    # Session init already started a recovery turn for this
                    # exact persisted message (it was the trailing user item in
                    # history). Running it again would answer it twice.
                    _recovery_resumed_item_ids.pop(conversation_id, None)
                    _logger.info(
                        "post_session_events: message %s already answered by the "
                        "recovery turn; not starting another conv=%s",
                        _persisted_item_id,
                        conversation_id,
                        extra={"session_id": conversation_id},
                    )
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "already_running",
                            "turn": _turn_bind_epoch.get(conversation_id),
                            "detail": "The recovery turn is already answering this message.",
                        },
                    )
                _raw_content = message_body.get("content")
                if isinstance(_raw_content, list):
                    message_body["content"] = await _resolve_forwarded_message_content(
                        _raw_content,
                        session_id=conversation_id,
                        server_client=server_client,
                    )

                _if_running = bool(message_body.pop("if_running", False))
                _not_running = JSONResponse(
                    status_code=409,
                    content={
                        "error": "not_running",
                        "detail": "No turn is running to take this message.",
                    },
                )
                if _if_running and conversation_id not in _active_turns:
                    # Meant for the turn that is running; with none, it must not start one.
                    return _not_running
                if conversation_id in _active_turns:
                    _awaiting_approval = pending_approvals.has_pending(conversation_id)
                    # Offered only whole: a message the harness cannot take as it is (an
                    # image, a file) waits and runs as a turn instead of losing a part.
                    _can_forward = (
                        not _awaiting_approval
                        and conversation_id in _live_response_id
                        and _is_steerable(message_body.get("content"))
                    )
                    if _if_running and not _can_forward:
                        return _not_running
                    _logger.info(
                        "post_session_events: buffering message for active turn conv=%s "
                        "awaiting_approval=%s",
                        conversation_id,
                        _awaiting_approval,
                        extra={"session_id": conversation_id},
                    )
                    if not _can_forward:
                        _session_message_buffers.setdefault(conversation_id, []).append(
                            message_body
                        )
                        # Nothing will read it before the turn ends.
                        _queue_changed(conversation_id, message_body)
                        return JSONResponse(
                            status_code=202,
                            content={
                                "status": "buffered",
                                "turn": _turn_bind_epoch.get(conversation_id),
                                "detail": "Message buffered; it runs after the active turn.",
                            },
                        )
                    _injection_id = f"inj_{uuid.uuid4().hex[:16]}"
                    message_body["injection_id"] = _injection_id
                    _offer: asyncio.Future[bool] | None = None
                    if _if_running:
                        # Never its own turn: refused by the running one, it is cancelled.
                        message_body[_ONLY_RUNNING_TURN] = True
                        _offer = asyncio.get_running_loop().create_future()
                        _offer_answers[_injection_id] = (conversation_id, _offer)
                    _session_message_buffers.setdefault(conversation_id, []).append(message_body)
                    _forwarded = False
                    if process_manager is not None:
                        _harness_body = {
                            k: v for k, v in message_body.items() if k != _ONLY_RUNNING_TURN
                        }
                        # A Result steered in gets its wake reminder here, as a turn's prefix
                        # does (ADR 0031); the stored notice never holds it.
                        _reminder = result_notice_reminder(
                            _message_texts(message_body.get("content"))
                        )
                        if _reminder:
                            _harness_body["content"] = _prefix_content_with_tail(
                                _harness_body.get("content"), _reminder
                            )
                        try:
                            _hc = await process_manager.get_client(conversation_id, "any")
                            _injection_resp = await _hc.post(
                                f"/v1/sessions/{conversation_id}/events",
                                json=_harness_body,
                                timeout=5.0,
                            )
                            _forwarded = _injection_resp.status_code < 400
                            if not _forwarded:
                                _logger.warning(
                                    "post_session_events: mid-turn injection forward rejected "
                                    "conv=%s status=%s body=%s",
                                    conversation_id,
                                    _injection_resp.status_code,
                                    _response_body_preview(_injection_resp),
                                    extra={"session_id": conversation_id},
                                )
                        except (httpx.HTTPError, RuntimeError, asyncio.TimeoutError):
                            _logger.debug(
                                "mid-turn injection forward failed for %s; "
                                "LLM will see message on next turn",
                                conversation_id,
                                exc_info=True,
                                extra={"session_id": conversation_id},
                            )
                    if not _forwarded:
                        if _if_running:
                            _answer_offer(_injection_id, taken=False)
                            _remove_buffered(conversation_id, _injection_id)
                            return _not_running
                        message_body.pop("injection_id", None)  # never offered: it waits
                        _queue_changed(conversation_id, message_body)
                    elif _offer is not None and not await _offer_verdict(_injection_id, _offer):
                        # The running turn refused it: no turn will read it, nothing is kept.
                        _remove_buffered(conversation_id, _injection_id)
                        return _not_running
                    return JSONResponse(
                        status_code=202,
                        content={
                            "status": "buffered",
                            # The message joins this turn: steered in, or run right after it.
                            "turn": _turn_bind_epoch.get(conversation_id),
                            "detail": "Message buffered; active turn will process it.",
                        },
                    )

                new_item = {
                    "type": "message",
                    "role": message_body.get("role", "user"),
                    "content": message_body.get("content", []),
                }
                if conversation_id in _session_histories:
                    _session_histories[conversation_id].append(new_item)
                else:
                    persisted_item_id = message_body.get("persisted_item_id")
                    loaded = await _load_history_as_input(
                        conversation_id,
                        drop_item_id=persisted_item_id,
                    )
                    loaded.append(new_item)
                    _session_histories[conversation_id] = loaded

                _begin_turn_slot(conversation_id)
                _logger.info(
                    "post_session_events: starting background turn conv=%s",
                    conversation_id,
                    extra={"session_id": conversation_id},
                )

                _publish_turn_status(conversation_id, "running")

                if stream:
                    response = await _stream_message_to_harness(message_body, conversation_id)
                    if not isinstance(response, StreamingResponse):
                        _on_proxy_stream_end(
                            conversation_id,
                            error=_harness_error_response_error(response),
                        )
                    return response

                _turn_task = asyncio.create_task(
                    _run_turn_bg(message_body, conversation_id),
                    name=f"turn-{conversation_id}",
                )
                _active_turns[conversation_id] = _turn_task
                _turn_task.add_done_callback(
                    _background_tasks.discard,
                )
                _background_tasks.add(_turn_task)

                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "accepted",
                        "turn": _turn_bind_epoch.get(conversation_id),
                        "detail": "Turn started.",
                    },
                )
            finally:
                async with _cond:
                    _ingest_now_serving[conversation_id] = _seq + 1
                    _cond.notify_all()

        if body_type == "interrupt":
            # A stop also drops the messages waiting behind the turn.
            _drop_message_buffer(conversation_id)
            data = body.get("data")
            note = data.get("note") if isinstance(data, dict) else None
            await _cancel_inprocess_turn(
                conversation_id, note if isinstance(note, str) and note else None
            )
            return Response(status_code=204)

        if body_type == "stop_session":
            _drop_message_buffer(conversation_id)
            await _cancel_inprocess_turn(conversation_id)
            return Response(status_code=204)

        if body_type == "pause":
            # A Helper's pause (ADR 0027): its next tool call waits in the registry.
            _pause_session(conversation_id)
            return Response(status_code=204)

        if body_type == "resume":
            if mcp_execution_registry.resume(conversation_id):
                _publish_session_status(conversation_id, "running")
            return Response(status_code=204)

        if body_type == "context_reset":
            # The server stored a reset checkpoint (``POST /sessions/{id}/reset``): drop the
            # cached history and warm client so the next turn cold-starts from it.
            await _drop_superside_chat_warm_client(conversation_id, reason="the context was reset")
            return Response(status_code=204)

        if body_type == "workspace_change":
            # The server stored a new working directory; drop every cached copy so the next
            # tool call re-reads it (tools resolve it per call via ``_session_runtime_cwd``).
            _forget_session_workspace(conversation_id)
            return Response(status_code=204)

        if body_type == "effort_change":
            effort = body.get("effort") if isinstance(body, dict) else None
            if effort is not None and not isinstance(effort, str):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'effort' must be a string or null",
                    },
                )
            # In-process harnesses apply the effort on their next turn, from the
            # forwarded turn body (see ``_turn_reasoning``).
            if effort:
                _session_reasoning_effort[conversation_id] = effort
            else:
                _session_reasoning_effort.pop(conversation_id, None)
            return Response(status_code=204)

        if body_type == "model_change":
            # The SDK harnesses read the model from the forwarded turn body.
            return Response(status_code=204)

        if body_type == "compact":
            if _session_harness_name(conversation_id) == "claude-sdk":
                return await _handle_claude_sdk_compact(conversation_id)
            return Response(status_code=204)

        if body_type == "approval":
            _data = body.get("data") or body
            _elicit_action = _data.get("action", "")
            # ``content`` is the person's answer when the prompt asked for
            # more than consent (an MCP ``requestedSchema``). Dropping it here
            # is what used to make the awaiting caller invent one.
            _elicit_content = _data.get("content")
            pending_approvals.resolve(
                _data.get("elicitation_id", ""),
                _elicit_action == "accept",
                _elicit_content if isinstance(_elicit_content, dict) else None,
            )
            # A decline stops the turn, unless the verdict says not to: an approval answered
            # later from an inbox wants the agent to see "declined" and carry on.
            if _elicit_action == "decline" and _data.get("interrupt", True) is not False:
                try:
                    _int_client = await process_manager.get_client(conversation_id, "any")
                    await _int_client.post(
                        f"/v1/sessions/{conversation_id}/events",
                        json={"type": "interrupt"},
                        timeout=5.0,
                    )
                except Exception:  # noqa: BLE001 — best-effort; deny path continues
                    pass
            body = {**{k: v for k, v in _data.items() if k != "interrupt"}, "type": "approval"}

        try:
            harness_client = await process_manager.get_client(conversation_id, "any")
        except NoLiveHarnessError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no_live_harness",
                    "detail": "no harness subprocess is running for this conversation",
                },
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "no_harness",
                    "detail": _client_safe_error_detail(exc, context="harness lookup"),
                },
            )
        try:
            resp = await harness_client.post(
                f"/v1/sessions/{conversation_id}/events",
                json=body,
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "harness_forward_failed",
                    "detail": _client_safe_error_detail(exc, context="harness event forward"),
                    "event_type": body_type,
                },
            )
        return _forward_harness_response(resp)

    async def _resolve_conversation_id(response_id: str) -> str | None:
        return _resp_to_conv.get(response_id)

    @app.get("/v1/sessions/{session_id}/resources")
    async def list_session_resources(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        type: str | None = Query(default=None),
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        spec = await _resolve_session_agent_spec(session_id)
        full = resource_registry.list_resources(
            session_id,
            resource_type=cast(_ResourceType | None, type),
            agent_spec=spec,
        )
        page = paginate_in_memory(
            full.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    def _build_typed_list_response(
        session_id: str,
        resource_type: _ResourceType,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        from omnigent.entities.pagination import paginate_in_memory

        filtered = resource_registry.list_resources(
            session_id,
            resource_type=resource_type,
        )
        page = paginate_in_memory(
            filtered.data,
            id_fn=lambda r: r.id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [session_resource_view_to_dict(r) for r in page.data]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": data,
                "first_id": page.first_id,
                "last_id": page.last_id,
                "has_more": page.has_more,
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/environments")
    async def list_session_environments(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "environment",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    def _environment_reach(root: str, agent_spec: AgentSpec | None) -> dict[str, object]:
        """Describe what the default environment's file browsing can reach.

        ``unconfined`` reports that no OS-level sandbox is applied, so the
        environment's shell already reads anything the runner can and a
        browser may range beyond the listed roots. ``roots`` always names
        the grants the environment's own file tools reach, workspace first,
        so a caller can anchor on the workspace and label the rest.

        :param root: Resolved absolute environment root.
        :param agent_spec: Agent spec for the session, if any.
        :returns: JSON-ready ``{"unconfined": bool, "roots": [...]}``.
        """
        from omnigent.harnesses.sandbox import (
            ReachableRoot,
            is_unconfined,
            reach_payload,
            reachable_roots,
            resolve_sandbox,
        )

        root_path = Path(root)
        spec_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None
        if spec_os_env is None:
            # No spec to resolve (dev/standalone): report the root alone
            # rather than guessing a wider reach.
            return reach_payload(
                [ReachableRoot(path=root_path, access="write", origin="cwd", kind="tree")],
                unconfined=False,
            )
        policy = resolve_sandbox(spec_os_env, root_path)
        return reach_payload(reachable_roots(root_path, policy), unconfined=is_unconfined(policy))

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}")
    async def get_session_environment(
        session_id: str,
        environment_id: str,
    ) -> JSONResponse:
        agent_spec = await _resolve_session_agent_spec(session_id)
        resource = resource_registry.get_resource(
            session_id,
            environment_id,
        )
        if resource is None or resource.type != "environment":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Environment {environment_id!r} not found",
                    }
                },
            )
        content = session_resource_view_to_dict(resource)
        if environment_id == DEFAULT_ENVIRONMENT_ID:
            root = resource_registry.compute_default_env_root(session_id, agent_spec)
            if root is not None:
                raw_metadata = content.get("metadata")
                metadata: dict[str, object] = (
                    dict(cast(Mapping[str, object], raw_metadata))
                    if isinstance(raw_metadata, Mapping)
                    else {}
                )
                metadata["root"] = root
                home = os.path.expanduser("~")
                if os.path.isabs(home):
                    metadata["home"] = home
                metadata["reachable"] = _environment_reach(root, agent_spec)
                content = {**content, "metadata": metadata}
        return JSONResponse(
            status_code=200,
            content=content,
        )

    @app.get("/v1/sessions/{session_id}/resources/terminals")
    async def list_session_terminals(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        return _build_typed_list_response(
            session_id,
            "terminal",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.post("/v1/sessions/{session_id}/resources/terminals")
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        terminal_name = body.get("terminal")
        session_key = body.get("session_key")
        if not terminal_name or not session_key:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": ("'terminal' and 'session_key' are required"),
                    }
                },
            )

        from omnigent.harnesses.datamodel import OSEnvSpec, TerminalEnvSpec

        cwd_override = body.get("cwd")
        sandbox_override = body.get("sandbox")
        spec = body.get("spec") or {}

        agent_spec = await _resolve_session_agent_spec(session_id)
        agent_os_env = getattr(agent_spec, "os_env", None) if agent_spec is not None else None

        declared_terminal = None
        if agent_spec is not None:
            terminals_map = getattr(agent_spec, "terminals", None) or {}
            declared_terminal = terminals_map.get(terminal_name)

        if declared_terminal is not None:
            from omnigent.tools.builtins.sys_terminal import (
                _materialize_terminal_spec_for_launch,
                _synthesize_parent_os_env,
            )

            default_root = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = _materialize_terminal_spec_for_launch(declared_terminal, default_root)
            agent_os_env = _synthesize_parent_os_env(agent_os_env, default_root)
            cwd_override = cwd_override or spec.get("cwd")
        else:
            spec_cwd = spec.get("cwd")
            if spec_cwd is None or spec_cwd in (".", "./"):
                spec_cwd = resource_registry.compute_default_env_root(session_id, agent_spec)
            env_spec = TerminalEnvSpec(
                os_env=OSEnvSpec(
                    type=spec.get("os_env_type", "caller_process"),
                    cwd=spec_cwd,
                    sandbox=(agent_os_env.sandbox if agent_os_env is not None else None),
                ),
                command=spec.get("command", "bash"),
                args=spec.get("args", []),
                env=spec.get("env", {}),
                scrollback=spec.get("scrollback", 10000),
                tmux_allow_passthrough=bool(spec.get("tmux_allow_passthrough", False)),
                tmux_start_on_attach=bool(spec.get("tmux_start_on_attach", False)),
            )
        try:
            resource_view = await resource_registry.launch_auxiliary_terminal(
                session_id=session_id,
                terminal_name=terminal_name,
                session_key=session_key,
                spec=env_spec,
                cwd_override=cwd_override,
                sandbox_override=sandbox_override,
                parent_os_env=agent_os_env,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "terminal_launch_failed",
                        "message": _client_safe_error_detail(exc, context="terminal launch"),
                    }
                },
            )

        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource_view),
        )

    @app.get("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def get_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        resource = await resource_registry.get_terminal_resource(
            session_id,
            terminal_id,
        )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.post("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer")
    async def transfer_session_terminal(
        session_id: str,
        terminal_id: str,
        request: Request,
    ) -> JSONResponse:
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'target_session_id' is required",
                    }
                },
            )
        try:
            resource = await resource_registry.transfer_terminal(
                source_session_id=session_id,
                target_session_id=target_session_id,
                terminal_id=terminal_id,
            )
        except RuntimeError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "resource_conflict",
                        "message": _client_safe_error_detail(exc, context="terminal transfer"),
                    }
                },
            )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"Terminal {terminal_id!r} not found",
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    @app.delete("/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
    async def delete_session_terminal(
        session_id: str,
        terminal_id: str,
    ) -> JSONResponse:
        closed = await resource_registry.close_terminal(
            session_id,
            terminal_id,
        )
        if not closed:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Terminal {terminal_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "id": terminal_id,
                "object": "session.resource.deleted",
                "deleted": True,
            },
        )

    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def terminal_resource_attach_ws(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
    ) -> None:
        await websocket.accept()
        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            await websocket.close(
                code=WS_CLOSE_TERMINAL_NOT_FOUND,
                reason="terminal resource not found or not running",
            )
            return
        await bridge_tmux_control_to_websocket(
            websocket,
            socket_path=str(entry.instance.socket_path),
            tmux_target=entry.instance.tmux_target,
            read_only=read_only,
            on_client_interaction=entry.instance.note_client_interaction,
        )

    # Reused by the loopback direct-attach listener (see
    # ``omnigent.runner.direct_attach``): same attach handler served on a
    # token-gated 127.0.0.1 port so a same-machine browser can skip the
    # server relay.
    app.state.terminal_attach_handler = terminal_resource_attach_ws

    async def _require_os_env(session_id: str) -> AgentSpec | None:
        spec = await _resolve_session_agent_spec(session_id)
        if spec is not None and getattr(spec, "os_env", None) is None:
            raise HTTPException(
                status_code=404,
                detail="Session agent has no os_env configured; filesystem API unavailable.",
            )
        return spec

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem")
    async def list_environment_root(
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> JSONResponse:
        await _require_os_env(session_id)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            "",
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/search")
    async def search_environment_files(
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        return await _fs_search(
            session_id,
            environment_id,
            "",
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments/{environment_id}/search/{path:path}"
    )
    async def search_environment_files_under(
        session_id: str,
        environment_id: str,
        path: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> JSONResponse:
        """Search under a directory, so results match what the tree shows."""
        return await _fs_search(
            session_id,
            environment_id,
            path,
            q=q,
            include=include,
            exclude=exclude,
            limit=limit,
        )

    async def _fs_search(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        q: str,
        include: str | None,
        exclude: str | None,
        limit: int,
    ) -> JSONResponse:
        import asyncio as _asyncio

        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
            _validate_path,
            index_search,
            merge_entries,
            split_glob_list,
        )

        include_patterns = split_glob_list(include)
        exclude_patterns = split_glob_list(exclude)

        agent_spec = await _require_os_env(session_id)  # also resolves spec
        await _ensure_session_registered(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fs = CallerProcessFilesystem(env)

        # The budgeted walk is the only source for ignored files (and, until a
        # ``git status`` has run, untracked ones), so it always runs. Alongside
        # it, git's index adds every tracked file in one read however large the
        # repo, and the Changed tab's latest ``git status`` adds untracked files
        # past the budget. Absolute (browse-anywhere) paths have no registry.
        walk = _asyncio.ensure_future(
            fs.search_files(
                q,
                path=path,
                include=include_patterns,
                exclude=exclude_patterns,
                limit=limit,
            )
        )
        indexed: list[FilesystemEntry] | None = None
        try:
            registry = None
            if not fs._absolute(path):
                env_root = fs._resolve("")
                registry = await _resolve_session_fs_registry(session_id)
                if (
                    registry is None
                    or registry.cwd != env_root
                    or registry.git_root != detect_git_root(env_root)
                ):
                    # The session registry watches a different tree than this
                    # walk covers, or a repository boundary moved since it was
                    # built; either way its index would answer for the wrong
                    # files. Consult one rooted where the search actually runs.
                    registry = _search_registry_for_root(env_root)
            if registry is not None:
                indexed = await _asyncio.to_thread(
                    index_search,
                    registry,
                    fs._resolve(path),
                    _validate_path(path) if path else "",
                    q,
                    include=include_patterns,
                    exclude=exclude_patterns,
                    limit=limit,
                )
        except BaseException:
            walk.cancel()
            raise
        entries, truncated = await walk
        if indexed is not None:
            entries = merge_entries(indexed, entries, limit)
        data = [_fs_entry_to_dict(e) for e in entries]
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "base": str(fs._resolve(path)),
                "data": data,
                "has_more": len(entries) >= limit,
                # The scan budget ran out before the tree did, so "no matches"
                # here would be a lie — the caller must be able to say so.
                "truncated": truncated,
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/environments/{environment_id}/changes")
    async def list_filesystem_changes(
        session_id: str,
        environment_id: str,  # noqa: ARG001
    ) -> JSONResponse:
        import asyncio as _asyncio

        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)
        try:
            # ``list_changed_files`` shells out to ``git status`` synchronously,
            # which on a large repo (cold untracked cache) can take seconds.
            # Offload to a thread so it never blocks the event loop — a blocked
            # loop can't answer the server's runner-stream relay probe and the
            # session's first turn 503s with runner_unavailable.
            raw_changes = (
                await _asyncio.to_thread(
                    session_registry.list_changed_files,
                    session_id,
                    limit=10_000,
                )
                if session_registry is not None
                else []
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        data = [
            {
                "object": "session.environment.filesystem.entry",
                "path": rec["path"],
                "name": rec["path"].split("/")[-1],
                "status": rec["status"],
                "bytes": rec.get("bytes"),
                "modified_at": rec.get("modified_at"),
                "lines_added": rec.get("lines_added"),
                "lines_removed": rec.get("lines_removed"),
            }
            for rec in raw_changes
        ]
        return JSONResponse(
            status_code=200,
            content={"object": "list", "data": data, "has_more": False},
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/diff/{relative_path:path}"
    )
    async def read_environment_file_diff(
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> JSONResponse:
        agent_spec = await _require_os_env(session_id)
        await _ensure_session_registered(session_id)
        session_registry = await _resolve_session_fs_registry(session_id)

        from omnigent.entities.environment_filesystem import InvalidPath
        from omnigent.runner.environment_filesystem import _validate_path

        try:
            relative_path = _validate_path(relative_path)
        except InvalidPath as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": str(exc),
                    }
                },
            )
        if not relative_path:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_path",
                        "message": "Cannot diff the environment root",
                    }
                },
            )

        import asyncio as _asyncio

        from omnigent.runtime.filesystem_registry import GitStatusUnavailable

        try:
            # Offloaded like list_filesystem_changes: get_changed_file shells out
            # to git (status + show) synchronously, so keep it off the loop.
            record = (
                await _asyncio.to_thread(
                    session_registry.get_changed_file, session_id, relative_path
                )
                if session_registry is not None
                else None
            )
        except GitStatusUnavailable as exc:
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "git_status_failed", "message": exc.reason}},
            )
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (
                            f"Path {relative_path!r} is not in the "
                            "changed-files registry for this session"
                        ),
                    }
                },
            )
        is_deleted = record.get("status") == "deleted"

        before: str | None = (
            await _asyncio.to_thread(session_registry.get_baseline, relative_path)
            if session_registry is not None
            else None
        )

        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        after: str | None = None
        if not is_deleted:
            env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
            fs = CallerProcessFilesystem(env)
            content = await fs.read(relative_path, limit=None)
            after = content.data.decode(content.encoding or "utf-8", errors="replace")

        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.file_diff",
                "path": relative_path,
                "before": before,
                "after": after,
            },
        )

    # ── GitHub integration (read-only): PR metadata + the PR's files / diff ──
    # The list and patch come from the ``gh`` CLI (the PR's "Files changed");
    # only the per-file expand-context reader uses ``git show``. See
    # omnigent.runner.github_resource. Each shells out synchronously, so it is
    # offloaded to a thread like the changed-files / diff routes above (a blocked
    # loop 503s the session).

    async def _github_workspace_root(session_id: str) -> str:
        """Resolve the workspace root for GitHub routes, or 404 when headless."""
        agent_spec = await _require_os_env(session_id)
        root = resource_registry.compute_default_env_root(session_id, agent_spec)
        if root is None:
            raise HTTPException(
                status_code=404,
                detail="Session has no filesystem; GitHub API unavailable.",
            )
        return root

    async def _github_call(session_id: str, operation: str, **kwargs: Any) -> JSONResponse:
        from omnigent.runner import github_resource

        root = await _github_workspace_root(session_id)
        function = getattr(github_resource, operation)
        try:
            result = await asyncio.to_thread(function, root, session_id=session_id, **kwargs)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/sessions/{session_id}/resources/github")
    async def read_github_info(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_info", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/changes")
    async def read_github_changes(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_changed_files", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/diff")
    async def read_github_pr_diff(session_id: str, pr_url: str | None = None) -> JSONResponse:
        return await _github_call(session_id, "github_pr_diff", pr_url=pr_url)

    @app.get("/v1/sessions/{session_id}/resources/github/diff/{relative_path:path}")
    async def read_github_file_diff(
        session_id: str,
        relative_path: str,
        base: str | None = None,
        pr_url: str | None = None,
        previous_path: str | None = None,
        head_sha: str | None = None,
        base_sha: str | None = None,
    ) -> JSONResponse:
        if relative_path.startswith("/") or any(
            seg in ("", "..") for seg in relative_path.split("/")
        ):
            raise HTTPException(status_code=400, detail="Invalid path")
        return await _github_call(
            session_id,
            "github_file_diff",
            base=base or "",
            path=relative_path,
            pr_url=pr_url,
            previous_path=previous_path,
            head_sha=head_sha,
            base_sha=base_sha,
        )

    @app.post("/v1/sessions/{session_id}/resources/github/prs")
    async def update_github_pr_route(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise HTTPException(status_code=400, detail="Expected a pull request URL")
        return await _github_call(
            session_id, "update_session_pr", url=body["url"], action=body.get("action", "attach")
        )

    @app.post("/v1/sessions/{session_id}/resources/github/preferences")
    async def set_github_preference_route(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        return await _github_call(
            session_id,
            "set_github_preference",
            account=body.get("account"),
            remote=body.get("remote"),
            pr_url=body.get("pr_url"),
        )

    @app.get(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def read_or_list_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        download: bool = False,
    ) -> Response:
        await _require_os_env(session_id)
        if download:
            return await _fs_download(session_id, environment_id, relative_path)
        return await _fs_list_or_read(
            session_id,
            environment_id,
            relative_path,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )

    _exclusive_create_lock = asyncio.Lock()

    @app.put(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        body = await request.json()
        content_str = body.get("content", "")
        encoding = body.get("encoding", "utf-8")
        create_parents = body.get("create_parents", True)
        if encoding == "base64":
            # Binary content (an upload) travels as base64 inside the JSON body.
            try:
                content_bytes = base64.b64decode(content_str, validate=True)
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=400, content={"error": {"message": "content is not valid base64"}}
                )
        else:
            content_bytes = content_str.encode(encoding)
        # ``if_exists: "fail"`` is an exclusive create (two uploads that picked the same free name
        # cannot overwrite each other): the check and the write happen under one lock.
        exclusive = body.get("if_exists") == "fail"
        async with _exclusive_create_lock if exclusive else contextlib.AsyncExitStack():
            existed = False
            try:
                existing = await fs.read(relative_path, limit=None)
                existed = True
                if existing.encoding and filesystem_registry is not None:
                    filesystem_registry.seed_snapshot(
                        relative_path,
                        existing.data.decode(existing.encoding, errors="replace"),
                        session_id=session_id,
                    )
            except Exception:  # noqa: BLE001
                pass
            if exclusive and existed:
                return JSONResponse(
                    status_code=409,
                    content={"error": {"message": "A file with that name already exists"}},
                )
            result = await fs.write(
                relative_path,
                content_bytes,
                create_parents=create_parents,
            )
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        _notify_file_written(relative_path, env)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.write_result",
                "operation": result.operation,
                "path": result.path,
                "created": result.created,
                "bytes_written": result.bytes_written,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.patch(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.entities.environment_filesystem import (
            TextEditRequest,
        )
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        try:
            existing = await fs.read(relative_path, limit=None)
            if existing.encoding and filesystem_registry is not None:
                filesystem_registry.seed_snapshot(
                    relative_path,
                    existing.data.decode(existing.encoding, errors="replace"),
                    session_id=session_id,
                )
        except Exception:  # noqa: BLE001
            pass
        body = await request.json()
        edit_req = TextEditRequest(
            old_text=body.get("old_text"),
            new_text=body.get("new_text"),
            replace_all=body.get("replace_all", False),
        )
        result = await fs.edit_text(relative_path, edit_req)
        if filesystem_registry is not None:
            filesystem_registry.record_change(relative_path, result.operation, session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.edit_result",
                "operation": result.operation,
                "path": result.path,
                "replacements": result.replacements,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "entry": _fs_entry_to_dict(result.entry) if result.entry else None,
            },
        )

    @app.delete(
        "/v1/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}"
    )
    async def delete_environment_path(
        session_id: str,
        environment_id: str,
        relative_path: str,
        recursive: bool = Query(default=False),
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        fs = CallerProcessFilesystem(env)
        result = await fs.delete(relative_path, recursive=recursive)
        if filesystem_registry is not None and result.type == "file":
            filesystem_registry.record_change(relative_path, "deleted", session_id)
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.filesystem.delete_result",
                "operation": result.operation,
                "path": result.path,
                "deleted": result.deleted,
                "type": result.type,
                "bytes_deleted": result.bytes_deleted,
                "entries_deleted": result.entries_deleted,
            },
        )

    async def _ensure_session_registered(session_id: str) -> None:
        if session_id in _session_start_cache:
            return
        snapshot = await _session_snapshot(session_id)
        _session_start_cache[session_id] = snapshot.created_at
        # Only memoize a workspace the server actually returned; a failed
        # fetch is re-resolved lazily by _session_workspace_value.
        if snapshot.ok:
            _session_workspace_cache[session_id] = snapshot.workspace

    async def _resolve_session_spec_entry(session_id: str) -> _SpecEntry | None:
        if session_id in _session_spec_cache:
            return _session_spec_cache[session_id]
        if spec_resolver is None:
            _session_spec_cache[session_id] = None
            return None
        lock = _session_spec_locks.setdefault(session_id, asyncio.Lock())
        generation = _session_cache_generation(session_id)
        async with lock:
            if session_id in _session_spec_cache:
                return _session_spec_cache[session_id]
            snapshot = await _session_snapshot(session_id)
            if not snapshot.ok:
                raise OmnigentError(
                    f"session spec resolver: GET /v1/sessions/{session_id} "
                    f"failed with HTTP {snapshot.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            agent_id = snapshot.agent_id
            if not agent_id:
                raise OmnigentError(
                    f"session spec resolver: session {session_id!r} has no agent_id",
                    code=ErrorCode.NOT_FOUND,
                )
            spec_entry = await spec_resolver(agent_id, session_id)
            if spec_entry is None:
                # The session still references agent_id, but its stored bundle
                # no longer resolves (deleted or rebound out from under the
                # live session). A session-lifecycle condition, not a generic
                # NOT_FOUND: the distinct code lets the terminal-ensure and
                # turn-dispatch paths surface a lifecycle reason instead of a
                # runner startup fault.
                raise OmnigentError(
                    f"session spec resolver: agent {agent_id!r} for "
                    f"session {session_id!r} was not found",
                    code=ErrorCode.SESSION_AGENT_MISSING,
                )
            sub_agent_name = snapshot.sub_agent_name
            # Root the child at its own bundle dir. Always wrapped, so an
            # unresolvable workdir registers nothing rather than falling back
            # to the parent's bundle root.
            if sub_agent_name:
                _session_sub_agent_names[session_id] = sub_agent_name
                if _unwrap_resolved_spec(spec_entry) is not None:
                    sub_entry = _resolve_sub_agent_spec_entry(spec_entry, sub_agent_name)
                    if sub_entry is None:
                        _warn_unresolved_sub_agent(session_id, sub_agent_name)
                        _session_sub_agent_resolved[session_id] = False
                    else:
                        spec_entry = sub_entry
                        _session_sub_agent_resolved[session_id] = True
            if _session_cache_generation_is_current(session_id, generation):
                _session_spec_cache[session_id] = spec_entry
            return spec_entry

    async def _resolve_session_agent_spec(session_id: str) -> AgentSpec | None:
        entry = await _resolve_session_spec_entry(session_id)
        return _unwrap_spec_entry(entry)

    async def _resolve_session_agent_spec_or_none(session_id: str) -> AgentSpec | None:
        """Resolve the session agent spec, tolerating resolution failure.

        Callers that can continue without a spec swallow ``OmnigentError``
        through this resolver.
        """
        try:
            return await _resolve_session_agent_spec(session_id)
        except OmnigentError:
            return None

    async def _resolve_session_skills(session_id: str) -> list[SkillSpec]:
        cached = _session_skills_cache.get(session_id)
        if cached is not None:
            expires_at, cached_skills = cached
            if time.monotonic() < expires_at:
                return cached_skills
        entry = await _resolve_session_spec_entry(session_id)
        spec = _unwrap_resolved_spec(entry) if entry is not None else None
        if spec is None:
            return []
        workspace = await _session_workspace_value(session_id)
        candidate_roots = [
            Path(workspace).resolve()
            if workspace is not None
            else (runner_workspace.resolve() if runner_workspace is not None else None),
            _resolved_spec_workdir(entry),
        ]
        roots: list[Path] = []
        for candidate in candidate_roots:
            if candidate is None:
                continue
            resolved = candidate.resolve()
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            roots.append(Path.cwd())

        skills = await asyncio.to_thread(
            resolve_session_skills, spec, tuple(roots), _resolved_spec_workdir(entry)
        )
        _session_skills_cache[session_id] = (
            time.monotonic() + _SESSION_SKILLS_CACHE_TTL_SECONDS,
            skills,
        )
        return skills

    @app.get("/v1/sessions/{session_id}/models")
    async def get_session_models(session_id: str) -> JSONResponse:
        spec = await _resolve_session_agent_spec(session_id)
        if spec is None:
            return JSONResponse(status_code=200, content={"workers": {}})
        from omnigent.models.model_catalog import catalog_for_spec

        try:
            catalog = await asyncio.to_thread(catalog_for_spec, spec)
        except Exception:
            _logger.exception(
                "get_session_models: catalog_for_spec failed for session=%s",
                session_id,
                extra={"session_id": session_id},
            )
            return JSONResponse(status_code=200, content={"workers": {}})
        return JSONResponse(status_code=200, content={"workers": catalog})

    def _model_configuration_source(session_id: str) -> dict[str, str] | None:
        """Return the session's non-secret model-provider coordinates."""
        from omnigent.models.model_catalog import (
            model_configuration_source,
            resolve_model_provider,
        )

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        harness = _session_harness_name(session_id)
        provider = resolve_model_provider(spec, harness)
        return model_configuration_source(provider, harness=harness)

    def _with_model_configuration_source(
        session_id: str, rows: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        source = _model_configuration_source(session_id)
        if source is None:
            return [dict(row) for row in rows]
        return [{**row, "source": source} for row in rows]

    @app.get("/v1/sessions/{session_id}/model-options")
    async def get_session_model_options(session_id: str) -> JSONResponse:
        """Session model listing.

        The claude-sdk and pi harnesses publish no runner-side catalog, so
        the listing is empty and the server falls back to its own catalog.
        """
        del session_id
        return JSONResponse(status_code=200, content={"models": []})

    @app.post("/v1/sessions/{session_id}/skills/resolve")
    async def resolve_session_skill(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "Request body must be JSON."},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Request body must be a JSON object.",
                },
            )
        name = body.get("name")
        arguments = body.get("arguments", "")
        if not isinstance(name, str) or not name:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'name' is required."},
            )
        if not isinstance(arguments, str):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'arguments' must be a string."},
            )
        skills = await _resolve_session_skills(session_id)
        skill = find_skill_by_name(skills, name)
        if skill is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "skill_not_found",
                    "detail": (f"Skill {name!r} not found for session {session_id!r}."),
                    "available": sorted(s.name for s in skills),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"meta_text": format_skill_meta_text(skill, arguments)},
        )

    async def _fs_download(
        session_id: str,
        environment_id: str,
        path: str,
    ) -> StreamingResponse:
        """Serve a file's complete bytes as an attachment.

        The read path inlines content in a JSON envelope, so it caps at
        ``_MAX_READ_BYTES``. A download streams from a descriptor and needs
        no cap; ``open_download`` binds that descriptor to what the sandbox
        can read before a byte is served.

        :param session_id: Session identifier.
        :param environment_id: Environment resource id.
        :param path: Path within the environment, or an absolute path.
        :returns: The file streamed with ``Content-Disposition: attachment``.
        :raises InvalidPath: If the path names a directory.
        :raises FilesystemPathNotFound: If nothing the caller may see exists
            at the path.
        """
        from omnigent.runner.environment_filesystem import CallerProcessFilesystem

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
        env = resource_registry.resolve_environment(session_id, environment_id, agent_spec)
        fobj, resolved, size = await CallerProcessFilesystem(env).open_download(path)

        async def _chunks() -> AsyncIterator[bytes]:
            # Stop at the size announced in Content-Length so a file growing
            # underneath the download cannot overrun the response.
            remaining = size
            try:
                while remaining > 0:
                    chunk = await asyncio.to_thread(fobj.read, min(64 * 1024, remaining))
                    if not chunk:
                        # Ending short of Content-Length would hand the client
                        # a silently incomplete file; abort the transfer instead.
                        raise RuntimeError(f"{resolved.name} shrank during download")
                    remaining -= len(chunk)
                    yield chunk
            finally:
                fobj.close()

        # Same header Starlette's FileResponse builds: a plain quoted filename
        # when it is URL-safe, else the RFC 5987 encoded form.
        quoted = urllib.parse.quote(resolved.name)
        disposition = (
            f'attachment; filename="{resolved.name}"'
            if quoted == resolved.name
            else f"attachment; filename*=utf-8''{quoted}"
        )
        return StreamingResponse(
            _chunks(),
            media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": disposition,
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _fs_list_or_read(
        session_id: str,
        environment_id: str,
        path: str,
        *,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            CallerProcessFilesystem,
        )

        await _ensure_session_registered(session_id)
        agent_spec = await _resolve_session_agent_spec(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )

        fs = CallerProcessFilesystem(env)
        resolved = fs._resolve(path)

        if resolved.is_dir():
            page = await fs.list_dir(
                path,
                limit=limit,
                after=after,
                before=before,
                order=order,
            )
            data = [_fs_entry_to_dict(e) for e in page.data]
            return JSONResponse(
                status_code=200,
                content={
                    "object": "list",
                    # Absolute base the entry paths are relative to. Callers
                    # that only ever browse the workspace can keep ignoring it.
                    "base": str(resolved),
                    "data": data,
                    "first_id": page.first_id,
                    "last_id": page.last_id,
                    "has_more": page.has_more,
                },
            )

        content = await fs.read(path)
        content_type_guess, _ = mimetypes.guess_type(path)
        payload: dict[str, object] = {
            "object": "session.environment.filesystem.file_content",
            "path": content.path,
            "content_type": content_type_guess,
            "bytes": content.bytes,
            "truncated": content.truncated,
        }
        if content.encoding:
            payload["encoding"] = content.encoding
            payload["content"] = content.data.decode(content.encoding)
        else:
            import base64

            payload["encoding"] = "base64"
            payload["content"] = base64.b64encode(content.data).decode()
        return JSONResponse(status_code=200, content=payload)

    def _fs_entry_to_dict(entry: FilesystemEntry) -> dict[str, object]:
        from omnigent.runner.environment_filesystem import entry_payload

        return entry_payload(entry)

    @app.post("/v1/sessions/{session_id}/resources/environments/{environment_id}/shell")
    async def run_environment_shell(
        session_id: str,
        environment_id: str,
        request: Request,
    ) -> JSONResponse:
        from omnigent.runner.environment_filesystem import (
            _run_os_env_async,
        )

        agent_spec = await _require_os_env(session_id)
        env = resource_registry.resolve_environment(
            session_id,
            environment_id,
            agent_spec,
        )
        body = await request.json()
        command = body.get("command")
        if not command or not isinstance(command, str):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'command' is required",
                    }
                },
            )
        timeout = body.get("timeout")
        if timeout is not None and not isinstance(timeout, int):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'timeout' must be an integer",
                    }
                },
            )
        result = await _run_os_env_async(
            env.shell,
            command,
            timeout,
        )
        return JSONResponse(
            status_code=200,
            content={
                "object": "session.environment.shell_result",
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "cwd": result.get("cwd"),
            },
        )

    @app.get("/v1/sessions/{session_id}/resources/{resource_id}")
    async def get_session_resource(
        session_id: str,
        resource_id: str,
    ) -> JSONResponse:
        resource = resource_registry.get_resource(
            session_id,
            resource_id,
        )
        if resource is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": (f"Resource {resource_id!r} not found"),
                    }
                },
            )
        return JSONResponse(
            status_code=200,
            content=session_resource_view_to_dict(resource),
        )

    def _clear_session_agent_caches(session_id: str, agent_id: str | None = None) -> None:
        _session_spec_cache.pop(session_id, None)
        _session_agent_ids.pop(session_id, None)
        _session_harness_overrides.pop(session_id, None)
        # Bump so any in-flight fill discards its write rather than reinstating it.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1
        _session_snapshot_cache.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_tool_schemas.pop(session_id, None)
        _session_mcp_spec_hash.pop(session_id, None)
        _instruction_delivery_warned.pop(session_id, None)
        _session_sub_agent_resolved.pop(session_id, None)
        _forget_superside_chat_prefix(session_id)
        if agent_id:
            _spec_cache.pop(agent_id, None)

    def _forget_session_workspace(session_id: str) -> None:
        """Drop the cached working directory (and what was derived from it).

        The server's session row is the one source; the next read re-fetches it. A tool call
        already running keeps the directory it started in.
        """
        # Bump first so an in-flight fetch discards its stale write.
        _session_cache_generations[session_id] = _session_cache_generations.get(session_id, 0) + 1
        _session_workspace_cache.pop(session_id, None)
        _session_snapshot_cache.pop(session_id, None)
        _session_init_envelopes.pop(session_id, None)
        _session_skills_cache.pop(session_id, None)
        _session_fs_registries.pop(session_id, None)

    async def _invalidate_session_agent_state(session_id: str, new_agent_id: str | None) -> None:
        """Clear all agent-derived caches and release the harness subprocess.

        Both dispatch paths (background ``_run_turn_bg_setup_and_stream`` and
        direct-stream ``_stream_message_to_harness``) call this shared routine
        on an in-conversation agent switch, so the eviction scope cannot
        diverge between the two paths.
        """
        _clear_session_agent_caches(session_id, new_agent_id)
        if process_manager is not None:
            await process_manager.release(session_id)

    @app.delete("/v1/sessions/{session_id}/resources")
    async def cleanup_session_resources(
        session_id: str,
    ) -> JSONResponse:
        if process_manager is not None:
            await process_manager.release(session_id)
        await resource_registry.cleanup_session(session_id)
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.resources.cleaned",
                "cleaned": True,
            },
        )

    @app.post("/v1/sessions/{session_id}/reset-state")
    async def reset_session_state(session_id: str) -> JSONResponse:
        await _teardown_session_terminals(session_id)
        if process_manager is not None:
            await process_manager.release(session_id)
        await resource_registry.cleanup_session(session_id)
        _clear_session_agent_caches(session_id, _session_agent_ids.get(session_id))
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "object": "session.state_reset",
                "reset": True,
            },
        )

    @app.post("/v1/sessions/{session_id}/studio/keep-warm")
    async def keep_studio_warm(session_id: str) -> JSONResponse:
        """A file's studio is open (ADR 0024): keep its harness warm for ten more minutes."""
        if not await _is_studio(session_id):
            return JSONResponse(status_code=404, content={"warm": False})
        _studio_runners.touch(session_id)
        _ensure_studio_sweeper()
        warm = process_manager is not None and process_manager.has_session(session_id)
        if warm and process_manager is not None:
            process_manager.note_activity(session_id)
        return JSONResponse(status_code=200, content={"warm": warm})

    @app.post("/v1/sessions/{session_id}/agent-cache/reset")
    async def reset_session_agent_cache(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        agent_id = body.get("agent_id") if isinstance(body, dict) else None
        if not isinstance(agent_id, str) or not agent_id:
            agent_id = _session_agent_ids.get(session_id)
        if not agent_id:
            with contextlib.suppress(OmnigentError, httpx.HTTPError, RuntimeError):
                snapshot = await _session_snapshot(session_id)
                if snapshot.ok and snapshot.agent_id:
                    agent_id = snapshot.agent_id

        _clear_session_agent_caches(session_id, agent_id)
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "agent_id": agent_id,
                "object": "session.agent_cache_reset",
                "reset": True,
            },
        )

    @app.post("/v1/sessions/{session_id}/mcp/execute")
    async def mcp_execute(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": -32700, "message": "Parse error: invalid JSON"}},
            )
        method: str = body.get("method") or ""
        params: _JsonObject = body.get("params") or {}

        from omnigent.superchat.feature import RELAY_MARK

        scope = getattr(request, "scope", None)  # absent on the retained-operation re-entry below
        if (
            RELAY_MARK in body
            and scope is not None
            and (scope.get("client") or (None,))[0] != "tunnel"
        ):
            # The relay mark is the server's: only a call that came through the server tunnel may
            # carry it. Anything else (a direct connection) has it stripped.
            body = {k: v for k, v in body.items() if k != RELAY_MARK}

        raw_operation = body.get("_omnigent_operation")
        if method == "tools/call" and raw_operation is not None:
            operation = raw_operation if isinstance(raw_operation, dict) else {}
            operation_id = operation.get("id")
            operation_step = operation.get("step")
            if not (
                isinstance(operation_id, str)
                and 1 <= len(operation_id) <= 128
                and isinstance(operation_step, str)
                and 1 <= len(operation_step) <= 64
            ):
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Invalid runner MCP operation metadata",
                        }
                    },
                )

            nested_body = cast("_JsonObject", {**body, "_omnigent_operation": None})
            nested_body.pop("_omnigent_operation")

            async def _run_retained_mcp_execution() -> McpExecutionResult:
                nested_response = await mcp_execute(
                    session_id,
                    cast("Request", _BodyRequest(nested_body)),
                )
                nested_content = json.loads(bytes(nested_response.body))
                if not isinstance(nested_content, dict):
                    raise RuntimeError("Runner MCP execution returned a non-object response")
                return McpExecutionResult(
                    status_code=nested_response.status_code,
                    content=cast("_JsonObject", nested_content),
                )

            try:
                retained = await mcp_execution_registry.execute(
                    session_id=session_id,
                    operation_id=operation_id,
                    step=operation_step,
                    params=cast("_JsonObject", {"method": method, "params": params}),
                    run=_run_retained_mcp_execution,
                )
            except McpExecutionConflict as exc:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": str(exc)}},
                )
            return JSONResponse(status_code=retained.status_code, content=retained.content)

        if method == "tools/list":
            if mcp_manager is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Runner MCP manager not configured",
                        }
                    },
                )
            spec_entry = _session_spec_cache.get(session_id)
            spec = _unwrap_resolved_spec(spec_entry)
            if spec is None and spec_resolver is not None:
                spec = await _resolve_session_agent_spec_or_none(session_id)
            if spec is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": f"No spec available for session {session_id!r}",
                        }
                    },
                )
            try:
                result = await mcp_manager.schemas_for(spec)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": _client_safe_error_detail(exc, context="MCP tool dispatch"),
                        }
                    },
                )
            return JSONResponse(
                content={
                    "result": {
                        "schemas": result.schemas,
                        "tool_names": list(result.tool_names),
                        "failures": result.failures,
                    }
                }
            )

        if method == "tools/call":
            import json as _json

            from omnigent.runner.tool_dispatch import execute_tool

            # A paused Helper's call waits here, at its tool boundary (ADR 0027).
            if not await mcp_execution_registry.hold(session_id):
                return JSONResponse(status_code=200, content=STOPPED_RESULT)

            tool_name = cast(str, params.get("name") or "")
            arguments = cast(_JsonObject, params.get("arguments") or {})
            input_responses = cast(_JsonObject | None, params.get("inputResponses"))
            request_state = cast(str | None, params.get("requestState"))
            if not tool_name:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": "Missing tool name"}},
                )

            if tool_name == "sys_read_inbox":
                # A scan that failed at initialization must not leave the
                # drain reporting an empty inbox; this is a no-op once done.
                await _recover_undrained_subagent_results(session_id)

            if "__" in tool_name:
                if mcp_manager is None:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "code": -32000,
                                "message": "Runner MCP manager not configured",
                            }
                        },
                    )
                spec_entry = _session_spec_cache.get(session_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    spec = await _resolve_session_agent_spec_or_none(session_id)
                if spec is None:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": f"No spec available for session {session_id!r}",
                            }
                        },
                    )
                from omnigent.tools.mcp import McpElicitationRequired

                try:
                    if input_responses is not None:
                        route = mcp_manager._resolve_tool_route(spec, tool_name)
                        if route is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        owning, bare_tool = route
                        if owning.connection is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        output = await owning.connection.call_tool_with_elicitation(
                            bare_tool,
                            arguments,
                            input_responses=input_responses,
                            request_state=request_state,
                        )
                    else:
                        output = await mcp_manager.call_tool(
                            spec,
                            tool_name,
                            arguments,
                            session_id=session_id,
                        )
                except McpElicitationRequired as elicit:
                    return JSONResponse(
                        content={
                            "result": {
                                "input_required": {
                                    "inputRequests": elicit.input_requests,
                                    "requestState": elicit.request_state,
                                },
                            },
                        },
                    )
                except Exception as exc:
                    _logger.exception(
                        "MCP tool dispatch failed for %s",
                        tool_name,
                        extra={"session_id": session_id},
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            else:
                spec_entry = _session_spec_cache.get(session_id)
                spec_workdir = _resolved_workdir_for_spec(spec_entry, runner_workspace)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    try:
                        resolved_entry = await _resolve_session_spec_entry(session_id)
                        spec_workdir = _resolved_workdir_for_spec(resolved_entry, runner_workspace)
                        spec = _unwrap_resolved_spec(resolved_entry)
                    except (OmnigentError, httpx.HTTPError, RuntimeError):
                        pass
                _agent_id_local = _session_agent_ids.get(session_id)
                try:
                    dispatch_workspace = await _session_runtime_cwd(session_id)
                    # Almost always a cache hit: the relay that makes this
                    # call possible at all already warmed it at start (see
                    # _ensure_comment_relay_started). The fallback fetch only
                    # covers a tool call that somehow beats that warm-up.
                    dispatch_labels = await _tool_labels_for_session(session_id)
                    output = await execute_tool(
                        tool_name=tool_name,
                        arguments=_json.dumps(arguments),
                        server_client=server_client,
                        terminal_registry=terminal_registry,
                        resource_registry=resource_registry,
                        agent_spec=spec,
                        conversation_id=session_id,
                        task_id=session_id,
                        agent_id=_agent_id_local,
                        agent_name=getattr(spec, "name", None),
                        runner_workspace=dispatch_workspace,
                        local_tool_workdir=spec_workdir,
                        mcp_manager=None,
                        labels=dispatch_labels,
                        session_inbox=_session_inboxes.get(session_id),
                        session_async_tasks=_session_async_tasks.get(session_id),
                        harness_client=None,
                        publish_event=_publish_event,
                        filesystem_registry=filesystem_registry,
                        effective_harness=_session_harness_name(session_id),
                        relay=body.get(RELAY_MARK) is True,
                        turn_said=(
                            None
                            if (live := _live_response_id.get(session_id)) is None
                            else _turn_said.get(session_id) == live
                        ),
                    )
                except Exception as exc:
                    _logger.exception(
                        "MCP tool dispatch failed for %s",
                        tool_name,
                        extra={"session_id": session_id},
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            return JSONResponse(content={"result": {"output": output}})

        return JSONResponse(
            status_code=200,
            content={"error": {"code": -32601, "message": f"Method not found: {method!r}"}},
        )

    def _summarize_model_id(model: str) -> str:
        """Provider-prefix a bare Claude id; the LLM client routes bare ids to OpenAI."""
        return f"anthropic/{model}" if model.startswith("claude") else model

    def _bound_summarize_route(session_id: str) -> tuple[str, dict[str, str]] | None:
        """The session's own provider, from its saved inference binding: the wire and
        connection its harness uses (the engine's model proxy, for a model connection).

        ``None`` when the session's harness has no binding (or it cannot be resolved here).
        """
        harness = _session_harness_name(session_id)
        if not harness:
            return None
        from omnigent.inference_config import (
            load_runtime_inference_config,
            resolve_bound_provider,
        )
        from omnigent.llms.background import provider_connection

        try:
            provider = resolve_bound_provider(
                load_runtime_inference_config(), harness, allow_empty=True
            )
            return provider_connection(provider) if provider is not None else None
        except Exception:
            _logger.warning(
                "summarizer: could not resolve %s's bound provider",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return None

    def _summarize_route(session_id: str, model: str) -> tuple[str | None, dict[str, str] | None]:
        """``(wire, connection)`` the summarizer calls *model* with for *session_id*: the
        session's bound provider when it has one, else the legacy per-spec resolution (whose
        wire is told from the model id)."""
        bound = _bound_summarize_route(session_id)
        if bound is not None:
            return bound
        return None, _resolve_summarize_connection(session_id, _summarize_model_id(model))

    def _resolve_summarize_connection(
        session_id: str,
        model: str,
    ) -> dict[str, str] | None:
        from omnigent.spec.types import ApiKeyAuth, ProviderAuth

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return _resolve_local_provider_connection(model)
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        if spec is None:
            return _resolve_local_provider_connection(model)

        auth = getattr(spec.executor, "auth", None)

        if isinstance(auth, ProviderAuth):
            return _resolve_provider_connection(auth.name, model)

        if isinstance(auth, ApiKeyAuth):
            conn: dict[str, str] = {"api_key": auth.api_key}
            if auth.base_url:
                conn["base_url"] = auth.base_url
            return conn

        if auth is None:
            from omnigent.runtime.workflow import _load_global_auth

            global_auth = _load_global_auth()
            if isinstance(global_auth, ApiKeyAuth):
                conn = {"api_key": global_auth.api_key}
                if global_auth.base_url:
                    conn["base_url"] = global_auth.base_url
                return conn

        # No spec-level auth or global auth resolved: fall
        # back to the runner's own local provider config (the ``providers:``
        # block in ~/.omnigent/config.yaml — the credentials the native CLIs
        # themselves launch with). Without this, a plain local/self-hosted
        # deployment has no way to authenticate this call at all: the LLM
        # adapter sends no Authorization header when connection_params is
        # None (it never reads provider env vars itself), so callers like
        # rollover's summarizer always 401 on such a deployment.
        return _resolve_local_provider_connection(model)

    def _resolve_local_provider_connection(model: str) -> dict[str, str] | None:
        """Resolve a connection from ~/.omnigent/config.yaml's providers: block.

        Picks the family's ``default: true`` provider, falling back to the
        first provider configured for that family — the same precedence
        :func:`_resolve_provider_connection` uses once a provider name is
        known, but starting from the model alone.

        :param model: LLM model string, e.g. ``"gpt-4o"`` or ``"claude-haiku-4-5"``.
        :returns: A ``{"base_url", "api_key"}`` connection dict, or ``None``
            when no local provider serves this model's family.
        """
        family = resolve_model_provider_family(model)
        if family is None:
            _logger.warning(
                "/v1/summarize: model %r is not a recognized Anthropic or OpenAI id; "
                "refusing to guess a local provider for it",
                model,
                extra={"session_id": runner_primary_session_id()},
            )
            return None
        try:
            from omnigent.onboarding.detected import effective_config_with_detected
            from omnigent.onboarding.provider_config import (
                first_available_provider,
                get_default_provider,
                load_config,
            )

            config = effective_config_with_detected(load_config())
            entry = get_default_provider(config, family) or first_available_provider(
                config, family
            )
            if entry is None:
                return None
            fam = entry.family(family)
            if fam is None:
                return None
            conn: dict[str, str] = {}
            if fam.api_key:
                conn["api_key"] = fam.api_key
            if fam.base_url:
                conn["base_url"] = (
                    normalize_anthropic_base_url(fam.base_url)
                    if family == "anthropic"
                    else fam.base_url
                )
            return conn or None
        except Exception:
            _logger.warning(
                "/v1/summarize: failed to resolve a local provider for model %r",
                model,
                exc_info=True,
                extra={"session_id": runner_primary_session_id()},
            )
            return None

    def _resolve_provider_connection(
        provider_name: str,
        model: str = "",
    ) -> dict[str, str] | None:
        try:
            from omnigent.onboarding.detected import effective_config_with_detected
            from omnigent.onboarding.provider_config import (
                load_config,
                load_providers,
            )

            config = load_config()
            providers = load_providers(effective_config_with_detected(config))
            entry = providers.get(provider_name)
            if entry is None:
                return None
            _preferred_name = resolve_model_provider_family(model) if model else None
            if model and _preferred_name is None:
                _logger.warning(
                    "/v1/summarize: model %r is not a recognized Anthropic or OpenAI "
                    "id; refusing to guess a family for provider %r",
                    model,
                    provider_name,
                    extra={"session_id": runner_primary_session_id()},
                )
                return None
            # No model hint at all (empty string): try both slots on this named
            # entry, same as always — there is no family to prefer or refuse.
            _try_order = (
                (_preferred_name, "openai" if _preferred_name == "anthropic" else "anthropic")
                if _preferred_name is not None
                else ("anthropic", "openai")
            )
            _resolved_name: str | None = None
            family_conn = None
            for _name in _try_order:
                family_conn = entry.family(_name)
                if family_conn is not None:
                    _resolved_name = _name
                    break
            if family_conn is None:
                return None
            conn: dict[str, str] = {}
            if family_conn.api_key:
                conn["api_key"] = family_conn.api_key
            if family_conn.base_url:
                conn["base_url"] = (
                    normalize_anthropic_base_url(family_conn.base_url)
                    if _resolved_name == "anthropic"
                    else family_conn.base_url
                )
            return conn or None
        except Exception:
            _logger.warning(
                "/v1/summarize: failed to resolve provider %r",
                provider_name,
                exc_info=True,
                extra={"session_id": runner_primary_session_id()},
            )
            return None

    @app.post("/v1/summarize")
    async def summarize(request: Request) -> JSONResponse:
        body = await request.json()
        messages = body.get("messages")
        model = body.get("model")
        if isinstance(model, str) and model:
            model = _summarize_model_id(model)
        if not isinstance(messages, list) or not model:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'messages' (list) and 'model' (str) are required",
                    }
                },
            )
        connection: dict[str, str] | None = body.get("connection") or None
        if connection is None:
            session_id: str | None = body.get("session_id")
            if session_id is not None:
                connection = _resolve_summarize_connection(
                    session_id,
                    model,
                )
        extra_instructions = body.get("extra_instructions") or None
        llm_client = _get_runner_llm_client()
        resp = await llm_client.responses.create(
            model=model,
            input=build_summarization_input(messages),
            instructions=build_summarization_prompt(
                messages, extra_instructions=extra_instructions
            ),
            tools=[],
            connection_params=connection,
        )
        summary_text = extract_summary_text(resp)
        from omnigent.runtime.compaction import count_text_tokens

        token_count = count_text_tokens(summary_text, model)
        return JSONResponse(content={"text": summary_text, "token_count": token_count})

    @app.post("/v1/elicitations/{elicitation_id}")
    async def elicitation(elicitation_id: str, request: Request) -> Response:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={"error": "not_implemented", "detail": "Runner not configured"},
            )
        body = await request.json()
        response_id = body.get("response_id")
        if not response_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "response_id required in elicitation body",
                },
            )
        conv_id = await _resolve_conversation_id(response_id)
        if conv_id is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": f"Cannot resolve response {response_id}"},
            )
        try:
            client = await process_manager.get_client(conv_id, "any")
        except NoLiveHarnessError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no_live_harness",
                    "detail": "no harness subprocess is running for this conversation",
                },
            )
        try:
            event_body = {
                "type": "approval",
                "elicitation_id": elicitation_id,
                "action": body.get("action"),
            }
            if body.get("content") is not None:
                event_body["content"] = body["content"]
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json=event_body,
                timeout=30.0,
            )
            return _forward_harness_response(resp)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "elicitation_failed",
                    "detail": _client_safe_error_detail(exc, context="elicitation forward"),
                },
            )

    async def _catch_up_scan() -> None:
        recreated_prompts = pending_approvals.notify_server_reconnect()
        if recreated_prompts:
            _logger.info(
                "Recreating %d pending approval or elicitation request(s) after reconnect",
                recreated_prompts,
                extra={"session_id": runner_primary_session_id()},
            )
        # The tunnel just reconnected, which usually means the SERVER restarted
        # (deploy, crash, replica failover) and lost its in-memory session-status
        # cache. This runner did not restart, so every status source still
        # believes its last edge was delivered and nothing re-asserts — a
        # session mid-turn during the restart would sit on a stale ``idle`` for
        # the rest of the turn. Re-arm them before the item scan below.
        if resource_registry is not None:
            try:
                resource_registry.resync_session_statuses()
            except Exception:
                _logger.warning(
                    "Session status resync failed after reconnect",
                    exc_info=True,
                    extra={"session_id": runner_primary_session_id()},
                )
        # A server outage at child-completion time fails the wake POST past its
        # bounded retries; the server is reachable again now, so re-deliver
        # those stranded wakes or the parent never learns its child finished.
        _retry_stranded_wakes()
        for session_id in list(_session_histories):
            try:
                after_id = _last_server_item_id.get(session_id)
                all_new: list[_JsonObject] = []
                while True:
                    params: dict[str, str] = {
                        "limit": "100",
                        "order": "asc",
                    }
                    if after_id:
                        params["after"] = after_id
                    resp = await server_client.get(
                        f"/v1/sessions/{session_id}/items",
                        params=params,
                        timeout=10.0,
                    )
                    if resp.status_code != 200:
                        break
                    page = resp.json()
                    page_items = page.get("data", [])
                    if not page_items:
                        break
                    all_new.extend(page_items)
                    last_id = page_items[-1].get("id")
                    if last_id:
                        after_id = last_id
                        _last_server_item_id[session_id] = last_id
                    if not page.get("has_more", False):
                        break
                if not all_new:
                    continue
                # The cursor is only as new as the last load, so these items can repeat what the
                # history already holds: the next turn assembles it from the record instead.
                if session_id not in _active_turns:
                    _session_histories.pop(session_id, None)
                _last = all_new[-1]
                if (
                    session_id not in _active_turns
                    and _last.get("type") == "message"
                    and _last.get("role") == "user"
                ):
                    _begin_turn_slot(session_id)
                    _publish_turn_status(session_id, "running")
                    agent_id = _session_agent_ids.get(session_id)
                    msg_body: _JsonObject = {
                        "agent_id": agent_id,
                        "model": agent_id or "",
                        # Catch-up has no live server dispatch carrying renderer state.
                        "browser_renderer_available": False,
                    }
                    _turn_task = asyncio.create_task(
                        _run_turn_bg(msg_body, session_id),
                        name=f"turn-catchup-{session_id}",
                    )
                    _active_turns[session_id] = _turn_task
                    _turn_task.add_done_callback(
                        _background_tasks.discard,
                    )
                    _background_tasks.add(_turn_task)
            except (httpx.HTTPError, RuntimeError):
                _logger.warning(
                    "Catch-up scan failed for %s",
                    session_id,
                    exc_info=True,
                )

    app.state.catch_up_scan = _catch_up_scan

    return app


def create_runner_app_from_env() -> FastAPI:
    """Lightweight uvicorn ``--factory`` entry point for transport subprocesses.

    Reads ``RUNNER_SERVER_URL`` from the environment and constructs a
    minimal :class:`httpx.AsyncClient` for the Omnigent server, then delegates
    to :func:`create_runner_app` with no :class:`HarnessProcessManager`,
    no spec resolver, and no terminal registry.

    Used as the default ``app_factory_path`` for
    :class:`~omnigent.runner.transports.tcp.RunnerTCPSubprocess` and
    :class:`~omnigent.runner.transports.uds.RunnerSubprocess`.  It is
    intentionally lighter than :func:`omnigent.runner._entry.create_app`
    so transport smoke tests start quickly without spawning harness pools
    or sweeping orphan directories.

    :returns: A :class:`FastAPI` runner app backed by an httpx client
        pointed at ``RUNNER_SERVER_URL``.
    :raises RuntimeError: If ``RUNNER_SERVER_URL`` is not set in the
        environment.
    """
    import os

    import httpx

    server_url = os.environ.get("RUNNER_SERVER_URL", "").strip()
    if not server_url:
        raise RuntimeError("RUNNER_SERVER_URL is required for the runner subprocess factory")
    from omnigent_client._http import is_loopback_url

    server_client = httpx.AsyncClient(
        base_url=server_url,
        timeout=httpx.Timeout(5.0, read=None),
        # A proxy cannot reach a loopback server, so local targets bypass it.
        trust_env=not is_loopback_url(server_url),
    )
    return create_runner_app(server_client=server_client)


async def _resolve_harness_config(
    *,
    agent_id: str | None,
    spec_resolver: SpecResolver | None,
    session_id: str | None = None,
    model_override: str | None = None,
    harness_override: str | None = None,
    sub_agent_name: str | None = None,
    cwd: Path | None = None,
) -> tuple[str, dict[str, str] | None]:
    """Resolve harness type + spawn-env from the agent spec.

    :param agent_id: Agent id to resolve the spec for.
    :param spec_resolver: Resolver that returns the spec for *agent_id*.
    :param session_id: Session/conversation id, threaded to the resolver.
    :param model_override: Per-session ``/model`` override, applied to the
        spawn-env model so it takes effect on the SDK harnesses.
    :param harness_override: Per-session brain-harness override (validated
        at session create, forwarded by the server in the message body),
        e.g. ``"pi"``. Replaces the spec's ``executor.config.harness``.
    :param sub_agent_name: For a sub-agent session, the dispatched
        sub-agent's name (e.g. ``"claude_code"``). The bound *agent_id*
        resolves to the PARENT spec, so without this swap a child's turn
        resolves the parent's harness (``claude-sdk``) and the process
        manager respawns the wrong harness for the child. When set,
        the parent entry is swapped for the child's — spec AND bundle dir —
        via :func:`_resolve_sub_agent_spec_entry` before harness derivation,
        so the spawn-env advertises the child's bundle rather than the
        parent's. ``None`` for top-level sessions.
    :param cwd: Runtime working directory for harnesses that need it.
    :returns: ``(harness, spawn_env)`` derived from the resolved spec.
    :raises RuntimeError: When a spec_resolver is configured but the spec
        cannot be resolved. Callers catch this to surface a clean error
        rather than spawning an invalid harness subprocess.
    """
    if agent_id and spec_resolver:
        spec_entry = await spec_resolver(agent_id, session_id)
        spec = _unwrap_resolved_spec(spec_entry)
        workdir = _resolved_spec_workdir(spec_entry)
        if spec is not None:
            # Swap to the sub-agent's own spec so its harness (not the
            # parent's) drives the turn. Mirrors the POST /v1/sessions and
            # _run_turn_bg swaps; applied here so the harness-HTTP path is
            # sub-agent-aware too, even after a reconnect drops the
            # in-memory _session_sub_agent_names map.
            # The child's bundle dir comes from the same resolution, so the
            # spawn-env below advertises the child's bundle — not the
            # parent's, whose skills and tools the child has no claim to.
            if sub_agent_name:
                sub_entry = _resolve_sub_agent_spec_entry(spec_entry, sub_agent_name)
                if sub_entry is None:
                    _warn_unresolved_sub_agent(session_id, sub_agent_name)
                else:
                    spec = _unwrap_resolved_spec(sub_entry)
                    workdir = _resolved_spec_workdir(sub_entry)
            raw_harness = (
                harness_override or spec.executor.config.get("harness") or spec.executor.type
            )
            harness = canonicalize_harness(raw_harness) or raw_harness
            spawn_env = _build_spawn_env_from_spec(
                spec,
                raw_harness,
                cwd=cwd,
                workdir=workdir,
                model_override=model_override,
                session_id=session_id,
            )
            return harness, spawn_env

    if spec_resolver is not None:
        # spec_resolver is configured but either agent_id was not provided or
        # the resolver returned no spec. Fail loud rather than silently falling
        # through to the test-only harness and spawning a broken subprocess.
        if not agent_id:
            raise RuntimeError(
                "Cannot select a harness: agent_id is missing and a spec_resolver "
                "is configured. Ensure agent_id is forwarded in the turn body."
            )
        raise RuntimeError(
            f"No agent spec found for agent_id={agent_id!r}; cannot select a harness."
        )

    # Fallback for tests that register a custom harness in _HARNESS_MODULES
    # (spec_resolver is None in the test runner).
    return "runner-test-default", None


# The per-harness env var that carries the model into the spawn-env (SDK /
# in-process) harnesses. Used to apply a per-session ``/model`` override at
# highest precedence — see :func:`_build_spawn_env_from_spec`.
_HARNESS_MODEL_ENV_KEY = model_env_keys()


class _SpawnEnvBuilder(Protocol):
    def __call__(
        self,
        spec: object,
        *,
        cwd: Path | None,
        workdir: Path | None,
    ) -> dict[str, str]:
        raise NotImplementedError


class _ModelCopyValue(Protocol):
    def model_copy(self, *, update: Mapping[str, object]) -> object: ...


async def _ensure_session_subagent_router(
    session_id: str,
    harness: str | None,
    *,
    server_client: httpx.AsyncClient | None,
    routing_class: SessionRoutingClass | None = None,
) -> None:
    """Start this session's subagent-routing endpoint.

    Started for Smart Routing sessions only: a plain session must not carry
    the loopback server, its on-disk bearer token, or an in-process hook on
    every ``Task`` for a verdict the server never routes.

    Never raises: ``ensure_session_router_quietly`` owns the bridge-dir
    resolution too, so a hostile or pre-existing ``$TMPDIR`` root cannot
    fail session creation for harnesses that do not even use routing.

    :param session_id: Session/conversation identifier.
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :param server_client: Runner→server client the relay forwards on.
        ``None`` (in-process tests) skips the start.
    :param routing_class: The session's Smart Routing class. ``None``
        reads whatever was stamped at session init, which for an unknown
        session is the plain class.
    """
    from omnigent.runner.subagent_routing import ensure_session_router_quietly

    resolved = routing_class if routing_class is not None else session_routing_class(session_id)
    ensure_session_router_quietly(
        session_id,
        server_client=server_client,
        harness=harness,
        routing_class=resolved,
    )


def _build_spawn_env_from_spec(
    spec: AgentSpec,
    harness: str,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
    model_override: str | None = None,
    session_id: str | None = None,
) -> dict[str, str] | None:
    """Build spawn-env from spec — mirrors workflow.py's helpers.

    :param spec: The resolved agent spec.
    :param harness: Requested harness, e.g. ``"claude-sdk"``.
    :param cwd: Runtime working directory for harnesses that need it.
    :param workdir: Bundle workdir, threaded to the builders.
    :param session_id: Session/conversation id, used to hand the harness
        this session's subagent-routing endpoint. ``None`` omits it.
    :param model_override: The per-session ``/model`` override, e.g.
        ``"claude-sonnet-4-6"``, or ``None``. When set, it overrides the
        ``HARNESS_<H>_MODEL`` the builder baked in (spec model / provider
        default / catalog default) so ``/model`` actually takes effect on
        the SDK / in-process harnesses.
    :returns: The spawn-env dict, or ``None`` for unknown harnesses.
    """
    harness = canonicalize_harness(harness) or harness
    # A ``${VAR}`` that survived spec loading must fail the turn here, not
    # reach the provider as a literal model id.
    from omnigent.spec.parser import check_unresolved_env_vars

    configured_model = model_override or getattr(getattr(spec, "executor", None), "model", None)
    if configured_model:
        check_unresolved_env_vars("executor.model", configured_model)
    effective_spec = spec
    from omnigent.inference_config import load_runtime_inference_config, parse_inference_config

    has_inference_bindings = bool(parse_inference_config(load_runtime_inference_config()))
    if has_inference_bindings and dataclasses.is_dataclass(spec):
        effective_spec = dataclasses.replace(
            spec,
            executor=dataclasses.replace(
                spec.executor,
                config={**spec.executor.config, "harness": harness},
                model=model_override if model_override is not None else spec.executor.model,
            ),
        )
    if model_override is not None:
        executor = getattr(spec, "executor", None)
        if hasattr(spec, "model_copy") and hasattr(executor, "model_copy"):
            copied_executor = cast(_ModelCopyValue, executor).model_copy(
                update={"model": model_override}
            )
            effective_spec = cast(
                AgentSpec,
                cast(_ModelCopyValue, spec).model_copy(update={"executor": copied_executor}),
            )
    try:
        from omnigent.runtime.workflow import (
            _build_claude_sdk_spawn_env,
            _build_pi_spawn_env,
        )

        if harness == "claude-sdk":
            env = _build_claude_sdk_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        elif harness == "pi":
            env = _build_pi_spawn_env(effective_spec, cwd=cwd, workdir=workdir)
        else:
            builder_path = spawn_env_builders().get(harness)
            if builder_path is not None:
                builder = load_object(builder_path)
                if not callable(builder):
                    raise TypeError(f"spawn environment builder {builder_path!r} is not callable")
                env = cast(_SpawnEnvBuilder, builder)(
                    effective_spec,
                    cwd=cwd,
                    workdir=workdir,
                )
            else:
                # Unknown harnesses have no spawn env.
                return None
    except ImportError:
        return None

    if env is not None:
        from omnigent.harnesses.agent_env import (
            desktop_session_passthrough,
            strip_desktop_session_env,
        )

        env = strip_desktop_session_env(env)
        env.update(desktop_session_passthrough(effective_spec.os_env))

    # Point the harness process at this session's subagent-routing endpoint
    # when one is running (started at session init). Scoped to *harness* so
    # only that harness's router vars are set. Empty when the session has no
    # router.
    if env is not None and session_id:
        from omnigent.runner.subagent_routing import session_router_env

        env.update(session_router_env(session_id, harness))

    # Per-session ``/model`` override wins over everything the builder baked
    # into HARNESS_<H>_MODEL. Without this, `/model` is recorded in the
    # readout but the turn still uses the provider/catalog default.
    if model_override and env is not None:
        model_key = _HARNESS_MODEL_ENV_KEY.get(harness)
        if model_key is not None:
            env[model_key] = model_override

    # Routing visibility: log the resolved gateway target so operators can
    # confirm which provider a turn actually hits (api.anthropic.com /
    # api.openai.com for a key, vs a gateway). Logged here in the
    # runner process (INFO is emitted) rather than the harness subprocess
    # (which suppresses inner.* INFO).
    if env is not None:
        prefix = f"HARNESS_{harness.upper().replace('-', '_')}"
        _logger.info(
            "%s gateway routing: gateway=%s base_url=%s model=%s",
            harness,
            env.get(f"{prefix}_GATEWAY"),
            # A harness that carries per-family URLs (pi) sets only the plural
            # ``_BASE_URLS`` JSON; without the fallback it logs base_url=None.
            env.get(f"{prefix}_GATEWAY_BASE_URL") or env.get(f"{prefix}_GATEWAY_BASE_URLS"),
            env.get(_HARNESS_MODEL_ENV_KEY.get(harness, f"{prefix}_MODEL")),
            extra={"session_id": session_id},
        )
    return env


# ── Agent-start policy gate ────────────────────────────────────────────


async def _evaluate_agent_start_gate(
    spec: AgentSpec,
    harness: str,
) -> PolicyVerdict | None:
    """Evaluate ``__agent_start`` through the spec's policy gate.

    Constructs a :class:`RunnerToolPolicyGate` from the spec and
    evaluates a synthetic ``__agent_start`` tool call.  This reuses
    the same gate that guards MCP tool calls — no round-trip to the
    Omnigent server required.

    :param spec: The resolved agent spec (``AgentSpec``).
    :param harness: Canonical harness name, e.g. ``"claude-sdk"``.
    :returns: A :class:`PolicyVerdict` if the spec has guardrails
        policies, ``None`` if no policies apply.
    """
    from omnigent.runner.policy import RunnerToolPolicyGate

    gate = RunnerToolPolicyGate.from_spec(spec)
    if gate.is_empty:
        return None

    sandbox_dict: _JsonObject | None = None
    if spec.os_env is not None and spec.os_env.sandbox is not None:
        sandbox_dict = cast(_JsonObject, dataclasses.asdict(spec.os_env.sandbox))

    return await gate.evaluate_tool_call(
        "sys_agent_start",
        {
            "agent_name": getattr(spec, "name", None) or "",
            "harness": harness,
            "sandbox": sandbox_dict,
        },
    )


def _apply_sandbox_override_from_verdict(
    spec: AgentSpec,
    verdict_data: object,
) -> None:
    """Apply sandbox override from a policy verdict's ``data`` field.

    The ``enforce_sandbox`` policy returns replacement ``data`` shaped
    as ``{"name": "sys_agent_start", "arguments": {"sandbox": {...}}}``.
    This extracts the ``sandbox`` dict and mutates ``spec.os_env``
    in-place.

    :param spec: The agent spec (``AgentSpec``) — mutated in-place.
    :param verdict_data: The ``PolicyVerdict.data`` payload, expected
        to be a dict with ``arguments.sandbox``.
    """
    from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec

    if not isinstance(verdict_data, Mapping):
        return
    args = verdict_data.get("arguments")
    if not isinstance(args, Mapping):
        return
    sandbox_override = args.get("sandbox")
    if not isinstance(sandbox_override, Mapping):
        return

    if spec.os_env is None:
        spec.os_env = OSEnvSpec()
    if spec.os_env.sandbox is None:
        spec.os_env.sandbox = OSEnvSandboxSpec()

    for key, value in sandbox_override.items():
        if hasattr(spec.os_env.sandbox, key):
            setattr(spec.os_env.sandbox, key, value)
