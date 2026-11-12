"""Lower-layer helpers for the sessions routes (call-depth 0-1).

Leaf/near-leaf helpers extracted from ``sessions.py``: SSE item builders,
publishers, persistence, validation, and the runner-forward primitives.
Imports shared state/constants from ``.common``; imported by
``.orchestration`` and by the router in ``sessions.py``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import secrets
import time
import urllib.parse
import weakref
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Final, Literal, cast

import httpx
from fastapi import (
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError, StatementError

from omnigent.db.utils import generate_task_id
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import (
    USER_SESSION_TITLE_MAX_CHARS,
    Agent,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    SlashCommandData,
    StoredFile,
    synthesize_conversation_title,
)
from omnigent.entities.conversation import (
    ITEM_TYPE_TO_DATA_CLS,
    parse_item_data,
)
from omnigent.entities.permission import SessionPermission
from omnigent.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorImpact,
    ErrorPhase,
    OmnigentError,
    category_for_code,
    restart_on_stale_cursor,
)
from omnigent.inference_config import snapshot_runtime_config
from omnigent.models.model_metadata import concrete_reported_model
from omnigent.policies.types import EvaluationContext
from omnigent.runner.identity import (
    token_bound_runner_id,
)
from omnigent.runner.launch_failure import classify_native_turn_error
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.subagent_routing import ROUTING_DECISION_LABEL_KEY
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runtime import (
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.public_error_codes import REPLY_NOT_SAVED, public_error_code
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server import presence, session_live_state, shutdown_state
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
)
from omnigent.server.host_registry import HostConnection, HostRegistry, RunnerExitReports
from omnigent.server.hosts import (
    MANAGED_SANDBOX_LABEL_NAMESPACE,
    HostDeployment,
    HostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    RepoWorkspace,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
)

# Shared constants, state, and small dataclasses live in the _sessions.common
# leaf module; import them here so this module and its re-exporters see the same
# objects. The mutable caches are shared by reference across the package.
# Runtime bindings that tests patch on the historical ``sessions`` facade are
# imported from common as facade-delegating proxies. They stay out of common's
# ``__all__`` and the facade's explicit re-exports, preserving its real runtime
# bindings so a facade-level monkeypatch is honoured in this module too.
from omnigent.server.routes._sessions.common import (  # noqa: F401
    _APPROVAL_TYPE,
    _CHILD_PREVIEW_LIMIT,
    _CLAUDE_NATIVE_UI_LABEL_KEY,
    _CLAUDE_NATIVE_UI_LABEL_VALUE,
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _DENY_SENTINEL_PREFIX,
    _ELICITATION_MODE,
    _EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
    _HOOK_ELICITATION_ID_RE,
    _HOST_LAUNCH_RESULT_TIMEOUT_S,
    _LABEL_VALUE_MAX_LEN,
    _LAST_TASK_ERROR_AGENT_NAME_LABEL_KEY,
    _LAST_TASK_ERROR_CAUSE_LABEL_KEY,
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY,
    _LAST_TASK_ERROR_TITLE_LABEL_KEY,
    _MAX_TERMINAL_LAUNCH_ARG_LEN,
    _MAX_TERMINAL_LAUNCH_ARGS,
    _MODEL_TOKEN_KEYS,
    _RUNNER_CONVICTION_POLL_S,
    _RUNNER_FORWARD_TIMEOUT,
    _SERVER_STREAM_EVENT_ADAPTER,
    _SESSION_STREAM_HEARTBEAT_INTERVAL_S,
    _SHARED_DISCOVERY_KEY,
    _SLASH_COMMAND_TYPE,
    _STOP_RUNNER_RESULT_TIMEOUT_S,
    _STOP_SESSION_TYPE,
    _TURN_ACTOR_LABEL,
    _UI_ADDED_AGENT_TITLE_PREFIX,
    _UPLOAD_READ_CHUNK_BYTES,
    COST_CONTROL_OVERRIDE_VALUES,
    SUBAGENT_ROUTING_OVERRIDE_VALUES,
    _catalog_prefetch_tasks,
    _logger,
    _managed_launch_tasks,
    _native_ask_gate_locks,
    _pending_policy_ask_writes,
    _read_explicit_unread,
    _read_last_seen,
    _session_active_response_cache,
    _session_background_task_count_cache,
    _session_background_tasks_cache,
    _session_mcp_startup_cache,
    _session_sandbox_status_cache,
    _session_status_cache,
    _session_terminal_pending_cache,
    build_policy_engine,
    get_agent_cache,
    get_caps,
    get_server_host_registry,
    get_server_runner_router,
    session_stream,
    set_server_runner_router,
    user_session_stream,
)
from omnigent.server.schemas import (
    BackgroundTaskInfo,
    ChildSessionSummary,
    CompletedEvent,
    CreatedSessionResponse,
    ErrorDetail,
    ErrorEvent,
    McpServerStartup,
    ModelUsage,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    PolicyDeniedEvent,
    ReasoningStartedEvent,
    ReasoningTextDeltaEvent,
    ResponseObject,
    RetryErrorDetail,
    SandboxStatus,
    SessionBtwSidechatEvent,
    SessionChildSessionUpdatedEvent,
    SessionCreateMetadata,
    SessionEventInput,
    SessionGitOptions,
    SessionInputConsumedEvent,
    SessionInputConsumedPayload,
    SessionInterruptedEvent,
    SessionInterruptedPayload,
    SessionListItem,
    SessionMcpStartupEvent,
    SessionModelEvent,
    SessionReasoningEffortEvent,
    SessionResourceListPage,
    SessionResourcePaginatedList,
    SessionSandboxStatusEvent,
    SessionStatusEvent,
    SessionSupersededEvent,
    SessionTerminalPendingEvent,
    SessionTitleEvent,
    ToolOutputDeltaEvent,
)
from omnigent.spec.types import (
    AgentSpec,
    Phase,
    PolicyAction,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import (
    ARCHIVED_AT_LABEL_KEY,
    PINNED_LABEL_KEY,
    ConversationNotFoundError,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.util.cost_plan import (
    COST_CONTROL_LABEL_NAMESPACE,
    reserved_cost_control_keys,
)
from omnigent.util.reasoning_effort import (
    EFFORT_VALUES,
    validate_effort,
)
from omnigent.util.session_lifecycle import (
    labels_with_closed_status,
    title_without_closed_marker,
)


def _publish_policy_denied(session_id: str, reason: str, phase: str) -> None:
    """
    Publish a native policy-DENY signal on the session stream.

    A native harness's policy DENY is decided synchronously in the
    ``/policies/evaluate`` hook response, so nothing on the stream otherwise
    reflects that an action was blocked. This surfaces the decision as a
    positive event for observers (web UI, capability bench). Fire-and-forget.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param reason: Deny reason from the deciding policy.
    :param phase: The policy phase the DENY landed on, e.g. ``"tool_call"``.
    :returns: None.
    """
    event = PolicyDeniedEvent(
        type="response.policy_denied",
        conversation_id=session_id,
        reason=reason,
        phase=phase,
    )
    session_stream.publish(session_id, event.model_dump())


def _read_state_entry(user_id: str | None, session_id: str) -> tuple[int | None, bool]:
    """
    Read the caller's read-state for one session, for embedding in the
    per-user ``GET /v1/sessions`` list items.

    :param user_id: Authenticated user id, or ``None`` in single-user mode.
    :param session_id: Session/conversation identifier.
    :returns: ``(last_seen, unread)`` — the wall-clock baseline (or ``None``
        when the user has never seen the session) and the explicit-unread flag.
    """
    key = _discovery_key(user_id)
    last_seen = _read_last_seen.get(key, {}).get(session_id)
    unread = session_id in _read_explicit_unread.get(key, set())
    return last_seen, unread


def _set_read_state(user_id: str | None, session_id: str, last_seen: int, unread: bool) -> None:
    """
    Set the caller's read-state for one session.

    :param user_id: Authenticated user id, or ``None`` in single-user mode.
    :param session_id: Session/conversation identifier.
    :param last_seen: Wall-clock baseline in seconds.
    :param unread: Whether the session is explicitly flagged unread.
    """
    key = _discovery_key(user_id)
    _read_last_seen.setdefault(key, {})[session_id] = last_seen
    if unread:
        _read_explicit_unread.setdefault(key, set()).add(session_id)
    else:
        unread_set = _read_explicit_unread.get(key)
        if unread_set is not None:
            unread_set.discard(session_id)


def _prune_session_read_state(session_id: str) -> None:
    """
    Drop a session's read-state from every user's caches.

    Called when a session leaves the default view for good — on delete, and
    on archive (archived sessions are hidden and never show the unread dot).
    This bounds the otherwise-monotonic ``_read_last_seen`` growth to live,
    non-archived sessions. Read-state is a session-level removal (the session
    is gone/archived for everyone), so it clears across all users. Unarchiving
    does NOT restore the prior state — the session reads as seen, which is the
    intended "done with it" semantics of archiving.

    :param session_id: Session/conversation identifier.
    """
    for seen in _read_last_seen.values():
        seen.pop(session_id, None)
    for unread in _read_explicit_unread.values():
        unread.discard(session_id)


def _discovery_key(user_id: str | None) -> str:
    """
    Map an (optional) user id to the :mod:`user_session_stream` channel key.

    :param user_id: Authenticated user id, e.g. ``"alice@example.com"``, or
        ``None`` in single-user / no-auth mode.
    :returns: ``user_id`` when set, else :data:`_SHARED_DISCOVERY_KEY`.
    """
    return user_id if user_id is not None else _SHARED_DISCOVERY_KEY


def _announce_session_added(user_id: str | None, session_id: str) -> None:
    """
    Push a ``session_added`` discovery event to a user's updates streams.

    Called after a session becomes accessible to ``user_id`` (created, forked,
    or shared) so that user's open tabs surface it without a list poll. A no-op
    when the user has no stream connected.

    :param user_id: The user the session is now accessible to (the owner on
        create/fork, the grantee on share), or ``None`` in single-user mode.
    :param session_id: The newly-accessible session id, e.g. ``"conv_abc123"``.
    """
    user_session_stream.publish(
        _discovery_key(user_id), {"type": "session_added", "session_id": session_id}
    )


async def _grant_default_public(
    app_state: Any,
    permission_store: PermissionStore | None,
    session_id: str,
    *,
    managed: bool,
    workspace: str | None,
    host_id: str | None = None,
) -> None:
    """Apply the server's default-public-sessions policy to a just-created session.

    Writes the read-only ``__public__`` grant when the admin setting covers this
    session (see :func:`new_session_starts_public`); a no-op otherwise, and in
    single-user mode (no permission store). A session bound to a server-managed
    sandbox host counts as managed even when it didn't request a new sandbox.
    """
    from omnigent.server.sharing_settings import (
        DefaultPublicSessions,
        default_public_policy,
        host_is_managed_sandbox,
        new_session_starts_public,
    )

    if permission_store is None:
        return
    if (
        not managed
        and host_id is not None
        and default_public_policy(app_state) is DefaultPublicSessions.SANDBOX
    ):
        managed = host_is_managed_sandbox(getattr(app_state, "host_registry", None), host_id)
    if not new_session_starts_public(app_state, managed=managed, workspace=workspace):
        return
    await asyncio.to_thread(permission_store.ensure_user, RESERVED_USER_PUBLIC)
    await asyncio.to_thread(permission_store.grant, RESERVED_USER_PUBLIC, session_id, LEVEL_READ)


def announce_hosts_changed(user_id: str | None) -> None:
    """
    Push a ``hosts_changed`` event to a user's session-updates streams.

    Called when a host owned by ``user_id`` connects or disconnects so the
    client invalidates its hosts cache without polling. A no-op when the user
    has no stream connected.

    :param user_id: Owner of the host that changed, or ``None`` in
        single-user mode.
    """
    user_session_stream.publish(_discovery_key(user_id), {"type": "hosts_changed"})


def announce_projects_changed(user_id: str | None) -> None:
    """
    Push a ``projects_changed`` event to a user's session-updates streams.

    Called after one of ``user_id``'s projects is created, updated (renamed,
    config change), or deleted, so that user's other connected clients refresh
    their projects cache instead of showing the stale name until a reload. A
    no-op when the user has no stream connected.

    :param user_id: Owner of the project that changed, or ``None`` in
        single-user mode.
    """
    user_session_stream.publish(_discovery_key(user_id), {"type": "projects_changed"})


def _native_ask_gate_lock(conversation_id: str, deciding_policy: str) -> asyncio.Lock:
    """
    Return the lock serializing native ASK gates for one (session, policy).

    Concurrent native tool calls that all trip the same ASKing policy must
    prompt the human once, not once each. Callers hold the returned lock
    across the entire human-approval wait and re-evaluate the policy under it;
    the first approval records a checkpoint that collapses the siblings to
    ALLOW. Get-or-create is race-free because there is no ``await`` between the
    lookup and the insert (single event loop).

    :param conversation_id: Omnigent conversation id whose ASK gate is being
        serialized, e.g. ``"conv_abc123"``. Sub-agent native tool calls
        evaluate against the parent conversation id, so they share its lock.
    :param deciding_policy: Name of the policy that produced the ASK verdict,
        e.g. ``"session_cost_guard"``. Distinct policies get distinct locks so
        their approval prompts can surface concurrently.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        caller for the same ``(conversation_id, deciding_policy)`` pair.
    """
    key = (conversation_id, deciding_policy)
    lock = _native_ask_gate_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _native_ask_gate_locks[key] = lock
    return lock


async def _poll_request_disconnect(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._poll_request_disconnect(*args, **kwargs)


async def _poll_request_disconnect_impl(request: Request) -> None:
    """
    Resolve once Starlette reports the client closed the connection.

    Long-poll routes that park on a verdict (e.g. the Claude-native
    ``PermissionRequest`` hook) use this to detect that the upstream
    client has hung up — Claude closes its HTTP request when its
    TUI prompt receives an answer first, and without this wait the
    handler would sit out the full timeout to notice.

    Blocks on ``request.receive()`` rather than polling
    ``request.is_disconnected()``. The poll variant runs each check
    inside a pre-cancelled anyio ``CancelScope`` (Starlette's
    non-blocking receive idiom); an external ``Task.cancel()`` that
    lands while that scope is unwinding coalesces with the scope's own
    cancellation and is swallowed with it, so the poller survives its
    cancel and the caller's race cleanup blocks on it forever.
    A blocking receive has no cancel scope in its await chain, so
    cancellation always propagates; it is also cheaper than waking
    twice a second.

    :param request: The active FastAPI :class:`Request`. By the time
        the handler parks, the route has consumed the body, so the
        next receive yields only ``http.disconnect``.
    :returns: None when the disconnect is observed. Cancellation
        propagates: callers that race this against a verdict Future
        cancel the wait once the verdict arrives.
    """
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


def _attachment_disposition(filename: str) -> str:
    """Build a safe ``Content-Disposition: attachment`` header value.

    The filename is user-controlled, so it cannot be interpolated
    into the header verbatim — a quote or newline would let the
    uploader inject header content or break parsing. We emit an
    ASCII-only ``filename`` fallback (with quotes/backslashes/control
    characters stripped) plus an RFC 5987 ``filename*`` parameter that
    percent-encodes the full UTF-8 name for modern browsers.

    :param filename: The stored, user-supplied filename.
    :returns: A ``Content-Disposition`` header value forcing download.
    """
    # ASCII fallback: drop anything outside printable ASCII and the
    # characters that are structurally significant in the header.
    ascii_name = "".join(ch for ch in filename if 0x20 <= ord(ch) < 0x7F and ch not in '"\\')
    if not ascii_name:
        ascii_name = "download"
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


# A file id maps to one set of bytes for the file's whole life — content is
# never rewritten in place, only deleted — so the id is a strong validator and
# the bytes can be cached indefinitely. ``private`` keeps the response out of
# shared caches, since the bytes are readable only by session members.
FILE_CONTENT_CACHE_CONTROL = "private, max-age=31536000, immutable"


def _file_content_etag(file_id: str) -> str:
    """Build the strong ``ETag`` for a stored file's content.

    :param file_id: The stored file identifier.
    :returns: A quoted entity tag value.
    """
    return f'"{file_id}"'


def _if_none_match_matches(header: str | None, etag: str) -> bool:
    """Check an ``If-None-Match`` request header against an entity tag.

    :param header: Raw ``If-None-Match`` value, or ``None`` when absent.
    :param etag: The current entity tag, quoted.
    :returns: ``True`` when the client's cached copy is still current.
    """
    if not header:
        return False
    candidates = [candidate.strip() for candidate in header.split(",")]
    if "*" in candidates:
        return True
    # Comparison is weak per RFC 9110: strip the ``W/`` prefix before matching.
    return any(candidate.removeprefix("W/") == etag for candidate in candidates)


def _stored_file_to_resource(
    session_id: str,
    stored: StoredFile,
) -> dict[str, Any]:
    """Convert a :class:`StoredFile` to a session file resource dict.

    Matches the ``session.resource`` shape with ``type: "file"``
    used by the unified inventory and the session-scoped file
    endpoints.

    :param session_id: Owning session/conversation id.
    :param stored: The stored file entity.
    :returns: JSON-serializable resource dict.
    """
    return {
        "id": stored.id,
        "object": "session.resource",
        "type": "file",
        "session_id": session_id,
        "name": stored.filename,
        "metadata": {
            "filename": stored.filename,
            "bytes": stored.bytes,
            "created_at": stored.created_at,
            "source_metadata": stored.source_metadata,
        },
    }


def _publish_and_persist_resource_event(
    session_id: str,
    event_type: str,
    resource_id: str,
    resource_type: str,
    conversation_store: ConversationStore,
    resource: dict[str, Any] | None = None,
) -> None:
    """Publish an SSE event and persist it as a conversation item.

    Emits the event on the live session stream so connected
    clients see it immediately, and appends a ``resource_event``
    conversation item so reconnecting clients discover it in the
    snapshot.

    :param session_id: Session/conversation identifier.
    :param event_type: SSE event type, e.g.
        ``"session.resource.created"``.
    :param resource_id: Opaque id of the affected resource.
    :param resource_type: Kind of resource, e.g. ``"terminal"``.
    :param conversation_store: Store for persisting the item.
    :param resource: Full resource dict for created events.
    """
    from omnigent.entities.conversation import ResourceEventData

    sse_payload: dict[str, Any] = {"type": event_type}
    if event_type == "session.resource.created":
        sse_payload["resource"] = resource or {}
    else:
        sse_payload["resource_id"] = resource_id
        sse_payload["resource_type"] = resource_type
        sse_payload["session_id"] = session_id

    session_stream.publish(session_id, sse_payload)

    item = NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=event_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )
    try:
        conversation_store.append(session_id, [item])
    except (AttributeError, TypeError, ValueError, RuntimeError):
        _logger.debug(
            "Failed to persist resource event for session=%s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )


def _canonical_tool_input(tool_input: dict[str, Any] | None) -> dict[str, Any]:
    """
    Canonicalize a tool input for terminal-resolved correlation.

    The park side records an absent / non-dict input as ``None`` (a
    permission prompt whose hook payload carries no ``tool_input`` — see
    the ``_publish_and_wait_for_harness_elicitation`` call sites), while
    the mirror side normalizes the parsed transcript arguments to ``{}``
    (see :func:`_drive_terminal_resolved_elicitation`). Both mean "no
    input", so collapse them to ``{}`` before comparing — otherwise a
    no-input prompt would never match its own mirrored result (``None ==
    {}`` is ``False``) and, with no count-based fallback, would orphan
    until the hook timeout.

    :param tool_input: Parked or mirrored tool input, e.g.
        ``{"command": "ls"}``, ``{}``, or ``None``.
    :returns: The dict unchanged, or ``{}`` when it is ``None``.
    """
    return tool_input if isinstance(tool_input, dict) else {}


def _signal_terminal_resolved_harness_elicitation(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._signal_terminal_resolved_harness_elicitation(*args, **kwargs)


def _signal_terminal_resolved_harness_elicitation_impl(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
) -> None:
    """
    Resolve the parked prompt a mirrored tool result belongs to,
    ending its long-poll promptly.

    Called when the transcript forwarder mirrors a tool result
    (``function_call_output``) for a native session. A tool result is
    only written AFTER the user answered that tool's permission prompt
    in the native terminal — on accept the tool ran and produced output,
    on reject the harness records a rejection result — so its arrival is
    a reliable "the terminal already resolved this" signal.

    Correlation is by exact tool identity, never positional: a result
    resolves a parked prompt only when it has the SAME ``tool_name`` AND
    the SAME ``tool_input`` in the same session. Claude Code's
    ``PermissionRequest`` payload carries no ``tool_use_id`` (the id is
    minted only when the tool call is emitted, after the permission
    check), so ``(tool_name, tool_input)`` is the only correlation signal
    available — and both sides are unmodified JSON round-trips of the
    same input, so exact equality holds whenever they describe the same
    call (absent input and empty input both canonicalize to ``{}`` via
    :func:`_canonical_tool_input`, since the park and mirror sides spell
    "no input" differently — ``None`` vs ``{}``). A non-matching or
    ambiguous result resolves nothing; the web verdict or timeout still
    applies. Exact-only matching is what stops
    one prompt's result from clearing a different prompt: approving
    ``Bash{ls}`` in the web UI un-parks it, and mirroring its own output
    must not then clear a still-pending ``Bash{pwd}`` sibling (an
    unrelated auto-allowed same-named tool's output is harmless for the
    same reason).

    Best-effort and idempotent: a no-op when no parked prompt matches
    (e.g. the web UI already resolved it, the tool needed no permission,
    or it is an unrelated tool). Harness-agnostic by construction —
    keyed on the parked prompt's tool identity, not on a claude-native
    check — so a Codex hook that records ``tool_name`` benefits too.

    :param session_id: Omnigent conversation id whose forwarder mirrored the
        result, e.g. ``"conv_abc123"``.
    :param tool_name: Tool name the result is for, e.g. ``"Bash"``.
    :param tool_input: Tool input the result is for, e.g.
        ``{"command": "ls"}``, or ``None`` if unavailable.
    """
    candidates = [
        parked
        for parked in _harness_parked_elicitations.values()
        if parked.session_id == session_id
        and parked.tool_name == tool_name
        and not parked.resolved_elsewhere.is_set()
    ]
    if not candidates:
        return
    mirrored_input = _canonical_tool_input(tool_input)
    for parked in candidates:
        if _canonical_tool_input(parked.tool_input) == mirrored_input:
            parked.resolved_elsewhere.set()
            return
    # No exact input match. Correlation is exact-only: resolving a
    # same-named-but-different-input prompt here would clear the wrong
    # card, so leave every candidate to its own result / web verdict /
    # timeout. This branch is reached routinely and benignly — e.g. after
    # a sibling prompt was web-approved and un-parked, its mirrored output
    # finds only the still-pending different-input prompt — so it logs at
    # debug, not warning. (A genuine match failing to compare equal would
    # also land here, but is indistinguishable from the benign case inside
    # this call; both inputs are unmodified JSON round-trips, so such drift
    # is not expected.)
    _logger.debug(
        "Mirrored %s result in %s matched no parked prompt by input "
        "(%d same-named prompt(s) pending); leaving them to web verdict/timeout.",
        tool_name,
        session_id,
        len(candidates),
    )


def _consume_pre_resolved_harness_elicitation(
    session_id: str,
    elicitation_id: str,
    request_fingerprint: str | None = None,
) -> _PreResolvedHarnessElicitation | None:
    """
    Consume a resolution that arrived before the hook wait registered.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :param request_fingerprint: Digest of the consuming re-park's request
        params, e.g. a sha256 hex string. A verdict-carrying tombstone
        is adopted ONLY on a proven same-question match: both sides
        must carry a fingerprint and they must be equal. Any other
        combination (either side ``None``, or a mismatch) fails closed
        — the tombstone is dropped and the prompt is re-published — so
        a stale approval can never gate a DIFFERENT question that
        reused this id. Terminal-side tombstones (``result is None``)
        skip the check: adopting one only fail-asks, and their producer
        has no params to fingerprint.
    :returns: The consumed tombstone when one matched this session
        (its ``result`` carries the web verdict to honor, or ``None``
        for a terminal-side resolution), or ``None`` when nothing was
        pre-resolved.
    """
    _prune_pre_resolved_harness_elicitations()
    tombstone = _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    if tombstone is None:
        return None
    if tombstone.session_id != session_id:
        _harness_pre_resolved_elicitations[elicitation_id] = tombstone
        return None
    if tombstone.result is not None and (
        tombstone.request_fingerprint is None
        or request_fingerprint is None
        or tombstone.request_fingerprint != request_fingerprint
    ):
        # A verdict is replayed only on a proven same-question match.
        # A differing fingerprint means the id was reused by a LATER,
        # different question; a missing fingerprint on either side
        # means the match cannot be proven (e.g. the gap path found no
        # valid pending prompt to digest). Both fail closed: drop the
        # tombstone and let the new prompt be published — the safe cost
        # is one re-ask, never a stale approval gating a new question.
        return None
    return tombstone


def _prune_pre_resolved_harness_elicitations(now: float | None = None) -> None:
    """
    Prune stale or excess pre-resolved harness elicitation tombstones.

    :param now: Optional wall-clock timestamp from ``time.time()``,
        e.g. ``1710000000.0``. ``None`` reads the current time.
    :returns: None.
    """
    if not _harness_pre_resolved_elicitations:
        return
    # Resolve limits through the facade so a test's monkeypatch of these
    # constants is honored here.
    from omnigent.server.routes import sessions as _facade

    now = time.time() if now is None else now
    expired = [
        elicitation_id
        for elicitation_id, tombstone in _harness_pre_resolved_elicitations.items()
        if now - tombstone.created_at > _facade._HARNESS_PRE_RESOLVED_ELICITATION_TTL_S
    ]
    for elicitation_id in expired:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)
    overflow = (
        len(_harness_pre_resolved_elicitations)
        - _facade._HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES
    )
    if overflow <= 0:
        return
    oldest = sorted(
        _harness_pre_resolved_elicitations.items(),
        key=lambda item: item[1].created_at,
    )[:overflow]
    for elicitation_id, _tombstone in oldest:
        _harness_pre_resolved_elicitations.pop(elicitation_id, None)


def _signal_harness_elicitation_resolved_by_id(
    session_id: str,
    elicitation_id: str,
) -> None:
    """
    Resolve or pre-resolve one parked harness elicitation by id.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Harness elicitation id, e.g.
        ``"elicit_codex_abc123"``.
    :returns: None.
    :raises OmnigentError: If the id is malformed or belongs to a
        different session.
    """
    if not elicitation_id:
        raise OmnigentError(
            "external_elicitation_resolved requires data.elicitation_id.",
            code=ErrorCode.INVALID_INPUT,
        )
    owner = _harness_elicitation_owners.get(elicitation_id)
    if owner is not None and owner != session_id:
        raise OmnigentError(
            "Elicitation does not belong to this session.",
            code=ErrorCode.INVALID_INPUT,
        )
    _prune_pre_resolved_harness_elicitations()
    parked = _harness_parked_elicitations.get(elicitation_id)
    if parked is None:
        _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
            session_id=session_id,
            created_at=time.time(),
        )
        _prune_pre_resolved_harness_elicitations()
        return
    parked.resolved_elsewhere.set()


def _format_sse(event_type: str, data: dict[str, Any]) -> str:
    """
    Format an SSE event string for the wire.

    :param event_type: SSE event name, e.g.
        ``"response.output_text.delta"``.
    :param data: The event payload dict.
    :returns: A formatted SSE message string ending in two newlines.
    """
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _permission_level_from_grants(
    user_id: str | None,
    grants: list[SessionPermission],
    is_admin: bool,
) -> int | None:
    """
    Derive a user's permission level from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers._get_permission_level_sync`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param user_id: The authenticated user, or ``None`` for unauthenticated
        requests, e.g. ``"alice@example.com"``.
    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :param is_admin: Whether the user holds the admin flag.  Pass the result
        of a single ``permission_store.is_admin(user_id)`` call made once
        for the whole page rather than repeating it per session.
    :returns: Numeric level (1–4), or ``None`` when permissions are disabled
        or the user is unauthenticated.
    """
    if user_id is None:
        return None
    if is_admin:
        return LEVEL_OWNER
    user_grant = next((g for g in grants if g.user_id == user_id), None)
    if user_grant is not None:
        return user_grant.level
    public_grant = next((g for g in grants if g.user_id == RESERVED_USER_PUBLIC), None)
    if public_grant is not None:
        return public_grant.level
    return None


def _owner_from_grants(grants: list[SessionPermission]) -> str | None:
    """
    Find the session owner from a pre-fetched list of grants.

    Mirrors :func:`omnigent.server.routes._auth_helpers.get_session_owner_id`
    but operates on grants already held in memory so callers can batch the
    permission-store query across many sessions at once.

    :param grants: All grants for the session, as returned by
        ``permission_store.list_for_sessions()[conv_id]``.
    :returns: The ``user_id`` of the first grant whose level is at least
        :data:`LEVEL_OWNER`, or ``None`` if no such grant exists.
    """
    return next((g.user_id for g in grants if g.level >= LEVEL_OWNER), None)


#: Awaited with the session id and the failure's public message when a turn fails before
#: reaching a runner (:func:`_record_turn_failure`), so a feature can settle what waited for it.
ON_TURN_FAILED: dict[str, Callable[[str, str], Awaitable[None]]] = {}


def _session_status_from_cache(
    conversation_id: str,
    db_status: str | None = None,
) -> Literal["idle", "running", "failed"]:
    """
    Map the relay-fed status cache value to a list-item status.

    The cache stores the fine-grained relay status (``"running"``,
    ``"waiting"``, ``"failed"``, ``"idle"``); the list-item shape
    collapses ``"running"``/``"waiting"`` to ``"running"``. A cache
    miss falls back to *db_status* — the row value the tunnel-holding
    replica persisted (``omnigent_conversation_metadata.live_status``) — so a replica
    that does NOT hold this session's runner tunnel still serves the
    real status. No cache entry and no row value presents as ``"idle"``.

    :param conversation_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param db_status: ``Conversation.live_status`` when the caller has
        the row, else ``None``.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"``.
    """
    cached = _session_status_cache.get(conversation_id)
    if cached is None:
        cached = db_status
    if cached in ("running", "waiting"):
        return "running"
    if cached == "failed":
        return "failed"
    return "idle"


def _session_status_with_child_rollup(
    conversation_id: str,
    child_session_ids: list[str],
    db_status: str | None = None,
) -> Literal["idle", "running", "failed"]:
    """
    Map a session's cached status plus direct child activity to list status.

    A parent session should read as ``"running"`` in the sidebar while any
    direct sub-agent child is still ``"running"`` or ``"waiting"``, even if
    the parent runner has already gone idle. This keeps every sidebar row
    honest without mounting a child-session query for each row.

    :param conversation_id: Parent session/conversation identifier,
        e.g. ``"conv_parent123"``.
    :param child_session_ids: Direct sub-agent child conversation ids,
        e.g. ``["conv_child1", "conv_child2"]``.
    :param db_status: The row's persisted ``live_status``, used when the
        local cache has no entry (this replica doesn't hold the runner
        tunnel). The child rollup below stays cache-only — a wrong-pod
        miss there just skips the parent's roll-up spinner, best-effort.
    :returns: One of ``"idle"``, ``"running"``, ``"failed"`` for the
        session-list row.
    """
    own_status = _session_status_from_cache(conversation_id, db_status)
    if own_status == "running":
        return "running"
    # Background shells outliving a turn do NOT make the row running: the
    # session takes a new message immediately, and the tally only refreshes on
    # the next ``Stop`` hook, so a spinner keyed off it can outlive the shells.
    # The in-chat indicator still reports them from the count.
    if any(
        _session_status_cache.get(child_id) in ("running", "waiting")
        for child_id in child_session_ids
    ):
        return "running"
    return own_status


async def _collect_descendant_conversation_ids(
    conversation_store: ConversationStore,
    root_id: str,
) -> list[str]:
    """
    Return every sub-agent descendant of ``root_id``, at any depth.

    Walks the tree one level at a time (child, grandchild, and so on),
    batching each level into a single ``list_child_conversation_ids_by_parent``
    call so an N-level tree costs N queries rather than one per node.

    :param conversation_store: Store for child-id lookup.
    :param root_id: Root session/conversation identifier.
    :returns: Descendant ids in breadth-first order. Empty if ``root_id``
        has no sub-agent descendants.
    """
    descendant_ids: list[str] = []
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        child_ids_map = await asyncio.to_thread(
            conversation_store.list_child_conversation_ids_by_parent,
            frontier,
        )
        next_frontier: list[str] = []
        for parent_id in frontier:
            for child_id in child_ids_map.get(parent_id, []):
                if child_id not in seen:
                    seen.add(child_id)
                    descendant_ids.append(child_id)
                    next_frontier.append(child_id)
        frontier = next_frontier
    return descendant_ids


@dataclass(frozen=True)
class SessionLiveness:
    """
    The two honest liveness signals for a single session.

    Returned (keyed by session id) by the server's
    ``_bulk_session_liveness`` / ``_session_liveness`` lookups and
    consumed by the list-item builder, the ``WS /v1/sessions/updates``
    stream, the single-session ``SessionResponse`` snapshot, and
    ``GET /health``. Splitting the old single conflated boolean into
    two fields lets the open-session view distinguish "runner stopped
    but host can relaunch — just send a message" from "host offline —
    reconnect / fork".

    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session. This
        is the sole reachability signal: it does **not** fold in
        host-relaunch optimism (a dead runner on a live host reads
        ``False`` here, not ``True``). A session with no runner
        binding (in-process executor / not yet dispatched) reads
        ``True``.
    :param host_online: Whether the session's host tunnel is live
        (status online and fresh within ``HOST_LIVENESS_TTL_S``).
        ``True`` when the session's ``host_id`` is in the online-hosts
        set, ``False`` when a ``host_id`` is set but not online, and
        ``None`` when the session has no ``host_id`` (CLI / local).
        Used only to choose what the open view shows when
        ``runner_online`` is ``False``; never participates in the
        reachability decision.
    :param host_version: Version string from the bound host's
        ``host.hello`` frame, e.g. ``"0.1.0"`` — surfaced in the
        session info popover. ``None`` when the session has no host
        binding, the host is offline, or its version isn't resolvable
        on this replica (the version lives in the in-memory host
        registry, not the hosts table, so a host connected to another
        replica reads ``None`` here).
    """

    runner_online: bool
    host_online: bool | None
    host_version: str | None = None


async def _apply_liveness_to_items(
    items: list[SessionListItem],
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
) -> None:
    """
    Attach runner + host liveness to session-list items when a lookup is
    wired.

    Both ``GET /v1/sessions`` and ``WS /v1/sessions/updates`` use this so
    HTTP reconciliation preserves the same ``runner_online`` /
    ``host_online`` fields that push frames patch into the web cache.

    :param items: Session-list rows to annotate.
    :param liveness_lookup: Bulk liveness lookup from session id to a
        :class:`SessionLiveness` pair, e.g.
        ``{"conv_abc123": SessionLiveness(runner_online=True,
        host_online=None)}``. ``None`` means this server cannot compute
        liveness for list rows, in which case both fields are left
        ``None``.
    :returns: ``None``. Mutates ``items`` in place.
    """
    if liveness_lookup is None or not items:
        return
    liveness = await asyncio.to_thread(liveness_lookup, [item.id for item in items])
    for item in items:
        result = liveness[item.id]
        item.runner_online = result.runner_online
        item.host_online = result.host_online
        # A dead runner's parked prompts died with it, but the persisted
        # pending count has no crash-time writer (a runner/host/replica that
        # dies without a graceful resolve never decrements the row) — so an
        # offline runner reads as zero pending rather than lighting a phantom
        # inbox badge over an empty prompt list. Reconciled durably when the
        # runner reconnects (see ``_on_runner_connect``'s pending resync).
        if not result.runner_online:
            item.pending_elicitations_count = 0


def _targeted_elicitation_event(
    event: dict[str, Any],
    *,
    target_session_id: str,
) -> dict[str, Any]:
    """
    Return an elicitation event annotated with its resolution target.

    Child-session elicitations can be mirrored into an ancestor's
    chat stream. The mirrored card is rendered in the ancestor
    conversation, but the harness Future still belongs to the child.
    ``target_session_id`` tells clients which session's resolve URL
    should receive the verdict.

    :param event: Original ``response.elicitation_request`` event,
        e.g. ``{"type": "response.elicitation_request",
        "elicitation_id": "elicit_abc", "params": {...}}``.
    :param target_session_id: Session that owns the parked
        elicitation, e.g. ``"conv_child123"``.
    :returns: A shallow event copy with a copied ``params`` dict
        carrying ``target_session_id``.
    """
    mirrored = dict(event)
    params = event.get("params")
    if isinstance(params, dict):
        mirrored["params"] = {**params, "target_session_id": target_session_id}
    else:
        mirrored["params"] = {"target_session_id": target_session_id}
    return mirrored


def _ancestor_session_ids(
    conv_store: ConversationStore,
    session_id: str,
) -> list[str]:
    """
    Return ancestor session ids for a session, nearest parent first.

    :param conv_store: Store used to read conversation parent links.
    :param session_id: Session to walk upward from, e.g.
        ``"conv_child123"``.
    :returns: Ancestor ids in parent-to-root order. Empty when the
        session is top-level or missing.
    """
    ancestors: list[str] = []
    seen = {session_id}
    current = conv_store.get_conversation(session_id)
    while current is not None and current.parent_conversation_id is not None:
        parent_id = current.parent_conversation_id
        if parent_id in seen:
            break
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = conv_store.get_conversation(parent_id)
    return ancestors


def _publish_elicitation_request_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    event: dict[str, Any],
) -> None:
    """
    Mirror a child elicitation request into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param event: Original ``response.elicitation_request`` event.
    """
    mirrored = _targeted_elicitation_event(event, target_session_id=session_id)
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        session_stream.publish(ancestor_id, mirrored)


def _publish_elicitation_resolved_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    elicitation_id: str,
    action: str | None = None,
    reason: str | None = None,
) -> None:
    """
    Mirror an elicitation-resolved event into each ancestor stream.

    :param conv_store: Store used to discover ancestor sessions.
    :param session_id: Child session that owns the elicitation,
        e.g. ``"conv_child123"``.
    :param elicitation_id: Elicitation correlation id, e.g.
        ``"elicit_abc123"``.
    :param action: Optional MCP verdict carried through to the
        mirrors; see :func:`_publish_elicitation_resolved`.
    :param reason: Optional no-verdict reason carried through to the
        mirrors; see :func:`_publish_elicitation_resolved`.
    """
    for ancestor_id in _ancestor_session_ids(conv_store, session_id):
        _publish_elicitation_resolved(ancestor_id, elicitation_id, action=action, reason=reason)


@restart_on_stale_cursor
def _descendant_sessions(
    conv_store: ConversationStore,
    session_id: str,
) -> list[Conversation]:
    """
    Return descendant sub-agent conversations for a session.

    :param conv_store: Store used to list conversations.
    :param session_id: Ancestor session id, e.g. ``"conv_root123"``.
    :returns: Sub-agent conversations below ``session_id``. Empty
        for sessions with no descendants.
    """
    descendants: list[Conversation] = []
    queue: deque[str] = deque([session_id])
    seen = {session_id}
    while queue:
        parent_id = queue.popleft()
        after: str | None = None
        while True:
            page = conv_store.list_conversations(
                kind="sub_agent",
                parent_conversation_id=parent_id,
                limit=100,
                after=after,
            )
            for child in page.data:
                if child.id in seen:
                    continue
                seen.add(child.id)
                descendants.append(child)
                queue.append(child.id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
    return descendants


def _pending_elicitation_snapshot_for_session(
    conv_store: ConversationStore,
    conv: Conversation,
) -> list[dict[str, Any]]:
    """
    Return pending elicitation events visible from a session snapshot.

    The current session's own outstanding prompts are returned first.
    Pending prompts from descendant sub-agents are appended with
    ``params.target_session_id`` so a cold-loaded ancestor chat can
    render and resolve child approvals.
    Duplicate ids are skipped because live mirroring also records the
    ancestor copy in the in-memory index.

    The descendant walk costs one ``list_conversations`` query per
    session in the tree, so it is skipped entirely unless some session
    other than ``conv`` has an outstanding prompt in the in-memory
    index (the common case is none anywhere).

    :param conv_store: Store used to list descendant sub-agents.
    :param conv: Session conversation being snapshotted.
    :returns: Pending elicitation event dicts suitable for
        :class:`SessionResponse.pending_elicitations`.
    """
    events = pending_elicitations.snapshot_for(conv.id)
    if not (set(pending_elicitations.pending_session_ids()) - {conv.id}):
        return events
    seen = {
        event.get("elicitation_id")
        for event in events
        if isinstance(event.get("elicitation_id"), str)
    }
    for child in _descendant_sessions(conv_store, conv.id):
        for event in pending_elicitations.snapshot_for(child.id):
            elicitation_id = event.get("elicitation_id")
            if isinstance(elicitation_id, str) and elicitation_id in seen:
                continue
            if isinstance(elicitation_id, str):
                seen.add(elicitation_id)
            events.append(_targeted_elicitation_event(event, target_session_id=child.id))
    return events


def _publish_input_consumed(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """
    Publish a ``session.input.consumed`` event for a just-persisted
    conversation item.

    Mirrors the wire shape consumers depend on for rendering the
    input (user message bubble, tool-result block, etc.) at the
    moment of acceptance.

    :param session_id: The session/conversation identifier whose
        stream should receive the event.
    :param item: The persisted :class:`ConversationItem` carrying
        the canonical ``id`` / ``type`` / ``data`` fields.
    :param cleared_pending_id: When this message drained a
        :mod:`omnigent.runtime.pending_inputs` entry (native-terminal
        web message mirrored back from the transcript), that entry's
        id, e.g. ``"pending_a1b2c3"`` — so clients drop the optimistic
        bubble by id. ``None`` when nothing was drained.

    Hidden context items (``is_meta``, e.g. injected skill text or a
    Claude background-task notification) are published too, flagged in
    ``data.is_meta``: subscribers hide or re-label them, and the web UI
    shows the task notification as a system marker so the turn Claude
    resumes on it starts a new bubble.
    """
    event = SessionInputConsumedEvent(
        type="session.input.consumed",
        data=SessionInputConsumedPayload(
            item_id=item.id,
            type=item.type,
            data=item.data.model_dump() if item.data is not None else {},
            created_by=item.created_by,
            cleared_pending_id=cleared_pending_id,
        ),
    )
    session_stream.publish(session_id, event.model_dump())


# Wall-clock start of each session's in-flight compaction. A long compaction
# re-announces in_progress on every status poll; carrying one stable
# started_at lets clients anchor their elapsed counter to the true start,
# even across a page reload (the live stream has no replay).
_compaction_started_at: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()


def _publish_compaction_in_progress(session_id: str) -> None:
    """
    Publish the standard compaction progress event to a session stream.

    Repeated calls while the same compaction runs reuse the ``started_at``
    recorded on the first call; ``completed``/``failed`` clear it so the
    next compaction starts a fresh clock.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    started_at = _compaction_started_at.setdefault(session_id, int(time.time()))
    session_stream.publish(
        session_id,
        {"type": "response.compaction.in_progress", "started_at": started_at},
    )


def _publish_compaction_completed(session_id: str, total_tokens: int | None) -> None:
    """
    Publish the compaction-finished event to a session stream.

    Emitted after :func:`compact_conversation_now` returns
    successfully. Clients that rendered a spinner on the
    ``response.compaction.in_progress`` event should upgrade it to
    the permanent "Conversation compacted" marker on this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param total_tokens: Tiktoken estimate of the post-compaction
        context size, e.g. ``8421``. ``None`` when unavailable.
    """
    _compaction_started_at.pop(session_id, None)
    payload: dict[str, object] = {"type": "response.compaction.completed"}
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    session_stream.publish(session_id, payload)


def _publish_compaction_failed(session_id: str) -> None:
    """
    Publish the compaction-failed event to a session stream.

    Emitted when :func:`compact_conversation_now` raises. Clients
    that rendered a spinner on the
    ``response.compaction.in_progress`` event should dismiss it
    without leaving a permanent marker — the conversation history
    was not modified.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    _compaction_started_at.pop(session_id, None)
    session_stream.publish(session_id, {"type": "response.compaction.failed"})


def _publish_external_assistant_message(
    session_id: str,
    item: ConversationItem,
    *,
    response_id: str,
    agent_name: str,
) -> None:
    """
    Broadcast an assistant message appended outside the task runtime.

    Terminal-backed integrations such as native Claude produce output
    in a live terminal first, then mirror the semantic text into AP.
    There is no ``agent_task`` to watch, so this helper publishes the
    completed output item directly. The browser reducer renders the
    persisted message content from ``response.output_item.done``;
    emitting synthetic text deltas here would duplicate the same
    transcript item when the snapshot path also sees it.

    :param session_id: Session/conversation identifier.
    :param item: Persisted assistant message item.
    :param response_id: Legacy endpoint response id. The persisted
        item already carries this value, so the publisher does not
        need it separately.
    :param agent_name: Legacy endpoint agent/model name. The
        persisted item already carries this value.
    :returns: None.
    """
    del response_id, agent_name
    api_item = item.to_api_dict()
    event = OutputItemDoneEvent(type="response.output_item.done", item=api_item)
    session_stream.publish(session_id, event.model_dump())


def _resolve_llm_model(
    conv: Conversation | None,
    *,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> str | None:
    """
    Resolve the LLM model identifier from a conversation's agent spec.

    Uses injected agent dependencies when available, falling back to the
    runtime globals for legacy callers. Returns ``None`` when the conversation
    has no agent binding or the spec cannot be loaded.

    :param conv: The conversation entity, or ``None``.
    :param agent_store: Optional store for resolving the bound agent.
    :param agent_cache: Optional cache for loading the bound agent spec.
    :returns: Model string (e.g. ``"databricks-gpt-5-5"``), or
        ``None`` when unavailable.
    """
    if conv is None or conv.agent_id is None:
        return None
    try:
        # Import ``get_agent_cache`` from the runtime at call time so a test
        # patching ``omnigent.runtime.get_agent_cache`` is honored (the
        # module-level name is a facade proxy that bypasses that patch).
        from omnigent.runtime import get_agent_cache

        if agent_store is None:
            from omnigent.runtime._globals import _agent_store

            agent_store = _agent_store
        if agent_store is None:
            return None
        if agent_cache is None:
            agent_cache = get_agent_cache()
        agent = agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        return loaded.spec.llm.model if loaded.spec.llm else None
    # UUID bind failures are wrapped by SQLAlchemy; do not hide broader DB errors.
    except (
        KeyError,
        AttributeError,
        ValueError,
        ImportError,
        OSError,
        RuntimeError,
        StatementError,
        # A corrupt/unparseable cached bundle (e.g. a spec-load race) must
        # degrade the same as a missing agent, not crash the caller.
        OmnigentError,
    ):
        # ``RuntimeError`` covers ``get_agent_cache()`` before the runtime is
        # initialized: this is a best-effort display resolver (now also called
        # on native cost-only broadcasts), so an uninitialized runtime must
        # degrade to "model unknown" — the cost still records, just unattributed.
        return None


def _resolve_harness(*args: Any, **kwargs: Any) -> str | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._resolve_harness(*args, **kwargs)


def _resolve_harness_impl(
    conv: Conversation | None,
    *,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> str | None:
    """
    Resolve the canonical harness for a conversation's bound agent.

    Mirrors :func:`_resolve_llm_model`: loads the parsed spec via the agent
    cache and returns the executor's harness
    (``executor.config["harness"]``, else ``executor.type``), canonicalized.
    Surfacing this on :class:`SessionResponse` lets the REPL render the
    active credential for the correct provider *family* — anthropic for
    claude-sdk, openai for pi — instead of guessing the
    family from the model string (which is wrong when the agent declares no
    model, e.g. a generic-provider launcher).

    :param conv: The conversation entity, or ``None``.
    :param agent_store: Optional store for resolving the bound agent.
    :param agent_cache: Optional cache for loading the bound agent spec.
    :returns: The canonical harness (e.g. ``"pi"`` or
        ``"claude-sdk"``), or ``None`` when unavailable.
    """
    if conv is None:
        return None
    # A persisted per-session override (validated + canonicalized at
    # create) wins over the spec's declared harness, so the snapshot
    # reports what the runner actually spawns.
    if conv.harness_override:
        return conv.harness_override
    if conv.agent_id is None:
        return None
    try:
        from omnigent.harness_aliases import canonicalize_harness
        from omnigent.runtime import get_agent_cache

        if agent_store is None:
            from omnigent.runtime._globals import _agent_store

            agent_store = _agent_store
        if agent_store is None:
            return None
        if agent_cache is None:
            agent_cache = get_agent_cache()
        agent = agent_store.get(conv.agent_id)
        if agent is None:
            return None
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
        executor = loaded.spec.executor
        # For a bundled-agent head sub-agent, report the HEAD's own harness,
        # not the bundle brain's — `harness` is this session's provider family
        # (a gpt head runs codex, not the claude-sdk brain). Falls back to the
        # brain harness when the head declares none or can't be matched.
        if conv.sub_agent_name:
            from omnigent.runtime.workflow import _find_spec_by_name

            sub = _find_spec_by_name(loaded.spec, conv.sub_agent_name)
            if sub is not None:
                executor = sub.executor
        harness = (
            executor.config.get("harness")
            or loaded.spec.executor.config.get("harness")
            or executor.type
        )
        return canonicalize_harness(harness) or harness
    # UUID bind failures are wrapped by SQLAlchemy; do not hide broader DB errors.
    except (
        KeyError,
        AttributeError,
        ValueError,
        ImportError,
        OSError,
        RuntimeError,
        StatementError,
        # A corrupt/unparseable cached bundle (e.g. a spec-load race) must
        # degrade the same as a missing agent, not crash the caller.
        OmnigentError,
    ):
        return None


def _validated_harness_override(value: str | None, agent: Agent) -> str | None:
    """
    Validate + canonicalize a session-create ``harness_override``.

    Mirrors the CLI's ``--harness`` rules (``_apply_harness_override_to_executor``
    in ``omnigent/chat.py``): the canonical name must be a known bundle
    harness, and the bound agent must be an ``executor.type: omnigent``
    spec — other executor types have no ``config.harness``, so an
    override there would be a silent no-op.

    :param value: The raw override from the request body, e.g. ``"pi"``.
        ``None`` means no override.
    :param agent: The bound agent row (already fetched by the caller).
    :returns: The canonical harness id.
    :raises OmnigentError: ``invalid_input`` for an unknown harness, a
        non-omnigent executor type, or an unloadable agent bundle.
    """
    if value is None:
        return None
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.runtime import get_agent_cache
    from omnigent.spec._omnigent_compat import (
        OMNIGENT_EXECUTOR_TYPE,
        OMNIGENT_HARNESSES,
    )

    canonical = canonicalize_harness(value) or value
    if canonical not in OMNIGENT_HARNESSES:
        raise OmnigentError(
            f"invalid harness_override: must be one of "
            f"{sorted(OMNIGENT_HARNESSES)}, got {value!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError) as exc:
        raise OmnigentError(
            f"harness_override requires a loadable agent spec; "
            f"agent {agent.name!r} failed to load: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    executor_type = loaded.spec.executor.type
    if executor_type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"harness_override only applies to executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r} agents; agent {agent.name!r} "
            f"declares executor.type {executor_type!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return canonical


def _validated_harness_override_executor_type(agent: Agent) -> None:
    """Validate that *agent* is an ``executor.type: omnigent`` spec.

    Used by the ``"auto"`` harness path to enforce the same executor-type
    gate as :func:`_validated_harness_override` without requiring a concrete
    harness name (the real harness is resolved at first-message time).

    :raises OmnigentError: ``invalid_input`` when the agent is not an
        omnigent executor type or the bundle cannot be loaded.
    """
    from omnigent.runtime import get_agent_cache
    from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE

    try:
        loaded = get_agent_cache().load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError) as exc:
        raise OmnigentError(
            f"harness_override 'auto' requires a loadable agent spec; "
            f"agent {agent.name!r} failed to load: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    executor_type = loaded.spec.executor.type
    if executor_type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"harness_override 'auto' only applies to executor.type "
            f"{OMNIGENT_EXECUTOR_TYPE!r} agents; agent {agent.name!r} "
            f"declares executor.type {executor_type!r}",
            code=ErrorCode.INVALID_INPUT,
        )


#: ``executor.config`` key by which a spec hands its brain harness to Smart
#: Routing. ``auto`` is the only accepted value.
SMART_ROUTING_HARNESS_CONFIG_KEY = "smart_routing_harness"

#: The ``harness_override`` sentinel meaning "the router picks the harness".
AUTO_HARNESS_SENTINEL = "auto"


def _validated_spec_smart_routing_harness(spec: AgentSpec) -> str | None:
    """
    Read a spec's opt-in for routing its own brain harness.

    A spec that pins ``executor.config.harness`` also pins the family every
    sub-agent is routed within, so a cross-family sub-agent cannot stay on its
    declared harness. ``smart_routing_harness: auto`` lets such a spec keep its
    pin for a normal session and hand the harness to the router when Smart
    Routing is on.

    Validated on the same rules as :func:`_validated_harness_override`: the
    value must be the ``"auto"`` sentinel, and only an ``executor.type:
    omnigent`` spec has a swappable brain harness to give away.

    :param spec: The bound agent's parsed spec.
    :returns: ``"auto"`` when the spec opts in, else ``None``.
    :raises OmnigentError: ``invalid_input`` for any other value, or for the
        key on a non-omnigent executor type.
    """
    from omnigent.spec._omnigent_compat import OMNIGENT_EXECUTOR_TYPE

    value = spec.executor.config.get(SMART_ROUTING_HARNESS_CONFIG_KEY)
    if value is None:
        return None
    key = f"executor.config.{SMART_ROUTING_HARNESS_CONFIG_KEY}"
    if value != AUTO_HARNESS_SENTINEL:
        raise OmnigentError(
            f"invalid {key}: must be {AUTO_HARNESS_SENTINEL!r}, got {value!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    if spec.executor.type != OMNIGENT_EXECUTOR_TYPE:
        raise OmnigentError(
            f"{key} only applies to executor.type {OMNIGENT_EXECUTOR_TYPE!r} "
            f"agents; this spec declares executor.type {spec.executor.type!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return AUTO_HARNESS_SENTINEL


def _utc_day(epoch_seconds: int) -> str:
    """
    Convert a Unix epoch timestamp to its UTC calendar day.

    :param epoch_seconds: Unix epoch seconds, e.g. ``1749081600``.
    :returns: The UTC date as ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
    """
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()


def _record_daily_cost(
    conv: Conversation | None,
    delta_usd: float,
    conversation_store: ConversationStore,
) -> None:
    """
    Add a turn's LLM cost to the session owner's daily rollup.

    Record every priced turn, including sessions without a budget policy.
    Bucket spending by the current UTC day; nonpositive deltas and sessions
    without a resolvable owner are no-ops.

    Read the owner's username and generation together, falling back to the
    root session for a sub-agent without its own grant. Retain that snapshot
    through the locked write so deletion or re-registration cannot transfer
    old work's spending to a new account.

    :param conv: The conversation row for the session, or ``None``
        (a no-op — no owner to attribute to).
    :param delta_usd: The turn's cost in USD; ``<= 0`` is a no-op.
    :param conversation_store: Store for the owner lookup and the
        daily-cost UPSERT.
    """
    if conv is None or delta_usd <= 0:
        return
    owner = conversation_store.get_session_owner_authority(conv.id)
    if owner is None and conv.root_conversation_id != conv.id:
        # Sub-agent: no direct owner grant — fall back to the root session's
        # owner so sub-agent spend is attributed rather than silently dropped.
        owner = conversation_store.get_session_owner_authority(conv.root_conversation_id)
    if owner is None:
        return
    from omnigent.db.account_authority import target_account_scope
    from omnigent.db.utils import now_epoch

    with target_account_scope(owner.user_id, owner.generation):
        conversation_store.add_daily_cost(owner.user_id, _utc_day(now_epoch()), delta_usd)


def _priced_cost_for_display(usage: dict[str, Any]) -> float | None:
    """
    Extract ``total_cost_usd`` for client display, or ``None`` when unpriced.

    The key is present only when a turn was priced, so its absence ("—" in
    the UI) is distinct from a priced ``$0.00``. The cost-budget policy is
    unaffected — it reads the value with a ``0.0`` default.

    :param usage: A conversation's ``session_usage`` dict, e.g.
        ``{"input_tokens": 1200, "total_cost_usd": 0.42}`` (priced) or
        ``{"input_tokens": 1200}`` (unpriced — no cost key).
    :returns: The cumulative cost in USD when priced, else ``None``.
    """
    if "total_cost_usd" not in usage:
        return None
    try:
        return float(usage["total_cost_usd"])
    except (TypeError, ValueError):
        # Defensive: a malformed persisted value must not break the
        # snapshot / SSE emit. Treat it as unpriced.
        return None


def _model_usage_bucket(usage: dict[str, Any], model: str) -> dict[str, float]:
    """
    Get-or-create the per-model usage sub-bucket inside ``usage["by_model"]``.

    The nested ``by_model`` map attributes token/cost usage to the specific
    LLM that produced it, keyed on the raw harness-reported model id (faithful
    and simplest — alias normalization is intentionally deferred). This mutates
    ``usage`` in place, creating ``by_model`` and the per-model dict on first
    use, and returns the model's bucket for the caller to increment / set.

    :param usage: The conversation's mutable ``session_usage`` dict.
    :param model: The raw harness model id, e.g. ``"claude-sonnet-4-6"`` or
        ``"databricks-gpt-5-5"``.
    :returns: The mutable per-model bucket, e.g. ``{"input_tokens": 1200}``.
    """
    raw_by_model = usage.setdefault("by_model", {})
    if not isinstance(raw_by_model, dict):
        raw_by_model = {}
        usage["by_model"] = raw_by_model
    by_model = cast(dict[str, Any], raw_by_model)
    raw_bucket = by_model.setdefault(model, {})
    if not isinstance(raw_bucket, dict):
        raw_bucket = {}
        by_model[model] = raw_bucket
    return cast(dict[str, float], raw_bucket)


def _add_model_usage_delta(
    bucket: dict[str, float],
    token_deltas: dict[str, int],
    cost_delta: float | None,
) -> None:
    """
    Add one turn's per-model token/cost deltas into a model bucket (ADD).

    Mirrors the flat-counter increments in :func:`_accumulate_session_usage`
    so the per-model totals stay consistent with the flat totals: every flat
    increment is matched by an increment to exactly one model bucket, so the
    sum of per-model buckets equals the flat total. ``cost_delta`` is added
    only when the turn was priced (``None`` otherwise), preserving the
    "priced ⟺ ``total_cost_usd`` key present" contract at the per-model level.

    :param bucket: The model's mutable bucket from :func:`_model_usage_bucket`.
    :param token_deltas: This turn's per-bucket token counts to add, keyed by
        the same names as :data:`_TOKEN_BREAKDOWN_KEYS`, e.g.
        ``{"input_tokens": 1200, "output_tokens": 340, ...}``.
    :param cost_delta: This turn's priced cost in USD to add, or ``None`` when
        the turn was unpriced (the model's cost key stays absent).
    """
    for key, delta in token_deltas.items():
        bucket[key] = bucket.get(key, 0) + delta
    if cost_delta is not None:
        bucket["total_cost_usd"] = bucket.get("total_cost_usd", 0.0) + cost_delta


def _usage_by_model_for_display(usage: dict[str, Any]) -> dict[str, ModelUsage] | None:
    """
    Project the nested ``by_model`` usage map into typed :class:`ModelUsage`.

    Companion to :func:`_token_breakdown_for_display` for the per-model view:
    reads ``usage["by_model"]`` (the subtree-summed map from
    :func:`load_session_usage`) and builds a ``{model_id: ModelUsage}`` dict
    for the API. Token buckets are coerced to ``int`` and ``total_cost_usd``
    to ``float``; an absent bucket stays ``None`` on the model (so a model
    that was never priced has no cost), and malformed values are skipped.

    :param usage: A subtree-summed usage dict, e.g.
        ``{"input_tokens": 1500, "by_model": {"claude-sonnet-4-6":
        {"input_tokens": 1500, "total_cost_usd": 0.42}}}``.
    :returns: The per-model map, or ``None`` when no per-model usage is
        present (so ``exclude_none`` omits the field entirely).
    """
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict) or not by_model:
        return None
    result: dict[str, ModelUsage] = {}
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        fields: dict[str, Any] = {}
        for key in _MODEL_TOKEN_KEYS:
            value = bucket.get(key)
            if value is None:
                continue
            try:
                fields[key] = int(value)
            except (TypeError, ValueError):
                continue
        cost = _priced_cost_for_display(bucket)
        if cost is not None:
            fields["total_cost_usd"] = cost
        result[model] = ModelUsage(**fields)
    return result or None


def _coerce_cumulative_field(
    data: dict[str, Any],
    key: str,
    *,
    numeric: bool,
) -> float | int | None:
    """
    Read and validate an optional cumulative usage field from event data.

    :param data: The ``external_session_usage`` event ``data`` dict.
    :param key: Field name, e.g. ``"cumulative_input_tokens"``.
    :param numeric: When ``True`` accept any finite non-negative number
        (cost); when ``False`` require a finite non-negative int (token
        counts).
    :returns: The validated value, or ``None`` when the key is absent.
    :raises OmnigentError: When present but the wrong type / negative /
        non-finite (``NaN`` / ``inf`` — which the monotonic ``max(old,
        new)`` clamp downstream would otherwise latch permanently).
    """
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OmnigentError(
            f"external_session_usage data.{key} must be a finite non-negative "
            f"{'number' if numeric else 'int'}",
            code=ErrorCode.INVALID_INPUT,
        )
    if (not numeric and not isinstance(value, int)) or not math.isfinite(value) or value < 0:
        raise OmnigentError(
            f"external_session_usage data.{key} must be a finite non-negative "
            f"{'number' if numeric else 'int'}",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


async def _persist_external_model_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast the model the harness reports it is running.

    Mirrors a harness-side model report — the launch's own model, or a
    ``/model`` change made inside the pane — onto the Omnigent session:
    writes ``reported_model`` VERBATIM (the harness's own spelling,
    never collapsed to a picker alias) so the value survives reload,
    and publishes a ``session.model`` SSE event so every surface
    re-renders from it. The user's request (``model_override``) is
    deliberately untouched: requests and reports are separate roles,
    and only reports are ever displayed. Unlike the PATCH path
    (:func:`update_session`), this does NOT forward a ``model_change``
    back to the runner — the terminal is already on the model, so
    re-injecting ``/model`` would loop.

    No-ops (no write, no event) when the reported model already equals
    the persisted ``reported_model`` — the steady state between real
    changes, since forwarders re-observe on every poll.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` (read at the route
        boundary); ``conv.reported_model`` is the dedupe baseline.
    :param body: External model-change event body. ``data.model`` must
        be a non-empty string — the harness's verbatim model, e.g.
        ``"claude-opus-4-8[1m]"`` or ``"gpt-5.6-luna"``.
    :param conversation_store: Store used to upsert ``reported_model``.
    :raises OmnigentError: If ``data.model`` is missing or not a
        non-empty string.
    """
    raw_model = body.data.get("model")
    if not isinstance(raw_model, str) or not raw_model.strip():
        raise OmnigentError(
            "external_model_change requires data.model to be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    model = concrete_reported_model(raw_model)
    if model is not None and conv.inference_snapshot is not None:
        from omnigent.inference_config import binding_for_harness

        snapshot = conv.inference_snapshot
        binding = binding_for_harness(snapshot["runtime_config"], snapshot["harness"])
        allowed = binding.model_allowlist if binding is not None else None
        if allowed is not None and model not in allowed:
            prefixes = {
                "pi-native": ("omnigent/", "omnigent-openai/", "omnigent-completions/"),
            }.get(snapshot["harness"], ())
            model = next(
                (
                    model.removeprefix(p)
                    for p in prefixes
                    if model.startswith(p) and model.removeprefix(p) in allowed
                ),
                model,
            )
    if model is None or conv.reported_model == model:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        reported_model=model,
    )
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())


async def _persist_external_session_title(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a session rename made inside the terminal.

    Mirrors a ``/rename`` typed into a claude-native session's Claude Code
    pane onto the Omnigent session: writes ``title`` so the new name
    survives reload and publishes a ``session.title`` SSE event so the
    web session list updates live.

    The rename is authoritative — an operator typing ``/rename`` is an
    explicit act, so it overwrites whatever title the session currently
    carries, including one set from the web UI. This is why it uses a
    plain ``update_conversation`` rather than the seed-only
    compare-and-swap behind ``POST /sessions/{id}/auto-title``, which
    exists to stop an *automatic* titler from clobbering a human's name.

    No-ops (no write, no event) when the title already matches, so a
    forwarder that re-sends after a cursor rewind, or a rename echoing
    back a name the web UI just set, costs nothing.

    Declined for child sessions, whose titles are structural rather than
    display text: ``sys_session_send`` writes them as ``"<agent>:<label>"``
    and the sub-agent tooling parses them back apart. That also covers the
    legacy ``:closed:`` title marker, which only ever lands on a child row
    (both writers reject a non-sub-agent title).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` (read at the route
        boundary); ``conv.title`` is the dedupe baseline.
    :param body: External title event body. ``data.title`` must be a
        non-empty single-line string, e.g. ``"auth-refactor"``.
    :param conversation_store: Store used to upsert ``title``.
    :raises OmnigentError: If ``data.title`` is not a non-empty single line
        within the user title length limit.
    """
    raw_title = body.data.get("title")
    # Newlines are rejected outright rather than folded into spaces — a
    # multi-line title means the sender is confused, not that it wants one
    # long line. Mirrors ``POST /sessions/{id}/auto-title``.
    if not isinstance(raw_title, str) or "\n" in raw_title or "\r" in raw_title:
        raise OmnigentError(
            "external_session_title requires data.title to be a single-line string",
            code=ErrorCode.INVALID_INPUT,
        )
    title = " ".join(raw_title.split())
    if not title:
        raise OmnigentError(
            "external_session_title requires data.title to be non-empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if len(title) > USER_SESSION_TITLE_MAX_CHARS:
        raise OmnigentError(
            f"external_session_title requires data.title to be at most "
            f"{USER_SESSION_TITLE_MAX_CHARS} characters",
            code=ErrorCode.INVALID_INPUT,
        )
    if conv.parent_conversation_id is not None:
        return
    if conv.title == title:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        title=title,
    )
    event = SessionTitleEvent(
        type="session.title",
        conversation_id=session_id,
        title=title,
    )
    session_stream.publish(session_id, event.model_dump())


def _validate_external_reasoning_effort(body: SessionEventInput) -> str | None:
    """
    Validate a terminal-observed reasoning-effort payload.

    :param body: External effort-change event body. ``data.reasoning_effort``
        must be present and either ``None`` or a supported effort string, e.g.
        ``"medium"``.
    :returns: Normalized effort string, or ``None`` when the terminal cleared
        to its default effort.
    :raises OmnigentError: If the payload is missing or unsupported.
    """
    if "reasoning_effort" not in body.data:
        raise OmnigentError(
            "external_reasoning_effort_change requires data.reasoning_effort",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_effort = body.data["reasoning_effort"]
    if raw_effort is None:
        return None
    if not isinstance(raw_effort, str) or not raw_effort.strip():
        raise OmnigentError(
            "external_reasoning_effort_change requires data.reasoning_effort "
            "to be a non-empty string or null",
            code=ErrorCode.INVALID_INPUT,
        )
    effort = raw_effort.strip()
    try:
        return validate_effort(effort, "session metadata", EFFORT_VALUES)
    except ValueError as exc:
        raise OmnigentError(
            f"invalid reasoning_effort: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


async def _persist_external_reasoning_effort_change(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> None:
    """
    Persist and broadcast a reasoning-effort switch made inside the terminal.

    Mirrors a native-terminal thinking-level change onto the Omnigent session.
    Unlike the public PATCH path, this deliberately does NOT forward an
    ``effort_change`` back to the runner: the terminal is already on that
    effort, so re-injecting it would loop.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id`` at the route boundary.
    :param body: External effort-change event body.
    :param conversation_store: Store used to update ``reasoning_effort``.
    :returns: None.
    """
    effort = _validate_external_reasoning_effort(body)
    if conv.reasoning_effort == effort:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        reasoning_effort=effort,
        _unset_reasoning_effort=effort is None,
    )
    event = SessionReasoningEffortEvent(
        type="session.reasoning_effort",
        conversation_id=session_id,
        reasoning_effort=effort,
    )
    session_stream.publish(session_id, event.model_dump())


#: ``feature name -> hook(session_id, item)``, called with each visible assistant message once it
#: is stored and before its ``message.done`` is published, so what a feature records about it
#: (the outbox row of a proactive message) is there when a client refetches. Hooks may block on
#: the database: they run on a worker thread, never on the event loop, one message of a session
#: at a time in the order stored. A failing hook is logged and never fails the persist.
ON_MESSAGE_STORED: dict[str, Callable[[str, ConversationItem], None]] = {}

#: ``session id -> lock`` that keeps a session's hooks and ``message.done`` in stored order.
_message_stored_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
#: Signals scheduled by a synchronous caller, held until they finish.
_message_stored_tasks: set[asyncio.Task[None]] = set()


def _is_visible_assistant_message(item: ConversationItem) -> bool:
    return (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "assistant"
        and not item.data.is_meta
    )


def _run_message_stored_hooks(session_id: str, item: ConversationItem) -> None:
    """Blocking: every :data:`ON_MESSAGE_STORED` hook for *item*, each failure logged."""
    for hook in list(ON_MESSAGE_STORED.values()):
        try:
            hook(session_id, item)
        except Exception:  # noqa: BLE001 - bookkeeping, never a reason to fail a persist
            _logger.warning("message stored hook failed for %s", session_id, exc_info=True)


async def _signal_message_stored_async(session_id: str, item: ConversationItem) -> None:
    """Tell the session's family stream an assistant message was stored (``message.done``).

    The one publish for it: every path that persists a visible assistant message calls this
    (or :func:`_signal_message_stored`) with the store-assigned item, so the id a client
    refetches by always exists. The hooks run first, off the loop, so ``message.done`` follows
    what they recorded.
    """
    if not _is_visible_assistant_message(item):
        return
    from omnigent.superchat.family.signals import notify_message_done

    if not ON_MESSAGE_STORED:
        notify_message_done(session_id, item.id)
        return
    lock = _message_stored_locks.get(session_id)
    if lock is None:
        lock = _message_stored_locks[session_id] = asyncio.Lock()
    async with lock:
        await asyncio.to_thread(_run_message_stored_hooks, session_id, item)
        notify_message_done(session_id, item.id)


def _signal_message_stored(session_id: str, item: ConversationItem) -> None:
    """:func:`_signal_message_stored_async` for a caller that cannot await.

    On the event loop the signal is scheduled, not run: the hooks still run off the loop and
    before this message's ``message.done`` (which is all a refetch depends on), only after the
    caller's own synchronous publishes, such as ``response.output_item.done`` on the session
    stream, which carries no outbox state. Off the loop (a worker thread) it runs here.
    """
    if not _is_visible_assistant_message(item):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from omnigent.superchat.family.signals import notify_message_done

        _run_message_stored_hooks(session_id, item)
        notify_message_done(session_id, item.id)
        return
    task = loop.create_task(_signal_message_stored_async(session_id, item))
    _message_stored_tasks.add(task)
    task.add_done_callback(_message_stored_tasks.discard)


def _publish_external_conversation_item(
    session_id: str,
    item: ConversationItem,
    cleared_pending_id: str | None = None,
    message_id: str | None = None,
) -> None:
    """
    Broadcast a terminal-observed conversation item.

    User messages use ``session.input.consumed`` so the web UI renders
    them exactly like local/composer messages. Assistant/tool-side
    items use ``response.output_item.done`` because they are already
    completed records from Claude's transcript, not token deltas from
    an active Omnigent task.

    :param session_id: Session/conversation identifier.
    :param item: Persisted conversation item.
    :param cleared_pending_id: For a native user message, the id of the
        optimistic pending-input entry the caller drained for it (so
        clients drop that bubble by id), or ``None``. The drain happens
        at the persist site — see :func:`_persist_external_conversation_item`
        — because it also folds the entry's file blocks into the durable
        item before append.
    :param message_id: Optional live-preview stream finalized by this item.
    :returns: None.
    """
    if item.type == "message" and isinstance(item.data, MessageData):
        if item.data.role == "user":
            _publish_input_consumed(session_id, item, cleared_pending_id=cleared_pending_id)
            return
        if item.data.is_meta:
            # Hidden context on a non-user message has no live rendering
            # path that filters on the flag, so keep it off the stream.
            return
    if (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "assistant"
    ):
        inflight_text.retire_native_previews(session_id)
    _signal_message_stored(session_id, item)
    event = OutputItemDoneEvent(type="response.output_item.done", item=item.to_api_dict())
    payload = event.model_dump()
    if message_id is not None:
        payload["message_id"] = message_id
    session_stream.publish(session_id, payload)


def _publish_external_output_text_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed assistant text delta.

    Terminal-backed integrations can observe streaming output before
    their completed transcript item is available. This publishes the
    standard Responses-style text-delta SSE event without persisting
    anything; the final assistant message is persisted separately when
    the integration posts ``external_conversation_item``.

    The optional ``message_id`` / ``index`` / ``final`` fields are
    carried through when present (native live streaming) and
    omitted otherwise — ``exclude_none`` keeps the wire shape identical
    to in-process task streaming for callers that don't set them.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_TEXT_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or any
        provided ``message_id`` / ``index`` / ``final`` has the wrong
        type.
    """
    delta = body.data.get("delta")
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_output_text_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    message_id = body.data.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        raise OmnigentError(
            "external_output_text_delta data.message_id must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    index = body.data.get("index")
    # ``bool`` is an ``int`` subclass; reject it explicitly so a stray
    # boolean index is a loud error rather than a silent 0/1.
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
        raise OmnigentError(
            "external_output_text_delta data.index must be an integer",
            code=ErrorCode.INVALID_INPUT,
        )
    final = body.data.get("final")
    if final is not None and not isinstance(final, bool):
        raise OmnigentError(
            "external_output_text_delta data.final must be a boolean",
            code=ErrorCode.INVALID_INPUT,
        )
    event = OutputTextDeltaEvent(
        type="response.output_text.delta",
        delta=delta,
        message_id=message_id,
        index=index,
        final=final,
    )
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_external_tool_output_delta(session_id: str, body: SessionEventInput) -> None:
    """Broadcast a terminal-observed function-call output delta.

    :param session_id: Session/conversation identifier.
    :param body: Event body containing string ``call_id`` and ``delta`` values.
    :returns: None.
    :raises OmnigentError: If either required value is missing or not a string.
    """
    call_id = body.data.get("call_id")
    delta = body.data.get("delta")
    if not isinstance(call_id, str) or not call_id:
        raise OmnigentError(
            "external_tool_output_delta requires non-empty string data.call_id",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_tool_output_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    event = ToolOutputDeltaEvent(
        type="response.function_call_output.delta",
        call_id=call_id,
        delta=delta,
    )
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


def _publish_external_output_reasoning_delta(session_id: str, body: SessionEventInput) -> None:
    """
    Broadcast a terminal-observed reasoning (chain-of-thought) delta.

    The reasoning analogue of :func:`_publish_external_output_text_delta`:
    terminal-backed integrations (the codex-native forwarder) observe a
    streaming ``thinking`` block before the completed assistant item exists. This
    publishes the standard reasoning SSE events the SPA already renders —
    ``response.reasoning.started`` once (when ``data.started`` is true, marking a
    new reasoning block) followed by ``response.reasoning_text.delta`` — without
    persisting anything. Reasoning has no completed conversation item; the block
    is finalized when the assistant message is persisted via
    ``external_conversation_item``.

    :param session_id: Session/conversation identifier.
    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_OUTPUT_REASONING_DELTA_TYPE`.
    :returns: None.
    :raises OmnigentError: If ``data.delta`` is not a string, or ``data.started``
        is provided with a non-boolean type.
    """
    delta = body.data.get("delta")
    if not isinstance(delta, str):
        raise OmnigentError(
            "external_output_reasoning_delta requires string data.delta",
            code=ErrorCode.INVALID_INPUT,
        )
    started = body.data.get("started")
    if started is not None and not isinstance(started, bool):
        raise OmnigentError(
            "external_output_reasoning_delta data.started must be a boolean",
            code=ErrorCode.INVALID_INPUT,
        )
    if started:
        session_stream.publish(
            session_id,
            ReasoningStartedEvent(type="response.reasoning.started").model_dump(exclude_none=True),
        )
    event = ReasoningTextDeltaEvent(type="response.reasoning_text.delta", delta=delta)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))


_VALID_ELICITATION_ACTIONS: tuple[str, ...] = ("accept", "decline", "cancel")
# Why a resolved event carries no verdict. ``"unanswered"``: the hook stopped
# waiting (a severed poll never re-parked, or the ask timed out) before anyone
# answered, so the prompt is gone rather than decided.
_VALID_ELICITATION_RESOLVED_REASONS: tuple[str, ...] = ("unanswered",)


def _publish_elicitation_resolved(
    session_id: str,
    elicitation_id: str,
    action: str | None = None,
    reason: str | None = None,
) -> None:
    """
    Universal "approval done" signal — single publish drives both
    sidebar (via :func:`pending_elicitations.record_publish` decrement)
    and the chat-side ``ApprovalCard`` flip on every live subscriber.
    Idempotent on duplicate emissions for the same id.

    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :param elicitation_id: Correlation id, e.g. ``"elicit_abc123"``.
    :param action: Optional MCP verdict (``"accept"``/``"decline"``/
        ``"cancel"``) so downstream consumers — notably the
        sub-agent block notifier's parent resolution notice — can
        state how the gate was answered instead of leaving agents to
        guess. Omitted from the payload when unknown or not one of
        the three MCP actions.
    :param reason: Why there is no verdict, e.g. ``"unanswered"`` when
        the hook stopped waiting before anyone answered, so the card
        can say the prompt expired instead of implying someone resolved
        it. Omitted when unknown, not a recognised reason, or when a
        verdict is present — a verdict and a no-verdict reason are
        mutually exclusive on the wire.
    """
    payload: dict[str, Any] = {
        "type": "response.elicitation_resolved",
        "elicitation_id": elicitation_id,
    }
    if action in _VALID_ELICITATION_ACTIONS:
        payload["action"] = action
    elif reason in _VALID_ELICITATION_RESOLVED_REASONS:
        payload["reason"] = reason
    session_stream.publish(session_id, payload)


async def _forward_approval_to_runner(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
) -> None:
    """
    Forward an approval verdict to the session's bound runner.

    Runner-side elicitations (policy approvals parked in the runner's
    ``_pending_approvals`` dict, scaffold dispatch) resolve when the
    canonical ``approval`` event reaches the runner's ``/events``. The
    server↔runner contract stays the ``approval`` event regardless of
    how the verdict arrived at the server (resolve URL or approval
    event). No-op when no runner is bound (in-process setups). HTTP
    errors are logged, not raised — a dead runner must not fail the
    caller's resolution (the server-side Future was already set).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param data: The approval payload to forward verbatim as the
        event ``data``, e.g. ``{"elicitation_id": "elicit_abc",
        "action": "accept"}``.
    :param runner_router: Router used to resolve the bound runner, or
        ``None`` in in-process setups (forward skipped).
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _APPROVAL_TYPE, "data": data},
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.exception(
            "Approval forward failed for %r",
            session_id,
            extra={"session_id": session_id},
        )


def _parse_external_assistant_message(
    body: SessionEventInput,
) -> tuple[str, str, str]:
    """
    Validate and unpack an external assistant-message event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_ASSISTANT_MESSAGE_TYPE`.
    :returns: ``(agent_name, text, response_id)``.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    agent_name = body.data.get("agent")
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise OmnigentError(
            "external_assistant_message requires data.agent",
            code=ErrorCode.INVALID_INPUT,
        )
    text = body.data.get("text")
    if not isinstance(text, str) or not text:
        raise OmnigentError(
            "external_assistant_message requires non-empty data.text",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_assistant_message data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    return agent_name.strip(), text, response_id.strip()


async def _persist_external_assistant_message(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist and broadcast assistant text produced outside Omnigent tasks.

    The event is append-only conversation history. It intentionally
    bypasses the legacy persist path so mirroring a
    Claude terminal response does not create or steer an Omnigent
    agent task.

    :param session_id: Session/conversation identifier.
    :param body: External assistant-message event body.
    :param conversation_store: Store used to append the message.
    :returns: Store-assigned conversation item id.
    """
    agent_name, text, response_id = _parse_external_assistant_message(body)
    item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="assistant",
            agent=agent_name,
            content=[{"type": "output_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    persisted = persisted_items[0]
    _publish_external_assistant_message(
        session_id,
        persisted,
        response_id=response_id,
        agent_name=agent_name,
    )
    return persisted.id


def _parse_external_conversation_item(
    body: SessionEventInput,
) -> NewConversationItem:
    """
    Validate and unpack an external conversation-item event.

    :param body: ``POST /events`` body whose type is
        :data:`_EXTERNAL_CONVERSATION_ITEM_TYPE`.
    :returns: A parsed :class:`NewConversationItem` ready to append.
    :raises OmnigentError: If required fields are missing or
        malformed.
    """
    item_type = body.data.get("item_type")
    if not isinstance(item_type, str) or item_type not in ITEM_TYPE_TO_DATA_CLS:
        raise OmnigentError(
            "external_conversation_item requires known data.item_type",
            code=ErrorCode.INVALID_INPUT,
        )
    item_data = body.data.get("item_data")
    if not isinstance(item_data, dict):
        raise OmnigentError(
            "external_conversation_item requires object data.item_data",
            code=ErrorCode.INVALID_INPUT,
        )
    response_id = body.data.get("response_id")
    if response_id is None:
        response_id = generate_task_id()
    if not isinstance(response_id, str) or not response_id.strip():
        raise OmnigentError(
            "external_conversation_item data.response_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    message_id = body.data.get("message_id")
    if message_id is not None and (not isinstance(message_id, str) or not message_id):
        raise OmnigentError(
            "external_conversation_item data.message_id must be a non-empty string",
            code=ErrorCode.INVALID_INPUT,
        )
    # NOTE: producers that can re-post (the native transcript forwarders
    # retry timed-out POSTs whose disposition they cannot know) send a
    # ``data.source_id`` dedup key; the persist path derives the item's
    # stable id from it so the append is idempotent (see
    # ``_persist_external_conversation_item``). Items without one keep the
    # store-assigned random id and no server-side dedup.
    # Cap a native tool result so a multi-MB output isn't persisted + broadcast as one frame.
    if item_type == "function_call_output" and isinstance(item_data.get("output"), str):
        item_data = {**item_data, "output": cap_tool_output(item_data["output"])}
    try:
        data = parse_item_data(item_type, {"type": item_type, **item_data})
    except (ValueError, TypeError) as exc:
        raise OmnigentError(
            f"Invalid data payload for external item type {item_type!r}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    if message_id is not None and isinstance(data, MessageData) and data.role == "assistant":
        data = data.model_copy(update={"stream_message_id": message_id})
    return NewConversationItem(
        type=item_type,
        response_id=response_id.strip(),
        data=data,
    )


def _background_task_delivery_status(
    status: str,
    background_task_count: int | None,
) -> str:
    """Collapse a background-task ``waiting`` back to ``idle``.

    A claude-native session relabels its ``Stop`` turn-end ``idle`` to
    ``waiting`` (in the forwarder) while background shells linger. The turn
    itself is over — the TUI takes a new prompt immediately — so ``waiting``
    misreports the session as mid-turn everywhere the status is read as a
    turn gate: the composer queues each send behind "Steer", the sidebar dot
    spins, and because ``waiting`` keeps ``_session_active_response_cache``
    populated a reconnect reopens the settled turn's streaming bubble. For a
    sub-agent it also hangs the orchestrator — the terminal-delivery branch
    in ``post_event`` keys off ``idle``/``failed`` and no follow-up ``Stop``
    ever comes.

    The tally alone already drives every background-shell affordance at
    ``idle`` (the in-chat "N background tasks still running" indicator reads
    the count, not the status), so deliver ``idle`` and let the count speak
    for the shells. Normalizing here rather than in the forwarder also covers
    runners that predate the change.

    :param status: The incoming external status, e.g. ``"waiting"``.
    :param background_task_count: Parsed background-shell tally, or ``None``.
    :returns: ``"idle"`` for a background-task ``waiting``; otherwise
        ``status`` unchanged.
    """
    if status == "waiting" and background_task_count is not None and background_task_count > 0:
        return "idle"
    return status


# Cap the per-shell detail a single edge can carry, mirroring the forwarder's
# own cap so a malformed payload can't bloat the status event server-side.
_MAX_FORWARDED_BACKGROUND_TASKS = 100


def _parse_background_tasks(raw: object) -> list[BackgroundTaskInfo] | None:
    """
    Validate the ``background_tasks`` detail off an external status edge.

    The claude-native forwarder sends this alongside a positive
    ``background_task_count`` (see :func:`_background_task_delivery_status`).
    It is best-effort display data: non-dict / unvalidatable entries are
    dropped rather than failing the whole edge, and the list is capped at
    :data:`_MAX_FORWARDED_BACKGROUND_TASKS`.

    :param raw: The raw ``data.background_tasks`` value off the wire.
    :returns: Parsed detail, or ``None`` when absent or nothing validated.
    """
    if not isinstance(raw, list):
        return None
    parsed: list[BackgroundTaskInfo] = []
    for entry in raw[:_MAX_FORWARDED_BACKGROUND_TASKS]:
        if not isinstance(entry, dict):
            continue
        try:
            parsed.append(BackgroundTaskInfo.model_validate(entry))
        except ValidationError:
            continue
    return parsed or None


# Claude Code writes this record into its own transcript when a turn is
# interrupted (Escape, or steering while a tool is in flight). Anchored so a
# user's bracketed question such as "[Request interrupted by user?]" stays a
# real message. Kept in sync with `INTERRUPT_RE` in web/src/lib/systemMessage.ts,
# which re-classifies the mirrored record as a muted "Interrupted" marker.
_CLAUDE_INTERRUPT_RECORD_RE = re.compile(r"^\[Request interrupted by user(?: for tool use)?\]$")


def _is_native_interrupt_record(data: MessageData) -> bool:
    """
    Whether a mirrored user item is the vendor CLI's own interrupt record.

    The transcript forwarder mirrors every user-role record back, but this
    one is synthesized by Claude itself — it is not the round-trip of a web
    message, so no :mod:`omnigent.runtime.pending_inputs` entry exists for
    it. Draining one anyway shifts the FIFO by a slot: the marker absorbs
    the queued message's uploads and the real message persists with none.

    Runtime ``[System: ...]`` notices are deliberately NOT matched here.
    Those are posted through ``POST /v1/sessions/{id}/events`` and record a
    pending entry of their own (see
    :func:`_dispatch_session_event_to_runner`), so their mirror-back must
    keep draining normally.

    Matched on the first line only, exactly as ``parseSystemMessage`` does
    web-side. The two predicates must agree: a record the web hides as a
    marker but the server drains for would put the bug straight back.

    :param data: The mirrored user message's data, whose ``content`` is a
        list of block dicts, e.g. ``[{"type": "input_text", "text":
        "[Request interrupted by user]"}]``.
    :returns: ``True`` for an interrupt record, ``False`` for a real
        user message.
    """
    if any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
        for block in data.content
    ):
        return False
    text = _message_text(data.content)
    if text is None:
        return False
    first_line = text.strip().split("\n", 1)[0]
    return _CLAUDE_INTERRUPT_RECORD_RE.match(first_line) is not None


def _merge_pending_file_blocks(
    item: NewConversationItem,
    pending_content: list[dict[str, Any]],
) -> NewConversationItem:
    """
    Prepend a pending entry's file blocks onto a user-message item.

    The claude-native transcript mirrors a user message back as
    text-only — ``input_image`` / ``input_file`` blocks are dropped. The
    optimistic pending-input entry still carries them (with real
    ``file_id``s, assigned at upload), so we fold them into the durable
    item here. Without it the image renders only on the optimistic
    bubble and vanishes from history on the next reload.

    No-op when the pending entry has no file blocks, or when the item
    already carries file blocks (defensive — a future transcript that
    does include them must not be doubled).

    :param item: The parsed user-message item about to be persisted.
        Its ``data`` is a :class:`MessageData` whose ``content`` is a
        list of block dicts, e.g. ``[{"type": "input_text",
        "text": "hi"}]``.
    :param pending_content: The drained pending entry's content blocks,
        e.g. ``[{"type": "input_image", "file_id": "file_x",
        "filename": "a.png"}, {"type": "input_text", "text": "hi"}]``.
    :returns: A copy of *item* with the file blocks prepended, or *item*
        unchanged when there is nothing to merge.
    """
    if not isinstance(item.data, MessageData):
        return item
    file_blocks = [
        block
        for block in pending_content
        if isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
    ]
    if not file_blocks:
        return item
    already_has_files = any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_file")
        for block in item.data.content
    )
    if already_has_files:
        return item
    merged_data = item.data.model_copy(update={"content": [*file_blocks, *item.data.content]})
    return item.model_copy(update={"data": merged_data})


def _message_text(content: list[dict[str, Any]]) -> str | None:
    """
    Extract joined text from message content blocks.

    :param content: Message content blocks, e.g.
        ``[{"type": "output_text", "text": "Done"}]``.
    :returns: Joined text from ``text`` / ``input_text`` fields,
        or ``None`` when no text field exists.
    """
    parts: list[str] = []
    found_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("input_text")
        if isinstance(text, str):
            found_text = True
            parts.append(text)
    return "\n".join(parts) if found_text else None


def _response_agent_name_from_store(
    conversation_store: ConversationStore,
    session_id: str,
    response_id: str | None,
) -> str | None:
    """Resolve a native failure's speaker without consulting the current binding."""
    if not response_id:
        return None
    page = conversation_store.list_items(
        session_id, limit=_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT, order="desc", type="message"
    )
    names = {
        item.data.agent.strip()
        for item in page.data
        if item.response_id == response_id
        and isinstance(item.data, MessageData)
        and item.data.role == "assistant"
        and not item.data.is_meta
        and item.data.agent
        and item.data.agent.strip()
    }
    return next(iter(names)) if len(names) == 1 else None


def _latest_assistant_text_from_store(
    conversation_store: ConversationStore,
    session_id: str,
    *,
    response_id: str | None = None,
    stop_at_user_message: bool = False,
) -> str | None:
    """
    Return the latest persisted assistant message text for a session.

    Native harnesses mirror completed transcript items to the AP
    server, not necessarily to the runner's in-memory history. This
    helper lets Omnigent forward the durable assistant output with the
    terminal-observed idle edge.

    :param conversation_store: Store used to read conversation items.
    :param session_id: Session/conversation id, e.g.
        ``"conv_child123"``.
    :param response_id: When known, only return text belonging to this turn.
    :param stop_at_user_message: Without a response id, stop at the latest
        non-meta user message so a failure cannot borrow an earlier reply.
    :returns: Latest assistant text, or ``None`` when none is
        persisted yet.
    """
    page = conversation_store.list_items(
        session_id,
        limit=_EXTERNAL_STATUS_ASSISTANT_SCAN_LIMIT,
        order="desc",
        type="message",
    )
    for item in page.data:
        if not isinstance(item.data, MessageData):
            continue
        if item.data.is_meta:
            continue
        if response_id is not None and item.response_id != response_id:
            continue
        if stop_at_user_message and response_id is None and item.data.role == "user":
            return None
        if item.data.role != "assistant":
            continue
        text = _message_text(item.data.content)
        if text is not None:
            return text
    return None


@dataclass(frozen=True)
class _RunnerForwardResult:
    """
    HTTP result from forwarding a session-control event to the runner.

    :param status_code: Runner response status, e.g. ``204``.
    :param body: Runner response body text. Empty string when the runner
        returns no body.
    """

    status_code: int
    body: str


def _require_external_status_forward(
    session_id: str,
    status: str,
    runner_result: _RunnerForwardResult | None,
) -> None:
    """
    Fail loudly when required external status forwarding does not land.

    Terminal native sub-agent completion is delivered to the parent
    runner through this forward. Dropping it would leave the parent
    waiting forever with no inbox result.

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param status: External status value, e.g. ``"idle"``.
    :param runner_result: HTTP result returned by the runner, or ``None``
        when no runner could be reached.
    :returns: None.
    :raises OmnigentError: If the runner was unavailable or
        rejected the forwarded status.
    """
    if runner_result is None:
        raise OmnigentError(
            f"Could not reach runner to deliver external_session_status "
            f"{status!r} for sub-agent session {session_id!r}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if runner_result.status_code >= 400:
        detail = runner_result.body[:500]
        suffix = f": {detail}" if detail else ""
        raise OmnigentError(
            f"Runner rejected external_session_status {status!r} for "
            f"sub-agent session {session_id!r} with status "
            f"{runner_result.status_code}{suffix}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _publish_child_status_to_parent(session_id: str, status: str) -> None:
    """
    Mirror a status transition onto the session's parent stream.

    A sub-agent's ``busy`` / ``current_task_status`` on the parent's Agents
    rail comes from ``session.child_session.updated`` events. The runner
    fans those out only for children it registered in-process, so a child
    reused after a runner restart, or driven directly rather than through
    its parent, changes status without the parent's stream ever hearing of
    it. The server sees every transition in ``_session_status_cache``, so
    it republishes the child's current summary to the parent from here; a
    top-level session (no parent) publishes nothing.

    The store reads run on the ordered live-state worker so a ``running``
    → ``idle`` pair can never fan out reversed, and the event-loop caller
    only pays a queue put.

    :param session_id: Session whose cached status just changed,
        e.g. ``"conv_child123"``.
    :param status: The new status, e.g. ``"running"``. Captured here rather
        than re-read on the worker so each edge fans out its own value.
    """
    store = session_live_state.conversation_store()
    if store is None:
        return

    def _fan_out() -> None:
        conv = store.get_conversation(session_id)
        if conv is None or conv.parent_conversation_id is None:
            return
        parent_id = conv.parent_conversation_id
        items_by_child = store.list_latest_message_items_for_conversations([conv.id], 10)
        summary = _child_session_summary_from_conversation(
            conv,
            parent_id,
            _latest_message_preview(items_by_child.get(conv.id, [])),
            cached_status=status,
        )
        event = SessionChildSessionUpdatedEvent(
            type="session.child_session.updated",
            conversation_id=parent_id,
            child_session_id=conv.id,
            child=summary.model_dump(mode="json"),
        )
        session_stream.publish(parent_id, event.model_dump())

    session_live_state.submit("child_status_fanout", _fan_out)


# Bound fallback assistant text while preserving multiline tracebacks and
# runner-exit diagnostics within the limit.
_FAILURE_LOG_DETAIL_MAX_CHARS: Final[int] = 4500


def _failure_log_detail(error: ErrorDetail | None) -> str:
    """Render a turn failure's reason for the log, bounded in length.

    :param error: The failure's typed detail, or ``None`` when the publisher
        attached none.
    :returns: The reason, truncated with a dropped-character count when it
        exceeds :data:`_FAILURE_LOG_DETAIL_MAX_CHARS`; ``"no detail"`` when
        ``error`` is ``None`` or carries no message.
    """
    if error is None or not error.message.strip():
        return "no detail"
    message = error.message
    if len(message) <= _FAILURE_LOG_DETAIL_MAX_CHARS:
        return message
    dropped = len(message) - _FAILURE_LOG_DETAIL_MAX_CHARS
    return f"{message[:_FAILURE_LOG_DETAIL_MAX_CHARS]}… (+{dropped} chars)"


def _publish_status(
    session_id: str,
    status: str,
    error: ErrorDetail | None = None,
    response_id: str | None = None,
    background_task_count: int | None = None,
    background_tasks: list[BackgroundTaskInfo] | None = None,
    blocked_on: str | None = None,
    turn: int | None = None,
    persist_live_status: bool = True,
    paused: bool = False,
    scheduled_run_outcome: Literal["auto", "failed"] = "auto",
    failure_origin: str | None = None,
    activity_id: str | None = None,
) -> None:
    """
    Publish a typed :class:`SessionStatusEvent` to the live stream and
    update the cache the list endpoint reads.

    ``status`` must be one of the literals on
    :class:`SessionStatusEvent` (``idle`` / ``running`` / ``waiting``
    / ``failed``); other values fail Pydantic validation rather than
    silently shipping a non-conforming wire shape (rule 15).

    Every publish site funnels through here so the in-memory
    ``_session_status_cache`` stays coherent with the SSE stream.
    Without this, paths that publish but don't write the cache —
    notably the ``external_session_status`` handler used by the
    claude-native forwarder — leave the sidebar stuck on "idle"
    while the chat itself shows "Working…".

    :param session_id: Session/conversation identifier.
    :param status: New session status value.
    :param error: Failure detail to forward on a ``"failed"``
        transition, e.g. ``ErrorDetail(code="runner_error",
        message="turn setup failed: ...")``. ``None`` for every
        non-failed transition. Carrying it lets clients render a
        terminal error line for SETUP-phase failures that never emit
        a ``response.failed`` event.
    :param response_id: Optional response id for terminal-backed status
        edges, e.g. ``"codex_turn_abc123"``.
    :param failure_origin: Stable slug naming the publish path behind a
        ``"failed"`` edge, e.g. ``"runner_disconnected_mid_turn"``. Every
        server-side failure logs one ERROR from here, so without it the
        dozen unrelated causes that reach this function are one
        undifferentiated signature. Ignored for non-failed edges.
    :param activity_id: The running Super Chat turn's Activity id, e.g.
        ``"turn:conv_abc:resp_xyz"``; ``None`` (left off the wire) otherwise.
    """
    # ``failed`` is sticky against a trailing ``idle``. A turn error is
    # terminal — it must not be silently downgraded to ``idle`` by a
    # follow-on quiescence signal. This matters for claude-native: the
    # turn-error edge comes from the ``StopFailure`` hook (→ ``failed``),
    # but the pane then goes quiet, so the PTY-activity watcher emits a
    # trailing ``idle`` ~1s later. Without this guard that ``idle`` would
    # erase the error state before the user could see it. The next
    # ``running`` edge (new activity) clears ``failed`` normally, so the
    # error persists exactly until the session does real work again. No
    # in-process flow performs a legitimate ``failed`` → ``idle``
    # transition (compaction failure publishes ``running`` → ``idle``, not
    # ``failed``), so this is a safe, harness-agnostic invariant.
    if status == "idle" and _session_status_cache.get(session_id) == "failed":
        # Session stays ``failed`` (terminal); the turn is over, so drop any
        # tracked in-flight response id rather than leaving it for the
        # snapshot to reopen a streaming bubble.
        _session_active_response_cache.pop(session_id, None)
        return
    previous_status = _session_status_cache.get(session_id)
    _session_status_cache[session_id] = status
    if previous_status != status:
        _publish_child_status_to_parent(session_id, status)
    # Mirror the transition onto the conversation row (best-effort,
    # deduplicated, off-loop) so replicas that don't hold this session's
    # runner tunnel serve the same sidebar status.
    if persist_live_status:
        session_live_state.persist_live_status(session_id, status)
    # Event-driven scheduled-run completion. A terminal edge (idle = the turn
    # completed; failed = it errored/disconnected) flips the conversation's
    # still-``running`` scheduled_task_run to succeeded/failed. This is the
    # primary FU-1 mechanism: the run transitions the instant the turn ends,
    # driven by the same terminal event that persists live_status — no poll.
    # The event's own ``error`` carries the failure classification, so no label
    # re-read is needed (and none of the race that would imply). A no-op for
    # the common case: interactive (non-scheduled) conversations have no
    # running run, and the reverse lookup cheaply returns None. running/waiting
    # edges are skipped entirely so the hot path pays nothing mid-turn.
    if scheduled_run_outcome == "failed":
        session_live_state.persist_scheduled_run_completion(
            session_id,
            "failed",
            error_code="incomplete",
            error="runner disappeared before the turn reached a terminal state",
        )
        session_live_state.end_work_run(session_id, ok=False)
    elif status == "idle":
        session_live_state.persist_scheduled_run_completion(session_id, "succeeded")
        session_live_state.end_work_run(session_id, ok=True)
    elif status == "failed":
        # Canonical server-side broken-turn signal: every server-originated
        # failed turn (runner disconnect mid-turn, setup/dispatch failure,
        # rejection) funnels through here, so log once at ERROR for the
        # dashboard. Relayed runner failures arrive via session_stream and are
        # already logged runner-side, so they don't reach this path.
        #
        # Because every cause shares this one line, the row has to carry which
        # path published it: the origin slug, the failure code, and the status
        # the session was leaving. The message keeps its "session turn failed
        # for <id>: <detail>" shape so existing detail-matching stays valid.
        origin = failure_origin or "unattributed"
        failure_code = error.code if error is not None else "none"
        _logger.error(
            "session turn failed for %s (origin=%s code=%s prev=%s): %s",
            session_id,
            origin,
            failure_code,
            previous_status or "unknown",
            _failure_log_detail(error),
            extra=debug_event(
                "session_turn_failed",
                session_id=session_id,
                origin=origin,
                code=failure_code,
                previous_status=previous_status or "unknown",
                response_id=response_id,
            ),
        )
        session_live_state.persist_scheduled_run_completion(
            session_id,
            "failed",
            error_code=error.code if error is not None else None,
            error=error.message if error is not None else None,
        )
        session_live_state.end_work_run(session_id, ok=False)
    # Track the in-flight response id for snapshot-based reconnect (see
    # _session_active_response_cache). A running/waiting edge that names a
    # turn opens it; any idle/failed edge closes it.
    if status in ("running", "waiting"):
        if response_id is not None:
            _session_active_response_cache[session_id] = response_id
    else:
        _session_active_response_cache.pop(session_id, None)
    # Keep the background-shell tally sticky alongside the status (see the
    # cache's declaration). A ``Stop`` hook reports an authoritative count
    # (``None`` is never sent by it): a positive count sets the tally, and
    # an explicit ``0`` clears it so a finished background shell drops the
    # indicator on the next turn end. ``None`` means "no information" (the
    # trailing PTY-activity ``idle`` carries none) and must NOT wipe the
    # count the Stop hook just published. A new ``running`` turn does NOT clear
    # it either — background shells outlive turn boundaries, so the tally
    # persists across the turn (the composer pill stays lit alongside the
    # working shimmer) until the next authoritative count. Only a failure
    # clears it: a dead session may never post another count to drop a stale
    # tally.
    # The per-shell detail rides in lockstep with the count: they arrive on the
    # same Stop edge, so an authoritative count owns both caches (a positive
    # count stores the detail — possibly empty when a runner sent count-only —
    # and a ``0`` / failure clears it). ``None`` leaves both untouched.
    if background_task_count is not None:
        if background_task_count > 0:
            _session_background_task_count_cache[session_id] = background_task_count
            _session_background_tasks_cache[session_id] = background_tasks or []
        else:
            _session_background_task_count_cache.pop(session_id, None)
            _session_background_tasks_cache.pop(session_id, None)
    elif status == "failed":
        _session_background_task_count_cache.pop(session_id, None)
        _session_background_tasks_cache.pop(session_id, None)
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status=status,  # type: ignore[arg-type]
        response_id=response_id,
        error=error,
        background_task_count=background_task_count,
        background_tasks=background_tasks,
        blocked_on=blocked_on,
        paused=True if paused else None,
        turn=turn,
        activity_id=activity_id,
    )
    payload = event.model_dump()
    if turn is None:
        payload.pop("turn", None)
    if response_id is None:
        payload.pop("response_id", None)
    if background_task_count is None:
        payload.pop("background_task_count", None)
    # Only put detail on the wire when there's something to show — an absent or
    # empty list stays off (the count alone drives the pill; the client clears
    # its detail when the count clears).
    if not background_tasks:
        payload.pop("background_tasks", None)
    if blocked_on is None:
        payload.pop("blocked_on", None)
    if not paused:
        payload.pop("paused", None)
    if activity_id is None:
        payload.pop("activity_id", None)
    session_stream.publish(session_id, payload)
    from omnigent.superchat.family.signals import notify_chat_status

    notify_chat_status(session_id, status, blocked_on)


def reconcile_orphaned_running_status(
    session_id: str,
    conversation_store: ConversationStore,
    stale_before: int,
) -> bool:
    """
    Settle a session that reads ``running`` but whose runner is
    confirmed gone down to a non-running resting state.

    A ``running`` live-status is only meaningful while a runner is
    actually executing the turn. When the runner (and its host) have
    dropped past the liveness window — a server replica that restarted
    and outlived the runner, a crashed host, a graceful disconnect
    mid-turn — the persisted ``running`` is stale: no executor will
    ever emit the terminal edge that would clear it, so it sticks
    forever. The sidebar then shows a turn that isn't happening, and
    ``stop_session`` reports a success it never delivered.

    This is the lazy-on-read backstop for that stale state. The store performs
    one conditional transition so a fresh liveness stamp or terminal status
    written by another replica wins the race. A successful transition updates
    the local cache and stream without issuing a second status write, and
    classifies any associated scheduled run as failed/incomplete.

    :param session_id: Session/conversation identifier to settle.
    :param conversation_store: Store performing the conditional transition.
    :param stale_before: Runner stamps at or after this epoch are fresh.
    :returns: Whether this call performed the transition.
    """
    if not conversation_store.settle_orphaned_live_status(session_id, stale_before):
        return False
    _publish_status(
        session_id,
        "idle",
        persist_live_status=False,
        scheduled_run_outcome="failed",
    )
    return True


def _truncate_label(value: str) -> str:
    """Truncate a label value to fit the ``conversation_labels.value`` column.

    Long failure messages (tracebacks, 5xx bodies) overflow the column and
    cause a ``DataError`` that silently drops the error reason. Error messages
    front-load their signal, so keeping the head and appending an ellipsis
    preserves the useful part while flagging that more was dropped. The store
    clamps again as a final guard, but truncating here keeps the marker and
    makes the call site directly testable.

    :param value: The raw string to truncate.
    :returns: ``value`` unchanged if it already fits, else the head trimmed to
        the column width with a trailing ``…`` to signal truncation.
    """
    if len(value) <= _LABEL_VALUE_MAX_LEN:
        return value
    return value[: _LABEL_VALUE_MAX_LEN - 1] + "…"


async def _persist_session_status_error_labels(
    session_id: str,
    error: ErrorDetail | None,
    conversation_store: ConversationStore,
    *,
    agent_name: str | None = None,
) -> None:
    """
    Persist or clear the reload-visible failure detail for a session status.

    ``session.status`` is an SSE edge, so its ``error`` object disappears on
    reload. Terminal-native sessions can fail before any transcript item is
    written, so store the latest failure detail as runner-owned labels and let
    snapshots project it as ``last_task_error``. Empty string clears stale
    values because the label store is upsert-only.

    :param session_id: Session/conversation identifier.
    :param error: Failure detail from a ``session.status: failed`` edge, or
        ``None`` to clear stale error labels on subsequent activity.
    :param conversation_store: Store used to upsert labels.
    :param agent_name: Agent responsible for this failure, captured before a rebind.
    """
    # Structured fields are optional (present only when the runner classified
    # the failure). Always write all keys — empty when absent — because the
    # label store is upsert-only and a stale title/cause from a prior failure
    # must not leak onto a later, unclassified one.
    updates = (
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: _truncate_label(error.code),
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: _truncate_label(error.message),
            _LAST_TASK_ERROR_AGENT_NAME_LABEL_KEY: _truncate_label(agent_name or ""),
            _LAST_TASK_ERROR_TITLE_LABEL_KEY: _truncate_label(error.title or ""),
            _LAST_TASK_ERROR_CAUSE_LABEL_KEY: _truncate_label(error.cause or ""),
            _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY: _truncate_label(error.remediation or ""),
        }
        if error is not None
        else {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: "",
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "",
            _LAST_TASK_ERROR_AGENT_NAME_LABEL_KEY: "",
            _LAST_TASK_ERROR_TITLE_LABEL_KEY: "",
            _LAST_TASK_ERROR_CAUSE_LABEL_KEY: "",
            _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY: "",
        }
    )
    try:
        await asyncio.to_thread(conversation_store.set_labels, session_id, updates)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Failed to persist session status error labels for %s",
            session_id,
            extra={"session_id": session_id},
        )


def _last_task_error_from_labels(labels: Mapping[str, str]) -> dict[str, str] | None:
    """
    Project runner-owned failure labels into the typed API error shape.

    Terminal/native runtimes can fail before they write any transcript item,
    so the session-status relay stores the latest failure as durable labels.
    This helper is the single server-side boundary where those internal labels
    become public ``last_task_error`` data for snapshots and child summaries.

    :param labels: Conversation labels, usually after closed-status projection.
    :returns: ``{"code": "...", "message": "..."}``, or ``None`` when either
        value is absent/cleared.
    """
    raw_error_code = labels.get(_LAST_TASK_ERROR_CODE_LABEL_KEY)
    raw_error_message = labels.get(_LAST_TASK_ERROR_MESSAGE_LABEL_KEY)
    if raw_error_code and raw_error_message:
        error: dict[str, str] = {
            "code": classify_native_turn_error(raw_error_code, raw_error_message),
            "message": raw_error_message,
        }
        for key, label in (
            ("agent_name", _LAST_TASK_ERROR_AGENT_NAME_LABEL_KEY),
            ("title", _LAST_TASK_ERROR_TITLE_LABEL_KEY),
            ("cause", _LAST_TASK_ERROR_CAUSE_LABEL_KEY),
            ("remediation", _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY),
        ):
            value = labels.get(label)
            if value:
                error[key] = value
        return error
    return None


def _publish_terminal_pending(session_id: str, pending: bool) -> None:
    """
    Publish a typed :class:`SessionTerminalPendingEvent` and update the
    cache the snapshot reads.

    Every relay site that changes the terminal-spin-up flag funnels
    through here so the in-memory ``_session_terminal_pending_cache``
    stays coherent with the SSE stream — a client connecting
    mid-spin-up seeds the spinner from the snapshot's
    ``terminal_pending`` field, while already-connected clients update
    live off this event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param pending: ``True`` while the runner is auto-creating the
        terminal; ``False`` once it lands or auto-create fails.
    """
    # Store only ``True`` entries; delete on clear so the cache never
    # accumulates stale ``False`` entries for every terminal-first session
    # that has ever completed spin-up. The snapshot getter uses
    # ``.get(id, False)`` so absent == False.
    if pending:
        _session_terminal_pending_cache[session_id] = True
    else:
        _session_terminal_pending_cache.pop(session_id, None)
    event = SessionTerminalPendingEvent(
        type="session.terminal_pending",
        conversation_id=session_id,
        pending=pending,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_sandbox_status(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._publish_sandbox_status(*args, **kwargs)


def _publish_sandbox_status_impl(session_id: str, stage: str, error: str | None = None) -> None:
    """
    Publish a typed :class:`SessionSandboxStatusEvent` and update the
    cache the snapshot reads.

    Every stage transition of a managed-sandbox launch funnels through
    here so the in-memory ``_session_sandbox_status_cache`` stays
    coherent with the SSE stream — a client opening the session
    mid-launch seeds its progress indicator from the snapshot's
    ``sandbox_status`` field, while already-connected clients update
    live off this event. Thread-safe (``session_stream.publish`` is a
    thread-safe broadcast and the cache write is a single dict
    assignment), so the launch pipeline may call this from the worker
    thread its sandbox exec steps run on.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param stage: The launch stage just entered, e.g.
        ``"provisioning"`` — one of
        :data:`omnigent.server.schemas.SandboxLaunchStage`.
    :param error: Failure detail when *stage* is ``"failed"``, e.g.
        ``"managed sandbox launch failed: spend limit reached"``.
        ``None`` for non-terminal stages.
    """
    # "ready" evicts: from then on the session looks like any
    # host-bound session and the snapshot carries no launch state.
    # Failures stay cached (mirroring ManagedLaunchTracker retention)
    # so a reload after a dead launch still shows the reason.
    status = SandboxStatus.model_validate({"stage": stage, "error": error})
    previous = _session_sandbox_status_cache.get(session_id)
    log = _logger.error if stage == "failed" else _logger.info
    log(
        "Managed sandbox launch stage: %s",
        stage,
        extra=debug_event(
            "sandbox_launch_failed" if stage == "failed" else "sandbox_launch_stage",
            session_id=session_id,
            stage=(previous.stage if previous else "unknown") if stage == "failed" else stage,
        ),
    )
    if status.stage == "ready":
        _session_sandbox_status_cache.pop(session_id, None)
    else:
        _session_sandbox_status_cache[session_id] = status
    event = SessionSandboxStatusEvent(
        type="session.sandbox_status",
        conversation_id=session_id,
        stage=status.stage,
        error=status.error,
    )
    session_stream.publish(session_id, event.model_dump())


def _publish_mcp_startup(session_id: str, servers: dict[str, McpServerStartup]) -> None:
    """
    Publish a typed :class:`SessionMcpStartupEvent` to the live stream.

    Fired when a native forwarder reports harness MCP-server startup
    progress via ``external_mcp_startup``, so the web UI can show
    per-server startup state while the harness boots instead of an
    apparently hung session. Also updates the snapshot cache so a client
    opening the session mid-startup seeds the band from the snapshot's
    ``mcp_startup`` field; a map with nothing left to show — empty, or
    every server ``ready`` — evicts the cache entry, mirroring the web
    store's all-ready clear so a reloading client never seeds a band
    that renders nothing.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param servers: Latest per-server startup map, e.g.
        ``{"safe": McpServerStartup(status="starting", error=None)}``.
    """
    if any(record.status != "ready" for record in servers.values()):
        _session_mcp_startup_cache[session_id] = servers
    else:
        _session_mcp_startup_cache.pop(session_id, None)
    event = SessionMcpStartupEvent(
        type="session.mcp_startup",
        conversation_id=session_id,
        servers=servers,
    )
    session_stream.publish(session_id, event.model_dump())


def _invalidate_runner_backed_snapshot_state(session_id: str) -> None:
    """
    Drop runner-derived session snapshot overlays for one session.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    """
    from omnigent.server.smart_routing import invalidate_runner_catalog

    # Routing's candidate catalog is runner-derived too: a rebind or a relaunch
    # can change which models the session can be switched onto, so it must not
    # keep routing off the previous runner's list.
    invalidate_runner_catalog(session_id)


def _publish_changed_files_invalidated(session_id: str, environment_id: str = "default") -> None:
    """
    Publish a coarse filesystem-change invalidation to the live stream.

    The event tells web clients to refetch visible filesystem views
    for the environment instead of polling the tree while a session is
    active. It is intentionally coarse because git-mode workspaces can
    only answer "the working tree changed" cheaply, not per-directory
    deltas.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param environment_id: Environment resource id,
        e.g. ``"default"``.
    """
    session_stream.publish(
        session_id,
        {
            "type": "session.changed_files.invalidated",
            "session_id": session_id,
            "environment_id": environment_id,
        },
    )


def _publish_interrupted(session_id: str, response_id: str | None = None) -> None:
    """
    Publish a ``session.interrupted`` event to the live stream.

    The event is co-emitted with ``response.incomplete`` (reason
    ``"user_interrupt"``) by the runtime cancel handler so off-the-
    shelf Responses parsers still close cleanly. This helper is
    responsible only for the session-level signal — not the
    response-level one.

    :param session_id: The session/conversation identifier whose
        stream should receive the event, e.g. ``"conv_abc123"``.
    :param response_id: Optional response id for terminal-backed
        interrupted turns, e.g. ``"codex_turn_abc123"``.
    """
    event = SessionInterruptedEvent(
        type="session.interrupted",
        data=SessionInterruptedPayload(
            requested_at=int(time.time()),
            response_id=response_id,
        ),
    )
    payload = event.model_dump()
    if response_id is None:
        data = payload.get("data")
        if isinstance(data, dict):
            data.pop("response_id", None)
    session_stream.publish(session_id, payload)


def _publish_session_superseded(session_id: str, target_conversation_id: str) -> None:
    """
    Publish a ``session.superseded`` event to the live stream.

    Emitted when a Claude ``/clear`` rotates a session away (see
    ``_post_clear_supersession`` in
    ``omnigent/claude_native_forwarder.py``): a client actively viewing
    ``session_id`` follows to ``target_conversation_id``. Live-only —
    there is no SSE replay, so a client connecting after the rotation
    relies on the persisted notice message instead.

    :param session_id: The superseded (old) conversation id whose stream
        should receive the event, e.g. ``"conv_old"``.
    :param target_conversation_id: The conversation to redirect to, e.g.
        ``"conv_new"``.
    """
    event = SessionSupersededEvent(
        type="session.superseded",
        conversation_id=session_id,
        target_conversation_id=target_conversation_id,
        reason="clear",
    )
    session_stream.publish(session_id, event.model_dump())
    # Discard any unconsumed pending inputs on the superseded session — notably
    # the ``/clear`` the user typed in the web UI. ``/clear`` is never mirrored
    # back as a committed item (the session rotated away), so its pending entry
    # would otherwise linger forever as a stuck optimistic bubble, re-hydrating
    # from the snapshot on every reload of the old chat. Live viewers already
    # drop the bubble on the ``session.superseded`` event above; this stops it
    # coming back. We deliberately do NOT emit ``session.input.consumed`` (that
    # would commit ``/clear`` as a user message) — the persisted clear notice
    # already explains the rotation, so the input is simply abandoned.
    discarded = 0
    while pending_inputs.resolve_oldest(session_id) is not None:
        discarded += 1
    if discarded:
        _logger.info(
            "Discarded %d unconsumed pending input(s) on superseded session %s",
            discarded,
            session_id,
            extra={"session_id": session_id},
        )


def _publish_btw_sidechat(
    session_id: str,
    *,
    question: str,
    answer: str,
    truncated: bool,
) -> None:
    """
    Publish a transient ``session.btw_sidechat`` overlay to the live stream.

    Emitted when the claude-native forwarder scrapes a settled ``/btw``
    side-chat from the pane (see ``_relay_btw_overlay`` in the
    claude-native forwarder). Broadcast-only: nothing is written to the
    conversation store, so the ephemeral exchange never lands in the main
    transcript. Live viewers render a dismissable overlay; a client that
    connects later never sees it (no SSE replay), matching the terminal
    overlay's Escape-to-close, leave-no-history behavior.

    The oldest pending input is also discarded so a web composer's
    optimistic ``/btw`` bubble does not linger as a stuck "queued" message —
    the same reconciliation ``_publish_session_superseded`` performs.
    ``/btw`` is never committed as a user turn (no ``session.input.consumed``
    is emitted); the overlay carries the request text instead.

    :param session_id: Conversation id whose stream receives the event.
    :param question: The ``/btw`` request line as typed.
    :param answer: The side-chat answer text.
    :param truncated: True when the pane clipped a longer answer.
    """
    event = SessionBtwSidechatEvent(
        type="session.btw_sidechat",
        conversation_id=session_id,
        question=question,
        answer=answer,
        truncated=truncated,
    )
    session_stream.publish(session_id, event.model_dump())
    # Drop the optimistic ``/btw`` bubble (the oldest unconsumed input) so it
    # does not spin forever — ``/btw`` never round-trips through the transcript
    # to earn a ``session.input.consumed``. Only the oldest is resolved so a
    # follow-up the user queued after ``/btw`` is left intact.
    if pending_inputs.resolve_oldest(session_id) is not None:
        _logger.info(
            "Discarded the pending /btw input on session %s",
            session_id,
            extra={"session_id": session_id},
        )


async def _get_runner_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient | None:
    """Call-time proxy so a ``sessions._get_runner_client`` patch is honored here.

    The real body lives on the ``sessions`` facade (as the ``_impl`` alias); tests
    patch the facade attribute, so sibling callers must resolve it there at call
    time rather than binding the pre-split local copy.
    """
    from omnigent.server.routes import sessions as _facade

    return await _facade._get_runner_client(*args, **kwargs)


async def _get_runner_client_impl(
    session_id: str,
    runner_router: RunnerRouter | None,
    *,
    conversation: Conversation | None = None,
) -> httpx.AsyncClient | None:
    """
    Get an HTTP client for the runner bound to a session.

    Uses the ``RunnerRouter`` to resolve the pinned runner. Falls
    back to the in-process runner client for test setups.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or
        ``None`` for in-process setups.
    :param conversation: An already-loaded conversation to reuse for routing.
    :returns: An ``httpx.AsyncClient`` pointed at the runner,
        or ``None`` if no runner is available.
    """
    from omnigent.runtime import get_runner_client

    if runner_router is not None:
        try:
            if conversation is None:
                routed = runner_router.client_for_session_resources(session_id)
            else:
                routed = runner_router.client_for_session_resources(
                    session_id,
                    conversation=conversation,
                )
            return routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s",
                session_id,
                extra={"session_id": session_id},
            )
            return None
    return cast("httpx.AsyncClient | None", get_runner_client())


async def _query_host_runner_status(
    host_conn: HostConnection,
    host_registry: HostRegistry,
    runner_id: str,
) -> str | None:
    """
    Ask a host whether a runner's process is alive, dead, or unknown.

    The host owns runner-process liveness (it holds the ``Popen``), so it
    can answer the one question the server's tunnel registry cannot: is an
    absent-from-the-tunnel runner still coming (booting) or gone for good
    (stopped, crashed, or lost to a host restart)? Used before the connect
    grace so the dispatch path waits only for a runner that is coming.

    :param host_conn: Live host connection to query.
    :param host_registry: Registry used to enqueue the outbound frame.
    :param runner_id: Runner to ask about, e.g. ``"runner_abc123..."``.
    :returns: ``"alive"``, ``"dead"``, or ``"unknown"`` from the host; or
        ``None`` when the host didn't reply in time, the connection
        dropped, or the host is too old to support the query. ``None``
        means "no authoritative answer" — the caller falls back to the
        plain connect grace, preserving the prior blind-wait behavior.
    """
    from omnigent.host.frames import HostRunnerStatusFrame, encode_host_frame
    from omnigent.server.routes import sessions as _facade

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    host_conn.pending_runner_status[request_id] = future
    frame = encode_host_frame(HostRunnerStatusFrame(request_id=request_id, runner_id=runner_id))
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError:
            return None
        result = await asyncio.wait_for(
            future,
            timeout=_facade._HOST_RUNNER_STATUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        # Defensive: this query only ever *speeds up* the connect grace, so
        # any unexpected failure (e.g. the future resolved with an error)
        # must degrade to "no verdict" and fall back to the wait rather than
        # break the message POST. CancelledError is a BaseException and still
        # propagates, so the race helper's cancel/drain is unaffected.
        _logger.warning(
            "host.runner_status query for runner %s failed; falling back to grace",
            runner_id,
            exc_info=True,
        )
        return None
    finally:
        host_conn.pending_runner_status.pop(request_id, None)
    return result.get("status")


async def _wait_for_runner_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._wait_for_runner_client(*args, **kwargs)


async def _wait_for_runner_client_impl(
    session_id: str,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    *,
    runner_id: str | None,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None = None,
) -> httpx.AsyncClient | None:
    """
    Wait until a runner connects, then resolve the session's runner client.

    The tunnel registry owns the event-driven "runner connected" signal.
    After that signal fires, this helper intentionally resolves through
    :func:`_get_runner_client` instead of constructing a client directly
    from the registry session: the router re-checks the conversation's
    current ``runner_id`` binding and preserves the existing ownership /
    capability checks.

    When ``runner_exit_reports`` is supplied, the wait also ends the
    moment the daemon reports this runner died (``host.runner_exited``).
    That report is the authoritative "this runner is busted" signal — a
    crashed runner can never connect, so waiting out ``timeout_s`` would
    only delay the caller's failure handling. Returning ``None`` on the
    report (same as a timeout) lets the caller persist the failure the
    instant we are convinced, neither speculatively early nor a full
    timeout late.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_router: The ``RunnerRouter`` instance, or ``None`` for
        in-process test setups.
    :param tunnel_registry: The server's ``TunnelRegistry`` instance, or
        ``None`` in test setups without runner tunnels.
    :param runner_id: Runner id expected to connect, e.g.
        ``"runner_0123456789abcdef"``.
    :param timeout_s: Maximum seconds to wait, e.g. ``3.0``.
    :param runner_exit_reports: Crash-report store consulted to abort the
        wait early when this runner is reported dead. ``None`` keeps the
        plain wait-to-timeout behavior.
    :returns: A runner HTTP client if one becomes available, otherwise
        ``None`` (timed out, or the runner was reported dead).
    """
    if runner_id is None:
        return None
    if tunnel_registry is None:
        return await _get_runner_client(session_id, runner_router)
    if runner_exit_reports is None:
        session = await tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
        return None if session is None else await _get_runner_client(session_id, runner_router)
    # Race the event-driven connect signal against the crash-report poll;
    # whichever resolves first wins. A report means the runner is busted —
    # stop waiting and let the caller fail the turn now.
    connect_task = asyncio.ensure_future(
        tunnel_registry.wait_for_runner(runner_id, timeout_s=timeout_s)
    )
    try:
        while not connect_task.done():
            if runner_exit_reports.get(runner_id) is not None:
                return None
            await asyncio.wait({connect_task}, timeout=_RUNNER_CONVICTION_POLL_S)
    finally:
        if not connect_task.done():
            connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect_task
    session = connect_task.result()
    return None if session is None else await _get_runner_client(session_id, runner_router)


async def _validate_session_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str | None,
    agent: Any,
    agent_cache: AgentCache | None,
    request: Request,
) -> str:
    """
    Validate a session's workspace against the agent's os_env boundary.

    Wraps the seven-step validation in
    :mod:`omnigent.server.routes._workspace_validation` and
    raises :class:`OmnigentError` on failure so the route layer
    converts the error into a 400 response with a clear message.
    See ``designs/SESSION_WORKSPACE_SELECTION.md`` for the full
    semantic spec.

    The caller's host ownership is checked BEFORE the ``host.stat``
    round-trip the validation performs, so a non-owner never reaches
    another user's host (raises 403/404 via ``resolve_host_owner``).

    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``, or ``None`` when auth is disabled.
    :param host_id: Stable host id, e.g. ``"host_a1b2c3d4..."``.
    :param workspace: Absolute path supplied by the caller, e.g.
        ``"/Users/corey/universe/src/foo"``. ``None`` is rejected
        with the "workspace required when host_id is set" message.
    :param agent: The agent the session binds to. Used to load the
        bundle and read ``os_env.cwd`` for boundary computation.
    :param agent_cache: Cache for loading parsed agent specs from
        bundle storage. Required because session-create needs the
        spec; ``None`` is treated as a server config error.
    :param request: FastAPI request; ``request.app.state``
        carries the host registry and host store.
    :returns: The canonicalized workspace path that should be
        stored on the session row, e.g.
        ``"/Users/corey/universe/src/foo"`` (realpath; symlinks
        already resolved by the host).
    :raises OmnigentError: With ``ErrorCode.INVALID_INPUT`` on
        any validation failure (offline host, missing path,
        outside boundary, missing subdir). With
        ``ErrorCode.INTERNAL_ERROR`` if ``agent_cache`` is unset.
    """
    return await validate_existing_host_workspace(
        user_id=user_id,
        host_id=host_id,
        workspace=workspace,
        agent=agent,
        agent_cache=agent_cache,
        host_store=getattr(request.app.state, "host_store", None),
        host_registry=getattr(request.app.state, "host_registry", None),
    )


@dataclass
class _HostLaunchAttempt:
    """
    Outcome of a relaunch ``host.launch_runner`` round-trip.

    :param runner_id: The token-bound runner id minted for this attempt,
        e.g. ``"runner_token_abc123..."``. Always set (the binding is
        rotated before the frame is sent), even when the host refused.
    :param error_code: Structured failure category from the host's result
        frame, e.g. ``"harness_not_configured"``; ``None`` on a successful
        launch, on a timeout waiting for the result, or when the host sent
        no code.
    :param error: Human-readable failure message from the host, e.g.
        ``"harness 'codex' is not configured on host 'laptop' — run
        `omnigent setup` ..."``; ``None`` when there was no error.
    :param acknowledged: Whether the host confirmed ``status="launched"``;
        timeout and lost-connection attempts remain unconfirmed.
    """

    runner_id: str
    error_code: str | None = None
    error: str | None = None
    acknowledged: bool = False


async def _launch_runner_on_host(*args: Any, **kwargs: Any) -> _HostLaunchAttempt:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._launch_runner_on_host(*args, **kwargs)


# Per-conversation relaunch locks (single-flight; weak values so a lock is
# collected once no flight holds or awaits it), the most recent attempt per
# conversation (so a rider that short-circuits onto another flight's binding
# surfaces THAT flight's structured refusal instead of a generic connect
# timeout), and strong refs to the detached superseded-runner stops.
# custom-lint: disable-next=workspace-scoped-cache -- lock; collision only serializes
_relaunch_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_relaunch_last_attempt: WorkspaceScopedCache[str, _HostLaunchAttempt] = WorkspaceScopedCache()
# Riders read the memo within a flight's own window (milliseconds), so it only
# has to outlive the racing callers, not the conversation. Cap it: a
# weak-valued map would drop entries the racers still need, and an uncapped one
# would keep a row for every conversation this process ever relaunched.
_RELAUNCH_MEMO_MAX = 512
# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_detached_supersede_stops: set[asyncio.Task[None]] = set()


def _spawn_superseded_runner_stop(
    session_id: str,
    host_id: str,
    runner_id: str,
    host_registry: HostRegistry,
) -> None:
    """
    Stop a relaunch's superseded runner as a retained background task.

    The relaunch has already rotated the session's binding, so the old
    runner serves nothing — but left alive it idles forever (tunnel still
    authenticating, transcript forwarder still tailing the session): one
    leaked generation per relaunch, each holding a native pane. Detached so
    the relaunch's latency never waits out the stop's ack timeout; the host
    connection is live by construction here (the launch frame just went
    over it), so delivery is the overwhelmingly common case, and the
    host-side supersession check covers a lost frame. Any failure is
    logged rather than left as an unretrieved task exception.

    :param session_id: Session/conversation identifier.
    :param host_id: The session's owning host.
    :param runner_id: The superseded runner's id.
    :param host_registry: Registry holding the live host tunnel.
    """

    async def _stop_and_log() -> None:
        try:
            await _stop_session_host_runner(
                session_id,
                host_id,
                runner_id,
                host_registry,
                expect_already_stopped=True,
            )
        except Exception:  # noqa: BLE001 — must never die unobserved
            _logger.warning(
                "Superseded-runner stop failed for session %s runner %s; "
                "the host-side supersession check reaps it on the next relaunch",
                session_id,
                runner_id,
                exc_info=True,
                extra={"session_id": session_id},
            )

    task = asyncio.create_task(_stop_and_log())
    _detached_supersede_stops.add(task)
    task.add_done_callback(_detached_supersede_stops.discard)


async def _launch_runner_on_host_impl(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
) -> _HostLaunchAttempt:
    """
    Ask a host to spawn a runner for a session and capture the result.

    Generates a new binding token, writes the runner_id to the session
    row, sends ``host.launch_runner`` (carrying the session's canonical
    harness so the host can refuse an unconfigured one), and waits up to
    :data:`_HOST_LAUNCH_RESULT_TIMEOUT_S` for the host's result frame.
    Does NOT wait for the runner to *connect* — the caller polls for that
    separately; this only captures the spawn/refuse verdict so a
    structured refusal (harness not configured) can be surfaced instead
    of silently timing out as ``RUNNER_UNAVAILABLE``.

    Single-flight per conversation: concurrent callers (a turn POST racing a
    terminal ensure, both seeing the runner offline) serialize on a
    per-conversation lock, and a caller that waited re-reads the row — a
    binding another flight just rotated is ridden instead of spawning a
    second runner. The superseded runner is stopped best-effort in the
    background (see :func:`_spawn_superseded_runner_stop`); before this,
    every relaunch leaked the previous runner process on the host.

    :param conv: The conversation that needs a runner.
    :param conversation_store: Store for updating ``runner_id``.
    :param host_registry: In-memory ``HostRegistry``.
    :param host_conn: The live ``HostConnection`` for the host.
    :returns: The :class:`_HostLaunchAttempt` — the new runner id plus any
        structured refusal from the host.
    """
    lock = _relaunch_locks.setdefault(conv.id, asyncio.Lock())
    async with lock:
        # Re-read under the lock: a binding that moved past the caller's
        # snapshot means another flight just relaunched (concurrently, or
        # a moment ago) — ride its runner rather than rotating it away
        # and double-spawning. Return that flight's own attempt when it is
        # on record, so a structured refusal (harness not configured,
        # workspace missing) still reaches this caller's error surface.
        fresh = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if fresh is None:
            # Deleted mid-relaunch: don't rotate a vanished row or spawn a
            # runner for a session nothing can route to.
            return _HostLaunchAttempt(
                runner_id=conv.runner_id or "",
                error_code="session_not_found",
                error="session was deleted while its relaunch was in flight",
            )
        if fresh.runner_id and fresh.runner_id != conv.runner_id:
            last = _relaunch_last_attempt.get(conv.id)
            if last is not None and last.runner_id == fresh.runner_id:
                return last
            return _HostLaunchAttempt(runner_id=fresh.runner_id)
        attempt = await _launch_runner_on_host_locked(
            fresh, conversation_store, host_registry, host_conn
        )
        # Re-insert so the plain dict's insertion order is newest-last, then
        # evict from the front once past the cap.
        _relaunch_last_attempt.pop(conv.id, None)
        _relaunch_last_attempt[conv.id] = attempt
        while len(_relaunch_last_attempt) > _RELAUNCH_MEMO_MAX:
            del _relaunch_last_attempt[next(iter(_relaunch_last_attempt))]
        return attempt


async def _launch_runner_on_host_locked(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
) -> _HostLaunchAttempt:
    """The launch round-trip proper; runs under the conversation's lock."""
    from omnigent.host.frames import HostLaunchRunnerFrame, encode_host_frame
    from omnigent.runner.identity import token_bound_runner_id

    await host_registry.admit_launch(host_conn, conv.id)
    superseded_runner_id = conv.runner_id
    binding_token = secrets.token_urlsafe(32)
    new_runner_id = token_bound_runner_id(binding_token)

    await asyncio.to_thread(
        conversation_store.replace_runner_id,
        conv.id,
        new_runner_id,
    )
    _logger.info(
        "Session bound to runner",
        extra=debug_event(
            "session_runner_bound",
            session_id=conv.id,
            runner_id=new_runner_id,
            operation="replace" if superseded_runner_id else "launch",
            stage="runner_launch",
        ),
    )
    with runner_log_scope(conv.id, new_runner_id):
        if superseded_runner_id and conv.host_id is not None:
            # The old runner is unbound as of the replace above; reap it so it
            # doesn't idle on the host forever (tunnel still authenticating,
            # forwarder still tailing this session).
            _spawn_superseded_runner_stop(
                conv.id, conv.host_id, superseded_runner_id, host_registry
            )

        # Pull workspace from the session row — populated and validated
        # at session create per designs/SESSION_WORKSPACE_SELECTION.md.
        # The check constraint guarantees workspace is non-NULL when
        # host_id is set, so this assertion is a tripwire for any path
        # that bypassed the validation.
        if conv.workspace is None:  # pragma: no cover — constraint guards
            _logger.error(
                "session %s has host_id=%s but workspace is NULL — schema "
                "constraint should have prevented this",
                conv.id,
                conv.host_id,
                extra=debug_event(
                    "runner_launch_failed",
                    session_id=conv.id,
                    stage="runner_launch",
                    error_code="host_launch_failed",
                ),
            )
            return _HostLaunchAttempt(runner_id=new_runner_id)
        request_id = secrets.token_hex(8)
        launch_future: asyncio.Future[dict[str, str | None]] = (
            asyncio.get_running_loop().create_future()
        )
        host_conn.pending_launches[request_id] = launch_future
        launch_frame = encode_host_frame(
            HostLaunchRunnerFrame(
                request_id=request_id,
                binding_token=binding_token,
                workspace=conv.workspace,
                session_id=conv.id,
                # Canonical harness (see _resolve_harness) so the host runs the
                # same configuration check it does at create-time launch. None
                # (agent not resolvable) skips the host-side check — fail open.
                harness=_resolve_harness(conv),
                inference_config=snapshot_runtime_config(conv.inference_snapshot),
            )
        )
        try:
            host_registry.send_text(host_conn, launch_frame)
        except ConnectionError:
            host_conn.pending_launches.pop(request_id, None)
            _logger.warning(
                "Host %s connection lost while launching runner for %s",
                conv.host_id,
                conv.id,
                extra=debug_event(
                    "runner_launch_failed",
                    session_id=conv.id,
                    stage="runner_launch",
                    error_code="host_launch_failed",
                ),
            )
            return _HostLaunchAttempt(runner_id=new_runner_id)
        try:
            result = await asyncio.wait_for(
                launch_future,
                timeout=_HOST_LAUNCH_RESULT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            # No result yet — fall through to the caller's connect wait, which
            # preserves the prior fire-and-forget timing for a slow-but-fine host.
            host_conn.pending_launches.pop(request_id, None)
            # A slow host, not a refusal: the launch may still land.
            _logger.warning(
                "Host launch acknowledgement timed out",
                extra=debug_event(
                    "runner_launch_failed",
                    stage="runner_launch",
                    error_code="host_launch_timeout",
                    error_category=ErrorCategory.HOST.value,
                    error_impact=ErrorImpact.TRANSIENT.value,
                    error_phase=ErrorPhase.RUNNER_LAUNCH.value,
                ),
            )
            return _HostLaunchAttempt(runner_id=new_runner_id)
        if result.get("status") == "failed":
            refusal_code = result.get("error_code")
            # Unmapped codes (spawn failures) are attributed on the host's own row.
            refusal_category: str | None = None
            if isinstance(refusal_code, str):
                mapped = category_for_code(refusal_code)
                if mapped is not ErrorCategory.UNKNOWN:
                    refusal_category = mapped.value
            _logger.error(
                "Host refused runner launch",
                extra=debug_event(
                    "runner_launch_failed",
                    stage="runner_launch",
                    error_code=refusal_code,
                    error_category=refusal_category,
                    error_impact=ErrorImpact.BLOCKING.value,
                    error_phase=ErrorPhase.RUNNER_LAUNCH.value,
                ),
            )
            return _HostLaunchAttempt(
                runner_id=new_runner_id,
                error_code=result.get("error_code"),
                error=result.get("error"),
            )
        return _HostLaunchAttempt(runner_id=new_runner_id, acknowledged=True)


async def cancel_managed_launch_tasks() -> None:
    """
    Cancel and await every in-flight background managed launch.

    Lifespan-teardown hook: without it, a slow provision outlives the
    ASGI shutdown and dies wherever the loop teardown happens to kill
    it. Cancellation is deterministic teardown of the TASK only — an
    already-provisioned sandbox is not terminated here (there is no
    time budget for provider calls during shutdown); its armed launch
    token expires with the provider lifetime cap that also reaps the
    sandbox.

    :returns: None once every task has settled (cancellations and any
        in-flight failures are absorbed via ``return_exceptions``).
    """
    tasks = list(_managed_launch_tasks)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _provision_managed_sandbox(
    *,
    session_id: str,
    owner: str,
    sandbox_config: HostDeployment,
    repos: Sequence[RepoWorkspace],
    tracker: ManagedLaunchTracker,
    host_store: HostStore,
    host_to_relaunch: Host | None,
    provider: str | None = None,
    agent_name: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> HostLaunch | None:
    """
    Run the provision phase of a background managed launch.

    Dispatches to :func:`relaunch_host` (existing host row)
    or :func:`launch_host` (fresh identity) and converts any
    failure into a settled tracker entry — the background task has no
    caller to raise to.

    :param session_id: Session/conversation identifier.
    :param owner: User the managed host acts for.
    :param sandbox_config: The deployment's sandbox config.
    :param repos: Repository workspaces to clone (empty for none).
    :param tracker: The app's launch tracker (failed here on error).
    :param host_store: Persistent host registrations.
    :param host_to_relaunch: Existing host row for a relaunch, or
        ``None`` for a first launch.
    :param provider: Requested host provider for a first launch, or
        ``None`` for the default. Ignored on a relaunch, which stays on
        the host row's recorded provider.
    :param agent_name: Server-resolved built-in agent name the session
        runs, passed to providers that classify runners by agent, or
        ``None`` to leave it unstamped.
    :param labels: The session's current labels, forwarded to the launcher's
        ``prepare_for_launch`` — see :func:`omnigent.server.hosts.launch_host`.
    :returns: The launch result, or ``None`` when the launch failed
        (the tracker entry is already settled with the reason).
    """
    from omnigent.server.hosts import launch_host, relaunch_host

    def _on_stage(stage: str) -> None:
        """
        Relay a launch-pipeline stage to the session's progress surface.

        Passed into the launch helpers, which may invoke it from the
        worker thread their sandbox exec steps run on —
        :func:`_publish_sandbox_status` is thread-safe.

        :param stage: The stage just entered, e.g. ``"cloning"``.
        """
        _publish_sandbox_status(session_id, stage)

    try:
        if host_to_relaunch is not None:
            return await relaunch_host(
                config=sandbox_config,
                host=host_to_relaunch,
                host_store=host_store,
                repos=repos,
                agent_name=agent_name,
                labels=labels,
                on_stage=_on_stage,
            )
        return await launch_host(
            config=sandbox_config,
            owner=owner,
            host_store=host_store,
            repos=repos,
            provider=provider,
            agent_name=agent_name,
            labels=labels,
            on_stage=_on_stage,
        )
    except HTTPException as exc:
        _logger.warning(
            "Managed sandbox launch failed for session %s: %s",
            session_id,
            exc.detail,
            extra={"session_id": session_id},
        )
        tracker.fail(session_id, str(exc.detail))
        _publish_sandbox_status(session_id, "failed", str(exc.detail))
        return None
    except Exception:  # noqa: BLE001
        # Broad on purpose: this is a fire-and-forget task — an
        # unexpected error must settle the tracker (or a waiting
        # message POST hangs until its timeout) and must not escape
        # as an unhandled-task traceback.
        _logger.exception(
            "Managed sandbox launch crashed for session %s",
            session_id,
            extra={"session_id": session_id},
        )
        tracker.fail(session_id, "internal error during managed sandbox launch")
        _publish_sandbox_status(
            session_id, "failed", "internal error during managed sandbox launch"
        )
        return None


async def _wait_for_managed_runner_tunnel(
    session_id: str,
    runner_id: str,
    tunnel_registry: TunnelRegistry,
    tracker: ManagedLaunchTracker,
) -> bool:
    """
    Wait for a launched managed runner to connect, failing the launch on timeout.

    :param session_id: Session/conversation identifier.
    :param runner_id: Runner id returned by the host launch frame.
    :param tunnel_registry: Runner tunnel registry to wait on.
    :param tracker: Managed launch tracker to settle on failure.
    :returns: ``True`` when the runner connected; ``False`` after publishing
        and retaining a failed launch status.
    """
    from omnigent.server.routes import sessions as _facade

    runner = await tunnel_registry.wait_for_runner(
        runner_id,
        timeout_s=_facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
    )
    if runner is not None:
        return True
    _logger.error(
        "Managed runner connection timed out",
        extra=debug_event(
            "runner_connect_failed",
            session_id=session_id,
            runner_id=runner_id,
            stage="runner_connect",
            error_code="runner_connect_timeout",
        ),
    )
    reason = "managed runner did not connect after launch"
    tracker.fail(session_id, reason)
    _publish_sandbox_status(session_id, "failed", reason)
    return False


async def _await_settled_managed_launch(launch: ManagedLaunch) -> None:
    """
    Block until a managed launch settles, raising its failure.

    The rendezvous a message POST takes when it races a background
    managed launch (create-time provisioning or a dead-sandbox
    relaunch): resolve as soon as the launch settles, surface the
    recorded reason when it failed, and give up with a clear retry
    hint when the launch outlives the rendezvous budget.

    :param launch: The session's tracker entry.
    :raises OmnigentError: 503 when the launch failed or is still
        running at the timeout.
    """
    from omnigent.server.hosts import MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S

    try:
        await asyncio.wait_for(
            launch.settled.wait(),
            timeout=MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise OmnigentError(
            "The session's managed sandbox is still provisioning; try again shortly",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from None
    if launch.error is not None:
        raise OmnigentError(
            f"The session's managed sandbox failed to launch: {launch.error}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


async def _get_runner_client_for_resource_access(
    *args: Any, **kwargs: Any
) -> httpx.AsyncClient | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._get_runner_client_for_resource_access(*args, **kwargs)


async def _get_runner_client_for_resource_access_impl(
    session_id: str,
    *,
    conversation: Conversation | None = None,
) -> httpx.AsyncClient | None:
    """Return the authoritative runner client for session resources.

    Requires the session to be bound to a runner via
    ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise. If no
    runner router is configured (unit-test/in-process setups), callers
    may fall back to local registries. ``conversation`` reuses the row
    already loaded during authorization.
    """
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_router = get_runner_router()
    if runner_router is not None:
        if conversation is None:
            routed_runner = runner_router.client_for_session_resources(session_id)
        else:
            routed_runner = runner_router.client_for_session_resources(
                session_id,
                conversation=conversation,
            )
        return routed_runner.client
    return cast("httpx.AsyncClient | None", get_runner_client())


# Client-safe message for a session whose bound agent no longer resolves.
# Mirrors the native-terminal payload's wording: never forward the runner's
# internal resolver text, which names the resolver and the raw agent id.
_SESSION_AGENT_MISSING_CLIENT_MESSAGE = (
    "This session's agent is no longer available; it was deleted or "
    "replaced. Recreate the agent or start a new session, then retry."
)


def _raise_if_session_agent_missing_payload(payload: object) -> None:
    """Re-raise a runner ``session_agent_missing`` error body as a typed 410.

    Inspects an already-parsed runner error body for the typed
    ``session_agent_missing`` code and re-derives the ``OmnigentError``
    (``http_status`` 410, matching ``create_session_terminal``'s code
    passthrough) so server proxies surface the session-lifecycle condition
    instead of flattening it into a generic gateway failure or forwarding
    the runner's raw message. Uses a fixed client-safe message — never the
    runner's internal resolver text. No-op for any other body or code.

    :param payload: Parsed runner response body, e.g. ``resp.json()``.
    :raises OmnigentError: Typed ``session_agent_missing`` (HTTP 410).
    """
    if not isinstance(payload, dict):
        return
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code") == ErrorCode.SESSION_AGENT_MISSING:
        raise OmnigentError(
            _SESSION_AGENT_MISSING_CLIENT_MESSAGE,
            code=ErrorCode.SESSION_AGENT_MISSING,
        )


def _raise_if_runner_session_agent_missing(resp: httpx.Response) -> None:
    """Re-raise a runner ``session_agent_missing`` error as a typed 410.

    Response-level wrapper over
    :func:`_raise_if_session_agent_missing_payload` for proxies that hold
    the raw ``httpx.Response``. No-op for a non-JSON payload.

    :param resp: Runner HTTP response with a non-2xx status.
    :raises OmnigentError: Typed ``session_agent_missing`` (HTTP 410).
    """
    try:
        payload: object = resp.json()
    except ValueError:
        return
    _raise_if_session_agent_missing_payload(payload)


async def _proxy_get_session_resources_to_runner(
    runner_client: httpx.AsyncClient,
    session_id: str,
    resource_type: str | None = None,
) -> SessionResourcePaginatedList:
    """Proxy ``GET /resources`` to the runner with strict validation.

    :param runner_client: HTTP client bound to the session's runner.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param resource_type: Optional ``?type=`` filter forwarded to the
        runner, e.g. ``"environment"``. ``None`` returns all types.
    :returns: The runner's validated resource page.
    :raises OmnigentError: Typed ``session_agent_missing`` (410) re-derived
        from the runner body when the session's agent is gone.
    :raises HTTPException: 502 on any other runner failure or malformed
        response.
    """
    try:
        resp = await runner_client.get(
            f"/v1/sessions/{session_id}/resources",
            # Runner-side list_session_resources applies the type filter.
            params={"type": resource_type} if resource_type else None,
            timeout=10.0,
        )
        if resp.status_code != 200:
            # Re-derive the typed session-lifecycle 410 (agent deleted or
            # rebound) instead of flattening it to a generic 502.
            _raise_if_runner_session_agent_missing(resp)
            _logger.warning(
                "session resources: runner returned %d for session=%s",
                resp.status_code,
                session_id,
                extra={"session_id": session_id},
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint failed",
            )

        try:
            body = resp.json()
            if not isinstance(body, dict):
                raise TypeError("response body must be an object")
            page = SessionResourceListPage.model_validate(body)
        except (TypeError, ValueError, ValidationError) as exc:
            _logger.warning(
                "session resources: malformed runner response for session=%s: %s",
                session_id,
                exc,
                extra={"session_id": session_id},
            )
            raise HTTPException(
                status_code=502,
                detail="runner session-resources endpoint returned malformed response",
            ) from exc

        return SessionResourcePaginatedList(
            data=page.data,
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )
    except HTTPException:
        raise
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.warning(
            "session resources: runner call failed for session=%s (%s)",
            session_id,
            exc,
            extra={"session_id": session_id},
        )
        raise HTTPException(
            status_code=502,
            detail="runner session-resources endpoint unavailable",
        ) from exc


async def _reset_runner_resources_after_switch(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._reset_runner_resources_after_switch(*args, **kwargs)


async def _reset_runner_resources_after_switch_impl(session_id: str) -> None:
    """Best-effort reset of the session's runner-side state after a switch.

    Run as a fire-and-forget background task by the switch-agent route. Calls
    the runner's dedicated ``POST /v1/sessions/{id}/reset-state`` endpoint,
    which closes the cached primary OSEnv + terminals AND drops the
    spec-derived session caches. Two reasons:

    1. **Sandbox correctness.** The primary OSEnv (which backs the web-UI
       filesystem / shell endpoints) is materialized once per session from the
       *original* agent's spec and cached. Closing it AND invalidating the
       spec/snapshot caches forces the next access to re-resolve and
       re-materialize from the NEW agent's spec, so those endpoints run
       under the switched-to agent's ``os_env``/sandbox — not the old one.
       (Agent ``sys_os_*`` tool calls already re-derive os_env per call, and
       native terminals re-evaluate the sandbox gate on respawn; this closes
       the remaining stale path.)
    2. **Terminal rebuild.** A lingering native terminal would otherwise shadow
       the switch-back transcript rebuild (auto-create skips while one exists).

    A dedicated endpoint (rather than ``DELETE /resources``) keeps the
    session-deletion contract untouched — deletion never needs the
    switch-specific cache reset.

    A switch only runs while the session is idle, so closing the env + terminal
    here is safe — unlike doing it inside the next turn's dispatch, which wedges
    that turn. cwd is re-derived from the runner's bound workspace, so the
    working directory / git worktree is preserved (only the sandbox changes;
    a ``fork``/``start_in_scratch`` agent gets a fresh scratch copy). The
    claude-native auto-create gate remains the switch-back safety net if this
    call is lost (runner offline, races).

    :param session_id: Session/conversation id just switched, e.g.
        ``"conv_abc123"``.
    :returns: None.
    """
    try:
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is None:
            return
        reset_resp = await runner_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/reset-state",
            timeout=15.0,
        )
        # httpx only raises on transport errors — a 4xx/5xx reset response
        # still returns. A non-2xx means the runner did NOT close the old
        # env, so it must take the failure path below (suppressing the
        # invalidation publish); HTTPStatusError is an httpx.HTTPError.
        reset_resp.raise_for_status()
    except (httpx.HTTPError, HTTPException, OmnigentError, RuntimeError):
        # Best-effort: a runner hiccup must not break the (already-committed)
        # switch. OmnigentError covers the session-not-runner-bound / runner-
        # offline case raised by _get_runner_client_for_resource_access. The
        # auto-create gate rebuilds on switch-back regardless. No
        # changed-files event on this path either: the runner's env cache is
        # still the OLD agent's, so a triggered refetch would re-serve it —
        # and a lost runner rebuilds from the new spec on relaunch anyway.
        _logger.warning(
            "post-switch runner-resource reset failed for session=%s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return
    # The old agent's cached OSEnv is now closed, so a refetch triggered by
    # this event re-materializes filesystem state from the NEW agent's spec.
    # This is what flips the web Files tab when the switch crosses an
    # os_env boundary (none→some shows it, some→none hides it) — the
    # session.agent_changed event fires before the reset and so cannot
    # carry a trustworthy availability signal.
    _publish_changed_files_invalidated(session_id)


def _publish_error_event(session_id: str, error: ErrorData) -> None:
    """
    Publish a live ``response.error`` event for a persisted error item.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param error: Durable error payload to mirror into SSE.
    :returns: None.
    """
    event = ErrorEvent(
        type="response.error",
        source=error.source,
        error=RetryErrorDetail(code=error.code, message=error.message),
    )
    session_stream.publish(session_id, event.model_dump())


async def _forward_session_change_to_runner(
    *args: Any, **kwargs: Any
) -> _RunnerForwardResult | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._forward_session_change_to_runner(*args, **kwargs)


#: Forward budget for a control event the runner answers by driving the TUI.
#: The claude-native slash-command injector's worst case is ~16s (1s tmux
#: advertisement + 5s commit-poll + 10s submit-verify; the 4s dialog watch
#: only runs after a fast verify), so the default 5s budget could time out on
#: a legitimately-still-working injection and report a failure that did not
#: happen.
_TUI_INJECT_FORWARD_TIMEOUT_S = 20.0


async def _forward_session_change_to_runner_impl(
    session_id: str,
    runner_router: Any,
    event: dict[str, Any],
    timeout_s: float = 5.0,
) -> _RunnerForwardResult | None:
    """
    Best-effort POST a control event to the bound runner.

    Used for control inputs the runner dispatches by harness in its
    ``/v1/sessions/{id}/events`` handler — claude-native injects the
    corresponding slash command into the tmux pane; other harnesses
    return 204 no-op. Two kinds of caller use this:

    * PATCH-driven harness notifications (``effort_change``,
      ``model_change``) — claude-native injects the slash command,
      other harnesses re-read the persisted value at the next turn
      boundary, so they ignore the return value.
    * Explicit ``compact`` — the caller inspects the returned status
      to decide whether the runner handled the control (claude-native,
      200) or the Omnigent server must run its own in-process compaction
      (204 / no runner). See the ``compact`` branch in
      :func:`post_event`.

    Mirrors the interrupt-forward fallback chain: prefer the per-
    session router binding, fall back to the global runner client
    (in-process / test setups where the router hasn't bound the
    session). When neither resolves to a client, the POST is silently
    skipped — the persisted value on the Omnigent side is the authoritative
    fallback, picked up by the next spawn.

    Non-2xx runner responses (e.g. 503 when the tmux pane isn't
    advertised yet) are logged as warnings so the failure surfaces
    in the Omnigent log — otherwise the POST succeeds at the httpx layer
    and the status would be silently dropped.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :param event: The ``/events`` POST body, e.g.
        ``{"type": "effort_change", "effort": "high"}``,
        ``{"type": "model_change", "model": "claude-opus-4-7"}``, or
        ``{"type": "compact"}``.
    :param timeout_s: Request budget, e.g. ``5.0``. Callers whose event the
        runner answers by driving the TUI pass
        :data:`_TUI_INJECT_FORWARD_TIMEOUT_S`.
    :returns: The runner's HTTP status/body, or ``None`` when no
        runner client could be resolved or the POST failed at the
        transport layer (in both cases the AP-side persisted value /
        operation is the authoritative fallback).
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return None
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=event,
            timeout=timeout_s,
        )
    except (httpx.HTTPError, ConnectionError):
        # Transport-level miss (runner asleep, tunnel still reconnecting). The
        # persisted AP-side value stays authoritative and the runner re-reads it,
        # so this is a recovered condition — WARNING, matching the non-2xx branch
        # below rather than out-ranking it.
        _logger.warning(
            "Session-change forward did not reach the runner for session=%r type=%r; "
            "the persisted value remains authoritative",
            session_id,
            event.get("type"),
            exc_info=True,
            extra={"session_id": session_id},
        )
        return None
    if resp.status_code >= 400:
        _logger.warning(
            "Session-change forward rejected for session=%s type=%r status=%s body=%s",
            session_id,
            event.get("type"),
            resp.status_code,
            resp.text,
            extra={"session_id": session_id},
        )
    return _RunnerForwardResult(status_code=resp.status_code, body=resp.text)


async def _stop_session_via_runner(*args: Any, **kwargs: Any) -> bool:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._stop_session_via_runner(*args, **kwargs)


async def _stop_session_via_runner_impl(
    session_id: str,
    runner_router: Any,
) -> bool:
    """
    Forward a ``stop_session`` request to the bound runner, surfacing
    failures to the caller instead of swallowing them.

    Unlike :func:`_forward_session_change_to_runner` (used for
    ``effort_change`` / ``model_change``, where a dropped forward is
    benign — the runner re-reads the persisted value at the next turn),
    a failed ``stop_session`` means the session is *still alive*. The
    web UI's "Stop session" action is destructive and treats a 2xx as
    success (it closes the confirmation dialog), so a swallowed failure
    would tell the user the session stopped when it did not. This
    helper therefore raises on a transport error or non-2xx runner
    response.

    Runner-client resolution mirrors the best-effort helper's fallback
    chain: prefer the per-session router binding, fall back to the
    global runner client (in-process / test setups). When neither
    resolves to a client there is no live runner bound — the session is
    not running on any runner, so the stop is a no-op success and this
    returns ``False`` without raising (the caller uses that to discard
    the turn fence it installed, since no runner means nothing else
    would ever lift it).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param runner_router: The session's ``RunnerRouter`` (may be
        ``None`` in tests / in-process setups).
    :returns: ``True`` if the stop was delivered to a runner (2xx),
        ``False`` if no runner client resolved (nothing forwarded).
    :raises OmnigentError: ``RUNNER_UNAVAILABLE`` (HTTP 503) if the
        runner could not be reached or reported a non-2xx — e.g. the
        claude-native tmux pane is wedged and ``kill_session`` failed.
        The web UI maps this to a visible "stop failed" state rather
        than closing the dialog as if the session stopped.
    """
    from omnigent.runtime import get_runner_client

    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return False
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": _STOP_SESSION_TYPE},
            timeout=5.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        # WSTunnelTransport raises bare ConnectionError on tunnel close.
        raise OmnigentError(
            f"Could not reach the runner to stop session {session_id!r}: {exc}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    if resp.status_code >= 400:
        raise OmnigentError(
            f"Runner failed to stop session {session_id!r} "
            f"(status {resp.status_code}): {resp.text}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    return True


async def _stop_session_host_runner(
    session_id: str,
    host_id: str,
    runner_id: str,
    host_registry: Any,
    *,
    expect_already_stopped: bool = False,
) -> bool:
    """
    Terminate the host-launched runner backing a host-spawned session.

    "Stop session" on a host-spawned session must end the dedicated runner
    subprocess the host launched for it — there is exactly one runner per
    host-launched session (see ``POST /v1/hosts/{host_id}/runners`` and the
    host-launch branch of session create). Killing the ``claude`` tmux pane
    via :func:`_stop_session_via_runner` is not enough on its own: the
    runner stays connected, so ``GET /health`` keeps reporting
    ``runner_online: true`` for the session and the web UI never shows it as
    disconnected — new messages are accepted and hang on "working" against a
    dead pane.

    Bringing the runner's tunnel down is what flips ``runner_online`` to
    ``false``; ``_on_runner_disconnect`` then marks the session and the web
    UI renders the "Agent disconnected — click to show reconnect command"
    banner, identical to the end state a CLI-launched session reaches when
    its process exits.

    Best-effort by design: the pane is already gone before this runs, so a
    host that is offline, was replaced, or is slow to acknowledge is logged
    and swallowed rather than failing the whole Stop. In the common case —
    the host's ``omnigent host`` tunnel is open while the user drives
    the web UI — the stop is delivered and the runner exits. The runner this
    targets is read from the caller's own (owner-gated) session row, so it
    can only ever stop the runner bound to that session.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param host_id: Owning host identifier from the session row, e.g.
        ``"host_a1b2c3d4..."``.
    :param runner_id: Runner bound to the session, e.g.
        ``"runner_token_abc123..."``.
    :param host_registry: The :class:`HostRegistry` tracking live host
        tunnels on this replica, or ``None`` when host support is not wired
        (in-process / test setups without a host tunnel).
    :param expect_already_stopped: Log a host-reported ``failed`` at debug
        instead of warning, for callers that race another reaper for the same
        runner (the relaunch belt: see
        :func:`_spawn_superseded_runner_stop`). Delivery failures still warn.
    :returns: ``True`` when the stop was delivered and acknowledged (the
        runner is exiting, so a tunnel drop is expected); ``False`` on any
        best-effort early-out (no host registry, host offline/replaced,
        ack timeout, or host-reported failure) where the runner may keep
        running and no tunnel drop will follow.
    """
    if host_registry is None:
        return False
    conn = host_registry.get(host_id)
    if conn is None:
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s is offline; "
            "the runner may linger online and the session will not show as "
            "disconnected",
            runner_id,
            session_id,
            host_id,
            extra={"session_id": session_id},
        )
        return False
    from omnigent.host.frames import HostStopRunnerFrame, encode_host_frame

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
    conn.pending_stops[request_id] = future
    stop_frame = encode_host_frame(
        HostStopRunnerFrame(request_id=request_id, runner_id=runner_id),
    )
    try:
        host_registry.send_text(conn, stop_frame)
    except ConnectionError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Cannot stop runner %s for session %s: host %s connection was replaced",
            runner_id,
            session_id,
            host_id,
            extra={"session_id": session_id},
        )
        return False
    try:
        result = await asyncio.wait_for(
            future,
            timeout=_STOP_RUNNER_RESULT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        conn.pending_stops.pop(request_id, None)
        _logger.warning(
            "Host %s did not acknowledge stop of runner %s for session %s",
            host_id,
            runner_id,
            session_id,
            extra={"session_id": session_id},
        )
        return False
    if result.get("status") == "failed":
        # An unknown runner means someone already reaped it. Expected for the
        # relaunch belt, which the host's own supersession normally beats, so
        # a warning there would report a successful reap as a failure.
        (_logger.debug if expect_already_stopped else _logger.warning)(
            "Host %s failed to stop runner %s for session %s: %s",
            host_id,
            runner_id,
            session_id,
            result.get("error"),
            extra={"session_id": session_id},
        )
        return False
    return True


def _build_new_item(
    body: SessionEventInput,
    response_id: str,
    created_by: str | None = None,
) -> NewConversationItem:
    """
    Construct a :class:`NewConversationItem` from a POSTed event.

    Validates the data payload via ``parse_item_data`` and wraps the
    result with the response_id linkage required by the conversation
    store.

    The item *type* is checked at the route boundary, but ``data`` is a
    free-form dict there, so a caller can name a known type and omit the
    fields it requires — ``{"type": "message"}`` with no ``role`` or
    ``content`` is the shape seen in production. That is bad input, so the
    raised ``ValidationError`` becomes an
    :class:`~omnigent.errors.OmnigentError` the caller can act on rather
    than escaping as an unhandled 500.

    :param body: Validated event input — guaranteed to be a known
        item type (the route checked ``_ALLOWED_EVENT_TYPES``).
    :param response_id: The task id the new item should be tagged
        with — either the steered active task or a freshly-created
        one.
    :param created_by: Authenticated identity of the actor posting
        the event, recorded for per-message attribution. ``None`` in
        single-user mode.
    :returns: A :class:`NewConversationItem` ready for delivery
        or persistence.
    :raises OmnigentError: When ``body.data`` does not satisfy the
        payload schema for ``body.type``.
    """
    try:
        data = parse_item_data(body.type, {"type": body.type, **body.data})
    except ValidationError as exc:
        raise OmnigentError(
            f"invalid data for {body.type!r} item: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return NewConversationItem(
        type=body.type,
        response_id=response_id,
        data=data,
        created_by=created_by,
    )


def _parse_skill_slash_command(body: SessionEventInput) -> tuple[str, str]:
    """
    Validate and unpack a structured skill slash-command event.

    The REPL posts ``type="slash_command"`` for skill invocations.
    Other command kinds are surfaced by terminal transcript bridges
    through ``external_conversation_item`` and are not executable
    session inputs on this route.

    :param body: Validated event input with ``type="slash_command"``
        and data such as ``{"kind": "skill", "name": "grill-me",
        "arguments": "review this plan"}``.
    :returns: ``(skill_name, arguments)`` with whitespace-trimmed
        command name and raw argument text.
    :raises OmnigentError: If the payload is not a skill command
        or is missing a usable skill name / arguments string.
    """
    kind = body.data.get("kind", "skill")
    if kind != "skill":
        raise OmnigentError(
            "slash_command events only support kind='skill'; use the "
            "dedicated control event for built-in commands",
            code=ErrorCode.INVALID_INPUT,
        )
    name = body.data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise OmnigentError(
            "slash_command requires non-empty data.name",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments = body.data.get("arguments", "")
    if not isinstance(arguments, str):
        raise OmnigentError(
            "slash_command data.arguments must be a string",
            code=ErrorCode.INVALID_INPUT,
        )
    return name.strip(), arguments


def _build_skill_slash_command_policy_body(body: SessionEventInput) -> SessionEventInput:
    """
    Build the user-message shape used for input policy evaluation.

    Skill commands inject a hidden meta message containing the full
    skill body, but input guardrails should evaluate the text the user
    actually typed, not the skill instructions maintained by the
    server. This preserves the legacy policy surface of
    ``/<skill> <arguments>`` without making bundled skill content
    policy-sensitive.

    :param body: Validated ``slash_command`` event body with data such
        as ``{"name": "grill-me", "arguments": "review this plan"}``.
    :returns: Synthetic user ``message`` event for policy evaluation.
    :raises OmnigentError: If the slash-command payload is invalid.
    """
    skill_name, arguments = _parse_skill_slash_command(body)
    command_text = f"/{skill_name}" if not arguments else f"/{skill_name} {arguments}"
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": command_text}],
        },
    )


async def _resolve_skill_meta_text_via_runner(
    session_id: str,
    skill_name: str,
    arguments: str,
    runner_client: httpx.AsyncClient,
) -> str:
    """
    Resolve a skill's hidden ``<skill>`` meta text on the bound runner.

    Skill content is runner-owned: the runner reads the ``SKILL.md``
    body and resource files from the skill's directory on its own
    filesystem, so the embedded ``<path>`` and resource listing are
    valid where the harness executes. Wraps
    ``POST /v1/sessions/{id}/skills/resolve``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param skill_name: Exact skill name to resolve, e.g.
        ``"code-review"``.
    :param arguments: Raw argument string typed after the slash
        command, e.g. ``"review this plan"``. Empty when none.
    :param runner_client: HTTP client pointed at the bound runner.
    :returns: The hidden ``<skill>`` meta text for a single
        ``input_text`` block.
    :raises OmnigentError: If the skill is not exposed for the session
        (the runner 404s with the available list), or the runner is
        unreachable / errors while resolving.
    """
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/skills/resolve",
            json={"name": skill_name, "arguments": arguments},
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError) as exc:
        raise OmnigentError(
            f"Runner unreachable while resolving skill {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code not in (200, 404):
        # Re-derive the typed session-lifecycle 410 (agent deleted or
        # rebound) instead of flattening it into an INTERNAL_ERROR 500 —
        # the exact server-fault mis-attribution this code path must avoid.
        # This branch is reached because SESSION_AGENT_MISSING maps to 410
        # (non-404); if that HTTP mapping ever changed to 404, the 404 arm
        # below would swallow it as a skill-not-found INVALID_INPUT.
        _raise_if_runner_session_agent_missing(resp)
        raise OmnigentError(
            f"Runner failed to resolve skill {skill_name!r}: HTTP {resp.status_code}",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Parse the body once, guarded: a transport proxy / HTML error page /
    # non-object body must surface as a controlled runner failure, not an
    # uncaught 500.
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("expected a JSON object")
    except ValueError as exc:
        raise OmnigentError(
            f"Runner returned a malformed skill resolution for {skill_name!r}: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    if resp.status_code == 404:
        available = payload.get("available", [])
        raise OmnigentError(
            f"Skill {skill_name!r} not found. Available skills: {available}",
            code=ErrorCode.INVALID_INPUT,
        )
    meta_text = payload.get("meta_text")
    if not isinstance(meta_text, str):
        raise OmnigentError(
            f"Runner returned malformed skill resolution for {skill_name!r}: missing 'meta_text'",
            code=ErrorCode.INTERNAL_ERROR,
        )
    return meta_text


async def _dispatch_skill_slash_command_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent: Agent,
    has_mcp_servers: bool,
    created_by: str | None,
) -> str:
    """
    Persist a skill slash command and forward hidden skill context.

    Skill content is runner-owned: this asks the bound runner to
    resolve the skill (``POST /v1/sessions/{id}/skills/resolve``) into
    its ``<skill>`` meta text, reading the ``SKILL.md`` body and
    resource files from the skill's directory *on the runner* — so the
    embedded ``<path>`` and resource listing are valid where the harness
    executes. The server then persists the result (runner-resolves,
    server-persists). Appends two conversation items with the same
    response id:

    * a visible ``slash_command`` item for the UI transcript;
    * a hidden ``message`` item with ``is_meta=True`` containing the
      full skill instructions for runner history replay.

    Only the hidden message is sent to the runner as input. The visible
    command is published as ``response.output_item.done`` after the
    runner accepts the event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for ``session_id``.
    :param body: Structured ``slash_command`` event body.
    :param conversation_store: Store used to append both durable
        items.
    :param runner_client: HTTP client pointed at the bound runner.
    :param agent: Agent bound to the conversation.
    :param has_mcp_servers: ``True`` when the agent spec declares MCP
        servers; forwarded unchanged to the runner event.
    :param created_by: Authenticated actor id, e.g.
        ``"alice@example.com"``, or ``None`` in single-user mode.
    :returns: The persisted visible ``slash_command`` item id.
    :raises OmnigentError: If the skill is not exposed for the
        session, or the runner is unreachable while resolving it.
    """
    import uuid

    skill_name, arguments = _parse_skill_slash_command(body)
    meta_text = await _resolve_skill_meta_text_via_runner(
        session_id,
        skill_name,
        arguments,
        runner_client,
    )

    response_id = f"turn_{uuid.uuid4().hex}"
    meta_content = [{"type": "input_text", "text": meta_text}]
    visible_item = NewConversationItem(
        type=_SLASH_COMMAND_TYPE,
        response_id=response_id,
        data=SlashCommandData(
            agent=agent.name,
            kind="skill",
            name=skill_name,
            arguments=arguments,
        ),
        created_by=created_by,
    )
    meta_item = NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="user",
            content=meta_content,
            is_meta=True,
        ),
        created_by=created_by,
    )
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [visible_item, meta_item],
    )
    visible = persisted_items[0]

    # Mirror the plain-message path's title seeding: a session whose FIRST
    # message is a skill invocation (web landing composer, REPL) would
    # otherwise keep a NULL title and the sidebar falls back to the
    # conversation id. Titled from the typed command ("/debate kafka…"),
    # NOT the hidden meta item — that's the full SKILL.md instruction blob.
    command_text = f"/{skill_name} {arguments}" if arguments else f"/{skill_name}"
    await _seed_missing_title(
        conv,
        [{"type": "input_text", "text": command_text}],
        conversation_store,
    )

    runner_body: dict[str, Any] = {
        "type": "message",
        "role": "user",
        "content": meta_content,
        "agent_id": conv.agent_id,
        "model": agent.name,
        "has_mcp_servers": has_mcp_servers,
        # Live-renderer hint: the runner drops ``browser_*`` schemas for
        # the turn when no renderer is subscribed to the session stream.
        "browser_renderer_available": session_stream.has_subscribers(session_id),
        # The forwarded message carries ``meta_content`` — i.e. the
        # META item (persisted_items[1]), not the user-visible item.
        # Hand the runner that id so a cold-cache reload drops the
        # right persisted copy (see _forward_event_to_runner).
        "persisted_item_id": persisted_items[1].id,
    }
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source. The
    # "auto" sentinel is resolved to a concrete harness at first-message
    # time and never forwarded verbatim.
    if conv.harness_override is not None and conv.harness_override != "auto":
        runner_body["harness_override"] = conv.harness_override

    try:
        await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=_RUNNER_FORWARD_TIMEOUT,
        )
        event = OutputItemDoneEvent(type="response.output_item.done", item=visible.to_api_dict())
        session_stream.publish(session_id, event.model_dump())
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.exception(
            "Forward of skill slash command failed for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        _publish_status(session_id, "idle")
        raise OmnigentError(
            "Runner is unreachable; message was persisted but could not be delivered. "
            "The runner may be restarting — retry or spawn a new session.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    return visible.id


def _title_content_from_item(
    item: NewConversationItem | ConversationItem,
) -> list[dict[str, Any]]:
    """
    Extract title candidate content blocks from a session item.

    User ``message`` items contribute their text. A Skill ``slash_command``
    item (``kind == "skill"``) contributes its typed command, e.g.
    ``"/my-plugin:my-skill ARG-123"`` — a Claude Code native session whose
    first action is a Skill arrives over the transcript bridge as a
    ``slash_command``, not a user ``message``, so without this it stays
    untitled and the sidebar falls back to the generic "Claude Code" label
    (#851). CLI built-ins (``kind == "command"`` — ``/clear``, ``/compact``,
    ``/model``, …) are excluded so a surfaced built-in never becomes the
    session title. Tool results and assistant-shaped messages return an empty
    list so callers leave the conversation title unchanged.

    :param item: The parsed item being persisted, e.g. a user
        ``"message"`` item with input text content.
    :returns: Content blocks that may contribute to a synthesized
        title, e.g. ``[{"type": "input_text", "text": "Hello"}]``.
    """
    if item.type == _SLASH_COMMAND_TYPE:
        # Title a Skill-first session from the typed command; skip surfaced CLI
        # built-ins (kind == "command") which aren't meaningful session topics.
        if not isinstance(item.data, SlashCommandData) or item.data.kind != "skill":
            return []
        command = f"/{item.data.name}"
        arguments = item.data.arguments.strip()
        text = f"{command} {arguments}" if arguments else command
        return [{"type": "input_text", "text": text}]
    if item.type != "message":
        return []
    if not isinstance(item.data, MessageData):
        return []
    if item.data.role != "user":
        return []
    if item.data.is_meta:
        return []
    return item.data.content


async def _seed_missing_title(
    conv: Conversation,
    content: list[dict[str, Any]],
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled conversation's title from message content blocks.

    No-op when the conversation already has a title or the blocks
    yield no usable text. Mutates ``conv.title`` in place on success
    so callers holding the row see the persisted value.

    :param conv: The conversation row for the session.
    :param content: Title-candidate blocks, e.g.
        ``[{"type": "input_text", "text": "/debate kafka vs sqs"}]``.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    if conv.title is not None:
        return
    title = synthesize_conversation_title(content)
    if title is None:
        return
    updated = await asyncio.to_thread(
        conversation_store.update_conversation,
        conv.id,
        title=title,
    )
    if updated is not None:
        conv.title = updated.title


async def _seed_missing_title_from_user_message(
    conv: Conversation,
    item: NewConversationItem,
    conversation_store: ConversationStore,
) -> None:
    """
    Set an untitled session's title from a user message.

    The app UI creates sessions with ``initial_items=[]`` and posts
    the first user message through ``POST /v1/sessions/{id}/events``.
    This helper also covers callers that pass initial items to
    ``POST /v1/sessions``. Non-user-message items are ignored, and
    already-titled conversations are left unchanged.

    :param conv: The conversation row for the session.
    :param item: The parsed item being persisted.
    :param conversation_store: Store used to persist the title.
    :returns: None.
    """
    await _seed_missing_title(conv, _title_content_from_item(item), conversation_store)


def _extract_user_text_for_routing(body: SessionEventInput) -> str:
    """Extract plain text from a user message event for the routing judge.

    Concatenates all ``input_text`` blocks in ``body.data["content"]``,
    returning the first 4 000 characters.  Returns ``""`` for non-message
    events or events with no text content.
    """
    content = body.data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "input_text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)[:4000]


async def _emit_server_routing_decision(
    session_id: str,
    conversation_store: ConversationStore,
    model: str,
    verdict: dict[str, Any],
    *,
    agent: str | None = None,
    scope: str = "turn",
    harness: str | None = None,
    decision_id: str | None = None,
    attempted_override: str | None = None,
) -> str | None:
    """Persist and publish a ``routing_decision`` transcript chip.

    Called by the server-side routing path before the turn is forwarded
    to the runner.  The chip shows the judge's model pick at turn start
    — the same UX the runner-side advisor produced, but driven entirely
    by the server.

    :param agent: Sub-agent name to include when mirroring a child
        session's routing decision into the parent's transcript.
    :param scope: What the decision governs, e.g. ``"child_session"``.
    :param harness: Harness the decision applies to, when it picked one.
    :param decision_id: Decision identity shared with the child-sessions
        API. ``None`` mints one.
    :param attempted_override: Model the spawning agent asked for and the
        router overrode — an LLM-supplied ``args.model``, or a native
        spawn's own ``requested_model``. ``None`` when nothing was asked
        for, or when the pick names the same arm as the ask.
    :returns: The decision id, so callers can join it onto the session
        row, or ``None`` when the payload failed validation and no chip
        was recorded.
    """
    import uuid

    rationale = verdict.get("rationale", "")
    applied = verdict.get("applied", True)
    resolved_decision_id = decision_id or str(uuid.uuid4())
    raw_model = verdict.get("raw_model")
    # Which router answered, so the chip can mark an AI-Gateway-routed decision.
    router_source = verdict.get("router_source")
    item_data: dict[str, Any] = {
        "model": model,
        "applied": bool(applied),
        "rationale": rationale if isinstance(rationale, str) else "",
        "scope": scope,
        "harness": harness,
        "decision_id": resolved_decision_id,
        "raw_model": raw_model if isinstance(raw_model, str) and raw_model else None,
        "attempted_override": attempted_override,
        "router_source": (
            router_source if isinstance(router_source, str) and router_source else None
        ),
    }
    if agent is not None:
        item_data["agent"] = agent
    try:
        parsed_data = parse_item_data("routing_decision", item_data)
    except (ValueError, TypeError):
        _logger.warning(
            "Server routing: failed to parse routing_decision data",
            extra={"session_id": session_id},
        )
        return None

    routing_item = NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=parsed_data,
    )
    try:
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [routing_item])
        persisted_id: str | None = persisted[0].id if persisted else None
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Server routing: routing_decision persist failed for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        persisted_id = None

    # Publish live event so the web UI renders the chip immediately.
    session_stream.publish(
        session_id,
        {
            "type": "response.output_item.done",
            "item": {
                "id": persisted_id,
                "type": "routing_decision",
                **item_data,
            },
        },
    )
    return resolved_decision_id


@dataclass
class _SessionEventDispatchResult:
    """
    Outcome of forwarding one item-event to the runner.

    :param item_id: Store-assigned id of the AP-persisted item, e.g.
        ``"item_abc123"``. ``None`` for the claude-native message
        bypass, which persists nothing AP-side.
    :param pending_id: Id of the :mod:`omnigent.runtime.pending_inputs`
        entry recorded for a native-terminal web message, e.g.
        ``"pending_a1b2c3"`` — surfaced to the sender so it can adopt
        the id and dedupe against the snapshot. ``None`` for non-native
        events (already persisted, so no separate pending entry).
    :param turn: The runner's number for the turn the message started or joined (its
        ``session.status`` edges carry the same number); ``None`` when it did not say.
    """

    item_id: str | None
    pending_id: str | None
    turn: int | None = None


def _extract_persistent_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Extract a persistable conversation item from a runner SSE event.

    Returns a ``NewConversationItem`` for:

    - ``response.output_item.done`` events carrying an assistant
      message, function_call, or function_call_output.
    - ``compaction`` events carrying a conversation summary from
      the runner's compaction system.

    Returns ``None`` for all other events (transient deltas, turn
    lifecycle, compaction progress indicators, etc.).

    :param event: Parsed SSE event dict from the runner stream.
    :param response_id: Turn-scoped id from the most recent
        ``response.in_progress`` event. All items persisted within
        the same turn share this id so the web UI can group them
        into a single bubble and pair function_calls with their
        outputs. Falls back to a fresh uuid when unavailable.
    :returns: A ``NewConversationItem`` ready for
        ``conv_store.append()``, or ``None``.
    """
    import uuid

    evt_type = event.get("type")

    if evt_type == "compaction":
        try:
            data = parse_item_data("compaction", event)
        except (ValueError, TypeError):
            _logger.warning("Failed to parse compaction item from SSE")
            return None

        return NewConversationItem(
            type="compaction",
            response_id=f"compact_{uuid.uuid4().hex}",
            data=data,
        )

    if evt_type != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in ("message", "function_call", "function_call_output"):
        return None
    # Skip transient observed function_call events (status
    # ``in_progress`` / ``action_required``).  Only ``completed``
    # function_calls are durable — the scaffold emits them after
    # the dispatch Future resolves.  Persisting interim statuses
    # creates orphan conversation items whose spinners never
    # resolve in the web UI.
    if item_type == "function_call" and item.get("status") != "completed":
        return None
    try:
        data = parse_item_data(item_type, item)
    except (ValueError, TypeError):
        _logger.warning(
            "Failed to parse persistent item from SSE: %s",
            item_type,
        )
        return None

    return NewConversationItem(
        type=item_type,
        response_id=response_id or f"turn_{uuid.uuid4().hex}",
        data=data,
    )


def _resource_event_item_from_sse(
    session_id: str,
    event: dict[str, Any],
) -> NewConversationItem | None:
    """
    Build a ``resource_event`` conversation item from a runner SSE event.

    The runner emits ``session.resource.created`` /
    ``session.resource.deleted`` when an agent tool
    (``sys_terminal_launch`` / ``sys_terminal_close``) materializes or
    tears down a session resource mid-turn. The relay republishes the
    raw event onto the live ``session_stream`` (so connected clients
    update instantly); this helper produces the durable conversation
    item so a client that reconnects mid-turn rediscovers the resource
    in the snapshot — matching the REST resource path
    (:func:`_publish_and_persist_resource_event`).

    Returns ``None`` for every other event type, and for malformed
    resource events (missing id / type) so a bad frame can't poison
    the relay.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param event: Parsed SSE event dict from the runner stream.
    :returns: A ``resource_event`` :class:`NewConversationItem`, or
        ``None``.
    """
    from omnigent.entities.conversation import ResourceEventData

    evt_type = event.get("type")
    if evt_type == "session.resource.created":
        resource = event.get("resource")
        if not isinstance(resource, dict):
            return None
        resource_id = resource.get("id")
        resource_type = resource.get("type")
    elif evt_type == "session.resource.deleted":
        resource = None
        resource_id = event.get("resource_id")
        resource_type = event.get("resource_type")
    else:
        return None

    # Require non-empty id/type. ``isinstance(x, str)`` alone admits
    # ``""``, which would persist a malformed resource_event item the
    # snapshot can't resolve back to a real resource. Drop the frame
    # instead — the snapshot endpoint stays the source of truth.
    if not resource_id or not isinstance(resource_id, str):
        return None
    if not resource_type or not isinstance(resource_type, str):
        return None

    return NewConversationItem(
        type="resource_event",
        response_id=session_id,
        data=ResourceEventData(
            event_type=evt_type,
            resource_id=resource_id,
            resource_type=resource_type,
            resource=resource,
        ),
    )


def _routing_decision_item_from_sse(
    event: dict[str, Any],
) -> NewConversationItem | None:
    """
    Build a ``routing_decision`` conversation item from a runner SSE event.

    The runner's cost advisor emits a ``response.output_item.done`` with a
    ``routing_decision`` item at the START of an advised turn (the
    intelligent model router's pick). This produces the durable,
    display-only transcript item so the pick survives reload at the right
    position (BEFORE the turn's assistant output); the relay also
    re-publishes a live event carrying the persisted item id so the live
    chip and a turn-start snapshot refetch dedup by the same id (no
    double render).

    Returns ``None`` for every other event, and for a malformed routing
    item (empty model) so a bad frame can't poison the relay.

    :param event: Parsed SSE event dict from the runner stream.
    :returns: A ``routing_decision`` :class:`NewConversationItem`, or
        ``None``.
    """
    if event.get("type") != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "routing_decision":
        return None
    try:
        data = parse_item_data("routing_decision", item)
    except (ValueError, TypeError):
        _logger.warning("Failed to parse routing_decision item from SSE")
        return None
    # No turn response_id exists yet (emitted before response.in_progress),
    # so stamp a fresh routing id — the chip renders as its own standalone
    # line at turn start.
    import uuid

    return NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=data,
    )


def _optional_error_text(value: object) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value.strip() else None


def _error_item_from_sse(
    event: dict[str, Any],
    response_id: str | None = None,
) -> NewConversationItem | None:
    """
    Build a durable ``error`` item from a runner error SSE event.

    The web UI already renders live ``response.error`` and
    ``response.failed`` error payloads as real error banners. This
    helper mirrors turn-scoped payloads into conversation history so the
    banner survives refresh/reconnect.

    A bare ``response.error`` emitted before ``response.in_progress`` is
    a session/startup signal, not a transcript turn. Leaving it live-only
    avoids creating an orphan banner at the top of the transcript; when
    a user sends a message into the failed native terminal, the AP-side
    fast-fail path records that user item and its sibling error in order.

    :param event: Parsed runner SSE event.
    :param response_id: Current response id, e.g. ``"resp_abc123"``.
        ``None`` means no turn is active.
    :returns: A ``type="error"`` item, or ``None`` when the event has
        no structured error payload or is not tied to a turn.
    """
    evt_type = event.get("type")
    raw_error: Any
    source = event.get("source")
    if evt_type == "response.error":
        if response_id is None:
            return None
        raw_error = event.get("error")
    elif evt_type == "response.failed":
        raw_response = event.get("response")
        raw_error = raw_response.get("error") if isinstance(raw_response, dict) else None
        if raw_error is None:
            raw_error = event.get("error")
        source = event.get("source") or "execution"
        if response_id is None and isinstance(raw_response, dict):
            raw_response_id = raw_response.get("id")
            if isinstance(raw_response_id, str) and raw_response_id:
                response_id = raw_response_id
    else:
        return None
    if response_id is None:
        return None
    if not isinstance(raw_error, dict):
        return None
    raw_code = raw_error.get("code")
    raw_message = raw_error.get("message")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return None
    if not isinstance(raw_message, str) or not raw_message.strip():
        return None
    if source not in ("llm", "execution", "tool", "harness"):
        source = "execution"
    return NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source=source,
            code=raw_code,
            message=raw_message,
            # A classified failure's headline and next step must survive a
            # reload, or the card loses its sign-in link once it comes from
            # history instead of the live stream.
            title=_optional_error_text(raw_error.get("title")),
            cause=_optional_error_text(raw_error.get("cause")),
            remediation=_optional_error_text(raw_error.get("remediation")),
        ),
    )


async def _relay_persist_error_once(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> Literal["persisted", "duplicate", "skipped", "failed"]:
    """
    Persist a runner error item unless the same error already exists.

    Native terminal startup can fail again on every runner reconnect.
    Dedupe by the visible payload ``(source, code, message)`` only
    when no user message has appeared since the matching error. That
    suppresses reconnect spam while still recording a new error for a
    user-initiated retry against the same broken terminal.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param item: The candidate ``type="error"`` item.
    :returns: ``"persisted"`` if this call appended the item,
        ``"duplicate"`` if a matching recent error already exists,
        ``"skipped"`` if no store or non-error item was provided, or
        ``"failed"`` if the store operation failed.
    """
    if conversation_store is None:
        return "skipped"
    if not isinstance(item.data, ErrorData):
        return "skipped"
    try:
        recent = await asyncio.to_thread(
            conversation_store.list_items,
            session_id,
            limit=20,
            order="desc",
        )
        for existing in recent.data:
            if (
                existing.type == "message"
                and isinstance(existing.data, MessageData)
                and existing.data.role == "user"
            ):
                break
            if existing.type != "error" or not isinstance(existing.data, ErrorData):
                continue
            if (
                existing.data.source == item.data.source
                and existing.data.code == item.data.code
                and existing.data.message == item.data.message
            ):
                return "duplicate"
        await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
        return "persisted"
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Relay error persist failed for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        return "failed"


async def _record_turn_failure(
    conversation_store: ConversationStore | None,
    session_id: str,
    failure: ErrorDetail,
    *,
    failure_origin: str,
) -> None:
    """
    Record a turn that failed before reaching a runner, and publish its end.

    The one place such a failure becomes visible: it stores a single ``error``
    item carrying a public code (``runner_unavailable`` reads as
    ``sandbox_unavailable``; deduplicated against a repeat with no user message
    in between), publishes a terminal ``response.failed`` event so streams that
    follow turn ends (the family stream's ``turn.done``) fire, then publishes the
    ``failed`` status.

    :param conversation_store: Store used for the durable append.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param failure: The failure, e.g. ``ErrorDetail(code="runner_unavailable", ...)``.
    :param failure_origin: Slug naming the failing path, for the status log.
    """
    turn_id = generate_task_id()
    error = ErrorData(
        source="execution",
        code=public_error_code(failure.code),
        message=failure.message,
    )
    persisted = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(type="error", response_id=turn_id, data=error),
    )
    if persisted == "persisted":
        _publish_error_event(session_id, error)
    session_stream.publish(
        session_id,
        {
            "type": "response.failed",
            "source": "execution",
            "response": {
                "id": turn_id,
                "object": "response",
                "status": "failed",
                "error": {"code": error.code, "message": error.message},
            },
        },
    )
    for on_failed in list(ON_TURN_FAILED.values()):
        try:
            await on_failed(session_id, error.message)
        except Exception:  # noqa: BLE001 - a feature's settling never fails the turn's end
            _logger.warning("turn-failed hook raised for %s", session_id, exc_info=True)
    _publish_status(session_id, "failed", failure, failure_origin=failure_origin)


async def _relay_persist(
    conversation_store: ConversationStore | None,
    session_id: str,
    item: NewConversationItem,
) -> None:
    """
    Persist a single conversation item from the relay.

    :param conversation_store: Store instance, or ``None`` to skip.
    :param session_id: Session/conversation identifier.
    :param item: The item to persist.
    """
    if conversation_store is None:
        return
    try:
        persisted = await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [item],
        )
        for stored in persisted:
            if not stored.deduplicated:
                await _signal_message_stored_async(session_id, stored)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Relay persist failed for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        if (
            item.type == "message"
            and isinstance(item.data, MessageData)
            and item.data.role == "assistant"
        ):
            # The person read the reply live; say it is not in the history rather than let it
            # vanish on the next read. Stored like any turn error, so the transcript shows it.
            error = ErrorData(
                source="execution",
                code=REPLY_NOT_SAVED,
                message="The assistant's reply could not be saved to the conversation.",
            )
            note = NewConversationItem(type="error", response_id=item.response_id, data=error)
            if (
                await _relay_persist_error_once(conversation_store, session_id, note)
                == "persisted"
            ):
                _publish_error_event(session_id, error)


async def _relay_response_policy_deny_reason(
    conversation_store: ConversationStore,
    session_id: str,
    text: str,
) -> str | None:
    """
    Evaluate *text* against the session's OUTPUT (RESPONSE) phase policies.

    Runner-relayed (scaffold) harnesses never POST the assistant message
    back through ``POST /v1/sessions/{id}/events``, so the
    ``Phase.RESPONSE`` evaluator there is unreachable for them. The relay's
    terminal text flush is their single persist point, so this evaluates the
    same output policies over the final assistant text right before it
    becomes durable — making a spec's ``response``-phase policy enforceable
    in the runner topology.

    Fails OPEN (returns ``None``) on any evaluation error, matching the LLM
    phases' advisory default: a policy-engine hiccup must not destroy the
    narration the user already watched.

    :param conversation_store: Store for the conversation/labels lookup.
    :param session_id: Session/conversation identifier.
    :param text: The joined assistant text segment about to persist.
    :returns: The deny reason when an output policy DENYs, else ``None``.
    """
    from omnigent.runtime._globals import _agent_store

    if _agent_store is None:
        # Fail open, but loudly: a mis-initialized runtime would otherwise
        # silently disable RESPONSE-phase gating for every relayed session.
        _logger.warning(
            "Relay: agent store not initialized; skipping RESPONSE-phase "
            "policy evaluation for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        return None
    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or conv.agent_id is None:
            return None
        body = SessionEventInput(
            type="message",
            data={
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        )
        # The relay has no HTTP caller; the acting principal is the
        # turn-initiating human persisted at forward time (same label the
        # policy-evaluate route falls back to), so per-user policies gate
        # on the correct actor.
        turn_actor = (conv.labels or {}).get(_TURN_ACTOR_LABEL)
        verdict = await _evaluate_output_policy(
            session_id,
            conv,
            body,
            conversation_store,
            _agent_store,
            None,
            actor=_build_actor(turn_actor),
        )
    except Exception:  # noqa: BLE001 — fail open: output phases are advisory on error
        _logger.exception(
            "Relay: RESPONSE-phase policy evaluation failed for session=%s; "
            "persisting the text unmodified",
            session_id,
            extra={"session_id": session_id},
        )
        return None
    if verdict is None:
        return None
    return str(verdict.get("reason") or "Denied by policy")


async def _flush_relay_text(
    conversation_store: ConversationStore | None,
    session_id: str,
    text_acc: list[str],
    response_id: str | None,
    model_id: str | None,
    *,
    deny_reason: str | None = None,
    evaluate_response_phase: bool = False,
) -> None:
    """
    Persist buffered assistant text as a message item and clear the buffer.

    Scaffold harnesses (claude-sdk) stream text deltas with no per-message
    ``output_item.done``, so the relay buffers them. Flushing at each
    text→function_call boundary (not only at ``response.completed``) keeps
    the persisted transcript interleaved — ``[text, tool, text, tool]`` —
    instead of collapsing a turn's narration into one block after its tool
    calls (which renders tools-above-text + run-on text on reload).

    After a confirmed persist the item is also published to the live
    stream as ``response.output_item.done`` (mirroring the native path's
    :func:`_publish_external_conversation_item`). Live clients already
    rendered the text from the deltas; the publish delivers the
    store-assigned item id so they can stamp it onto the streamed block.
    Without it the rendered block stays id-less and every reconnect's
    itemId-keyed reconciliation splices the persisted copy in as a
    duplicate. Clients must dedupe this event by CONTENT, not by
    open-section state: at a mid-turn tool-call boundary the streamed
    text has already been closed/committed client-side (by the
    function_call item or interleaved reasoning) before this publish
    arrives. The web stamps the id onto the matching streamed
    ``text_done`` block in place (web ``chatStore.ts``
    ``pumpStreamEvents``); the TUI consumes a byte-equal committed
    segment (``_repl.py`` ``_TurnProseTracker``).

    The buffer and the in-flight replay are cleared ONLY after the append
    is confirmed: clearing first would let a reconnect during the persist
    ``await`` see neither the (not-yet-committed) message nor the replay,
    dropping the narration — and a swallowed append failure would lose it
    permanently. On failure the buffers are left intact so the text still
    replays and is retried at the next flush / ``response.completed``.

    On an OUTPUT-phase DENY the denied text must never become a durable
    assistant message. Two deny sources feed this (both persist the same
    ``[Denied by policy: ...]`` sentinel the ``_evaluate_output_policy``
    route path uses):

    - *deny_reason*: the ``PHASE_LLM_RESPONSE`` DENY the policy-evaluate
      route recorded for this session's in-flight turn — the harness only
      errors the turn after the text already streamed, so the relay is
      the last gate before the denied content persists.
    - *evaluate_response_phase*: evaluate the joined text against the
      spec's ``Phase.RESPONSE`` policies right here. Runner-relayed
      harnesses never POST the assistant message back through
      ``POST .../events``, so this flush is the only place the
      ``response`` phase can fire in this topology.

    :param conversation_store: Store to append to, or ``None`` to skip
        persistence (test parsing path).
    :param session_id: Conversation/session id, e.g. ``"conv_abc123"``.
    :param text_acc: Accumulated delta strings; cleared in place on success.
    :param response_id: Turn id so the segment groups with its tool calls.
    :param model_id: Assistant agent label for the message.
    :param deny_reason: When set, an output policy already denied this
        turn's assistant text; persist the deny sentinel instead of it.
    :param evaluate_response_phase: When ``True`` (terminal flush), gate
        the text through the spec's RESPONSE-phase policies before
        persisting.
    """
    if not text_acc:
        return
    text = "".join(text_acc)
    if not text.strip():
        # Whitespace-only: nothing worth persisting. Drop it so it neither
        # accumulates into the next segment nor replays as an empty bubble.
        text_acc.clear()
        inflight_text.reset_text(session_id)
        return
    if conversation_store is None:
        text_acc.clear()
        return
    if deny_reason is None and evaluate_response_phase:
        deny_reason = await _relay_response_policy_deny_reason(
            conversation_store, session_id, text
        )
    if deny_reason is not None:
        # Substitute the sentinel for the denied content — same Option-B
        # shape as the ``_evaluate_output_policy`` route path, so follow-up
        # turns and the items API see a consistent deny record. Publish the
        # sentinel delta too: live clients already rendered the denied text
        # from the stream (that flash is the residual gap full buffering
        # would close), so without a visible sentinel the deny would only
        # be discoverable after a reload.
        text = f"{_DENY_SENTINEL_PREFIX}{deny_reason}]"
        # Commit the substitution into the retry buffer itself: a failed
        # persist below leaves ``text_acc`` for the next flush, and that
        # retry must carry the sentinel, never the denied content. Without
        # this, a RESPONSE-phase deny would be re-evaluated from scratch on
        # retry — and a stateful policy whose labels moved on the first
        # DENY could flip to ALLOW and leak the original text.
        text_acc[:] = [text]
        _publish_policy_deny(session_id, deny_reason)
    import uuid

    try:
        item = NewConversationItem(
            type="message",
            response_id=response_id or f"turn_{uuid.uuid4().hex}",
            data=parse_item_data(
                "message",
                {
                    "type": "message",
                    "role": "assistant",
                    "agent": model_id or "unknown",
                    "content": [{"type": "output_text", "text": text}],
                },
            ),
        )
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
    except Exception:  # noqa: BLE001
        # Keep text_acc + the in-flight buffer so the narration isn't lost:
        # it still replays on reconnect and is retried at the next flush.
        _logger.exception(
            "Relay: failed to persist assistant text segment for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        return
    # Confirmed persisted — now safe to clear. Synchronous (no await before
    # the next yield), so no reconnect observes the committed message and a
    # stale replay together.
    text_acc.clear()
    inflight_text.reset_text(session_id)
    # Publish the persisted item so live clients learn its store-assigned
    # id and stamp it onto the already-rendered streamed text (see the
    # docstring). Ordered before the boundary item / terminal event the
    # caller publishes next; clients match it back to the streamed text
    # by byte-equal content, not by open-section state.
    await _signal_message_stored_async(session_id, persisted[0])
    done_event = OutputItemDoneEvent(
        type="response.output_item.done",
        item=persisted[0].to_api_dict(),
    )
    session_stream.publish(session_id, done_event.model_dump())


def _agent_provider_family(agent: Agent) -> str | None:
    """Return the provider family of an agent's harness, or ``None``.

    Loads the agent's spec to read its ``harness_kind`` and maps it to a
    provider family (``"anthropic"`` / ``"openai"``). Returns ``None`` when
    the bundle can't be loaded or the harness is unknown — callers treat
    ``None`` as "can't confirm same family".

    :param agent: The agent whose harness family to resolve.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None``.
    """
    from omnigent.onboarding.provider_config import provider_family_for_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001
        return None
    return provider_family_for_harness(spec.executor.harness_kind)


def _same_provider_family(*args: Any, **kwargs: Any) -> bool:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._same_provider_family(*args, **kwargs)


def _same_provider_family_impl(a: Agent, b: Agent) -> bool:
    """Return whether two agents share a (known) provider family.

    ``False`` when either family is undeterminable, so a fork that can't
    confirm both agents speak the same provider resets model settings and
    skips resuming the source's native session (the runner rebuilds the
    native transcript from Omnigent items instead).

    :param a: First agent (e.g. the fork source's agent).
    :param b: Second agent (e.g. the switch target).
    :returns: ``True`` when both resolve to the same non-``None`` family.
    """
    family_a = _agent_provider_family(a)
    return family_a is not None and family_a == _agent_provider_family(b)


def _filesystem_attachment_in_history(
    session_id: str,
    conversation_store: ConversationStore,
    file_store: FileStore | None,
    *,
    up_to_response_id: str | None = None,
    content: Sequence[dict[str, Any]] = (),
) -> str | None:
    """Find a retained attachment requiring filesystem tools, using its stored name.

    :param session_id: Source session whose history will be retained.
    :param conversation_store: Store containing the ordered source history.
    :param file_store: Store containing authoritative attachment filenames.
    :param up_to_response_id: Inclusive fork cutoff, or all history when absent.
    :param content: Additional incoming message blocks to check before stored history.
    :returns: A referenced filesystem attachment's name, or ``None``.
    """
    from omnigent.harnesses.native_attachments import requires_filesystem

    if file_store is None:
        return None
    filenames: dict[str, str] = {}
    files_after: str | None = None
    while True:
        files_page = file_store.list(session_id, limit=1000, after=files_after, order="asc")
        for stored_file in files_page.data:
            if requires_filesystem(stored_file.filename):
                filenames[stored_file.id] = stored_file.filename
        if not files_page.has_more or not files_page.data:
            break
        files_after = files_page.last_id
    if not filenames:
        return None

    for block in content:
        file_id = block.get("file_id")
        if isinstance(file_id, str) and file_id in filenames:
            return filenames[file_id]

    # Descending order finds the last item of the cutoff response first,
    # matching the fork store's inclusive position cutoff.
    retained = up_to_response_id is None
    items_after: str | None = None
    while True:
        page = conversation_store.list_items(
            session_id, limit=1000, after=items_after, order="desc"
        )
        for item in page.data:
            if item.response_id == up_to_response_id:
                retained = True
            if not retained or not isinstance(item.data, MessageData):
                continue
            for block in item.data.content:
                file_id = block.get("file_id")
                if isinstance(file_id, str) and file_id in filenames:
                    return filenames[file_id]
        if not page.has_more or not page.data:
            return None
        items_after = page.last_id


def _load_agent_spec_for_session(*args: Any, **kwargs: Any) -> AgentSpec | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._load_agent_spec_for_session(*args, **kwargs)


def _load_agent_spec_for_session_impl(
    conv: Conversation,
    agent_store: AgentStore,
) -> AgentSpec | None:
    # Split from _build_policy_engine_from_spec so the caller can run the
    # cheap guardrails/default-policy skip check between the two and avoid
    # paying for engine construction when no policy could fire. Both halves
    # are blocking DB/IO, so each is run under asyncio.to_thread.
    if conv.agent_id is None:
        return None
    agent = agent_store.get(conv.agent_id)
    if agent is None:
        return None
    agent_cache = cast(AgentCache, get_agent_cache())
    return agent_cache.load(
        agent.id,
        agent.bundle_location,
        expand_env=agent.session_id is None,
    ).spec


def _build_policy_engine_from_spec(*args: Any, **kwargs: Any) -> PolicyEngine:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return _facade._build_policy_engine_from_spec(*args, **kwargs)


def _build_policy_engine_from_spec_impl(
    spec: AgentSpec,
    session_id: str,
    conversation_store: ConversationStore,
    conversation: Conversation | None = None,
) -> PolicyEngine:
    """Build an engine for *spec*, reusing a conversation row when held.

    Every caller of this wrapper already loaded the conversation to
    resolve *spec*; passing it lets the builder skip its own read. Only
    the row's immutable identity is reused — the builder re-derives
    labels, session_state and model from a fresh read (see
    :func:`build_policy_engine`).
    """
    caps = get_caps()
    host_connection = (
        caps.policy_llm_connection_factory() if caps.policy_llm_connection_factory else None
    )
    return build_policy_engine(
        spec=spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        conversation=conversation,
        # The spec was resolved from this row's agent binding; the builder
        # confirms it against its own fresh read and fails closed if a
        # switch-agent landed in between.
        expected_agent_id=conversation.agent_id if conversation is not None else None,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        server_llm=caps.llm,
        host_connection=host_connection,
    )


async def _apply_pending_policy_ask_writes(
    session_id: str,
    conv: Conversation,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    data: dict[str, Any],
) -> None:
    """
    Apply (or drop) policy writes stashed for a relay tool-call ASK.

    Called when an ``approval`` verdict resolves a runner-owned policy
    elicitation (both approval entry points — the ``approval`` event and the
    resolve URL — route here via their callers). On ``accept`` the deciding
    policy's stashed ``state_updates`` / ``set_labels`` are persisted by a
    freshly built engine — exactly what the native ``_hold_native_ask_gate``
    path does inline. On any other verdict (decline / cancel / missing) they
    are dropped (POLICIES.md §7.2: a denied ASK leaves no trace). No-op when
    the elicitation has no stashed writes (the common case — most ASKs and
    all non-policy elicitations).

    :param session_id: Session id that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param conv: The session conversation, for the agent / spec lookup.
    :param conversation_store: Store the engine persists session state to.
    :param agent_store: Store for the agent spec lookup.
    :param data: The approval payload, carrying ``elicitation_id`` and the
        verdict ``action`` (e.g. ``{"elicitation_id": "elicit_x",
        "action": "accept"}``).
    :returns: None.
    """
    elicitation_id = data.get("elicitation_id", "")
    pending = _pending_policy_ask_writes.get(elicitation_id)
    if pending is None:
        return
    if pending.from_mcp:
        # MCP entries: the retry path (POST /mcp with requestState)
        # pops and applies the writes itself. Applying here too would
        # double-apply non-idempotent ops (e.g. INCREMENT state
        # updates for cost-budget counters). Leave the entry for the
        # retry path (it owns cleanup), dropping it only on decline.
        if data.get("action") != "accept":
            # Declined — remove the stashed writes (POLICIES.md §7.2:
            # a denied ASK leaves no trace).
            _pending_policy_ask_writes.pop(elicitation_id, None)
        return
    # Non-MCP relay path: claim the entry atomically (no await between
    # the get above and this pop, so duplicate verdicts — a client
    # transport retry racing its original POST, or a second client —
    # can never both apply the same non-idempotent writes).
    pending = _pending_policy_ask_writes.pop(elicitation_id, None)
    if pending is None:
        # A concurrent duplicate verdict already claimed the writes.
        return
    if data.get("action") != "accept":
        # Declined — the claim already removed the stashed writes
        # (POLICIES.md §7.2: a denied ASK leaves no trace).
        return
    # Resolve the agent spec + build the engine off the event loop: the
    # lookup, cold-cache bundle fetch, and engine construction are all
    # blocking DB/IO.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return
    try:
        engine = await asyncio.to_thread(
            _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
        )
    except BaseException:
        # A raise here (e.g. a concurrent agent rebind) must not lose the
        # approved writes with no retry possible — restore the claim.
        _pending_policy_ask_writes.setdefault(elicitation_id, pending)
        raise
    # The label/state writes hit the DB synchronously too — keep them
    # off the loop.
    if pending.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, pending.set_labels)
    if pending.state_updates:
        with contextlib.suppress(ConversationNotFoundError):
            await asyncio.to_thread(engine.apply_state_updates, pending.state_updates)


def _build_actor(user_id: str | None) -> dict[str, str] | None:
    """
    Build the ``actor`` dict for :class:`EvaluationContext`.

    Returns ``{"run_as": user_id}`` when the authenticated user is
    known, ``None`` otherwise (tests, legacy callers without auth).

    :param user_id: Authenticated user email from the request,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled or the caller is unauthenticated.
    :returns: Actor dict or ``None``.
    """
    if user_id is None:
        return None
    return {"run_as": user_id}


def _build_evaluation_context(
    phase: Phase,
    data: dict[str, Any] | str,
    event: dict[str, Any],
    *,
    actor: dict[str, str] | None = None,
) -> EvaluationContext:
    """
    Build an :class:`EvaluationContext` from a proto-style event dict.

    Maps the proto ``Event.data`` shape to the internal convention:

    - ``TOOL_CALL``: ``content = {"name": name, "arguments": args}``,
      ``tool_name = name``.
    - ``TOOL_RESULT``: ``content = {"result": result_str}``,
      ``tool_name`` from ``request_data.name``,
      ``request_data`` from the event's ``request_data`` field.
    - ``REQUEST`` / ``RESPONSE``: ``content = str(data)``.

    :param phase: Internal phase enum.
    :param data: ``event.data`` dict from the proto request.
    :param event: Full event dict (for ``request_data``, ``context``).
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: Ready-to-evaluate context.
    """
    # A native hook may stamp the session's live model into the event context
    # (e.g. the codex hook reads it from ``config.toml`` at gate time — the
    # source of truth for an in-TUI ``/model`` selection). When present, this
    # wins over the engine's server-resolved model (see
    # ``PolicyEngine._inject_model``); ``None`` falls back to that resolution.
    raw_context_value = event.get("context")
    raw_context = raw_context_value if isinstance(raw_context_value, dict) else {}
    supplied_model = raw_context.get("model")
    hook_model = supplied_model if isinstance(supplied_model, str) and supplied_model else None
    # The harness, when a native hook stamped it (e.g. the codex hook), so
    # policies can tailor messages to the session's model-switch surface
    # (codex-native is terminal-only). Carried through unchanged — the engine
    # neither resolves nor overrides it.
    supplied_harness = raw_context.get("harness")
    hook_harness = (
        supplied_harness if isinstance(supplied_harness, str) and supplied_harness else None
    )
    structured_data = data if isinstance(data, dict) else {}
    if phase == Phase.TOOL_CALL:
        raw_tool_name = structured_data.get("name")
        tool_name = raw_tool_name if isinstance(raw_tool_name, str) else ""
        raw_args = structured_data.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}
        return EvaluationContext(
            phase=phase,
            content={"name": tool_name, "arguments": args},
            tool_name=tool_name or None,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    if phase == Phase.TOOL_RESULT:
        tool_result = structured_data.get("result", "")
        raw_request_data = event.get("request_data")
        request_data = raw_request_data if isinstance(raw_request_data, dict) else None
        result_tool_name = None
        if request_data is not None:
            raw_tool_name = request_data.get("name")
            result_tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None
        return EvaluationContext(
            phase=phase,
            content={
                "result": tool_result if isinstance(tool_result, str) else json.dumps(tool_result),
            },
            tool_name=result_tool_name,
            request_data=request_data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # LLM_REQUEST / LLM_RESPONSE — content is the full request/response dict.
    if phase in (Phase.LLM_REQUEST, Phase.LLM_RESPONSE):
        return EvaluationContext(
            phase=phase,
            content=data,
            actor=actor,
            model=hook_model,
            harness=hook_harness,
        )
    # REQUEST / RESPONSE — content is the user/assistant text. The wire ``data``
    # is a dict for every current first-party producer (``{"text"|"content":
    # ...}``), but
    # a bare string is still accepted for ``PHASE_REQUEST`` for compatibility
    # with older or third-party callers that send the prompt text directly.
    # Accept both, and NEVER raise here: a crash 500s the evaluate endpoint,
    # which silently fails the request/result gate OPEN (the exact symptom
    # that let cost-over-budget terminal prompts through).
    if isinstance(data, str):
        text = data
    elif isinstance(data, dict):
        text = data.get("text") or data.get("content") or str(data)
    else:
        text = str(data)
    text_str = text if isinstance(text, str) else json.dumps(text)
    # REQUEST content is the structured dict ({"user_content", "attachments"}) so
    # every request reaches policies in one shape, whatever the entry point. This
    # native/terminal path carries no uploads, so ``attachments`` is always empty;
    # the web input gate (_evaluate_input_policy) is what populates it. RESPONSE
    # stays a plain string — attachments are an input-only concern.
    request_or_response_content: Any = (
        {"user_content": text_str, "attachments": []} if phase == Phase.REQUEST else text_str
    )
    return EvaluationContext(
        phase=phase,
        content=request_or_response_content,
        actor=actor,
        model=hook_model,
        harness=hook_harness,
    )


def _extract_user_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from a user message event body.

    Mirrors the logic in ``workflow._extract_user_text`` but
    operates on the raw ``SessionEventInput.data`` dict rather
    than a parsed ``MessageData`` object.

    :param body: The validated ``message`` event with
        ``role: "user"``.
    :returns: Joined text from ``input_text`` / ``text`` content
        blocks. Empty string if no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("input_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _publish_policy_deny(session_id: str, reason: str) -> None:
    """
    Publish the ``[Denied by policy: ...]`` sentinel on the session stream.

    The sentinel text is a load-bearing contract (the REPL renders it, e2e
    tests assert it, and native harnesses relay it to the model), so it is
    always carried in a ``response.output_text.delta``.

    Input DENY callers also persist the same sentinel as an assistant
    conversation item. This stream publish remains separate so live clients
    still get immediate feedback before the handler returns. Stamping a unique
    ``message_id`` (matching how live streaming text is tagged) routes the
    delta through the web's live-preview path, where it folds into a single
    ``live:<id>`` block rather than a response-scoped stray bubble.

    Safe for the other consumers: the REPL converts any ``output_text.delta``
    to a ``TextDelta`` regardless of ``message_id``; the ``/v1/responses`` API
    surfaces the deny via input-deny synthesis (not session-stream deltas);
    and the only ``message_id``-gated accumulator (``_relay_runner_stream``)
    reads runner-relayed deltas, never this server-published one.

    :param session_id: Session/conversation identifier.
    :param reason: Human-readable deny reason from the policy verdict.
    """
    session_stream.publish(
        session_id,
        {
            "type": "response.output_text.delta",
            "delta": f"[Denied by policy: {reason}]",
            # Unique per deny so two separate denials don't fold into one
            # block; a single delta carries the whole sentinel, so index 0.
            "message_id": f"deny_{secrets.token_hex(8)}",
            "index": 0,
        },
    )


def _publish_input_deny_terminal(session_id: str, conv: Conversation, reason: str) -> None:
    """
    Publish a terminal ``response.completed`` for an INPUT-phase DENY.

    The short-circuit never forwards to a runner, so no runner-relayed
    terminal ``response.*`` event is emitted. SSE consumers that drive a
    turn off the live-tail (the headless ``-p`` client,
    :class:`omnigent_client.SessionsChat.send`) iterate until a
    turn-terminal event arrives and would otherwise block forever. The
    output carries the same sentinel text so the terminal-snapshot fallback
    also surfaces the deny.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation whose agent/model name tags the response.
    :param reason: Human-readable deny reason from the policy verdict.
    """
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    response = ResponseObject(
        id=f"deny_{secrets.token_hex(8)}",
        status="completed",
        model=conv.agent_id or "policy",
        created_at=int(time.time()),
        completed_at=int(time.time()),
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": sentinel}],
            }
        ],
    )
    session_stream.publish(
        session_id,
        CompletedEvent(type="response.completed", response=response).model_dump(exclude_none=True),
    )


async def _persist_policy_deny_sentinel(
    session_id: str,
    conv: Conversation,
    reason: str,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
) -> None:
    """
    Persist the ``[Denied by policy: ...]`` sentinel as assistant history.

    INPUT policy DENY returns synchronously and never forwards the user turn
    to a runner, so no downstream stream relay can append the assistant-side
    deny marker. Persisting the same assistant message shape used by OUTPUT
    policy DENY keeps follow-up turns and the items API consistent with the
    streamed deny users already see.

    After persisting, publish the committed item as a
    ``response.output_item.done`` — the same commit event a streamed
    assistant message emits (see :func:`_flush_relay_text`). Without it the
    live deny only exists as the ``_publish_policy_deny`` sentinel delta,
    which the web folds into a provisional ``live:`` preview block that the
    terminal ``response.completed`` sweeps; the deny then reappeared only
    after a refresh re-hydrated the persisted item. Emitting the commit event
    lets the web reconcile the preview into a durable, itemId-keyed block that
    survives the sweep, a reconnect, and a refresh alike.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation whose agent/model name tags the message.
    :param reason: Human-readable deny reason from the policy verdict.
    :param conversation_store: Store for item persistence.
    :param agent_store: Store used to resolve the agent's display name.
    """
    import uuid

    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    agent = agent_store.get(conv.agent_id) if conv.agent_id else None
    agent_name = agent.name if agent is not None else conv.agent_id or "policy"
    item = NewConversationItem(
        type="message",
        response_id=f"deny_{uuid.uuid4().hex}",
        data=parse_item_data(
            "message",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": sentinel}],
                "agent": agent_name,
            },
        ),
    )
    persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
    if persisted:
        done_event = OutputItemDoneEvent(
            type="response.output_item.done",
            item=persisted[0].to_api_dict(),
        )
        session_stream.publish(session_id, done_event.model_dump())


def _extract_assistant_text_from_event(body: SessionEventInput) -> str:
    """
    Extract concatenated text from an assistant message event.

    Mirrors :func:`_extract_user_text_from_event` but for
    assistant messages. Content blocks use ``"text"`` (not
    ``"input_text"``).

    :param body: The validated ``message`` event with
        ``role: "assistant"``.
    :returns: Joined text from content blocks. Empty string if
        no text blocks found.
    """
    content = body.data.get("content") or []
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _replace_text_in_message_body(
    body: SessionEventInput,
    replacement: str,
) -> SessionEventInput:
    """
    Return a copy of the message body with all text content
    blocks replaced by *replacement*.

    Used by OUTPUT policy DENY to substitute the deny sentinel
    into the persisted message while preserving non-text content
    blocks (images, etc.) and all other body fields.

    :param body: The original assistant message event.
    :param replacement: The deny sentinel text,
        e.g. ``"[Denied by policy: harmful content]"``.
    :returns: A new body with text blocks replaced.
    """
    content = body.data.get("content") or []
    new_content: list[dict[str, Any]] = []
    replaced = False
    for block in content:
        if isinstance(block, dict) and "text" in block:
            if not replaced:
                new_content.append({"type": "output_text", "text": replacement})
                replaced = True
        else:
            new_content.append(block)
    if not replaced:
        new_content.append({"type": "output_text", "text": replacement})
    new_data = {**body.data, "content": new_content}
    return type(body)(type=body.type, data=new_data)


async def _evaluate_output_policy(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate an assistant message against OUTPUT phase policies.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. On DENY, returns a verdict dict with
    ``_denied_body`` — the caller should persist this modified
    body (text replaced with deny sentinel) instead of the
    original.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        with ``_denied_body`` on DENY.
    """

    assistant_text = _extract_assistant_text_from_event(body)
    if not assistant_text:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    engine = await asyncio.to_thread(
        _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
    )
    ctx = EvaluationContext(
        phase=Phase.RESPONSE,
        content=assistant_text,
        tool_name=None,
        actor=actor,
    )
    result = await engine.evaluate(ctx)

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    # DENY — build the denied body with sentinel text.
    # The caller persists this modified body instead of the
    # original (Option B).
    if result.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
    reason = result.reason or "Denied by policy"
    sentinel = f"{_DENY_SENTINEL_PREFIX}{reason}]"
    denied_body = _replace_text_in_message_body(body, sentinel)
    return {
        "verdict": "deny",
        "reason": reason,
        "_denied_body": denied_body,
    }


async def _stream_live_events(
    request: Request,
    session_id: str,
    on_subscribed: Callable[[], Awaitable[Iterable[dict[str, Any]]]] | None = None,
    viewer_user_id: str | None = None,
    viewer_idle: bool = False,
    presence_root_id: str | None = None,
) -> AsyncIterator[str]:
    """
    Yield SSE-formatted events from the conversation's live stream.

    Events are delivered live from the moment :func:`session_stream.subscribe`
    is invoked forward — there is no buffer and no replay. Events
    published before this generator subscribed are lost; clients
    reconcile pre-subscribe state via the snapshot endpoint
    (``GET /v1/sessions/{id}``) and dedupe by item id.

    An intentional session close emits ``[DONE]``. Server shutdown and
    subscriber overflow instead end without it so clients reconnect and
    reconcile from the persisted snapshot after the server returns.

    ``finally`` is cleanup-only (presence deregistration): yielding
    from ``finally`` during client ``aclose`` / ``GeneratorExit``
    raises ``RuntimeError: async generator ignored GeneratorExit``.
    The subscribe iterator is wrapped in ``contextlib.aclosing`` so
    outer ``aclose`` tears down the pub-sub subscriber slot
    immediately (a bare ``async for`` would defer that to GC).

    Each emitted dict is validated against
    :data:`ServerStreamEvent` at the wire boundary so a runtime
    that publishes an unmodelled ``type`` fails loud rather than
    serializing an unknown event verbatim.

    The subscribe call passes a ``ready_event`` heartbeat plus
    ``heartbeat_interval_s``. The ready heartbeat is yielded
    immediately after the live-tail subscriber slot is registered,
    before any snapshot hook runs, so clients can wait for a
    concrete subscription acknowledgment before posting a fast
    one-shot turn. The interval heartbeat keeps an idle stream
    emitting ``session.heartbeat`` events on a fixed cadence (see
    :data:`_SESSION_STREAM_HEARTBEAT_INTERVAL_S`). Without that,
    a stream that sits between turns has nothing crossing the wire;
    the client's SSE read-timeout and this route's
    ``request.is_disconnected()`` check (only polled on event
    arrival) both lag for minutes after a half-open socket forms
    (e.g. after a laptop sleep). The heartbeat gives both sides a
    regular byte to fire against.

    :param request: The FastAPI request, used to detect disconnect.
    :param session_id: Session/conversation identifier whose stream
        to subscribe to, e.g. ``"conv_abc123"``.
    :param on_subscribed: Optional snapshot-on-connect hook forwarded to
        :func:`session_stream.subscribe`; its events are yielded ahead of
        the live tail so a fresh client sees current resource state
        without polling. ``None`` (default) keeps the pure live-tail
        shape used by callers that reconcile via the snapshot endpoint.
    :param viewer_user_id: Authenticated identity to register in the
        session's presence registry for this stream's lifetime, e.g.
        ``"alice@example.com"``. ``None`` (default, and the reserved
        single-user sentinel mapped via ``attribution_user``) skips
        presence tracking entirely.
    :param viewer_idle: The viewer's connect-time idle flag (tab
        backgrounded), from the route's ``idle`` query param. Ignored
        when *viewer_user_id* is ``None``.
    :param presence_root_id: Root conversation of the streamed
        session's tree (its ``root_conversation_id``), e.g.
        ``"conv_root123"``. Presence is scoped to the tree's root so
        viewers of different agents/sub-agents in one session see
        each other. Required when *viewer_user_id* is set; ignored
        otherwise.
    :returns: An async iterator of SSE message strings.
    :raises ValueError: If *viewer_user_id* is set without
        *presence_root_id* — a per-conversation presence scope would
        silently split a session's viewers per agent.
    """
    # Presence registers before the subscribe loop: the join broadcast
    # fans out to ALREADY-subscribed co-viewers, while this stream
    # learns the full list (self included) from the snapshot-on-connect
    # presence event — full-state events make that ordering race benign.
    presence_token: str | None = None
    if viewer_user_id is not None:
        if presence_root_id is None:
            raise ValueError("presence_root_id is required when viewer_user_id is set")
        presence_token = presence.connect(
            presence_root_id, session_id, viewer_user_id, viewer_idle
        )
    try:
        # ``aclosing`` propagates outer ``aclose`` into ``subscribe``;
        # a bare ``async for`` would leave the subscriber slot until GC.
        async with contextlib.aclosing(
            session_stream.subscribe(
                session_id,
                heartbeat_interval_s=_SESSION_STREAM_HEARTBEAT_INTERVAL_S,
                ready_event={"type": "session.heartbeat"},
                # In-flight text replay must be captured synchronously at slot
                # registration (before ``ready_event`` suspends), not in the
                # async ``on_subscribed`` hook, or window deltas double-render.
                # Resource state stays in ``on_subscribed`` — it needs
                # awaits and is not dedup-sensitive.
                pre_ready_snapshot=lambda: inflight_text.snapshot_for(session_id),
                on_subscribed=on_subscribed,
            )
        ) as live_events:
            async for event in live_events:
                if await request.is_disconnected():
                    break
                event_type = event.get("type")
                if not isinstance(event_type, str):
                    raise ValueError(
                        f"session stream event missing string ``type`` field: {event!r}",
                    )
                validated = _SERVER_STREAM_EVENT_ADAPTER.validate_python(event)
                yield _format_sse(event_type, validated.model_dump())
    except session_stream.SubscriberOverflowError:
        _logger.warning(
            "session stream subscriber overflowed for %s; closing for snapshot reconnect",
            session_id,
            extra={"session_id": session_id},
        )
    else:
        # Server restart is a transport drop, not a permanent session close.
        # Never yield from finally: aclose / GeneratorExit cannot accept a yield.
        if not shutdown_state.server_shutting_down():
            yield "data: [DONE]\n\n"
    finally:
        # The non-None checks besides presence_token's are type
        # narrowing only: a minted token implies both were set above.
        if (
            presence_token is not None
            and viewer_user_id is not None
            and presence_root_id is not None
        ):
            presence.disconnect(presence_root_id, viewer_user_id, presence_token)


def _validate_terminal_launch_args(value: list[str] | None) -> list[str] | None:
    """
    Validate per-session native-terminal pass-through args.

    Enforces a flat list of strings within bounded count / length.
    The flat-list shape is the security boundary: there is no key for
    a caller to smuggle internal launch wiring (bridge dir, Omnigent URL,
    auth) through — those stay runner-owned (see
    designs/NATIVE_RUNNER_SERVER_LAUNCH.md).

    :param value: The candidate args, e.g.
        ``["--dangerously-skip-permissions"]``, or ``None`` to leave
        unset / unchanged.
    :returns: The validated list unchanged, or ``None`` when *value*
        is ``None``.
    :raises ValueError: If *value* is not a list of strings, exceeds
        :data:`_MAX_TERMINAL_LAUNCH_ARGS` entries, or any entry
        exceeds :data:`_MAX_TERMINAL_LAUNCH_ARG_LEN` characters.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        raise ValueError("terminal_launch_args must be a list of strings")
    if len(value) > _MAX_TERMINAL_LAUNCH_ARGS:
        raise ValueError(f"terminal_launch_args exceeds {_MAX_TERMINAL_LAUNCH_ARGS} entries")
    for arg in value:
        if len(arg) > _MAX_TERMINAL_LAUNCH_ARG_LEN:
            raise ValueError(
                f"terminal_launch_args entry exceeds {_MAX_TERMINAL_LAUNCH_ARG_LEN} characters"
            )
    return value


def _validated_cost_control_mode_override(value: str | None) -> str | None:
    """
    Validate a caller-supplied per-session cost-control switch.

    :param value: The candidate value, e.g. ``"on"``, or ``None``
        when the caller did not set / wants to clear the override.
    :returns: The value unchanged when valid, or ``None``.
    :raises OmnigentError: 400 (``invalid_input``) when *value* is
        anything other than ``"on"``, ``"off"``, or ``None``.
    """
    if value is None or value in COST_CONTROL_OVERRIDE_VALUES:
        return value
    raise OmnigentError(
        f"invalid cost_control_mode_override: {value!r} (expected 'on', 'off', or null to clear)",
        code=ErrorCode.INVALID_INPUT,
    )


def _validated_subagent_routing_override(value: str | None) -> str | None:
    """
    Validate a caller-supplied per-session subagent-routing switch.

    Two-state: ``"on"`` routes subagent spawns and ``"off"`` / unset both
    read as Default. ``None`` from a PATCH clears the stored value, which
    lands the session on Default rather than inheriting anything.

    :param value: The candidate value, e.g. ``"on"``, or ``None`` when
        the caller did not set / wants to clear the override.
    :returns: The value unchanged when valid, or ``None``.
    :raises OmnigentError: 400 (``invalid_input``) when *value* is
        anything other than ``"on"``, ``"off"``, or ``None``.
    """
    if value is None or value in SUBAGENT_ROUTING_OVERRIDE_VALUES:
        return value
    raise OmnigentError(
        f"invalid subagent_routing_override: {value!r} (expected 'on', 'off', or null to clear)",
        code=ErrorCode.INVALID_INPUT,
    )


def _parse_session_create_metadata(metadata: str) -> SessionCreateMetadata:
    """
    Parse the JSON metadata part from bundled session creation.

    :param metadata: Raw JSON string from the multipart form,
        e.g. ``{"title": "debug auth flow"}``.
    :returns: Validated :class:`SessionCreateMetadata`.
    :raises OmnigentError: If the JSON fails the request schema.
    """
    try:
        parsed = SessionCreateMetadata.model_validate_json(metadata)
        reasoning_effort = validate_effort(
            parsed.reasoning_effort,
            "session metadata",
            EFFORT_VALUES,
        )
        # Bounds-check the native-terminal args; raises ValueError
        # (wrapped below) on a malformed or oversized list.
        _validate_terminal_launch_args(parsed.terminal_launch_args)
        return parsed.model_copy(update={"reasoning_effort": reasoning_effort})
    except (ValidationError, ValueError) as exc:
        raise OmnigentError(
            f"invalid session metadata: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _multipart_missing_detail(field: str) -> dict[str, Any]:
    """
    Build a FastAPI-style missing multipart field error.

    :param field: Missing form field name, e.g. ``"bundle"``.
    :returns: A validation-detail dict for HTTP 422 responses.
    """
    return {
        "type": "missing",
        "loc": ["body", field],
        "msg": "Field required",
        "input": None,
    }


def _require_host_conn_for_worktree(host_id: str | None, request: Request) -> HostConnection:
    """
    Resolve the live host connection for a worktree operation.

    :param host_id: Target host id from the session request, e.g.
        ``"host_a1b2c3d4..."``. ``None`` is rejected — git worktree
        creation requires a host (the server has no filesystem).
    :param request: FastAPI request carrying ``app.state.host_registry``.
    :returns: The live :class:`HostConnection` for ``host_id``.
    :raises OmnigentError: ``invalid_input`` when ``host_id`` is
        ``None``; ``internal_error`` when no host registry is
        configured; ``conflict`` when the host is offline.
    """
    if host_id is None:
        raise OmnigentError(
            "git worktree creation requires host_id",
            code=ErrorCode.INVALID_INPUT,
        )
    host_registry = cast(HostRegistry | None, getattr(request.app.state, "host_registry", None))
    if host_registry is None:
        # Server misconfiguration, not bad client input — mirror
        # _validate_session_workspace, which also returns internal_error.
        raise OmnigentError(
            "host registry is not configured; cannot create a worktree",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    return host_conn


async def _create_session_worktree(
    *,
    host_id: str | None,
    source_repo: str | None,
    git: SessionGitOptions,
    request: Request,
) -> CreatedWorktree:
    """
    Create a git worktree on the host for a new session branch.

    Validates the branch name server-side (the host re-validates), then
    proxies ``host.create_worktree``. The returned workspace preserves
    the selected subdirectory in the new worktree. See
    designs/SESSION_GIT_WORKTREE.md.

    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
        Required (worktree creation needs a host).
    :param source_repo: Canonical path of the picked source repo (the
        boundary-validated workspace), e.g. ``"/Users/alice/myrepo"``.
        ``None`` is a programming error and fails loud.
    :param git: Validated git options (``branch_name``, optional
        ``base_branch``).
    :param request: FastAPI request carrying the host registry.
    :returns: The worktree root for rollback, the relocated ``workspace``,
        and ``branch`` (to store as ``git_branch``).
    :raises OmnigentError: ``invalid_input`` for a bad branch name,
        missing source repo, or a host-reported git failure (duplicate
        branch, bad base ref, not a repo); ``conflict`` when the host is
        offline or unresponsive; ``internal_error`` when no host registry
        is configured.
    """
    from omnigent.host.git_worktree import WorktreeError, validate_branch_name
    from omnigent.server.routes._host_worktree import (
        WorktreeHostUnavailableError,
        WorktreeProxyError,
        create_worktree_on_host,
    )

    if source_repo is None:  # pragma: no cover — host_id guarantees a workspace
        raise OmnigentError(
            "git worktree creation requires a source repository workspace",
            code=ErrorCode.INVALID_INPUT,
        )
    try:
        validate_branch_name(git.branch_name)
    except WorktreeError as exc:
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc

    host_conn = _require_host_conn_for_worktree(host_id, request)
    host_registry = request.app.state.host_registry
    try:
        return await create_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            repo_path=source_repo,
            branch_name=git.branch_name,
            base_branch=git.base_branch,
            existing_branch=git.existing_branch,
        )
    except WorktreeHostUnavailableError as exc:
        # Host offline / unresponsive — infra, not user input.
        raise OmnigentError(exc.message, code=ErrorCode.CONFLICT) from exc
    except WorktreeProxyError as exc:
        # Host-reported git failure (dup branch, bad base, not a repo) —
        # user-correctable input.
        raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc


# Opt-in ``?delete_branch=true`` cannot reach git when the host tunnel
# is down (users typically see this as ``runner_online: false``). Refuse
# the delete instead of 404'ing or silently skipping cleanup.
_DELETE_WORKTREE_OFFLINE_MESSAGE = (
    "Cannot delete worktree — runner offline. "
    "Delete session only (delete_branch=false) or wait for the runner to reconnect."
)


async def _remove_session_worktree_best_effort(
    *,
    host_id: str,
    worktree_path: str,
    branch: str,
    delete_branch: bool,
    request: Request,
    reason: str,
    conversation_store: ConversationStore | None = None,
    exclude_conversation_id: str | None = None,
    fail_if_unavailable: bool = False,
    expected_root_fingerprint: str | None = None,
) -> None:
    """
    Best-effort removal of a session's git worktree.

    Used for create-rollback (orphan cleanup) and opt-in session-delete
    cleanup. Host-reported git failures are logged so the caller's
    primary operation still completes. When ``fail_if_unavailable`` is
    set, an unreachable host raises ``CONFLICT`` instead of skipping —
    the session is left in place so the caller can retry without
    worktree cleanup.

    :param host_id: Host that owns the worktree, e.g.
        ``"host_a1b2c3d4..."``.
    :param worktree_path: Absolute worktree directory to remove on the
        host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch checked out in the worktree, e.g.
        ``"feature/login"``.
    :param delete_branch: When ``True``, also run ``git branch -D``
        after removing the worktree directory.
    :param request: FastAPI request carrying the host registry.
    :param reason: Short label for log lines, e.g.
        ``"create-rollback"`` or ``"session-delete"``.
    :param conversation_store: Store used to check whether another live
        session shares this directory. ``None`` skips the check — correct
        for create-rollback, whose worktree was made moments ago in the
        same request and cannot be referenced by anything else.
    :param exclude_conversation_id: The conversation whose delete triggered
        this removal, excluded from that check. Required with
        *conversation_store*.
    :param expected_root_fingerprint: Recorded root identity; absent legacy sessions
        may only remove their exact stored workspace.
    :param fail_if_unavailable: When ``True``, raise ``CONFLICT`` if the
        host cannot be reached to run git. Create-rollback leaves this
        ``False`` so a failed create still surfaces its original error.
    """
    from omnigent.server.routes._host_worktree import (
        WorktreeHostUnavailableError,
        WorktreeProxyError,
        list_worktrees_on_host,
        recorded_worktree_root,
        remove_worktree_on_host,
        worktree_root_fingerprint,
    )
    from omnigent.server.routes._workspace_validation import _is_subpath_of

    # A fork reusing the source's directory, or several sessions attached to
    # one existing worktree, all run in the same cwd. Removing it under them
    # leaves their runners on a deleted directory, so leave a shared worktree
    # alone and let the last session out clean it up. Checked before host
    # reachability so an offline host does not 409 a delete that would not
    # have touched the directory anyway.
    if conversation_store is not None and exclude_conversation_id is not None:
        cleanup_root = recorded_worktree_root(worktree_path, expected_root_fingerprint)
        if cleanup_root is None:
            _logger.warning(
                "Workspace %s no longer matches its recorded cleanup root", worktree_path
            )
            return
        shared = await asyncio.to_thread(
            conversation_store.has_other_live_session_in_workspace,
            host_id=host_id,
            workspace=cleanup_root,
            exclude_conversation_id=exclude_conversation_id,
            include_subdirectories=True,
        )
        if shared:
            _logger.info(
                "Keeping worktree %s (%s): another live session still runs there",
                worktree_path,
                reason,
            )
            return

    host_registry = getattr(request.app.state, "host_registry", None)
    if host_registry is None:
        if fail_if_unavailable:
            raise OmnigentError(
                "host registry is not configured; cannot delete a worktree",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return
    host_conn = host_registry.get(host_id)
    if host_conn is None:
        if fail_if_unavailable:
            raise OmnigentError(
                _DELETE_WORKTREE_OFFLINE_MESSAGE,
                code=ErrorCode.CONFLICT,
            )
        _logger.warning(
            "Skipping worktree removal (%s) for %s: host %s offline",
            reason,
            worktree_path,
            host_id,
        )
        return
    try:
        if conversation_store is not None and exclude_conversation_id is not None:
            worktrees = await list_worktrees_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                repo_path=worktree_path,
                for_cleanup=True,
            )
            # Missing directories can resolve inside an unrelated enclosing repository.
            expected_root = expected_root_fingerprint or worktree_root_fingerprint(worktree_path)
            # Keep worktrees that have been repurposed for another branch or detached HEAD.
            roots = [
                path
                for tree in worktrees
                if isinstance(path := tree.get("path"), str)
                and _is_subpath_of(worktree_path, path)
                and worktree_root_fingerprint(path) == expected_root
                and tree.get("branch") == branch
                and not tree.get("is_main", True)
            ]
            if not roots:
                _logger.warning(
                    "No matching linked worktree for %s; skipping cleanup", worktree_path
                )
                return
            worktree_path = max(roots, key=len)
        await remove_worktree_on_host(
            host_registry=host_registry,
            host_conn=host_conn,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
        )
    except WorktreeHostUnavailableError as exc:
        if fail_if_unavailable:
            raise OmnigentError(
                _DELETE_WORKTREE_OFFLINE_MESSAGE,
                code=ErrorCode.CONFLICT,
            ) from exc
        _logger.warning(
            "Best-effort worktree removal (%s) failed for %s: host %s unavailable",
            reason,
            worktree_path,
            host_id,
        )
    except WorktreeProxyError:
        _logger.warning(
            "Best-effort worktree removal (%s) failed for %s",
            reason,
            worktree_path,
            exc_info=True,
        )


def _resolve_subagent_spec(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> AgentSpec | None:
    """
    Load the parent bundle and resolve a child sub-agent's trusted spec.

    This is the single trusted source for any per-sub-agent launch wiring
    the server derives at create time (terminal-first labels, YOLO
    pass-through args). The spec comes from the server-loaded parent
    bundle — never from caller-supplied request fields — so a caller
    cannot smuggle in launch config a sub-agent's own bundle did not
    declare.

    :param agent: The parent agent row, e.g. the ``polly`` orchestrator,
        whose bundle contains the sub-agent specs.
    :param sub_agent_name: The dispatched sub-agent's name, e.g.
        ``"claude_code"``.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        disables resolution (returns ``None``).
    :returns: The matching child :class:`AgentSpec`, or ``None`` when the
        cache is absent, the bundle fails to load, or no sub-agent matches.
    """
    if agent_cache is None:
        return None
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        parent_spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        ).spec
    except Exception:  # noqa: BLE001
        # A bundle that fails to load here must not break session
        # creation; the session still works, just without the
        # derived labels / launch args.
        _logger.warning(
            "Could not load bundle for agent %s to resolve sub-agent %r spec",
            agent.id,
            sub_agent_name,
            exc_info=True,
        )
        return None
    return _find_spec_by_name(parent_spec, sub_agent_name)


def _require_declared_subagent(
    *,
    agent: Agent,
    sub_agent_name: str,
    agent_cache: AgentCache | None,
) -> None:
    """
    Reject a ``sub_agent_name`` the parent's spec does not declare.

    ``POST /v1/sessions`` persists ``sub_agent_name`` verbatim, and no
    downstream spec-swap site rejects an unresolvable one: each warns and
    runs the session against the PARENT spec instead. An undeclared name
    would therefore be stored once and answered by the parent for the
    session's whole life, with a warning as the only sign. This gate rejects
    it up front, before any row is persisted, mirroring normal dispatch
    (``tool_dispatch`` rejects an undeclared ``agent``) and the
    ``AGENTSPEC.md`` contract that unlisted names are rejected.

    It narrows, but does not bound, what can reach the downstream fallback.
    The check runs only when the bundle LOADS and the name is positively
    absent: with no agent cache, or on any load failure, it returns without
    adjudicating, so a never-declared name still reaches a swap site by
    either route. What the gate guarantees is one direction only — a name
    this check REJECTED never gets persisted.

    :param agent: The parent agent row whose bundle declares the
        sub-agents.
    :param sub_agent_name: The requested sub-agent name to validate.
    :param agent_cache: Cache for loading the parsed parent bundle.
        ``None`` skips the check (cannot resolve the tree).
    :raises OmnigentError: 404 ``NOT_FOUND`` when the bundle loads and
        declares no sub-agent named ``sub_agent_name``.
    """
    if agent_cache is None:
        return
    from omnigent.runtime.workflow import _find_spec_by_name

    try:
        parent_spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        ).spec
    except Exception:  # noqa: BLE001
        # Can't load the bundle -> can't prove the name is undeclared.
        # Leave it to the runner, which warns and runs the session on the
        # parent spec, rather than rejecting a create we cannot adjudicate.
        return
    if _find_spec_by_name(parent_spec, sub_agent_name) is None:
        raise OmnigentError(
            f"Sub-agent not declared in parent spec: {sub_agent_name!r}",
            code=ErrorCode.NOT_FOUND,
        )


def _spec_harness(spec: AgentSpec) -> str:
    """
    Return the canonical harness identifier for a resolved spec.

    :param spec: A parsed agent / sub-agent spec.
    :returns: The canonical harness id, e.g. ``"claude-native"`` or
        ``"codex-native"``; falls back to ``executor.type`` when no
        ``harness`` is declared.
    """
    from omnigent.harness_aliases import canonicalize_harness

    harness = spec.executor.config.get("harness") or spec.executor.type
    return canonicalize_harness(harness) or harness


def _reject_reserved_cost_control_label_seed(labels: dict[str, str]) -> None:
    """
    Reject a session-create body that seeds policy-owned labels.

    ``cost_control.*`` is the cost advisor's telemetry namespace and its
    only legitimate writer is the session's bound runner — which cannot
    exist yet at create time, so a seed is always a forgery.

    :param labels: The client-supplied initial labels, e.g.
        ``{"team": "ml"}``.
    :raises OmnigentError: 400 when any ``cost_control.*`` key is
        present.
    """
    reserved = reserved_cost_control_keys(labels)
    if reserved:
        raise OmnigentError(
            f"labels {', '.join(repr(key) for key in reserved)} "
            f"are in the policy-owned {COST_CONTROL_LABEL_NAMESPACE}* "
            "namespace and cannot be set at session creation",
            code=ErrorCode.INVALID_INPUT,
        )


def _reject_server_reserved_label_seed(labels: dict[str, str] | None) -> None:
    """
    Reject a client-supplied label map that touches server-internal keys.

    Keys in this set are written exclusively by server internals and must
    not be client-settable — doing so would let callers forge security-
    critical metadata (e.g. the policy-evaluation actor identity).

    :param labels: The client-supplied label mapping, or ``None``.
    :raises OmnigentError: 400 when any reserved key is present.
    """
    if not labels:
        return
    from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY

    if WORKTREE_ROOT_LABEL_KEY in labels:
        raise OmnigentError(
            f"label {WORKTREE_ROOT_LABEL_KEY!r} is server-internal and cannot be set by clients",
            code=ErrorCode.INVALID_INPUT,
        )
    if _TURN_ACTOR_LABEL in labels:
        raise OmnigentError(
            f"label {_TURN_ACTOR_LABEL!r} is server-internal and cannot be set by clients",
            code=ErrorCode.INVALID_INPUT,
        )
    # The archive timestamp is stamped by the server on the archive transition
    # only; a client write would forge the retention clock, including on shared
    # sessions the caller does not own.
    if ARCHIVED_AT_LABEL_KEY in labels:
        raise OmnigentError(
            f"label {ARCHIVED_AT_LABEL_KEY!r} is server-internal and cannot be set by clients",
            code=ErrorCode.INVALID_INPUT,
        )
    # Pins are per-user: the client may only write the bare canonical
    # ``omnigent.pinned`` key (which the route rewrites to the CALLER's per-user
    # key). A suffixed ``omnigent.pinned.<user>`` is server-derived — accepting
    # one from a client would let a caller pin/unpin a shared session for
    # another user, or forge arbitrary per-user pin rows, defeating the per-user
    # isolation. Reject any suffixed form; only the bare key is client-writable.
    suffixed_pin = next(
        (k for k in labels if k.startswith(f"{PINNED_LABEL_KEY}.")),
        None,
    )
    if suffixed_pin is not None:
        raise OmnigentError(
            f"label {suffixed_pin!r} is server-derived; set the bare "
            f"{PINNED_LABEL_KEY!r} key to pin for yourself",
            code=ErrorCode.INVALID_INPUT,
        )
    # Sandbox lifecycle labels are written only by server internals and re-read
    # across a relaunch to rebuild the runner Pod (e.g. the repository it
    # re-clones). A client seed here would forge that reconstruction state, so
    # reserve the whole namespace — every current and future key under it —
    # rather than enumerating one key at a time.
    sandbox_key = next(
        (k for k in labels if k.startswith(MANAGED_SANDBOX_LABEL_NAMESPACE)),
        None,
    )
    if sandbox_key is not None:
        raise OmnigentError(
            f"label {sandbox_key!r} is in the server-internal "
            f"{MANAGED_SANDBOX_LABEL_NAMESPACE}* namespace and cannot be set by clients",
            code=ErrorCode.INVALID_INPUT,
        )


def _require_cost_control_label_authority(
    *,
    reserved_keys: Sequence[str],
    tunnel_token: str | None,
    bound_runner_id: str | None,
    allowed_tunnel_tokens: frozenset[str] | None,
    multi_user: bool,
) -> None:
    """
    Authorize a label write touching the policy-owned ``cost_control.*`` keys.

    These are the cost advisor's telemetry labels, so ordinary session
    editors must not set them via PATCH; the advisor's persist proves
    itself with the runner tunnel binding token (allow-listed, or bound
    to this session's runner id — the tunnel route's trust model).
    Single-user servers skip the check: loopback runners may register
    under stable ids unrelated to any token, and there is no second
    identity to forge against.

    :param reserved_keys: The ``cost_control.*`` keys the request tries
        to write, e.g. ``("cost_control.plan",)``. Quoted in the error.
    :param tunnel_token: Value of the ``X-Omnigent-Runner-Tunnel-Token``
        request header, or ``None`` when absent.
    :param bound_runner_id: The session's current ``runner_id``, or
        ``None`` when no runner is bound.
    :param allowed_tunnel_tokens: The server's tunnel-token allow-list,
        or ``None`` when not configured.
    :param multi_user: ``True`` when the server enforces per-user
        permissions (a permission store is configured).
    :raises OmnigentError: 403 when the caller presents no acceptable
        runner proof on a multi-user server.
    """
    if not multi_user:
        return
    keys = ", ".join(repr(key) for key in reserved_keys)
    token = (tunnel_token or "").strip()
    if token:
        if allowed_tunnel_tokens is not None and token in allowed_tunnel_tokens:
            return
        if bound_runner_id is not None and token_bound_runner_id(token) == bound_runner_id:
            return
    raise OmnigentError(
        f"labels {keys} are in the policy-owned "
        f"{COST_CONTROL_LABEL_NAMESPACE}* namespace; only the session's "
        "bound runner may write them",
        code=ErrorCode.FORBIDDEN,
    )


def _persist_stored_session_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    *,
    agent_id: str,
    agent_name: str,
    agent_bundle_location: str,
    agent_description: str | None,
    runner_id: str | None = None,
    inference_snapshot: dict[str, Any] | None = None,
    inference_model: str | None = None,
    created_by: str | None = None,
) -> CreatedSessionResponse:
    """
    Persist database rows for a bundle already written to artifacts.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for deleting the bundle on failure.
    :param metadata: Validated session metadata. A set
        ``parent_session_id`` creates the conversation as a
        sub-agent child of that session.
    :param agent_id: New agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Agent name loaded from the uploaded spec.
    :param agent_bundle_location: Artifact key for the stored bundle.
    :param agent_description: Optional description from the spec.
    :param runner_id: Optional runner binding inherited from the
        parent session, e.g. ``"runner_abc123"``.
    :param created_by: Identity of the creating user, recorded on the
        session-scoped agent so its code can only be mutated by the owner.
        ``None`` in single-user mode.
    :returns: Response with the new session id.
    :raises OmnigentError: If the agent insert violates integrity
        checks or the parent session no longer exists.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    inference_kwargs: dict[str, Any] = {}
    if inference_snapshot is not None:
        inference_kwargs["inference_snapshot"] = inference_snapshot
    if inference_model is not None:
        inference_kwargs["model_override"] = inference_model
    try:
        created = conversation_store.create_session_with_agent(
            agent_id=agent_id,
            agent_name=agent_name,
            agent_bundle_location=agent_bundle_location,
            agent_description=agent_description,
            title=metadata.title,
            labels=metadata.labels,
            reasoning_effort=metadata.reasoning_effort,
            workspace=metadata.workspace,
            terminal_launch_args=metadata.terminal_launch_args,
            parent_conversation_id=metadata.parent_session_id,
            runner_id=runner_id,
            project_id=metadata.project_id,
            host_id=metadata.host_id,
            created_by=created_by,
            **inference_kwargs,
        )
    except ConversationNotFoundError as exc:
        # Parent was authorized by the caller but vanished (deleted)
        # before the insert transaction ran.
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise OmnigentError(
            str(exc),
            code=ErrorCode.NOT_FOUND,
        ) from exc
    except IntegrityError as exc:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        # Expected integrity failures here are uniqueness collisions:
        # generated agent id, generated conversation id, or
        # agents.session_id. The route maps those to 409.
        raise OmnigentError(
            f"session agent write failed integrity checks: {exc.orig}",
            code=ErrorCode.ALREADY_EXISTS,
        ) from exc
    except SQLAlchemyError:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise

    # The create request has no conv id in its URL; stamp the minted id so
    # the create span joins the session's session.id group.
    from omnigent.runtime import telemetry

    telemetry.set_session_id(created.conversation.id)
    return CreatedSessionResponse(
        session_id=created.conversation.id,
        agent_id=agent_id,
        agent_name=agent_name,
    )


def _delete_stored_session_bundle_after_failure(
    artifact_store: ArtifactStore,
    agent_bundle_location: str,
) -> None:
    """
    Delete an uploaded bundle after database creation fails.

    Cleanup failures are logged but suppressed so the original
    exception remains the error seen by callers.

    :param artifact_store: Store that contains the uploaded bundle.
    :param agent_bundle_location: Artifact key to delete, e.g.
        ``"ag_abc123/a1b2c3d4"``.
    :returns: None.
    """
    try:
        artifact_store.delete(agent_bundle_location)
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Failed to delete uploaded session bundle %s after rollback",
            agent_bundle_location,
            exc_info=True,
        )


async def _authorize_bundled_parent_and_inherit_runner(
    parent_session_id: str,
    *,
    user_id: str | None,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> str | None:
    """
    Authorize a bundled create's parent link and resolve runner affinity.

    The caller must have READ access to the parent session
    before inheriting anything, mirroring the JSON create path —
    without this, a forged parent link lets the caller inherit runner
    bindings and parent a session they don't control. On success the
    parent's runner binding is inherited (sub-agent co-location),
    subject to a defense-in-depth ownership check: a runner the
    caller doesn't own is not inherited.

    :param parent_session_id: The requested parent session id,
        e.g. ``"conv_abc123"``.
    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``.
    :param permission_store: Permission store for the access
        check; ``None`` in single-user / no-auth mode.
    :param conversation_store: Store for the parent-conversation read.
    :param runner_router: Router for the runner-ownership check;
        ``None`` skips it.
    :returns: The inherited runner id, or ``None`` when the parent has
        no runner binding or ownership disallows inheritance.
    :raises OmnigentError: 403/404 when the caller may not access the
        parent session.
    """
    await _require_access(
        user_id,
        parent_session_id,
        LEVEL_READ,
        permission_store,
        conversation_store,
    )
    parent_conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        parent_session_id,
    )
    if parent_conv is None:
        return None
    inherited_runner_id = parent_conv.runner_id
    if inherited_runner_id is not None and user_id is not None and runner_router is not None:
        runner_owner = runner_router.runner_owner(inherited_runner_id)
        if runner_owner is not None and runner_owner != user_id:
            return None
    return inherited_runner_id


async def _notify_runner_of_bundled_child(
    session_id: str,
    agent_id: str,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Notify the inherited runner that a bundled child session exists.

    Lets the runner initialize per-session state (inbox queue,
    agent-id cache) before the first forwarded event, mirroring the
    JSON create path's post-create notify. Failures are logged and
    swallowed — the notify is additive and must not fail the create.

    :param session_id: The new child session id, e.g. ``"conv_abc123"``.
    :param agent_id: The child's session-scoped agent id,
        e.g. ``"ag_abc123"``.
    :param runner_router: Router used to resolve the bound runner's
        client; ``None`` falls back to the in-process runner.
    :returns: None.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        return
    try:
        # Bundled children keep the legacy id-only body: bundle creation plumbs
        # no harness/model override for a session-init envelope to seed. The
        # create and rebind notifies send the full envelope instead.
        await runner_client.post(
            "/v1/sessions",
            json={
                "session_id": session_id,
                "agent_id": agent_id,
                "sub_agent_name": None,
            },
            timeout=10.0,
        )
    except (httpx.HTTPError, ConnectionError):
        _logger.warning(
            "Failed to notify runner about bundled session %s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )


def _registered_runner_id(
    runner_router: RunnerRouter | None,
    raw_runner_id: str,
    *,
    user_id: str | None = None,
) -> str:
    """
    Validate a runner id from ``PATCH /v1/sessions/{id}``.

    When ``user_id`` is provided the function also enforces runner
    ownership: only the user who established the tunnel may
    bind sessions to that runner.

    :param runner_router: Router backed by the live tunnel registry.
        ``None`` means this server cannot bind runners.
    :param raw_runner_id: Runner id from the request body, e.g.
        ``"runner_abc123"``.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. ``None`` skips the ownership
        check (single-user / no-auth mode).
    :returns: Trimmed registered runner id.
    :raises OmnigentError: If the id is empty, the router is
        unavailable, the runner is not registered, or the caller
        does not own the runner.
    """
    runner_id = raw_runner_id.strip()
    if not runner_id:
        raise OmnigentError(
            "runner_id must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if runner_router is None:
        raise OmnigentError(
            "runner router is not configured",
            code=ErrorCode.INTERNAL_ERROR,
        )
    if not runner_router.runner_is_online(runner_id):
        raise OmnigentError(
            f"runner {runner_id!r} is not registered",
            code=ErrorCode.INVALID_INPUT,
        )
    # Enforce runner ownership. A caller must own the runner
    # they are trying to bind to a session.
    if user_id is not None:
        runner_owner = runner_router.runner_owner(runner_id)
        if runner_owner is not None and runner_owner != user_id:
            raise OmnigentError(
                f"runner {runner_id!r} is not owned by the requesting user",
                code=ErrorCode.FORBIDDEN,
            )
    return runner_id


def _latest_message_preview(
    items: list[ConversationItem],
    limit_chars: int = _CHILD_PREVIEW_LIMIT,
) -> str | None:
    """
    Return a single-line text preview from newest-first message items.

    Powers the sub-agent rail row's status line so the user can see what
    the child is saying without opening it. The caller supplies a
    batched newest-first message list for one child; this function joins
    ``input_text`` / ``output_text`` blocks from the first non-meta
    message with text, collapses whitespace, and truncates to
    ``limit_chars``. Hidden meta messages carry durable runner context
    and must never be shown as user-facing previews.

    :param items: Newest-first message items for one conversation.
    :param limit_chars: Max preview length in characters,
        e.g. ``150``.
    :returns: Truncated single-line preview text, e.g.
        ``"I'll search the codebase for references…"``, or ``None``.
    """
    for item in items:
        if not isinstance(item.data, MessageData) or item.data.is_meta:
            continue
        parts: list[str] = []
        for block in item.data.content:
            block_type = block.get("type")
            text = block.get("text")
            if block_type in ("input_text", "output_text") and isinstance(text, str):
                parts.append(text)
        collapsed = " ".join(" ".join(parts).split())
        if not collapsed:
            continue
        if len(collapsed) <= limit_chars:
            return collapsed
        # Trim to one char less than the limit so the trailing ellipsis
        # keeps the field at ``limit_chars`` total.
        return collapsed[: max(0, limit_chars - 1)].rstrip() + "…"
    return None


def _child_session_current_task_status_from_cached_status(status: object) -> str | None:
    """
    Map cached session lifecycle status onto child-summary task status.

    :param status: Cached ``session.status`` value.
    :returns: Public ``ChildSessionSummary.current_task_status`` value.
    """
    if status in ("running", "waiting"):
        return "in_progress"
    if status == "idle":
        return "completed"
    if status == "failed":
        return "failed"
    return None


def _child_session_summary_from_conversation(
    conv: Conversation,
    parent_session_id: str,
    last_message_preview: str | None,
    *,
    cached_status: str | None = None,
) -> ChildSessionSummary:
    """
    Build a :class:`ChildSessionSummary` from a child conversation.

    Parses the canonical sub-agent title format
    ``"{agent_type}:{session_name}"`` written by
    :func:`omnigent.tools.builtins.spawn._spawn_one`, plus the
    3-segment ``"ui:{agent_name}:{user_label}"`` form written by the
    Web UI "Add agent" flow (surfaced as ``tool={agent_name}`` and
    ``session_name={user_label}``). Tolerates malformed/legacy rows:
    if the title is ``None`` or has no colon, ``tool`` falls back to
    the raw title and ``session_name`` is ``None`` — the row is still
    surfaced so debug views can investigate.

    Native-harness children are the exception: their titles are
    uniqueness keys built from opaque runtime ids, so Codex and Claude
    rows take ``tool`` from their labels instead of the title.

    ``busy`` is derived from the relay-fed ``_session_status_cache``
    (the tasks table has been removed). ``agent_id`` and ``agent_name``
    are read from the conversation row directly.

    :param conv: A child :class:`Conversation` row
        (``kind="sub_agent"``) from
        :meth:`ConversationStore.list_conversations`.
    :param parent_session_id: The parent session id from the
        route, e.g. ``"conv_parent987"``. Passed in rather than
        re-reading from ``conv.parent_conversation_id`` to keep
        the helper indifferent to legacy rows where the FK might
        be missing.
    :param last_message_preview: Preview text derived from a batched
        child-message lookup, or ``None`` when no visible message exists.
    :param cached_status: Session status to derive ``busy`` /
        ``current_task_status`` from, e.g. ``"running"``. ``None`` reads the
        live ``_session_status_cache``; a status-edge publisher passes the
        edge's own value so a burst of transitions fans out one summary per
        edge instead of the latest status repeated.
    :returns: A populated :class:`ChildSessionSummary`.
    """
    display_title = title_without_closed_marker(conv.title)
    # Child sessions aren't pinnable (the pin affordance lives on top-level
    # sidebar rows only), but strip any per-user ``omnigent.pinned.<user>`` keys
    # defensively so a shared child's summary can never expose another viewer's
    # pin key. No collapse-to-canonical here: there's no pin to surface.
    raw_labels = {k: v for k, v in conv.labels.items() if not k.startswith(f"{PINNED_LABEL_KEY}.")}
    labels = labels_with_closed_status(raw_labels, conv.title)
    tool: str | None
    session_name: str | None
    if display_title and ":" in display_title:
        head, _, tail = display_title.partition(":")
        if head == _UI_ADDED_AGENT_TITLE_PREFIX and ":" in tail:
            # User-added agent: "ui:<agent_name>:<user_label>". Surface the
            # bound agent as ``tool`` and the user's label as ``session_name``
            # so the Agents rail renders it like any other child row.
            agent_name, _, user_label = tail.partition(":")
            tool = agent_name
            session_name = user_label
        else:
            tool = head
            session_name = tail
    else:
        tool = display_title or None
        session_name = None

    # Derive busy from the relay-fed cache; tasks table is gone.
    if cached_status is None:
        cached_status = _session_status_cache.get(conv.id)
    if cached_status in ("running", "waiting"):
        busy = True
    else:
        busy = False
    last_task_error = _last_task_error_from_labels(labels)
    current_task_status = _child_session_current_task_status_from_cached_status(cached_status)
    if last_task_error is not None:
        current_task_status = "failed"

    routing_decision_id = conv.labels.get(ROUTING_DECISION_LABEL_KEY)
    return ChildSessionSummary(
        id=conv.id,
        parent_session_id=parent_session_id,
        title=display_title,
        task_summary=conv.task_summary,
        tool=tool,
        session_name=session_name,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        # agent_id comes from the conversation row; agent_name and task_id
        # are no longer available from the (removed) tasks table.
        agent_id=conv.agent_id,
        agent_name=None,
        current_task_id=None,
        current_task_status=current_task_status,
        busy=busy,
        labels=labels,
        last_task_error=last_task_error,
        last_message_preview=last_message_preview,
        # Surface the sub-agent's parked-elicitation count from the same
        # in-memory index that feeds the sidebar badge, so the Agents
        # rail can flag a child that's awaiting user input.
        pending_elicitations_count=pending_elicitations.count_for(conv.id),
        # The model routing picked for this child, reported only when a
        # decision actually produced it: a user-pinned model_override is not a
        # routed model, and reporting one with a null decision id makes the
        # two fields contradict each other. The decision is joined through a
        # conversation label rather than a new column.
        routed_model=conv.model_override if routing_decision_id is not None else None,
        routing_decision_id=routing_decision_id,
    )


def _mcp_tool_result(rpc_id: int | str | None, text: str) -> Response:
    """
    Wrap a plain-text tool result in a JSON-RPC 2.0 MCP ``tools/call`` response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param text: The tool output text to embed in the ``content`` block.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope with a single ``text`` content block.
    """
    body = json.dumps(
        {"jsonrpc": "2.0", "id": rpc_id, "result": {"content": [{"type": "text", "text": text}]}}
    )
    return Response(content=body, media_type="application/json")


async def _handle_advise_models_mcp(
    rpc_id: int | str | None,
    conv: Any,
    arguments: dict[str, Any],
    agent_store: Any,
    *,
    session_id: str | None = None,
    runner_router: Any = None,
) -> Response:
    """
    Server-side handler for ``sys_advise_models`` MCP tool calls.

    Intercepts the call before the runner forward because the deployment's
    routing backends live in the server process.

    :param rpc_id: The JSON-RPC request id.
    :param conv: The :class:`Conversation` for this session.
    :param arguments: Parsed tool arguments from the LLM.
    :param agent_store: Store for agent lookup (used to resolve sub-agent harnesses).
    :returns: A JSON-RPC 2.0 ``tools/call`` result response.
    """
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list):
        return _mcp_tool_result(
            rpc_id, json.dumps({"error": "tasks must be a list", "router_on": False})
        )

    from omnigent.server.routing_backend import backends_from_caps

    caps = get_caps()
    routing_client = backends_from_caps(caps).any()
    if routing_client is None:
        return _mcp_tool_result(rpc_id, json.dumps({"router_on": False, "recommendations": []}))

    from omnigent.models.model_catalog import spec_harness
    from omnigent.server.smart_routing import _WORKER_NAME_TO_HARNESS, fetch_runner_models

    # Fetch live model catalog from the runner once; used below to populate
    # per-agent model lists when the caller omits explicit models.
    # Keys are worker names ("self", "claude_code", etc.) as returned by
    # catalog_for_spec. None when runner discovery is unavailable.
    _runner_catalog: dict[str, list[str]] | None = None
    if session_id is not None and runner_router is not None:
        _runner_client = await _get_runner_client(session_id, runner_router)
        if _runner_client is not None:
            _runner_catalog = await fetch_runner_models(session_id, _runner_client)

    # Resolve the parent agent spec to look up sub-agent harnesses.
    spec: Any | None = None
    if conv.agent_id is not None:
        agent_obj = await asyncio.to_thread(agent_store.get, conv.agent_id)
        if agent_obj is not None:
            try:
                spec = (
                    get_agent_cache()
                    .load(
                        agent_obj.id,
                        agent_obj.bundle_location,
                        expand_env=agent_obj.session_id is None,
                    )
                    .spec
                )
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "_handle_advise_models_mcp: failed to load spec for agent=%s", conv.agent_id
                )

    def _resolve_harness_for_worker(agent: str) -> str | None:
        if spec is not None:
            sub_agents = getattr(spec, "sub_agents", None) or []
            for sub in sub_agents:
                if getattr(sub, "name", None) == agent:
                    h = spec_harness(sub)
                    if h:
                        return h
                    break
        return _WORKER_NAME_TO_HARNESS.get(agent)

    recommendations: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title = task.get("title", "")
        task_text = task.get("task", "")
        agents_spec = task.get("agents")
        if not isinstance(agents_spec, list) or not agents_spec:
            continue

        # Build harness→models map for the routing client, plus two reverse
        # maps for resolving the chosen agent after the verdict:
        # - harness_to_agent: preferred path when the judge picks a harness
        # - model_to_agent: fallback when harness is absent or unrecognised
        # Insertion order is preserved; first-agent-wins dedup applies when
        # the same model appears in multiple harness lists.
        model_to_agent: dict[str, str] = {}
        harness_to_agent: dict[str, str] = {}
        harness_models: dict[str, list[str]] = {}
        for agent_entry in agents_spec:
            if not isinstance(agent_entry, dict):
                continue
            agent = agent_entry.get("agent", "")
            explicit_models: list[str] | None = agent_entry.get("models")
            if explicit_models is not None and not isinstance(explicit_models, list):
                explicit_models = None
            if explicit_models:
                harness_key = agent  # use agent name as key when models are explicit
                candidates = explicit_models
            else:
                harness_key = _resolve_harness_for_worker(agent) or agent
                # Prefer the worker name, then its normalized harness key.
                candidates = (
                    (_runner_catalog or {}).get(agent)
                    or (_runner_catalog or {}).get(harness_key)
                    or []
                )
            if candidates:
                harness_models.setdefault(harness_key, [])
                harness_to_agent.setdefault(harness_key, agent)
                for m in candidates:
                    if m not in model_to_agent:
                        model_to_agent[m] = agent
                        harness_models[harness_key].append(m)

        if not harness_models:
            recommendations.append(
                {"title": title, "agent": None, "model": None, "rationale": "no candidates"}
            )
            continue
        try:
            verdict = await routing_client.route(task_text, harness_models)
        except Exception:  # noqa: BLE001  # routing failures must not crash the advisor
            _logger.exception("_handle_advise_models_mcp: route failed task=%r", title)
            verdict = None
        if verdict is None:
            recommendations.append(
                {
                    "title": title,
                    "agent": None,
                    "model": None,
                    "rationale": "router returned no verdict",
                }
            )
        else:
            # Prefer the judge's harness pick; fall back to model ownership.
            chosen_agent = (
                harness_to_agent.get(verdict.harness) if verdict.harness else None
            ) or model_to_agent.get(verdict.model)
            recommendations.append(
                {
                    "title": title,
                    "agent": chosen_agent,
                    "model": verdict.model,
                    "rationale": verdict.rationale,
                }
            )

    return _mcp_tool_result(
        rpc_id, json.dumps({"router_on": True, "recommendations": recommendations})
    )


def _mcp_ok_response(rpc_id: int | str | None, result: dict[str, Any]) -> Response:
    """
    Wrap *result* in a JSON-RPC 2.0 success response.

    :param rpc_id: The JSON-RPC request id (may be int, str, or ``None``
        for notifications), e.g. ``1``.
    :param result: The JSON-serialisable result payload, e.g.
        ``{"tools": [...]}``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 envelope.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result})
    return Response(content=body, media_type="application/json")


def _mcp_error_response(
    rpc_id: int | str | None,
    code: int,
    message: str,
) -> Response:
    """
    Wrap an error in a JSON-RPC 2.0 error response.

    :param rpc_id: The JSON-RPC request id. Use ``None`` when the id
        could not be parsed, e.g. ``None``.
    :param code: JSON-RPC error code, e.g. ``-32601`` (method not found)
        or ``-32000`` (application error).
    :param message: Human-readable error description,
        e.g. ``"Method not found: 'unsupported/method'"``.
    :returns: A :class:`Response` with ``Content-Type: application/json``
        carrying the JSON-RPC 2.0 error envelope.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": code, "message": message},
        }
    )
    return Response(content=body, media_type="application/json")


def _mcp_input_required_response(
    rpc_id: int | str | None,
    elicitation_id: str,
    message: str,
    request_state: str,
    session_id: str | None = None,
    ask_timeout: int | None = None,
) -> Response:
    """
    Return an MCP ``InputRequiredResult`` asking the runner to collect
    user approval before retrying the tool call.

    Follows the Multi Round-Trip Requests (MRTR) spec:
    ``https://modelcontextprotocol.io/specification/draft/basic/utilities/mrtr``.
    The ``elicitation_id`` is used as the key in ``inputRequests`` so the
    runner can identify the approval Future without inspecting the opaque
    ``requestState``. When URL-mode is active and ``session_id`` is
    known, adds ``mode``/``url`` to params.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param elicitation_id: Server-minted elicitation id used both as the
        ``inputRequests`` key and inside the opaque ``requestState``,
        e.g. ``"elicit_abc123"``.
    :param message: Human-readable prompt shown to the user,
        e.g. ``"Allow tool sys_os_shell?"``.
    :param request_state: Opaque state blob the client echoes on retry.
        Contains the ``elicitation_id`` and ``session_id`` so the server
        can verify authenticity on retry without server-side storage.
    :param session_id: Session/conversation id for constructing the
        approval page URL, e.g. ``"conv_abc123"``. ``None`` omits the
        URL (form mode).
    :param ask_timeout: Seconds the deciding policy lets the prompt wait,
        e.g. ``86400``; carried as ``params.ask_timeout`` so the runner's
        park is bounded by it. ``None`` leaves the runner's default.
    :returns: A :class:`Response` carrying the JSON-RPC 2.0
        ``InputRequiredResult`` envelope.
    """

    params: dict[str, Any] = {
        "message": message,
        "requestedSchema": {
            "type": "object",
            "properties": {"approved": {"type": "boolean"}},
            "required": ["approved"],
        },
    }
    if session_id is not None and _ELICITATION_MODE == "url":
        params["mode"] = "url"
        params["url"] = f"/approve/{session_id}/{elicitation_id}"
    else:
        params["mode"] = "form"
    if ask_timeout is not None:
        params["ask_timeout"] = ask_timeout

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "resultType": "input_required",
                "inputRequests": {
                    elicitation_id: {
                        "method": "elicitation/create",
                        "params": params,
                    }
                },
                "requestState": request_state,
            },
        }
    )
    return Response(content=body, media_type="application/json")


async def _handle_mcp_tools_list(
    rpc_id: int | str | None,
    session_id: str,
    runner_router: RunnerRouter | None,
) -> Response:
    """
    Handle a ``tools/list`` JSON-RPC request for the MCP proxy endpoint.

    Delegates execution to the runner's ``POST
    /v1/sessions/{id}/mcp/execute`` endpoint so that stdio MCP
    subprocesses spawn on the runner's machine (correct ``cwd``,
    env, and tooling). The Omnigent server's role here is routing only —
    policy evaluation happens in ``tools/call``.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id whose agent's tools to list,
        e.g. ``"conv_abc123"``.
    :param runner_router: Router used to get an httpx client pointed
        at the session's runner. ``None`` returns an error.
    :returns: A JSON-RPC 2.0 ``tools/list`` result response, or an
        error response when the runner is unavailable.
    """
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        # Fall back to the in-process runner client (local single-user mode).
        from omnigent.runtime import get_runner_client

        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    _logger.debug(
        "MCP tools/list: delegating to runner execute for session=%r",
        session_id,
        extra={"session_id": session_id},
    )
    try:
        resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={"method": "tools/list", "params": {}},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in data:
        err = data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    result = data.get("result", {})
    # schemas are already in OpenAI function-tool format from RunnerMcpManager;
    # convert back to MCP inputSchema format for the tools/list response since
    # ProxyMcpManager on the runner expects MCP-shaped tools/list output.
    schemas: list[dict[str, Any]] = result.get("schemas", [])
    tools = []
    for schema in schemas:
        # schema shape: {"type": "function", "name": "srv__tool",
        #                "description": "...", "parameters": {...}}
        tools.append(
            {
                "name": schema.get("name", ""),
                "description": schema.get("description", ""),
                "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    failures: dict[str, str] = result.get("failures", {})
    for srv, msg in failures.items():
        _logger.warning("runner MCP server %r unavailable: %s", srv, msg)

    _logger.debug(
        "MCP tools/list: session=%r returning %d tools, %d failures",
        session_id,
        len(tools),
        len(failures),
        extra={"session_id": session_id},
    )
    return _mcp_ok_response(rpc_id, {"tools": tools})


async def _read_upload_capped(file: UploadFile, limit_bytes: int) -> bytes:
    """
    Read an uploaded file into memory, aborting if it exceeds *limit_bytes*.

    Reads in :data:`_UPLOAD_READ_CHUNK_BYTES` chunks and raises HTTP 413 as
    soon as the cap is crossed, so an oversized upload never buffers more
    than one chunk past the limit.

    :param file: The multipart upload.
    :param limit_bytes: Maximum allowed size in bytes.
    :returns: The full file content.
    :raises HTTPException: 413 when the upload exceeds *limit_bytes*.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Attachment exceeds the {limit_bytes // (1024 * 1024)} MB "
                    "limit for this file type."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


# Page size for walking a session's files when totalling its filesystem attachments.
_FILESYSTEM_QUOTA_PAGE_SIZE = 100


async def _require_filesystem_attachment_harness(filename: str) -> None:
    """Reject filesystem attachments: no supported harness can open them.

    :param filename: The attached file, named in the error.
    :raises HTTPException: 415 always.
    """
    raise HTTPException(
        status_code=415,
        detail=(
            f"'{filename}' cannot be attached: this session's harness cannot open this file type."
        ),
    )


def require_filesystem_attachment_runtime(
    *,
    host_id: str | None,
    runner_id: str | None,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    runner_router: RunnerRouter | None = None,
) -> None:
    """Require a connected build that can deliver and restore native file attachments.

    :param host_id: Session's assigned host, or None before host selection.
    :param runner_id: Session's current runner, when already launched.
    :param host_registry: Live host connections on this server replica.
    :param tunnel_registry: Live runner connections on this server replica.
    :param runner_router: Router used to distinguish a remote host from an offline one.
    :raises OmnigentError: When the runtime needs an upgrade, connection, or reroute.
    """
    from omnigent.harnesses.native_attachments import CAP_FILESYSTEM_ATTACHMENTS

    host = host_registry.get(host_id) if host_id and host_registry is not None else None
    runner = tunnel_registry.get(runner_id) if runner_id and tunnel_registry is not None else None
    for connection in (host, runner):
        if (
            connection is not None
            and CAP_FILESYSTEM_ATTACHMENTS not in connection.hello.capabilities
        ):
            raise OmnigentError(
                "Update Omnigent on this host and restart it before attaching archives, "
                "Office documents, or databases. This host cannot restore these files "
                "when a session resumes.",
                code=ErrorCode.CONFLICT,
            )
    if host is not None or runner is not None:
        return
    if host_id and runner_router is not None and runner_router.host_is_on_another_replica(host_id):
        raise OmnigentError(
            "Attachment support must be checked on the host's server replica",
            code=ErrorCode.WRONG_REPLICA,
        )
    raise OmnigentError(
        "Connect an updated Omnigent host before attaching archives, Office documents, "
        "or databases, then retry.",
        code=ErrorCode.CONFLICT,
    )


def _enforce_filesystem_attachment_policy(
    filenames: Sequence[str],
    *,
    session_id: str | None,
    file_store: FileStore,
    sizes: Sequence[int] | None = None,
) -> int:
    """
    Apply deployment policy to files requiring filesystem tools entering a session.

    Enforces the operator denylist and the per-session file-count and total-byte
    quotas before any bytes are read, so a rejected upload or copy never
    buffers. The server accounts for all stored uploads, including files that
    are not currently present in the runner cache.

    :param filenames: The incoming files' names, e.g. ``["bundle.zip"]``.
    :param session_id: Destination session, whose existing attachments are counted,
        or ``None`` for a new, empty destination.
    :param file_store: Store used to total the session's current usage.
    :param sizes: The incoming files' byte sizes when already known (a copy),
        checked against the per-file and remaining-session limits. ``None``
        for an upload, whose size is enforced by the returned read cap.
    :returns: The byte cap a single upload must stay within.
    :raises HTTPException: 415 when an extension is denied by configuration,
        or 413 when the files would exceed a per-file or per-session quota.
    """
    from omnigent.harnesses.native_attachments import requires_filesystem
    from omnigent.server.server_config import (
        filesystem_attachment_denied_extensions,
        filesystem_attachment_file_limit,
        filesystem_attachment_total_bytes_limit,
        filesystem_attachment_upload_limit,
    )

    denied = filesystem_attachment_denied_extensions()
    for filename in filenames:
        suffix = PurePath(filename).suffix.lower()
        if suffix in denied:
            raise HTTPException(
                status_code=415,
                detail=f"Attachments of type '{suffix}' are not accepted by this deployment.",
            )

    max_files = filesystem_attachment_file_limit()
    max_total_bytes = filesystem_attachment_total_bytes_limit()
    per_file = filesystem_attachment_upload_limit()

    used_files = 0
    used_bytes = 0
    after: str | None = None
    # Walk every page: stopping at a fixed page count would let a session hide
    # filesystem attachments behind enough inline ones. The walk ends early once the
    # quota is already exhausted, since the answer can't change after that.
    while (
        session_id is not None
        and used_files + len(filenames) <= max_files
        and used_bytes < max_total_bytes
    ):
        page = file_store.list(
            session_id=session_id,
            limit=_FILESYSTEM_QUOTA_PAGE_SIZE,
            after=after,
            order="asc",
        )
        for stored in page.data:
            # This quota covers the types that require filesystem tools.
            if requires_filesystem(stored.filename):
                used_files += 1
                used_bytes += stored.bytes
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id

    if used_files + len(filenames) > max_files:
        raise HTTPException(
            status_code=413,
            detail=(
                f"This session already holds {used_files} file attachments "
                f"(limit {max_files}). Remove one before attaching another."
            ),
        )
    remaining = max_total_bytes - used_bytes
    if remaining <= 0 or (sizes is not None and sum(sizes) > remaining):
        raise HTTPException(
            status_code=413,
            detail=(
                f"This session's file attachments would exceed the "
                f"{max_total_bytes // (1024 * 1024)} MB limit "
                f"({used_bytes // (1024 * 1024)} MB already used)."
            ),
        )
    if sizes is not None and any(size > per_file for size in sizes):
        raise HTTPException(
            status_code=413,
            detail=(f"File attachments are limited to {per_file // (1024 * 1024)} MB each."),
        )

    # Cap an upload at whichever is smaller: the per-file limit, or the
    # session's remaining budget. Without the second term a single upload could
    # overshoot the session total by nearly a whole file.
    return min(per_file, remaining)


#: How many sessions may warm their catalogs at once. A tunnel flap reconnects
#: every session bound to the runner at once, and each warm-up costs a runner
#: round trip plus a provider listing on a worker thread; the cap keeps that
#: burst off the executor the concurrent session re-init needs.
_CATALOG_PREFETCH_CONCURRENCY = 4

#: One semaphore per event loop: the server runs a single loop, but an asyncio
#: primitive cannot be shared across the loops the test suite creates.
# custom-lint: disable-next=workspace-scoped-cache -- keyed by object identity
_catalog_prefetch_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


def _catalog_prefetch_semaphore() -> asyncio.Semaphore:
    """
    Return this loop's catalog-prefetch concurrency gate.

    :returns: The semaphore bounding concurrent prefetches.
    """
    loop = asyncio.get_running_loop()
    semaphore = _catalog_prefetch_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_CATALOG_PREFETCH_CONCURRENCY)
        _catalog_prefetch_semaphores[loop] = semaphore
    return semaphore


async def _run_catalog_prefetch(
    coro: Coroutine[Any, Any, Any],
    session_id: str,
) -> None:
    """
    Run one best-effort catalog prefetch under the concurrency cap.

    Retrieves its own exception: a prefetch is fire-and-forget, so anything it
    raises (a runner round trip torn down mid-flight, say) would otherwise
    surface as an unretrieved-task warning and drown real errors. A failure
    here just leaves a cold cache, which every reader already handles.

    :param coro: The prefetch coroutine to run.
    :param session_id: Session/conversation identifier, for the log line.
    """
    try:
        async with _catalog_prefetch_semaphore():
            await coro
    except asyncio.CancelledError:
        coro.close()
        raise
    except Exception:  # noqa: BLE001 - best-effort warm-up; a cold cache is fine.
        _logger.debug(
            "Catalog prefetch failed for %s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )


def prefetch_session_routing_catalogs(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
) -> None:
    """
    Warm the catalogs routing reads, at session launch instead of at turn time.

    Both are runner-derived, and paying for them while a user's prompt is held
    is what made a first routed message slow: the native picker vocabulary is
    awaited by the turn path when its cached entry is stale, and the runner
    model catalog is a round trip per turn for panes that have no picker
    vocabulary. Started when the runner binds, both land well before the first
    prompt; a turn arriving before they finish still falls back to its own
    inline fetch, so this only ever removes waiting.

    Only routed, live sessions warm anything. Smart Routing is the sole reader
    of these caches, and the caller reconnects *every* session bound to a
    runner — a plain-session host with a flapping tunnel would otherwise spend
    two runner round trips per pane, on nothing, while the session re-init
    running alongside it waits for the same executor. Archived sessions are
    skipped for the same reason: nothing is going to route a turn on them.

    Fire-and-forget: a failed prefetch is a cold cache, which every reader
    already handles.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param conv: Conversation row, read for the routing state and the wrapper
        label.
    :param runner_client: HTTP client pointed at the newly bound runner.
    """
    from omnigent.runner.subagent_routing import routing_class_from_snapshot
    from omnigent.server.smart_routing import prefetch_runner_catalog

    if conv.archived:
        return
    routing_class = routing_class_from_snapshot(
        cost_control_mode=conv.cost_control_mode_override,
        harness_override=conv.harness_override,
        labels=conv.labels,
    )
    if not routing_class.routing_enabled:
        return

    _catalog_prefetch_tasks.add(
        task := asyncio.create_task(
            _run_catalog_prefetch(prefetch_runner_catalog(session_id, runner_client), session_id)
        )
    )
    task.add_done_callback(_catalog_prefetch_tasks.discard)


__all__ = [
    "FILE_CONTENT_CACHE_CONTROL",
    "SessionLiveness",
    "_HostLaunchAttempt",
    "_RunnerForwardResult",
    "_SessionEventDispatchResult",
    "_add_model_usage_delta",
    "_agent_provider_family",
    "_ancestor_session_ids",
    "_announce_session_added",
    "_apply_liveness_to_items",
    "_apply_pending_policy_ask_writes",
    "_attachment_disposition",
    "_authorize_bundled_parent_and_inherit_runner",
    "_await_settled_managed_launch",
    "_background_task_delivery_status",
    "_build_actor",
    "_build_evaluation_context",
    "_build_new_item",
    "_build_policy_engine_from_spec",
    "_build_skill_slash_command_policy_body",
    "_canonical_tool_input",
    "_child_session_current_task_status_from_cached_status",
    "_child_session_summary_from_conversation",
    "_coerce_cumulative_field",
    "_collect_descendant_conversation_ids",
    "_consume_pre_resolved_harness_elicitation",
    "_create_session_worktree",
    "_delete_stored_session_bundle_after_failure",
    "_descendant_sessions",
    "_discovery_key",
    "_dispatch_skill_slash_command_to_runner",
    "_emit_server_routing_decision",
    "_error_item_from_sse",
    "_evaluate_output_policy",
    "_extract_assistant_text_from_event",
    "_extract_persistent_item_from_sse",
    "_extract_user_text_for_routing",
    "_extract_user_text_from_event",
    "_file_content_etag",
    "_flush_relay_text",
    "_format_sse",
    "_forward_approval_to_runner",
    "_forward_session_change_to_runner",
    "_get_runner_client",
    "_get_runner_client_for_resource_access",
    "_grant_default_public",
    "_handle_advise_models_mcp",
    "_handle_mcp_tools_list",
    "_if_none_match_matches",
    "_invalidate_runner_backed_snapshot_state",
    "_last_task_error_from_labels",
    "_latest_assistant_text_from_store",
    "_latest_message_preview",
    "_launch_runner_on_host",
    "_load_agent_spec_for_session",
    "_mcp_error_response",
    "_mcp_input_required_response",
    "_mcp_ok_response",
    "_mcp_tool_result",
    "_merge_pending_file_blocks",
    "_message_text",
    "_model_usage_bucket",
    "_multipart_missing_detail",
    "_native_ask_gate_lock",
    "_notify_runner_of_bundled_child",
    "_owner_from_grants",
    "_parse_external_assistant_message",
    "_parse_external_conversation_item",
    "_parse_session_create_metadata",
    "_parse_skill_slash_command",
    "_pending_elicitation_snapshot_for_session",
    "_permission_level_from_grants",
    "_persist_external_assistant_message",
    "_persist_external_model_change",
    "_persist_external_reasoning_effort_change",
    "_persist_external_session_title",
    "_persist_policy_deny_sentinel",
    "_persist_session_status_error_labels",
    "_persist_stored_session_bundle",
    "_poll_request_disconnect",
    "_priced_cost_for_display",
    "_provision_managed_sandbox",
    "_proxy_get_session_resources_to_runner",
    "_prune_pre_resolved_harness_elicitations",
    "_prune_session_read_state",
    "_publish_and_persist_resource_event",
    "_publish_btw_sidechat",
    "_publish_changed_files_invalidated",
    "_publish_compaction_completed",
    "_publish_compaction_failed",
    "_publish_compaction_in_progress",
    "_publish_elicitation_request_to_ancestors",
    "_publish_elicitation_resolved",
    "_publish_elicitation_resolved_to_ancestors",
    "_publish_error_event",
    "_publish_external_assistant_message",
    "_publish_external_conversation_item",
    "_publish_external_output_reasoning_delta",
    "_publish_external_output_text_delta",
    "_publish_external_tool_output_delta",
    "_publish_input_consumed",
    "_publish_input_deny_terminal",
    "_publish_interrupted",
    "_publish_mcp_startup",
    "_publish_policy_denied",
    "_publish_policy_deny",
    "_publish_sandbox_status",
    "_publish_session_superseded",
    "_publish_status",
    "_publish_terminal_pending",
    "_query_host_runner_status",
    "_read_state_entry",
    "_read_upload_capped",
    "_record_daily_cost",
    "_record_turn_failure",
    "_registered_runner_id",
    "_reject_reserved_cost_control_label_seed",
    "_reject_server_reserved_label_seed",
    "_relay_persist",
    "_relay_persist_error_once",
    "_remove_session_worktree_best_effort",
    "_replace_text_in_message_body",
    "_require_cost_control_label_authority",
    "_require_declared_subagent",
    "_require_external_status_forward",
    "_require_filesystem_attachment_harness",
    "_require_host_conn_for_worktree",
    "_reset_runner_resources_after_switch",
    "_reset_runner_resources_after_switch_impl",
    "_resolve_harness",
    "_resolve_llm_model",
    "_resolve_skill_meta_text_via_runner",
    "_resolve_subagent_spec",
    "_resource_event_item_from_sse",
    "_routing_decision_item_from_sse",
    "_same_provider_family",
    "_same_provider_family_impl",
    "_seed_missing_title",
    "_seed_missing_title_from_user_message",
    "_session_status_from_cache",
    "_session_status_with_child_rollup",
    "_set_read_state",
    "_signal_harness_elicitation_resolved_by_id",
    "_signal_terminal_resolved_harness_elicitation",
    "_spec_harness",
    "_stop_session_host_runner",
    "_stop_session_via_runner",
    "_stored_file_to_resource",
    "_stream_live_events",
    "_targeted_elicitation_event",
    "_title_content_from_item",
    "_truncate_label",
    "_usage_by_model_for_display",
    "_utc_day",
    "_validate_external_reasoning_effort",
    "_validate_session_workspace",
    "_validate_terminal_launch_args",
    "_validated_cost_control_mode_override",
    "_validated_harness_override",
    "_validated_harness_override_executor_type",
    "_validated_spec_smart_routing_harness",
    "_validated_subagent_routing_override",
    "_wait_for_managed_runner_tunnel",
    "_wait_for_runner_client",
    "announce_hosts_changed",
    "cancel_managed_launch_tasks",
    "prefetch_session_routing_catalogs",
]
