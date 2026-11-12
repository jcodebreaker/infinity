"""Higher-layer orchestration helpers for the sessions routes (call-depth 2+).

Flows that compose the lower-layer ``.helpers`` primitives: runner relay,
session-event dispatch, native-terminal launch, MCP tool calls, fork/switch.
Imports state/constants from ``.common`` and primitives from ``.helpers``;
imported by the router in ``sessions.py``."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any, Final, Literal, NamedTuple, cast

import httpx
from fastapi import (
    HTTPException,
    Request,
)
from fastapi.responses import Response
from pydantic import ValidationError

from omnigent.db.utils import generate_agent_id, generate_task_id
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import debug_event, runner_log_scope
from omnigent.entities import (
    Agent,
    CommentsFingerprint,
    Conversation,
    ConversationItem,
    ErrorData,
    MessageData,
    NewConversationItem,
    ResourceEventData,
    SlashCommandData,
)
from omnigent.entities.conversation import (
    FunctionCallData,
    FunctionCallOutputData,
)
from omnigent.entities.permission import SessionPermission
from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError
from omnigent.evals.identity import is_eval_identity
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE as _HARNESS_NOT_CONFIGURED_ERROR_CODE,
)
from omnigent.host.frames import (
    WORKSPACE_MISSING_ERROR_CODE as _WORKSPACE_MISSING_ERROR_CODE,
)
from omnigent.host.frames import (
    classify_launch_refusal as _classify_launch_refusal,
)
from omnigent.host.frames import (
    workspace_missing_message as _workspace_missing_message,
)
from omnigent.llms.context_window import resolve_effective_context_window
from omnigent.models.model_metadata import concrete_reported_model
from omnigent.policies.types import (
    ElicitationRequest,
    EvaluationContext,
    PolicyResult,
)
from omnigent.runner.mcp_execution_registry import (
    MCP_OPERATION_ID_PARAM,
    RUNNER_MCP_EXECUTION_DETACHED_CODE,
    RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.session_init_protocol import build_runner_session_init_payload
from omnigent.runner.subagent_routing import (
    ROUTING_DECISION_LABEL_KEY,
    auto_harness_session,
    harness_family,
    subagent_routing_enabled,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runtime import (
    get_policy_store,
    inflight_text,
    pending_elicitations,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import (
    build_elicitation_request_event,
    resolve_ask_timeout,
)
from omnigent.runtime.policies.builder import (
    _sum_subtree_usage,
    ancestor_ids_from_tree,
    load_session_tree,
    load_session_usage,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.runtime.workflow import _find_spec_by_name
from omnigent.server import session_live_state, shutdown_state
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_READ,
    local_single_user_enabled,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    background_session_titles_enabled,
    prepare_background_session_title,
)
from omnigent.server.bundles import bundle_location, validate_agent_bundle
from omnigent.server.creation_logging import creation_metadata, creation_stage, session_created
from omnigent.server.host_registry import (
    HostConnection,
    HostRegistry,
    RunnerExitReports,
    multi_replica,
)
from omnigent.server.hosts import (
    MANAGED_REPO_LABEL_KEY,
    HostDeployment,
    HostLaunch,
    ManagedLaunchTracker,
    RepoWorkspace,
    host_resume_supported,
    host_sandbox_is_running,
    parse_repo_workspace,
    read_managed_repo_workspaces,
)
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_session_owner_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._session_create_validation import (
    validate_session_agent,
    validate_session_model_metadata,
)

# Shared constants, state, and small dataclasses live in the _sessions.common
# leaf module; import them here so this module and its re-exporters see the same
# objects. The mutable caches are shared by reference across the package.
# Runtime bindings that tests patch on the historical ``sessions`` facade are
# imported from common as facade-delegating proxies. They stay out of common's
# ``__all__`` and the facade's explicit re-exports, preserving its real runtime
# bindings so a facade-level monkeypatch is honoured in this module too.
from omnigent.server.routes._sessions.common import (  # noqa: F401
    _CLAUDE_NATIVE_UI_LABEL_KEY,
    _CLAUDE_NATIVE_UI_LABEL_VALUE,
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _FENCE_EXEMPT_EVENT_TYPES,
    _LAST_CONTEXT_TOKENS_LABEL_KEY,
    _LAST_CONTEXT_WINDOW_LABEL_KEY,
    _MANAGED_RESUMABLE_TUNNEL_STALE_S,
    _MODEL_TOKEN_KEYS,
    _RUNNER_FORWARD_TIMEOUT,
    _RUNNER_RELAY_READY_TIMEOUT_S,
    _RUNNER_SESSION_INIT_TIMEOUT_S,
    _SUBAGENT_FORWARD_RECONNECT_WAIT_S,
    _TERMINAL_RESPONSE_EVENT_TYPES,
    _TURN_ACTOR_LABEL,
    _deferred_elicitation_clear_tasks,
    _intentional_stop_sessions,
    _interrupt_fenced_sessions,
    _llm_response_denied_turns,
    _logger,
    _managed_launch_tasks,
    _MirroredToolCall,
    _native_mirror_locks,
    _native_popup_forward_tasks,
    _pending_policy_ask_writes,
    _PendingPolicyAskWrites,
    _policy_evaluation_locks,
    _recent_mirrored_tool_calls,
    _RelayHandle,
    _runner_relay_tasks,
    _runner_status_probe_backoff,
    _runner_status_probe_inflight,
    _RunnerStatusProbeBackoff,
    _session_active_response_cache,
    _session_background_task_count_cache,
    _session_background_tasks_cache,
    _session_mcp_startup_cache,
    _session_sandbox_status_cache,
    _session_status_cache,
    _session_terminal_pending_cache,
    get_caps,
    get_server_runner_router,
    session_stream,
    set_server_runner_router,
)

# Lower-layer helpers (SSE builders, publishers, persistence, runner-forward
# primitives) live in _sessions.helpers.
from omnigent.server.routes._sessions.helpers import (
    SessionLiveness,
    _await_settled_managed_launch,
    _build_new_item,
    _build_policy_engine_from_spec,
    _child_session_summary_from_conversation,
    _coerce_cumulative_field,
    _collect_descendant_conversation_ids,
    _consume_pre_resolved_harness_elicitation,
    _create_session_worktree,
    _delete_stored_session_bundle_after_failure,
    _emit_server_routing_decision,
    _error_item_from_sse,
    _extract_persistent_item_from_sse,
    _extract_user_text_for_routing,
    _extract_user_text_from_event,
    _flush_relay_text,
    _forward_approval_to_runner,
    _forward_session_change_to_runner,
    _get_runner_client,
    _handle_advise_models_mcp,
    _invalidate_runner_backed_snapshot_state,
    _is_native_interrupt_record,
    _last_task_error_from_labels,
    _latest_assistant_text_from_store,
    _latest_message_preview,
    _launch_runner_on_host,
    _load_agent_spec_for_session,
    _mcp_error_response,
    _mcp_input_required_response,
    _mcp_ok_response,
    _merge_pending_file_blocks,
    _message_text,
    _model_usage_bucket,
    _owner_from_grants,
    _parse_external_conversation_item,
    _pending_elicitation_snapshot_for_session,
    _permission_level_from_grants,
    _persist_session_status_error_labels,
    _persist_stored_session_bundle,
    _poll_request_disconnect,
    _priced_cost_for_display,
    _provision_managed_sandbox,
    _prune_pre_resolved_harness_elicitations,
    _publish_elicitation_request_to_ancestors,
    _publish_elicitation_resolved,
    _publish_elicitation_resolved_to_ancestors,
    _publish_error_event,
    _publish_external_conversation_item,
    _publish_input_consumed,
    _publish_sandbox_status,
    _publish_status,
    _publish_terminal_pending,
    _query_host_runner_status,
    _read_state_entry,
    _record_daily_cost,
    _reject_reserved_cost_control_label_seed,
    _reject_server_reserved_label_seed,
    _relay_persist,
    _relay_persist_error_once,
    _remove_session_worktree_best_effort,
    _require_declared_subagent,
    _require_external_status_forward,
    _resolve_harness,
    _resolve_llm_model,
    _resolve_subagent_spec,
    _resource_event_item_from_sse,
    _routing_decision_item_from_sse,
    _RunnerForwardResult,
    _seed_missing_title_from_user_message,
    _session_status_from_cache,
    _session_status_with_child_rollup,
    _SessionEventDispatchResult,
    _signal_terminal_resolved_harness_elicitation,
    _spec_harness,
    _stop_session_via_runner,
    _usage_by_model_for_display,
    _validate_session_workspace,
    _validate_terminal_launch_args,
    _validated_cost_control_mode_override,
    _validated_harness_override,
    _validated_harness_override_executor_type,
    _validated_spec_smart_routing_harness,
    _validated_subagent_routing_override,
    _wait_for_managed_runner_tunnel,
    _wait_for_runner_client,
)
from omnigent.server.routes._sessions.turn_metrics import TurnClock
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.schemas import (
    BackgroundTaskInfo,
    ChildSessionSummary,
    CreatedSessionResponse,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    ErrorDetail,
    NativeModelOption,
    SessionCreateInput,
    SessionCreateMetadata,
    SessionEventInput,
    SessionListItem,
    SessionModelEvent,
    SessionResponse,
    SessionStatusEvent,
    SessionSuperchat,
    SessionUsageEvent,
)
from omnigent.spec.types import (
    AgentSpec,
    Phase,
    PolicyAction,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import (
    PINNED_LABEL_KEY,
    RUNNER_LIVENESS_TTL_S,
    ConversationNotFoundError,
    NameAlreadyExistsError,
    pinned_label_key,
    runner_seen_is_fresh,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import Host, HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.superchat.approvals.policy import remember_browser_snapshot
from omnigent.superchat.feature import is_super_chat
from omnigent.superchat.rounds import record as rounds
from omnigent.superchat.rounds import trace as rounds_trace
from omnigent.superchat.transcript.blocks import turn_activity_id
from omnigent.telemetry import emit as _tel_emit
from omnigent.telemetry.anon import anon_user_id as _tel_anon_user_id
from omnigent.telemetry.anon import current_anon_user_id as _tel_current_anon
from omnigent.telemetry.events import NativeSessionUsageEvent as _TelNativeSessionUsageEvent
from omnigent.telemetry.events import SessionCreatedEvent as _TelSessionCreatedEvent
from omnigent.telemetry.events import TurnEndEvent as _TelTurnEndEvent
from omnigent.telemetry.installation_id import get_installation_id as _get_installation_id
from omnigent.telemetry.surface import classify_surface as _classify_surface
from omnigent.util.session_lifecycle import (
    labels_with_closed_status,
    title_without_closed_marker,
)


def _harness_elicitation_request_fingerprint(params: ElicitationRequestParams) -> str:
    """
    Digest one elicitation's request params for tombstone matching.

    Harness elicitation ids can recur for DIFFERENT questions (e.g. the
    codex id derives from a JSON-RPC request id that a later request may
    reuse), so a verdict tombstone written against a possibly-zombie
    waiter carries this digest and a re-park adopts it only for the same
    logical question.

    :param params: The elicitation's request params.
    :returns: A sha256 hex digest of the canonicalized params, e.g.
        ``"9f86d081..."``.
    """
    canonical = json.dumps(
        params.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _publish_and_wait_for_harness_elicitation(
    request: Request,
    *,
    session_id: str,
    params: ElicitationRequestParams,
    timeout_s: float,
    conversation_store: ConversationStore | None = None,
    elicitation_id: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
) -> ElicitationResult | None:
    """
    Publish one harness-originated elicitation and wait for web verdict.

    Mirrors the ``omnigent claude`` permission hook contract: the
    hook parks a server-side Future, publishes the standard
    ``response.elicitation_request`` event, waits until the session
    ``approval`` event resolves the Future, and always publishes
    ``response.elicitation_resolved`` when the upstream wait ends.

    The wait ends on the first of three signals: (1) the web verdict
    Future (session ``approval`` event); (2) the terminal-resolved
    Event, set when a mirrored tool result for this gated tool proves
    the prompt was answered in the native TUI (see
    :func:`_signal_terminal_resolved_harness_elicitation`); or (3)
    upstream disconnect / ``timeout_s``. Only (1) yields a verdict;
    (2) and (3) return ``None`` (fail-ask). (1) and (2) publish
    ``response.elicitation_resolved`` immediately; (3) defers it by
    ``_HARNESS_ELICITATION_REPARK_GRACE_S`` and skips it when the
    caller re-parks the same ``elicitation_id`` (hook retries after a
    severed long-poll reuse their id), so a still-blocked prompt's
    card survives the gap. A caller-supplied id likewise re-attaches
    to a verdict that landed during a gap via the pre-resolved
    tombstone, returned at registration time without re-publishing.

    :param request: FastAPI request object so upstream disconnect can
        be detected.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param params: Elicitation params to publish.
    :param timeout_s: Maximum wait in seconds, e.g. ``300.0``.
    :param conversation_store: Optional store used to mirror
        child-session prompts into ancestor streams. ``None`` keeps
        the prompt scoped to ``session_id`` only.
    :param elicitation_id: Optional precomputed correlation id, e.g.
        ``"elicit_codex_abc123"``. ``None`` mints a random id.
    :param tool_name: Gated tool name, e.g. ``"Bash"``, used to
        correlate a mirrored tool result back to this prompt for the
        terminal-resolved fast path. ``None`` (e.g. Codex) disables
        that correlation; the prompt still resolves via web verdict,
        disconnect, or timeout.
    :param tool_input: Gated tool input, e.g. ``{"command": "ls"}``,
        used with ``tool_name`` to disambiguate the result when several
        same-named prompts are parked at once.
    :returns: Web verdict, or ``None`` on terminal-side resolution,
        timeout, or disconnect.
    """
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    # ``resolved_elsewhere`` is set when a native-side signal proves the
    # prompt was answered outside the web UI: either a mirrored tool
    # result for this gated tool, or Codex app-server's exact
    # ``serverRequest/resolved`` notification. Raced below so the wait
    # ends promptly without relying on the web verdict or on disconnect
    # detection (unreliable behind the Databricks Apps proxy).
    request_fingerprint = _harness_elicitation_request_fingerprint(params)
    parked = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        resolved_elsewhere=asyncio.Event(),
        request_fingerprint=request_fingerprint,
    )
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    _harness_parked_elicitations[elicitation_id] = parked
    # settled = verdict / terminal-resolved (clear the card now); a
    # severed wait instead defers the clear so a hook retry can re-park.
    published_request = False
    settled = False
    # MCP verdict when a real web verdict settles the wait, so the
    # finally's resolved fan-out carries it (None = severed / terminal).
    settled_action: str | None = None
    try:
        tombstone = _consume_pre_resolved_harness_elicitation(
            session_id, elicitation_id, request_fingerprint
        )
        if tombstone is not None:
            # Verdict from the un-parked gap; None = terminal answered (fail-ask).
            return tombstone.result
        event = ElicitationRequestEvent(
            type="response.elicitation_request",
            elicitation_id=elicitation_id,
            params=params,
        )
        event_payload = event.model_dump()
        session_stream.publish(session_id, event_payload)
        published_request = True
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                event_payload,
            )
        disconnect_task = asyncio.create_task(
            _poll_request_disconnect(request),
        )
        resolved_elsewhere_task = asyncio.create_task(parked.resolved_elsewhere.wait())
        race_tasks = (disconnect_task, resolved_elsewhere_task)
        try:
            waiters: set[asyncio.Future[Any]] = {future, *race_tasks}
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for race_task in race_tasks:
                if not race_task.done():
                    race_task.cancel()
                    # Bounded: a cancellation swallowed inside the race
                    # task (e.g. coalesced into an anyio cancel-scope
                    # unwind) must not convert this cleanup into
                    # an unbounded wait — that wedged the whole request
                    # for the gate's timeout. ``asyncio.wait`` absorbs
                    # the CancelledError outcome; an unreaped task is
                    # logged and abandoned to die with the request.
                    # Resolve through the facade so a test's
                    # monkeypatch of this constant is honored here.
                    from omnigent.server.routes import sessions as _facade

                    _reaped, still_pending = await asyncio.wait(
                        {race_task},
                        timeout=_facade._RACE_TASK_REAP_TIMEOUT_S,
                    )
                    if still_pending:
                        _logger.warning(
                            "Race task %r for elicitation %s survived its "
                            "cancellation (swallowed cancel); abandoning it.",
                            race_task.get_coro(),
                            elicitation_id,
                        )
        # Only an actual web verdict yields a result; a terminal-side
        # resolution, disconnect, or timeout returns None (fail-ask).
        # Checking ``future in done`` (not ``future.done()``) avoids
        # honoring a verdict that lands in the same tick as a disconnect.
        if future in done and future.exception() is None:
            settled = True
            verdict = future.result()
            settled_action = getattr(verdict, "action", None)
            return verdict
        settled = parked.resolved_elsewhere.is_set()
        return None
    finally:
        # Pop only our own entries — a hook retry may have re-parked
        # this id with a new future while this wait was unwinding.
        if _harness_elicitation_registry.get(elicitation_id) is future:
            _harness_elicitation_registry.pop(elicitation_id, None)
            _harness_elicitation_owners.pop(elicitation_id, None)
        if _harness_parked_elicitations.get(elicitation_id) is parked:
            _harness_parked_elicitations.pop(elicitation_id, None)
        if published_request and not settled:
            # Severed without an answer — defer the clear (scheduled
            # before any await so handler cancellation can't skip it).
            _schedule_deferred_elicitation_clear(
                session_id,
                elicitation_id,
                conversation_store,
            )
        elif published_request:
            _publish_elicitation_resolved(session_id, elicitation_id, action=settled_action)
            if conversation_store is not None:
                await asyncio.to_thread(
                    _publish_elicitation_resolved_to_ancestors,
                    conversation_store,
                    session_id,
                    elicitation_id,
                    settled_action,
                )


def _schedule_deferred_elicitation_clear(
    session_id: str,
    elicitation_id: str,
    conversation_store: ConversationStore | None,
) -> None:
    """
    Clear one elicitation's approval card after the re-park grace, unless
    a hook retry re-parks the id first.

    A wait severed without an answer (proxy cut, timeout) may still be
    blocked in the native terminal; clearing immediately wiped the only
    surface a headless sub-agent's user can answer from. A hook that
    died for real never re-parks, so the clear still fires after the
    grace and badges don't stick. That clear carries
    ``reason="unanswered"``: nobody decided anything, so the card can say
    the prompt expired instead of implying someone resolved it elsewhere.

    :param session_id: Session that owns the elicitation, e.g.
        ``"conv_abc123"``.
    :param elicitation_id: Correlation id whose card may need clearing,
        e.g. ``"elicit_claude_0f3a..."``.
    :param conversation_store: Store used to mirror the clear into
        ancestor streams, or ``None`` to keep it session-local.
    """

    async def _clear_after_grace() -> None:
        """
        Sleep out the grace, then publish the clear unless re-parked.

        :returns: None.
        """
        # Resolve through the facade so a test's monkeypatch of this
        # constant is honored here.
        from omnigent.server.routes import sessions as _facade

        await asyncio.sleep(_facade._HARNESS_ELICITATION_REPARK_GRACE_S)
        if elicitation_id in _harness_elicitation_registry:
            # Re-parked — the new wait owns the eventual clear.
            return
        _publish_elicitation_resolved(session_id, elicitation_id, reason="unanswered")
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
                reason="unanswered",
            )

    task = asyncio.create_task(_clear_after_grace())
    _deferred_elicitation_clear_tasks.add(task)
    task.add_done_callback(_deferred_elicitation_clear_tasks.discard)


async def _best_effort_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
) -> None:
    """Stop a running session before a destructive lifecycle action.

    Mirrors the client-side stop-then-archive/delete pattern. A session
    reads as "running" here if it is itself running, has live background
    tasks, or has any sub-agent descendant (child, grandchild, and so on)
    still running or waiting, matching the unbounded depth that
    ``delete_conversation``'s recursive subtree delete already covers.
    Each running descendant must be stopped on its own session id: it
    executes on its own runner, separate from its ancestors', so stopping
    a parent never reaches it. Every stop attempt is independently
    best-effort, so one runner being unreachable does not skip stopping
    the others, and none of this may block the caller from archiving or
    deleting the session.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant-id lookup.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    """
    try:
        descendant_ids = await _collect_descendant_conversation_ids(conversation_store, session_id)
        status = _session_status_with_child_rollup(session_id, descendant_ids)
        own_status = _session_status_from_cache(session_id)
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Best-effort stop failed for %s; proceeding anyway",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return

    # Background shells outlive their turn without making the session read as
    # running (the sidebar must not spin for an ended turn), but they are live
    # work on the runner — a destructive action still has to stop them.
    has_background_tasks = (
        own_status != "failed" and _session_background_task_count_cache.get(session_id, 0) > 0
    )
    if status != "running" and not has_background_tasks:
        return

    async def _stop(target_id: str) -> None:
        try:
            await _stop_session_via_runner(target_id, runner_router)
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Best-effort stop failed for %s; proceeding anyway",
                target_id,
                exc_info=True,
                extra={"session_id": target_id},
            )

    if own_status == "running" or has_background_tasks:
        await _stop(session_id)
    for descendant_id in descendant_ids:
        if _session_status_cache.get(descendant_id) in ("running", "waiting"):
            await _stop(descendant_id)


# Strong references to detached archive stops so the tasks can't be
# garbage-collected mid-stop (asyncio only holds weak refs to tasks).
# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_detached_stop_tasks: set[asyncio.Task[None]] = set()

# Deferred archive stops still inside the undo grace, keyed by session id.
# Scheduling a stop cancels the session's prior pending one, and an unarchive
# cancels it outright — so a stale timer from an earlier archive can't fire
# after an Undo + re-archive on the same replica. This is the same-replica
# fast path; the persisted ``archived`` re-check in _archive_stop is the
# cross-replica backstop. One entry per session.
# custom-lint: disable-next=workspace-scoped-cache -- undo-window teardown timers
_pending_archive_stops: dict[str, asyncio.Task[None]] = {}

# When the deferred archive stop re-reads the row and that read fails, retry a
# few times before giving up: a transient blip must not force a blind stop-or-
# skip guess (either can be wrong). Small/short — this only rides out a brief
# failure, not a sustained outage.
_ARCHIVE_STOP_LOOKUP_ATTEMPTS = 3
_ARCHIVE_STOP_LOOKUP_RETRY_S = 0.2


async def _archive_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
    host_registry: Any,
) -> None:
    """
    Stop an archived session and tear down its host-launched runner.

    Archive carries the whole teardown: it is the one lifecycle action
    with no client-side stop, so both halves run here. Killing the pane
    alone leaves a host-spawned session's dedicated runner connected,
    which keeps ``runner_online`` true and hangs a later message on
    "working" against a dead pane — the failure
    :func:`_stop_session_host_runner` exists to prevent.

    Every step is best-effort: a wedged, offline, or already-stopped
    runner must not leave the session un-archived.

    The persisted ``archived`` flag is the undo guard: archiving pops an
    Undo pill, and teardown is deferred past that window (see
    :func:`_spawn_archive_stop`). When it fires it re-reads the row and
    skips when the session is no longer archived, so undoing keeps the
    runner alive. Reading the persisted flag — not a per-replica in-memory
    marker — makes this correct even when the Undo lands on a different
    replica than the one holding the timer.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant and row lookups.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    :param host_registry: The ``HostRegistry`` tracking live host
        tunnels, or ``None`` when host support is not wired.
    """
    # Resolve through the facade so a test's monkeypatch is honored here.
    from omnigent.server.routes import sessions as _facade

    # Re-read the row to honor a late Undo before tearing down. Neither guess on
    # a failed read is safe — stopping could kill a session just unarchived on
    # another replica (dead pane), skipping could leak a runner that should be
    # down. So retry a transient blip a few times; only give up (and skip, the
    # conservative choice that never kills a live session) on a sustained
    # outage, which a later lifecycle event then reaps.
    conv: Any = None
    for _attempt in range(_ARCHIVE_STOP_LOOKUP_ATTEMPTS):
        try:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            break
        except Exception:  # noqa: BLE001
            if _attempt + 1 >= _ARCHIVE_STOP_LOOKUP_ATTEMPTS:
                _logger.warning(
                    "Archive teardown lookup failed for %s after %d attempts; "
                    "leaving the runner (reaped by a later lifecycle event)",
                    session_id,
                    _ARCHIVE_STOP_LOOKUP_ATTEMPTS,
                    exc_info=True,
                    extra={"session_id": session_id},
                )
                return
            await asyncio.sleep(_ARCHIVE_STOP_LOOKUP_RETRY_S)
    # Confirmed read: skip when the session is no longer archived (an Undo,
    # possibly on another replica) — it's live again, so leave its runner alone.
    if conv is None or not conv.archived:
        return
    # Past the point of no return: unregister so a late cancel (an Undo racing
    # this teardown) can't interrupt it mid-stop and strand the intentional-
    # stop marker below, which would exclude the session from child recovery.
    _pending_archive_stops.pop(session_id, None)

    await _facade._best_effort_stop(session_id, conversation_store, runner_router)
    if not conv.host_id or not conv.runner_id:
        return
    # Mark the tunnel drop intentional BEFORE tearing it down so the relay
    # renders a quiet stopped state rather than "runner_disconnected".
    _intentional_stop_sessions.add(session_id)
    try:
        delivered = await _facade._stop_session_host_runner(
            session_id,
            conv.host_id,
            conv.runner_id,
            host_registry,
        )
    except Exception:  # noqa: BLE001
        _logger.debug(
            "Archive host-runner teardown failed for %s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        delivered = False
    if not delivered:
        # No tunnel drop will follow, so the marker would outlive this stop
        # and later swallow a genuine runner_disconnected as a quiet idle.
        _intentional_stop_sessions.discard(session_id)


def _spawn_archive_stop(
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: Any,
    host_registry: Any = None,
) -> None:
    """
    Defer :func:`_archive_stop` past the undo grace, as a retained task.

    Archiving pops an Undo pill; undoing it unarchives the session. A stop
    that tore the runner down immediately would leave an unarchived
    session with a dead pane, so the teardown sleeps past the grace
    (``_ARCHIVE_STOP_UNDO_GRACE_S``, wider than the pill) and then re-reads
    the persisted ``archived`` flag, skipping if the session was undone.
    That store re-check is the guard that matters, and it holds across
    replicas.

    Awaiting the stop inline would hold the PATCH for its per-runner
    timeouts (seconds against a wedged runner) even though the archive
    proceeds regardless of the stop's outcome, so it is detached.

    Scheduling here also cancels the session's prior pending stop, so a
    re-archive replaces it rather than leaving two timers; that plus the
    unarchive cancel in :func:`_cancel_pending_archive_stop` covers the
    common same-replica undo without waiting out the grace.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store for descendant and row lookups.
    :param runner_router: The ``RunnerRouter`` for runner-client
        resolution, or ``None`` in tests / in-process setups.
    :param host_registry: The ``HostRegistry`` tracking live host
        tunnels, or ``None`` when host support is not wired.
    """
    # Replace any pending stop from an earlier archive of this session.
    _cancel_pending_archive_stop(session_id)

    async def _stop_after_grace() -> None:
        # Resolve through the facade so a test's monkeypatch of the grace
        # constant is honored here.
        from omnigent.server.routes import sessions as _facade

        try:
            await asyncio.sleep(_facade._ARCHIVE_STOP_UNDO_GRACE_S)
        except asyncio.CancelledError:
            return
        await _archive_stop(session_id, conversation_store, runner_router, host_registry)

    task = asyncio.create_task(_stop_after_grace())
    _pending_archive_stops[session_id] = task
    _detached_stop_tasks.add(task)

    def _done(t: asyncio.Task[None]) -> None:
        _detached_stop_tasks.discard(t)
        # Clear the map entry only if it still points at this task — a
        # re-archive may have already replaced it.
        if _pending_archive_stops.get(session_id) is t:
            del _pending_archive_stops[session_id]

    task.add_done_callback(_done)


def _cancel_pending_archive_stop(session_id: str) -> None:
    """
    Cancel a session's pending deferred archive stop if it hasn't fired.

    Called when a session is unarchived (Undo, or an explicit unarchive)
    and when a re-archive supersedes an earlier one, so the common
    same-replica undo keeps the runner alive without waiting out the
    grace. A no-op when none is pending or it already ran. Cancelling only
    reaches the grace sleep: once :func:`_archive_stop` begins the teardown
    it unregisters itself, so this can't interrupt a stop in flight. The
    persisted-flag re-check in :func:`_archive_stop` covers the case a
    cross-replica Undo can't reach this in-memory timer.

    :param session_id: Session/conversation identifier.
    """
    task = _pending_archive_stops.pop(session_id, None)
    if task is not None:
        task.cancel()


def _labels_for_viewer(labels: dict[str, str], user_id: str | None) -> dict[str, str]:
    """
    Collapse the dynamic-suffix label families to their canonical bare keys.

    Two families are stored under indexed/per-user keys but presented on the
    wire as one bare key, so the client reads the same key it writes:

    - Pins are stored per-user as ``omnigent.pinned.<user>`` (see
      :func:`pinned_label_key`); we present a single ``omnigent.pinned`` set iff
      THIS viewer pinned the session, and drop every ``omnigent.pinned.*`` key.
      That keeps the per-user dimension server-side and stops other users' pins
      from bloating the payload.
    - Sandbox repos are stored one per repo as ``omnigent.sandbox.repo.<index>``
      (avoiding the 256-char label-value cap); we present the canonical
      ``omnigent.sandbox.repo`` as the space-joined list so clients that read
      the bare key (e.g. the fork dialog) see every repo.

    :param labels: The stored conversation labels.
    :param user_id: The requesting viewer, or ``None`` in single-user mode.
    :returns: A copy with the pin and sandbox-repo families collapsed to their
        canonical bare keys.
    """
    my_key = pinned_label_key(user_id)
    my_pin = labels.get(my_key)
    repos = read_managed_repo_workspaces(labels)
    cleaned = {
        k: v
        for k, v in labels.items()
        if k != PINNED_LABEL_KEY
        and not k.startswith(f"{PINNED_LABEL_KEY}.")
        and k != MANAGED_REPO_LABEL_KEY
        and not k.startswith(f"{MANAGED_REPO_LABEL_KEY}.")
    }
    if my_pin is not None:
        cleaned[PINNED_LABEL_KEY] = my_pin
    if repos:
        cleaned[MANAGED_REPO_LABEL_KEY] = " ".join(repos)
    return cleaned


def _build_session_list_item(
    conv: Conversation,
    *,
    agent_names_by_id: Mapping[str, str | None],
    grants: list[SessionPermission],
    user_id: str | None,
    user_is_admin: bool,
    permissions_enabled: bool,
    pending_count: int,
    child_session_ids: list[str],
    comments_fingerprint: CommentsFingerprint | None,
) -> SessionListItem:
    """
    Assemble one :class:`SessionListItem` from a conversation row and
    pre-fetched batch data.

    Single source of truth for the list-item shape, shared by the
    ``GET /v1/sessions`` page builder and the ``WS /v1/sessions/updates``
    push stream so the two never drift. The caller is responsible for
    batching the permission grants, agent names, and pending-elicitation
    counts across the whole set and passing the per-conversation slice
    here.

    :param conv: The persisted conversation entity. Must have a
        non-``None`` ``agent_id`` (i.e. be a session, not a plain
        conversation) — the caller filters these out beforehand.
    :param agent_names_by_id: Map from agent id to display name, as
        returned by ``agent_store.get_names()``,
        e.g. ``{"ag_abc": "research-agent"}``.
    :param grants: All permission grants for this conversation, as
        returned by ``permission_store.list_for_sessions()[conv.id]``.
        Empty list when permissions are disabled.
    :param user_id: The authenticated requesting user, or ``None`` when
        unauthenticated / permissions disabled,
        e.g. ``"alice@example.com"``.
    :param user_is_admin: Whether ``user_id`` holds the admin flag, from
        a single ``permission_store.is_admin()`` call made once for the
        whole batch.
    :param permissions_enabled: ``True`` when a permission store is
        wired; gates owner/level population to mirror ``list_sessions``.
    :param pending_count: Number of outstanding elicitations for this
        conversation, from ``pending_elicitations.counts_for()``.
    :param child_session_ids: Direct sub-agent children for this
        conversation, as returned by
        ``conversation_store.list_child_conversation_ids_by_parent()``.
    :param comments_fingerprint: Change-detection summary of this
        conversation's review comments, from
        ``comment_store.get_comments_fingerprints()[conv.id]``. ``None``
        when the conversation has no comments or no comment store is
        wired — emitted as ``comments_count=0`` /
        ``comments_updated_at=None`` so the two states look identical
        on the wire.
    :returns: The assembled :class:`SessionListItem`.
    """
    # ``conv.agent_id`` is guaranteed non-None by the caller (sessions
    # only); assert for the type checker without a runtime branch.
    assert conv.agent_id is not None
    level = _permission_level_from_grants(user_id, grants, user_is_admin)
    owner = _owner_from_grants(grants) if permissions_enabled else None
    # Per-viewer read tracking, embedded so the client hydrates the unread
    # dots straight from the list (no separate fetch). Built per-user here —
    # `user_id` is the requesting caller, never broadcast to other viewers.
    viewer_last_seen, viewer_unread = _read_state_entry(user_id, conv.id)
    return SessionListItem(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_names_by_id.get(conv.agent_id),
        status=_list_status_with_starting(
            _session_status_with_child_rollup(conv.id, child_session_ids, conv.live_status),
            conv.id,
        ),
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        # Collapse per-user pin keys to the canonical bare key for this viewer
        # (never leak another user's pin key), then add the closed marker.
        labels=labels_with_closed_status(_labels_for_viewer(conv.labels, user_id), conv.title),
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        reasoning_effort=conv.reasoning_effort,
        permission_level=level,
        owner=owner,
        external_session_id=conv.external_session_id,
        # The persisted row count is a CROSS-REPLICA mirror: the replica
        # holding the runner's tunnel writes it, and a replica that doesn't
        # hold it falls back to the row (max() prefers "shows the parked
        # approval" whichever side lags). That fallback only makes sense for
        # a runner-bound session — an unbound session (no runner_id) has no
        # tunnel on any replica, so the local in-memory index is
        # authoritative and the row (an async mirror that lags a resolve's
        # decrement) must not override it. Gating on runner_id keeps the
        # cross-replica fallback where it's needed while making the unbound
        # path index-only and free of the persist-lag race.
        pending_elicitations_count=(
            max(pending_count, conv.pending_elicitation_count or 0)
            if conv.runner_id is not None
            else pending_count
        ),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        comments_count=comments_fingerprint.count if comments_fingerprint else 0,
        comments_updated_at=(
            comments_fingerprint.last_updated_at if comments_fingerprint else None
        ),
        viewer_last_seen=viewer_last_seen,
        viewer_unread=viewer_unread,
        # Transient; set by the store only on a content search. The WS
        # push-stream path leaves it None (no query in flight there).
        search_snippet=conv.search_snippet,
        parent_session_id=conv.parent_conversation_id,
        project_id=conv.project_id,
    )


def _publish_subtree_cost_to_ancestors(
    conv_store: ConversationStore,
    session_id: str,
    conv: Conversation | None = None,
) -> None:
    """
    Re-publish each ancestor's subtree-summed cost after a child usage update.

    A sub-agent's spend is persisted on its own child conversation, so an
    ancestor's stored ``session_usage`` doesn't move when the child spends —
    yet the ancestor's displayed "Session cost" reads its own number, so a
    parent's badge would never reflect a running sub-agent. (The policy gate
    already reads the subtree sum via :func:`load_session_usage`; this is the
    display side.) For each ancestor of *session_id*, recompute its subtree
    priced cost and publish a ``session.usage`` event carrying it.

    One tree load serves the whole walk. Every ancestor of this session is in
    the same tree, so both the chain and each ancestor's sum are derived from
    one set of freshly-read rows — previously each ancestor paged the tree
    again, and the chain came from a conversation row that may have been read
    before a concurrent delete/recreate moved this session elsewhere.

    Sync (does store reads + SSE fan-out); call via
    :func:`asyncio.to_thread`, mirroring the elicitation ancestor-publish
    helpers. ``session_stream.publish`` is safe to call from a worker thread.

    :param conv_store: Store used to load the tree.
    :param session_id: The child session whose usage just changed, e.g.
        ``"conv_child123"``.
    :param conv: The child's already-loaded conversation row, when the caller
        holds one. Only its ``root_conversation_id`` is used, and only as a
        hint: :func:`load_session_tree` verifies the tree it names actually
        contains this session and resolves the root itself when it does not.
        The parameter belongs to this function, not to whichever caller first
        needed it, so that no caller can be removed and leave a signature
        behind that its remaining callers already depend on.
    :returns: None.
    """
    tree = load_session_tree(
        session_id,
        conv_store,
        conv.root_conversation_id if conv is not None else None,
    )
    for ancestor_id in ancestor_ids_from_tree(tree, session_id):
        ancestor_usage = _sum_subtree_usage(tree, ancestor_id)
        subtree_cost = _priced_cost_for_display(ancestor_usage)
        usage_by_model = _usage_by_model_for_display(ancestor_usage)
        if subtree_cost is None and usage_by_model is None:
            # Ancestor's subtree has no priced cost or token usage yet —
            # leave its badge showing "—"/its snapshot value rather than
            # emit $0.00.
            continue
        payload: dict[str, Any] = {
            "type": "session.usage",
            "conversation_id": ancestor_id,
        }
        if subtree_cost is not None:
            payload["total_cost_usd"] = subtree_cost
        if usage_by_model is not None:
            payload["usage_by_model"] = usage_by_model
        event = SessionUsageEvent(**payload)
        session_stream.publish(ancestor_id, event.model_dump(exclude_none=True))


def _build_session_response(
    conv: Conversation,
    items: list[ConversationItem],
    status: Literal["idle", "running", "waiting", "failed"],
    permission_level: int | None = None,
    background_task_count: int | None = None,
    background_tasks: list[BackgroundTaskInfo] | None = None,
    llm_model: str | None = None,
    context_window: int | None = None,
    last_total_tokens: int | None = None,
    last_task_error: dict[str, str] | None = None,
    agent_name: str | None = None,
    runner_online: bool | None = None,
    host_online: bool | None = None,
    host_resumable: bool = False,
    pending_elicitation_events: list[dict[str, Any]] | None = None,
    subtree_usage: dict[str, Any] | None = None,
    model_options: list[dict[str, Any]] | None = None,
    viewer_id: str | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> SessionResponse:
    """
    Build a :class:`SessionResponse` from store-side entities.

    ``status`` is derived from the conversation's tasks by the
    caller via :func:`_derive_session_lifecycle` — the conversation
    row itself owns no lifecycle column.

    :param conv: The persisted conversation entity.
    :param items: Committed conversation items in chronological
        order, each a :class:`ConversationItem`.
    :param status: Derived session lifecycle status,
        e.g. ``"running"``.
    :param background_task_count: Background shells still running as of the
        last status edge (claude-native), so a reload re-shows "N shells
        still running" even after the session settles to ``"idle"``. ``None``
        when none are tracked.
    :param background_tasks: Per-shell detail for that tally, so a reload can
        restore it. ``None`` when none are tracked.
    :param permission_level: The requesting user's numeric level
        on this session (1=read, 2=edit, 3=manage), or ``None``
        when permissions are disabled.
    :param runner_online: Session-scoped liveness for the bound
        runner/host, e.g. ``False`` for a dead tunneled runner.
        ``None`` when no lookup is wired.
    :param llm_model: The LLM model identifier from the bound
        agent's spec, e.g. ``"anthropic/claude-sonnet-4-6"``.
        ``None`` when not available.
    :param context_window: Context window size in tokens looked up
        from litellm server-side, e.g. ``200_000``. ``None`` when
        the model is not in litellm's registry.
    :param last_total_tokens: Total token count (input + output) from
        the most recently completed task's usage, e.g. ``45231``.
        ``None`` when no task has completed yet. Lets clients seed
        their context-ring on conversation resume without waiting for
        the next ``response.completed`` SSE event.
    :param last_task_error: Error dict from the most recently failed
        task, e.g. ``{"code": "executor_error", "message": "..."}``.
        ``None`` when ``status`` is not ``"failed"`` or the task has
        no stored error.
    :param agent_name: Human-readable agent name, e.g.
        ``"research-agent"``. ``None`` when the agent row is not
        available at snapshot-build time.
    :param runner_online: Strict runner reachability — ``True`` iff a
        runner tunnel is currently registered for this session (see
        :class:`SessionLiveness`). ``None`` when the caller has no
        liveness lookup wired (e.g. focused tests), in which case the
        field is omitted from the API projection.
    :param host_online: Whether the session's host tunnel is live, or
        ``None`` when the session has no ``host_id`` or no lookup is
        wired (see :class:`SessionLiveness`). Used only to decide what
        the open view shows when ``runner_online`` is ``False``.
    :param pending_elicitation_events: Optional precomputed
        outstanding elicitation events. ``None`` reads only the
        current session's entries from the pending-elicitations index.
    :param subtree_usage: Precomputed subtree usage dict (this session
        plus its sub-agent descendants, from
        :func:`load_session_usage`), used to display a cost that
        includes sub-agents, e.g. ``{"total_cost_usd": 11.19}``.
        ``None`` falls back to this conversation's own ``session_usage``
        (correct for childless sessions). Passed by the snapshot path;
        other callers omit it.
    :param model_options: Runner-owned native model picker options,
        e.g. ``[{"id": "gpt-5.5", "displayName": "GPT-5.5"}]``.
        ``None`` is treated as ``[]``.
    :param agent_store: Optional store used to resolve the session harness.
    :param agent_cache: Optional cache used to load the session harness spec.
    :returns: The :class:`SessionResponse` for the API.
    :raises OmnigentError: If ``conv.agent_id`` is ``None``.
    """
    if conv.agent_id is None:
        raise OmnigentError(
            "Session has no agent binding",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Usage to display for this node: the SUBTREE total (this session + its
    # sub-agents) when the caller computed it, else this conversation's own
    # usage. Shared by the cost indicator and the per-model breakdown so
    # both read the same numbers.
    display_usage = subtree_usage if subtree_usage is not None else (conv.session_usage or {})
    harness = _resolve_harness(conv, agent_store=agent_store, agent_cache=agent_cache)
    # Collapse per-user pin keys to the canonical bare key for this viewer, so
    # the snapshot never carries another user's pin key (see _labels_for_viewer).
    labels = labels_with_closed_status(_labels_for_viewer(conv.labels, viewer_id), conv.title)
    return SessionResponse(
        id=conv.id,
        agent_id=conv.agent_id,
        agent_name=agent_name,
        status=status,
        background_task_count=background_task_count,
        background_tasks=background_tasks,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        title=title_without_closed_marker(conv.title),
        labels=labels,
        runner_id=conv.runner_id,
        host_id=conv.host_id,
        runner_online=runner_online,
        host_online=host_online,
        host_resumable=host_resumable,
        reasoning_effort=conv.reasoning_effort,
        items=items,
        permission_level=permission_level,
        sub_agent_name=conv.sub_agent_name,
        kind=conv.kind,
        parent_session_id=conv.parent_conversation_id,
        root_conversation_id=conv.root_conversation_id,
        llm_model=llm_model,
        harness=harness,
        model_override=conv.model_override,
        cost_control_mode_override=conv.cost_control_mode_override,
        subagent_routing_override=conv.subagent_routing_override,
        share_workspace_files=conv.share_workspace_files,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        # Seed the client's cost indicator on resume. Uses the SUBTREE
        # total (this session + its sub-agents) when the caller computed
        # it, so a parent's badge reflects its sub-agents' spend; falls
        # back to this conversation's own usage otherwise. A priced
        # cumulative total, or None (rendered "—") when never priced.
        total_cost_usd=_priced_cost_for_display(display_usage),
        # Per-model breakdown over the same subtree usage. None (omitted)
        # when no per-model usage was recorded.
        usage_by_model=_usage_by_model_for_display(display_usage),
        last_task_error=last_task_error,
        external_session_id=conv.external_session_id,
        terminal_launch_args=conv.terminal_launch_args,
        # Replay outstanding approval prompts into the snapshot.
        # The live SSE stream has no buffer, so a prompt emitted
        # before the user opened this chat would otherwise never
        # render — the UI rebuilds blocks from the snapshot on
        # cold load, then live-tails. Empty list when nothing is
        # outstanding (the common case).
        pending_elicitations=(
            pending_elicitation_events
            if pending_elicitation_events is not None
            else pending_elicitations.snapshot_for(conv.id)
        ),
        # Replay un-consumed web messages on native-terminal sessions
        # so a client that posted then navigated away / rebound re-
        # hydrates the optimistic bubble. Empty for non-native sessions
        # (their message is already persisted into ``items``).
        pending_inputs=pending_inputs.snapshot_for(conv.id),
        workspace=conv.workspace,
        git_branch=conv.git_branch,
        archived=conv.archived,
        model_options=[
            NativeModelOption.model_validate(option) for option in (model_options or [])
        ],
        # Replay terminal spin-up state so a client connecting while the
        # runner is still creating a terminal-first session's terminal
        # sees the Terminal-pill spinner. Populated by the runner SSE
        # relay; absent (False) for non-terminal-first sessions or once
        # the terminal lands / auto-create fails.
        terminal_pending=_session_terminal_pending_cache.get(conv.id, False),
        # Replay managed-sandbox launch progress so a client opening the
        # session mid-launch (the Web UI navigates here immediately
        # after the non-blocking managed create) sees the provisioning
        # indicator. None for sessions without a managed launch and
        # once the launch succeeds; a failed launch is retained with
        # its reason. Populated by _publish_sandbox_status.
        sandbox_status=_session_sandbox_status_cache.get(conv.id),
        # Replay harness MCP-server startup state (codex-native) so a
        # client opening the session mid-startup sees the startup band.
        mcp_startup=_session_mcp_startup_cache.get(conv.id),
        # In-flight turn id so a mid-turn reconnect can reopen a streaming
        # ``activeResponse`` (the turn-start ``running`` edge that carried it
        # is not replayed on the SSE stream). Populated for native-terminal
        # sessions whose forwarder stamps a turn id; ``None`` otherwise.
        active_response_id=_session_active_response_cache.get(conv.id),
        project_id=conv.project_id,
    )


def _sane_relay_token_count(value: object) -> int:
    """
    Coerce a runner-reported token count to a trusted non-negative int.

    Relay usage frames are POSTed under the session owner's own bearer
    token, so a forged negative or non-finite count must never reach the
    additive :meth:`ConversationStore.increment_session_usage` delta: a
    negative would claw back the session's cumulative ``total_cost_usd``
    (the figure the relay cost-budget gate reads, since relay sessions
    never post ``policy_cost_usd``) and a ``NaN`` / ``inf`` would poison
    every downstream sum. Anything that is not a finite, non-negative,
    non-bool number collapses to ``0``; the turn is then priced on
    whatever counts survive.

    :param value: Raw ``usage`` field from the harness response, e.g.
        ``1200``, ``-1``, or ``float("nan")``.
    :returns: The value as a non-negative ``int``, or ``0`` when it is
        missing / the wrong type / negative / non-finite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0:
        return 0
    return int(value)


def _served_by_model_proxy(
    conv: Conversation | None, conversation_store: ConversationStore
) -> bool:
    """Whether *conv* (its root, for a sub-agent without a snapshot) uses the model proxy."""
    from omnigent.server.inference_catalog import snapshot_uses_model_proxy

    if conv is None:
        return False
    snapshot = conv.inference_snapshot
    if snapshot is None and conv.root_conversation_id != conv.id:
        root = conversation_store.get_conversation(conv.root_conversation_id)
        snapshot = root.inference_snapshot if root is not None else None
    return snapshot_uses_model_proxy(snapshot)


def _accumulate_session_usage(
    resp_obj: dict[str, Any],
    session_id: str,
    conversation_store: ConversationStore,
) -> float | None:
    """
    Increment the session's cumulative token counters from a
    ``response.completed`` event's usage data.

    Called synchronously from the relay loop. Builds a usage delta from
    the response's ``usage`` field and atomically applies it to the
    persisted ``session_usage`` via a single database transaction
    (``SELECT FOR UPDATE`` on PostgreSQL, SQLite's single-writer lock
    otherwise). This prevents the read-modify-write race that caused
    concurrent relay completions to silently drop each other's cost /
    token deltas (#9). No-op when the response carries no usage data.

    Cost is computed when the model's per-token pricing is
    available from the MLflow catalog (looked up once per call
    from the response's ``model`` field). When the harness instead
    reports an authoritative per-turn ``cost_usd``, that value is used directly in preference to
    the catalog estimate. The ``total_cost_usd`` key is written
    **only when the turn is priced** (catalog pricing available or a
    harness-reported cost) — an unpriced session leaves it absent
    (its presence is what distinguishes a priced ``$0.00`` from
    "unpriced"; see :func:`_priced_cost_for_display`).

    :param resp_obj: The ``response`` dict from the
        ``response.completed`` SSE event.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store for reading and writing
        the ``session_usage`` column.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or carries no
        usage to accumulate.
    """
    usage_obj = resp_obj.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    # Sanitize every runner-reported count: a forged negative / non-finite
    # value here would otherwise flow into both the additive token counters
    # and (via the catalog-pricing branch below) the additive cost delta.
    input_tokens = _sane_relay_token_count(usage_obj.get("input_tokens", 0))
    output_tokens = _sane_relay_token_count(usage_obj.get("output_tokens", 0))
    total_tokens = _sane_relay_token_count(usage_obj.get("total_tokens", 0))
    if not any((input_tokens, output_tokens, total_tokens)):
        return None

    cache_read_input_tokens = _sane_relay_token_count(usage_obj.get("cache_read_input_tokens", 0))
    cache_creation_input_tokens = _sane_relay_token_count(
        usage_obj.get("cache_creation_input_tokens", 0)
    )

    # Load conversation metadata for pricing only (NOT for reading session_usage —
    # the atomic increment_session_usage call below handles that separately to
    # avoid the read-modify-write race).
    conv = conversation_store.get_conversation(session_id)

    # Compute cost delta if pricing is available for the model. Resolve
    # the model to price with, most-specific first:
    #   1. ``usage.model`` — the model the harness actually used this turn.
    #      Relay executors report it; it's the only signal when the spec
    #      pins no ``llm.model`` (a supervisor that delegates / uses the
    #      harness default), so it's what makes those sessions priceable.
    #   2. the session's ``model_override`` (a ``/model`` switch).
    #   3. the agent spec's ``llm.model`` (the static default).
    # The response's top-level ``model`` is the AGENT NAME, not the LLM
    # model, so it is never used here. The ``total_cost_usd`` key is
    # created only on this priced branch, so an unpriced session never
    # gains a (misleading $0.00) cost key.
    cost_delta = 0.0
    priced = False
    # Prefer an authoritative harness-reported cost over the catalog estimate.
    provider_cost = usage_obj.get("cost_usd")
    usage_model = usage_obj.get("model")
    llm_model = (
        usage_model
        if isinstance(usage_model, str) and usage_model
        else (
            conv.reported_model
            if conv and conv.reported_model
            else (
                conv.model_override if conv and conv.model_override else _resolve_llm_model(conv)
            )
        )
    )
    if llm_model:
        if (
            isinstance(provider_cost, (int, float))
            and math.isfinite(provider_cost)
            and provider_cost >= 0
        ):
            cost_delta = float(provider_cost)
            priced = True
        else:
            from omnigent.llms.context_window import (
                compute_llm_cost,
                fetch_model_pricing_with_provider,
            )
            from omnigent.onboarding.provider_config import load_config

            # Load provider config to check for custom pricing
            provider_config = load_config()
            # Resolve harness from conversation (agent spec + overrides)
            harness = _resolve_harness(conv) if conv else None
            pricing = fetch_model_pricing_with_provider(
                llm_model, provider_config=provider_config, harness=harness
            )
            priced = pricing is not None
            if pricing is not None:
                # Cache-aware: price the sanitized cache-read / cache-creation
                # token counts at their own (cheaper read / pricier write)
                # rates. Pass the validated locals, not the raw response usage
                # — a forged negative / non-finite count must not reach the
                # cost sum (compute_llm_cost multiplies without a lower bound).
                cost_delta = compute_llm_cost(
                    {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_input_tokens": cache_read_input_tokens,
                        "cache_creation_input_tokens": cache_creation_input_tokens,
                    },
                    pricing,
                )

    # Build the delta dict and atomically apply it to the persisted
    # session_usage in a single DB transaction (SELECT FOR UPDATE on
    # PostgreSQL; SQLite's exclusive write lock on SQLite). This is the fix
    # for the read-modify-write race that caused concurrent completions to
    # overwrite each other's deltas (#9).
    delta: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
    }
    if priced:
        delta["total_cost_usd"] = cost_delta
    if llm_model:
        # Per-model attribution. Tokens are attributed whenever the model is
        # known — including unpriced turns — so the per-model token view is
        # complete; cost is attributed only when this model's turn was priced
        # (keeping the model's cost key absent otherwise, matching the flat
        # "priced ⟺ key present" contract).
        model_delta: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
        }
        if priced:
            model_delta["total_cost_usd"] = cost_delta
        delta["by_model"] = {llm_model: model_delta}

    new_current = conversation_store.increment_session_usage(session_id, delta)
    # Per-user daily rollup. A session whose model calls go through the engine's model proxy is
    # recorded there (it reads the real charge off the response); adding it here would count the
    # turn twice.
    if not _served_by_model_proxy(conv, conversation_store):
        _record_daily_cost(conv, cost_delta, conversation_store)
    return _priced_cost_for_display(new_current)


async def _persist_relay_reported_model(
    resp_obj: dict[str, Any],
    session_id: str,
    conversation_store: ConversationStore,
) -> None:
    """Persist the concrete model reported by an SDK harness response."""
    usage_obj = resp_obj.get("usage")
    if not isinstance(usage_obj, dict):
        return
    raw_model = usage_obj.get("model")
    model = concrete_reported_model(raw_model)
    if model is None:
        return
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.reported_model == model:
        return
    await asyncio.to_thread(
        conversation_store.update_conversation,
        session_id,
        reported_model=model,
    )
    session_stream.publish(
        session_id,
        SessionModelEvent(
            type="session.model",
            conversation_id=session_id,
            model=model,
        ).model_dump(exclude_none=True),
    )


def _persist_native_cumulative_usage(
    session_id: str,
    data: dict[str, Any],
    conversation_store: ConversationStore,
) -> float | None:
    """
    Persist cumulative cost / token usage reported by a native harness.

    Unlike the Omnigent relay path (:func:`_accumulate_session_usage`), which adds
    per-response *deltas*, native harnesses (claude-native / codex-native)
    report *cumulative* session usage — so the flat session fields are written
    with SET semantics. The per-model ``by_model`` buckets are the exception:
    they accumulate each report's growth (new - old) attributed to the active
    model, so they stay correct across mid-session model switches (see below).
    The two paths never run for the same session, so they don't conflict.

    Reads explicit cumulative fields from the ``external_session_usage`` event's
    ``data`` (all optional; a no-op when none are present):

    - ``cumulative_cost_usd`` — total session cost for DISPLAY, e.g.
      claude-native forwards Claude Code's own ``cost.total_cost_usd``
      (exact billing; used directly). Stored in ``total_cost_usd``, which
      drives the badge and the per-user daily rollup, so the badge matches
      ``/cost`` in the Claude TUI.
    - ``policy_cost_usd`` — total session cost for ENFORCEMENT (the
      cost-budget gate). claude-native forwards ``max(S, real-time
      transcript estimate)`` here so the gate reflects in-flight sub-agent
      spend while the displayed ``S`` is frozen for the sub-agent's run.
      Stored verbatim in ``policy_cost_usd`` (the policy engine seeds from
      it, falling back to ``total_cost_usd`` when absent). Not fed into the
      daily rollup — that uses the authoritative ``total_cost_usd``.
    - ``cumulative_input_tokens`` / ``cumulative_output_tokens`` — total session
      tokens, e.g. codex-native's ``tokenUsage.total``. When
      ``cumulative_cost_usd`` is absent, cost is computed from these via
      :func:`fetch_model_pricing`.
    - ``cumulative_cache_read_input_tokens`` — the cached portion *included
      in* ``cumulative_input_tokens`` (e.g. codex-native's
      ``tokenUsage.total.cachedInputTokens``). Split out of the input total
      so :func:`compute_llm_cost` prices it at the cache-read rate rather
      than the full input rate. Absent for harnesses that don't report it.
    - ``model`` — LLM model id to price with (e.g. ``"databricks-gpt-5-5"``);
      falls back to the agent spec's model when absent.

    The ``total_cost_usd`` key is written only on the priced branches
    below (exact billing, or token-priced when the model is in the
    catalog), so an unpriced native session leaves it absent — the same
    "priced ⟺ key present" contract the relay path uses. ``policy_cost_usd``
    is written only when the event carries it (claude-native with the
    display/policy split); codex-native and the relay omit it and the
    policy engine falls back to ``total_cost_usd``.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param data: The ``external_session_usage`` event ``data`` dict.
    :param conversation_store: Store for reading and writing ``session_usage``.
    :returns: The session's cumulative priced cost in USD after this
        update (for the caller to broadcast on a ``session.usage``
        event), or ``None`` when the session is unpriced or no
        cumulative field was present.
    :raises OmnigentError: When a cumulative field is the wrong type.
    """
    cost = _coerce_cumulative_field(data, "cumulative_cost_usd", numeric=True)
    policy_cost = _coerce_cumulative_field(data, "policy_cost_usd", numeric=True)
    cin = _coerce_cumulative_field(data, "cumulative_input_tokens", numeric=False)
    cout = _coerce_cumulative_field(data, "cumulative_output_tokens", numeric=False)
    ccache = _coerce_cumulative_field(data, "cumulative_cache_read_input_tokens", numeric=False)
    if cost is None and policy_cost is None and cin is None and cout is None:
        return None

    # Deliberately a FRESH read — never a caller-supplied row. This row is the
    # baseline for the monotonic clamps and the daily-rollup delta below, and
    # ``set_session_usage`` rewrites the whole usage JSON; a row read earlier
    # in the request would widen the forged-low-report race window and could
    # overwrite a concurrent report's growth with stale state.
    conv = conversation_store.get_conversation(session_id)
    current: dict[str, Any] = dict(conv.session_usage) if conv and conv.session_usage else {}
    # Native usage is cumulative (SET semantics), so the per-turn delta
    # for the daily rollup is new_total - old_total. Capture the old
    # cumulative + enforcement costs before the fields below overwrite them.
    # Both are clamped MONOTONIC below (a write may only raise them): the
    # ``external_session_usage`` event is posted with the session owner's own
    # bearer token (the forwarder uses no privileged identity), so a client
    # could otherwise replay it with a falsified low cost to reset the gate's
    # cost to ~0 (disabling the budget DENY/ASK) and drive the daily rollup
    # delta negative (clawing back already-spent budget). Monotonicity makes a
    # downward report a no-op, so the worst a forged post can do is leave the
    # figure unchanged. (See also the runner-token guard on cost_control.*
    # label writes — usage was the missing half.)
    old_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    old_policy_cost = float(current.get("policy_cost_usd", 0.0) or 0.0)
    # Old cumulative token totals, captured before the overwrites below so
    # per-model attribution can add only this report's growth (new - old).
    old_tokens = {key: int(current.get(key, 0) or 0) for key in _MODEL_TOKEN_KEYS}
    if cin is not None:
        # The reported input total is INCLUSIVE of cached tokens (codex's
        # ``inputTokens`` counts cache reads). Split the cached portion into
        # its own bucket so compute_llm_cost prices it at the cache-read rate;
        # ``input_tokens`` keeps only the non-cached remainder (its contract).
        # Clamp cached to the total so a malformed report never makes
        # ``input_tokens`` negative.
        cached = min(int(ccache), int(cin)) if ccache is not None else 0
        current["cache_read_input_tokens"] = cached
        current["input_tokens"] = int(cin) - cached
    if cout is not None:
        current["output_tokens"] = cout
    if cin is not None or cout is not None:
        # ``total_tokens`` reflects the full input (non-cached + cached) plus
        # output, so the split above doesn't shrink the displayed total.
        current["total_tokens"] = (
            int(current.get("input_tokens", 0))
            + int(current.get("cache_read_input_tokens", 0))
            + int(current.get("output_tokens", 0))
        )

    # Resolve the model for per-model attribution on any broadcast that carries
    # tokens OR a priced cost — both the token-pricing branch and the per-model
    # attribution below need it. A cost-only broadcast must resolve it too:
    # claude-native forwards Claude Code's statusLine total (S) with NO token
    # counts, so gating model resolution on tokens alone dropped that cost from
    # ``by_model`` entirely — the per-model TOKEN USAGE view undercounted the
    # session total by every native (sub-)agent's spend, while the flat
    # ``total_cost_usd`` (and the Session-cost badge) still included it.
    # Priority mirrors the relay path's ``_accumulate_session_usage``: the
    # event's ``model`` (the statusLine's active model, forwarded alongside the
    # cost) wins, then the session's ``model_override`` (the forwarder mirrors
    # in-pane /model switches there), then the agent spec's static model.
    # Computed once out of the pricing-only branch so attribution works even on
    # an unpriced turn. (The agent-cache lookup in ``_resolve_llm_model`` is
    # memoized, so resolving on cost-only polls is cheap.)
    has_tokens = cin is not None or cout is not None
    needs_model = has_tokens or cost is not None
    model_name = (
        (
            data.get("model")
            or (conv.model_override if conv and conv.model_override else None)
            or _resolve_llm_model(conv)
        )
        if needs_model
        else None
    )
    if cost is not None:
        # Monotonic: a reported total below the persisted one is ignored.
        current["total_cost_usd"] = max(old_cost, float(cost))
    elif has_tokens:
        if isinstance(model_name, str) and model_name:
            from omnigent.llms.context_window import (
                compute_llm_cost,
                fetch_model_pricing_with_provider,
            )
            from omnigent.onboarding.provider_config import load_config

            # Load provider config to check for custom pricing
            provider_config = load_config()
            # Resolve harness from conversation (agent spec + overrides)
            harness = _resolve_harness(conv) if conv else None
            pricing = fetch_model_pricing_with_provider(
                model_name, provider_config=provider_config, harness=harness
            )
            if pricing is not None:
                # SET (cumulative) — price the running token totals.
                # ``current`` carries the cache-read split when the harness
                # reports it (codex-native does), so compute_llm_cost prices
                # cache reads at their own rate; it falls back to the input
                # rate for cache tokens when the catalog omits a cache price
                # (e.g. ``databricks-*`` entries today).
                # Monotonic, like the explicit-cost branch: token totals are
                # also client-SET, so a lowered token report can't drop the
                # priced cost below the persisted figure.
                current["total_cost_usd"] = max(old_cost, compute_llm_cost(current, pricing))

    # ADD this report's growth (new - old) to the active model, not the whole
    # cumulative total, so per-model buckets sum to the flat total across model
    # switches. Clamp >= 0 so a lowered report never claws a bucket back (flat
    # tokens are SET not clamped, so buckets can exceed them then — fail-safe).
    if isinstance(model_name, str) and model_name:
        bucket = _model_usage_bucket(current, model_name)
        for key in _MODEL_TOKEN_KEYS:
            if key in current:
                bucket[key] = int(bucket.get(key, 0)) + max(0, int(current[key]) - old_tokens[key])
        if "total_cost_usd" in current:
            bucket["total_cost_usd"] = float(bucket.get("total_cost_usd", 0.0)) + max(
                0.0, float(current["total_cost_usd"]) - old_cost
            )

    # Enforcement value (claude-native display/policy split). Stored
    # separately from the displayed ``total_cost_usd`` so the gate can read
    # the real-time figure (incl. in-flight sub-agent spend) while the badge
    # shows the frozen statusLine total. Monotonic, like total_cost_usd: this
    # is the value the cost-budget gate actually reads, so a forged low report
    # must never lower it. When an in-flight estimate later resolves below a
    # prior peak the clamp keeps the peak — conservative (the gate errs toward
    # MORE enforcement, never less), which is the safe direction for a budget.
    if policy_cost is not None:
        current["policy_cost_usd"] = max(old_policy_cost, float(policy_cost))

    conversation_store.set_session_usage(session_id, current)
    # Per-user daily rollup. Native reports cumulative totals, so the turn's
    # delta is the increase in cumulative cost. Uses the authoritative
    # ``total_cost_usd`` (= statusLine S), NOT ``policy_cost_usd`` — the
    # daily report must reflect real spend, not the real-time gate estimate.
    new_cost = float(current.get("total_cost_usd", 0.0) or 0.0)
    # Non-negative by the monotonic clamp above; ``max(0.0, ...)`` keeps the
    # daily rollup from ever being clawed back even if that invariant changes.
    if not _served_by_model_proxy(conv, conversation_store):
        _record_daily_cost(conv, max(0.0, new_cost - old_cost), conversation_store)
    return _priced_cost_for_display(current)


async def _persist_external_session_usage(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    conv: Conversation | None = None,
    user_id: str | None = None,
    host_installation_id: str | None = None,
) -> int | None:
    """
    Persist and broadcast a token-usage update from a terminal-backed runtime.

    At least one of ``data.context_tokens`` (non-negative int),
    ``data.context_window`` (positive int), or a cumulative usage field
    (:func:`_persist_native_cumulative_usage`) must be present.

    :param session_id: Session/conversation identifier.
    :param body: External session-usage event body.
    :param conversation_store: Store used to upsert the labels.
    :param conv: The session's already-loaded conversation row, when the
        caller holds one (the events route's access check reads it). Supplies
        the tree root to the subtree roll-up and the ancestor publish — both
        read-only, and both verify a supplied root against the tree it
        produces — so those steps don't re-read the row. NOT passed to the
        own-usage persist: its monotonic-clamp baseline must be a fresh read
        (see :func:`_persist_native_cumulative_usage`). ``None`` makes each
        step resolve the row itself.
    :param user_id: The flush POST's authenticated caller, hashed into the
        usage telemetry event. ``None`` falls back to the ambient request user.
    :param host_installation_id: Installation ID of the machine that posted the
        flush, read from the request header. Recorded on the usage telemetry
        event; ``None`` leaves the field unset rather than costing a lookup.
    :returns: The persisted ``context_tokens`` when present, else ``None``.
    :raises OmnigentError: On missing / malformed fields.
    """
    raw_tokens = body.data.get("context_tokens")
    if raw_tokens is not None and (not isinstance(raw_tokens, int) or raw_tokens < 0):
        raise OmnigentError(
            "external_session_usage data.context_tokens must be a non-negative int",
            code=ErrorCode.INVALID_INPUT,
        )
    raw_window = body.data.get("context_window")
    if raw_window is not None and (not isinstance(raw_window, int) or raw_window <= 0):
        raise OmnigentError(
            "external_session_usage data.context_window must be a positive int",
            code=ErrorCode.INVALID_INPUT,
        )
    _CUMULATIVE_USAGE_KEYS = (
        "cumulative_cost_usd",
        # ``policy_cost_usd`` alone is a valid post: mid-turn the displayed
        # statusLine total (``cumulative_cost_usd``) is frozen, so the
        # forwarder posts only the advancing real-time enforcement cost.
        "policy_cost_usd",
        "cumulative_input_tokens",
        "cumulative_output_tokens",
    )
    has_cumulative = any(body.data.get(k) is not None for k in _CUMULATIVE_USAGE_KEYS)
    if raw_tokens is None and raw_window is None and not has_cumulative:
        raise OmnigentError(
            "external_session_usage requires at least one of "
            "data.context_tokens, data.context_window, or a cumulative usage field",
            code=ErrorCode.INVALID_INPUT,
        )

    # Native harnesses report cumulative cost / tokens (SET semantics) — distinct
    # from the Omnigent relay's per-response accumulation. Persist this session's
    # own cumulative usage (its priced own-cost return is unused — the badge shows
    # the subtree total computed below, not own cost).
    await asyncio.to_thread(
        _persist_native_cumulative_usage,
        session_id,
        body.data,
        conversation_store,
    )
    _n_in = body.data.get("cumulative_input_tokens")
    _n_out = body.data.get("cumulative_output_tokens")
    _n_cost = body.data.get("cumulative_cost_usd")
    _n_model: str | None = body.data.get("model") or None
    if _n_in is not None or _n_out is not None or _n_cost is not None:
        _tel_emit(
            _TelNativeSessionUsageEvent(
                installation_id=_get_installation_id(),
                session_id=session_id,
                anon_user_id=(
                    _tel_anon_user_id(user_id, _get_installation_id())
                    if user_id is not None
                    else _tel_current_anon()
                ),
                host_installation_id=host_installation_id,
                input_tokens=int(_n_in) if _n_in is not None else None,
                output_tokens=int(_n_out) if _n_out is not None else None,
                cost_usd=(float(_n_cost) if isinstance(_n_cost, (int, float)) else None),
                model=_n_model,
            )
        )
    label_updates: dict[str, str] = {}
    if raw_tokens is not None:
        label_updates[_LAST_CONTEXT_TOKENS_LABEL_KEY] = str(raw_tokens)
    if raw_window is not None:
        label_updates[_LAST_CONTEXT_WINDOW_LABEL_KEY] = str(raw_window)
    await asyncio.to_thread(
        conversation_store.set_labels,
        session_id,
        label_updates,
    )
    # The displayed cost is this session's SUBTREE total (itself + its
    # sub-agents), matching the GET snapshot. A sub-agent persists its spend on
    # its own child conversation, so broadcasting only this session's own cost
    # would drop a parent's badge back to own-cost on every parent flush and
    # hide in-flight sub-agent spend until the next child flush (the badge would
    # oscillate own ⇄ subtree). For a childless session the subtree is just
    # itself, so this equals own cost — one indexed tree query per flush. The
    # caller's row supplies the root so the tree scan needs no extra row read.
    subtree_usage = await asyncio.to_thread(
        load_session_usage,
        session_id,
        conversation_store,
        root_conversation_id=conv.root_conversation_id if conv is not None else None,
    )
    subtree_cost = _priced_cost_for_display(subtree_usage)
    usage_by_model = _usage_by_model_for_display(subtree_usage)
    # Only include fields that were sent; the client treats absent
    # fields as "no change" so a window-only update doesn't zero tokens.
    # ``total_cost_usd`` is included only when the subtree is priced
    # (``exclude_none`` strips it otherwise) — an unpriced session keeps
    # showing "—" from the snapshot rather than a misleading $0.00.
    event_payload: dict[str, Any] = {
        "type": "session.usage",
        "conversation_id": session_id,
    }
    if raw_tokens is not None:
        event_payload["context_tokens"] = raw_tokens
    if raw_window is not None:
        event_payload["context_window"] = raw_window
    if subtree_cost is not None:
        event_payload["total_cost_usd"] = subtree_cost
    if usage_by_model is not None:
        event_payload["usage_by_model"] = usage_by_model
    event = SessionUsageEvent(**event_payload)
    session_stream.publish(session_id, event.model_dump(exclude_none=True))
    # This session's usage also moves its ANCESTORS' subtree cost (its spend
    # rolls up into every ancestor), so re-publish each ancestor's subtree cost
    # too — otherwise a grandparent's badge wouldn't reflect a deep descendant.
    # No-op for a top-level session (no ancestors). Threaded: it pages the
    # conversation tree per ancestor.
    await asyncio.to_thread(
        _publish_subtree_cost_to_ancestors,
        conversation_store,
        session_id,
        conv,
    )
    return raw_tokens


def _context_labels_from_turn_usage(
    resp_usage: dict[str, Any],  # type: ignore[explicit-any]  # SSE usage payload
) -> dict[str, str]:
    """Build context-window indicator labels from an in-process turn's usage.

    An in-process harness (``claude-sdk`` / ``pi``) reports
    the turn's window fill as ``usage.context_tokens`` and its observed model as
    ``usage.model`` on ``response.completed``. Mirror the two labels the
    claude-native ``external_session_usage`` path writes, so the web context
    ring renders identically: the numerator
    (:data:`_LAST_CONTEXT_TOKENS_LABEL_KEY`) from ``context_tokens`` and the
    denominator (:data:`_LAST_CONTEXT_WINDOW_LABEL_KEY`) from the model's
    catalog window. This is what lets a model-unpinned claude-sdk session show a
    context ring at all — it has no spec model to size the window from.

    Returns an empty dict when the turn reports no ``context_tokens`` (a native
    terminal harness reports its fill via ``external_session_usage`` instead),
    so this path stays in-process-only and never double-writes the labels.

    :param resp_usage: The ``response.usage`` dict from a ``response.completed``
        event.
    :returns: Label updates for :meth:`ConversationStore.set_labels`; empty when
        the turn carries no window-fill signal.
    """
    context_tokens = resp_usage.get("context_tokens")
    if not isinstance(context_tokens, int) or context_tokens < 0:
        return {}
    labels: dict[str, str] = {_LAST_CONTEXT_TOKENS_LABEL_KEY: str(context_tokens)}
    model = resp_usage.get("model")
    if isinstance(model, str) and model:
        from omnigent.llms.context_window import get_model_context_window

        try:
            window = get_model_context_window(model)
        except Exception:  # noqa: BLE001 — window lookup is best-effort; skip the denominator on a miss
            window = None
        if isinstance(window, int) and window > 0:
            labels[_LAST_CONTEXT_WINDOW_LABEL_KEY] = str(window)
    return labels


async def _persist_model_change_note(
    session_id: str,
    model_override: str | None,
    conversation_store: ConversationStore,
) -> None:
    """
    Append a ``[System: ...]`` transcript note recording a model switch.

    Records a web/REPL ``/model`` change as a user-role system marker
    (the web UI renders ``[System: ...]`` user messages centered + muted
    via ``SystemMessageView``) so the user gets a durable record in the
    conversation that the switch happened — not just a transient composer
    hint. Persisted through the store as append-only history (does NOT
    start an agent turn, unlike the message-post path) and published over
    SSE so connected clients render it live.

    The caller gates this to **non-native** sessions (those WITHOUT an
    ``omnigent.wrapper`` native label, via ``_is_native_terminal_session``)
    and to real ``/model`` commands: claude-native / codex-native manage
    their model through the in-TUI picker / launch flag and must not receive
    an injected AP-side item, and ``silent`` bind-time auto-applies are
    skipped (see the ``live_forward`` guard in ``update_session``). The gate
    keys on ``omnigent.wrapper`` rather than ``omnigent.ui == "terminal"``
    because the latter is also set on chat-first SDK sessions that expose a
    REPL terminal view (e.g. polly / debby), which DO want the note. The note
    is a user-role message, so the agent sees it in history on the next turn —
    consistent with other ``[System: ...]`` markers (timer fired, sub-agent
    done).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param model_override: The new model id, e.g.
        ``"databricks-gpt-5-4"``, or ``None`` when the override was
        cleared back to the agent default.
    :param conversation_store: Store used to append the note item.
    :returns: None.
    """
    text = (
        f"[System: model changed to {model_override}]"
        if model_override is not None
        else "[System: model reset to the agent default]"
    )
    item = NewConversationItem(
        type="message",
        response_id=generate_task_id(),
        data=MessageData(
            role="user",
            is_system_notice=True,
            content=[{"type": "input_text", "text": text}],
        ),
    )
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    _publish_external_conversation_item(session_id, persisted_items[0])


async def _resolve_elicitation(
    session_id: str,
    data: dict[str, Any],
    runner_router: RunnerRouter | None,
    conversation_store: ConversationStore | None = None,
) -> None:
    """
    Resolve one outstanding elicitation from an approval payload.

    Shared by the two entry points that deliver a verdict for a
    parked elicitation: the ``type == "approval"`` branch of
    ``POST /v1/sessions/{id}/events`` and the dedicated
    ``POST /v1/sessions/{id}/elicitations/{eid}/resolve`` URL
    endpoint (URL-based elicitation). Both converge here so
    resolution semantics — server-side harness Future, sidebar
    badge clear, and runner forward — stay identical regardless of
    how the verdict arrived.

    Three effects, in order:

    1. **Server-side harness Future.** Claude-native permission
       hooks (and any other server-parked elicitation) register a
       Future in ``_harness_elicitation_registry``. If one exists
       for this id, is unresolved, and is owned by *this* session
       (cross-user guard), set its result. An
       ownership mismatch silently skips resolution — the runner
       forward below still fires so a runner-side elicitation with
       the same id can reject it on its own terms.
    2. **Sidebar badge clear.** Publish
       ``response.elicitation_resolved`` so every subscribed client
       (other tabs, the REPL TUI) flips its ``ApprovalCard`` and the
       pending-elicitation badge decrements. Idempotent.
    3. **Runner forward.** Runner-side elicitations (policy
       approvals parked in the runner's ``_pending_approvals`` dict)
       resolve when the approval event reaches the runner's
       ``/events``. Forwarded as a canonical ``approval`` event.

    :param session_id: Session/conversation identifier that owns
        the elicitation, e.g. ``"conv_abc123"``.
    :param data: Approval payload carrying the ``elicitation_id``
        correlation key plus the MCP ``ElicitationResult`` fields
        (``action``, optional ``content``), e.g.
        ``{"elicitation_id": "elicit_abc", "action": "accept"}``.
    :param runner_router: Router used to resolve the session's bound
        runner for the forward, or ``None`` in in-process setups
        (the forward is skipped when no runner is bound).
    :param conversation_store: Optional store used to mirror the
        resolved signal into ancestor streams when ``session_id`` is
        a child session. ``None`` keeps the signal scoped locally.
    """
    # Empty-string default is intentional, NOT a fail-loud miss: the
    # resolve-URL caller always supplies the id (it comes from the URL
    # path), but the public ``approval`` event caller may post a
    # malformed body. A missing id degrades gracefully below (no Future
    # matches, no resolved event published) rather than 500-ing the
    # client — the runner forward still fires so the runner can reject.
    elicitation_id = data.get("elicitation_id", "")
    harness_future = _harness_elicitation_registry.get(elicitation_id)
    if harness_future is not None and not harness_future.done():
        # Only the session that owns this elicitation
        # may resolve its server-side Future. A mismatch skips
        # resolution (the runner forward still fires below).
        if _harness_elicitation_owners.get(elicitation_id) == session_id:
            result_payload = {
                k: v for k, v in data.items() if k not in ("elicitation_id", "interrupt")
            }
            try:
                verdict_result = ElicitationResult.model_validate(result_payload)
            except ValidationError:
                _logger.warning(
                    "Invalid approval payload for %r",
                    elicitation_id,
                    exc_info=True,
                )
            else:
                harness_future.set_result(verdict_result)
                # The waiter may be a zombie: a proxy can sever the
                # long-poll client-side while holding the backend
                # connection open, so the verdict set above is written to
                # a connection nobody reads and disconnect detection never
                # fires. Tombstone the verdict too, so a re-park of the
                # same stable id adopts it instead of re-asking; a verdict
                # that WAS delivered leaves a tombstone that ages out
                # unconsumed (session-checked, TTL-pruned). The parked
                # waiter's request fingerprint rides along so a LATER,
                # different question reusing the id can't inherit it.
                _parked_for_fingerprint = _harness_parked_elicitations.get(elicitation_id)
                _prune_pre_resolved_harness_elicitations()
                _harness_pre_resolved_elicitations[elicitation_id] = (
                    _PreResolvedHarnessElicitation(
                        session_id=session_id,
                        created_at=time.time(),
                        result=verdict_result,
                        request_fingerprint=(
                            _parked_for_fingerprint.request_fingerprint
                            if _parked_for_fingerprint is not None
                            else None
                        ),
                    )
                )
                _prune_pre_resolved_harness_elicitations()
    elif harness_future is None and isinstance(elicitation_id, str) and elicitation_id:
        # Nothing parked (severed long-poll mid-retry, or a runner-side
        # id that just ages out) — tombstone the verdict so a re-park
        # returns it; consume is session-checked, so no cross-session use.
        result_payload = {k: v for k, v in data.items() if k != "elicitation_id"}
        try:
            pre_resolved = ElicitationResult.model_validate(result_payload)
        except ValidationError:
            pre_resolved = None
        if pre_resolved is not None:
            # The pending index still holds the answered prompt (the
            # resolved fan-out below drains it), so fingerprint the
            # tombstone with the question's params: a later, different
            # question that reuses this id must not inherit the verdict.
            gap_fingerprint: str | None = None
            pending_entry = pending_elicitations.lookup(elicitation_id)
            if pending_entry is not None and pending_entry[0] == session_id:
                try:
                    gap_fingerprint = _harness_elicitation_request_fingerprint(
                        ElicitationRequestParams.model_validate(pending_entry[1].get("params"))
                    )
                except ValidationError:
                    gap_fingerprint = None
            _prune_pre_resolved_harness_elicitations()
            _harness_pre_resolved_elicitations[elicitation_id] = _PreResolvedHarnessElicitation(
                session_id=session_id,
                created_at=time.time(),
                result=pre_resolved,
                request_fingerprint=gap_fingerprint,
            )
            _prune_pre_resolved_harness_elicitations()
    # Wake a currently-parked long-poll via resolved_elsewhere, not only its
    # Future: setting the Future alone races the sever/re-park cycle and the
    # ASK-gated call hangs. Set the event directly; the signal helper's
    # parked-is-None branch would clobber the verdict-carrying tombstone.
    if isinstance(elicitation_id, str) and elicitation_id:
        _parked = _harness_parked_elicitations.get(elicitation_id)
        if _parked is not None and _harness_elicitation_owners.get(elicitation_id) == session_id:
            _parked.resolved_elsewhere.set()

    # Fan-out for every other subscribed client (other tabs, REPL
    # TUI). Idempotent vs. the runner's own ``wait_for_user_approval``
    # finally / harness hook finally — those also publish for the id.
    # The verdict rides along so the parent-side resolution notice
    # states how the gate was answered instead of a bare "resolved".
    if isinstance(elicitation_id, str) and elicitation_id:
        _publish_elicitation_resolved(session_id, elicitation_id, action=data.get("action"))
        if conversation_store is not None:
            await asyncio.to_thread(
                _publish_elicitation_resolved_to_ancestors,
                conversation_store,
                session_id,
                elicitation_id,
                data.get("action"),
            )
    # Runner-side elicitations (policy approvals, scaffold dispatch)
    # resolve when the canonical approval event reaches the runner.
    await _forward_approval_to_runner(session_id, data, runner_router)


async def _hold_native_ask_gate(*args: Any, **kwargs: Any) -> bool:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._hold_native_ask_gate(*args, **kwargs)


async def _hold_native_ask_gate_impl(
    request: Request,
    *,
    session_id: str,
    phase: Phase,
    data: dict[str, Any],
    engine: PolicyEngine,
    result: PolicyResult,
    conversation_store: ConversationStore,
    elicitation_id: str | None = None,
) -> bool:
    """
    Hold a server-side ASK gate until a human resolves it.

    Publishes a ``response.elicitation_request`` (the web UI / REPL
    render the approve card) and parks a server-side Future via
    :func:`_publish_and_wait_for_harness_elicitation`, exactly as the
    ``PermissionRequest`` hook does. The human approves through the
    elicitation's resolve URL; this collapses the verdict to a single
    boolean the caller maps to ALLOW / DENY.

    Used for any phase whose ASK must be resolved on the server rather
    than by a runner-side ``wait_for_user_approval`` park:
    :attr:`Phase.TOOL_CALL` (the native ``PreToolUse`` hook gate) and
    :attr:`Phase.REQUEST` (the user-message input gate, which has no
    runner in the loop yet — see :func:`_evaluate_input_policy`).

    Unlike the old ASK→``defer`` path, the gate lives on the server,
    so a permissive native ``permission_mode`` (``acceptEdits`` /
    ``bypassPermissions``) cannot skip it — the action stays blocked
    until a real human verdict. Timeout / disconnect fail closed
    (return ``False`` → DENY).

    On approve, the ASK-accumulated ``set_labels`` / ``state_updates``
    are applied (POLICIES.md §7.2: side effects land only on approve);
    a denied / timed-out ASK leaves no trace.

    :param request: FastAPI request, for upstream-disconnect detection
        inside the parking helper.
    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param phase: Enforcement phase being gated, e.g.
        :attr:`Phase.TOOL_CALL` or :attr:`Phase.REQUEST`.
    :param data: The proto event ``data`` — for a tool call,
        ``{"name": "Bash", "arguments": {"command": "ls"}}``; for a
        request, the user ``message`` body
        (``{"role": "user", "content": [...]}``).
    :param engine: The policy engine, used to resolve the per-policy
        ``ask_timeout`` and to apply approved side effects.
    :param result: The composed ASK :class:`PolicyResult` — carries
        the reason, deciding_policy, and withheld set_labels.
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :param elicitation_id: Optional stable re-attach id from the
        calling hook, e.g. ``"elicit_evaluate_abc123"``. When supplied,
        ``_publish_and_wait_for_harness_elicitation`` re-attaches to the
        existing parked elicitation rather than publishing a new card —
        used by ``POST /policies/evaluate`` retries so a hook retry after
        a transient 5xx / connect-drop does not prompt the human twice.
        ``None`` mints a fresh id (the default for non-retry callers).
    :returns: ``True`` iff a human accepted; ``False`` on cancel /
        timeout / disconnect (fail closed).
    :raises ElicitationDeclinedError: when the human explicitly
        declines (``action == "decline"``). Callers should abort the
        turn rather than continuing with a DENY.
    """
    tool_name = data.get("name")
    tool_input = data.get("arguments")
    params = ElicitationRequestParams(
        mode="form",
        message=result.reason or "Approval required",
        requestedSchema={},
        phase=phase.value,
        policy_name=result.deciding_policy or "unknown",
        content_preview=json.dumps(data)[:1024],
    )
    # Per-policy ``ask_timeout`` override wins over the spec-level default.
    timeout_s = float(resolve_ask_timeout(engine, result))
    # Use the caller-supplied id when present (hook retries re-attach to
    # the same elicitation); otherwise mint a fresh one.
    if elicitation_id is None:
        elicitation_id = f"elicit_{secrets.token_hex(16)}"
    verdict = await _publish_and_wait_for_harness_elicitation(
        request,
        session_id=session_id,
        params=params,
        timeout_s=timeout_s,
        elicitation_id=elicitation_id,
        conversation_store=conversation_store,
        tool_name=tool_name if isinstance(tool_name, str) else None,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
    )
    # Explicit user decline → raise so callers can abort the turn rather
    # than feeding a DENY message to the LLM and letting it continue.
    if verdict is not None and verdict.action == "decline":
        raise ElicitationDeclinedError(
            result.reason or "",
            policy_name=result.deciding_policy,
        )
    approved = verdict is not None and verdict.action == "accept"
    if approved:
        # POLICIES.md §7.2: writes accumulated by the ASKing policy
        # land only on approve.
        if result.set_labels:
            engine.apply_label_writes(result.set_labels)
        if result.state_updates:
            with contextlib.suppress(ConversationNotFoundError):
                engine.apply_state_updates(result.state_updates)
    return approved


def _native_mirror_lock(session_id: str) -> asyncio.Lock:
    """
    Return the lock serializing native transcript mirrors for one conversation.

    The persist site probes the store for an already-persisted mirror, drains
    the pending-input queue, and appends — three steps that must not interleave
    with a retry of the same mirror, or the retry can pass the probe, match a
    newer identical queued message, and write undelivered pairs for entries
    that are still on their way. Get-or-create is race-free because there is no
    ``await`` between the lookup and the insert (single event loop).

    :param session_id: Conversation id whose mirrors are serialized.
    :returns: A process-wide :class:`asyncio.Lock` shared by every concurrent
        mirror for ``session_id``.
    """
    lock = _native_mirror_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _native_mirror_locks[session_id] = lock
    return lock


def _drains_pending_inputs(item: NewConversationItem) -> bool:
    """
    Whether a mirrored item settles a queued web message.

    True for a web-composer user message echoed back by the transcript and for
    a slash command (typed in the web composer as plain text, mirrored as a
    ``slash_command`` item). Assistant and tool items never touch the queue.

    :param item: The parsed external item.
    :returns: ``True`` when persisting *item* drains a pending-input entry.
    """
    if item.type == "slash_command":
        return isinstance(item.data, SlashCommandData)
    return (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
        and not _is_native_interrupt_record(item.data)
    )


async def _persist_external_conversation_item(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
    enabled: bool = True,
) -> str:
    """
    Persist and broadcast a conversation item produced outside AP.

    Serialized per conversation (see :func:`_native_mirror_lock`) so a retried
    mirror observes the first attempt's commit before it touches the
    pending-input queue, and shielded from the caller's cancellation: a
    request dropped mid-append must still settle the queue entries it holds
    and publish its receipts, or they would stay held until the TTL and the
    live tab would never be acknowledged. See
    :func:`_persist_external_conversation_item_unlocked` for the parameters
    and behaviour.

    :returns: Store-assigned conversation item id.
    """
    task = asyncio.ensure_future(
        _persist_external_conversation_item_serialized(
            session_id,
            conv,
            body,
            conversation_store,
            created_by=created_by,
            background_title_coordinator=background_title_coordinator,
            enabled=enabled,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # The detached persist finishes on its own; keep its failure visible.
        task.add_done_callback(lambda done: _log_detached_mirror_persist(session_id, done))
        raise


def _log_detached_mirror_persist(session_id: str, task: asyncio.Task[str]) -> None:
    """
    Log a native mirror persist that failed after its request was cancelled.

    :param session_id: Conversation the mirror belonged to.
    :param task: The finished shielded persist task.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.warning(
            "native mirror persist for session=%s failed after its request was cancelled: %r",
            session_id,
            exc,
            extra={"session_id": session_id},
        )


async def _persist_external_conversation_item_serialized(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
    enabled: bool = True,
) -> str:
    """
    Run :func:`_persist_external_conversation_item_unlocked` under the mirror lock.

    :returns: Store-assigned conversation item id.
    """
    async with _native_mirror_lock(session_id):
        return await _persist_external_conversation_item_unlocked(
            session_id,
            conv,
            body,
            conversation_store,
            created_by=created_by,
            background_title_coordinator=background_title_coordinator,
            enabled=enabled,
        )


async def _persist_external_conversation_item_unlocked(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    created_by: str | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
    enabled: bool = True,
) -> str:
    """
    Persist and broadcast a conversation item produced outside AP.

    This is the transcript bridge path for native Claude. It appends
    user messages, assistant messages, tool calls, and tool results
    without starting or steering the placeholder Omnigent agent.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: Conversation row for title seeding.
    :param body: External item event body.
    :param conversation_store: Store used to append the item.
    :param created_by: Authenticated identity of the actor whose
        request triggered the forwarder POST, e.g.
        ``"alice@example.com"``. Used to attribute user messages typed
        directly in the native terminal (no pending-input entry exists
        for those). ``None`` in single-user / unauthenticated mode —
        no label is stamped in that case.
    :returns: Store-assigned conversation item id.
    """
    item = _new_external_conversation_item(session_id, body)
    if item.stable_id is not None and _drains_pending_inputs(item):
        # A forwarder retry of a mirror already persisted under its source-derived
        # id: nothing to drain, persist or publish. Draining again could match a
        # NEWER identical queued message and mark everything queued in between
        # as undelivered. Items that never touch the queue skip the lookup; their
        # retries are absorbed by the idempotent append below.
        existing = await asyncio.to_thread(conversation_store.get_item, session_id, item.stable_id)
        if existing is not None:
            return existing.id
    # A native user message round-tripping back from the transcript:
    # drain its optimistic pending-input entry and fold the entry's file
    # blocks (image / file) into the item BEFORE persisting. The transcript
    # is text-only, so without this the image is dropped from durable
    # history and disappears on every reload / navigation.
    # The vendor CLI's own interrupt record is exempt: it is synthesized by
    # Claude (not a queued web message) and has no pending entry, so
    # draining for it would hand the queued message's uploads to the marker.
    cleared_pending_id: str | None = None
    drained: pending_inputs.DrainedInput | None = None
    # Every drain below holds its entries in place (``hold=True``): they keep
    # their slot until the append settles, so a failed append restores the
    # queue exactly and a refill meanwhile cannot evict them. Older entries a
    # text match jumped over: a user message with a retry-safe identity
    # surfaces them as undelivered; a source-less mirror or a slash command
    # only holds them and puts them back afterwards.
    skipped_pending: list[pending_inputs.DrainedInput] = []
    uncertain_pending: list[pending_inputs.DrainedInput] = []
    held_older: list[pending_inputs.DrainedInput] = []
    if (
        item.type == "message"
        and isinstance(item.data, MessageData)
        and item.data.role == "user"
        and not item.data.is_meta
        and not _is_native_interrupt_record(item.data)
    ):
        # Match the mirror to its entry by text: a pasted message the TUI never
        # recorded leaves a stale head entry, and draining by position would
        # hand it to THIS message (see ``omnigent.runtime.pending_inputs``).
        # Skipped older entries are persisted as undelivered. A miss falls back
        # to the oldest entry.
        text = _message_text(item.data.content) or ""
        matched = pending_inputs.resolve_matching_text(session_id, text, hold=True)
        drained = matched.matched
        if item.stable_id is not None:
            skipped_pending = matched.skipped
            # Jumped-over entries that a positional drain may already have
            # settled: drained without an undelivered record.
            uncertain_pending = matched.uncertain
        else:
            # A mirror without a source id cannot be told from a retry of an
            # already-persisted one, and a retry matching a newer identical
            # message would brand everything queued in between undelivered.
            # Leave the older entries queued for a later mirror instead.
            held_older = [*matched.skipped, *matched.uncertain]
        if drained is None:
            drained = pending_inputs.resolve_oldest(session_id, hold=True)
            if drained is not None:
                # The mirror's true owner may be any entry still queued, so none
                # of them can be declared undelivered later.
                pending_inputs.mark_uncertain(session_id)
        if drained is not None:
            cleared_pending_id = drained.pending_id
            item = _merge_pending_file_blocks(item, drained.content)
            # Apply the original sender's identity recorded at POST time.
            # The transcript forwarder is the single writer here and has no
            # auth context, so the persisted item would otherwise have
            # created_by=None, causing session.input.consumed to broadcast
            # without an author — the label would flash in from the optimistic
            # bubble then disappear once the committed item arrived.
            if drained.created_by is not None and item.created_by is None:
                item = item.model_copy(update={"created_by": drained.created_by})
            # A web client that sends stable_id gets store-level idempotency:
            # use it directly as the item id so the append is a no-op on retry.
            # source_id from the forwarder takes precedence when both are set.
            if drained.stable_id is not None and item.stable_id is None:
                item = item.model_copy(update={"stable_id": drained.stable_id})
        elif item.created_by is None and created_by is not None:
            # No pending entry — direct terminal input. Fall back to the
            # identity authenticated on the forwarder's own request.
            item = item.model_copy(update={"created_by": created_by})
    elif item.type == "slash_command" and isinstance(item.data, SlashCommandData):
        # A command typed in the web composer was queued as plain text but comes
        # back as a slash_command item. Drain its own entry so it is not later
        # mistaken for a lost message; older entries stay in place.
        command_line = f"/{item.data.name} {item.data.arguments}".strip()
        matched = pending_inputs.resolve_matching_text(session_id, command_line, hold=True)
        drained = matched.matched
        if drained is not None:
            cleared_pending_id = drained.pending_id
        held_older = matched.skipped
    # Build the batch: skipped entries first (their positions must precede
    # the matched item to match broadcast order), then the anchor. Each
    # skipped entry gets a pair of items (user message + error) with stable
    # IDs derived from pending_id, so the whole batch is idempotent under the
    # append lock. A drain reports at most ``pending_inputs.
    # _MAX_ENTRIES_PER_CONVERSATION`` skipped entries, so one append writes at
    # most ``2 * cap + 1`` rows whatever state a rolled-back drain left the
    # queue in. A concurrent retry that slipped past the probe above comes
    # back deduplicated and its queue entries are unheld.
    try:
        skipped_new_items = _build_skipped_items(session_id, skipped_pending)
        batch = [*skipped_new_items, item]
        pending_background_title = prepare_background_session_title(
            coordinator=background_title_coordinator,
            conversation=conv,
            event=SessionEventInput(type=item.type, data=item.data.model_dump()),
            enabled=enabled and (drained is None or drained.background_titles_enabled),
        )
        persisted_items = await asyncio.to_thread(conversation_store.append, session_id, batch)
    except Exception:
        # Nothing was committed: unhold every drained entry so the forwarder's
        # retry drains the same entries and surfaces the same undelivered
        # messages.
        _restore_drained_inputs(
            session_id, [*skipped_pending, *uncertain_pending, *held_older], drained
        )
        raise
    persisted = persisted_items[-1]
    if persisted.deduplicated:
        # A re-post of an already-committed item: nothing new to render or
        # title. Every pending entry consumed above belongs to a LATER user
        # message.
        _restore_drained_inputs(
            session_id, [*skipped_pending, *uncertain_pending, *held_older], drained
        )
        return persisted.id
    # Landed: the drained entries are settled (uncertain ones leave without a
    # record — their mirror may already have been attributed by position);
    # older messages a slash command jumped over are still on their way and
    # go back into play.
    _release_drained_inputs(session_id, [*skipped_pending, *uncertain_pending, drained])
    _restore_drained_inputs(session_id, held_older, None)
    # Not a duplicate: publish side effects for each skipped pair. Items are
    # [user0, error0, user1, error1, ...]; 2 per skipped entry. The consumed
    # event names each skipped entry so clients settle those bubbles in order
    # before the matched message's own receipt arrives.
    for i, skipped in enumerate(skipped_pending):
        persisted_user = persisted_items[i * 2]
        persisted_error = persisted_items[i * 2 + 1]
        if not persisted_user.deduplicated:
            _publish_input_consumed(
                session_id, persisted_user, cleared_pending_id=skipped.pending_id
            )
            _publish_external_conversation_item(session_id, persisted_error)
    await _seed_missing_title_from_user_message(conv, item, conversation_store)
    if pending_background_title is not None:
        pending_background_title.schedule(expected_seed_title=conv.title)
    _publish_persisted_external_item(
        session_id, body, persisted, cleared_pending_id=cleared_pending_id
    )
    return persisted.id


def _new_external_conversation_item(
    session_id: str, body: SessionEventInput
) -> NewConversationItem:
    """Parse an external item event, keyed by its ``source_id`` when it has one."""
    item = _parse_external_conversation_item(body)
    # An at-least-once producer (the native transcript forwarders) retries a
    # timed-out POST it cannot know the disposition of, so the item's id is
    # derived from its ``source_id`` and the append is idempotent — the
    # dedupe check rides the append's own transaction, under its
    # conversation lock, costing the hot path no extra query. A dedupe hit
    # comes back flagged so the caller can unwind the duplicate's side effects
    # (no re-broadcast, and a wrongly-drained pending input is restored).
    source_id = body.data.get("source_id")
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 256:
            raise OmnigentError(
                "external_conversation_item data.source_id must be a "
                "non-empty string of at most 256 characters",
                code=ErrorCode.INVALID_INPUT,
            )
        item = item.model_copy(
            update={
                "stable_id": uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"omnigent-external-item:{session_id}:{source_id.strip()}",
                ).hex
            }
        )
    return item


def _publish_persisted_external_item(
    session_id: str,
    body: SessionEventInput,
    persisted: ConversationItem,
    cleared_pending_id: str | None = None,
) -> None:
    """Broadcast a newly persisted external item and drive any elicitation it resolves."""
    message_id = body.data.get("message_id")
    _publish_external_conversation_item(
        session_id,
        persisted,
        cleared_pending_id=cleared_pending_id,
        message_id=message_id if isinstance(message_id, str) else None,
    )
    _drive_terminal_resolved_elicitation(session_id, persisted)


async def _persist_external_conversation_items(
    session_id: str,
    bodies: list[SessionEventInput],
    conversation_store: ConversationStore,
) -> list[str]:
    """
    Persist a run of external items with one store append.

    Matches :func:`_persist_external_conversation_item` per body for items
    that neither drain a pending input nor seed a title. As on the per-entry
    path, a malformed body raises only after the bodies before it are applied.

    :returns: Store-assigned item ids, in body order.
    """
    items: list[NewConversationItem] = []
    error: Exception | None = None
    for body in bodies:
        try:
            items.append(_new_external_conversation_item(session_id, body))
        except Exception as exc:  # noqa: BLE001 — re-raised once the valid prefix is applied
            error = exc
            break
    persisted_items = (
        await asyncio.to_thread(conversation_store.append, session_id, items) if items else []
    )
    for body, persisted in zip(bodies[: len(items)], persisted_items, strict=True):
        if not persisted.deduplicated:
            _publish_persisted_external_item(session_id, body, persisted)
    if error is not None:
        raise error
    return [persisted.id for persisted in persisted_items]


def _restore_drained_inputs(
    session_id: str,
    skipped: list[pending_inputs.DrainedInput],
    drained: pending_inputs.DrainedInput | None,
) -> None:
    """
    Put a mirror's drained pending entries back into play, in queue order.

    Compensation for a drain whose persist did not land (a deduplicated retry,
    or an append that raised), and the way a slash command hands back the
    older entries it only held. Held entries are unheld in place; one the TTL
    evicted meanwhile is re-inserted at the front, newest-first, so the
    original order survives either way.

    :param session_id: Conversation the entries belong to.
    :param skipped: Older entries skipped by the text match, oldest first.
    :param drained: The matched (or FIFO-drained) entry, or ``None``.
    """
    for entry in reversed([*skipped, drained]):
        if entry is not None:
            pending_inputs.restore(session_id, entry)


def _release_drained_inputs(
    session_id: str, entries: list[pending_inputs.DrainedInput | None]
) -> None:
    """
    Drop a mirror's drained pending entries once their persist landed.

    :param session_id: Conversation the entries belong to.
    :param entries: The matched entry and any skipped ones; ``None`` is skipped.
    """
    for entry in entries:
        if entry is not None:
            pending_inputs.release(session_id, entry)


def _build_skipped_items(
    session_id: str,
    skipped_entries: list[pending_inputs.DrainedInput],
) -> list[NewConversationItem]:
    """
    Build ``NewConversationItem`` pairs for web inputs the native TUI never recorded.

    Each skipped entry produces ``[user_message, error_item]``: the message the
    person sent, followed by an error saying the harness never recorded it, so
    the transcript keeps the lost message instead of silently dropping it.
    Stable IDs derived from ``pending_id`` make each pair idempotent under the
    batch append so no pre-flight has_item probe is needed — if the anchor item
    is already persisted (a forwarder retry), these items are too, and append
    returns the whole batch deduplicated.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param skipped_entries: Older queue entries skipped by a text-matched drain.
    :returns: The item pairs in queue order, empty when nothing was skipped.
    """
    if not skipped_entries:
        return []
    message = (
        "The agent never recorded this message in its transcript before "
        "accepting a later one, so it was not delivered."
    )
    items: list[NewConversationItem] = []
    for skipped in skipped_entries:
        turn_id = generate_task_id()
        items.append(
            NewConversationItem(
                type="message",
                response_id=turn_id,
                data=MessageData(role="user", content=skipped.content),
                created_by=skipped.created_by,
                stable_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"omnigent-skipped-native-user:{session_id}:{skipped.pending_id}",
                ).hex,
            )
        )
        items.append(
            NewConversationItem(
                type="error",
                response_id=turn_id,
                data=ErrorData(
                    source="execution", code="native_prompt_not_recorded", message=message
                ),
                stable_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"omnigent-skipped-native-error:{session_id}:{skipped.pending_id}",
                ).hex,
            )
        )
    return items


async def _enrich_terminal_status_with_subagent_output(
    data: dict[str, Any],
    status: str,
    session_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Attach a native session's durable assistant text to a terminal status edge.

    Shared by both native delivery paths (the codex ``external_session_status``
    POST handler and the claude-native relay forward) so the parent inbox
    result carries the child's output. Native harnesses mirror transcript items
    to the store, not runner memory, so the text is read here and forwarded
    with the terminal edge.

    Failed or explicitly cancelled turns are filled only when the forwarder
    attached no output of its own. A harness-reported ``failure_detail`` wins
    over the store for failures; fallback text must belong to the current
    response or follow the latest user message when no response id is present.
    A turn stopped before any output must not borrow an earlier turn's reply.

    :param data: The ``external_session_status`` ``data`` to enrich, e.g.
        ``{"status": "idle"}``.
    :param status: Status edge; only the terminal ``"idle"`` / ``"failed"``
        edges are enriched.
    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conversation_store: Store read for the child's assistant text.
    :returns: ``data`` with ``"output"`` added when a terminal edge has a
        persisted assistant message; otherwise unchanged.
    """
    if status not in ("idle", "failed"):
        return data
    current_turn_only = status == "failed" or data.get("turn_outcome") == "cancelled"
    existing = data.get("output")
    if current_turn_only and isinstance(existing, str) and existing.strip():
        return data
    # The store's latest assistant text can be prose that preceded the error.
    failure_detail = data.get("failure_detail") if status == "failed" else None
    if isinstance(failure_detail, str) and failure_detail.strip():
        return {**data, "output": failure_detail.strip()}
    raw_response_id = data.get("response_id") if current_turn_only else None
    response_id = raw_response_id if isinstance(raw_response_id, str) and raw_response_id else None
    output = await asyncio.to_thread(
        _latest_assistant_text_from_store,
        conversation_store,
        session_id,
        response_id=response_id,
        stop_at_user_message=current_turn_only,
    )
    if output is None:
        return data
    return {**data, "output": output}


async def _heal_subagent_runner_binding_via_parent(
    child_conv: Conversation,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    conversation_store: ConversationStore,
) -> httpx.AsyncClient | None:
    """
    Repair a sub-agent's stale ``runner_id`` and return its parent's live client.

    A sub-agent child copies its parent's ``runner_id`` once at creation and is
    never repointed when the parent's runner is later relaunched.  This walks up
    the ancestor chain (immediate parent → root) to find the nearest live runner,
    heals the child's DB row to point at that runner, and returns the live
    ``httpx.AsyncClient`` so the caller can proceed as if the binding were always
    current.

    After healing, the caller must re-read the conversation row (the healed
    ``runner_id`` is now in the DB).  Whether session re-initialization is
    needed depends on the harness:

    - **Native-terminal harnesses** (``pi-native``, ``claude-native``): the
      replacement runner does not automatically spawn the child's terminal
      process — the caller must set ``_runner_needs_session_init = True`` so
      ``_ensure_runner_session_initialized`` creates it.
    - **SDK / non-native harnesses**: all active sub-agent sessions are loaded
      in-process by the runner on startup; the parent's live runner already
      holds the child's session state, so ``_runner_needs_session_init``
      should remain ``False`` (calling ``_ensure_runner_session_initialized``
      would be a no-op at best and a spurious timeout at worst).

    :param child_conv: The sub-agent child whose ``runner_id`` may be stale.
    :param runner_router: Router used to resolve runner clients, or ``None`` in
        in-process setups.
    :param tunnel_registry: Runner-tunnel registry used to await the ancestor
        runner's (re)connect, or ``None`` in setups without runner tunnels.
    :param conversation_store: Store used to look up ancestors and persist the
        healed ``runner_id``.
    :returns: The live ``httpx.AsyncClient`` for the nearest live ancestor runner
        after healing, or ``None`` when no live ancestor could be found.
    """
    # Walk the ancestor chain (immediate parent first, then root) to find a
    # live runner.  A single hop covers the common case; two hops cover nested
    # sub-agents where the immediate parent's runner is also stale but the root
    # is still healthy.
    candidate_ids: list[str] = []
    if child_conv.parent_conversation_id and child_conv.parent_conversation_id != child_conv.id:
        candidate_ids.append(child_conv.parent_conversation_id)
    if (
        child_conv.root_conversation_id
        and child_conv.root_conversation_id != child_conv.id
        and child_conv.root_conversation_id not in candidate_ids
    ):
        candidate_ids.append(child_conv.root_conversation_id)
    if not candidate_ids:
        return None

    live_runner_id: str | None = None
    live_client: httpx.AsyncClient | None = None
    for ancestor_id in candidate_ids:
        ancestor = await asyncio.to_thread(conversation_store.get_conversation, ancestor_id)
        if ancestor is None or ancestor.runner_id is None:
            continue
        ancestor_runner_id = ancestor.runner_id
        # Wait briefly for the ancestor's tunnel — covers the reconnect gap
        # right after a relaunch.  When no registry is wired (in-process /
        # tests) fall back to a best-effort direct resolve.
        if tunnel_registry is not None:
            client = await _wait_for_runner_client(
                ancestor_id,
                runner_router,
                tunnel_registry,
                runner_id=ancestor_runner_id,
                timeout_s=_SUBAGENT_FORWARD_RECONNECT_WAIT_S,
            )
        else:
            client = await _get_runner_client(ancestor_id, runner_router)
        if client is not None:
            live_runner_id = ancestor_runner_id
            live_client = client
            break

    if live_runner_id is None or live_client is None:
        return None

    if live_runner_id != child_conv.runner_id:
        # Heal the divergence so this child's row matches the live runner: the
        # next forward resolves directly and a future ``_on_runner_connect``
        # (which rebinds by matching runner_id) can recover it.
        try:
            await asyncio.to_thread(
                conversation_store.replace_runner_id, child_conv.id, live_runner_id
            )
        except ConversationNotFoundError:
            # The child was deleted between reading it and this heal (e.g.
            # removed mid-teardown).  Degrade gracefully rather than surfacing
            # a benign race as an unhandled 500.
            return None

    return live_client


async def _recover_subagent_status_forward_via_parent(
    child_conv: Conversation,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    conversation_store: ConversationStore,
    forward_body: dict[str, Any],
) -> _RunnerForwardResult | None:
    """
    Re-deliver a sub-agent terminal status through the parent's live runner.

    A native sub-agent child copies its parent's ``runner_id`` once, at
    creation (``create_conversation(..., runner_id=parent_conv.runner_id)`` —
    see :func:`_persist_external_subagent_start`). It is never repointed when
    the runner is later relaunched under a freshly minted ``runner_id`` (a host
    relaunch after a tunnel drop / server redeploy / crash mints a new binding
    token; only the *parent* conversation is rebound, via the PATCH path on its
    next message). The child then points at a permanently offline ``runner_id``,
    so its terminal ``idle``/``failed`` forward resolves no runner client and
    503s forever (``_forward_session_change_to_runner`` → ``None`` →
    :func:`_require_external_status_forward`). The parent never receives the
    child's inbox result and hangs with no timeout.

    A child always runs on its parent's runner, so the live binding is the
    parent's. This re-resolves the forward through the parent/root
    conversation's *current* ``runner_id``: it waits briefly for that runner's
    tunnel to (re)connect (covering the reconnect gap right after a relaunch),
    heals the child's stale ``runner_id`` so future forwards and
    ``_on_runner_connect`` resolve it correctly, and retries the forward.

    :param child_conv: The sub-agent child conversation whose terminal-status
        forward could not reach its pinned runner.
    :param runner_router: Router used to resolve the bound runner client, or
        ``None`` in in-process setups.
    :param tunnel_registry: Runner-tunnel registry used to await the parent
        runner's (re)connect, or ``None`` in setups without runner tunnels.
    :param conversation_store: Store used to look up the parent and persist the
        child's healed ``runner_id``.
    :param forward_body: The ``external_session_status`` event body to re-POST.
    :returns: The retry's :class:`_RunnerForwardResult` when a live parent
        runner was resolved, or ``None`` when none could be (the caller then
        fails the forward as before).
    """
    client = await _heal_subagent_runner_binding_via_parent(
        child_conv, runner_router, tunnel_registry, conversation_store
    )
    if client is None:
        return None
    return await _forward_session_change_to_runner(
        child_conv.id,
        runner_router,
        forward_body,
    )


def _drive_terminal_resolved_elicitation(session_id: str, persisted: ConversationItem) -> None:
    """
    Feed a mirrored tool item into the terminal-resolved fast path.

    A ``function_call`` records its tool identity by ``call_id`` so the
    matching ``function_call_output`` can be correlated back to a parked
    permission prompt. A ``function_call_output`` means the gated tool
    already ran (or was rejected) in the native terminal, so the prompt
    the web UI may still be showing was resolved there — resolve the
    matching parked prompt now instead of waiting for the hook timeout.
    Other item types are ignored.

    :param session_id: Omnigent conversation id the item was mirrored for,
        e.g. ``"conv_abc123"``.
    :param persisted: The stored conversation item the forwarder just
        mirrored via ``external_conversation_item``.
    """
    data = persisted.data
    if persisted.type == "function_call" and isinstance(data, FunctionCallData):
        try:
            parsed = json.loads(data.arguments) if data.arguments else {}
        except json.JSONDecodeError:
            parsed = {}
        _recent_mirrored_tool_calls[data.call_id] = _MirroredToolCall(
            tool_name=data.name,
            tool_input=parsed if isinstance(parsed, dict) else {},
        )
    elif persisted.type == "function_call_output" and isinstance(data, FunctionCallOutputData):
        identity = _recent_mirrored_tool_calls.get(data.call_id)
        if identity is not None:
            _signal_terminal_resolved_harness_elicitation(
                session_id, identity.tool_name, identity.tool_input
            )


async def _publish_runner_recovered_status(*args: Any, **kwargs: Any) -> None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._publish_runner_recovered_status(*args, **kwargs)


async def _publish_runner_recovered_status_impl(
    session_id: str,
    conversation_store: ConversationStore,
    *,
    require_disconnect_code: bool = False,
) -> None:
    """
    Clear a stale failed session status after runner recovery.

    Native terminal startup failures are sticky against trailing
    ``idle`` PTY-quiescence signals so users can see the error. A
    later runner bind/session-init success is different: it proves AP
    reached a live runner for this session again, so the old failure is
    stale and should not keep the conversation marked failed until the
    next user turn emits ``running``.

    Recovery also clears the durable ``last_task_error`` labels the
    disconnect relay persisted. Those labels survive reload so an
    ongoing disconnect still projects a "Disconnected" pill, but once
    the runner is reachable again the session is healthy and idle — the
    pill must drop without waiting for the next ``running`` edge.

    An explicit rebind/handshake (a PATCH ``/clear`` or ``/switch``, or
    the message-forward session-init) is a user-driven proof the runner
    is live, so it clears any stale ``failed`` state. A *passive* tunnel
    reconnect is weaker: the process merely came back on its own, saying
    nothing about a genuine task error. Callers on that path pass
    ``require_disconnect_code=True`` so only a ``runner_disconnected``
    failure is cleared — a genuine task failure (``response.failed`` / a
    setup error with any other ``last_task_error`` code) survives the
    reconnect, keeping the red "Failed" pill instead of silently flipping
    it back to idle and hiding the error.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conversation_store: Store used to read the persisted error
        code and clear the labels on genuine recovery.
    :param require_disconnect_code: When ``True`` (passive-reconnect
        caller), only clear if the persisted ``last_task_error.code`` is
        ``runner_disconnected``; when ``False`` (default, explicit
        rebind/handshake), clear any stale ``failed`` state. Labels are
        cleared in both cases.
    :returns: None.
    """
    if _session_status_cache.get(session_id) != "failed":
        return
    # A passive reconnect must distinguish a benign runner disconnect
    # from a real task failure: both land the cache on "failed", but only
    # the disconnect persists a ``runner_disconnected`` label. The
    # reconnect proves the runner is reachable again, which invalidates a
    # disconnect failure but says nothing about a genuine task error —
    # leave that one alone. Explicit rebinds skip this guard.
    if require_disconnect_code:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        last_error = _last_task_error_from_labels(conv.labels) if conv is not None else None
        if last_error is None or last_error.get("code") != "runner_disconnected":
            return
    _session_status_cache[session_id] = "idle"
    session_live_state.persist_live_status(session_id, "idle")
    event = SessionStatusEvent(
        type="session.status",
        conversation_id=session_id,
        status="idle",
        error=None,
    )
    session_stream.publish(session_id, event.model_dump())
    await _persist_session_status_error_labels(session_id, None, conversation_store)


async def _drop_runner_owned_prompts(
    session_id: str, conversation_store: ConversationStore
) -> None:
    """
    Resolve, as unanswered, every prompt the session itself parked (not mirrored child prompts).

    :param session_id: Session whose runner went away, e.g. ``"conv_abc123"``.
    :param conversation_store: Store used to mirror the resolution into ancestor streams.
    """
    for event in pending_elicitations.snapshot_for(session_id):
        params = event.get("params")
        if isinstance(params, dict) and params.get("target_session_id"):
            continue
        elicitation_id = event.get("elicitation_id")
        if not isinstance(elicitation_id, str) or not elicitation_id:
            continue
        _pending_policy_ask_writes.pop(elicitation_id, None)
        _publish_elicitation_resolved(session_id, elicitation_id, reason="unanswered")
        await asyncio.to_thread(
            _publish_elicitation_resolved_to_ancestors,
            conversation_store,
            session_id,
            elicitation_id,
            None,
            "unanswered",
        )


async def _mark_runner_sessions_offline_impl(
    convs: list[Conversation],
    error: ErrorDetail,
    conversation_store: ConversationStore,
    *,
    fail_idle_top_level: bool = False,
) -> None:
    """
    Fail the sessions a departed runner actually interrupted, with the cause.

    A runner going away ends in-flight turns; it does not retroactively
    fail work that already finished. Sub-agents ride their parent's runner,
    so the departure reaches every child bound to it — marking the idle
    ones ``failed`` painted a whole Agents rail red for sub-agents that had
    completed successfully. The runner's absence is already carried by
    liveness (``clear_runner_liveness``), which is what drives the
    reconnect affordances, so a session that was not mid-turn needs no
    status edge at all.

    The sessions that DO get failed carry the cause, persisted as durable
    labels. The cause is what lets the client render a benign
    "Disconnected" instead of a red "Failed", and what lets
    :func:`_publish_runner_recovered_status` clear the state when the
    runner comes back — that helper only clears a failure it can identify
    as a disconnect.

    :param convs: Conversations bound to the departed runner, from
        :meth:`ConversationStore.list_conversations_by_runner_id`.
    :param error: The cause to publish and persist, e.g.
        ``ErrorDetail(code="runner_disconnected", message="...")``.
    :param conversation_store: Store used to persist the error labels.
    :param fail_idle_top_level: When ``True``, also fail an idle top-level
        session. A crash report covers the runner that died *before* it
        could run anything, so its session never reached ``running`` and
        would otherwise sit silent; a sub-agent is spawned by an
        already-live runner and can never be in that state, so idle
        children are skipped either way.
    :returns: None.
    """
    for conv in convs:
        # The runner's approval parks died with it: drop their prompts so no one is
        # asked a question whose answer could never reach a turn.
        await _drop_runner_owned_prompts(conv.id, conversation_store)
        # An intentional teardown (Stop / archive) drops the tunnel on
        # purpose. The relay owns that path — it publishes a quiet idle and
        # consumes the marker — so peek without discarding here.
        if conv.id in _intentional_stop_sessions:
            continue
        # Cache first (this replica holds the runner's tunnel, so it saw the
        # turn edges), falling back to the row for a session whose live state
        # was published before a restart.
        live = _session_status_cache.get(conv.id, conv.live_status)
        interrupted = live in _MID_TURN_STATUSES
        dead_on_arrival = fail_idle_top_level and conv.kind != "sub_agent"
        if not interrupted and not dead_on_arrival:
            continue
        _publish_status(conv.id, "failed", error, failure_origin="runner_offline_sweep")
        await _persist_session_status_error_labels(conv.id, error, conversation_store)


async def _wait_for_host_bound_runner_client(
    session_id: str,
    runner_router: RunnerRouter | None,
    tunnel_registry: TunnelRegistry | None,
    *,
    runner_id: str,
    timeout_s: float,
    runner_exit_reports: RunnerExitReports | None,
    host_conn: HostConnection,
    host_registry: HostRegistry,
) -> httpx.AsyncClient | None:
    """
    Wait for a host-bound runner to connect, ending early if the host
    reports it already gone.

    Races the connect grace (:func:`_wait_for_runner_client`) against a
    one-shot ``host.runner_status`` query, because they answer different
    questions and either can settle the outcome first:

    * The runner connecting — or a crash report — resolves the wait exactly
      as :func:`_wait_for_runner_client` does. This is ground truth and
      always wins when it lands first.
    * Concurrently, the host — the authoritative owner of runner-process
      liveness — may report the runner ``dead`` or ``unknown`` (stopped,
      crashed, or lost to a host restart). That means it will never
      connect, so the wait ends immediately and the caller relaunches
      without burning the rest of the grace.

    Running the query *alongside* the wait rather than before it is what
    keeps the query strictly a speed-up: a host that is too old to answer,
    slow, or silent (verdict ``None`` / ``"alive"``) never shortcuts the
    wait, so the connect grace runs its normal course with no added
    latency.

    :param session_id: Session/conversation identifier.
    :param runner_router: The ``RunnerRouter`` instance, or ``None``.
    :param tunnel_registry: The server's ``TunnelRegistry``, or ``None``.
    :param runner_id: Runner id expected to connect.
    :param timeout_s: Maximum seconds to wait for the connect.
    :param runner_exit_reports: Crash-report store consulted by the
        connect wait to abort early on a reported death.
    :param host_conn: Live host connection to query for liveness.
    :param host_registry: Registry used to enqueue the query frame.
    :returns: The runner HTTP client if it connected, otherwise ``None``
        (timed out, crash report, or host-confirmed dead/unknown).
    """
    connect_task = asyncio.ensure_future(
        _wait_for_runner_client(
            session_id,
            runner_router,
            tunnel_registry,
            runner_id=runner_id,
            timeout_s=timeout_s,
            runner_exit_reports=runner_exit_reports,
        )
    )
    status_task = asyncio.ensure_future(
        _query_host_runner_status(host_conn, host_registry, runner_id)
    )
    try:
        done, _pending = await asyncio.wait(
            {connect_task, status_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        # The connect settling is authoritative (client, timeout, or crash
        # report) — the host's opinion no longer matters once it lands.
        if connect_task in done:
            return connect_task.result()
        # Only the status query has resolved so far.
        if status_task.result() in ("dead", "unknown"):
            # Host confirms the runner will never connect — stop waiting.
            return None
        # No verdict ("alive" or an unavailable/too-old/slow host): let the
        # connect grace run to its natural conclusion.
        return await connect_task
    finally:
        outstanding = [t for t in (connect_task, status_task) if not t.done()]
        for task in outstanding:
            task.cancel()
        if outstanding:
            # Drain the cancelled task(s); return_exceptions swallows the
            # CancelledError so cleanup never masks the real return/raise.
            await asyncio.gather(*outstanding, return_exceptions=True)


async def _run_managed_launch(
    *,
    session_id: str,
    owner: str,
    sandbox_config: HostDeployment,
    repos: Sequence[RepoWorkspace],
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    host_to_relaunch: Host | None = None,
    provider: str | None = None,
    agent_store: AgentStore | None = None,
    agent_id: str | None = None,
) -> None:
    """
    Provision a managed sandbox for a session in the background.

    The ``host_type="managed"`` create returns before the sandbox
    exists; this task carries the rest of the pipeline: provision the
    sandbox + start the host (:func:`launch_host`), bind the
    host + workspace to the session row, launch a runner on the host,
    and wait for that runner's tunnel so a message POST rendezvousing
    on *tracker* can forward immediately once the launch settles.

    The same pipeline serves a sandbox RELAUNCH (*host_to_relaunch* set):
    a message arriving for a session whose managed sandbox died kicks
    this task with the existing host row, and
    :func:`relaunch_host` provisions a new sandbox generation
    under the same host identity instead of minting a new one.

    Every exit path settles the tracker entry — success via
    ``finish`` (the session then looks like any host-bound session),
    failure via ``fail`` with the reason a waiting message POST
    reports. A session deleted mid-provision is detected at the bind
    step and the fresh sandbox is torn down.

    Server shutdown cancels this task (the lifespan teardown calls
    :func:`cancel_managed_launch_tasks`); an already-provisioned
    sandbox then leaks until the provider's lifetime cap reaps it
    (the armed launch token expires with the same cap).

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on auth-disabled servers).
    :param sandbox_config: The deployment's sandbox config.
    :param repo: Parsed repository-URL workspace to clone inside the
        sandbox, or ``None`` for an empty workspace.
    :param tracker: The app's :class:`ManagedLaunchTracker`; this
        session's entry was registered by the caller.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    :param host_to_relaunch: Existing managed host row to relaunch a new
        sandbox generation for, or ``None`` for a first launch (a
        fresh host identity is minted).
    The runner's agent classifier is resolved here rather than by the
    caller, so the read runs on the task that already owns the
    single-flight claim: the request path keeps no ``await`` between
    claiming and spawning, and only the winning caller reaches this
    function, so a losing concurrent message pays nothing for it. It is
    re-derived on every launch through the built-in gate, so there is no
    stored classifier to keep in sync — or to forge.

    :param provider: Host provider the create chose, or ``None`` for
        the default. Ignored on a relaunch.
    :param agent_store: Store the classifier is resolved from, or
        ``None`` (a stripped test wiring) to leave the runner
        unclassified.
    :param agent_id: Agent the session is bound to, resolved through the
        built-in gate into the runner Pod's ``omnigent.ai/agent``
        classifier, or ``None`` to leave it unstamped.
    """
    from omnigent.server.hosts import (
        deployment_with_inference_snapshot,
        resolve_managed_agent_label,
    )

    saved_conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if saved_conversation is not None:
        sandbox_config = deployment_with_inference_snapshot(
            sandbox_config,
            saved_conversation.inference_snapshot,
        )
    # Forwarded to the launcher's prepare_for_launch — a provider whose sandbox is a
    # pre-existing per-identity resource (Superchat's per-Muse host; see
    # omnigent.hosts.docker) selects it from the session's OWN labels
    # (e.g. "omnigent.host.owner" / "omnigent.tenant") rather than a launch-chosen name.
    labels = saved_conversation.labels if saved_conversation is not None else {}

    agent_name: str | None = None
    if agent_store is not None and agent_id is not None:
        agent_name = await asyncio.to_thread(
            resolve_managed_agent_label,
            agent_store,
            agent_id,
            session_id=session_id,
        )
    with runner_log_scope(session_id, None):
        managed = await _provision_managed_sandbox(
            session_id=session_id,
            owner=owner,
            sandbox_config=sandbox_config,
            repos=repos,
            tracker=tracker,
            host_store=host_store,
            host_to_relaunch=host_to_relaunch,
            provider=provider,
            agent_name=agent_name,
            labels=labels,
        )
        if managed is None:
            return
        await _bind_and_launch_managed_runner(
            session_id=session_id,
            managed=managed,
            sandbox_config=sandbox_config,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=host_registry,
            tunnel_registry=tunnel_registry,
            host_to_relaunch=host_to_relaunch,
        )


#: :func:`_await_launched_host`'s answer for a host that connected to another replica.
_HOST_ON_ANOTHER_REPLICA: Final = "another-replica"

#: With several replicas, how often a launch waiting for its host checks whether the
#: host connected to another one instead.
_CROSS_REPLICA_ONLINE_CHECK_S = 3.0


async def _await_launched_host(
    host_id: str,
    host_registry: HostRegistry,
    host_store: HostStore,
) -> HostConnection | Literal["another-replica"] | None:
    """
    Wait for a just-launched host's connection, the only proof it is up.

    The hosts row flips online a moment before the connection registers here, so
    the row alone lets a launch report ready with no host to start its runner on.
    With one engine only this replica's connection counts. With several
    (:func:`multi_replica`), the host may have dialled another replica: that is
    reported, so the caller settles and the waiting request is re-addressed there.

    :param host_id: The launched host's identifier.
    :param host_registry: Live host connections on this replica.
    :param host_store: Persistent host registrations, read only with several replicas.
    :returns: The connection, :data:`_HOST_ON_ANOTHER_REPLICA`, or ``None`` when the
        host connected nowhere within ``_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S``.
    """
    from omnigent.server.routes import sessions as _facade

    timeout_s = _facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S
    if not multi_replica():
        return await host_registry.wait_for(host_id, timeout_s)
    deadline = time.monotonic() + timeout_s
    while (remaining := deadline - time.monotonic()) > 0:
        conn = await host_registry.wait_for(host_id, min(_CROSS_REPLICA_ONLINE_CHECK_S, remaining))
        if conn is not None:
            return conn
        if await asyncio.to_thread(host_store.is_online, host_id):
            return _HOST_ON_ANOTHER_REPLICA
    return None


async def _bind_and_launch_managed_runner(
    *,
    session_id: str,
    managed: HostLaunch,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    host_to_relaunch: Host | None = None,
) -> None:
    """
    Bind a provisioned managed host to its session and launch a runner.

    The bind step doubles as the delete-race detector: a session
    deleted while its sandbox provisioned surfaces here as
    ``ConversationNotFoundError``, and the fresh sandbox is torn down
    (the delete route could not see the host binding yet). Settles
    the tracker on every path.

    A launch failure after the host is online also tears the sandbox
    down — UNLESS *host_to_relaunch* is set: that means ``managed.host_id``
    is an existing, persistent host identity getting a new sandbox
    generation, not a fresh one minted for this launch, so a failed
    generation is left alone (same reasoning as a failed wake: the
    identity is the user's, not this attempt's to destroy) rather than
    torn down.

    :param session_id: Session/conversation identifier.
    :param managed: The provision result (host id + workspace).
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the
        launch-runner frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the
        launched runner's connection. ``None`` in minimal test
        wirings (the rendezvous then settles at frame-send).
    :param host_to_relaunch: Existing managed host row being relaunched, or
        ``None`` for a first launch (a fresh host identity was just
        minted for ``managed.host_id``).
    """
    from omnigent.server.hosts import terminate_host

    try:
        conv = await asyncio.to_thread(
            conversation_store.set_host_id,
            session_id,
            managed.host_id,
            managed.workspace,
        )
    except ConversationNotFoundError:
        # The session was deleted while its sandbox provisioned. The
        # delete route couldn't see the host binding yet, so tear the
        # fresh sandbox down here (deleting the host row also revokes
        # its launch token).
        _logger.info(
            "Session %s was deleted during managed provisioning; "
            "terminating fresh sandbox on host %s",
            session_id,
            managed.host_id,
            extra={"session_id": session_id},
        )
        host = await asyncio.to_thread(host_store.get_host, managed.host_id)
        if host is not None:
            await terminate_host(host, host_store, sandbox_config)
        tracker.fail(session_id, "session was deleted while its sandbox was provisioning")
        _publish_sandbox_status(
            session_id, "failed", "session was deleted while its sandbox was provisioning"
        )
        return
    if host_to_relaunch is not None and not _workspace_survives_recreate(
        host_to_relaunch.sandbox_provider
    ):
        await _note_workspace_reset_on_recreate(session_id, conversation_store)
    # Host bound; what remains is launching the runner and waiting
    # for its tunnel.
    _publish_sandbox_status(session_id, "connecting")
    runner_id: str | None = None
    if host_registry is not None:
        host_conn = await _await_launched_host(managed.host_id, host_registry, host_store)
        if host_conn is None:
            reason = "managed host did not connect after launch"
            if host_to_relaunch is None:
                host = await asyncio.to_thread(host_store.get_host, managed.host_id)
                if host is not None:
                    await terminate_host(host, host_store, sandbox_config)
            tracker.fail(session_id, reason)
            _publish_sandbox_status(session_id, "failed", reason)
            return
        if isinstance(host_conn, HostConnection):
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
            )
            if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                # The sandbox image should bake in the harness, but if the
                # host refuses, fail the launch loudly (mirroring the
                # delete-during-provisioning path) rather than waiting out
                # the connect timeout for a runner that will never appear.
                # A first launch's fresh Job that will never serve this
                # session shouldn't outlive the launch that provisioned it;
                # a relaunch generation on an existing host leaves the
                # identity alone, same as a failed wake.
                reason = launch_attempt.error or "harness not configured on the sandbox host"
                if host_to_relaunch is None:
                    host = await asyncio.to_thread(host_store.get_host, managed.host_id)
                    if host is not None:
                        await terminate_host(host, host_store, sandbox_config)
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            runner_id = launch_attempt.runner_id
    if runner_id is not None and tunnel_registry is not None:
        connected = await _wait_for_managed_runner_tunnel(
            session_id,
            runner_id,
            tunnel_registry,
            tracker,
        )
        if not connected:
            # The runner never connected its tunnel; _wait_for_managed_runner_tunnel
            # already settled the tracker and published the failure. Same
            # first-launch-vs-relaunch reasoning as the branch above.
            if host_to_relaunch is None:
                host = await asyncio.to_thread(host_store.get_host, managed.host_id)
                if host is not None:
                    await terminate_host(host, host_store, sandbox_config)
            return
    tracker.finish(session_id)
    _publish_sandbox_status(session_id, "ready")


async def _maybe_relaunch_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Relaunch a dead managed sandbox for a session, if it has one.

    Called from the message-dispatch relaunch path when the session's
    host tunnel is gone. For an external (laptop) host that is the end
    of the line, but a managed host's sandbox is RELAUNCHABLE: the
    host row is durable, so a new sandbox generation can be provisioned
    under the same host identity — "send a message to wake the
    sandbox", mirroring how a message relaunches a dead runner on a
    live host.

    Single-flighted through the app's :class:`ManagedLaunchTracker`:
    the first message kicks the background relaunch, concurrent and
    later messages rendezvous on the same entry (the check-then-begin
    below has no ``await`` between check and begin, so it is atomic on
    the event loop). A previously FAILED attempt's retained entry is
    replaced — every new message retries.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (``host_id`` set; caller guards).
    :param app_state: ``request.app.state`` — supplies the host store,
        sandbox config, tracker, and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a relaunch engaged and settled
        successfully (the session row is re-bound; re-resolve the
        runner client). ``False`` when the host is not a managed
        sandbox or managed hosts are not configured — the caller
        falls through to the normal unavailable handling.
    :raises OmnigentError: 503 when the relaunch failed or timed out.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    tracker = getattr(app_state, "managed_launches", None)
    if host_store is None or sandbox_config is None or tracker is None:
        return False
    if conv.host_id is None:
        # No sandbox ever came up for this session (its first launch failed). It is still a
        # managed session when its create recorded so: launch it now, like the create did.
        provider = conv.labels.get(MANAGED_PROVIDER_LABEL_KEY)
        owner = conv.labels.get(MANAGED_OWNER_LABEL_KEY)
        if not provider or not owner:
            return False
        launch = tracker.get(session_id)
        if launch is None or launch.settled.is_set():
            _kick_managed_first_launch(
                session_id=session_id,
                conv=conv,
                provider=provider,
                owner=owner,
                sandbox_config=sandbox_config,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                app_state=app_state,
            )
            launch = tracker.get(session_id)
        if launch is not None:
            await _await_settled_managed_launch(launch)
        return True
    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or host.sandbox_provider is None:
        return False
    host_registry = getattr(app_state, "host_registry", None)
    host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
    # With one engine the connection is the only proof of life: an "online" row with no
    # connection here outlived it (the caller already gave the host its reconnect window),
    # so the host is relaunched.
    if host_is_live(host) and (host_conn is not None or multi_replica()):
        if not (host_resume_supported(host, sandbox_config) and host_conn is None):
            # The host row still reads live (status online with a fresh
            # heartbeat). For non-resumable providers or a live local tunnel,
            # avoid replacing a healthy workspace and let normal unavailable
            # handling surface the transient. Resumable managed hosts are the
            # exception: an idle-paused VM can leave a fresh DB row while this
            # process has no usable tunnel, so the first post-idle message must
            # attempt a wake immediately.
            return False
    launch = tracker.get(session_id)
    if launch is None or launch.settled.is_set():
        # A resumable managed host whose sandbox merely idle-stopped is WOKEN
        # in place (resume: same sandbox + workspace volume) rather than
        # relaunched onto a fresh empty sandbox — same gate the wake itself
        # uses (host_resume_supported). Both run in the background through this
        # same tracker, so the message parks on the rendezvous either way; only
        # the provision step differs.
        if host_resume_supported(host, sandbox_config):
            _kick_managed_wake(
                session_id=session_id,
                conv=conv,
                sandbox_config=sandbox_config,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                app_state=app_state,
            )
        else:
            _kick_managed_relaunch(
                session_id=session_id,
                conv=conv,
                host=host,
                sandbox_config=sandbox_config,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                app_state=app_state,
            )
        launch = tracker.get(session_id)
    if launch is not None:
        await _await_settled_managed_launch(launch)
    # A wake settles SUCCESSFULLY even when the host came online on a DIFFERENT
    # replica — and this one then holds no tunnel to write ``host.launch_runner``
    # to, so letting the caller fall through would spend its whole runner budget
    # waiting for a launch that cannot happen here. Raise WRONG_REPLICA instead:
    # the client re-addresses without the routing key, lands on the replica that
    # owns the tunnel, and the message-dispatch relaunch path there finds it in
    # its LOCAL registry and launches the runner inline (``routes_events``, gated
    # only on host presence, rebinding a stale ``runner_id`` on the way) — so the
    # turn completes on the retry, through machinery that already exists.
    #
    # Safe to raise: the rendezvous resolves before the handler persists anything,
    # so the re-addressed request is not a double-send. Skipped when there is
    # nothing to re-address — no host binding, no registry wired (single replica,
    # nothing can misroute), the tunnel IS here, or the host is dead everywhere
    # rather than merely elsewhere.
    local_host_registry = getattr(app_state, "host_registry", None)
    if (
        multi_replica()
        and conv.host_id is not None
        and local_host_registry is not None
        and local_host_registry.get(conv.host_id) is None
        and await asyncio.to_thread(host_store.is_online, conv.host_id)
    ):
        raise OmnigentError(
            f"host {conv.host_id} is live on another replica after its managed "
            "launch; retry without the routing key",
            code=ErrorCode.WRONG_REPLICA,
        )
    return True


async def _maybe_wake_stale_resumable_managed_sandbox(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> bool:
    """
    Wake a resumable managed host whose persisted liveness has gone stale.

    A resumable sandbox's idle pause is memory-preserving: the local host/runner WebSocket
    objects can remain registered until their ping loops time out, even though
    the VM is already paused and cannot answer new requests. When the durable
    host-store liveness row is stale, trust it over those in-memory objects,
    drop the stale entries, and route through the normal managed wake path.

    :param session_id: Session/conversation identifier.
    :param conv: Current conversation row.
    :param app_state: ``request.app.state`` — supplies stores and registries.
    :param conversation_store: Store holding the session row.
    :returns: ``True`` when a managed wake ran and settled.
    """
    host_store = getattr(app_state, "host_store", None)
    sandbox_config = getattr(app_state, "sandbox_config", None)
    if host_store is None or sandbox_config is None or conv.host_id is None:
        return False

    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    if host is None or not host_resume_supported(host, sandbox_config):
        return False
    host_registry = cast(HostRegistry | None, getattr(app_state, "host_registry", None))
    tunnel_registry = cast(TunnelRegistry | None, getattr(app_state, "tunnel_registry", None))
    host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
    host_tunnel_stale = (
        host_conn is not None
        and time.time() - host_conn.last_frame_at >= _MANAGED_RESUMABLE_TUNNEL_STALE_S
    )
    runner_session = (
        tunnel_registry.get(conv.runner_id)
        if tunnel_registry is not None and conv.runner_id is not None
        else None
    )
    runner_tunnel_stale = False
    if runner_session is not None and tunnel_registry is not None:
        runner_idle_s = tunnel_registry.seconds_since_last_frame(runner_session)
        runner_tunnel_stale = (
            runner_idle_s is not None and runner_idle_s >= _MANAGED_RESUMABLE_TUNNEL_STALE_S
        )

    host_row_online = host_is_live(host)
    sandbox_running = await asyncio.to_thread(host_sandbox_is_running, host, sandbox_config)
    if (
        sandbox_running is not False
        and host_row_online
        and host_conn is not None
        and not host_tunnel_stale
        and not runner_tunnel_stale
    ):
        return False

    if host_registry is not None:
        host_registry.deregister(conv.host_id)
    if tunnel_registry is not None and conv.runner_id is not None:
        tunnel_registry.deregister(conv.runner_id)

    _logger.info(
        "Managed host %s for session %s needs wake before reusing tunnels "
        "(host_row_online=%s, sandbox_running=%s, host_tunnel_stale=%s, "
        "runner_tunnel_stale=%s)",
        conv.host_id,
        session_id,
        host_row_online,
        sandbox_running,
        host_tunnel_stale,
        runner_tunnel_stale,
        extra={"session_id": session_id},
    )
    return await _maybe_relaunch_managed_sandbox(
        session_id=session_id,
        conv=conv,
        app_state=app_state,
        conversation_store=conversation_store,
    )


async def ensure_runner_connected(
    *,
    session_id: str,
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    raise_host_refusal: bool = False,
    allow_relaunch: bool = True,
) -> tuple[httpx.AsyncClient | None, Conversation]:
    """
    Bring a wakeable session's runner online for out-of-band resource access.

    Mirrors the message-dispatch relaunch ladder in ``post_event`` — wake a
    stale resumable managed sandbox, launch a runner on a live host, or
    relaunch a dead managed sandbox — but without any of the message-specific
    side effects (failed-turn persistence, session-init handshake, sub-agent
    binding heal). It answers one question: "is a live runner reachable, and
    if not, can we wake one right now?"

    Used by resource routes (e.g. shell create) so a user acting on a session
    whose runner merely went to sleep transparently reconnects it, instead of
    dead-ending on a 502. Only the wakeable states recover here:

    * runner already connected → returned as-is (the common fast path);
    * host online, pinned runner still booting → wait the connect grace
      (``_HOST_BOUND_RUNNER_CONNECT_GRACE_S``, racing a ``host.runner_status``
      query) before treating it as dead, so a booting runner isn't orphaned
      by an eager relaunch;
    * host online, runner truly dead → ``_launch_runner_on_host`` + connect wait;
    * host tunnel gone but managed/resumable → ``_maybe_relaunch_managed_sandbox``
      / ``_maybe_wake_stale_resumable_managed_sandbox``.

    Non-wakeable states (external host offline, not host-bound with a dead
    runner) return ``(None, conv)`` — the caller keeps its existing
    unavailable handling (the CLI reconnect path owns those).

    :param session_id: Session/conversation identifier.
    :param conv: Current session row.
    :param app_state: ``request.app.state`` — supplies the registries,
        managed-launch tracker, host store, and sandbox config.
    :param conversation_store: Store holding the session row.
    :param runner_router: The ``RunnerRouter``, or ``None`` for in-process.
    :param raise_host_refusal: Raise typed, non-persisting errors when the
        host rejects launch because the harness is unavailable or the
        workspace is gone. Resource callers keep the legacy ``False``
        behavior; retry recovery opts in because it has no message to persist.
    :param allow_relaunch: ``False`` skips the managed-sandbox wake and
        relaunch (they provision), leaving only a runner launch on an online
        host; the intent wake passes it.
    :returns: ``(runner_client, conv)`` — the client is ``None`` when no
        runner is reachable and none is wakeable; ``conv`` is re-read after
        any wake/relaunch so the caller sees the rebound row.
    :raises OmnigentError: 503 when a managed relaunch/wake failed or timed
        out (propagated from ``_maybe_relaunch_managed_sandbox``).
    """

    from omnigent.server.routes import sessions as _facade

    async def _reread() -> Conversation:
        refreshed = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        return refreshed if refreshed is not None else conv

    # Fast path: a live runner tunnel is already reachable.
    runner_client = await _get_runner_client(
        session_id,
        runner_router,
        conversation=conv,
    )
    if runner_client is not None:
        return runner_client, conv

    tunnel_registry = cast(TunnelRegistry | None, getattr(app_state, "tunnel_registry", None))
    host_registry = cast(HostRegistry | None, getattr(app_state, "host_registry", None))

    # A resumable managed host whose persisted liveness has gone stale wakes
    # in place; re-read the row and re-resolve, since the wake may have
    # relaunched the runner and rebound the session.
    if (
        allow_relaunch
        and conv.host_id is not None
        and await _maybe_wake_stale_resumable_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=app_state,
            conversation_store=conversation_store,
        )
    ):
        conv = await _reread()
        runner_client = await _get_runner_client(
            session_id,
            runner_router,
            conversation=conv,
        )
        if runner_client is not None:
            return runner_client, conv

    # Host-bound but no runner: ask the still-online host to spawn one, or
    # relaunch a managed sandbox whose host tunnel is gone. Mirrors the
    # gating in post_event — host presence, then managed relaunch fallback.
    if runner_client is None and conv.host_id is not None:
        host_conn = host_registry.get(conv.host_id) if host_registry is not None else None
        relaunched_runner_id: str | None = None
        if host_conn is not None:
            # A session whose runner is merely booting already has a runner_id
            # before its tunnel registers. Mirror post_event's connect grace:
            # wait briefly for that pinned runner (racing a host.runner_status
            # query, which cuts the wait short if the host reports it truly
            # gone) before relaunching — otherwise we'd spawn a second runner
            # and orphan the one still coming up.
            if (
                conv.runner_id is not None
                and host_registry is not None
                and _facade._HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0
            ):
                runner_client = await _wait_for_host_bound_runner_client(
                    session_id,
                    runner_router,
                    tunnel_registry,
                    runner_id=conv.runner_id,
                    timeout_s=_facade._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    runner_exit_reports=getattr(app_state, "runner_exit_reports", None),
                    host_conn=host_conn,
                    host_registry=host_registry,
                )
                if runner_client is not None:
                    return runner_client, await _reread()
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
            )
            # A harness-not-configured or workspace-missing refusal means the
            # runner will never appear — don't set relaunched_runner_id or the
            # caller will wait out the full connect timeout for nothing.
            # Record the refusal message in runner_exit_reports so the
            # runner_failed_to_start error surfaces the actionable cause
            # rather than the generic "may have failed to start" fallback.
            _refusal_code = _classify_launch_refusal(
                launch_attempt.error_code,
                launch_attempt.error,
                conv.workspace,
            )
            if _refusal_code == _WORKSPACE_MISSING_ERROR_CODE:
                # Rebuild from the authorized session row: the host's own
                # text is untrusted and may carry log tails or secrets.
                # Harness refusals keep the host's text by design: it is a
                # deterministic setup hint, not runner output.
                _refusal_message = _workspace_missing_message(conv.workspace)
            else:
                _refusal_message = launch_attempt.error or ""
            if _refusal_code is not None and raise_host_refusal:
                raise OmnigentError(
                    _refusal_message
                    or (
                        "The session harness is not configured on this host."
                        if _refusal_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE
                        else "The session workspace no longer exists on the host."
                    ),
                    code=(
                        ErrorCode.HARNESS_NOT_CONFIGURED
                        if _refusal_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE
                        else ErrorCode.WORKSPACE_MISSING
                    ),
                )
            if _refusal_code is not None and _refusal_message:
                _rer = getattr(app_state, "runner_exit_reports", None)
                if _rer is not None:
                    _rer.record(
                        launch_attempt.runner_id,
                        _refusal_message,
                        owner=host_conn.owner,
                    )
            if _refusal_code is None:
                relaunched_runner_id = launch_attempt.runner_id
        elif allow_relaunch and await _maybe_relaunch_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=app_state,
            conversation_store=conversation_store,
        ):
            conv = await _reread()
            runner_client = await _get_runner_client(
                session_id,
                runner_router,
                conversation=conv,
            )

        if runner_client is None and (host_conn is not None or allow_relaunch):
            runner_client = await _wait_for_runner_client(
                session_id,
                runner_router,
                tunnel_registry,
                runner_id=relaunched_runner_id,
                timeout_s=_facade._HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                runner_exit_reports=getattr(app_state, "runner_exit_reports", None),
            )
            conv = await _reread()

    return runner_client, conv


_WAKE_INTERVAL_S = 60.0
_last_wake_at: dict[tuple[str, str], float] = {}


def wake_conversation_runner(
    *,
    conv: Conversation,
    user_id: str | None,
    app_state: Any,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    permission_store: PermissionStore | None,
) -> None:
    """
    Start the person's Assistant Conversation runner in the background.

    Called when the owner opens or focuses the app so the first word does not
    wait on a cold launch. Does nothing for a Helper, a non-owner, an eval
    person, or a Conversation woken in the last minute. Never relaunches a
    managed sandbox, and never raises into the caller.

    :param conv: The session the owner marked read.
    :param user_id: The caller.
    :param app_state: ``request.app.state``.
    :param conversation_store: Store holding the session row.
    :param runner_router: The ``RunnerRouter``, or ``None`` for in-process.
    :param permission_store: Store that names the session's owner.
    :returns: None.
    """
    if user_id is None or not is_super_chat(conv.labels) or is_eval_identity(user_id):
        return
    key = (conv.id, user_id)
    now = time.monotonic()
    if now - _last_wake_at.get(key, -_WAKE_INTERVAL_S) < _WAKE_INTERVAL_S:
        return
    _last_wake_at[key] = now

    async def _wake() -> None:
        try:
            owner = await asyncio.to_thread(get_session_owner_id, conv.id, permission_store)
            if owner != user_id:
                return
            await ensure_runner_connected(
                session_id=conv.id,
                conv=conv,
                app_state=app_state,
                conversation_store=conversation_store,
                runner_router=runner_router,
                allow_relaunch=False,
            )
        except Exception:  # noqa: BLE001 - a wake never fails the read
            _logger.warning("Intent wake failed for session %s", conv.id, exc_info=True)

    task = asyncio.create_task(_wake())
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)


_wake_tasks: set[asyncio.Task[None]] = set()


def _recorded_repo_workspaces(
    session_id: str, labels: dict[str, str], *, invalid_label_fallback: str
) -> list[RepoWorkspace]:
    """Parse the repositories recorded on a managed session label."""
    raw_workspaces = read_managed_repo_workspaces(labels)
    if not raw_workspaces:
        return []
    try:
        return [parse_repo_workspace(workspace) for workspace in raw_workspaces]
    except ValueError:
        _logger.warning(
            "Session %s has an unparseable sandbox repo label (%r); %s",
            session_id,
            raw_workspaces,
            invalid_label_fallback,
            extra={"session_id": session_id},
        )
        return []


_WORKSPACE_RESET_ERROR_CODE = "managed_sandbox_workspace_reset"
_WORKSPACE_RESET_NOTICE = (
    "The sandbox backing this session no longer exists. A fresh sandbox is being "
    "created. Local files that were not committed and pushed are gone. If the "
    "session started from a repository, that repository has been cloned again."
)


def _workspace_survives_recreate(provider: str | None) -> bool:
    """Whether sandboxes of *provider* keep a session's files across a recreate.

    :param provider: The provider recorded on the host being relaunched, e.g. ``"docker"``.
    """
    from omnigent.hosts.registry import get_provider_metadata

    metadata = get_provider_metadata(provider) if provider else None
    return metadata is not None and metadata.workspace_survives_recreate


async def _note_workspace_reset_on_recreate(
    session_id: str,
    conversation_store: ConversationStore,
) -> None:
    """Persist and publish the user-facing notice that the workspace reset."""
    response_id = f"turn_{uuid.uuid4().hex}"
    visible_item = NewConversationItem(
        type="error",
        response_id=response_id,
        data=ErrorData(
            source="execution",
            code=_WORKSPACE_RESET_ERROR_CODE,
            message=_WORKSPACE_RESET_NOTICE,
            level="info",
        ),
    )
    try:
        persisted = await asyncio.to_thread(
            conversation_store.append,
            session_id,
            [visible_item],
        )
    except Exception:  # noqa: BLE001
        _logger.warning(
            "Failed to persist workspace-reset notice for session %s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return
    if persisted:
        _publish_external_conversation_item(session_id, persisted[0])


#: Labels a managed session's create records, so a failed first launch can be retried.
MANAGED_PROVIDER_LABEL_KEY = "omnigent.sandbox.provider"
MANAGED_OWNER_LABEL_KEY = "omnigent.sandbox.owner"


def managed_first_launch_labels(provider: str | None, owner: str) -> dict[str, str]:
    """
    The labels that mark a session as launched on a managed sandbox.

    :param provider: The host provider the create launched on (``None``: the default).
    :param owner: The user the sandbox host registers under.
    :returns: Labels for :meth:`ConversationStore.set_labels`.
    """
    return {MANAGED_PROVIDER_LABEL_KEY: provider or "default", MANAGED_OWNER_LABEL_KEY: owner}


def _kick_managed_first_launch(
    *,
    session_id: str,
    conv: Conversation,
    provider: str,
    owner: str,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Launch a managed sandbox for a session whose first launch never succeeded.

    Same single-flight rules as :func:`_kick_managed_relaunch` (synchronous, no ``await``
    between the caller's check and the claim), but there is no host row yet: the launch
    registers one, exactly as the session create would have.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (supplies the repo labels and bound agent).
    :param provider: Provider recorded at create (``"default"``: the deployment default).
    :param owner: User recorded at create; the host registers under them.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state``.
    """
    repos = _recorded_repo_workspaces(
        session_id,
        conv.labels,
        invalid_label_fallback="launching with an empty workspace",
    )
    _logger.info(
        "Managed sandbox for session %s never came up; launching it again",
        session_id,
        extra={"session_id": session_id},
    )
    tracker.begin(session_id)
    _publish_sandbox_status(session_id, "provisioning")
    launch_task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=owner,
            sandbox_config=sandbox_config,
            repos=repos,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
            provider=None if provider == "default" else provider,
            agent_store=getattr(app_state, "agent_store", None),
            agent_id=conv.agent_id,
        )
    )
    _managed_launch_tasks.add(launch_task)
    launch_task.add_done_callback(_managed_launch_tasks.discard)


def _kick_managed_relaunch(
    *,
    session_id: str,
    conv: Conversation,
    host: Host,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background relaunch for a dead sandbox.

    Recovers the session's create-time repository workspace from its label so
    the fresh generation re-clones it, claims the single-flight tracker entry,
    and schedules :func:`_run_managed_launch` with the existing host row. The
    new Pod re-hits admission, so its ``omnigent.ai/agent`` classifier is
    re-stamped — resolved by the launch task through the same built-in gate the
    initial launch uses, never read from a stored label.

    Stays synchronous so no ``await`` falls between the caller's
    ``tracker.get`` check and the ``tracker.begin`` claim, and none falls
    between that claim and the task that settles it.

    :param session_id: Session/conversation identifier.
    :param conv: The session row (supplies the repo label and bound agent).
    :param host: The dead managed host row to relaunch.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries and the
        agent store the classifier is re-derived from.
    """
    repos = _recorded_repo_workspaces(
        session_id,
        conv.labels,
        invalid_label_fallback="relaunching with an empty workspace",
    )
    _logger.info(
        "Managed sandbox for session %s (host %s) is gone; relaunching a new generation",
        session_id,
        conv.host_id,
        extra={"session_id": session_id},
    )
    tracker.begin(session_id)
    # Seed the relaunch's progress indicator immediately — the user is
    # typically watching the session page when "wake the sandbox" runs.
    _publish_sandbox_status(session_id, "provisioning")
    agent_store = getattr(app_state, "agent_store", None)
    if agent_store is None:
        _logger.warning(
            "session %s: relaunch has no agent store; runner stays unclassified",
            session_id,
            extra={"session_id": session_id},
        )
    relaunch_task = asyncio.create_task(
        _run_managed_launch(
            session_id=session_id,
            owner=host.user_id,
            sandbox_config=sandbox_config,
            repos=repos,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
            host_to_relaunch=host,
            agent_store=agent_store,
            agent_id=conv.agent_id,
        )
    )
    _managed_launch_tasks.add(relaunch_task)
    relaunch_task.add_done_callback(_managed_launch_tasks.discard)


def _kick_managed_wake(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Spawn the background wake for a dormant resumable host.

    Call-time proxy to the facade so a test's ``monkeypatch.setattr`` of this
    name on ``sessions`` is honored by sibling impl callers.
    """
    from omnigent.server.routes import sessions as _facade

    return _facade._kick_managed_wake(
        session_id=session_id,
        conv=conv,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        app_state=app_state,
    )


def _kick_managed_wake_impl(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    app_state: Any,
) -> None:
    """
    Register and spawn the background WAKE for a dormant resumable host.

    Unlike :func:`_kick_managed_relaunch` (which provisions a NEW sandbox and
    re-clones the repo), this resumes the SAME stopped sandbox in place
    (reattaching its persistent volume) — so it does NOT re-bind the session's
    host/workspace. Reuses the launch tracker so a racing message POST parks on
    the rendezvous instead of forwarding into a half-woken host or triggering a
    workspace-destroying relaunch.

    :param session_id: Session/conversation identifier.
    :param conv: The session row bound to the dormant host.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker.
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param app_state: ``request.app.state`` — supplies the registries.
    """
    _logger.info(
        "Managed host %s (session %s) is dormant but resumable; waking in background",
        conv.host_id,
        session_id,
        extra={"session_id": session_id},
    )
    tracker.begin(session_id)
    # Seed the progress indicator immediately — the user is watching the
    # session page when the wake fires (the composer let them send into a
    # host_asleep session).
    _publish_sandbox_status(session_id, "provisioning")
    wake_agent_store = getattr(app_state, "agent_store", None)
    if wake_agent_store is None:
        _logger.warning(
            "session %s: wake has no agent store; woken runner stays unclassified",
            session_id,
            extra={"session_id": session_id},
        )
    wake_task = asyncio.create_task(
        _run_managed_wake(
            session_id=session_id,
            conv=conv,
            sandbox_config=sandbox_config,
            tracker=tracker,
            conversation_store=conversation_store,
            host_store=host_store,
            host_registry=getattr(app_state, "host_registry", None),
            tunnel_registry=getattr(app_state, "tunnel_registry", None),
            agent_store=wake_agent_store,
            agent_id=conv.agent_id,
        )
    )
    _managed_launch_tasks.add(wake_task)
    wake_task.add_done_callback(_managed_launch_tasks.discard)


async def _run_managed_wake(
    *,
    session_id: str,
    conv: Conversation,
    sandbox_config: HostDeployment,
    tracker: ManagedLaunchTracker,
    conversation_store: ConversationStore,
    host_store: HostStore,
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    agent_store: AgentStore | None = None,
    agent_id: str | None = None,
) -> None:
    """
    Wake a dormant resumable managed host in the background, settling the
    tracker so a parked message POST forwards once the host is back.

    Resumes the stopped sandbox in place (:func:`resume_host`: resume +
    re-arm token + re-exec host, preserving the workspace volume — no re-bind),
    then launches a runner on the woken host and waits for its tunnel so a
    rendezvoused message resolves on the first try. The parked send runs the
    session-init handshake (transcript forwarder attach) before forwarding, so
    the first post-wake turn is mirrored + persisted.

    Mirrors :func:`_bind_and_launch_managed_runner` (launch runner + wait
    tunnel + settle) but with a resume instead of a fresh provision + bind.
    Every exit settles the tracker — a failed wake does NOT tear the sandbox
    down (the volume is the user's), it just surfaces the reason to the waiter.

    :param session_id: Session/conversation identifier.
    :param conv: The session row bound to the dormant host.
    :param sandbox_config: The deployment's sandbox config.
    :param tracker: The app's launch tracker (this session's entry was begun
        by the caller).
    :param conversation_store: Store holding the session row.
    :param host_store: Persistent host registrations.
    :param host_registry: Live host tunnels, used to send the launch-runner
        frame. ``None`` in minimal test wirings.
    :param tunnel_registry: Runner-tunnel registry used to await the launched
        runner's connection. ``None`` in minimal test wirings.
    :param agent_store: Store the runner's agent classifier is resolved from,
        or ``None`` (a stripped test wiring) to leave the runner unclassified.
    :param agent_id: Agent the session is bound to, resolved through the
        built-in gate into the woken runner Pod's ``omnigent.ai/agent``
        classifier, or ``None`` to leave it unstamped.
    """
    from omnigent.hosts.launcher import HostGoneError
    from omnigent.server.hosts import (
        deployment_with_inference_snapshot,
        resolve_managed_agent_label,
        resume_host,
    )

    sandbox_config = deployment_with_inference_snapshot(sandbox_config, conv.inference_snapshot)
    host_id = conv.host_id
    if host_id is None:
        reason = "managed session has no host binding"
        tracker.fail(session_id, reason)
        _publish_sandbox_status(session_id, "failed", reason)
        return

    def _on_stage(stage: str) -> None:
        """Relay the wake's launch-pipeline stage to the session's progress surface.

        Without it the wake shows the single ``"provisioning"`` band that
        ``_kick_managed_wake`` seeded for its whole duration, even while the host
        is already re-execing. resume_host may invoke this from the
        worker thread its ``start_host`` runs on; ``_publish_sandbox_status`` is
        thread-safe.
        """
        _publish_sandbox_status(session_id, stage)

    try:
        # Wake the same sandbox in place; resume_host is single-flight
        # per host and a no-op if it's already online.
        # Re-derived here rather than by the caller, matching the launch path:
        # the read runs on the task that already owns the single-flight claim,
        # and it is never read back from a stored label — so there is nothing to
        # keep in sync, or to forge.
        agent_name: str | None = None
        if agent_store is not None and agent_id is not None:
            agent_name = await asyncio.to_thread(
                resolve_managed_agent_label,
                agent_store,
                agent_id,
                session_id=session_id,
            )
        repos = _recorded_repo_workspaces(
            session_id,
            conv.labels,
            invalid_label_fallback="waking without a repository",
        )
        try:
            await resume_host(
                host_id,
                host_store,
                sandbox_config,
                force=True,
                on_stage=_on_stage,
                agent_name=agent_name,
            )
        except HostGoneError:
            _logger.info(
                "Managed host %s (session %s) sandbox is gone; recreating a fresh generation",
                host_id,
                session_id,
                extra={"session_id": session_id},
            )
            host = await asyncio.to_thread(host_store.get_host, host_id)
            if host is None:
                reason = "managed host not found after sandbox loss"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            await _run_managed_launch(
                session_id=session_id,
                owner=host.user_id,
                sandbox_config=sandbox_config,
                repos=repos,
                tracker=tracker,
                conversation_store=conversation_store,
                host_store=host_store,
                host_registry=host_registry,
                tunnel_registry=tunnel_registry,
                host_to_relaunch=host,
                agent_store=agent_store,
                agent_id=agent_id,
            )
            return
        _publish_sandbox_status(session_id, "connecting")
        refreshed = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if refreshed is None:
            tracker.fail(session_id, "session not found after wake")
            return
        runner_id: str | None = None
        host_conn: HostConnection | None = None
        if host_registry is not None:
            # resume_host waits on the hosts row, which reads online a moment before
            # the woken host's connection registers (or registers on another replica).
            # Launch the runner once the connection is here, instead of settling
            # "ready" with no runner.
            awaited = await _await_launched_host(host_id, host_registry, host_store)
            if awaited == _HOST_ON_ANOTHER_REPLICA:
                # The wake SUCCEEDED, just not here. Publishing a terminal "failed"
                # would be wrong AND outlive the recovery — sandbox_status is cached
                # PER POD and only "ready" evicts it. The runner launches from the
                # replica that owns the tunnel: the request parked on this rendezvous
                # is sent there by the WRONG_REPLICA raised in
                # _maybe_relaunch_managed_sandbox.
                tracker.finish(session_id)
                _publish_sandbox_status(session_id, "ready")
                return
            if awaited is None:
                tracker.fail(session_id, "managed host did not reconnect after wake")
                _publish_sandbox_status(
                    session_id, "failed", "managed host did not reconnect after wake"
                )
                return
            host_conn = awaited
        if host_conn is not None:
            launch_attempt = await _launch_runner_on_host(
                refreshed,
                conversation_store,
                host_registry,
                host_conn,
            )
            if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                reason = launch_attempt.error or "harness not configured on the sandbox host"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
            runner_id = launch_attempt.runner_id
        if runner_id is not None and tunnel_registry is not None:
            connected = await _wait_for_managed_runner_tunnel(
                session_id,
                runner_id,
                tunnel_registry,
                tracker,
            )
            if not connected:
                return
        tracker.finish(session_id)
        _publish_sandbox_status(session_id, "ready")
    except HTTPException as exc:
        tracker.fail(session_id, str(exc.detail))
        _publish_sandbox_status(session_id, "failed", str(exc.detail))
    except Exception:  # noqa: BLE001
        # Fire-and-forget task — settle the tracker (else a waiting message
        # POST hangs to its timeout) and never escape as an unhandled-task
        # traceback. A failed wake leaves the sandbox intact for a retry.
        _logger.exception(
            "Managed host wake crashed for session %s",
            session_id,
            extra={"session_id": session_id},
        )
        tracker.fail(session_id, "internal error during managed host wake")
        _publish_sandbox_status(session_id, "failed", "internal error during managed host wake")


async def _ensure_runner_session_initialized(
    session_id: str,
    conv: Conversation,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    initializer: RunnerSessionInitializer | None = None,
    *,
    suppress_recovery_turn: bool = False,
    require_success: bool = False,
) -> bool:
    """
    Drive — and wait for — the runner's session-init handshake.

    Posts ``POST /v1/sessions`` to a freshly (re)launched runner and
    awaits it, so the runner's ``create_session`` completes before the
    caller forwards a message. For a claude-native session that means
    the tmux terminal **and its transcript forwarder are watching**
    before the web message is injected into the TUI — the round-trip
    that promotes the optimistic bubble and streams the reply only
    happens if the forwarder is in place first.

    This closes the host-restart race: today the auto-relaunch /
    resume paths wait only for the runner's *tunnel* to register
    (``runner_client`` becomes non-None), not for the session
    handshake, so the message can be injected before the forwarder
    attaches and is lost. The new / runner-bound paths don't hit this
    because they run the handshake as a distinct step before any
    message (``create_session`` endpoint) or against a from-offset-0
    forwarder.

    Current servers route this and ``_on_runner_connect`` through one
    generation-aware initializer, so both callers await the same response.
    The runner retains its own single-flight as the compatibility backstop for
    older servers and cross-replica delivery.

    Best-effort and matching the create / PATCH handshakes: a transport
    error is logged and swallowed (the relay + ``_on_runner_connect``
    are the backstop), but the *await* — the actual fix — still
    serializes the handshake ahead of the caller's message forward.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for *session_id*; supplies
        ``agent_id`` and ``sub_agent_name`` for the handshake body.
    :param runner_client: Runner client already resolved for
        *session_id* (its tunnel is up).
    :param conversation_store: Store used to clear persisted disconnect
        error labels once the handshake proves the runner recovered.
    :param suppress_recovery_turn: When ``True``, ask the runner not to
        start a crash-recovery turn during ``create_session``.  Must be
        set whenever the caller will forward a message immediately after
        this call: the server persists the message to DB before calling
        session-init, so the runner's history load would otherwise see
        the pending message and start a recovery turn — the subsequent
        forward then arrives to an active turn, is buffered, and is
        processed a second time once the (redundant) recovery turn
        finishes.
    :param require_success: Raise ``runner_unavailable`` when the handshake
        fails instead of using the message path's best-effort fallback.
    :returns: ``True`` when a current runner explicitly confirmed its native
        terminal is ready; ``False`` for legacy or non-native responses.
    """
    try:
        if initializer is not None:
            resp = await initializer.initialize(
                conv,
                runner_client,
                timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
                suppress_recovery_turn=suppress_recovery_turn,
            )
        else:
            from omnigent.version import VERSION

            resp = await runner_client.post(
                "/v1/sessions",
                json=build_runner_session_init_payload(
                    conv,
                    server_version=VERSION,
                    suppress_recovery_turn=suppress_recovery_turn,
                ),
                timeout=_RUNNER_SESSION_INIT_TIMEOUT_S,
            )
        from omnigent.server.runner_session_init import runner_inference_verified

        if not runner_inference_verified(conv, resp):
            raise OmnigentError(
                "The runner did not accept this session's saved inference configuration",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        # httpx only raises on transport errors; a 4xx/5xx means create_session
        # likely didn't run (terminal + forwarder not set up), so surface it
        # via the same warning path rather than silently forwarding into a
        # half-initialized runner.
        resp.raise_for_status()
    except (httpx.HTTPError, ConnectionError) as exc:
        _logger.warning(
            "Session-init handshake to runner failed for session %s; "
            "forwarding the message anyway",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        if require_success or conv.inference_snapshot is not None:
            raise OmnigentError(
                "The recovered runner did not finish session initialization.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        return False

    await _publish_runner_recovered_status(session_id, conversation_store)
    from omnigent.server.child_session_recovery import (
        restore_active_children,
        schedule_child_restoration,
    )

    # Legacy callers leave descendant restoration to the runner-connect hook.
    if initializer is not None:
        if suppress_recovery_turn and not require_success:
            schedule_child_restoration(conv, runner_client, conversation_store, initializer)
        else:
            await _ensure_runner_relay_ready(
                session_id, conv.runner_id, runner_client, conversation_store
            )
            await restore_active_children(conv, runner_client, conversation_store, initializer)
    try:
        payload = resp.json()
    except ValueError:
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("session_init_protocol_version") == 2
        and payload.get("terminal_ready") is True
    )


async def _wake_parent_on_subagent_launch_failure(
    session_id: str,
    conv: Conversation,
    error: ErrorData,
    runner_router: RunnerRouter | None,
) -> None:
    """
    Wake the parent runner when a host-launched sub-agent fails to start.

    Mirrors the terminal-status path's parent-wake (the ``idle`` /
    ``failed`` branch of ``external_session_status`` in
    :func:`post_event`): forward an ``external_session_status: failed``
    edge — carrying the launch error as ``output`` so it lands in the
    parent's inbox — to the sub-agent's own runner, then require the
    forward to land. The runner's ``external_session_status`` handler
    maps ``failed`` to ``mark_subagent_work_terminal(status="failed")``,
    which marks the parent's work entry terminal and wakes the parent.

    No-ops for non-sub-agent sessions.

    :param session_id: Sub-agent session id, e.g. ``"conv_child123"``.
    :param conv: Conversation row for the sub-agent session.
    :param error: Launch error to relay to the parent as the turn result.
    :param runner_router: Router used to resolve the sub-agent's runner,
        or ``None`` (then the global client is used).
    :returns: None.
    :raises OmnigentError: If the parent's runner could not be reached
        or rejected the forwarded failure status — dropping it would
        strand the parent waiting forever.
    """
    if conv.kind != "sub_agent":
        return
    forward_body: dict[str, Any] = {
        "type": _EXTERNAL_SESSION_STATUS_TYPE,
        # ``output`` is the parent-inbox result text on a failed edge
        # (runner: ``output or "...turn failed"``); pass the real error.
        "data": {"status": "failed", "output": error.message},
    }
    runner_result = await _forward_session_change_to_runner(
        session_id,
        runner_router,
        forward_body,
    )
    _require_external_status_forward(session_id, "failed", runner_result)


async def _persist_host_launch_failure_turn(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    host_error: str | None,
    runner_router: RunnerRouter | None,
    *,
    created_by: str | None,
    host_error_code: str,
) -> str:
    """
    Persist a consumed user message and a host-launch failure error.

    Used when a message arrives for a host-bound session whose runner is
    dead and the host deterministically refuses the relaunch. The message
    is the real runner-start attempt, so the server records the user's
    message (so the input is consumed, not silently dropped) and a sibling
    ``type="error"`` item carrying the host's reason, then publishes the same
    live error/status events the web renders as an error banner. The host
    binding is left intact so a later message can relaunch after remediation.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param conv: Conversation row for the session.
    :param body: Original user message event.
    :param conversation_store: Store used for the durable append.
    :param host_error: The refusal text to surface. Workspace refusals must
        pass the server-rebuilt message (see
        :func:`~omnigent.host.frames.workspace_missing_message`); harness
        refusals pass the host's own text, which is a deterministic setup
        hint rather than runner output.
    :param runner_router: Router used to resolve a sub-agent's runner for
        the parent-wake forward, or ``None`` in in-process / test setups.
    :param created_by: Authenticated posting actor, e.g.
        ``"alice@example.com"``; ``None`` in single-user mode.
    :param host_error_code: Allowlisted structured host failure category.
    :returns: Store-assigned id of the consumed user message item.
    """
    if host_error_code not in {
        _HARNESS_NOT_CONFIGURED_ERROR_CODE,
        _WORKSPACE_MISSING_ERROR_CODE,
    }:
        raise ValueError(f"unsafe host launch error code: {host_error_code!r}")
    fallback_message = (
        "the agent's harness is not configured on the selected host"
        if host_error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE
        else "The session workspace no longer exists on the selected host."
    )
    error = ErrorData(
        source="execution",
        code=host_error_code,
        message=host_error if host_error else fallback_message,
    )
    turn_id = generate_task_id()
    user_item = _build_new_item(body, turn_id, created_by=created_by)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [user_item],
    )
    await _seed_missing_title_from_user_message(conv, user_item, conversation_store)
    error_persist_result = await _relay_persist_error_once(
        conversation_store,
        session_id,
        NewConversationItem(type="error", response_id=turn_id, data=error),
    )
    consumed = persisted_items[0]
    _publish_input_consumed(session_id, consumed)
    if error_persist_result == "persisted":
        _publish_error_event(session_id, error)
    _publish_terminal_pending(session_id, False)
    _publish_status(
        session_id,
        "failed",
        ErrorDetail(code=error.code, message=error.message),
        failure_origin="host_launch_failed",
    )
    # A host-launched sub-agent that cannot start must wake its parent —
    # no-ops for top-level sessions.
    await _wake_parent_on_subagent_launch_failure(session_id, conv, error, runner_router)
    return consumed.id


async def _persist_session_event(
    session_id: str,
    body: SessionEventInput,
    conversation_store: ConversationStore,
) -> str:
    """
    Persist a user event without forwarding to a runner.

    Used when the runner isn't online yet but the session has a
    ``host_id`` — the message is stored so the runner's crash-
    recovery block picks it up from history when it connects.

    :param session_id: Session/conversation identifier.
    :param body: The validated event input.
    :param conversation_store: Store for item persistence.
    :param agent_name: Agent name for title seeding.
    :returns: The store-assigned item id.
    """
    import uuid

    turn_id = f"turn_{uuid.uuid4().hex}"
    item = _build_new_item(body, turn_id)
    persisted_items = await asyncio.to_thread(
        conversation_store.append,
        session_id,
        [item],
    )
    conv = await asyncio.to_thread(
        conversation_store.get_conversation,
        session_id,
    )
    if conv is not None:
        await _seed_missing_title_from_user_message(
            conv,
            item,
            conversation_store,
        )
    item_id = persisted_items[0].id if persisted_items else turn_id
    _publish_external_conversation_item(session_id, persisted_items[0])
    return item_id


#: Placeholder model on a decision card for a routing call that never answered.
#: The same spelling the auto-harness path already uses, so the UI renders one
#: "not routed" outcome however routing failed.
_UNAVAILABLE_ROUTED_MODEL = "unavailable"


def _unavailable_routing_card(reason: str) -> tuple[str, dict[str, Any]]:
    """
    Build the declined ``routing_decision`` card for a routing call that failed.

    Routing is never allowed to block work, but a silent fall-through leaves
    the user with a session that quietly ignored the toggle they turned on.
    This is the visible half of failing open: no model is pinned, and the card
    names the reason.

    :param reason: Why nothing was routed, e.g.
        ``"Routing call failed: router returned HTTP 500"``.
    :returns: ``(model, verdict)`` ready for
        :func:`_emit_server_routing_decision`.
    """
    return _UNAVAILABLE_ROUTED_MODEL, {"rationale": reason, "applied": False}


def _pinned_spawn_family(parent: Conversation | None) -> str | None:
    """Return the family a parent's spawns are confined to, if any.

    Only a Smart Routing parent on a PINNED harness confines them — its
    spawns are routed inside its own family. An auto-harness parent hands
    the family choice to the router, and a parent that routes no spawns at
    all confines nothing.

    :param parent: The parent session's row, or ``None`` for a top-level
        create (nothing to stay in family with).
    :returns: ``"claude"`` / ``"gpt"`` / ``"pi"``, or ``None`` when the
        parent's spawns may land in any family.
    """
    if parent is None or not subagent_routing_enabled(parent.subagent_routing_override):
        return None
    if auto_harness_session(parent):
        return None
    return harness_family(_resolve_harness(parent))


def _reject_out_of_family_child(
    parent: Conversation | None,
    child_harness: str | None,
) -> None:
    """Refuse a pinned parent's create of a child in another family.

    The primary defense for the family rule: a pinned codex Smart Routing
    session must not be able to stand up a claude child at all, rather than
    standing one up and having routing decline it afterwards. The child is
    refused here, at the create, where the caller still sees the reason.

    :param parent: The parent session's row, or ``None`` for a top-level
        create.
    :param child_harness: The harness the child would run, e.g.
        ``"claude-native"``. ``None`` (unresolvable) is allowed through —
        nothing proves it is out of family.
    :raises OmnigentError: 400 ``INVALID_INPUT`` when the child's family
        differs from the family the parent's spawns are confined to.
    """
    family = _pinned_spawn_family(parent)
    child_family = harness_family(child_harness)
    if family is None or child_family is None or family == child_family:
        return
    parent_harness = _resolve_harness(parent)
    raise OmnigentError(
        f"A {parent_harness} session with Smart Routing spawns only agents in its own "
        f"model family; {child_harness} is not one. Pick an agent that runs on "
        f"{parent_harness}, or start the session in Smart Routing (auto) harness mode "
        "to let the router choose the family.",
        code=ErrorCode.INVALID_INPUT,
    )


def _publish_routed_model(session_id: str, model: str) -> None:
    """
    Publish a ``session.model`` SSE for a router-selected model.

    The model dropdown updates from PATCH responses and ``session.model``
    events only, so a routed session shows the launch model until this
    fires. Mirrors the event ``_persist_external_model_change`` publishes
    for harness-observed switches; if the harness-side apply later fails,
    that mirror corrects this value.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :param model: The catalog id the picker lists, e.g.
        ``"databricks-claude-opus-4-8"`` — the client sets its selection
        from it.
    :returns: None.
    """
    event = SessionModelEvent(
        type="session.model",
        conversation_id=session_id,
        model=model,
    )
    session_stream.publish(session_id, event.model_dump())


class _ForwardedItem(NamedTuple):
    """A user item the runner took.

    :param item_id: Store-assigned id of the persisted item.
    :param turn: The runner's number for the turn the message started or joined; ``None``
        when the runner did not say.
    """

    item_id: str
    turn: int | None


def _runner_turn(response: httpx.Response) -> int | None:
    """The ``turn`` a runner's acceptance names, or ``None`` (fakes may expose no body)."""
    payload: object = None
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        payload = response.json()
    turn = payload.get("turn") if isinstance(payload, dict) else None
    return turn if isinstance(turn, int) and not isinstance(turn, bool) else None


def _is_not_running_refusal(response: httpx.Response) -> bool:
    """Whether the runner refused an ``if_running`` message because no turn runs."""
    if response.status_code != 409:
        return False
    payload: object = None
    with contextlib.suppress(ValueError, AttributeError, TypeError):
        payload = response.json()
    return isinstance(payload, dict) and payload.get("error") == "not_running"


def _runner_reject_detail(response: httpx.Response) -> str:
    """
    Describe a runner's refusal of a forwarded event, for the user-visible error.

    The runner's error bodies are ``{"error": <code>, "detail": <text>}``, but a
    proxy or an unhandled path can return any shape, so this falls back to a
    body preview and finally to the bare status code — the caller needs a
    non-empty message either way. Tolerates response fakes that expose only
    ``status_code``, since those stand in for the runner across the tests.

    :param response: The runner's 4xx/5xx response to the forwarded event.
    :returns: A one-line detail, e.g.
        ``"harness_spawn_failed: harness spawn failed (see runner log)"``.
    """
    detail: str | None = None
    code: str | None = None
    payload: object = None
    with contextlib.suppress(ValueError, AttributeError):
        payload = response.json()
    if isinstance(payload, dict):
        raw_detail = payload.get("detail")
        raw_code = payload.get("error")
        detail = raw_detail.strip() if isinstance(raw_detail, str) and raw_detail.strip() else None
        code = raw_code.strip() if isinstance(raw_code, str) and raw_code.strip() else None
    if detail is None and code is not None:
        return code
    if detail is None:
        body = getattr(response, "text", "") or ""
        return body.strip()[:200] or f"runner returned status {response.status_code}"
    return f"{code}: {detail}" if code else detail


#: ``feature name -> hook(session_id, item_id, context)``, called once a message that carried a
#: top-level ``context`` is stored and before it reaches the runner, so a turn starting on it
#: already sees what the context recorded (the artifact open beside the chat). Set by a
#: feature's install; a failing hook is logged and never fails the send.
ON_MESSAGE_CONTEXT: dict[str, Callable[[str, str, dict[str, Any]], None]] = {}
#: ``context`` keys only the engine's own server-side dispatch may set (a wake it builds, such as
#: topic watch's ``proactive`` label, ADR 0036): dropped from every event that arrives through the
#: session events route, from a client or a runner.
ENGINE_ONLY_CONTEXT_KEYS: frozenset[str] = frozenset({"proactive"})


def _drop_engine_only_context(body: SessionEventInput) -> None:
    """Remove :data:`ENGINE_ONLY_CONTEXT_KEYS` from an event that came in from outside."""
    if body.context and not ENGINE_ONLY_CONTEXT_KEYS.isdisjoint(body.context):
        body.context = {k: v for k, v in body.context.items() if k not in ENGINE_ONLY_CONTEXT_KEYS}


#: ``feature name -> hook(session_id)``, called once a session is deleted so a feature drops what
#: it keeps for it. Set by a feature's install; a failing hook is logged, never fails the delete.
ON_SESSION_DELETED: dict[str, Callable[[str], None]] = {}
#: ``feature name -> hook(session_id, elicitation_id, action, answered_by)``, called (blocking,
#: off the loop)
#: once a person's ``accept`` or ``decline`` reached an elicitation through the session routes
#: (the resolve URL or the ``approval`` event), so a feature records who answered it and how.
#: A failing hook is logged and never fails the answer.
ON_ELICITATION_ANSWERED: dict[str, Callable[[str, str, str, str | None], None]] = {}


async def _signal_elicitation_answered(
    session_id: str, data: dict[str, Any], answered_by: str | None
) -> None:
    """Run :data:`ON_ELICITATION_ANSWERED` for a verdict *answered_by* delivered in *session_id*
    (``data``: ``elicitation_id`` and ``action``); a ``cancel`` or a malformed verdict runs
    nothing. A hook acts only on an elicitation of *session_id*."""
    elicitation_id = data.get("elicitation_id")
    action = data.get("action")
    if not isinstance(elicitation_id, str) or action not in ("accept", "decline"):
        return
    for hook in list(ON_ELICITATION_ANSWERED.values()):
        try:
            await asyncio.to_thread(hook, session_id, elicitation_id, action, answered_by)
        except Exception:  # noqa: BLE001 - bookkeeping, never a reason to fail the answer
            _logger.warning(
                "elicitation answered hook failed for %s", elicitation_id, exc_info=True
            )


async def _persist_user_event_item(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    *,
    created_by: str | None = None,
) -> ConversationItem:
    """
    Append a user event to the conversation and seed a missing title from it.

    The single place a POSTed item event becomes a stored item: the inline
    dispatch calls it, and so does the early-ack path (see
    :func:`schedule_deferred_message_dispatch`) that stores the message
    before waking the runner.

    :param session_id: Session/conversation identifier.
    :param conv: The conversation row for ``session_id``.
    :param body: The validated event input from the client.
    :param conversation_store: Store for item persistence.
    :param created_by: Authenticated identity of the posting actor.
    :returns: The stored item, with its store-assigned id.
    """
    import uuid

    item = _build_new_item(body, f"turn_{uuid.uuid4().hex}", created_by=created_by)
    persisted_items = await asyncio.to_thread(conversation_store.append, session_id, [item])
    await _seed_missing_title_from_user_message(conv, item, conversation_store)
    if body.type == "message":
        await rounds.note_message(
            conversation_store,
            conv,
            persisted_items[0].id,
            body.data,
            body.context,
            turn_running=_session_status_cache.get(session_id) in ("running", "waiting"),
        )
    if body.type == "message" and body.context:
        for hook in list(ON_MESSAGE_CONTEXT.values()):
            try:
                await asyncio.to_thread(hook, session_id, persisted_items[0].id, body.context)
            except Exception:  # noqa: BLE001 - context is a hint, never a reason to fail a send
                _logger.warning("message context hook failed for %s", session_id, exc_info=True)
    return persisted_items[0]


async def _forward_event_to_runner(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    agent_name: str | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    host_store: HostStore | None = None,
    persisted_item: ConversationItem | None = None,
    app_state: Any = None,
) -> _ForwardedItem:
    """
    Persist a user event and forward it to the runner.

    The server persists the item to the conversation store
    (invariant I1: persist-before-forward), publishes acknowledgment
    events, then POSTs the event to the runner's
    ``POST /v1/sessions/{id}/events``.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The conversation row for ``session_id``.
    :param body: The validated event input from the client.
    :param conversation_store: Store for item persistence.
    :param runner_client: HTTP client pointed at the runner.
    :param agent_name: Human-readable agent name for the
        ``model`` field on the runner body, e.g. ``"research-agent"``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary content store for
        resolving ``file_id`` references before forwarding.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint so ``proxy_stream`` knows to load
        the agent spec and initialise :class:`ProxyMcpManager` for
        this turn. ``False`` by default (agents without MCP servers).
    :param created_by: Authenticated identity of the posting actor,
        recorded on the persisted item for attribution.
    :param host_store: Host registrations, read only to learn whether this
        session's harness is AI-Gateway-backed (which router may route it).
        ``None`` reads as unknown, which counts as backed.
    :param persisted_item: The user item when the caller already stored it
        (see :func:`_persist_user_event_item`); it is then forwarded without
        a second append.
    :param app_state: ``request.app.state``, for the owner's Foundation pick a
        Super Chat with no stored model runs on. ``None``: no pick is resolved.
    :returns: The store-assigned id of the persisted item.
    """
    persisted_item = persisted_item or await _persist_user_event_item(
        session_id, conv, body, conversation_store, created_by=created_by
    )
    persisted_items = [persisted_item]
    # Don't publish status="running" or input.consumed here —
    # wait until after the forward to the runner succeeds.
    # Publishing early causes the REPL to start its streaming
    # timer before the turn actually starts, showing a
    # premature "working" phase.

    # Resolve file_id references (input_image, input_file) to
    # inline base64 data: URIs before forwarding. The runner and
    # harness don't have access to the server's file store — the
    # LLM endpoint needs the actual content, not an internal ID.
    forwarded_data = dict(body.data)
    if (
        file_store is not None
        and artifact_store is not None
        and "content" in forwarded_data
        and isinstance(forwarded_data["content"], list)
    ):
        from omnigent.runtime.content_resolver import (
            _resolve_message_content,
        )

        _unresolved = [
            b for b in forwarded_data["content"] if isinstance(b, dict) and "file_id" in b
        ]
        if _unresolved:
            try:
                forwarded_data["content"] = _resolve_message_content(
                    forwarded_data["content"],
                    file_store,
                    artifact_store,
                    session_id=session_id,
                )
                _logger.debug(
                    "Resolved %d file_id block(s) for session=%s before forwarding",
                    len(_unresolved),
                    session_id,
                    extra={"session_id": session_id},
                )
            except (ValueError, KeyError):
                _logger.warning(
                    "File reference resolution failed for session=%s "
                    "(unresolved file_id blocks will reach the runner unresolved — "
                    "runner will attempt fallback resolution)",
                    session_id,
                    exc_info=True,
                    extra={"session_id": session_id},
                )

    # Flatten SessionEventInput {type, data} into the runner's
    # discriminated-union shape {type, ...data_fields}. The runner's
    # POST handler expects the harness event shape, not the
    # session-API wrapper. Include agent_id so the runner can
    # resolve the harness type and spawn environment.
    runner_body: dict[str, Any] = {
        "type": body.type,
        **forwarded_data,
        "agent_id": conv.agent_id,
        # model tags the ResponseObject for REPL rendering.
        # Use the human-readable agent name when available.
        "model": agent_name or conv.agent_id or "",
        # Signal to proxy_stream that it should initialise
        # ProxyMcpManager and fetch MCP tool schemas for this turn.
        # Only included (and only True) when the agent has MCP
        # servers — False/absent saves the runner from a no-op spec
        # load on every turn for agents without MCP servers.
        "has_mcp_servers": has_mcp_servers,
        # Whether a live renderer (e.g. the desktop app's embedded
        # browser) is subscribed to this session's stream right now.
        # The runner drops the ``browser_*`` tool schemas for the turn
        # when this is False — a headless session must not advertise
        # tools that can never be served. Presence heuristic: any stream
        # subscriber counts (the protocol has no renderer-capability
        # registration), so a non-renderer viewer keeps tools advertised
        # — no worse than the pre-hint behavior.
        "browser_renderer_available": session_stream.has_subscribers(session_id),
        # Id of the item just persisted for this turn. On a cold runner
        # cache the runner reloads history (which includes this item in
        # PRE-resolution form) and drops it by id, appending its own
        # resolved copy — id-based dedup, not a role/content guess.
        "persisted_item_id": persisted_items[0].id,
    }
    if body.if_running:
        # The runner decides under its turn lock: with no turn running it starts none.
        runner_body["if_running"] = True
    # The Round the message is in, as a trace parent, when spans are exported (ADR 0041): the
    # turn it starts joins the Round's trace.
    if traceparent := rounds_trace.take(session_id):
        runner_body["traceparent"] = traceparent
    # Persist the turn-initiating actor so /policies/evaluate and MCP
    # tools/call can read it back on any server replica.  Skip system-driven
    # forwards (sub-agent results, parent-wake carry created_by=None) — they
    # must not stomp the in-flight turn's actor.
    # Known gap: a queued message from user B can overwrite this label while
    # user A's turn is still executing tool calls on a shared session.  The
    # runner's _active_turns guard prevents two turns from running on the same
    # session concurrently, but the label is written at server-forward time
    # (before the runner queues the message), not at runner-turn-start time.
    # For the common case (sequential users or single-user sessions) this is
    # correct; strictly concurrent shared-session use is an accepted gap.
    if created_by is not None:
        await asyncio.to_thread(
            conversation_store.set_labels,
            session_id,
            {_TURN_ACTOR_LABEL: created_by},
        )
    # Forward request-supplied client-side tool schemas so non-native
    # harnesses can emit (and tunnel) the caller's tools — the runner
    # merges these into the harness tool list (_merge_request_client_tools).
    # Without this the runner only ever sees the spec's builtin/MCP tools
    # and the model can't invoke client-side Read/Write/Glob/etc.
    if body.tools:
        runner_body["tools"] = body.tools
    # Per-event override wins; fall back to the persisted column so a
    # UI / REPL PATCH applies even when the client doesn't repeat
    # model_override on every event. ``is not None`` over ``or`` per
    # the no-invented-defaults rule.
    effective_runner_override = (
        body.model_override if body.model_override is not None else conv.model_override
    )
    # ── Auto-harness resolution ───────────────────────────────────────
    # When the session was created with harness_override="auto", the real
    # harness + model are determined here on the first message where user
    # text is available.  After resolution the sentinel is replaced with
    # the concrete harness so subsequent turns behave normally.
    # Tracks whether this block ran the router this turn, so the message
    # routing block below doesn't re-route the same message (which would
    # double the judge call, emit two cards, and risk a mismatched pick).
    _auto_resolved_this_turn = False
    # Auto-harness verdict captured for card emission AFTER the runner forward
    # and input.consumed (so the live SSE stream delivers the user bubble
    # before the routing card, matching the message routing path).
    _auto_card_model: str | None = None
    _auto_card_verdict: dict[str, Any] | None = None
    # Set when the auto-harness routing call itself failed. The card still
    # says so, but the route-once label is left unclaimed — the same rule the
    # turn, native-pane and child-spawn paths follow, so one outage at create
    # cannot be the reason this session never routes again.
    _auto_route_failed = False
    # The resolved harness, read back at card-emission time below.
    _auto_harness: str | None = None
    if conv.harness_override == "auto" and body.type == "message":
        from omnigent.server.smart_routing import (
            _AUTO_ROUTING_HARNESSES,
            route_session_harness,
        )

        _auto_text = _extract_user_text_for_routing(body)
        if _auto_text:
            _auto_resolved_this_turn = True
            # The router may land this session on any auto-routing harness, so
            # every one of them must be gateway-backed for the workspace router
            # to serve the pick; otherwise the built-in judge does.
            _auto_backed = _gateway_backed(
                await _session_routing_host(conv, host_store), _AUTO_ROUTING_HARNESSES
            )
            # For a forced-auto child, route against the parent's catalog (full
            # spawnable-worker map) rather than the child's leaf "self" catalog.
            _auto_harness, _auto_model, _auto_verdict, _auto_error = await route_session_harness(
                _auto_text,
                session_id=session_id,
                catalog_session_id=conv.parent_conversation_id,
                runner_client=runner_client,
                gateway_backed=_auto_backed,
                allow_static_fallback=_auto_backed,
            )
            try:
                # Always clear the "auto" sentinel even when routing
                # returned no harness (unavailable/failed) so the branch
                # doesn't re-run on every subsequent turn.
                _conv_updates: dict[str, Any] = (
                    {"harness_override": _auto_harness}
                    if _auto_harness is not None
                    else {"_unset_harness_override": True}
                )
                if _auto_model is not None and effective_runner_override is None:
                    _conv_updates["model_override"] = _auto_model
                    effective_runner_override = _auto_model
                _updated = await asyncio.to_thread(
                    conversation_store.update_conversation,
                    session_id,
                    **_conv_updates,
                )
                if _updated is not None:
                    conv = _updated
            except (OSError, ValueError):
                _logger.warning(
                    "auto-harness: failed to persist resolved harness for session=%s",
                    session_id,
                    exc_info=True,
                    extra={"session_id": session_id},
                )
            # Defer card emission until after input.consumed (see below).
            if _auto_model is not None and _auto_verdict is not None:
                _auto_card_model = _auto_model
                _auto_card_verdict = _auto_verdict
            elif _auto_error is not None:
                # Routing failed — surface why auto-harness fell back to defaults.
                _auto_card_model, _auto_card_verdict = _unavailable_routing_card(_auto_error)
                _auto_route_failed = True
    # ── Server-side intelligent routing ──────────────────────────────
    # When the session toggle is ON and no model has been chosen yet,
    # call the judge LLM on the FIRST message to pick the model for
    # the entire session.  The verdict is persisted as model_override
    # on the conversation so subsequent turns reuse it without another
    # judge call.
    # Route if: this session's own toggle is on (top-level), OR this is a
    # sub-agent and its parent's subagent-routing switch is on — the same switch
    # the native-subagent hook and the child-create path gate on, so one visible
    # knob governs every spawn.
    _parent_routing_on = False
    _parent_conv: Conversation | None = None
    if conv.parent_conversation_id is not None:
        _parent_conv = await asyncio.to_thread(
            conversation_store.get_conversation, conv.parent_conversation_id
        )
        _parent_routing_on = _parent_conv is not None and subagent_routing_enabled(
            _parent_conv.subagent_routing_override
        )
    _routing_enabled = (
        conv.cost_control_mode_override == "on" and conv.parent_conversation_id is None
    ) or _parent_routing_on
    _routed_model: str | None = None
    _routed_harness: str | None = None
    _verdict: dict[str, Any] | None = None
    # Set when the routing call itself failed — on either branch below. The
    # chip then carries the "unavailable" placeholder rather than a model,
    # nothing is pinned, and the route-once label is left unclaimed.
    _route_failed = False
    # The model the orchestrator's ``sys_session_send`` asked for, captured
    # before routing overwrites it: when the router picks something else the
    # attempt is recorded on the decision instead of silently vanishing.
    _attempted_override = effective_runner_override
    # A child that already carries a routing decision is done: its harness and
    # model were chosen on its first message and must not move again. Without
    # this the child clause below (which deliberately routes past an
    # orchestrator-supplied model) matched on EVERY message, so a follow-up
    # re-ran the judge and re-persisted ``harness_override`` — a mid-session
    # harness flip, contradicting the create-time-only contract the
    # ``harness_override`` forward below documents. The top-level arm's own
    # once-only gate is ``effective_runner_override is None``; the decision
    # label is the child's equivalent, because a child's model_override can be
    # set by ``sys_session_send`` without anything having routed.
    _child_routed_before = conv.parent_conversation_id is not None and bool(
        conv.labels.get(ROUTING_DECISION_LABEL_KEY)
    )
    # For child sessions, route even when the orchestrator specified a model via
    # sys_session_send (effective_runner_override is already set). Smart routing
    # always wins over the LLM's own model choice when the parent toggle is on.
    _should_route = (
        _routing_enabled
        and body.type == "message"
        # The auto-harness block above already routed this turn (harness +
        # model) — don't re-run the router for the same message.
        and not _auto_resolved_this_turn
        and not _child_routed_before
        and (effective_runner_override is None or conv.parent_conversation_id is not None)
    )
    if _should_route:
        _user_text = _extract_user_text_for_routing(body)
        if _user_text:
            if _parent_routing_on:
                # Child sessions: use route_session_harness to pick both harness
                # and model, overriding whatever the orchestrator specified in
                # sys_session_send.
                from omnigent.server.smart_routing import (
                    AUTO_NATIVE_ROUTING_HARNESSES,
                    route_session_harness,
                )

                # Confine the pick to the harness this CHILD runs. A child that
                # reaches here has one: either its sub-agent spec declares it
                # (a named worker like polly's ``pi``) or the spawn pinned it —
                # an unpinned child of a Smart Routing parent carries the
                # "auto" sentinel and was resolved by the auto block above.
                # Read off the PARENT before, which offered a pi worker the
                # brain's claude family and a claude worker the codex family;
                # under an auto parent it dropped the restriction entirely, so
                # a pi child could be handed a codex verdict it then ran pi on.
                _child_harness = _resolve_harness(conv)
                _child_pinned = _child_harness is not None and _child_harness != "auto"
                _child_family = harness_family(_child_harness) if _child_pinned else None
                # A single-harness candidate set is the honest offer for a
                # pinned child: the family filter alone still admits the other
                # harnesses in that family (a ``codex-native`` child would be
                # offered SDK ``codex``), and it cannot narrow a harness whose
                # family is unknown at all.
                _child_candidates = (_child_harness,) if _child_pinned else None
                # Which harnesses the pick may land on decides whose gateway
                # backing must hold: a cross-harness child can go either way, a
                # pinned one only where it already runs.
                _child_gateway_harnesses = (
                    (_child_harness or "",) if _child_pinned else AUTO_NATIVE_ROUTING_HARNESSES
                )
                _child_host = await _session_routing_host(conv, host_store)
                if _child_host is None and _parent_conv is not None:
                    _child_host = await _session_routing_host(_parent_conv, host_store)
                _child_backed = _gateway_backed(_child_host, _child_gateway_harnesses)
                # Route against the PARENT's catalog: it enumerates the
                # spawnable workers (claude_code/codex/pi) with full model
                # lists, whereas this child's own leaf catalog is "self"-only
                # and would force the static fallback (a smaller/different set).
                _routed_harness, _routed_model, _verdict, _route_err = await route_session_harness(
                    _user_text,
                    session_id=session_id,
                    catalog_session_id=conv.parent_conversation_id,
                    runner_client=runner_client,
                    allowed_family=_child_family,
                    harness_candidates=_child_candidates,
                    gateway_backed=_child_backed,
                    allow_static_fallback=_child_backed,
                )
                if _routed_model is not None:
                    effective_runner_override = _routed_model
                try:
                    _child_updates: dict[str, Any] = {}
                    if _routed_model is not None:
                        _child_updates["model_override"] = _routed_model
                    if _routed_harness is not None:
                        _child_updates["harness_override"] = _routed_harness
                    if _child_updates:
                        await asyncio.to_thread(
                            conversation_store.update_conversation,
                            session_id,
                            **_child_updates,
                        )
                        if _routed_model is not None:
                            # The child's picker lists catalog ids and its
                            # harness starts on this pin, so the catalog id
                            # is the spelling to publish.
                            _publish_routed_model(session_id, _routed_model)
                except (OSError, ValueError):
                    _logger.warning(
                        "smart_routing: failed to persist harness/model for child session=%s",
                        session_id,
                        exc_info=True,
                        extra={"session_id": session_id},
                    )
                if _routed_model is None and _route_err is not None:
                    # ``route_session_harness`` already fails open, so the spawn
                    # runs on whatever the orchestrator asked for — but the
                    # reason was unpacked and dropped, leaving no card and no
                    # way to tell an outage from a router with no opinion. Say
                    # it, on the same placeholder the other paths use. Set last
                    # so nothing above can pin or publish the placeholder.
                    _routed_model, _verdict = _unavailable_routing_card(_route_err)
                    _route_failed = True
            else:
                # Top-level sessions: model-only routing (harness already fixed by spec).
                from omnigent.server.smart_routing import route_turn_or_decline

                _harness = _resolve_harness(conv)
                # A turn cannot change harness, so only this session's own
                # family has to be gateway-backed for the workspace router.
                _turn_backed = _gateway_backed(
                    await _session_routing_host(conv, host_store), (_harness or "",)
                )
                # ``_or_decline``: a routing outage returns an error string
                # rather than raising. Raised here it became a 500 on the
                # events POST, so a router that was merely down cost the user
                # a message that had already been persisted.
                _routed_model, _verdict, _turn_route_err = await route_turn_or_decline(
                    _harness,
                    _user_text,
                    session_id=session_id,
                    runner_client=runner_client,
                    gateway_backed=_turn_backed,
                    allow_static_fallback=_turn_backed,
                )
                if _turn_route_err is not None:
                    # Not routed, and visibly so — the turn runs on the
                    # session's own model with the reason on its card.
                    _routed_model, _verdict = _unavailable_routing_card(_turn_route_err)
                    _route_failed = True
                if _routed_model is not None and not _route_failed:
                    effective_runner_override = _routed_model
                    # Persist as the session's model_override so all
                    # subsequent turns use this model automatically.
                    try:
                        await asyncio.to_thread(
                            conversation_store.update_conversation,
                            session_id,
                            model_override=_routed_model,
                        )
                        _publish_routed_model(session_id, _routed_model)
                    except (OSError, ValueError):
                        _logger.warning(
                            "smart_routing: failed to persist model_override "
                            "for session=%s; turn still uses routed model",
                            session_id,
                            exc_info=True,
                            extra={"session_id": session_id},
                        )
    # ────────────────────────────────────────────────────────────────
    if effective_runner_override is None and app_state is not None:
        # A Super Chat with no pin runs its owner's Foundation pick (ADR 0027), never the
        # connection's default: the same answer the session read gives as ``effective_model``.
        from omnigent.server.routes.sandbox_inference import session_model

        effective_runner_override = await session_model(app_state, conv, conversation_store)
    if effective_runner_override is not None:
        runner_body["model_override"] = effective_runner_override
    # Forward the persisted create-time effort on every downward event, like
    # model_override above. Safe and idempotent: the runner reads the body as a
    # raw dict (extra keys ignored) and the value never changes after create.
    if conv.reasoning_effort is not None:
        runner_body["reasoning"] = {"effort": conv.reasoning_effort}
    # Per-session brain-harness override — create-time only, so no
    # per-event value exists; the persisted column is the source.
    # _routed_harness is non-None when the child routing path resolved one
    # this turn (conv is not refreshed, so we use the in-flight value).
    _effective_harness = _routed_harness or conv.harness_override
    if _effective_harness is not None and _effective_harness != "auto":
        runner_body["harness_override"] = _effective_harness

    # The runner's sessions-native POST returns 202 immediately
    # and starts the turn as a background task. No streaming
    # response to drain — events flow through GET /stream.
    try:
        _forward_resp = await runner_client.post(
            f"/v1/sessions/{session_id}/events",
            json=runner_body,
            timeout=_RUNNER_FORWARD_TIMEOUT,
        )
        # httpx only raises on transport errors, so a rejection (e.g. a 400 on a
        # malformed body, or a 501 from a runner with no process manager) would
        # otherwise read as a started turn: input.consumed would tell the client
        # the runner has the message and the session would sit "running" until
        # something else moved it. The turn's own failures do NOT come back here
        # — the runner accepts with 202 and reports them over the relay — so
        # this only catches "the runner never took the message". Checked on the
        # status rather than via ``raise_for_status`` so the runner-client fakes
        # that only expose ``status_code`` behave as they do in production.
        if _is_not_running_refusal(_forward_resp):
            # A message meant only for a running turn found none: it never happened.
            await asyncio.to_thread(
                conversation_store.delete_item, session_id, persisted_items[0].id
            )
            raise OmnigentError(
                "No turn is running to take this message.", code=ErrorCode.CONFLICT
            )
        if _forward_resp.status_code >= 400:
            # The live runner took nothing, so ``idle`` would read as a finished
            # turn that never ran. Persist the reason: the status edge is
            # SSE-only and would vanish on reload. Not strictly terminal — the
            # item stays persisted, so a later reconnect can still replay it as
            # a recovery turn.
            _reject_detail = _runner_reject_detail(_forward_resp)
            _logger.warning(
                "Runner rejected forwarded event for session=%s status=%s detail=%s",
                session_id,
                _forward_resp.status_code,
                _reject_detail,
                extra=debug_event(
                    "turn_dispatch_rejected",
                    session_id=session_id,
                    status=_forward_resp.status_code,
                    detail=_reject_detail,
                ),
            )
            _reject_error = ErrorDetail(code="runner_rejected_event", message=_reject_detail)
            # Persist before publishing: a client that reloads on the ``failed``
            # edge must not race a snapshot that has no ``last_task_error`` yet.
            await _persist_session_status_error_labels(
                session_id, _reject_error, conversation_store
            )
            _publish_status(
                session_id,
                "failed",
                _reject_error,
                failure_origin="runner_rejected_event",
            )
            raise OmnigentError(
                f"Runner rejected the message: {_reject_detail}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        # Publish input.consumed AFTER the forward succeeds —
        # the runner has the message and will start the turn.
        _publish_input_consumed(session_id, persisted_items[0])
        _logger.info(
            "turn dispatched to runner for session=%s",
            session_id,
            extra=debug_event(
                "turn_dispatched",
                session_id=session_id,
                agent=agent_name or conv.agent_id or "",
                harness=_effective_harness or "",
            ),
        )
        # Emit the routing_decision chip AFTER input.consumed so the
        # live SSE stream delivers the user bubble before the chip —
        # matching the store order (user message was persisted first).
        # Auto-harness card (success or failure) emitted here for the same
        # ordering reason; it was resolved earlier in the turn.
        if _auto_card_model is not None and _auto_card_verdict is not None:
            _auto_decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                _auto_card_model,
                _auto_card_verdict,
                scope="session",
                harness=_auto_harness,
            )
            if not _auto_route_failed:
                # A failed call is NOT this session's routing decision: the
                # label is the route-once gate, so claiming it would make one
                # create-time outage the reason nothing routes this session
                # again. The card still shows what happened.
                await _stamp_routing_decision_label(
                    session_id, conversation_store, _auto_decision_id
                )
            if conv.parent_conversation_id is not None:
                await _emit_server_routing_decision(
                    conv.parent_conversation_id,
                    conversation_store,
                    _auto_card_model,
                    _auto_card_verdict,
                    agent=agent_name or "",
                    scope="session",
                    harness=_auto_harness,
                    decision_id=_auto_decision_id,
                )
        if _routed_model is not None and _verdict is not None:
            _decision_scope = "child_session" if _parent_routing_on else "turn"
            # The router wins over the orchestrator's own pick; the attempt is
            # recorded so the UI can show what it overrode. Compared on the bare
            # arm, so another spelling of the same model (a spawn's
            # ``system.ai.glm-5-2`` against the router's ``databricks-glm-5-2``)
            # is not reported as an override.
            from omnigent.server.smart_routing import _bare_id as _bare_model_id

            _overridden = (
                _attempted_override
                if _attempted_override is not None
                and not _route_failed
                and _bare_model_id(_attempted_override) != _bare_model_id(_routed_model)
                else None
            )
            _decision_id = await _emit_server_routing_decision(
                session_id,
                conversation_store,
                _routed_model,
                _verdict,
                scope=_decision_scope,
                harness=_routed_harness or _resolve_harness(conv),
                attempted_override=_overridden,
            )
            if not _route_failed:
                # A failed call is NOT this session's routing decision: the
                # label is the route-once gate, so stamping it would make one
                # outage the reason the session never routes again. The card
                # still shows what happened.
                await _stamp_routing_decision_label(session_id, conversation_store, _decision_id)
            # Mirror the routing decision into the parent session so the
            # orchestrator's transcript also shows which model was chosen
            # for this sub-agent — the decision is otherwise only visible
            # on the child session screen.
            if _parent_routing_on and conv.parent_conversation_id is not None:
                await _emit_server_routing_decision(
                    conv.parent_conversation_id,
                    conversation_store,
                    _routed_model,
                    _verdict,
                    agent=agent_name or "",
                    scope=_decision_scope,
                    harness=_routed_harness or _resolve_harness(conv),
                    decision_id=_decision_id,
                    attempted_override=_overridden,
                )
    except (httpx.HTTPError, ConnectionError) as exc:
        # Transport failure — the runner never answered. The message is already
        # persisted (invariant I1), and a trailing user item is what
        # ``create_session`` replays as a recovery turn when the runner
        # reconnects, so this really is a queued message rather than a failure.
        # Keep publishing ``idle`` so the composer is released for a retry.
        _logger.exception(
            "Forward to runner failed for session=%s",
            session_id,
            extra={"session_id": session_id},
        )
        _publish_status(session_id, "idle")
        raise OmnigentError(
            "Runner is unreachable; message was persisted but could not be delivered. "
            "The runner may be restarting — retry or spawn a new session.",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc

    return _ForwardedItem(persisted_items[0].id, _runner_turn(_forward_resp))


async def _stamp_routing_decision_label(
    session_id: str,
    conversation_store: ConversationStore,
    decision_id: str | None,
) -> None:
    """Record the decision behind a session's pinned model as a label.

    The child-sessions API reads it to render which decision produced a
    sub-agent's routed model, without a new conversation column.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``set_labels``.
    :param decision_id: Decision identity, or ``None`` to skip.
    """
    if decision_id is None:
        return
    try:
        await asyncio.to_thread(
            conversation_store.set_labels,
            session_id,
            {ROUTING_DECISION_LABEL_KEY: decision_id},
        )
    except (OSError, ValueError):
        _logger.warning(
            "smart_routing: failed to label routing decision for session=%s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )


# Sessions with a message dispatch still awaiting its runner (cold boot). The
# session list reports them as running so a booting session spins instead of
# reading idle until the runner accepts the message. Process-local and
# best-effort: with several replicas only the one handling the POST knows.
_dispatch_in_flight: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()


@contextlib.contextmanager
def _mark_dispatch_in_flight(conversation_id: str) -> Iterator[None]:
    """
    Count a message as in flight for *conversation_id* until the block exits.

    :param conversation_id: Session/conversation identifier.
    """
    _dispatch_in_flight[conversation_id] = _dispatch_in_flight.get(conversation_id, 0) + 1
    try:
        yield
    finally:
        remaining = _dispatch_in_flight.get(conversation_id, 1) - 1
        if remaining > 0:
            _dispatch_in_flight[conversation_id] = remaining
        else:
            _dispatch_in_flight.pop(conversation_id, None)


# Sessions whose message dispatch was acknowledged early and is still waking
# or reaching its runner, with the lock that keeps those dispatches in the
# order the messages were stored.
_deferred_dispatch_pending: dict[str, int] = {}
_deferred_dispatch_locks: dict[str, asyncio.Lock] = {}
_deferred_dispatch_tasks: set[asyncio.Task[None]] = set()
# Each session's early-acknowledged dispatches still to finish, with their stored item ids.
_deferred_dispatch_items: dict[str, dict[asyncio.Task[None], str | None]] = {}


def session_has_deferred_dispatch(session_id: str) -> bool:
    """
    Whether an early-acknowledged message of *session_id* has not reached its runner.

    A later message must queue behind it, or it could overtake the earlier one
    once the runner is up.

    :param session_id: Session/conversation identifier.
    """
    return _deferred_dispatch_pending.get(session_id, 0) > 0


async def managed_wake_may_precede_dispatch(conv: Conversation, app_state: Any) -> bool:
    """
    Whether delivering a message to *conv* may first have to wake a managed sandbox.

    A managed sandbox that was paused, expired or lost takes seconds to
    provision again, so the message must be stored and acknowledged before
    that wait.

    :param conv: The session row.
    :param app_state: ``request.app.state`` — supplies the host store.
    :returns: ``True`` when the session's host is a managed sandbox.
    """
    host_store = getattr(app_state, "host_store", None)
    if host_store is None:
        return False
    if conv.host_id is None:
        return False
    host = await asyncio.to_thread(host_store.get_host, conv.host_id)
    return host is not None and host.sandbox_provider is not None


def cancel_deferred_message_dispatch(session_id: str) -> list[str]:
    """
    Cancel *session_id*'s early-acknowledged dispatches: the person stopped the turn.

    The messages stay stored; they never reach the runner.

    :param session_id: Session/conversation identifier.
    :returns: The stored item ids whose dispatch was cancelled.
    """
    cancelled: list[str] = []
    for task, item_id in tuple(_deferred_dispatch_items.get(session_id, {}).items()):
        if task.done():
            continue
        task.cancel()
        if item_id is not None:
            cancelled.append(item_id)
    return cancelled


def schedule_deferred_message_dispatch(
    session_id: str,
    dispatch: Callable[[], Awaitable[object]],
    on_error: Callable[[Exception], Awaitable[object]],
    *,
    item_id: str | None = None,
) -> None:
    """
    Run a stored message's slow runner wake and forward after the HTTP ack.

    The message is already persisted, so the person's send is never lost: when
    *dispatch* fails, *on_error* records the failure as the turn's outcome.
    Dispatches of one session run strictly in the order they were scheduled,
    and the session reads as running from now until each finishes.

    :param session_id: Session/conversation identifier.
    :param dispatch: Wakes the runner and forwards the stored message.
    :param on_error: Surfaces a failed *dispatch* to the session.
    :param item_id: The stored message, for :func:`cancel_deferred_message_dispatch`.
    """
    _deferred_dispatch_pending[session_id] = _deferred_dispatch_pending.get(session_id, 0) + 1
    in_flight = contextlib.ExitStack()
    in_flight.enter_context(_mark_dispatch_in_flight(session_id))
    lock = _deferred_dispatch_locks.setdefault(session_id, asyncio.Lock())

    async def _run() -> None:
        try:
            async with lock:
                try:
                    await dispatch()
                except Exception as exc:  # noqa: BLE001 - any failure becomes the turn failure
                    _logger.warning(
                        "Deferred message dispatch failed for session=%s",
                        session_id,
                        exc_info=True,
                        extra={"session_id": session_id},
                    )
                    await on_error(exc)
        except Exception:  # noqa: BLE001 - a task nobody awaits must not die silently
            _logger.exception(
                "Could not record a failed message dispatch for session=%s",
                session_id,
                extra={"session_id": session_id},
            )
        finally:
            in_flight.close()
            remaining = _deferred_dispatch_pending.get(session_id, 1) - 1
            if remaining > 0:
                _deferred_dispatch_pending[session_id] = remaining
            else:
                _deferred_dispatch_pending.pop(session_id, None)
                _deferred_dispatch_locks.pop(session_id, None)

    task = asyncio.create_task(_run(), name=f"deferred-message-dispatch-{session_id}")
    _deferred_dispatch_tasks.add(task)
    _deferred_dispatch_items.setdefault(session_id, {})[task] = item_id

    def _forget(done: asyncio.Task[None]) -> None:
        _deferred_dispatch_tasks.discard(done)
        items = _deferred_dispatch_items.get(session_id)
        if items is not None:
            items.pop(done, None)
            if not items:
                _deferred_dispatch_items.pop(session_id, None)

    task.add_done_callback(_forget)


def _session_is_starting(conversation_id: str) -> bool:
    """
    Whether a session has a message waiting on a runner that has not taken it.

    :param conversation_id: Session/conversation identifier.
    :returns: ``True`` while a dispatch is in flight or a native message is
        parked in :mod:`omnigent.runtime.pending_inputs`.
    """
    return _dispatch_in_flight.get(conversation_id, 0) > 0 or pending_inputs.has_pending(
        conversation_id
    )


def _list_status_with_starting(
    status: Literal["idle", "running", "failed"],
    conversation_id: str,
) -> Literal["idle", "running", "failed"]:
    """
    Promote an idle session with a message still waiting on its runner to running.

    :param status: Rolled-up list status for the session.
    :param conversation_id: Session/conversation identifier.
    :returns: ``"running"`` for a booting session, else *status* unchanged.
    """
    if status == "idle" and _session_is_starting(conversation_id):
        return "running"
    return status


async def _dispatch_session_event_to_runner(*args: Any, **kwargs: Any) -> Any:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    session_id = kwargs.get("session_id", args[0] if args else None)
    if not isinstance(session_id, str):
        return await _facade._dispatch_session_event_to_runner(*args, **kwargs)
    with _mark_dispatch_in_flight(session_id):
        return await _facade._dispatch_session_event_to_runner(*args, **kwargs)


async def _dispatch_session_event_to_runner_impl(
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    runner_client: httpx.AsyncClient,
    *,
    agent_name: str | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    has_mcp_servers: bool = False,
    created_by: str | None = None,
    host_store: HostStore | None = None,
    persisted_item: ConversationItem | None = None,
    app_state: Any = None,
) -> _SessionEventDispatchResult:
    """
    Persist an item-event AP-side and forward it to the bound runner.

    Persist-before-forward (invariant I1): the item is stored, then forwarded
    via the harness's ``/events`` scaffold.

    :param session_id: Session/conversation identifier.
    :param conv: Conversation row for *session_id*.
    :param body: Validated event from the client.
    :param conversation_store: Used to persist the item.
    :param runner_client: The session's runner client, already
        resolved by the caller via :func:`_get_runner_client`.
    :param agent_name: Human-readable agent name for the ``model`` field.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references before forwarding.
    :param artifact_store: Optional binary store for the same.
    :param has_mcp_servers: ``True`` when the agent spec declares at
        least one MCP server. Forwarded to the runner as the
        ``has_mcp_servers`` hint. ``False`` by default.
    :param created_by: Authenticated identity of the posting actor,
        e.g. ``"alice@example.com"``, recorded on the persisted item.
    :param host_store: Host registrations, read only to learn whether this
        session's harness is AI-Gateway-backed (which router may route it).
        ``None`` reads as unknown, which counts as backed — the same posture
        an older host row gets.
    :param persisted_item: The user item when the caller already stored it
        before waking the runner; it is forwarded instead of appending a
        second copy.
    :returns: A :class:`_SessionEventDispatchResult` carrying the persisted
        item id.
    """
    forwarded = await _forward_event_to_runner(
        session_id,
        conv,
        body,
        conversation_store,
        runner_client,
        agent_name=agent_name,
        file_store=file_store,
        artifact_store=artifact_store,
        has_mcp_servers=has_mcp_servers,
        created_by=created_by,
        host_store=host_store,
        persisted_item=persisted_item,
        app_state=app_state,
    )
    return _SessionEventDispatchResult(
        item_id=forwarded.item_id, pending_id=None, turn=forwarded.turn
    )


# Sized to the runner liveness lease so the relay give-up, the disconnect
# timer, and the liveness-driven sidebar agree on when a dropped runner is
# gone; a crash is reported separately by the daemon and never waits this out.
RUNNER_DISCONNECT_GRACE_S: float = float(RUNNER_LIVENESS_TTL_S)
# Delay between relay stream reconnect attempts inside the grace window.
_RELAY_RETRY_INTERVAL_S: float = 0.5
# Session statuses that mean a turn was in flight. A runner going away
# only interrupts work in one of these states; from any other state the
# departure is a benign disconnect, carried by liveness rather than a
# failure. ``waiting`` counts because the turn's background work (shells,
# sub-agents) outlives the turn and dies with the runner.
_MID_TURN_STATUSES = ("running", "waiting")


class _RelayTransportLost(Exception):
    """Runner stream transport dropped mid-relay.

    :param intentional: Whether the session carried the intentional-stop
        marker when the transport dropped, snapshotted before the relay
        teardown consumes it.
    """

    def __init__(self, *, intentional: bool) -> None:
        super().__init__("runner stream transport lost")
        self.intentional = intentional


def _relinquish_session_live_state(session_id: str) -> None:
    """Drop local live state for a session now owned by another replica."""
    _session_status_cache.pop(session_id, None)
    _session_active_response_cache.pop(session_id, None)
    session_live_state.forget_live_status(session_id)


def _runner_stamp_is_live_elsewhere(
    *,
    stamp: int | None,
    reference_stamp: int | None,
) -> bool:
    """Return whether *stamp* is fresh evidence written by another replica."""
    return (
        stamp is not None
        and runner_seen_is_fresh(stamp)
        and (reference_stamp is None or stamp > reference_stamp)
    )


def _runner_live_on_another_replica_from_conversations(
    conversations: Sequence[Conversation],
    runner_id: str,
    reference_stamp: int | None,
) -> bool:
    """Check already-loaded runner-bound rows for a fresher replica's stamp."""
    return any(
        conv.runner_id == runner_id
        and _runner_stamp_is_live_elsewhere(
            stamp=conv.runner_last_seen,
            reference_stamp=reference_stamp,
        )
        for conv in conversations
    )


async def _runner_drop_interrupted_turn(
    session_id: str,
    conversation_store: ConversationStore,
) -> bool:
    """
    Report whether a departing runner caught this session mid-turn.

    Prefers the relay-fed cache — the replica holding the runner's tunnel
    saw the turn edges — and falls back to the row for a session whose live
    state was published before a restart, so a deploy mid-turn does not
    downgrade a real interruption to a benign one.

    An unreadable or missing row leaves the question open, and this runs
    inside the disconnect handler: answering "not mid-turn" there would
    both swallow the failure and let the error escape the handler, killing
    the relay without publishing anything — the silent truncation the
    failed status exists to prevent. So an indeterminate answer reports the
    drop, as the ungated relay always did.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store used to read the durable live status.
    :returns: ``True`` when a turn was in flight
        (:data:`_MID_TURN_STATUSES`) or the state is indeterminate.
    """
    cached = _session_status_cache.get(session_id)
    if cached is not None:
        return cached in _MID_TURN_STATUSES
    try:
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    except Exception:  # noqa: BLE001 — an unreadable row must not kill the relay
        _logger.warning(
            "Relay: live-status read failed for session=%s; reporting the drop",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return True
    if conv is None:
        return True
    return conv.live_status in _MID_TURN_STATUSES


async def _relay_runner_live_elsewhere(
    session_id: str,
    conversation_store: ConversationStore,
) -> bool:
    """
    Resolve this relay's bound runner and check it against another replica.

    The active relay's runner id is normally known from its own
    ``_runner_relay_tasks`` registration; a caller that drives
    :func:`_relay_runner_stream` directly (tests, or a code path
    bypassing :func:`_ensure_runner_relay`) has no such entry, so fall
    back to the session row's binding. One row read serves both the
    binding and the liveness stamp, keeping this path bounded.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store used to read the session row.
    :returns: ``True`` when the bound runner is confirmed live on
        another replica; ``False`` when unbound, unreadable, or not.
    """
    try:
        row = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    except Exception:  # noqa: BLE001 — fall through to the mid-turn check instead
        _logger.warning(
            "Relay: session-row lookup failed for session=%s",
            session_id,
            exc_info=True,
            extra={"session_id": session_id},
        )
        return False
    if row is None:
        return False
    handle = _runner_relay_tasks.get(session_id)
    runner_id = handle.runner_id if handle is not None else row.runner_id
    if runner_id is None:
        return False
    reference_stamp = session_live_state.last_liveness_stamp(runner_id)
    return _runner_live_on_another_replica_from_conversations([row], runner_id, reference_stamp)


async def _relay_runner_stream(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    ready: asyncio.Event | None = None,
) -> None:
    """
    Run the runner-stream relay, riding out transient tunnel drops.

    Transport drops from ingress recycles and sleep-wake reconnects
    re-register the runner within :data:`RUNNER_DISCONNECT_GRACE_S`, so a
    lost stream retries inside that window instead of failing the
    session. An intentional Stop exits quietly at once.

    Past the grace the runner is genuinely gone — unless this server is the
    one shutting down (:func:`omnigent.server.shutdown_state.server_shutting_down`):
    it closed the tunnel itself, so the loss says nothing about the runner
    and no session is failed. Otherwise only a session it caught mid-turn
    (:func:`_runner_drop_interrupted_turn`) gets the
    ``failed`` status and durable ``runner_disconnected`` labels — the same
    rule :func:`_mark_runner_sessions_offline_impl` applies to the runner's
    other sessions. An idle session had no work to interrupt, so it stays
    idle and the disconnect surfaces through liveness instead.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_client: HTTP client pointed at the runner.
    :param conversation_store: Store for persisting conversation items
        extracted from the runner's SSE stream.
    :param ready: Optional event set once the runner stream emits its
        ready heartbeat; see :func:`_relay_runner_stream_once`.
    """
    loop = asyncio.get_running_loop()
    deadline: float | None = None
    outage_started = 0.0
    retries = 0
    # One Round recorder across reconnects: a turn the runner keeps streaming after the tunnel
    # comes back is still recorded, and one whose stream is gone is ended from the store.
    round_record = rounds.RelayRound(session_id, conversation_store)
    while True:
        started = loop.time()
        try:
            await _relay_runner_stream_once(
                session_id,
                runner_client,
                conversation_store,
                ready,
                round_record,
            )
            round_record.lost()
            return
        except _RelayTransportLost as lost:
            now = loop.time()
            # An attempt that streamed longer than the grace was a live
            # tunnel dropping anew — give the new outage a fresh window.
            if deadline is None or now - started > RUNNER_DISCONNECT_GRACE_S:
                deadline = now + RUNNER_DISCONNECT_GRACE_S
                outage_started = now
                retries = 0
                _logger.info(
                    "Relay: runner transport lost for session=%s (intentional=%s, grace=%.1fs)",
                    session_id,
                    lost.intentional,
                    RUNNER_DISCONNECT_GRACE_S,
                    extra=debug_event(
                        "runner_stream_transport_lost",
                        session_id=session_id,
                        intentional_stop=lost.intentional,
                        cached_session_status=_session_status_cache.get(session_id),
                        grace_s=RUNNER_DISCONNECT_GRACE_S,
                    ),
                )
            if not lost.intentional and now + _RELAY_RETRY_INTERVAL_S < deadline:
                retries += 1
                _logger.info(
                    "Relay: runner transport lost for session=%s; retrying for %.1fs",
                    session_id,
                    deadline - now,
                    extra={"session_id": session_id},
                )
                # An absent runner: park until it re-registers rather than
                # re-opening the stream every interval, so an outage costs one
                # attempt, resolved the instant the tunnel is back. A runner
                # that is still registered failed the stream for another
                # reason (an HTTP error), so keep the interval backoff: the
                # waiter would return at once and spin. A client without a
                # tunnel transport (in-process tests) also keeps the interval.
                transport = getattr(runner_client, "_transport", None)
                wait = getattr(transport, "wait_for_runner", None)
                if wait is None or await wait(deadline - now):
                    await asyncio.sleep(_RELAY_RETRY_INTERVAL_S)
                continue
            if lost.intentional:
                decision = "intentional_stop"
            elif shutdown_state.server_shutting_down():
                decision = "server_shutdown"
            elif await _relay_runner_live_elsewhere(session_id, conversation_store):
                decision = "live_elsewhere"
            elif await _runner_drop_interrupted_turn(session_id, conversation_store):
                decision = "failed_mid_turn"
            else:
                decision = "idle_no_failure"
            # One row per outage outcome: which branch below fired, how long the
            # runner was gone against the grace, and how many retries it got.
            _logger.warning(
                "Relay: runner transport lost for session=%s (%s)",
                session_id,
                decision,
                exc_info=True,
                extra=debug_event(
                    "runner_stream_disconnected",
                    session_id=session_id,
                    intentional_stop=lost.intentional,
                    cached_session_status=_session_status_cache.get(session_id),
                    decision=decision,
                    grace_s=RUNNER_DISCONNECT_GRACE_S,
                    outage_s=round(now - outage_started, 3),
                    retries=retries,
                ),
            )
            if decision not in ("server_shutdown", "live_elsewhere"):
                # No relay of this process streams the session's turn again.
                round_record.lost()
            if decision == "intentional_stop":
                # User clicked Stop: the Stop handler brought this runner's
                # tunnel down on purpose (see _stop_session_host_runner), so
                # the drop is expected — not a failure. Publish a quiet idle
                # and clear any error label so the chat and sidebar settle
                # to a stopped state instead of rendering
                # "Error · runner_disconnected". The one-shot marker was
                # already consumed by the relay teardown, so a genuine later
                # disconnect surfaces normally.
                _publish_status(session_id, "idle")
                await _persist_session_status_error_labels(
                    session_id,
                    None,
                    conversation_store,
                )
            elif decision == "server_shutdown":
                # This server closed the tunnel on its way down; the runner is
                # reachable, just not by a process that stopped listening. The
                # replacement server re-adopts it on reconnect.
                _logger.info(
                    "Relay: transport lost during server shutdown for session=%s; "
                    "not failing the turn",
                    session_id,
                    extra={"session_id": session_id},
                )
            elif decision == "live_elsewhere":
                # The runner re-tunnelled to another replica before this one
                # noticed the drop; that replica now owns the turn.
                _relinquish_session_live_state(session_id)
                _logger.info(
                    "Relay: runner live on another replica for session=%s; no failure to report",
                    session_id,
                    extra={"session_id": session_id},
                )
            elif decision == "idle_no_failure":
                # The runner went away while this session sat idle (host
                # asleep, host restart, `omnigent host` stopped). Nothing was
                # interrupted, so there is no error to report: publishing one
                # lit a red "connection to the host dropped" banner over a
                # session that had simply finished its last turn. The absence
                # is already carried by liveness (``clear_runner_liveness``),
                # which drives the reconnect affordance. Stay silent — no
                # status edge, and no clearing of labels either, so a genuine
                # earlier failure keeps its error.
                _logger.info(
                    "Relay: runner gone for idle session=%s; no failure to report",
                    session_id,
                    extra={"session_id": session_id},
                )
            else:
                # Publish a failed status so the client's SSE stream sees a
                # clean error event instead of silent truncation (#1114).
                disconnect_error = ErrorDetail(
                    code="runner_disconnected",
                    message="Runner disconnected unexpectedly.",
                )
                _publish_status(
                    session_id,
                    "failed",
                    disconnect_error,
                    failure_origin="runner_disconnected_mid_turn",
                )
                # Persist the disconnect cause as durable labels so the
                # distinction survives into snapshots and child-session
                # summaries. Without this the relay-fed cache only carries a
                # generic ``failed`` and ``last_task_error`` is dropped,
                # leaving the UI unable to tell a benign runner disconnect
                # from a real task failure (Option B: render a "Disconnected"
                # pill, not the red "Failed" pill). Cleared on the next
                # ``running`` edge by the session.status handler, exactly
                # like other failure labels.
                await _persist_session_status_error_labels(
                    session_id,
                    disconnect_error,
                    conversation_store,
                )
            return


async def _relay_runner_stream_once(
    session_id: str,
    runner_client: httpx.AsyncClient,
    conversation_store: ConversationStore,
    ready: asyncio.Event | None = None,
    round_record: rounds.RelayRound | None = None,
) -> None:
    """
    Subscribe to the runner's SSE stream and relay events locally.

    One connection attempt: transport loss raises
    :class:`_RelayTransportLost` for the :func:`_relay_runner_stream`
    supervisor, which retries inside the disconnect grace and owns the
    terminal failure / quiet-stop handling.

    Long-lived background task that opens
    ``GET /v1/sessions/{id}/stream`` on the runner and publishes
    each event to the local ``session_stream`` pub-sub. Also
    updates ``_session_status_cache`` from turn lifecycle events
    and persists conversation items (assistant messages, tool
    calls) to the conversation store as they arrive.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_client: HTTP client pointed at the runner.
    :param conversation_store: Store for persisting conversation
        items extracted from the runner's SSE stream.
    :param ready: Optional event set once the runner stream emits its
        ready heartbeat, proving AP's runner-side no-replay subscriber
        slot is registered. ``None`` is accepted for direct unit tests
        that exercise relay parsing/persistence without asserting on
        startup readiness.
    :param round_record: The session's Round recorder, kept across the
        supervisor's reconnects; ``None`` makes one for this attempt.
    """
    text_acc: list[str] = []
    current_response_id: str | None = None
    # Whether this is a Super Chat (read once, at its first turn): its running edges name the
    # turn's Activity (``activity_id``) so the strip can show steps before the first word.
    super_chat: bool | None = None
    # Model/agent label from the turn header, stamped on text segments
    # flushed at tool-call boundaries (the boundary event carries no model).
    current_model: str | None = None
    failure_agent_name: str | None = None
    # Wall-clock time when the current turn's response.in_progress arrived,
    # used to compute per-turn latency in TurnEndEvent.
    _turn_start_s: float | None = None
    # Map tool call_id → response_id so a function_call_output that
    # arrives after a new response.in_progress (different response_id)
    # still pairs with its matching function_call. Without this, the
    # web UI's block stream clears its pending-tool state on the
    # response_id transition and the tool card spinner never resolves.
    tool_call_response_ids: dict[str, str] = {}
    # The person's wait per turn, from their message (ADR 0026).
    turn_clock = TurnClock(session_id)
    # What the turns of a Superchat session do, recorded as their Round's events (ADR 0029).
    if round_record is None:
        round_record = rounds.RelayRound(session_id, conversation_store)
    _logger.info(
        "Relay: connecting to runner GET /stream for session=%s",
        session_id,
        extra={"session_id": session_id},
    )

    # Read timeout: 3x the runner's session-stream heartbeat interval
    # (15s). Between turns the runner emits ``session.heartbeat`` every
    # 15s to keep proxies from dropping the idle connection. If 3
    # consecutive heartbeats are missed (45s), the connection is likely
    # dead — surface it so the supervising retry loop reconnects (or,
    # past the grace, fails the session). ``connect`` stays at httpx's
    # default (5s); ``write``/``pool`` are not rate-limiting here.
    _relay_timeout = httpx.Timeout(connect=5.0, read=45.0, write=None, pool=None)
    try:
        async with runner_client.stream(
            "GET",
            f"/v1/sessions/{session_id}/stream",
            timeout=_relay_timeout,
        ) as resp:
            resp.raise_for_status()
            _logger.info(
                "Relay: connected to runner GET /stream for session=%s",
                session_id,
                extra=debug_event("runner_stream_connected", session_id=session_id),
            )
            buffer = ""
            heartbeat_seen = False
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    data_line = next(
                        (ln for ln in frame.splitlines() if ln.startswith("data:")),
                        None,
                    )
                    if data_line is None:
                        continue
                    payload = data_line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    evt_type = event.get("type", "")
                    # The runner emits session.status events
                    # directly.
                    # Re-publish via _publish_status so the event
                    # gets the conversation_id field required by
                    # SessionStatusEvent's schema. The cache write
                    # happens inside _publish_status itself.
                    # Runner-emitted keepalive — consumed to reset the
                    # read timeout; not forwarded to the session stream
                    # (the Omnigent subscriber generates its own heartbeats).
                    if evt_type == "session.heartbeat":
                        if not heartbeat_seen:
                            heartbeat_seen = True
                            _logger.info(
                                "Relay: runner stream ready for session=%s",
                                session_id,
                                extra=debug_event("runner_stream_ready", session_id=session_id),
                            )
                        if ready is not None:
                            ready.set()
                        continue

                    # The terminal of a turn the engine stopped: recorded stopped, whatever
                    # status the executor gave it (ADR 0029).
                    stopped_turn = (
                        evt_type in _TERMINAL_RESPONSE_EVENT_TYPES
                        and session_id in _interrupt_fenced_sessions
                    )
                    # Stopped turn: drop its trailing response.* output (no
                    # forward, no persist) but keep text_acc — the pre-stop
                    # narration the user watched persists at the terminal flush.
                    if session_id in _interrupt_fenced_sessions:
                        if evt_type == "session.status" and event.get("status") == "running":
                            _interrupt_fenced_sessions.discard(session_id)
                        elif evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                            # Terminal proves the stopped turn is over (completed =
                            # the stop lost the race); process it normally.
                            _interrupt_fenced_sessions.discard(session_id)
                        elif (
                            evt_type.startswith("response.")
                            and evt_type not in _FENCE_EXEMPT_EVENT_TYPES
                        ):
                            continue

                    if evt_type == "session.status":
                        status = event.get("status", "")
                        if status == "idle":
                            # A turn whose stream this relay lost ends from the store.
                            round_record.lost()
                        if status:
                            # Forward the runner's failure detail on a
                            # ``failed`` transition so a SETUP-phase
                            # failure (which never emits response.failed)
                            # surfaces a real error message downstream
                            # instead of ending the turn silently.
                            raw_err = event.get("error")
                            status_error = (
                                ErrorDetail.model_validate(raw_err)
                                if isinstance(raw_err, dict)
                                else None
                            )
                            if status == "failed" and status_error is not None:
                                await _persist_session_status_error_labels(
                                    session_id,
                                    status_error,
                                    conversation_store,
                                    agent_name=failure_agent_name,
                                )
                            elif status == "running":
                                failure_agent_name = None
                                await _persist_session_status_error_labels(
                                    session_id,
                                    None,
                                    conversation_store,
                                )
                                # A new turn proves the runner is live again, so
                                # a prior Stop that never dropped the tunnel must
                                # not leave the intentional-stop marker to swallow
                                # this turn's genuine disconnect. Fence-independent
                                # (the fence may already be cleared by a terminal
                                # stop event), so it fires on every running edge.
                                _intentional_stop_sessions.discard(session_id)
                            # PTY-activity status is a UI signal only. Terminal
                            # sub-agent delivery rides the Stop/StopFailure hook
                            # via external_session_status (the codex-shared path)
                            # — the PTY idle oscillates on mid-turn lulls and
                            # would deliver a premature, lock-out completion.
                            raw_blocked_on = event.get("blocked_on")
                            raw_response_id = event.get("response_id")
                            # The runner finishing a turn the server had written
                            # off as a runner drop proves the drop was transient:
                            # honor the completion and clear the disconnect cause.
                            # A genuine task failure keeps its sticky ``failed``
                            # (the guard only clears a ``runner_disconnected`` label).
                            if (
                                status == "idle"
                                and _session_status_cache.get(session_id) == "failed"
                            ):
                                try:
                                    await _publish_runner_recovered_status(
                                        session_id,
                                        conversation_store,
                                        require_disconnect_code=True,
                                    )
                                except Exception:  # noqa: BLE001 — a read error must not kill the relay
                                    # Fail soft: the session keeps its existing
                                    # failed status (the sticky rule below drops
                                    # this idle) and the relay keeps streaming.
                                    _logger.warning(
                                        "Relay: disconnect-recovery check failed for session=%s; "
                                        "keeping failed status",
                                        session_id,
                                        exc_info=True,
                                        extra={"session_id": session_id},
                                    )
                                else:
                                    continue
                            _publish_status(
                                session_id,
                                status,
                                status_error,
                                # The runner names the turn a failed edge closes so
                                # the web can fold it into that turn's error card.
                                response_id=(
                                    raw_response_id
                                    if isinstance(raw_response_id, str) and raw_response_id
                                    else None
                                ),
                                failure_origin="relayed_runner_status",
                                activity_id=(
                                    turn_activity_id(session_id, current_response_id)
                                    if super_chat
                                    and current_response_id is not None
                                    and status in ("running", "waiting")
                                    else None
                                ),
                                paused=event.get("paused") is True,
                                blocked_on=(
                                    raw_blocked_on
                                    if isinstance(raw_blocked_on, str) and raw_blocked_on
                                    else None
                                ),
                                turn=(
                                    event["turn"]
                                    if isinstance(event.get("turn"), int)
                                    and not isinstance(event.get("turn"), bool)
                                    else None
                                ),
                            )
                        if status == "running":
                            text_acc.clear()
                            # A new turn invalidates any deny marker a prior
                            # turn left un-consumed (e.g. its terminal event
                            # never reached the relay across a reconnect).
                            _llm_response_denied_turns.pop(session_id, None)
                        continue

                    # Terminal spin-up status from the runner's auto-create
                    # path. Re-publish via _publish_terminal_pending so the
                    # event carries conversation_id and the cache write
                    # (read by the snapshot) stays coherent with the stream.
                    if evt_type == "session.terminal_pending":
                        # Use ``is True`` (not bool()) so a malformed frame
                        # with a string like ``"false"`` can't strand the
                        # spinner on — the runner always sends a real bool.
                        _publish_terminal_pending(
                            session_id,
                            event.get("pending") is True,
                        )
                        continue

                    # Track the turn's response_id from lifecycle
                    # events so persisted items share one id.
                    if evt_type == "response.in_progress":
                        _turn_start_s = time.monotonic()
                        resp_obj = event.get("response", {})
                        _rid = resp_obj.get("id")
                        if isinstance(_rid, str) and _rid:
                            current_response_id = _rid
                            if super_chat is None:
                                _conv = await asyncio.to_thread(
                                    conversation_store.get_conversation, session_id
                                )
                                super_chat = _conv is not None and is_super_chat(_conv.labels)
                            if super_chat:
                                _publish_status(
                                    session_id,
                                    "running",
                                    response_id=_rid,
                                    activity_id=turn_activity_id(session_id, _rid),
                                )
                        _model = resp_obj.get("model")
                        failure_agent_name = _model if isinstance(_model, str) and _model else None
                        if isinstance(_model, str) and _model:
                            current_model = _model
                        await turn_clock.start(conversation_store)
                    turn_clock.observe(event)
                    await round_record.observe(event, stopped=stopped_turn)
                    if evt_type == rounds.TOOL_INPUT_DELTA:
                        continue  # recorded only; clients see the call when it is complete

                    # Accumulate response-scoped (scaffold) text deltas for
                    # persistence. Native message-scoped deltas (with a
                    # message_id) persist via their own output_item.done(message),
                    # so buffering them here would double-persist. Guard on
                    # non-empty str (like inflight_text.record_publish) so a
                    # malformed delta can't break the later "".join(text_acc).
                    if evt_type == "response.output_text.delta" and not event.get("message_id"):
                        _delta = event.get("delta")
                        if isinstance(_delta, str) and _delta:
                            text_acc.append(_delta)

                    # Track tool call_id → response_id so a
                    # function_call_output that arrives under a later
                    # response still pairs with its call.  Done
                    # before _extract_persistent_item_from_sse because
                    # the parse may fail (serialization alias mismatch)
                    # while the mapping is still needed for the live
                    # event patch below.
                    _raw_item = event.get("item")
                    _item = _raw_item if isinstance(_raw_item, dict) else {}
                    _item_type = _item.get("type")
                    _item_call_id = _item.get("call_id")
                    _persist_rid: str | None
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and isinstance(_item_call_id, str)
                        and current_response_id is not None
                    ):
                        tool_call_response_ids[_item_call_id] = current_response_id

                    # For function_call_output, use the response_id
                    # of the matching function_call so the web UI
                    # pairs them in the same bubble even when a new
                    # response.in_progress has already overwritten
                    # current_response_id.
                    if (
                        _item_type == "function_call_output"
                        and isinstance(_item_call_id, str)
                        and _item_call_id in tool_call_response_ids
                    ):
                        _persist_rid = tool_call_response_ids[_item_call_id]
                    else:
                        _persist_rid = current_response_id

                    # Flush buffered narration as its own message BEFORE the
                    # function_call it preceded, so the transcript interleaves
                    # [text, tool, text, tool] instead of pooling a turn's text
                    # after its tool calls (tools-above-text + run-on on reload).
                    if (
                        _item_type == "function_call"
                        and _item.get("status") == "completed"
                        and text_acc
                    ):
                        # A deny recorded by a mid-turn LLM_RESPONSE evaluation
                        # (harnesses that gate per LLM call) marks this segment
                        # as denied content; consume it the same way the
                        # terminal flush does.
                        _boundary_deny = _llm_response_denied_turns.pop(session_id, None)
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            current_model,
                            deny_reason=_boundary_deny,
                            # Gate each segment as it becomes durable: without
                            # this a RESPONSE-phase policy is bypassed whenever
                            # the model emits the offending text before a tool
                            # call (the segment persists here, ahead of the
                            # terminal-flush evaluation).
                            evaluate_response_phase=_boundary_deny is None,
                        )
                        # A failed append leaves text_acc for retry — re-arm
                        # the marker so the retry persists the sentinel.
                        if _boundary_deny is not None and text_acc:
                            _llm_response_denied_turns[session_id] = _boundary_deny

                    conv_item = _extract_persistent_item_from_sse(
                        event,
                        response_id=_persist_rid,
                    )
                    if conv_item is not None:
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            conv_item,
                        )

                    # On ANY terminal event (not just completed), persist the
                    # final text segment: narration streamed before a failure /
                    # cancel must survive reload too, ordered BEFORE the error
                    # item below and before the publish pops the in-flight
                    # replay entry (flush → publish keeps reload == live).
                    # NB: fenced deltas never reached text_acc (the fence's
                    # continue precedes accumulation), so a post-Stop flush
                    # carries pre-stop narration only.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _resp_obj = event.get("response")
                        _resp_model = (
                            _resp_obj.get("model") if isinstance(_resp_obj, dict) else None
                        )
                        _final_model = (
                            _resp_model
                            if isinstance(_resp_model, str) and _resp_model
                            else current_model
                        )
                        # An LLM_RESPONSE DENY recorded by the policy-evaluate
                        # route means this buffered text is the denied content;
                        # the flush persists the deny sentinel instead. Popped
                        # here (not merely read) so the marker can never bleed
                        # into a later turn.
                        #
                        # Scope: the LLM_RESPONSE verdict exists only after the
                        # full stream, so it can sanitize only this final
                        # buffered segment; text persisted at earlier tool-call
                        # boundaries is already durable (the turn still fails
                        # with a persisted deny error item). Segment-complete
                        # durable gating is the RESPONSE phase's job — the
                        # boundary flush above evaluates it for every segment.
                        _deny_reason = _llm_response_denied_turns.pop(session_id, None)
                        await _flush_relay_text(
                            conversation_store,
                            session_id,
                            text_acc,
                            current_response_id,
                            _final_model,
                            deny_reason=_deny_reason,
                            # Terminal flush is the only place the runner
                            # topology can evaluate the spec's RESPONSE-phase
                            # output policies over the final assistant text.
                            evaluate_response_phase=_deny_reason is None,
                        )
                        # A failed append leaves text_acc intact for a retry
                        # at a later flush — re-arm the marker so the retry
                        # still persists the sentinel, not the denied text.
                        if _deny_reason is not None and text_acc:
                            _llm_response_denied_turns[session_id] = _deny_reason

                    error_item = _error_item_from_sse(
                        event,
                        response_id=current_response_id,
                    )
                    if error_item is not None:
                        await _relay_persist_error_once(
                            conversation_store,
                            session_id,
                            error_item,
                        )

                    # Persist resource lifecycle events
                    # (session.resource.created / .deleted) emitted by
                    # agent-tool terminal launches/closes so reconnecting
                    # clients rediscover the resource in the snapshot.
                    # The live publish below already updates connected
                    # clients.
                    resource_item = _resource_event_item_from_sse(session_id, event)
                    if (
                        resource_item is not None
                        and event.get("persist_resource_event") is not False
                    ):
                        resource_data = resource_item.data
                        await _relay_persist(
                            conversation_store,
                            session_id,
                            resource_item,
                        )
                        # Self-heal the spin-up flag: a created terminal is
                        # authoritative proof the session is no longer
                        # "starting up", so clear it even if the runner's
                        # auto-create finally was skipped (e.g. hard kill
                        # between launch and clear). Only fire on a real
                        # state change to avoid redundant stream traffic.
                        if (
                            isinstance(resource_data, ResourceEventData)
                            and resource_data.event_type == "session.resource.created"
                            and resource_data.resource_type == "terminal"
                            and _session_terminal_pending_cache.get(session_id, False)
                        ):
                            _publish_terminal_pending(session_id, False)

                    # Intelligent-model-router decision emitted by the runner's
                    # cost advisor at turn start. Persist as a display-only
                    # transcript item (arrival order = BEFORE the assistant
                    # output), then re-publish the live event carrying the
                    # store-assigned id so the live chip and a turn-start
                    # snapshot refetch dedup by the same id. Handled
                    # exclusively here (persist + publish + continue) so the
                    # raw, id-less runner event is not also forwarded below.
                    routing_item = _routing_decision_item_from_sse(event)
                    if routing_item is not None:
                        # Persist failure must NOT suppress the live chip
                        # (the owner's hard requirement: the pick shows the
                        # moment the turn starts). On a store error, log and
                        # still publish the live event — id-less, so a later
                        # snapshot can't dedup it, but a missing reload chip
                        # beats no chip at all.
                        try:
                            persisted = await asyncio.to_thread(
                                conversation_store.append, session_id, [routing_item]
                            )
                            _persisted_id: str | None = persisted[0].id if persisted else None
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "Relay: routing_decision persist failed for session=%s; "
                                "publishing the live chip without a durable id",
                                session_id,
                                extra={"session_id": session_id},
                            )
                            _persisted_id = None
                        session_stream.publish(
                            session_id,
                            {
                                **event,
                                "item": {**event["item"], "id": _persisted_id},
                            },
                        )
                        continue

                    # Accumulate LLM token usage from the harness
                    # response so policy callables can read
                    # event["context"]["usage"]["total_cost_usd"].
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _terminal_response = event.get("response")
                        if isinstance(_terminal_response, dict):
                            await _persist_relay_reported_model(
                                _terminal_response,
                                session_id,
                                conversation_store,
                            )
                    if evt_type == "response.completed":
                        # Persist the turn's usage (cost + token buckets) so
                        # policy callables can read
                        # event["context"]["usage"]["total_cost_usd"] and the
                        # subtree roll-up below sees the new totals.
                        _accumulate_session_usage(
                            event.get("response", {}),
                            session_id,
                            conversation_store,
                        )
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        _turn_status = evt_type.split(".", 1)[1]  # "completed" etc.
                        _latency_ms: float | None = None
                        if _turn_start_s is not None:
                            _latency_ms = (time.monotonic() - _turn_start_s) * 1000
                        _turn_start_s = None
                        _response = event.get("response")
                        _resp_usage = (
                            _response.get("usage") or {} if isinstance(_response, dict) else {}
                        )
                        _turn_in = _resp_usage.get("input_tokens")
                        _turn_out = _resp_usage.get("output_tokens")
                        _turn_cost = _resp_usage.get("cost_usd")
                        _turn_model: str | None = _resp_usage.get("model") or current_model or None
                        _tel_emit(
                            _TelTurnEndEvent(
                                installation_id=_get_installation_id(),
                                session_id=session_id,
                                anon_user_id=_tel_current_anon(),
                                # The relay task has no request to carry the
                                # header, and resolving the host would cost a
                                # read per turn end — so the field is skipped.
                                host_installation_id=None,
                                status=_turn_status,
                                latency_ms=_latency_ms,
                                model=_turn_model,
                                input_tokens=(int(_turn_in) if _turn_in is not None else None),
                                output_tokens=(int(_turn_out) if _turn_out is not None else None),
                                cost_usd=(
                                    float(_turn_cost)
                                    if isinstance(_turn_cost, (int, float))
                                    else None
                                ),
                            )
                        )
                        if evt_type == "response.completed":
                            # Persist the context-window indicator labels for an
                            # in-process turn (claude-sdk / pi):
                            # numerator from usage.context_tokens, denominator
                            # from the observed model's window. This is the only
                            # path that gives a model-unpinned claude-sdk session
                            # a context ring — it has no spec model to size the
                            # window from — and mirrors the labels the native
                            # external_session_usage path writes. Empty (a no-op)
                            # for a turn without context_tokens, so native
                            # terminal harnesses (which post their own usage) are
                            # never double-written. Threaded: the label build
                            # (get_model_context_window may do a cold blocking
                            # catalog fetch — offload it off the shared relay
                            # loop, like the snapshot path does) and the DB
                            # label write below.
                            _context_labels = await asyncio.to_thread(
                                _context_labels_from_turn_usage, _resp_usage
                            )
                            if _context_labels:
                                await asyncio.to_thread(
                                    conversation_store.set_labels,
                                    session_id,
                                    _context_labels,
                                )
                            # Between turns: check whether this session's past is due
                            # to be folded into its checkpoint (ADR 0031), in the
                            # background, never awaited here.
                            from omnigent.server.memory_upkeep import schedule_fold

                            schedule_fold(session_id, _resp_usage)
                            # Push the server-computed cost AND token breakdown
                            # to the web client's session indicator, rolled up
                            # over the spawn subtree. The session's own event
                            # carries its SUBTREE total (this conversation + its
                            # sub-agents), and each ancestor gets its own subtree
                            # total on its own stream — so a supervisor's badge
                            # includes its sub-agents and a parent updates live
                            # when a relay sub-agent spends. Mirrors the native
                            # path (_persist_external_session_usage); the roll-up
                            # was wired for native only, but relay agents (e.g.
                            # claude-sdk) need it too. Cost is included only when
                            # priced; the token breakdown rides along whenever any
                            # bucket is recorded (so an unpriced session still
                            # surfaces tokens). context_tokens/window ride on the
                            # same event so the ring updates live. Threaded: store
                            # reads + SSE fan-out.
                            # One row read serves both the subtree sum and
                            # the ancestor publish below (each would
                            # otherwise re-derive the tree root itself).
                            _usage_conv = await asyncio.to_thread(
                                conversation_store.get_conversation,
                                session_id,
                            )
                            _subtree_usage = await asyncio.to_thread(
                                load_session_usage,
                                session_id,
                                conversation_store,
                                root_conversation_id=(
                                    _usage_conv.root_conversation_id
                                    if _usage_conv is not None
                                    else None
                                ),
                            )
                            _subtree_cost = _priced_cost_for_display(_subtree_usage)
                            _usage_by_model = _usage_by_model_for_display(_subtree_usage)
                            if (
                                _subtree_cost is not None
                                or _usage_by_model is not None
                                or _context_labels
                            ):
                                _usage_payload: dict[str, Any] = {
                                    "type": "session.usage",
                                    "conversation_id": session_id,
                                }
                                _ctx_tokens_label = _context_labels.get(
                                    _LAST_CONTEXT_TOKENS_LABEL_KEY
                                )
                                if _ctx_tokens_label is not None:
                                    _usage_payload["context_tokens"] = int(_ctx_tokens_label)
                                _ctx_window_label = _context_labels.get(
                                    _LAST_CONTEXT_WINDOW_LABEL_KEY
                                )
                                if _ctx_window_label is not None:
                                    _usage_payload["context_window"] = int(_ctx_window_label)
                                if _subtree_cost is not None:
                                    _usage_payload["total_cost_usd"] = _subtree_cost
                                if _usage_by_model is not None:
                                    _usage_payload["usage_by_model"] = _usage_by_model
                                session_stream.publish(
                                    session_id,
                                    SessionUsageEvent(**_usage_payload).model_dump(
                                        exclude_none=True
                                    ),
                                )
                                # Ancestors' badges only move on cost/usage, not a
                                # context-only publish — keep the roll-up gated.
                                if _subtree_cost is not None or _usage_by_model is not None:
                                    await asyncio.to_thread(
                                        _publish_subtree_cost_to_ancestors,
                                        conversation_store,
                                        session_id,
                                        _usage_conv,
                                    )

                    # Reset the turn-scoped response_id on any
                    # terminal event so it doesn't leak to the
                    # next turn.
                    if evt_type in _TERMINAL_RESPONSE_EVENT_TYPES:
                        current_response_id = None

                    # Patch the live event's response_id for
                    # function_call_output items whose call_id maps
                    # to a known function_call response_id. This
                    # ensures the web UI's block stream pairs the
                    # tool result with its call in the same bubble.
                    if (
                        evt_type == "response.output_item.done"
                        and isinstance(event.get("item"), dict)
                        and event["item"].get("type") == "function_call_output"
                    ):
                        _live_cid = event["item"].get("call_id")
                        if isinstance(_live_cid, str) and _live_cid in tool_call_response_ids:
                            event = {
                                **event,
                                "item": {
                                    **event["item"],
                                    "response_id": tool_call_response_ids[_live_cid],
                                },
                            }
                    if evt_type == "response.elicitation_request":
                        session_stream.publish(session_id, event)
                        await asyncio.to_thread(
                            _publish_elicitation_request_to_ancestors,
                            conversation_store,
                            session_id,
                            event,
                        )
                        continue
                    if evt_type == "response.elicitation_resolved":
                        session_stream.publish(session_id, event)
                        elicitation_id = event.get("elicitation_id")
                        if isinstance(elicitation_id, str) and elicitation_id:
                            if event.get("reason") == "unanswered":
                                # No retry follows an unanswered park; drop its stash.
                                _pending_policy_ask_writes.pop(elicitation_id, None)
                            await asyncio.to_thread(
                                _publish_elicitation_resolved_to_ancestors,
                                conversation_store,
                                session_id,
                                elicitation_id,
                                event.get("action"),
                                reason=event.get("reason"),
                            )
                        continue
                    session_stream.publish(session_id, event)

    except (httpx.HTTPError, ConnectionError) as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            _logger.warning(
                "Relay: runner GET /stream rejected for session=%s with HTTP %s",
                session_id,
                exc.response.status_code,
                extra=debug_event(
                    "runner_stream_http_rejected",
                    session_id=session_id,
                    http_status=exc.response.status_code,
                ),
            )
        # WSTunnelTransport raises bare ConnectionError on tunnel close;
        # treat the same as HTTPError. The finally below consumes the
        # intentional-stop marker, so snapshot it now for the supervisor's
        # retry-vs-quiet-exit decision.
        raise _RelayTransportLost(intentional=session_id in _intentional_stop_sessions) from exc
    except asyncio.CancelledError:
        raise
    finally:
        _logger.info(
            "Relay: task exiting for session=%s",
            session_id,
            extra=debug_event("runner_stream_closed", session_id=session_id),
        )
        # Drop any in-flight assistant-text entry so a relay that exits
        # WITHOUT a terminal turn event (runner death / tunnel drop
        # mid-turn, or a rebind cancellation) can't strand it forever.
        # Normal turn-ends already clear via record_publish.
        inflight_text.discard(session_id)
        # The intentional-stop marker is consumed by the disconnect handler
        # above on the expected path; discard it here too so a relay that
        # exits some other way (clean [DONE], rebind cancellation) can't
        # leave a stale marker to swallow a later genuine disconnect on the
        # reused per-session relay task.
        _intentional_stop_sessions.discard(session_id)
        # Relay ended (runner dropped/rebound): re-discover runner-backed
        # snapshot overlays next time.
        _invalidate_runner_backed_snapshot_state(session_id)


def _ensure_runner_relay(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start (or replace) the SSE relay for ``session_id``.

    No-op when a healthy relay is already bound to ``runner_id``.
    When the bound runner changes (last-write-wins PATCH-rebind),
    the stale relay is cancelled and a fresh one is created
    against the new runner.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the new relay subscribes to,
        e.g. ``"runner_abc123"``. ``None`` skips relay
        (in-process path with no runner binding).
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay.
    :param conversation_store: Store for persisting items from
        the runner's SSE stream. ``None`` disables persistence.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    """
    if runner_client is None or runner_id is None:
        _logger.info(
            "Relay: skipping for session=%s (runner_client=%s, runner_id=%s)",
            session_id,
            runner_client is not None,
            runner_id,
            extra={"session_id": session_id},
        )
        return None
    existing = _runner_relay_tasks.get(session_id)
    if existing is not None:
        if existing.runner_id == runner_id and not existing.task.done():
            _logger.info(
                "Relay: reusing existing for session=%s runner=%s",
                session_id,
                runner_id,
                extra={"session_id": session_id},
            )
            return existing  # same runner, healthy task
        _logger.info(
            "Relay: replacing stale for session=%s (old_runner=%s done=%s)",
            session_id,
            existing.runner_id,
            existing.task.done(),
            extra={"session_id": session_id},
        )
        if not existing.task.done():
            existing.task.cancel()  # stale binding; replace
    else:
        _logger.info(
            "Relay: creating new for session=%s runner=%s",
            session_id,
            runner_id,
            extra={"session_id": session_id},
        )
    ready = asyncio.Event()
    # Runtime callers always supply a store. ``None`` is retained for
    # heartbeat-only relay readiness tests that never emit persistable frames.
    relay_store = cast(ConversationStore, conversation_store)
    with runner_log_scope(session_id, runner_id):
        task = asyncio.create_task(
            _relay_runner_stream(
                session_id,
                runner_client,
                relay_store,
                ready,
            ),
            name=f"runner-relay-{session_id}",
        )
    handle = _RelayHandle(runner_id=runner_id, task=task, ready=ready)
    _runner_relay_tasks[session_id] = handle

    def _on_done(t: asyncio.Task[None]) -> None:
        # Clear our slot only if it still holds this task — a
        # later rebind may have replaced us.
        current = _runner_relay_tasks.get(session_id)
        if current is not None and current.task is t:
            _runner_relay_tasks.pop(session_id, None)
            # A deny marker is only consumable by this relay's flushes;
            # drop any leftover so the (unbounded) marker dict cannot
            # leak entries for relays that died before their terminal
            # flush. A replacement relay re-records on the next verdict.
            _llm_response_denied_turns.pop(session_id, None)

    task.add_done_callback(_on_done)
    return handle


async def _ensure_runner_relay_ready(*args: Any, **kwargs: Any) -> _RelayHandle | None:
    """Call-time proxy so a facade patch of this symbol is honored here."""
    from omnigent.server.routes import sessions as _facade

    return await _facade._ensure_runner_relay_ready(*args, **kwargs)


async def _ensure_runner_relay_ready_impl(
    session_id: str,
    runner_id: str | None,
    runner_client: httpx.AsyncClient | None,
    conversation_store: ConversationStore | None = None,
) -> _RelayHandle | None:
    """
    Start the runner SSE relay and wait for its subscription ack.

    The runner stream has no replay buffer. For item events, Omnigent must
    subscribe to runner output before it forwards the input event; a
    fast harness can otherwise complete before Omnigent is listening, leaving
    the user with an apparently successful empty response.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param runner_id: Runner id the relay should bind to, e.g.
        ``"runner_abc123"``. ``None`` skips relay setup.
    :param runner_client: HTTP client pointed at ``runner_id``.
        ``None`` skips relay setup.
    :param conversation_store: Store for persisting relayed items.
    :returns: The active relay handle, or ``None`` when no runner is
        bound.
    :raises OmnigentError: If the relay cannot observe the
        runner stream's ready heartbeat before the timeout.
    """
    handle = _ensure_runner_relay(
        session_id,
        runner_id,
        runner_client,
        conversation_store,
    )
    if handle is None or handle.ready.is_set():
        return handle
    try:
        await asyncio.wait_for(
            handle.ready.wait(),
            timeout=_RUNNER_RELAY_READY_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        if handle.task.done():
            raise OmnigentError(
                "Runner stream relay exited before becoming ready",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            ) from exc
        raise OmnigentError(
            "Timed out waiting for runner stream relay to subscribe",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc
    # The runner just acknowledged this session's stream. A session bound after
    # the tunnel connected has no ``runner_last_seen`` until the next ping; stamp
    # it here so a server recycle in that window cannot read it as orphaned.
    if runner_id is not None:
        session_live_state.touch_runner_liveness([runner_id])
    return handle


async def _register_policy_elicitation(
    session_id: str,
    result: PolicyResult,
    arguments_preview: str,
    conversation_store: ConversationStore,
    *,
    tool_name: str | None = None,
    actor: dict[str, str] | None = None,
) -> str:
    """
    Publish an elicitation request event on the session stream.

    Approval state lives on the runner (in-memory
    ``_pending_approvals`` dict). The server just publishes the
    ``response.elicitation_request`` SSE event so the client
    sees the approval prompt, and returns the elicitation_id
    so the runner can key its Future on it.

    Before publishing, the server attests the prompt's policy context (each ASKing
    policy's own reason, the tool, the principal) in
    :mod:`omnigent.runtime.pending_elicitations`, so a durable inbox reads facts the
    server established rather than text on the event.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param result: The :class:`PolicyResult` with action=ASK,
        carrying the reason and deciding_policy fields.
    :param arguments_preview: Truncated argument string for
        the elicitation UI preview (max ~1024 chars).
    :param conversation_store: Store used to mirror child-session
        prompts into ancestor streams.
    :param tool_name: The gated tool, e.g. ``"sys_os_shell"``; shown on the event.
    :param actor: Principal the call runs as, e.g. ``{"run_as": "alice@example.com"}``;
        attested so an inbox files the prompt under its owner.
    :returns: The generated elicitation id, e.g. ``"elicit_a1b2c3..."``.
    """
    policies = result.deciding_policies or ["unknown"]
    elicitation_id = f"elicit_{secrets.token_hex(16)}"
    elicitation = ElicitationRequest(
        message=result.reason or "Approval required",
        requested_schema={},
        phase=Phase.TOOL_CALL.value,
        policy_names=policies,
        content_preview=arguments_preview[:1024],
    )
    # Approval state lives on the runner (in-memory
    # _pending_approvals dict of elicitation_id → Future).
    # The server just publishes the elicitation SSE event and
    # returns the elicitation_id. The runner parks on the
    # Future; the client's approval event is forwarded to the
    # runner which resolves it. No server-side state needed.
    _elicit_event = build_elicitation_request_event(
        elicitation_id, elicitation, session_id=session_id
    )
    if tool_name:
        _elicit_event["params"]["tool_name"] = tool_name
    pending_elicitations.attest(
        elicitation_id,
        {
            "session_id": session_id,
            "phase": Phase.TOOL_CALL.value,
            "tool_name": tool_name,
            "run_as": (actor or {}).get("run_as"),
            "policy_reasons": dict(result.ask_reasons or {}),
            "content_preview": arguments_preview[:1024],
        },
    )
    _logger.info(
        "policy ASK raised: session=%s tool=%s policy=%s elicitation_id=%s",
        session_id,
        tool_name,
        ",".join(policies),
        elicitation_id,
        extra={"session_id": session_id},
    )
    session_stream.publish(session_id, _elicit_event)
    await asyncio.to_thread(
        _publish_elicitation_request_to_ancestors,
        conversation_store,
        session_id,
        _elicit_event,
    )
    return elicitation_id


def _policy_evaluation_lock(session_id: str) -> asyncio.Lock:
    """Return the lock serializing policy state updates for one session."""
    lock = _policy_evaluation_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _policy_evaluation_locks[session_id] = lock
    return lock


async def _evaluate_policy_with_fresh_engine(
    session_id: str,
    spec: AgentSpec,
    conversation_store: ConversationStore,
    conv: Conversation,
    ctx: EvaluationContext,
) -> tuple[PolicyEngine, PolicyResult]:
    """Build and evaluate atomically against persisted policy state."""
    async with _policy_evaluation_lock(session_id):
        engine = await asyncio.to_thread(
            _build_policy_engine_from_spec, spec, session_id, conversation_store, conv
        )
        return engine, await engine.evaluate(ctx)


async def _evaluate_tool_call_policy(
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
    Evaluate a tool call against TOOL_CALL phase policy rules.

    Pure evaluation — does NOT persist the event. Returns
    ``None`` on ALLOW. Returns a verdict dict on DENY or ASK.

    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``function_call`` event with
        ``evaluate_policy: true``.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW (fall through). Verdict dict
        on DENY/ASK.
    """

    tool_name = body.data.get("name")
    if not tool_name or not isinstance(tool_name, str):
        raise OmnigentError(
            "function_call event with evaluate_policy requires a non-empty 'name' field in data",
            code=ErrorCode.INVALID_INPUT,
        )
    arguments_str = body.data.get("arguments", "{}")

    # Resolve agent spec + build engine off the event loop (blocking
    # DB/IO). Tool-call policy always evaluates (no guardrails skip).
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    try:
        args_payload = json.loads(arguments_str)
    except (ValueError, TypeError):
        args_payload = arguments_str

    ctx = EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": tool_name, "arguments": args_payload},
        tool_name=tool_name,
        actor=actor,
    )
    engine, result = await _evaluate_policy_with_fresh_engine(
        session_id, spec, conversation_store, conv, ctx
    )

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — publish elicitation event. Approval state lives
    # on the runner (_pending_approvals dict).
    elicitation_id = await _register_policy_elicitation(
        session_id=session_id,
        result=result,
        arguments_preview=arguments_str,
        conversation_store=conversation_store,
        tool_name=tool_name,
        actor=actor,
    )
    # The deciding policy's writes (e.g. a cost-budget checkpoint via
    # ``state_updates``) must land ONLY on approve. This relay path returns
    # ``pending`` and the verdict arrives later off-request, so stash them to
    # apply when the matching ``approval`` resolves with accept (see
    # _apply_pending_policy_ask_writes). The native path applies these inline
    # in _hold_native_ask_gate; without this, a relay/non-native session's
    # checkpoint is never recorded and the ASK re-prompts every tool call.
    # Always store an entry even when there are no deferred writes —
    # the MCP retry path checks the pending map to verify the
    # elicitation was genuinely issued by the server.
    _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
        state_updates=result.state_updates,
        set_labels=result.set_labels,
    )
    return {
        "verdict": "pending",
        "elicitation_id": elicitation_id,
        # Spec-resolved approval window; the runner's park honors it.
        "ask_timeout": resolve_ask_timeout(engine, result),
    }


async def _evaluate_input_policy(
    request: Request,
    session_id: str,
    conv: Conversation,
    body: SessionEventInput,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    _runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
) -> dict[str, Any] | None:
    """
    Evaluate a user message against REQUEST (input) phase policy rules.

    Does not persist the event. On ALLOW returns ``None`` (caller
    forwards the message). On DENY returns a verdict dict (caller does
    NOT forward). On ASK this function **parks for human approval**
    before returning: unlike the ``tool_call`` phase — where the runner
    parks via ``wait_for_user_approval`` — the REQUEST phase has no
    runner in the loop yet (the message hasn't been forwarded), so the
    approval gate must live here. It reuses :func:`_hold_native_ask_gate`
    (the same server-side park the native ``tool_call`` gate uses):
    accept collapses to ALLOW (``None``, forward the message), while
    decline / timeout collapses to a DENY verdict (fail-closed).

    :param request: The active FastAPI request, threaded to
        :func:`_hold_native_ask_gate` for upstream-disconnect detection
        while parked on an ASK.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param conv: The session's :class:`Conversation` entity.
    :param body: The validated ``message`` event.
    :param conversation_store: Store for label state.
    :param agent_store: Store for agent spec lookups.
    :param _runner_router: Unused, kept for signature
        consistency.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: ``None`` on ALLOW or an approved ASK (fall through to the
        forward path). A verdict dict ``{"verdict": "deny", "reason":
        ...}`` on DENY or a declined / timed-out ASK.
    """

    user_text = _extract_user_text_from_event(body)
    # A message with a text attachment (e.g. an uploaded CSV) may carry no typed
    # text — those ``input_file`` blocks are decoded below and must not be skipped
    # here. ``content`` being a list is the cheap precondition for that; the
    # actual (blocking) decode is deferred until after the policy-skip check.
    raw_content_blocks = body.data.get("content")
    content_blocks = (
        [block for block in raw_content_blocks if isinstance(block, dict)]
        if isinstance(raw_content_blocks, list)
        else []
    )
    has_content_blocks = bool(content_blocks)
    if not user_text and not has_content_blocks:
        return None

    # Resolve the agent spec off the event loop (blocking DB + cold-cache
    # bundle fetch). Spec only, so the cheap skip check below runs before
    # the more expensive engine build.
    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return None
    # Skip only when there are no agent guardrails AND no server-wide
    # default policies AND no session policies. Without this, default/
    # session policies (e.g. deny_pii_in_llm_request added via the UI)
    # are silently skipped for agents without a guardrails: YAML block.
    if not spec.guardrails and not get_caps().default_policies and get_policy_store() is None:
        return None

    # Text-like attachments (e.g. an uploaded CSV) arrive as ``input_file`` blocks
    # base64-inlined straight to the model — they are NOT part of ``user_text`` and
    # would otherwise reach the LLM unscanned. Decode their text here so
    # request-phase policies (e.g. deny_pii_in_llm_request) scan the attachment
    # content too. Deferred until after the skip check so a no-policy agent never
    # pays the artifact fetch. Best-effort; only runs when stores are wired.
    attachments: list[dict[str, str]] = []
    if has_content_blocks and file_store is not None and artifact_store is not None:
        from omnigent.runtime.content_resolver import extract_text_attachments

        attachments = await asyncio.to_thread(
            extract_text_attachments,
            content_blocks,
            file_store,
            artifact_store,
            session_id=session_id,
        )
    if not user_text and not attachments:
        return None
    # Structured request content ({"user_content", "attachments"}) so policies
    # can reason about attachments per-file instead of a merged string.
    request_content = {
        "user_content": user_text,
        "attachments": attachments,
        "is_system_notice": body.data.get("is_system_notice") is True,
    }

    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content=request_content,
        tool_name=None,
        actor=actor,
    )
    engine, result = await _evaluate_policy_with_fresh_engine(
        session_id, spec, conversation_store, conv, ctx
    )

    if result.action == PolicyAction.ALLOW:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return None

    if result.action == PolicyAction.DENY:
        if result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)
        return {
            "verdict": "deny",
            "reason": result.reason or "Denied by policy",
        }

    # ASK — park server-side for human approval. The REQUEST phase has no
    # runner-side approval round-trip (the message has not been forwarded to
    # a runner yet, so nothing would park on a "pending" verdict — it would
    # collapse to a silent deny). Hold the gate here exactly like the native
    # tool_call path: _hold_native_ask_gate publishes the approval card,
    # awaits the human verdict on a server-side Future, and applies the
    # deciding policy's writes only on accept (POLICIES.md §7.2). Accept ->
    # ALLOW (fall through to forward the message); decline / timeout ->
    # DENY (fail-closed).
    try:
        approved = await _hold_native_ask_gate(
            request,
            session_id=session_id,
            phase=Phase.REQUEST,
            data=body.data,
            engine=engine,
            result=result,
            conversation_store=conversation_store,
        )
    except ElicitationDeclinedError as exc:
        return {
            "verdict": "deny",
            "reason": exc.args[0] or "Denied by policy",
        }
    if approved:
        return None
    return {
        "verdict": "deny",
        "reason": result.reason or "Denied by policy",
    }


async def _wake_parent_for_blocked_child(
    parent_id: str,
    child: Conversation,
    notice: str,
    *,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    app_state: Any = None,
) -> bool:
    """
    Deliver a parent-wake notice when a sub-agent blocks on an approval.

    Posts the ``[System: …]`` notice as a synthetic user message to the
    parent's ``POST /v1/sessions/{id}/events`` — the same path the runner's
    terminal-completion wake uses, so it starts a continuation turn (idle
    parent) or coalesces with pending input (busy parent). Best-effort: a
    missing parent, missing runner, or transport error is logged and swallowed
    (a dropped wake is no worse than the pre-fix no-wake baseline), but the
    *outcome* is reported back so the notifier can release its per-block
    debounce and let a later publish retry rather than silencing the block.

    :param parent_id: Parent session id, e.g. ``\"conv_parent123\"``.
    :param child: The blocked child :class:`Conversation`; used only for its
        label/id in the notice and logs.
    :param notice: The ``[System: …]`` text to inject into the parent.
    :param conversation_store: Used to load the parent :class:`Conversation`
        and persist the synthetic user message item.
    :param runner_router: Router used to resolve the parent's bound
        runner. ``None`` in in-process setups (the runtime singleton is
        consulted as a fallback).
    :param app_state: ``app.state``, so a Super Chat parent runs its owner's
        Foundation pick.
    :returns: ``True`` when the notice was dispatched to the parent's runner;
        ``False`` when delivery could not happen (parent gone, no runner bound,
        or the forward raised a transport error).
    """
    parent_conv = await asyncio.to_thread(conversation_store.get_conversation, parent_id)
    if parent_conv is None:
        # Parent vanished between publish and wake (cascading-delete race).
        _logger.debug(
            "subagent block notifier: parent %s missing; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    runner_client = await _get_runner_client(parent_id, runner_router)
    if runner_client is None:
        # WARNING (not DEBUG): an unbound parent is the transient-miss case the
        # notifier retries — surface it rather than burying it as routine.
        _logger.warning(
            "subagent block notifier: no runner bound for parent %s; dropping wake for %s",
            parent_id,
            child.id,
        )
        return False
    # Ensure the parent's SSE relay is live so the wake turn's output is
    # persisted (parity with post_event).
    _ensure_runner_relay(
        parent_id,
        parent_conv.runner_id,
        runner_client,
        conversation_store,
    )
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "is_system_notice": True,
            "content": [{"type": "input_text", "text": notice}],
        },
    )
    try:
        # None args: a system notice carries no agent/files/artifacts; the runner
        # recomputes has_mcp_servers from the parent's cached spec.
        await _dispatch_session_event_to_runner(
            parent_id,
            parent_conv,
            body,
            conversation_store,
            runner_client,
            agent_name=None,
            file_store=None,
            artifact_store=None,
            app_state=app_state,
        )
    except (httpx.HTTPError, OmnigentError):
        _logger.warning(
            "subagent block wake POST failed for parent=%s child=%s",
            parent_id,
            child.id,
            exc_info=True,
        )
        return False
    return True


def configure_subagent_block_notifier(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
    app_state: Any = None,
) -> Callable[[], None]:
    """
    Install the parent-wake notifier on the elicitation publish path.

    Wires :class:`SubagentBlockNotifier` into
    :mod:`omnigent.runtime.pending_elicitations` so a sub-agent that
    blocks on an approval immediately wakes its immediate parent through
    the same ``/events`` ingest path the runner-side terminal-completion
    wake already uses (see
    :func:`_wake_parent_for_blocked_child`). Top-level sessions (no
    parent) are no-ops; multi-user safety is inherent because the wake
    is delivered to the recorded ``parent_conversation_id`` only, never
    fanned out to collaborators or unrelated sessions.

    :param conversation_store: Store used to resolve a child's
        ``parent_conversation_id`` and to persist the wake message.
    :param runner_router: Router used by the wake to reach the parent's
        bound runner. ``None`` in in-process setups.
    :param app_state: ``app.state``, so a woken Super Chat runs its owner's
        Foundation pick.
    :returns: A callable that uninstalls the observer and cancels any
        in-flight wake futures. Call from the lifespan teardown.
    """
    from omnigent.runtime import pending_elicitations as _pending_elicitations
    from omnigent.runtime.subagent_block_notifier import SubagentBlockNotifier

    loop = asyncio.get_running_loop()

    async def _wake_dispatch(parent_id: str, child: Conversation, notice: str) -> bool:
        """
        Deliver one wake notice (the notifier's injected dispatch).

        :param parent_id: Parent session id.
        :param child: The blocked child :class:`Conversation`.
        :param notice: Pre-formatted ``[System: …]`` text.
        :returns: ``True`` when the notice reached the parent's runner,
            ``False`` when it could not be delivered (so the notifier
            releases the debounce and a re-publish can retry).
        """
        return await _wake_parent_for_blocked_child(
            parent_id,
            child,
            notice,
            conversation_store=conversation_store,
            runner_router=runner_router,
            app_state=app_state,
        )

    notifier = SubagentBlockNotifier(
        conversation_store=conversation_store,
        wake_dispatch=_wake_dispatch,
        loop=loop,
    )
    _pending_elicitations.set_elicitation_observer(notifier.observe)

    def _uninstall() -> None:
        """Remove the observer and cancel any outstanding wake futures."""
        _pending_elicitations.set_elicitation_observer(None)
        notifier.close()

    return _uninstall


def _ungatewayed_native_harnesses(host: Host | None, harnesses: Sequence[str]) -> list[str]:
    """Which of *harnesses* this host does not back with the workspace AI gateway.

    The external router's picks are gateway catalog ids, so a CLI pointed at
    Bedrock, a personal subscription, or any other provider cannot run one even
    with the binary installed — those harnesses are served by the built-in judge
    instead. Reads the map the host reported on its connect handshake; unknown
    (nothing reported to this replica yet, or no host bound) counts as backed.

    :param host: The session's target host, or ``None`` (e.g. a sandbox).
    :param harnesses: Harness ids to check, e.g. ``("claude-native",)``.
    :returns: The not-backed ids, in the order given.
    """
    from omnigent.gateway_inference import not_gateway_backed
    from omnigent.server.routing_backend import reported_gateway_inference

    return not_gateway_backed(reported_gateway_inference(host), harnesses)


def _gateway_backed(host: Host | None, harnesses: Sequence[str]) -> bool:
    """Whether the external router can serve a call over *harnesses* on *host*.

    :param host: The session's target host, or ``None``.
    :param harnesses: Every harness family the call may land on.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    return not _ungatewayed_native_harnesses(host, harnesses)


async def _session_routing_host(
    conv: Conversation,
    host_store: HostStore | None,
) -> Host | None:
    """Read the host a live session is bound to, for the gateway-backing check.

    Never raises and never blocks the turn: no host store, no ``host_id``, or a
    store error all read as "unknown", which counts as gateway-backed.

    :param conv: The session row.
    :param host_store: Persistent host registrations, or ``None``.
    :returns: The host row, or ``None`` when it cannot be read.
    """
    if host_store is None or conv.host_id is None:
        return None
    try:
        return await asyncio.to_thread(host_store.get_host, conv.host_id)
    except Exception:  # noqa: BLE001 — an unreadable host row is just unknown
        _logger.debug(
            "routing: could not read host %r for session=%s",
            conv.host_id,
            conv.id,
            exc_info=True,
            extra={"session_id": conv.id},
        )
        return None


async def _spawn_gateway_backed(
    request: Request,
    conv: Conversation,
    harnesses: Sequence[str],
) -> bool:
    """Whether the workspace router can serve a spawn over *harnesses*.

    :param request: Used to reach the app's host store.
    :param conv: The parent session row.
    :param harnesses: Every harness family the spawn may land on.
    :returns: ``True`` unless the host explicitly reports one as not backed.
    """
    host_store = getattr(request.app.state, "host_store", None)
    return _gateway_backed(await _session_routing_host(conv, host_store), harnesses)


def _create_resolved_harness(
    agent: Agent,
    harness_override: str | None,
    agent_cache: AgentCache | None,
) -> str | None:
    """Resolve the harness a create will run on, before any row exists.

    :param agent: The bound agent row.
    :param harness_override: The request's raw ``harness_override``, if any.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: The canonical harness id, or ``None`` when it cannot be resolved.
    """
    from omnigent.harness_aliases import canonicalize_harness

    if harness_override:
        return canonicalize_harness(harness_override) or harness_override
    if agent_cache is None:
        return None
    try:
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        # An unloadable spec just means "harness unknown"; the create's own
        # validation reports it.
        _logger.debug("create-time routing: agent %r failed to load", agent.name, exc_info=True)
        return None
    return canonicalize_harness(_spec_harness(loaded.spec)) or None


def _spec_routes_its_own_harness(
    body: SessionCreateInput,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> bool:
    """Whether this create must hand the brain harness to the router.

    A spec pinning ``executor.config.harness`` also pins the family its
    sub-agents are routed within, so a two-headed agent loses the head that
    lives in the other family. ``smart_routing_harness: auto`` opts the spec out
    of its own pin for a Smart Routing session, taking the same ``"auto"``
    sentinel path the brain-harness picker offers by hand.

    Off unless Smart Routing is on for this create, so a spec carrying the key
    behaves exactly as before with routing off. Deliberately narrow otherwise:
    a client's explicit ``harness_override`` or ``model_override`` is the user's
    own pick and outranks the spec, and a child / named sub-agent create is
    routed by the spawn and turn paths (which read the parent's routing state),
    not here.

    :param body: The validated create request.
    :param agent: The bound agent row.
    :param agent_cache: Cache for loading the agent's parsed spec.
    :returns: ``True`` when ``harness_override`` must become ``"auto"``.
    :raises OmnigentError: ``invalid_input`` when the spec's opt-in value is
        not the ``"auto"`` sentinel, or sits on a non-omnigent executor type.
    """
    if body.cost_control_mode_override != "on":
        return False
    if body.harness_override is not None or body.model_override is not None:
        return False
    if body.parent_session_id is not None or body.sub_agent_name is not None:
        return False
    if agent_cache is None:
        return False
    try:
        loaded = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.session_id is None
        )
    except (KeyError, AttributeError, ValueError, ImportError, OSError):
        # An unloadable spec just means "no opt-in"; the create's own
        # validation reports it.
        _logger.debug("create-time routing: agent %r failed to load", agent.name, exc_info=True)
        return False
    return _validated_spec_smart_routing_harness(loaded.spec) is not None


def _spawn_pins_its_harness(
    body: SessionCreateInput,
    agent: Agent,
    agent_cache: AgentCache | None,
) -> bool:
    """Whether this child create already names the harness the child runs on.

    Two pins count, and both come from outside the router: an explicit
    ``harness_override`` on the spawn, and a declared sub-agent whose spec
    carries its own harness (polly's ``pi`` / ``claude_code`` workers). Either
    way the child's CLI is decided before any message is routed, so handing the
    router the whole multi-harness catalog can only produce a verdict the pane
    will not honor.

    :param body: The validated create request.
    :param agent: The parent agent row whose bundle holds the sub-agent specs.
    :param agent_cache: Cache for loading the parsed parent bundle. ``None``
        cannot resolve a sub-agent spec, so only the explicit pin is seen.
    :returns: ``True`` when the child's harness is already chosen.
    """
    if body.harness_override is not None:
        return True
    if not body.sub_agent_name:
        return False
    from omnigent.models.model_catalog import spec_harness

    sub_spec = _resolve_subagent_spec(
        agent=agent,
        sub_agent_name=body.sub_agent_name,
        agent_cache=agent_cache,
    )
    return sub_spec is not None and spec_harness(sub_spec) is not None


async def _create_session_from_existing_agent(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    body: SessionCreateInput,
    request: Request,
    agent_cache: AgentCache | None = None,
    user_id: str | None = None,
    permission_store: PermissionStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
    project_store: ProjectStore | None = None,
    conversation_id: str | None = None,
) -> tuple[SessionResponse, Conversation]:
    """
    Create a session bound to an already-registered agent.

    This preserves the existing JSON ``POST /v1/sessions`` contract:
    clients that uploaded an agent separately still bind by durable
    ``agent_id`` and receive the full session snapshot.

    :param conversation_store: Store for conversation persistence.
    :param agent_store: Store for agent lookup by durable id.
    :param runner_router: Runner router used to validate any initial
        dispatch triggered by ``initial_items``.
    :param body: Validated JSON create request.
    :param agent_cache: Optional cache for loading parsed agent specs
        from bundles, used to populate ``llm_model`` and
        ``context_window`` in the response.
    :param user_id: Authenticated caller, e.g.
        ``"alice@example.com"``. Used to authorize parent-session
        and agent ownership and enforce runner
        ownership on parent-session inheritance.
    :param permission_store: Permission store for session-access
        checks. Required for authorization of
        ``parent_session_id`` and session-scoped ``agent_id``.
    :param liveness_lookup: Optional session-scoped liveness lookup
        to populate ``SessionResponse.runner_online``.
    :param file_store: Optional file metadata store for resolving
        ``file_id`` references in ``initial_items`` before forwarding
        to the runner.
    :param artifact_store: Optional binary content store for the same.
    :param conversation_id: Server-chosen session id (never client input), so a
        server-side creator can rely on the primary key for idempotency.
    :returns: The newly created session snapshot and its conversation row.
    :raises OmnigentError: 404 if no agent matches ``body.agent_id``;
        403/404 if ``parent_session_id`` or session-scoped ``agent_id``
        fails authorization.
    """
    from omnigent.server.routes._session_create_validation import (
        resolve_project_session_create,
    )

    project_resolution = await resolve_project_session_create(
        body=body,
        user_id=user_id,
        project_store=project_store,
    )
    body = project_resolution.body
    creation_metadata(parent_session_id=body.parent_session_id, host_type=body.host_type)
    assert body.agent_id is not None

    _reject_reserved_cost_control_label_seed(body.labels)
    _reject_server_reserved_label_seed(body.labels)

    agent = await validate_session_agent(
        user_id=user_id,
        agent_id=body.agent_id,
        agent_store=agent_store,
        permission_store=permission_store,
        conversation_store=conversation_store,
    )

    # A spec that hands its brain harness to the router (``smart_routing_harness:
    # auto``) takes the sentinel path below: the harness is not decided until the
    # first message.
    _spec_auto_brain = await asyncio.to_thread(
        _spec_routes_its_own_harness, body, agent, agent_cache
    )

    # Authorize parent_session_id before inheriting anything.
    # The caller must own or have READ access to the parent session;
    # otherwise a forged parent link lets them inherit runner
    # bindings and establish a parent-child relationship with a
    # session they don't control.
    if body.parent_session_id is not None:
        await _require_access(
            user_id,
            body.parent_session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )

    # Reject an undeclared sub-agent before persisting the row. Downstream
    # spec swaps warn and keep the parent on a miss, which is right for a
    # name that once resolved and has since been renamed or removed — but
    # for a name the parent's spec NEVER declared it would boot the child as
    # a parent clone for the session's whole life, off a request that was
    # wrong when it arrived. Reject it while the caller is still here to be
    # told, and leave the parent-fallback to the cases it fits.
    if body.sub_agent_name:
        await asyncio.to_thread(
            _require_declared_subagent,
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )

    # Reject shell-/flag-shaped values before any row or worktree exists.
    model_override, reasoning_effort = validate_session_model_metadata(
        model_override=body.model_override,
        reasoning_effort=body.reasoning_effort,
    )

    # Validated before any row exists so a bad value never creates an
    # orphan session; None (unset) defers to the spec default.
    cost_control_mode_override = _validated_cost_control_mode_override(
        body.cost_control_mode_override
    )
    subagent_routing_override = _validated_subagent_routing_override(
        body.subagent_routing_override
    )

    # A child of an auto-harness parent whose subagent-routing switch is on is
    # routed regardless of the harness/model the orchestrator chose: force the
    # "auto" sentinel so the first-message routing path picks both harness and
    # model, ignoring the tool's ``agent``/``model`` args. Only applied to
    # omnigent-executor agents (auto requires a swappable brain harness).
    #
    # The gate is the parent's subagent-routing switch — the one routing knob the
    # in-session UI exposes — matching the gate the native-subagent hook applies
    # to in-harness spawns. A session that starts on Smart Routing is stamped
    # "on" at create; a legacy row that predates the stamp carries no switch, so
    # it reads as Default and its children are not forced.
    #
    # A child of a session pinned to one harness family (a plain codex or
    # claude session) must NOT be forced to auto: the sentinel would hand the
    # router the whole multi-harness catalog and stamp the auto marker on the
    # child, so a codex session ends up with claude children. Those children
    # keep the harness they were created with and are routed in-family (see
    # the child-routing call in ``_forward_event_to_runner``).
    #
    # Nor is a spawn that NAMED its harness forced: a declared sub-agent
    # (polly's ``pi``) and an explicit ``harness_override`` both pin the CLI the
    # child boots on, which the sentinel does not move — it only re-decides the
    # row. Forcing them handed the router the whole catalog for a pane already
    # committed to one harness, so a ``pi`` worker could be stamped with an
    # applied codex verdict while running pi, and a ``claude-native`` worker
    # lost its terminal labels (skipped for forced-auto children below). Those
    # children route in their own family instead.
    _force_auto_for_child = False
    _parent_for_routing: Conversation | None = None
    if body.parent_session_id is not None:
        _parent_for_routing = await asyncio.to_thread(
            conversation_store.get_conversation, body.parent_session_id
        )
        if (
            _parent_for_routing is not None
            and subagent_routing_enabled(_parent_for_routing.subagent_routing_override)
            and auto_harness_session(_parent_for_routing)
            and not await asyncio.to_thread(_spawn_pins_its_harness, body, agent, agent_cache)
        ):
            try:
                await asyncio.to_thread(_validated_harness_override_executor_type, agent)
                _force_auto_for_child = True
            except OmnigentError:
                # Non-omnigent agent (e.g. a native wrapper) — can't route
                # harness; leave the orchestrator's choice untouched.
                _force_auto_for_child = False
        # A pinned Smart Routing parent may only spawn inside its own model
        # family, so the child that could never be routed is refused here
        # rather than created and declined afterwards. Auto parents are
        # unaffected (the router owns their family) and so is a parent that
        # routes no spawns — neither resolves the child's harness at all.
        if _pinned_spawn_family(_parent_for_routing) is not None:
            _reject_out_of_family_child(
                _parent_for_routing,
                await asyncio.to_thread(
                    _create_resolved_harness, agent, body.harness_override, agent_cache
                ),
            )

    # A session that starts on Smart Routing routes the subagents it spawns.
    # Stamped here, once, so the spawn gate reads one explicit switch instead
    # of re-deriving it from this session's (or its parent's) routing state.
    # Only "on" is written: unset already means Default, so stamping "off" on
    # every ordinary create would grow the overrides blob and cost a write for
    # no change in behavior. An explicit caller value always wins.
    #
    # Stamped for every routed create, pinned harness included: a pinned codex
    # session's launch installs the same generated ``hooks.json`` spawn gate and
    # routed-spawn tool pre-approvals a routed claude one does (see
    # ``ensure_session_router_quietly``), so the switch has a consumer on every
    # family. What a pinned session does *not* get is cross-family picks — the
    # router is offered its own family only. A plain create is still left unset:
    # nothing routes there, so nothing reads the switch.
    if subagent_routing_override is None and (
        cost_control_mode_override == "on"
        or (
            _parent_for_routing is not None
            and subagent_routing_enabled(_parent_for_routing.subagent_routing_override)
        )
    ):
        subagent_routing_override = "on"

    # Validated against the loaded spec (known harness + omnigent
    # executor type) before any row exists, mirroring the CLI's
    # --harness fail-loud rules.
    # "auto" defers harness + model selection to the first-message routing
    # path; validate executor type now but store the sentinel unchanged.
    harness_override: str | None
    if _force_auto_for_child or _spec_auto_brain or body.harness_override == "auto":
        await asyncio.to_thread(_validated_harness_override_executor_type, agent)
        harness_override = "auto"
        # Ignore any orchestrator-supplied model; routing picks it. (The spec
        # opt-in never reaches here with a client model, so none is dropped.)
        model_override = None
    else:
        harness_override = await asyncio.to_thread(
            _validated_harness_override, body.harness_override, agent
        )

    inference_snapshot = None
    if agent_cache is not None:
        from omnigent.runtime.workflow import _find_spec_by_name

        try:
            selection_spec = (
                await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
            ).spec
        except (KeyError, AttributeError, ValueError, ImportError, OSError):
            from omnigent.server.routes.sandbox_inference import managed_inference_configured

            if model_override is not None or (
                body.host_type == "managed"
                and managed_inference_configured(request, body.sandbox_provider)
            ):
                raise OmnigentError(
                    "Cannot load the agent to validate its configured inference provider",
                    code=ErrorCode.INVALID_INPUT,
                ) from None
            # Without a selection, retain creation when the harness is unknown.
            _logger.debug(
                "create-time model policy: agent %r failed to load", agent.name, exc_info=True
            )
            selection_spec = None
        if selection_spec is not None and body.sub_agent_name:
            selection_spec = _find_spec_by_name(selection_spec, body.sub_agent_name)
        from omnigent.server.routes.sandbox_inference import (
            prepare_create_inference,
        )

        if selection_spec is None and body.host_type == "managed":
            from omnigent.server.routes.sandbox_inference import managed_inference_configured

            if managed_inference_configured(request, body.sandbox_provider):
                raise OmnigentError(
                    "Cannot determine the harness for the configured inference provider",
                    code=ErrorCode.INVALID_INPUT,
                )
        if selection_spec is not None:
            inference_snapshot, model_override = await prepare_create_inference(
                request,
                body,
                selection_spec,
                user_id,
                conversation_store,
                harness_override=harness_override,
                model_override=model_override,
            )

    # Inherit runner affinity from the parent session so the child
    # is assigned to the same runner (sub-agent co-location).
    inherited_runner_id: str | None = None
    parent_workspace: str | None = None
    if body.parent_session_id is not None:
        parent_conv = conversation_store.get_conversation(body.parent_session_id)
        if parent_conv is not None:
            inherited_runner_id = parent_conv.runner_id
            parent_workspace = parent_conv.workspace
            # Defense-in-depth: don't inherit a runner the
            # caller doesn't own.
            if (
                inherited_runner_id is not None
                and user_id is not None
                and runner_router is not None
            ):
                runner_owner = runner_router.runner_owner(inherited_runner_id)
                if runner_owner is not None and runner_owner != user_id:
                    inherited_runner_id = None
    # A child that shares the parent's runner starts in the parent's *current* working
    # directory (it may have moved since the parent was created, see ``PUT .../workspace``).
    if inherited_runner_id is None:
        parent_workspace = None

    # Workspace validation: if the caller is binding to a host,
    # they must also pass a workspace, and the workspace must
    # satisfy the agent's os_env.cwd boundary on that host (per
    # designs/SESSION_WORKSPACE_SELECTION.md). Done before
    # create_conversation so a bad workspace never produces a row.
    # With git worktree creation, the validated path is the source
    # repo; the worktree it produces becomes the stored workspace.
    canonical_workspace: str | None = body.workspace
    if canonical_workspace is None and body.host_id is None:
        canonical_workspace = parent_workspace
    if body.host_id is not None:
        canonical_workspace = await _validate_session_workspace(
            user_id=user_id,
            host_id=body.host_id,
            workspace=body.workspace,
            agent=agent,
            agent_cache=agent_cache,
            request=request,
        )

    # Git worktree options (optional). Two modes on body.git:
    #  - create (default): make a worktree; it becomes the stored
    #    workspace and its branch is recorded.
    #  - bind (existing_worktree): workspace already IS the worktree;
    #    record its branch only, create nothing.
    git_branch: str | None = None
    # Set to the created worktree path ONLY when Omnigent creates one.
    # Gates create-rollback: an existing worktree bound via
    # existing_worktree must never be force-removed on failure — it is
    # the user's, not an Omnigent orphan.
    created_worktree_path: str | None = None
    if body.git is not None:
        if body.git.existing_worktree:
            # Starting in a pre-existing worktree: no worktree is created, but
            # record its branch so the sidebar shows it and the opt-in delete
            # flow can offer to remove it. Validate the name (the host never
            # runs git for this path, so the server is the only gate).
            from omnigent.host.git_worktree import WorktreeError, validate_branch_name

            try:
                validate_branch_name(body.git.branch_name)
            except WorktreeError as exc:
                raise OmnigentError(exc.message, code=ErrorCode.INVALID_INPUT) from exc
            git_branch = body.git.branch_name
        else:
            created_worktree = await _create_session_worktree(
                host_id=body.host_id,
                source_repo=canonical_workspace,
                git=body.git,
                request=request,
            )
            canonical_workspace = created_worktree.workspace or created_worktree.worktree_path
            git_branch = created_worktree.branch
            created_worktree_path = created_worktree.worktree_path

    # Native-terminal pass-through args.
    #
    # Named sub-agent creates (``body.sub_agent_name`` set) DERIVE these
    # from the trusted, server-loaded sub-spec only — any caller-supplied
    # ``body.terminal_launch_args`` is ignored. This is the YOLO seam:
    # claude-native maps ``permission_mode`` to ``--permission-mode``,
    # codex-native defaults to full bypass
    # (``--dangerously-bypass-approvals-and-sandbox``) so a headless worker
    # can edit/run unattended without stalling on native approval prompts
    # (opt out with ``yolo: false``). A caller cannot inject launch wiring by smuggling
    # args through the spawn body.
    #
    # Sessions that resolve their own agent (top-level sessions and the
    # manual Add Agent child flow where ``sub_agent_name`` is null) keep
    # the validated body args (e.g. ``["--permission-mode",
    # "bypassPermissions"]`` from the web permission-mode selector). The
    # flat-list shape plus this bounds check is the security boundary;
    # mirrors the multipart create + PATCH paths. When the body carries no
    # args, the agent's own trusted spec seeds them below — explicit
    # opt-ins only, never the headless worker default bypass.
    sub_spec: AgentSpec | None = None
    validated_launch_args: list[str] | None = None
    if body.sub_agent_name:
        sub_spec = _resolve_subagent_spec(
            agent=agent,
            sub_agent_name=body.sub_agent_name,
            agent_cache=agent_cache,
        )
    else:
        try:
            validated_launch_args = _validate_terminal_launch_args(body.terminal_launch_args)
        except ValueError as exc:
            raise OmnigentError(
                f"invalid terminal_launch_args: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

    # Spec-seeded defaults: a self-resolved agent's stored spec supplies the
    # spec-level reasoning effort.
    own_spec: AgentSpec | None = None
    needs_effort_from_spec = reasoning_effort is None and sub_spec is None
    if needs_effort_from_spec and agent_cache is not None and agent.bundle_location is not None:
        try:
            own_loaded = await asyncio.to_thread(
                agent_cache.load,
                agent.id,
                agent.bundle_location,
                expand_env=agent.session_id is None,
            )
            own_spec = own_loaded.spec if own_loaded is not None else None
        except (OSError, ValueError, RuntimeError, KeyError, AttributeError, ImportError):
            _logger.warning(
                "create: spec load for spec-seeded defaults failed "
                "(agent=%s); session starts with no spec-level effort",
                agent.id,
                exc_info=True,
            )

    if reasoning_effort is None:
        effort_spec: AgentSpec | None = sub_spec if sub_spec is not None else own_spec
        spec_effort = effort_spec.executor.reasoning_effort if effort_spec is not None else None
        if spec_effort is not None:
            _, reasoning_effort = validate_session_model_metadata(
                model_override=None,
                reasoning_effort=spec_effort,
            )

    initial_labels = dict(body.labels) if body.labels else {}
    if body.parent_session_id is not None:
        from omnigent.context.labels import LAUNCHING_LABEL_KEY

        initial_labels[LAUNCHING_LABEL_KEY] = "true"
        if _parent_for_routing is not None:
            initial_labels.update(rounds.helper_labels(_parent_for_routing.labels, initial_labels))
    if created_worktree_path is not None:
        from omnigent.server.routes._host_worktree import (
            WORKTREE_ROOT_LABEL_KEY,
            worktree_root_fingerprint,
        )

        initial_labels[WORKTREE_ROOT_LABEL_KEY] = worktree_root_fingerprint(created_worktree_path)
    if harness_override == "auto":
        from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

        initial_labels[AUTO_HARNESS_LABEL_KEY] = "1"

    snapshot_kwargs: dict[str, Any] = (
        {"inference_snapshot": inference_snapshot} if inference_snapshot is not None else {}
    )
    from omnigent.stores.conversation_store.overrides import encode_session_overrides

    try:
        # Include spec-seeded defaults before create; overflow must not leave a session.
        encode_session_overrides(
            {
                "reasoning_effort": reasoning_effort,
                "model_override": model_override,
                "cost_control_mode_override": cost_control_mode_override,
                "subagent_routing_override": subagent_routing_override,
                "harness_override": harness_override,
            }
        )
        with creation_stage("create_persistence_ms"):
            conv = conversation_store.create_conversation(
                agent_id=agent.id,
                title=body.title,
                conversation_id=conversation_id,
                parent_conversation_id=body.parent_session_id,
                runner_id=inherited_runner_id,
                kind="sub_agent" if body.parent_session_id else "default",
                sub_agent_name=body.sub_agent_name,
                host_id=body.host_id,
                workspace=canonical_workspace,
                git_branch=git_branch,
                terminal_launch_args=validated_launch_args,
                project_id=project_resolution.project_id,
                labels=initial_labels or None,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                cost_control_mode_override=cost_control_mode_override,
                subagent_routing_override=subagent_routing_override,
                harness_override=harness_override,
                **snapshot_kwargs,
            )
    except NameAlreadyExistsError as exc:
        if (
            created_worktree_path is not None
            and body.host_id is not None
            and git_branch is not None
        ):
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=created_worktree_path,
                branch=git_branch,
                # A recreated worktree checked out a pre-existing branch —
                # remove the directory but keep the user's branch (and its
                # unpushed commits).
                delete_branch=body.git is None or not body.git.existing_branch,
                request=request,
                reason="create-rollback",
            )
        raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
    except Exception:
        # Broad catch is intentional: ANY create_conversation failure
        # (integrity error, name clash, ...) must trigger orphan-worktree
        # cleanup before the error propagates. We re-raise unchanged
        # below, so nothing is swallowed. Gate on created_worktree_path,
        # NOT git_branch: only a worktree Omnigent created here may be
        # force-removed. An existing worktree bound via workspace_branch
        # also sets git_branch but is the user's — never destroy it.
        if (
            created_worktree_path is not None
            and body.host_id is not None
            and git_branch is not None
        ):
            await _remove_session_worktree_best_effort(
                host_id=body.host_id,
                worktree_path=created_worktree_path,
                branch=git_branch,
                # Same branch-preservation rule as the NameAlreadyExists
                # rollback above: never -D a pre-existing branch.
                delete_branch=body.git is None or not body.git.existing_branch,
                request=request,
                reason="create-rollback",
            )
        raise

    # The create request has no conv id in its URL, so the path-based
    # FastAPI hook can't tag it — stamp the minted id so the create span
    # joins the session's session.id group.
    from omnigent.runtime import telemetry

    session_created(conv.id, conv.runner_id)
    telemetry.set_session_id(conv.id)
    rounds.helper_created(conv)

    # Emit session.created exactly once at creation time.
    # Best-effort: skip if the host opted out via HostHelloFrame.
    try:
        _hr: HostRegistry | None = getattr(request.app.state, "host_registry", None)
        _host_opted_out = (
            _hr is not None
            and conv.host_id is not None
            and _hr.is_host_telemetry_opted_out(conv.host_id)
        )
        if not _host_opted_out:
            _install_id = _get_installation_id()
            _anon_uid = _tel_anon_user_id(user_id, _install_id)
            _client_header = request.headers.get("x-omnigent-client")
            _surface = (
                _client_header
                if _client_header in ("web", "desktop", "ios", "android", "cli")
                else _classify_surface(request.headers.get("user-agent"))
            )
            _host_install_id: str | None = None
            if _hr is not None and conv.host_id is not None:
                _host_install_id = _hr.get_host_installation_id(conv.host_id)
            # Resolve harness directly from the in-scope agent + cache so
            # the result is independent of _globals._agent_store (which is
            # only set when the server is started via the CLI).
            _tel_harness: str | None
            if conv.harness_override:
                _tel_harness = conv.harness_override
            elif agent_cache is not None:
                _tel_loaded = agent_cache.load(
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
                _tel_harness = _spec_harness(_tel_loaded.spec)
            else:
                _tel_harness = None
            # Only log agent name for known built-in orchestrators to avoid
            # leaking user-defined agent names.
            _NAMED_AGENTS = {"polly", "debby"}
            _tel_agent_name = agent.name if agent.name in _NAMED_AGENTS else None
            # Routing state at creation time.
            _tel_routing_on = conv.cost_control_mode_override == "on"
            _tel_emit(
                _TelSessionCreatedEvent(
                    session_id=conv.id,
                    agent_id=agent.id,
                    harness=_tel_harness,
                    surface=_surface,
                    installation_id=_install_id,
                    anon_user_id=_anon_uid,
                    host_installation_id=_host_install_id,
                    is_fork=body.parent_session_id is not None,
                    is_sub_agent=body.sub_agent_name is not None,
                    agent_name=_tel_agent_name,
                    routing_enabled=_tel_routing_on,
                )
            )
    except Exception:  # noqa: BLE001
        pass

    if body.initial_items:
        runner_client = await _get_runner_client(
            conv.id,
            runner_router,
            conversation=conv,
        )
        if runner_client is None:
            # No runner bound — persist initial items as history-only
            # seed via the conversation store. No execution fires; the
            # caller is responsible for binding a runner and posting a
            # follow-up event if they want the agent to react.
            # SessionEventInput carries no response_id; this is a
            # pre-execution history seed, so tag all items with a
            # synthetic ``"seed"`` response id. The runner overwrites
            # this on first turn via a normal append path.
            new_items = [
                _build_new_item(item, "seed", created_by=_attribution_user(user_id))
                for item in body.initial_items
            ]
            await asyncio.to_thread(conversation_store.append, conv.id, new_items)
        else:
            await _ensure_runner_relay_ready(
                conv.id,
                conv.runner_id,
                runner_client,
                conversation_store,
            )
            # Dispatch (not a plain forward) so native-terminal sessions take the
            # single-writer bypass — otherwise the forwarder's echo duplicates the kickoff.
            for item in body.initial_items:
                pending_background_title = prepare_background_session_title(
                    coordinator=background_title_coordinator,
                    conversation=conv,
                    event=item,
                    enabled=background_session_titles_enabled(request.headers),
                )
                await _dispatch_session_event_to_runner(
                    conv.id,
                    conv,
                    item,
                    conversation_store,
                    runner_client,
                    agent_name=agent.name,
                    file_store=file_store,
                    artifact_store=artifact_store,
                    created_by=_attribution_user(user_id),
                    host_store=getattr(request.app.state, "host_store", None),
                    app_state=request.app.state,
                )
                if pending_background_title is not None:
                    pending_background_title.schedule(expected_seed_title=conv.title)
        with creation_stage("create_persistence_ms"):
            refreshed = await asyncio.to_thread(conversation_store.get_conversation, conv.id)
        if refreshed is None:
            raise OmnigentError(
                f"Session {conv.id!r} disappeared after persisting initial items",
                code=ErrorCode.INTERNAL_ERROR,
            )
        conv = refreshed
    response = await _get_session_snapshot(
        conversation_store,
        conv.id,
        agent_store=agent_store,
        agent_cache=agent_cache,
        liveness_lookup=liveness_lookup,
        conversation=conv,
        request=request,
    )
    return response, conv


def _create_session_from_bundle(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore,
    metadata: SessionCreateMetadata,
    bundle_bytes: bytes,
    runner_id: str | None = None,
    spec: AgentSpec | None = None,
    inference_snapshot: dict[str, Any] | None = None,
    inference_model: str | None = None,
    created_by: str | None = None,
) -> CreatedSessionResponse:
    """
    Validate, store, and persist a bundled session request.

    Each upload creates a session-scoped agent row, even when a
    template agent with the same spec name already exists. Agent
    names are user-authored labels, not global content identities:
    reusing a template by name would make a fresh ``omnigent run
    <yaml>`` session execute whatever bundle that template currently
    points at, silently discarding the uploaded bundle and coupling
    unrelated users who chose the same name.

    :param conversation_store: Store that owns the atomic
        conversation-plus-agent transaction.
    :param artifact_store: Store for uploaded bundle bytes.
    :param metadata: Validated session metadata. When
        ``metadata.parent_session_id`` is set (already authorized by
        the caller), the session is created as a sub-agent
        child of that conversation.
    :param bundle_bytes: Raw uploaded ``.tar.gz`` agent bundle.
    :param runner_id: Optional runner binding inherited from the
        parent session (caller-resolved, ownership-checked),
        e.g. ``"runner_abc123"``. ``None`` leaves the session
        unbound.
    :param spec: Optional pre-validated spec for *bundle_bytes*. The
        multipart route validates the bundle once up front (it needs
        ``os_env.cwd`` for workspace validation before any row
        exists) and passes the result here so the tarball isn't
        extracted twice. ``None`` validates in this function.
    :param created_by: Identity of the creating user, recorded on the
        new session-scoped agent so its code can only be mutated by the
        owner. ``None`` in single-user mode.
    :returns: Response with the new session id.
    :raises OmnigentError: If bundle validation or agent insert
        integrity checks fail, or the parent session vanished
        between authorization and insert.
    :raises SQLAlchemyError: If the database transaction fails for
        any non-integrity reason.
    """
    if spec is None:
        # Enforce the policy-handler allowlist only on a shared /
        # multi-user server. On a trusted single-user/local server,
        # ``omnigent run`` uploads the operator's own bundle through this same
        # path, so custom handlers must keep working (the operator already has
        # code execution — the restriction would add no security there).
        spec = validate_agent_bundle(
            bundle_bytes,
            enforce_handler_allowlist=not local_single_user_enabled(),
        )
    assert spec.name is not None

    if metadata.reasoning_effort is None and spec.executor.reasoning_effort is not None:
        _, seeded_effort = validate_session_model_metadata(
            model_override=None,
            reasoning_effort=spec.executor.reasoning_effort,
        )
        metadata = metadata.model_copy(update={"reasoning_effort": seeded_effort})

    agent_id = generate_agent_id()
    agent_bundle_location = bundle_location(agent_id, bundle_bytes)
    try:
        artifact_store.put(agent_bundle_location, bundle_bytes)
    except Exception:
        _delete_stored_session_bundle_after_failure(
            artifact_store,
            agent_bundle_location,
        )
        raise
    return _persist_stored_session_bundle(
        conversation_store,
        artifact_store,
        metadata,
        agent_id=agent_id,
        agent_name=spec.name,
        agent_bundle_location=agent_bundle_location,
        agent_description=spec.description,
        runner_id=runner_id,
        inference_snapshot=inference_snapshot,
        inference_model=inference_model,
        created_by=created_by,
    )


async def _child_session_summaries_from_conversations(
    children: list[Conversation],
    parent_session_id: str,
    conv_store: ConversationStore,
) -> list[ChildSessionSummary]:
    """
    Build child summaries with one batched message-preview lookup.

    ``ChildSessionSummary.last_message_preview`` needs the latest visible
    message per child. Loading those by calling ``list_items`` once per
    child blocks the event loop and creates N+1 database traffic. This
    helper reads newest message items for all child ids in a worker
    thread, computes previews in memory, then builds summaries without
    further store access.

    :param children: Child conversation rows from
        ``list_conversations(kind="sub_agent")``.
    :param parent_session_id: Parent session id, e.g. ``"conv_parent987"``.
    :param conv_store: Conversation store used for the batched message read.
    :returns: One :class:`ChildSessionSummary` per input child, preserving
        input order.
    """
    if not children:
        return []
    child_ids = [child.id for child in children]
    message_items_by_child = await asyncio.to_thread(
        conv_store.list_latest_message_items_for_conversations,
        child_ids,
        10,
    )
    previews = {
        child_id: _latest_message_preview(message_items)
        for child_id, message_items in message_items_by_child.items()
    }
    return [
        _child_session_summary_from_conversation(
            child,
            parent_session_id,
            previews.get(child.id),
        )
        for child in children
    ]


# A tool result past this size is parsed and serialised on a worker thread: a
# deck result is about 1 MB, and decoding it on the loop stalls every session.
_MCP_LARGE_JSON_BYTES = 64 * 1024


async def _runner_json(resp: httpx.Response) -> Any:
    """
    Decode a runner response body, off the event loop when it is large.

    :param resp: A completed (non-streaming) runner response.
    :returns: The decoded JSON value.
    """
    raw = resp.content
    if len(raw) > _MCP_LARGE_JSON_BYTES:
        return await asyncio.to_thread(json.loads, raw)
    return resp.json()


def _arguments_preview(arguments: dict[str, Any], limit: int = 1024) -> str:
    """
    Return the first *limit* characters of ``json.dumps(arguments)``.

    Top-level strings are cut to *limit* first, so a large argument (a file
    body) is not serialised in full only to be sliced. The prefix is the same:
    a string's JSON form is never shorter than the string.

    :param arguments: The tool call's arguments.
    :param limit: Preview length in characters.
    :returns: The JSON preview.
    """
    cut = {
        key: value[:limit] if isinstance(value, str) else value for key, value in arguments.items()
    }
    return json.dumps(cut)[:limit]


async def _handle_mcp_tools_call(
    rpc_id: int | str | None,
    session_id: str,
    params: dict[str, Any],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None,
    *,
    actor: dict[str, str] | None = None,
    request: Request | None = None,
) -> Response:
    """
    Handle a ``tools/call`` JSON-RPC request for the MCP proxy endpoint.

    Steps:

    1. Validate the tool name (namespaced like ``github__search`` for MCP
       tools, or bare like ``sys_os_read`` for runner-local tools).
    2. Load session → agent → spec for policy evaluation.
    3. On first call: evaluate TOOL_CALL policy.  On DENY, return error.
       On ASK, emit a ``response.elicitation_request`` SSE event and
       return an MCP ``InputRequiredResult`` so the runner can park for
       user approval and retry per the MRTR spec.
    4. On retry (``requestState`` present in ``params``): verify the
       state, check the user's ``inputResponses``, and proceed if
       approved.
    5. Delegate execution to the runner's ``POST
       /v1/sessions/{id}/mcp/execute`` endpoint via the WS tunnel so
       that stdio MCP subprocesses and runner-local tools execute on the
       runner's machine (correct ``cwd``, environment, and tooling).
    6. Evaluate the TOOL_RESULT policy phase on the returned output;
       replace with a redaction notice on DENY.
    7. Return the result in MCP ``content`` format.

    :param rpc_id: The JSON-RPC request id, e.g. ``1``.
    :param session_id: The session id, e.g. ``"conv_abc123"``.
    :param params: The JSON-RPC ``params`` object.  On first call,
        contains ``"name"`` and ``"arguments"``.  On retry, also
        contains ``"requestState"`` (opaque blob from the server) and
        ``"inputResponses"`` (user's approval decision), e.g.
        ``{"name": "sys_os_shell", "arguments": {}, "requestState": "...",
        "inputResponses": {"elicit_abc": {"action": "accept"}}}``.
    :param conversation_store: Store for session and label state.
    :param agent_store: Store for agent lookup.
    :param runner_router: Router used to get a tunneled client pointed at
        the session's runner. ``None`` returns an error response.
    :param actor: Authenticated principal, e.g.
        ``{"run_as": "alice@example.com"}``. ``None`` when
        identity is unknown.
    :returns: A JSON-RPC 2.0 response carrying the tool result as MCP
        ``content`` blocks, an ``InputRequiredResult`` on ASK, or an
        error response when the call is denied, the runner is
        unavailable, or the underlying MCP call fails.
    """

    namespaced_name = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments") or {}
    request_state_str: str | None = params.get("requestState")
    input_responses: dict[str, Any] = params.get("inputResponses") or {}
    operation_id_value = params.get(MCP_OPERATION_ID_PARAM)
    if operation_id_value is not None and not (
        isinstance(operation_id_value, str) and 1 <= len(operation_id_value) <= 128
    ):
        return _mcp_error_response(rpc_id, -32000, "Invalid runner MCP operation id")
    operation_id = cast("str | None", operation_id_value)
    is_retry = request_state_str is not None

    def _runner_execute_body(
        execute_params: dict[str, Any],
        *,
        step: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"method": "tools/call", "params": execute_params}
        if operation_id is not None:
            body["_omnigent_operation"] = {"id": operation_id, "step": step}
        return body

    _logger.debug(
        "MCP tools/call: session=%r tool=%r is_retry=%r",
        session_id,
        namespaced_name,
        is_retry,
        extra={"session_id": session_id},
    )

    if not namespaced_name:
        return _mcp_error_response(rpc_id, -32000, "Missing tool name in tools/call params")

    # Session → agent → spec (needed for policy evaluation on both paths).
    # All three reads — conversation row, agent row, and the cold-cache
    # bundle fetch + spec parse — are blocking IO. Run them off the event
    # loop so an MCP tool call doesn't stall the single-worker server and
    # serialize concurrent requests behind it.
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None or conv.agent_id is None:
        return _mcp_error_response(
            rpc_id, -32000, f"Session not found or has no agent: {session_id!r}"
        )

    spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
    if spec is None:
        return _mcp_error_response(rpc_id, -32000, f"Agent not found: {conv.agent_id!r}")

    if is_retry:
        # ── Retry path: user has responded to the elicitation ────────
        # Verify the opaque requestState.
        try:
            state = json.loads(request_state_str)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return _mcp_error_response(rpc_id, -32000, "Invalid requestState: not valid JSON")
        if state.get("session_id") != session_id:
            # Reject cross-session replay.
            return _mcp_error_response(rpc_id, -32000, "requestState session mismatch")

        # Re-evaluate policy on every retry; approval cannot override a denial.
        retry_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        engine, retry_result = await _evaluate_policy_with_fresh_engine(
            session_id, spec, conversation_store, conv, retry_ctx
        )

        _logger.debug(
            "MCP tools/call retry TOOL_CALL policy: session=%r tool=%r action=%r reason=%r",
            session_id,
            namespaced_name,
            retry_result.action,
            retry_result.reason,
            extra={"session_id": session_id},
        )

        if retry_result.action == PolicyAction.DENY:
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {retry_result.reason or 'no reason given'}",
            )

        if retry_result.action == PolicyAction.ASK:
            # Policy still requires approval — verify the elicitation was
            # genuinely issued by the server (present in the pending map) and
            # that the user approved it.
            elicitation_id_from_state: str = state.get("elicitation_id", "")
            if elicitation_id_from_state not in _pending_policy_ask_writes:
                approval: dict[str, Any] = input_responses.get(elicitation_id_from_state) or {}
                if approval.get("action") == "accept":
                    return _mcp_error_response(
                        rpc_id, -32000, "Elicitation not found or already resolved"
                    )
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            approval = input_responses.get(elicitation_id_from_state) or {}
            if approval.get("action") != "accept":
                return _mcp_error_response(rpc_id, -32000, "Tool call denied by user")
            _pending = _pending_policy_ask_writes.pop(elicitation_id_from_state, None)
            # Approval applies to the stored call and its reviewed transform.
            # Older pending entries use the re-evaluated transform.
            if _pending is not None and _pending.reviewed_arguments is not None:
                if arguments != _pending.reviewed_arguments:
                    return _mcp_error_response(
                        rpc_id, -32000, "Retry arguments do not match the approved request"
                    )
                if _pending.transformed_arguments is not None:
                    arguments = cast("dict[str, object]", _pending.transformed_arguments)
                else:
                    arguments = cast("dict[str, object]", _pending.reviewed_arguments)
            elif retry_result.data is not None:
                arguments = cast("dict[str, object]", retry_result.data)
            # Apply the deciding policy's deferred writes now that the user
            # approved (POLICIES.md §7.2: only on accept).
            if _pending is not None:
                if _pending.set_labels:
                    await asyncio.to_thread(engine.apply_label_writes, _pending.set_labels)
                if _pending.state_updates:
                    with contextlib.suppress(ConversationNotFoundError):
                        await asyncio.to_thread(engine.apply_state_updates, _pending.state_updates)
        else:
            # The current policy controls arguments when approval is no longer required.
            if retry_result.data is not None:
                arguments = cast("dict[str, object]", retry_result.data)
        # Fall through to execution.
    else:
        # ── First call: evaluate TOOL_CALL policy ────────────────────
        call_ctx = EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": namespaced_name, "arguments": arguments},
            tool_name=namespaced_name,
            actor=actor,
        )
        engine, call_result = await _evaluate_policy_with_fresh_engine(
            session_id, spec, conversation_store, conv, call_ctx
        )

        # A gate that stops a call is logged at INFO so the tool and policy are on record.
        (_logger.debug if call_result.action == PolicyAction.ALLOW else _logger.info)(
            "MCP tools/call TOOL_CALL policy: session=%r tool=%r action=%r policy=%r reason=%r",
            session_id,
            namespaced_name,
            call_result.action,
            call_result.deciding_policies,
            call_result.reason,
            extra={"session_id": session_id},
        )

        if call_result.action == PolicyAction.DENY:
            if call_result.set_labels:
                await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
            return _mcp_error_response(
                rpc_id,
                -32000,
                f"Denied by policy: {call_result.reason or 'no reason given'}",
            )

        if call_result.action == PolicyAction.ASK:
            # Emit elicitation SSE event (for REPL approval UI) and return
            # InputRequiredResult per the MCP MRTR spec so the runner can
            # park on the approval Future and retry when the user decides.
            elicitation_id = await _register_policy_elicitation(
                session_id,
                call_result,
                _arguments_preview(arguments),
                conversation_store,
                tool_name=namespaced_name,
                actor=actor,
            )
            # Keep the reviewed call and deferred writes together until approval.
            _pending_policy_ask_writes[elicitation_id] = _PendingPolicyAskWrites(
                state_updates=call_result.state_updates,
                set_labels=call_result.set_labels,
                from_mcp=True,
                reviewed_arguments=arguments,
                transformed_arguments=cast("dict[str, object] | None", call_result.data),
            )
            # The client carries identifiers; reviewed arguments stay on the server.
            request_state_payload: dict[str, Any] = {
                "elicitation_id": elicitation_id,
                "session_id": session_id,
            }
            request_state = json.dumps(request_state_payload)
            return _mcp_input_required_response(
                rpc_id,
                elicitation_id=elicitation_id,
                message=call_result.reason or "Approval required to run this tool",
                request_state=request_state,
                session_id=session_id,
                ask_timeout=resolve_ask_timeout(engine, call_result),
            )
        # ALLOW — apply labels now that we know the action is not ASK.
        if call_result.set_labels:
            await asyncio.to_thread(engine.apply_label_writes, call_result.set_labels)
        # If the policy returned transformed arguments (e.g.
        # PII-redacted args), use them instead of the originals.
        if call_result.data is not None:
            arguments = cast("dict[str, object]", call_result.data)

    # ── Server-side sys_advise_models intercept ──────────────────────────
    # After policy evaluation (DENY/ASK handled above); arguments may have
    # been transformed. The advisor runs server-side where routing_client lives.
    if namespaced_name in ("sys_advise_models", "mcp__omnigent__sys_advise_models"):
        return await _handle_advise_models_mcp(
            rpc_id,
            conv,
            arguments,
            agent_store,
            session_id=session_id,
            runner_router=runner_router,
        )

    # ── Execute on the runner via WS tunnel ──────────────────────────
    # The runner owns stdio subprocess spawning (correct machine, cwd,
    # and env). We call its /mcp/execute endpoint through the same WS
    # tunnel the runner already opened to the Omnigent server at startup.
    runner_client = await _get_runner_client(session_id, runner_router)
    if runner_client is None:
        from omnigent.runtime import get_runner_client

        runner_client = cast("httpx.AsyncClient | None", get_runner_client())
    if runner_client is None:
        return _mcp_error_response(rpc_id, -32000, f"No runner bound for session {session_id!r}")
    try:
        from omnigent.runner.tool_dispatch import MCP_PROXY_FORWARD_TIMEOUT_S

        exec_resp = await runner_client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json=_runner_execute_body(
                {"name": namespaced_name, "arguments": arguments},
                step="initial",
            ),
            # ``sys_session_send`` returns a launch handle immediately; this
            # timeout now protects ordinary runner proxy hangs.
            timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
        )
        exec_resp.raise_for_status()
        exec_data = await _runner_json(exec_resp)
    except ConnectionError as exc:
        _logger.warning("Runner MCP execute detached: %s", exc, exc_info=True)
        if operation_id is not None:
            return _mcp_error_response(
                rpc_id,
                RUNNER_MCP_EXECUTION_DETACHED_CODE,
                RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
            )
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Runner MCP execute failed: %s", exc, exc_info=True)
        return _mcp_error_response(rpc_id, -32000, "Runner MCP execute failed.")

    if "error" in exec_data:
        err = exec_data["error"]
        return _mcp_error_response(
            rpc_id, err.get("code", -32000), err.get("message", "unknown error")
        )

    # ── MRTR: external MCP server needs user input ───────────────
    # The runner returns ``{"result": {"input_required": {...}}}``
    # when the external MCP server sent an ``InputRequiredResult``.
    # Surface each elicitation to the user via the existing SSE
    # infrastructure, gather responses, then retry on the runner.
    mcp_input_required = exec_data.get("result", {}).get("input_required")
    if mcp_input_required is not None:
        if request is None:
            return _mcp_error_response(
                rpc_id, -32000, "MCP server requires elicitation but no request context available"
            )
        input_requests: dict[str, Any] = mcp_input_required.get("inputRequests") or {}
        mcp_request_state: str = mcp_input_required.get("requestState", "")

        # Gather user responses for each inputRequest.
        elicitation_responses: dict[str, Any] = {}
        for eid, req_entry in input_requests.items():
            req_params = req_entry.get("params", {}) if isinstance(req_entry, dict) else {}
            elicit_params = ElicitationRequestParams(
                mode=cast(Literal["form", "url"], req_params.get("mode", "form")),
                message=req_params.get("message", "Approval required"),
                requestedSchema=cast(
                    Mapping[str, Any] | None,
                    req_params.get("requestedSchema"),
                ),
            )
            elicit_result = await _publish_and_wait_for_harness_elicitation(
                request,
                session_id=session_id,
                params=elicit_params,
                timeout_s=300.0,
                conversation_store=conversation_store,
            )
            if elicit_result is None:
                elicitation_responses[eid] = {"action": "decline"}
            else:
                resp_entry: dict[str, Any] = {"action": elicit_result.action}
                if elicit_result.content is not None:
                    resp_entry["content"] = elicit_result.content
                elicitation_responses[eid] = resp_entry

        # Retry on the runner with the user's inputResponses.
        try:
            retry_resp = await runner_client.post(
                f"/v1/sessions/{session_id}/mcp/execute",
                json=_runner_execute_body(
                    {
                        "name": namespaced_name,
                        "arguments": arguments,
                        "inputResponses": elicitation_responses,
                        "requestState": mcp_request_state,
                    },
                    step="elicitation-response",
                ),
                timeout=MCP_PROXY_FORWARD_TIMEOUT_S,
            )
            retry_resp.raise_for_status()
            exec_data = await _runner_json(retry_resp)
        except ConnectionError as exc:
            _logger.warning("Runner MCP retry detached: %s", exc, exc_info=True)
            if operation_id is not None:
                return _mcp_error_response(
                    rpc_id,
                    RUNNER_MCP_EXECUTION_DETACHED_CODE,
                    RUNNER_MCP_EXECUTION_DETACHED_MESSAGE,
                )
            return _mcp_error_response(rpc_id, -32000, "Runner MCP retry failed.")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Runner MCP retry failed: %s", exc, exc_info=True)
            return _mcp_error_response(rpc_id, -32000, "Runner MCP retry failed.")
        if "error" in exec_data:
            err = exec_data["error"]
            return _mcp_error_response(
                rpc_id, err.get("code", -32000), err.get("message", "unknown error")
            )
        # Multi-round MRTR: the server returned yet another
        # InputRequiredResult on the retry. Return an error rather
        # than looping indefinitely — the user can retry the tool.
        if exec_data.get("result", {}).get("input_required") is not None:
            return _mcp_error_response(
                rpc_id,
                -32000,
                "MCP server requires additional elicitation rounds (not yet supported)",
            )

    output: str = exec_data.get("result", {}).get("output", "")
    # A browser result names the page's elements, so a later click can be recognised.
    remember_browser_snapshot(session_id, namespaced_name, output)
    _logger.debug(
        "MCP tools/call execute: session=%r tool=%r output_len=%d",
        session_id,
        namespaced_name,
        len(output),
        extra={"session_id": session_id},
    )

    # ── TOOL_RESULT policy ───────────────────────────────────────────
    result_ctx = EvaluationContext(
        phase=Phase.TOOL_RESULT,
        content={"result": output},
        tool_name=namespaced_name,
        request_data={"name": namespaced_name, "arguments": arguments},
        actor=actor,
    )
    engine, result_policy = await _evaluate_policy_with_fresh_engine(
        session_id, spec, conversation_store, conv, result_ctx
    )

    if result_policy.set_labels:
        await asyncio.to_thread(engine.apply_label_writes, result_policy.set_labels)

    _logger.debug(
        "MCP tools/call TOOL_RESULT policy: session=%r tool=%r action=%r reason=%r",
        session_id,
        namespaced_name,
        result_policy.action,
        result_policy.reason,
        extra={"session_id": session_id},
    )

    if result_policy.action == PolicyAction.DENY:
        output = f"[Result suppressed by policy: {result_policy.reason or 'no reason given'}]"
    elif result_policy.data is not None:
        # Policy returned transformed output (e.g. PII-redacted content).
        # The TOOL_RESULT phase contract requires data to be a str; coerce
        # and warn rather than dropping the result if a policy author returns
        # the wrong type (common mistake: returning the full content dict).
        if not isinstance(result_policy.data, str):
            _logger.warning(
                "TOOL_RESULT policy data must be str; got %s — coercing via str()",
                type(result_policy.data).__name__,
            )
        output = (
            result_policy.data if isinstance(result_policy.data, str) else str(result_policy.data)
        )

    result_body = {"content": [{"type": "text", "text": output}]}
    if len(output) > _MCP_LARGE_JSON_BYTES:
        return await asyncio.to_thread(_mcp_ok_response, rpc_id, result_body)
    return _mcp_ok_response(rpc_id, result_body)


# The tunnel transport does not enforce httpx timeouts, so the runner status
# probe below is bounded here. Healthy runners answer from memory (99.85% of
# production probes finish inside 5 s), and the runner's own launch-config
# read of this snapshot has a 10 s budget the bound must leave room for.
_RUNNER_STATUS_PROBE_TIMEOUT_S: float = 5.0
# A non-200 that arrives within this is a prompt answer from a responsive
# runner (a freshly bound one answers 404 until session init lands) and is
# asked again next snapshot; a slower one counts as a slow probe.
_RUNNER_STATUS_PROBE_SLOW_S: float = 1.0
# After a slow probe the session's probe is skipped for a window that doubles
# per consecutive slow probe up to the cap, so a runner stalled for hours
# costs a probe every few minutes instead of every window. A prompt answer
# resets the streak.
_RUNNER_STATUS_PROBE_BACKOFF_S: float = 30.0
_RUNNER_STATUS_PROBE_BACKOFF_CAP_S: float = 300.0


def _runner_status_probe_window_s(failures: int) -> float:
    """
    Return the skip window after *failures* consecutive slow probes.

    :param failures: Consecutive slow probes so far, e.g. ``1`` after the first.
    :returns: Seconds to skip the probe, e.g. ``30.0`` then ``60.0``, capped.
    """
    return min(
        _RUNNER_STATUS_PROBE_BACKOFF_S * (2 ** (failures - 1)),
        _RUNNER_STATUS_PROBE_BACKOFF_CAP_S,
    )


async def _probe_runner_live_status(
    runner_client: httpx.AsyncClient, session_id: str, runner_id: str | None = None
) -> str | None:
    """
    Ask a session's bound runner for its live status, bounded, shared, and backed off.

    Concurrent snapshots of one session await the same in-flight probe. A 200
    records the status in ``_session_status_cache``; a probe that timed out,
    failed in transport, or answered slowly without a status puts the session
    in a skip window that doubles per consecutive slow probe.

    :param runner_client: HTTP client pointed at the session's runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param runner_id: The session's bound runner, e.g.
        ``"runner_0123456789abcdef"``. A skip window recorded against a
        different runner is discarded so a rebound session is probed at once.
    :returns: The runner's raw status, e.g. ``"running"``, or ``None`` when the
        probe is in backoff, timed out, failed, or returned a non-200.
    """
    probe = _runner_status_probe_inflight.get(session_id)
    if probe is None:
        backoff = _runner_status_probe_backoff.get(session_id)
        if backoff is not None and backoff.runner_id != runner_id:
            _runner_status_probe_backoff.pop(session_id, None)
        elif backoff is not None and time.monotonic() < backoff.skip_until:
            return None
        probe = asyncio.create_task(_run_runner_status_probe(runner_client, session_id, runner_id))
        _runner_status_probe_inflight[session_id] = probe
    # Shielded so one cancelled snapshot request does not abort the probe the
    # other waiters share.
    return await asyncio.shield(probe)


async def _run_runner_status_probe(
    runner_client: httpx.AsyncClient, session_id: str, runner_id: str | None
) -> str | None:
    """
    Run one bounded runner status probe and record its outcome.

    :param runner_client: HTTP client pointed at the session's runner.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param runner_id: The session's bound runner, recorded with any skip window.
    :returns: The runner's raw status on a 200, else ``None``.
    """
    started = time.monotonic()
    cache_before = _session_status_cache.get(session_id)
    try:
        try:
            resp = await asyncio.wait_for(
                runner_client.get(
                    f"/v1/sessions/{session_id}", timeout=_RUNNER_STATUS_PROBE_TIMEOUT_S
                ),
                timeout=_RUNNER_STATUS_PROBE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            failure = f"no answer within {_RUNNER_STATUS_PROBE_TIMEOUT_S:g}s"
        except (httpx.HTTPError, ConnectionError) as exc:
            failure = f"{type(exc).__name__}: {exc}"
        else:
            elapsed = time.monotonic() - started
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    raw = str(payload.get("status", "idle"))
                    _runner_status_probe_backoff.pop(session_id, None)
                    current = _session_status_cache.get(session_id)
                    if current is not None and current != cache_before:
                        # The runner's live relay published a status while this probe was in
                        # flight. The relay is the newer, ordered source; the probe's answer
                        # describes an earlier moment, and applying it would turn a finished
                        # turn back into ``running`` with no later edge to clear it.
                        return current
                    _session_status_cache[session_id] = raw
                    if raw in ("idle", "running", "waiting", "failed"):
                        session_live_state.persist_live_status(session_id, raw)
                    return raw
                failure = "HTTP 200 with a malformed body"
            elif elapsed < _RUNNER_STATUS_PROBE_SLOW_S:
                _runner_status_probe_backoff.pop(session_id, None)
                _logger.debug(
                    "Runner status probe for session=%s answered HTTP %s",
                    session_id,
                    resp.status_code,
                    extra={"session_id": session_id},
                )
                return None
            else:
                failure = f"HTTP {resp.status_code} after {elapsed:.1f}s"
        previous = _runner_status_probe_backoff.get(session_id)
        failures = (previous.failures if previous is not None else 0) + 1
        window = _runner_status_probe_window_s(failures)
        _runner_status_probe_backoff[session_id] = _RunnerStatusProbeBackoff(
            skip_until=time.monotonic() + window, failures=failures, runner_id=runner_id
        )
        _logger.warning(
            "Runner status probe for session=%s failed (%s); skipping it for %.0fs",
            session_id,
            failure,
            window,
            extra={"session_id": session_id},
        )
        return None
    finally:
        _runner_status_probe_inflight.pop(session_id, None)


async def _get_session_snapshot(
    conv_store: ConversationStore,
    session_id: str,
    permission_level: int | None = None,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
    conversation: Conversation | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    include_items: bool = True,
    runner_exit_reports: RunnerExitReports | None = None,
    refresh_state: bool = False,
    host_store: HostStore | None = None,
    sandbox_config: HostDeployment | None = None,
    viewer_id: str | None = None,
    request: Request | None = None,
    include_usage: bool = True,
) -> SessionResponse:
    """
    Read a full session snapshot from the store.

    Centralizes the create/get response building so both endpoints
    return identical projections. The lifecycle ``status`` is
    derived from the relay-fed ``_session_status_cache``, falling back to
    the persisted relay status after a server recycle (the tasks table is gone).

    :param conv_store: The conversation store to read from.
    :param session_id: Session/conversation identifier,
        e.g. ``"conv_abc123"``.
    :param permission_level: The requesting user's numeric level
        on this session, or ``None`` when permissions are disabled.
    :param agent_store: Optional agent store used to look up the
        bound agent's bundle location. ``None`` in legacy call sites
        that don't yet pass it.
    :param agent_cache: Optional agent cache used to load the parsed
        spec from the bundle (provides ``llm_model`` and
        ``context_window``). ``None`` in legacy call sites.
    :param conversation: The already-fetched conversation row to reuse,
        skipping the ``get_conversation`` read. Pass it when the caller
        just authorized the session (which fetched the same row) so the
        snapshot doesn't re-read it. ``None`` reads it here as before.
    :param liveness_lookup: Bulk session-liveness lookup (the server's
        ``_bulk_session_liveness``) used to populate ``runner_online``
        and ``host_online`` on the snapshot. ``None`` (e.g. focused
        tests) leaves both fields ``None`` so the client falls back to
        its ``/health`` poll.
    :param include_items: When ``False``, skip the committed-items read
        and return ``items=[]``. Callers that hydrate the transcript
        through ``GET /sessions/{id}/items`` (the web chat surface)
        pass ``False`` to avoid a redundant history read and serialization.
    :param include_usage: When ``False``, skip subtree usage aggregation and
        return unknown usage with ``usage_included=False``. Launch metadata
        does not need usage; display clients can fetch it separately.
    :param refresh_state: When ``True``, clear runner-backed snapshot
        overlays for this session before building the response. Browser
        reloads use this so a refresh re-reads current live-session
        capabilities instead of serving stale AP-process caches.
    :returns: The fully populated :class:`SessionResponse`.
    :raises OmnigentError: 404 if no session exists, 500 if the
        underlying conversation has no agent binding
        (see :func:`_build_session_response`).
    """
    conv = conversation
    if conv is None:
        conv = await asyncio.to_thread(conv_store.get_conversation, session_id)
    if conv is None:
        raise _session_not_found()
    # Return the most recent committed items while preserving the
    # SessionResponse contract that ``items`` is chronological. The
    # store's default page is the oldest 100 (``order="asc"``), which
    # makes long-session reconnects appear stale in clients that use the
    # snapshot directly.
    items: list[ConversationItem] = []
    if include_items:
        items_page = await asyncio.to_thread(
            conv_store.list_items,
            conversation_id=session_id,
            limit=100,
            order="desc",
        )
        items = list(reversed(items_page.data))
    # Resolve the bound runner client once — used for live status (on a
    # status-cache miss) and for native model options below.
    #
    # Prefer the router (multi-runner deployments wire only
    # ``set_runner_router``; the legacy ``get_runner_client`` singleton
    # stays ``None`` there). Fall back to the legacy singleton for
    # single-runner / in-process tests.
    from omnigent.runtime import get_runner_client, get_runner_router

    runner_client: httpx.AsyncClient | None = None
    runner_router = get_runner_router()
    if runner_router is not None:
        try:
            # Pass the authorized row: the router's own point read would be a
            # second read of the conversation this snapshot already holds, and on
            # a split-DB deployment that is one round trip per backend.
            routed = runner_router.client_for_session_resources(session_id, conversation=conv)
            runner_client = routed.client
        except (LookupError, httpx.HTTPError, OmnigentError):
            _logger.debug(
                "No runner bound for session=%s on snapshot build",
                session_id,
                extra={"session_id": session_id},
            )
    if runner_client is None:
        runner_client = get_runner_client()

    if refresh_state:
        _invalidate_runner_backed_snapshot_state(session_id)

    # A server recycle clears this cache while the persisted relay status survives.
    # Prefer it because native injection can finish before an external harness turn,
    # making the runner's generic active-turn probe report a false ``idle``.
    status = _session_status_from_cache(session_id, conv.live_status)
    if status == "idle":
        # Cache miss (or truly idle): either the server restarted, or the
        # relay has not yet published the first ``"running"`` event for a
        # freshly bound session (the relay's GET /stream is still in its
        # tunnel handshake). Ask the runner for live status so we don't
        # synthesize a stale ``"idle"`` while a turn is actually in flight.
        # ``_session_status_from_cache`` already collapses the fine-grained
        # relay values (``"waiting"`` → ``"running"``), so the raw cache value
        # is only needed here when it is actually missing (None).
        if _session_status_cache.get(session_id) is None and runner_client is not None:
            if (
                await _probe_runner_live_status(runner_client, session_id, conv.runner_id)
                is not None
            ):
                status = _session_status_from_cache(session_id)
    # last_total_tokens and last_task_error come from the context-tokens
    # label written by the forwarder (tasks table has been removed).
    last_total_tokens: int | None = None
    last_task_error: dict[str, str] | None = None
    raw_label = conv.labels.get(_LAST_CONTEXT_TOKENS_LABEL_KEY)
    if isinstance(raw_label, str) and raw_label.isdigit():
        last_total_tokens = int(raw_label)
    last_task_error = _last_task_error_from_labels(conv.labels)
    # Runner-crash durability: if the session's bound runner reported an
    # unexpected exit (host.runner_exited → RunnerExitReports), surface the
    # cause as last_task_error so a reload/late-open still renders the error
    # banner — the live session.status:failed push is gone by then. status
    # already reads "failed" from the cache (set by _on_runner_exited). The
    # report is keyed by the CURRENT runner_id, so a successful relaunch
    # (new token-bound runner_id) naturally stops matching. Access is gated
    # by the session-snapshot's own authorization, so the unscoped get is
    # correct here (the report is this session's own runner).
    if runner_exit_reports is not None and conv.runner_id is not None:
        exit_error = runner_exit_reports.get(conv.runner_id)
        if exit_error is not None:
            last_task_error = {"code": "runner_failed_to_start", "message": exit_error}
            status = "failed"
    llm_model: str | None = None
    context_window: int | None = None
    agent_name: str | None = None
    if agent_store is not None and agent_cache is not None and conv.agent_id is not None:
        try:
            agent = await asyncio.to_thread(agent_store.get, conv.agent_id)
            if agent is not None:
                agent_name = agent.name
                if agent.bundle_location is not None:
                    # Offload to a worker thread: on a cold cache this fetches
                    # the bundle from the artifact store and parses the spec —
                    # blocking IO that would otherwise stall the single-worker
                    # event loop on every page-load snapshot.
                    loaded = await asyncio.to_thread(
                        agent_cache.load,
                        agent.id,
                        agent.bundle_location,
                        expand_env=agent.session_id is None,
                    )
                    resolved_spec: AgentSpec | None = loaded.spec
                    if conv.sub_agent_name:
                        _sub_spec = _find_spec_by_name(loaded.spec, conv.sub_agent_name)
                        if _sub_spec is None:
                            # Unresolvable sub_agent_name — fall back to parent spec.
                            _logger.warning(
                                "Sub-agent %r for session %s did not resolve in the parent "
                                "spec; snapshot reports the parent's identity. Likely a "
                                "renamed/removed sub-agent or stale session metadata.",
                                conv.sub_agent_name,
                                session_id,
                                extra={"session_id": session_id},
                            )
                        else:
                            resolved_spec = _sub_spec
                    if resolved_spec is not None:
                        # Prefer the spec's name over the agent row's: the
                        # spec carries the clean identity (e.g. "claude-native-ui").
                        if resolved_spec.name:
                            agent_name = resolved_spec.name
                        llm_model = resolved_spec.executor.model

                        # Offload: may fetch a provider catalog (blocking IO).
                        context_window = await asyncio.to_thread(
                            resolve_effective_context_window,
                            resolved_spec.executor.context_window,
                            llm_model,
                            model_override=conv.model_override,
                        )
        except Exception:  # noqa: BLE001
            pass
    # The harness's own report is the display authority: when a session has
    # a verbatim ``reported_model``, it supersedes the spec-derived value on
    # the wire's ``llm_model`` field (the web renders and highlights only
    # from this).
    if reported_model := concrete_reported_model(conv.reported_model):
        llm_model = reported_model
    # A configured inference profile owns the session's model picker rows.
    from omnigent.server.routes.sandbox_inference import configured_snapshot, inference_service

    inference_configured = configured_snapshot(conv.inference_snapshot)
    inference_error = None
    model_options: list[dict[str, Any]] | None = None
    if inference_configured and conv.inference_snapshot is not None:
        catalog = (
            await inference_service(request).catalog(conv.inference_snapshot)
            if request is not None
            else conv.inference_snapshot["catalog"]
        )
        model_options = catalog["models"]
        inference_error = catalog.get("error")
        if not conv.reported_model:
            llm_model = conv.model_override or catalog.get("default_model")
    # Dynamic override from the forwarder (real Claude Code window).
    # Only present after the first statusLine tick; before that the
    # spec default applies.
    raw_window_label = conv.labels.get(_LAST_CONTEXT_WINDOW_LABEL_KEY)
    if isinstance(raw_window_label, str) and raw_window_label.isdigit():
        observed = int(raw_window_label)
        if observed > 0:
            context_window = observed
    # Resolve strict runner + host liveness for the open-session view.
    # The lookup hits the conversations + hosts tables, so offload it to
    # a worker thread (mirroring _apply_liveness_to_items). Left None on
    # both fields when no lookup is wired (focused tests).
    runner_online: bool | None = None
    host_online: bool | None = None
    if liveness_lookup is not None:
        liveness = await asyncio.to_thread(liveness_lookup, [session_id])
        result = liveness.get(session_id)
        if result is not None:
            runner_online = result.runner_online
            host_online = result.host_online
    # Display costs include descendants. Empty usage marks a skipped aggregate;
    # None would fall back to the parent's own spend and under-report it.
    subtree_usage = (
        await asyncio.to_thread(
            load_session_usage,
            conv.id,
            conv_store,
            root_conversation_id=conv.root_conversation_id,
        )
        if include_usage
        else {}
    )
    # Static signal telling the open view a host-bound, host-down session is a
    # resumable managed host it can wake by sending a message, vs a terminal
    # host_offline dead-end. Computed independently of liveness_lookup (the web
    # chat passes include_liveness=False, so host_online is None here and
    # liveness arrives via the poll/stream). One indexed host read, gated to
    # host-bound sessions.
    host_resumable = False
    if host_store is not None and sandbox_config is not None and conv.host_id is not None:
        host_for_resume = await asyncio.to_thread(host_store.get_host, conv.host_id)
        if host_for_resume is not None:
            host_resumable = host_resume_supported(host_for_resume, sandbox_config)
    response = _build_session_response(
        conv,
        items,
        status,
        permission_level,
        background_task_count=_session_background_task_count_cache.get(session_id),
        background_tasks=_session_background_tasks_cache.get(session_id),
        llm_model=llm_model,
        context_window=context_window,
        last_total_tokens=last_total_tokens,
        last_task_error=last_task_error,
        agent_name=agent_name,
        model_options=model_options,
        runner_online=runner_online,
        host_online=host_online,
        host_resumable=host_resumable,
        pending_elicitation_events=await asyncio.to_thread(
            _pending_elicitation_snapshot_for_session,
            conv_store,
            conv,
        ),
        subtree_usage=subtree_usage,
        viewer_id=viewer_id,
        agent_store=agent_store,
        agent_cache=agent_cache,
    )
    response.inference_configured = inference_configured
    response.inference_error = inference_error
    response.usage_included = include_usage
    from omnigent.superchat.lineage import session_superchat

    superchat = await asyncio.to_thread(session_superchat, conv_store, conv)
    response.superchat = SessionSuperchat.model_validate(superchat) if superchat else None
    return response


__all__ = [
    "_accumulate_session_usage",
    "_archive_stop",
    "_best_effort_stop",
    "_bind_and_launch_managed_runner",
    "_build_session_list_item",
    "_build_session_response",
    "_cancel_pending_archive_stop",
    "_child_session_summaries_from_conversations",
    "_create_session_from_bundle",
    "_create_session_from_existing_agent",
    "_detached_stop_tasks",
    "_dispatch_session_event_to_runner",
    "_drive_terminal_resolved_elicitation",
    "_enrich_terminal_status_with_subagent_output",
    "_ensure_runner_relay",
    "_ensure_runner_relay_ready",
    "_ensure_runner_session_initialized",
    "_evaluate_input_policy",
    "_evaluate_tool_call_policy",
    "_forward_event_to_runner",
    "_gateway_backed",
    "_get_session_snapshot",
    "_handle_mcp_tools_call",
    "_heal_subagent_runner_binding_via_parent",
    "_hold_native_ask_gate",
    "_kick_managed_relaunch",
    "_kick_managed_wake",
    "_labels_for_viewer",
    "_maybe_relaunch_managed_sandbox",
    "_maybe_wake_stale_resumable_managed_sandbox",
    "_persist_external_conversation_item",
    "_persist_external_conversation_items",
    "_persist_external_session_usage",
    "_persist_host_launch_failure_turn",
    "_persist_model_change_note",
    "_persist_native_cumulative_usage",
    "_persist_session_event",
    "_persist_user_event_item",
    "_publish_and_wait_for_harness_elicitation",
    "_publish_runner_recovered_status",
    "_publish_subtree_cost_to_ancestors",
    "_recover_subagent_status_forward_via_parent",
    "_register_policy_elicitation",
    "_relay_runner_stream",
    "_resolve_elicitation",
    "_run_managed_launch",
    "_run_managed_wake",
    "_runner_reject_detail",
    "_schedule_deferred_elicitation_clear",
    "_spawn_archive_stop",
    "_spawn_gateway_backed",
    "_wait_for_host_bound_runner_client",
    "_wake_parent_for_blocked_child",
    "configure_subagent_block_notifier",
    "ensure_runner_connected",
    "managed_wake_may_precede_dispatch",
    "schedule_deferred_message_dispatch",
    "session_has_deferred_dispatch",
]
