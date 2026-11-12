"""Events, stream, and delete session routes."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import secrets
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx
from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from omnigent.context.labels import (
    PAUSED_LABEL_KEY,
    ROUND_LABEL_KEY,
    is_artifact_chat,
    is_superside_chat,
)
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import add_audit_attrs, debug_event, mark_request_audit_suppressed
from omnigent.entities import (
    Conversation,
    ConversationItem,
    ErrorData,
    NewConversationItem,
)
from omnigent.entities.conversation import (
    parse_item_data,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    WORKSPACE_MISSING_ERROR_CODE as _WORKSPACE_MISSING_ERROR_CODE,
)
from omnigent.host.frames import (
    classify_launch_refusal as _classify_launch_refusal,
)
from omnigent.host.frames import (
    workspace_missing_message as _workspace_missing_message,
)
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runner.launch_failure import classify_native_turn_error
from omnigent.runner.routing import RunnerRouter, routing_host_id
from omnigent.runner.transports.ws_tunnel.frames import (
    EventAckFrame,
    EventBatchFrame,
    encode_frame,
)
from omnigent.runtime import (
    inflight_text,
    session_stream,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server import presence
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_LOCAL,
    AuthProvider,
    local_single_user_enabled,
)
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    background_session_titles_enabled,
    background_title_prompt,
    prepare_background_session_title,
    schedule_background_child_task_summary,
)
from omnigent.server.host_registry import HostRegistry, RunnerExitReports, multi_replica
from omnigent.server.routes._auth_helpers import (
    attribution_user as _attribution_user,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.common import (
    _ALLOWED_EVENT_TYPES,
    _APPROVAL_TYPE,
    _COMPACT_TYPE,
    _EXTERNAL_ASSISTANT_MESSAGE_TYPE,
    _EXTERNAL_BTW_DISMISS_TYPE,
    _EXTERNAL_BTW_SIDECHAT_TYPE,
    _EXTERNAL_COMPACTION_STATUS_TYPE,
    _EXTERNAL_COMPACTION_STATUS_VALUES,
    _EXTERNAL_CONVERSATION_ITEM_TYPE,
    _EXTERNAL_ELICITATION_RESOLVED_TYPE,
    _EXTERNAL_MCP_STARTUP_STATUS_VALUES,
    _EXTERNAL_MCP_STARTUP_TYPE,
    _EXTERNAL_MODEL_CHANGE_TYPE,
    _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
    _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
    _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
    _EXTERNAL_SESSION_INTERRUPTED_TYPE,
    _EXTERNAL_SESSION_STATUS_TYPE,
    _EXTERNAL_SESSION_STATUS_VALUES,
    _EXTERNAL_SESSION_SUPERSEDED_TYPE,
    _EXTERNAL_SESSION_TITLE_TYPE,
    _EXTERNAL_SESSION_USAGE_TYPE,
    _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
    _HOST_RECONNECT_GRACE_S,
    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
    _INTERRUPT_TYPE,
    _MCP_ELICITATION_TYPE,
    _RETRY_SESSION_TYPE,
    _SLASH_COMMAND_TYPE,
    _SNAPSHOT_RUNNER_TIMEOUT_S,
    _STOP_SESSION_TYPE,
    _SUBAGENT_STATUS_TYPE,
    _intentional_stop_sessions,
    _interrupt_fenced_sessions,
    _logger,
    _session_active_response_cache,
    _session_mcp_startup_cache,
    _session_sandbox_status_cache,
    _session_status_cache,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _TUI_INJECT_FORWARD_TIMEOUT_S,
    SessionLiveness,
    _apply_pending_policy_ask_writes,
    _await_settled_managed_launch,
    _background_task_delivery_status,
    _build_actor,
    _build_skill_slash_command_policy_body,
    _dispatch_skill_slash_command_to_runner,
    _evaluate_output_policy,
    _filesystem_attachment_in_history,
    _forward_session_change_to_runner,
    _get_runner_client,
    _get_runner_client_for_resource_access,
    _launch_runner_on_host,
    _parse_background_tasks,
    _persist_external_assistant_message,
    _persist_external_model_change,
    _persist_external_reasoning_effort_change,
    _persist_external_session_title,
    _persist_policy_deny_sentinel,
    _persist_session_status_error_labels,
    _prune_session_read_state,
    _publish_btw_sidechat,
    _publish_compaction_completed,
    _publish_compaction_failed,
    _publish_compaction_in_progress,
    _publish_elicitation_request_to_ancestors,
    _publish_external_output_reasoning_delta,
    _publish_external_output_text_delta,
    _publish_external_tool_output_delta,
    _publish_input_deny_terminal,
    _publish_interrupted,
    _publish_mcp_startup,
    _publish_policy_deny,
    _publish_session_superseded,
    _publish_status,
    _record_turn_failure,
    _remove_session_worktree_best_effort,
    _require_external_status_forward,
    _require_filesystem_attachment_harness,
    _resolve_harness,
    _response_agent_name_from_store,
    _session_status_from_cache,
    _signal_harness_elicitation_resolved_by_id,
    _stop_session_host_runner,
    _stop_session_via_runner,
    _stream_live_events,
    _wait_for_runner_client,
    reconcile_orphaned_running_status,
    require_filesystem_attachment_runtime,
)
from omnigent.server.routes._sessions.orchestration import (
    ON_SESSION_DELETED,
    _best_effort_stop,
    _child_session_summaries_from_conversations,
    _dispatch_session_event_to_runner,
    _drop_engine_only_context,
    _enrich_terminal_status_with_subagent_output,
    _ensure_runner_relay_ready,
    _ensure_runner_session_initialized,
    _evaluate_input_policy,
    _evaluate_tool_call_policy,
    _heal_subagent_runner_binding_via_parent,
    _mark_dispatch_in_flight,
    _maybe_relaunch_managed_sandbox,
    _maybe_wake_stale_resumable_managed_sandbox,
    _persist_external_conversation_item,
    _persist_external_conversation_items,
    _persist_external_session_usage,
    _persist_host_launch_failure_turn,
    _persist_user_event_item,
    _resolve_elicitation,
    _signal_elicitation_answered,
    _wait_for_host_bound_runner_client,
    cancel_deferred_message_dispatch,
    ensure_runner_connected,
    managed_wake_may_precede_dispatch,
    schedule_deferred_message_dispatch,
    session_has_deferred_dispatch,
)
from omnigent.server.routes._sessions.turn_metrics import mark_accepted
from omnigent.server.schemas import (
    BackgroundTaskInfo,
    ConversationDeleted,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ErrorDetail,
    McpServerStartup,
    SessionEventInput,
)
from omnigent.session_event_batch import (
    MAX_SESSION_EVENT_BATCH_EVENTS,
    MAX_SESSION_EVENT_REQUEST_BYTES,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import (
    RUNNER_LIVENESS_TTL_S,
    runner_seen_is_fresh,
)
from omnigent.stores.file_store import FileStore
from omnigent.stores.host_store import host_is_live
from omnigent.stores.permission_store import PermissionStore
from omnigent.superchat.lineage import is_super_chat_helper
from omnigent.superchat.models.org import refuse_if_suspended
from omnigent.superchat.rounds import record as rounds
from omnigent.superchat.subagents import is_live_child_row
from omnigent.telemetry import emit as _tel_emit
from omnigent.telemetry.anon import anon_user_id as _tel_anon_user_id
from omnigent.telemetry.events import SessionDeletedEvent as _TelSessionDeletedEvent
from omnigent.telemetry.events import SessionStoppedEvent as _TelSessionStoppedEvent
from omnigent.telemetry.events import TurnEndEvent as _TelTurnEndEvent
from omnigent.telemetry.installation_id import get_installation_id as _get_installation_id
from omnigent.telemetry.request_headers import (
    INSTALLATION_ID_HEADER as _TEL_INSTALLATION_ID_HEADER,
)
from omnigent.telemetry.request_headers import (
    parse_installation_id_header as _tel_parse_installation_id,
)
from omnigent.tools.client_specified import parse_client_side_tool_specs
from omnigent.util.session_lifecycle import (
    is_session_closed,
)

# custom-lint: disable-next=workspace-scoped-cache -- lock; collision only serializes
_retry_recovery_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_retry_recovery_tasks: WorkspaceScopedCache[str, asyncio.Task[dict[str, bool | str]]] = (
    WorkspaceScopedCache()
)

# POST /events types that arrive per streamed chunk — the harness echoing its
# own live output back. Their per-call audit row is pure noise (the content is
# already on the SSE-event logger), so the envelope is suppressed for them.
_TRANSIENT_AUDIT_EVENT_TYPES = frozenset(
    {
        _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
        _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE,
        _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE,
        _EXTERNAL_SESSION_USAGE_TYPE,
    }
)


def _plain_spaces(content: Any) -> Any:
    """A person's message *content* with each U+202F (the narrow no-break space macOS writes in a
    screenshot's name) as a plain space, so a name the model retypes is the name it was given."""
    if isinstance(content, str):
        return content.replace("\u202f", " ")
    if isinstance(content, list):
        return [
            {**block, "text": block["text"].replace("\u202f", " ")}
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            else block
            for block in content
        ]
    return content


def _is_batchable_external_item(event: SessionEventInput) -> bool:
    """
    Whether a batch entry can skip :func:`_post_event_impl` and be appended
    with its neighbors, which is the shape the claude-native forwarder posts.

    For an external item that is not a user message (pending-input drain) or a
    slash command (title seeding), the per-entry path only authorizes, persists
    and broadcasts. Entries carrying ``created_by`` or ``tools`` also stay
    per-entry so their validation is not duplicated here.
    """
    if (
        event.type != _EXTERNAL_CONVERSATION_ITEM_TYPE
        or event.created_by is not None
        or event.tools
    ):
        return False
    item_type = event.data.get("item_type")
    item_data = event.data.get("item_data")
    is_user_message = (
        item_type == "message" and isinstance(item_data, dict) and item_data.get("role") == "user"
    )
    return item_type != "slash_command" and not is_user_message


def _event_body_too_large() -> HTTPException:
    """Build the shared error for an oversized session-event request."""
    return HTTPException(
        status_code=400,
        detail="session event request exceeds the 10 MiB limit",
    )


class _SessionEventBodyLimitRoute(APIRoute):
    """Reject oversized event bodies before FastAPI parses them."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the encoded request limit from headers and ASGI chunks."""
        if scope["type"] != "http":
            await super().handle(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = 0
            if declared_bytes > MAX_SESSION_EVENT_REQUEST_BYTES:
                raise _event_body_too_large()

        received_bytes = 0

        async def receive_bounded() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_SESSION_EVENT_REQUEST_BYTES:
                    raise _event_body_too_large()
            return message

        await super().handle(scope, receive_bounded, send)


def _retry_recovery_lock(session_id: str) -> asyncio.Lock:
    """Return the process-local lock coordinating one session's retry."""
    lock = _retry_recovery_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _retry_recovery_locks[session_id] = lock
    return lock


def _evict_retry_recovery_task(
    session_id: str,
    completed_task: asyncio.Task[dict[str, bool | str]],
) -> None:
    """Evict a settled retry task without disturbing a newer attempt."""
    if _retry_recovery_tasks.get(session_id) is completed_task:
        _retry_recovery_tasks.pop(session_id, None)
    if not completed_task.cancelled():
        completed_task.exception()


async def _raise_if_runner_on_another_replica(
    conv: Conversation,
    app_state: Any,
    conversation_store: ConversationStore,
) -> None:
    """
    Re-address a routing miss instead of treating the runner as gone.

    A runner tunnel registers on the replica holding its host, so a bound
    runner whose tunnel is absent here while that host is live elsewhere means
    the request landed on the wrong replica. That holds for a sub-agent child
    too: it is served by its host-bound ancestor's runner (see
    :func:`routing_host_id`). Raising ``WRONG_REPLICA`` makes the client
    re-address rather than heal or relaunch against this replica's registry.

    :param conv: Session row whose runner client could not be resolved here.
    :param app_state: ``request.app.state`` — supplies the host registry and
        the host store.
    :param conversation_store: Store used to resolve a sub-agent's ancestors.
    :raises OmnigentError: ``WRONG_REPLICA`` when the routing host is live on
        another replica.
    """
    host_registry = getattr(app_state, "host_registry", None)
    host_store = getattr(app_state, "host_store", None)
    if host_registry is None or host_store is None or not multi_replica():
        return
    host_id = await asyncio.to_thread(routing_host_id, conv, conversation_store)
    if host_id is None or host_registry.get(host_id) is not None:
        return
    host = await asyncio.to_thread(host_store.get_host, host_id)
    if host is not None and host_is_live(host):
        raise OmnigentError(
            "session runner is on another replica; retry",
            code=ErrorCode.WRONG_REPLICA,
        )


async def _recover_retry_session(
    *,
    request: Request,
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> dict[str, bool | str]:
    """Recover runner/terminal readiness without creating transcript input."""
    conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conv is None:
        raise _session_not_found()

    original_runner_id = conv.runner_id
    runner_client = await _get_runner_client(session_id, runner_router)
    was_connected = runner_client is not None
    runner_relaunched = False
    if runner_client is None:
        await _raise_if_runner_on_another_replica(conv, request.app.state, conversation_store)
        runner_client, conv = await ensure_runner_connected(
            session_id=session_id,
            conv=conv,
            app_state=request.app.state,
            conversation_store=conversation_store,
            runner_router=runner_router,
            raise_host_refusal=True,
        )
        if runner_client is None:
            raise OmnigentError(
                "No runner is available to recover this session.",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        runner_relaunched = conv.runner_id != original_runner_id
    await _ensure_runner_session_initialized(
        session_id,
        conv,
        runner_client,
        conversation_store,
        initializer=getattr(request.app.state, "runner_session_initializer", None),
        suppress_recovery_turn=was_connected,
        require_success=True,
    )

    if runner_relaunched:
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        return {"queued": False, "recovered": True, "recovery": "runner_relaunched"}

    return {"queued": False, "recovered": False, "recovery": "already_connected"}


async def _retry_session_single_flight(
    *,
    request: Request,
    session_id: str,
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None,
) -> dict[str, bool | str]:
    """Share one in-flight recovery attempt across concurrent callers."""
    lock = _retry_recovery_lock(session_id)
    async with lock:
        task = _retry_recovery_tasks.get(session_id)
        if task is None:
            task = asyncio.create_task(
                _recover_retry_session(
                    request=request,
                    session_id=session_id,
                    conversation_store=conversation_store,
                    runner_router=runner_router,
                )
            )
            _retry_recovery_tasks[session_id] = task
            task.add_done_callback(
                lambda completed_task: _evict_retry_recovery_task(session_id, completed_task)
            )
    return await asyncio.shield(task)


@dataclass(frozen=True)
class _RunnerEventContext:
    """Authenticated tunnel identity for runner-originated external events."""

    app: Any
    headers: Headers
    user_id: str | None
    runner_id: str


_RUNNER_EVENT_TYPES = frozenset(
    {
        _EXTERNAL_CONVERSATION_ITEM_TYPE,
        _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE,
        _EXTERNAL_SESSION_STATUS_TYPE,
        _EXTERNAL_SESSION_USAGE_TYPE,
    }
)


class HelperSelection(BaseModel):
    """Which Helpers a pause or resume acts on; none named means all of them."""

    helper_ids: list[str] | None = None


def register_events_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    runner_exit_reports: RunnerExitReports | None = None,
    host_registry: HostRegistry | None = None,
    background_title_coordinator: BackgroundSessionTitleCoordinator | None = None,
    runner_tunnel_tokens: frozenset[str] | None = None,
    register_runner_ingest: Callable[[Callable[..., Awaitable[EventAckFrame]]], None]
    | None = None,
) -> None:
    """Register the events, stream, and delete routes on router."""

    event_router = APIRouter(route_class=_SessionEventBodyLimitRoute)

    def _has_runner_created_by_authority(
        request: Request | _RunnerEventContext, conv: Any
    ) -> bool:
        if isinstance(request, _RunnerEventContext):
            return conv.runner_id == request.runner_id
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if not token:
            return False
        if runner_tunnel_tokens is not None and token in runner_tunnel_tokens:
            return True
        runner_id = getattr(conv, "runner_id", None)
        return isinstance(runner_id, str) and token_bound_runner_id(token) == runner_id

    async def _require_helper_writer(
        request: Request | _RunnerEventContext, user_id: str | None, conv: Any
    ) -> None:
        """Refuse a message to a Super Chat Helper unless the engine itself sends it.

        A Helper is briefed and woken only by its parent chat's runner (or the server's
        own scheduled fires, which do not come through this route).
        """
        if conv.kind != "sub_agent" or _has_runner_created_by_authority(request, conv):
            return
        if user_id == RESERVED_USER_LOCAL and local_single_user_enabled():
            # A loopback runner without a binding token; the only user is the local one.
            return
        token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
        if token and conv.parent_conversation_id:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, conv.parent_conversation_id
            )
            if parent is not None and parent.runner_id == token_bound_runner_id(token):
                return
        if await asyncio.to_thread(is_super_chat_helper, conversation_store, conv):
            raise OmnigentError(
                "Helpers are read-only: message the chat that started it instead",
                code=ErrorCode.HELPER_READ_ONLY,
            )

    async def _authorized_conversation(
        request: Request | _RunnerEventContext, session_id: str
    ) -> tuple[str | None, Any]:
        """Require EDIT on the session and return the caller and its conversation."""
        user_id = (
            request.user_id
            if isinstance(request, _RunnerEventContext)
            else _get_user_id(request, auth_provider)
        )
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        if isinstance(request, _RunnerEventContext) and conv.runner_id != request.runner_id:
            raise OmnigentError("session is not bound to this runner", code=ErrorCode.FORBIDDEN)
        return user_id, conv

    @event_router.post(
        "/sessions/{session_id}/events",
        # Internal event ingestion — hidden from the public API reference.
        include_in_schema=False,
        status_code=202,
        # response_model=None: the body is a small acknowledgement
        # dict, not a domain model.
        response_model=None,
    )
    async def post_event(
        request: Request,
        session_id: str,
        body: SessionEventInput | list[SessionEventInput],
    ) -> dict[str, bool | str] | list[dict[str, bool | str]]:
        """
        Route entry for :func:`_post_event_impl`.

        A single object preserves the existing API. A top-level JSON array is
        processed in order and returns one acknowledgement per event when all
        entries succeed. Batch execution is not atomic: if an entry fails,
        earlier entries remain applied, later entries are not attempted, and
        the error response does not include acknowledgements from earlier
        entries. Messages count as in flight for the whole request, including
        runner launch.
        """
        with contextlib.ExitStack() as in_flight:
            if isinstance(body, list):
                if not body:
                    raise OmnigentError(
                        "session event batch must not be empty",
                        code=ErrorCode.INVALID_INPUT,
                    )
                if len(body) > MAX_SESSION_EVENT_BATCH_EVENTS:
                    raise OmnigentError(
                        "session event batch exceeds the 100-event limit",
                        code=ErrorCode.INVALID_INPUT,
                    )
                # A sub-agent transcript arrives as one array of up to 100 items with a
                # 10 s client timeout, and persisting it per entry on a store with
                # per-append overhead outlasts that. Each run of batchable entries is
                # authorized once and appended in one store call; every other entry
                # keeps the per-entry path, in order.
                acks: list[dict[str, bool | str]] = []
                for batchable, run in itertools.groupby(body, key=_is_batchable_external_item):
                    if not batchable:
                        for event in run:
                            acks.append(
                                await _post_event_impl(
                                    request,
                                    session_id,
                                    event,
                                    in_flight=in_flight if event.type == "message" else None,
                                )
                            )
                        continue
                    await _authorized_conversation(request, session_id)
                    add_audit_attrs(event_type=_EXTERNAL_CONVERSATION_ITEM_TYPE)
                    item_ids = await _persist_external_conversation_items(
                        session_id, list(run), conversation_store
                    )
                    acks.extend({"queued": False, "item_id": item_id} for item_id in item_ids)
                return acks
            return await _post_event_impl(
                request,
                session_id,
                body,
                in_flight=in_flight if body.type == "message" else None,
            )

    router.include_router(event_router)

    async def ingest_runner_batch(
        *,
        app: Any,
        headers: Headers,
        owner: str | None,
        runner_id: str,
        batch: EventBatchFrame,
    ) -> EventAckFrame:
        """Apply bounded, authenticated runner events through the HTTP event logic."""
        if len(batch.events) > 32 or len(encode_frame(batch).encode("utf-8")) > 256 * 1024:
            return EventAckFrame(batch.id, 0, "event batch exceeds count or size limit")
        request = _RunnerEventContext(app, headers, owner, runner_id)
        for index, payload in enumerate(batch.events):
            event_type = payload.get("type")
            if event_type not in _RUNNER_EVENT_TYPES:
                return EventAckFrame(batch.id, index, "event type is not allowed on runner tunnel")
            if event_type == _EXTERNAL_CONVERSATION_ITEM_TYPE:
                data = payload.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("source_id"), str):
                    return EventAckFrame(batch.id, index, "source_id required for tunnel replay")
            try:
                body = SessionEventInput.model_validate(payload)
                await _post_event_impl(request, batch.session_id, body)
            except OmnigentError as exc:
                return EventAckFrame(
                    batch.id,
                    index,
                    exc.code,
                    retryable=exc.http_status in {408, 429} or exc.http_status >= 500,
                )
            except ValueError:
                return EventAckFrame(batch.id, index, "invalid session event")
            except Exception:
                _logger.exception("Runner event ingestion failed for session %s", batch.session_id)
                return EventAckFrame(batch.id, index, "ingest failed", retryable=True)
        return EventAckFrame(batch.id, len(batch.events))

    if register_runner_ingest is not None:
        register_runner_ingest(ingest_runner_batch)

    async def _interrupt_session(
        session_id: str, round_id: str | None = None, note: str | None = None
    ) -> bool:
        """
        Stop *session_id*'s turn and, the same way, its running Helpers (ADR 0026 "Stop stops").

        The runner drops the messages waiting behind the turn. Without a runner, a message
        acknowledged early (the machine still waking) never dispatches: Stop holds through a
        relaunch.

        :param session_id: Session/conversation identifier.
        :param round_id: The Round stopped: only its Helpers (and Helpers in no Round) are
            stopped, other Rounds' keep working (ADR 0029). ``None`` stops every Helper.
        :param note: The Stop line; the runner writes it in the session's history.
        :returns: Whether the stop landed: the runner took it, or a waiting dispatch was
            cancelled.
        """
        _publish_interrupted(session_id)
        # Fence the cancelled turn (see _interrupt_fenced_sessions).
        _interrupt_fenced_sessions.add(session_id)
        cancelled_items = cancel_deferred_message_dispatch(session_id)
        runner_client = await _get_runner_client(session_id, runner_router)
        forwarded = False
        if runner_client is not None:
            try:
                interrupt_resp = await runner_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={"type": "interrupt", **({"data": {"note": note}} if note else {})},
                    timeout=5.0,
                )
                forwarded = interrupt_resp.status_code < 400
            except (httpx.HTTPError, ConnectionError):
                # WSTunnelTransport raises bare ConnectionError on tunnel close.
                _logger.exception("Interrupt forward failed for %r", session_id)
        if not forwarded:
            # The turn keeps running (or none runs) and nothing else lifts the
            # fence — remove it so the turn's remaining output isn't dropped.
            _interrupt_fenced_sessions.discard(session_id)
        for item_id in cancelled_items:
            # The stored message will not run: the transcript reads it again.
            session_stream.publish(
                session_id, {"type": "session.input.delivery", "data": {"item_id": item_id}}
            )
        if cancelled_items and not forwarded:
            _publish_status(session_id, "idle")
        try:
            await _interrupt_running_helpers(session_id, round_id)
        except Exception:
            _logger.exception("Interrupt cascade to helpers failed for %r", session_id)
        return forwarded or bool(cancelled_items)

    async def _helpers_of(session_id: str) -> tuple[list[Any], set[str]]:
        """*session_id*'s Helpers (never a file's studio) and the ids of the live ones, read as
        ``count_live_children`` does."""
        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=100,
            kind="sub_agent",
            parent_conversation_id=session_id,
            order="desc",
            sort_by="created_at",
        )
        children = [child for child in page.data if not is_artifact_chat(child.labels)]
        summaries = await _child_session_summaries_from_conversations(
            children, session_id, conversation_store
        )
        live = {
            summary.id
            for summary in summaries
            if is_live_child_row(summary.model_dump(mode="json"))
        }
        return children, live

    async def _interrupt_running_helpers(session_id: str, round_id: str | None = None) -> None:
        """Interrupt the running Helpers of *session_id* (read as ``count_live_children`` does)
        that are in *round_id*, or in no Round; every one when *round_id* is ``None``."""
        children, live_ids = await _helpers_of(session_id)
        live = [
            child.id
            for child in children
            if child.id in live_ids
            and (round_id is None or (child.labels or {}).get(ROUND_LABEL_KEY) in (None, round_id))
        ]
        results = await asyncio.gather(
            *(_interrupt_session(child_id, round_id) for child_id in live)
        )
        for child_id, delivered in zip(live, results, strict=True):
            _logger.info("interrupt helper=%s of=%s delivered=%s", child_id, session_id, delivered)

    async def _hold_helpers(
        request: Request, session_id: str, helper_ids: list[str] | None, *, paused: bool
    ) -> list[str]:
        """Pause or resume a Conversation's Helpers (ADR 0027); the Conversation itself never.

        Pause takes the live Helpers, resume the paused ones; *helper_ids* narrows either (an id
        that is not such a Helper of this Conversation is ignored). Each gets the
        ``superchat.paused`` label and its runner is told; the runner holds a paused Helper at
        its next tool boundary and stops one left paused for 30 minutes.

        :returns: The Helpers paused or resumed.
        """
        _user_id, conv = await _authorized_conversation(request, session_id)
        if conv.kind == "sub_agent":
            raise OmnigentError(
                "Pause and resume act on a conversation's helpers", code=ErrorCode.INVALID_INPUT
            )
        children, live = await _helpers_of(session_id)
        if paused:
            candidates = [c.id for c in children if c.id in live]
        else:
            candidates = [c.id for c in children if (c.labels or {}).get(PAUSED_LABEL_KEY)]
        wanted = set(helper_ids) if helper_ids is not None else None
        targets = [cid for cid in candidates if wanted is None or cid in wanted]
        value = str(int(time.time())) if paused else ""
        event = {"type": "pause" if paused else "resume"}

        async def _apply(child_id: str) -> bool:
            # A pause is labelled only once its runner holds it (a restarted runner restores
            # it from the label); a resume clears the label even with no runner to tell.
            acked = False
            runner_client = await _get_runner_client(child_id, runner_router)
            if runner_client is not None:
                try:
                    resp = await runner_client.post(
                        f"/v1/sessions/{child_id}/events", json=event, timeout=5.0
                    )
                    acked = resp.status_code < 400
                except (httpx.HTTPError, ConnectionError):
                    _logger.warning(
                        "%s forward failed for %r", event["type"], child_id, exc_info=True
                    )
            if not acked and paused:
                return False
            await asyncio.to_thread(
                conversation_store.set_labels, child_id, {PAUSED_LABEL_KEY: value}
            )
            return True

        done = await asyncio.gather(*(_apply(child_id) for child_id in targets))
        targets = [child_id for child_id, ok in zip(targets, done, strict=True) if ok]
        _logger.info("%s helpers=%s of=%s", event["type"], targets, session_id)
        return targets

    @router.post("/sessions/{session_id}/helpers/pause")
    async def pause_helpers(
        request: Request, session_id: str, body: HelperSelection | None = None
    ) -> dict[str, list[str]]:
        """Hold the Conversation's live Helpers at their next tool boundary (ADR 0027)."""
        ids = body.helper_ids if body is not None else None
        return {"paused": await _hold_helpers(request, session_id, ids, paused=True)}

    @router.post("/sessions/{session_id}/helpers/resume")
    async def resume_helpers(
        request: Request, session_id: str, body: HelperSelection | None = None
    ) -> dict[str, list[str]]:
        """Let the Conversation's paused Helpers go on (ADR 0027)."""
        ids = body.helper_ids if body is not None else None
        return {"resumed": await _hold_helpers(request, session_id, ids, paused=False)}

    async def _post_event_impl(
        request: Request | _RunnerEventContext,
        session_id: str,
        body: SessionEventInput,
        in_flight: contextlib.ExitStack | None = None,
        persisted_item: ConversationItem | None = None,
    ) -> dict[str, bool | str]:
        """
        Submit a session event (input message, tool output,
        approval, or interrupt).

        Dispatches on ``body.type``:

        - ``"interrupt"`` cancels any active task and publishes a
          ``session.interrupted`` event. Bypasses item persistence.
        - ``"approval"`` resolves an outstanding elicitation
          in-band (see :func:`_dispatch_approval`).
        - ``"external_assistant_message"`` appends and streams an
          assistant message observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_conversation_item"`` appends and streams a
          completed item observed outside the Omnigent task runtime,
          without starting or steering a task.
        - ``"external_output_text_delta"`` publishes a transient
          ``response.output_text.delta`` event observed outside the
          Omnigent task runtime, without persisting an item or starting /
          steering a task.
        - ``"external_tool_output_delta"`` publishes transient output for
          an in-progress function call without persisting an item.
        - ``"external_output_reasoning_delta"`` publishes a transient
          ``response.reasoning_text.delta`` event (preceded by one
          ``response.reasoning.started`` when ``data.started`` is true)
          observed outside the Omnigent task runtime, without persisting an
          item or starting / steering a task.
        - ``"external_session_interrupted"`` publishes a
          ``session.interrupted`` event observed outside the Omnigent task
          runtime, without persisting an item or starting / steering a
          task.
        - ``"external_elicitation_resolved"`` marks a native
          harness-originated elicitation as resolved elsewhere so
          subscribed clients clear the pending approval card.
        - ``"external_session_status"`` publishes a terminal-observed
          ``session.status`` edge without persisting an item or
          starting/steering a task.
        - ``"subagent.status"`` with ``data.idle=true`` publishes idle status
          for transcript inactivity without forwarding a completion
          to the runner.
        - ``"external_model_change"`` persists a terminal-observed
          model switch to ``model_override`` and publishes a
          ``session.model`` SSE event so the web picker reflects it.
        - ``"external_reasoning_effort_change"`` persists a terminal-observed
          thinking-level switch to ``reasoning_effort`` and publishes a
          ``session.reasoning_effort`` SSE event so the web picker reflects it.
        - ``"stop_session"`` terminates the live session without
          deleting the conversation (owner-only). Forwarded
          harness-agnostically to the runner, which hard-kills the
          external process for harnesses that have one (claude-native
          kills its tmux pane) and 204s otherwise. Stop is non-sticky:
          it writes no persistent marker, so the next message
          auto-relaunches the session on its (still-online) host via
          the normal message-dispatch relaunch path.
        - ``"retry_session"`` reconnects or relaunches the existing
          session runner without persisting or replaying user input.
        - ``"message"`` on an ``omnigent claude`` terminal session
          is forwarded to the bound runner for tmux injection only;
          the accepted prompt is persisted later when Claude records
          it in the terminal transcript.
        - Any other (item-typed) event is persisted into
          ``conversation_items`` via the legacy create-or-steer path
          (legacy persist path): if an active
          task is present, the item is delivered into its inbox;
          otherwise a new task is created and started. In both
          cases ``session.input.consumed`` fires with the persisted
          item's id.

        :param session_id: Session/conversation identifier.
        :param body: The validated :class:`SessionEventInput`.
        :param in_flight: When given, the session is marked as having a
            dispatch in flight once the caller is authorized, for the rest
            of the request.
        :param persisted_item: Set only by the early-ack path below: the user
            message it already stored, whose runner wake and forward this
            call now completes.
        :returns: ``{"queued": True, "item_id": "..."}`` for
            item-typed events, where ``item_id`` is the persisted
            conversation item id also emitted by
            ``session.input.consumed``; ``{"queued": False}`` for
            control and internal transient events.
        :raises OmnigentError: 404 if no session exists.
        """
        # Engine-only context (``proactive``) is set by server-side dispatch, never from here.
        _drop_engine_only_context(body)
        user_id, conv = await _authorized_conversation(request, session_id)
        if body.type in ("message", _SLASH_COMMAND_TYPE):
            await refuse_if_suspended(user_id)
        if body.type == "message":
            await _require_helper_writer(request, user_id, conv)
            if body.data.get("role", "user") == "user":
                mark_accepted(session_id)
                if "content" in body.data:
                    body.data["content"] = _plain_spaces(body.data["content"])
        if in_flight is not None:
            # Marked only after authorization, so an unauthorized caller
            # cannot flip a session to "running" even transiently.
            in_flight.enter_context(_mark_dispatch_in_flight(session_id))
        created_by = _attribution_user(user_id)
        body_created_by = _attribution_user(body.created_by)
        if body_created_by is not None:
            if not _has_runner_created_by_authority(request, conv):
                raise OmnigentError(
                    "created_by is reserved for runner-originated session events",
                    code=ErrorCode.FORBIDDEN,
                )
            try:
                await _require_access_and_level(
                    body_created_by,
                    session_id,
                    LEVEL_EDIT,
                    permission_store,
                    conversation_store,
                )
            except OmnigentError:
                pass
            else:
                created_by = body_created_by
        # Validate event type at the route boundary. Anything not in
        # ``_ALLOWED_EVENT_TYPES`` is a client mistake — failing here
        # is far better than silently persisting an item the agent
        # loop will only crash on later when ``parse_item_data`` runs
        # against the payload (rule 15 — fail loud).
        if body.type not in _ALLOWED_EVENT_TYPES:
            raise OmnigentError(
                f"Unknown event type: {body.type!r}. "
                f"Allowed types: {sorted(_ALLOWED_EVENT_TYPES)}",
                code=ErrorCode.INVALID_INPUT,
            )
        # Tag the audit envelope end-event with the event kind so a message,
        # interrupt, approval, stop, … are distinguishable in the audit table.
        # Transient per-chunk echoes suppress the row entirely (they are the
        # firehose; the content is already on the SSE-event logger).
        add_audit_attrs(event_type=body.type)
        if body.type in _TRANSIENT_AUDIT_EVENT_TYPES:
            mark_request_audit_suppressed()
        # For item types, validate the data payload shape against
        # the item-type's discriminator class. The control types
        # (interrupt, approval) bypass the item-persist path and have
        # their own payload schemas — they skip this check (interrupt
        # has no payload; approval's MCP-shape payload is validated
        # inside ``_dispatch_approval``).
        if body.type not in (
            _INTERRUPT_TYPE,
            _APPROVAL_TYPE,
            _MCP_ELICITATION_TYPE,
            _COMPACT_TYPE,
            _SLASH_COMMAND_TYPE,
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
        ):
            try:
                parse_item_data(body.type, {"type": body.type, **body.data})
            except (ValueError, TypeError) as exc:
                raise OmnigentError(
                    f"Invalid data payload for event type {body.type!r}: {exc}",
                    code=ErrorCode.INVALID_INPUT,
                ) from exc
        if body.type in ("message", _SLASH_COMMAND_TYPE):
            from omnigent.harnesses.native_attachments import (
                inline_filesystem_attachment_name,
                requires_filesystem,
            )

            content = body.data.get("content")
            inline_name = inline_filesystem_attachment_name(content)
            if inline_name is not None:
                raise OmnigentError(
                    f"Attachment {inline_name!r} must be uploaded to the session's "
                    "files and referenced by file_id.",
                    code=ErrorCode.INVALID_INPUT,
                )
            # Unsent uploads survive a switch or fork without appearing in history.
            if file_store is not None and isinstance(content, list):
                for block in content:
                    file_id = block.get("file_id") if isinstance(block, dict) else None
                    if not isinstance(file_id, str):
                        continue
                    stored = await asyncio.to_thread(file_store.get, file_id)
                    if stored is None or stored.session_id not in (None, session_id):
                        continue
                    if stored.filename is not None and requires_filesystem(stored.filename):
                        await _require_filesystem_attachment_harness(stored.filename)
                        break
        # Fail fast on malformed tools at the boundary. The raw dicts
        # (not the parsed objects) are what the runner stores — the
        # parse call is purely a validator.
        if body.tools:
            try:
                parse_client_side_tool_specs(body.tools)
            except ValueError as exc:
                raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if body.type == _RETRY_SESSION_TYPE:
            return await _retry_session_single_flight(
                request=cast("Request", request),
                session_id=session_id,
                conversation_store=conversation_store,
                runner_router=runner_router,
            )
        # ── Policy evaluation (path-agnostic) ────────────────
        # Evaluate policies BEFORE persistence/runner forwarding so
        # enforcement fires on both paths. On DENY, persist the
        # event (possibly with modified body) through whichever
        # path is active, then return the deny verdict. On ALLOW,
        # fall through to the normal persist/forward path.
        _policy_body = body  # may be replaced by OUTPUT deny
        _actor = _build_actor(user_id)
        if (
            body.type == "message" and body.data.get("role", "user") == "user"
        ) or body.type == _SLASH_COMMAND_TYPE:
            from omnigent.server.routes.sandbox_inference import validate_saved_selection

            await validate_saved_selection(cast("Request", request), conv, conv.model_override)

        # A closed sub-agent session (sys_session_close) rejects new user
        # input — the orchestrator must spawn a fresh session to continue.
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and is_session_closed(conv.labels, conv.title)
        ):
            raise OmnigentError(
                "Session is closed. Start a new sub-agent session to continue.",
                code=ErrorCode.CONFLICT,
            )
        if (
            body.type == "message"
            and persisted_item is None
            and await rounds.stopped_wake(conversation_store, conv, body.data, body.context)
        ):
            # Stop ended this Helper's Round, and its pending wakes with it (ADR 0029).
            _logger.info("wake of a stopped Round dropped session=%s", session_id)
            return {"queued": False, "delivered": False}
        if (
            body.type == "message"
            and body.data.get("role") == "user"
            and conv.agent_id is not None
        ):
            try:
                _input_verdict = await _evaluate_input_policy(
                    cast("Request", request),
                    session_id,
                    conv,
                    body,
                    conversation_store,
                    agent_store,
                    runner_router,
                    actor=_actor,
                    file_store=file_store,
                    artifact_store=artifact_store,
                )
            except Exception as _policy_exc:
                # Policy evaluation crashed (e.g. factory misconfigured).
                # Log and treat as DENY so the session doesn't hang on
                # "working" forever. The full cause is logged for admins;
                # the denial reason returned to (and streamed at) the client
                # stays generic so the raw exception text isn't exposed.
                _logger.warning(
                    "Input policy evaluation failed for %s: %s",
                    session_id,
                    _policy_exc,
                    exc_info=True,
                )
                _input_verdict = {
                    "verdict": "deny",
                    "reason": "Denied by policy (policy evaluation error).",
                }
            if _input_verdict is not None:
                # DENY or ASK — don't forward to runner. Publish a
                # deny sentinel on the session stream so the
                # client/REPL sees feedback.
                reason = _input_verdict.get("reason", "Denied by policy")
                # Surface the input-policy block on this post_event's audit row
                # so a "my message was blocked" case is queryable with its reason.
                add_audit_attrs(
                    policy_verdict=_input_verdict.get("verdict", "deny"),
                    policy_phase="input",
                    policy_reason=reason,
                )
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal ``response.completed`` renders the sentinel; the
                # trailing ``idle`` ends the turn. A request-phase deny/refuse
                # IS a turn boundary — the client optimistically went "working"
                # on send — but the message never reached a harness, so nothing
                # downstream (harness / interrupt / status file) emits the
                # turn-end. This ``idle`` is that signal; clients that settle a
                # turn only on a ``session.status`` edge (the REPL, the headless
                # ``-p`` client) hang without it. No leading ``running``: the
                # turn never dispatched, and a phantom ``running`` would fold a
                # concurrent live bubble mid-stream.
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                # Return the same shape the client expects from POST
                # /events so postEvent doesn't throw on an unexpected
                # response body. queued=False signals the event was
                # handled synchronously (denied, not queued for a turn).
                return {"queued": False, "denied": True, "reason": reason}
        elif body.type == _SLASH_COMMAND_TYPE and conv.agent_id is not None:
            _input_verdict = await _evaluate_input_policy(
                cast("Request", request),
                session_id,
                conv,
                _build_skill_slash_command_policy_body(body),
                conversation_store,
                agent_store,
                runner_router,
                file_store=file_store,
                artifact_store=artifact_store,
            )
            if _input_verdict is not None:
                reason = _input_verdict.get("reason", "Denied by policy")
                _publish_policy_deny(session_id, reason)
                await _persist_policy_deny_sentinel(
                    session_id,
                    conv,
                    reason,
                    conversation_store,
                    agent_store,
                )
                # Terminal response.completed + trailing ``idle`` turn-end (see
                # the message branch above for why the idle is required and the
                # leading running is not).
                _publish_input_deny_terminal(session_id, conv, reason)
                _publish_status(session_id, "idle")
                return {"queued": False, "denied": True, "reason": reason}
        elif (
            body.type == "message"
            and body.data.get("role") == "assistant"
            and conv.agent_id is not None
        ):
            _output_verdict = await _evaluate_output_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _output_verdict is not None:
                if _output_verdict.get("_denied_body") is not None:
                    _policy_body = _output_verdict["_denied_body"]
                    body = _policy_body
                # For OUTPUT DENY, fall through to persist the
                # denied body (with sentinel text). The verdict
                # is returned after persistence below.
                if _output_verdict["verdict"] == "deny":
                    pass  # fall through with modified body
                else:
                    return _output_verdict
        elif body.type == "function_call" and body.data.get("evaluate_policy"):
            _tool_verdict = await _evaluate_tool_call_policy(
                session_id,
                conv,
                body,
                conversation_store,
                agent_store,
                runner_router,
                actor=_actor,
            )
            if _tool_verdict is not None:
                return _tool_verdict
            # ALLOW — return explicit verdict so the request does
            # not fall through to the persist-and-forward path.
            # Policy evaluation requests are queries, not items to
            # persist or relay to the harness (which rejects
            # ``function_call`` as an unknown inbound event type).
            return {"verdict": "allow"}

        async def _wake_bound_runner_for_control(
            wake_conv: Any,
        ) -> tuple[Any, httpx.AsyncClient | None]:
            """
            Relaunch a disconnected-but-wakeable runner for a control event.

            A native-terminal /compact must run in the vendor TUI, so when no
            runner is bound we wake it the same way the message-dispatch path
            does (resume a managed sandbox / relaunch the runner on its still-
            online host), wait for the tunnel to connect, then re-run the
            session-init handshake and relay so the forwarded control reaches
            a live harness. Returns the (possibly re-read) conversation row and
            the resolved runner client, or ``(conv, None)`` when the session
            can't be woken (e.g. the host is offline).

            :param wake_conv: The session row (may have a stale binding).
            :returns: ``(conversation, runner_client_or_None)``.
            :raises OmnigentError: ``WRONG_REPLICA`` when the host tunnel is
                absent here but the host is live on another replica.
            """
            _app_state = request.app.state
            if wake_conv.host_id is not None and await _maybe_wake_stale_resumable_managed_sandbox(
                session_id=session_id,
                conv=wake_conv,
                app_state=_app_state,
                conversation_store=conversation_store,
            ):
                _refreshed = await asyncio.to_thread(
                    conversation_store.get_conversation, session_id
                )
                if _refreshed is not None:
                    wake_conv = _refreshed
            _client = await _get_runner_client(session_id, runner_router)
            if _client is None and wake_conv.host_id is not None:
                _host_reg = getattr(_app_state, "host_registry", None)
                _tunnel_reg = getattr(_app_state, "tunnel_registry", None)
                # Wrong-replica routing miss, checked before we try to launch:
                # the runner tunnel registers on the same replica as its host,
                # so an absent tunnel here while the host is live elsewhere
                # means the key routed to the wrong replica. Signal it (same as
                # the message path) so the client re-addresses without the key,
                # rather than getting a false "reconnect" 503 for a session that
                # is actually wakeable on another replica.
                _host_store = getattr(_app_state, "host_store", None)
                if (
                    multi_replica()
                    and _host_reg is not None
                    and _host_store is not None
                    and _host_reg.get(wake_conv.host_id) is None
                ):
                    _elsewhere = await asyncio.to_thread(_host_store.get_host, wake_conv.host_id)
                    if _elsewhere is not None and host_is_live(_elsewhere):
                        raise OmnigentError(
                            "session runner is on another replica; retry",
                            code=ErrorCode.WRONG_REPLICA,
                        )
                _relaunched_runner_id: str | None = None
                _host_conn = _host_reg.get(wake_conv.host_id) if _host_reg is not None else None
                if _host_conn is not None:
                    _attempt = await _launch_runner_on_host(
                        wake_conv,
                        conversation_store,
                        _host_reg,
                        _host_conn,
                    )
                    # A structured refusal (harness not configured / workspace
                    # missing) leaves no runner to compact against — treat as
                    # unwakeable; the caller surfaces the reconnect error.
                    if _attempt.error_code is None:
                        _relaunched_runner_id = _attempt.runner_id
                elif await _maybe_relaunch_managed_sandbox(
                    session_id=session_id,
                    conv=wake_conv,
                    app_state=_app_state,
                    conversation_store=conversation_store,
                ):
                    _refreshed = await asyncio.to_thread(
                        conversation_store.get_conversation, session_id
                    )
                    if _refreshed is not None:
                        wake_conv = _refreshed
                    _client = await _get_runner_client(session_id, runner_router)
                if _client is None:
                    _client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_reg,
                        runner_id=_relaunched_runner_id,
                        timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                        runner_exit_reports=runner_exit_reports,
                    )
            if _client is None:
                return wake_conv, None
            _refreshed = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if _refreshed is not None:
                wake_conv = _refreshed
            # Bring the terminal + transcript forwarder up before the control
            # is injected, mirroring the message-dispatch relaunch path.
            # suppress_recovery_turn=True: unlike the message path (which
            # suppresses because the server already persisted a pending message
            # the runner's history load would otherwise start a recovery turn
            # for), /compact persists nothing — so there is no pending item to
            # trigger recovery. Suppression is a harmless no-op here; kept for
            # parity so a wake never spuriously starts a turn.
            await _ensure_runner_session_initialized(
                session_id,
                wake_conv,
                _client,
                conversation_store,
                initializer=getattr(_app_state, "runner_session_initializer", None),
                suppress_recovery_turn=True,
            )
            await _ensure_runner_relay_ready(
                session_id,
                wake_conv.runner_id,
                _client,
                conversation_store,
            )
            return wake_conv, _client

        if body.type == _INTERRUPT_TYPE:
            # The person's Stop ends one Round (ADR 0029): its turn, its Helpers, its wakes.
            children, live_ids = await _helpers_of(session_id)
            stopped = await rounds.stop_round(conv, [c for c in children if c.id in live_ids])
            delivered = await _interrupt_session(
                session_id,
                stopped.round_id if stopped else None,
                stopped.line if stopped else None,
            )
            _logger.info("interrupt session=%s delivered=%s", session_id, delivered)
            return {"queued": False, "delivered": delivered}
        if body.type == _STOP_SESSION_TYPE:
            # Terminating the whole session (not just the current turn)
            # is a lifecycle action; require owner access on top of the
            # LEVEL_EDIT gate above so a shared editor can't kill the
            # owner's session.
            await _require_access(
                user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
            )
            # Fence the cancelled turn, same as interrupt.
            _interrupt_fenced_sessions.add(session_id)
            # Harness-agnostic forward: the runner kills the external
            # process for harnesses that have one (claude-native
            # hard-kills its tmux pane) and 204s otherwise. Unlike the
            # best-effort effort/model_change relay, a failed stop means
            # the session is still alive — so this helper RAISES on a
            # non-2xx / unreachable runner (503) rather than swallowing
            # it, letting the web UI show the stop didn't land instead
            # of closing the dialog as if it succeeded.
            try:
                stop_delivered = await _stop_session_via_runner(session_id, runner_router)
            except Exception:
                # Stop didn't land: the turn keeps running, so lift the
                # fence or its remaining output is dropped forever.
                _interrupt_fenced_sessions.discard(session_id)
                raise
            if not stop_delivered:
                # No runner resolved: nothing else lifts the fence (same as interrupt).
                _interrupt_fenced_sessions.discard(session_id)
            # Host-spawned sessions run on a dedicated runner the host
            # launched for this one session. Killing the pane (above) leaves
            # that runner connected, so GET /health keeps reporting
            # runner_online: true and the web UI never shows the session as
            # disconnected — new messages hang on "working" against a dead
            # pane. Stop the runner too so its tunnel drops and the web UI
            # shows the same "Agent disconnected — click to show reconnect
            # command" banner a CLI-launched session reaches on exit. Read
            # host_id / runner_id from the owner-gated session row so we can
            # only ever stop the runner bound to this session.
            stop_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if stop_conv is not None and stop_conv.host_id and stop_conv.runner_id:
                # Mark the tunnel drop as intentional BEFORE tearing it down so
                # the relay's disconnect handler renders a quiet stopped state
                # rather than "Error · runner_disconnected". Only host-spawned
                # sessions drop the tunnel on Stop; other harnesses leave the
                # runner connected, so there is nothing to suppress for them.
                _intentional_stop_sessions.add(session_id)
                teardown_delivered = await _stop_session_host_runner(
                    session_id,
                    stop_conv.host_id,
                    stop_conv.runner_id,
                    getattr(request.app.state, "host_registry", None),
                )
                if not teardown_delivered:
                    # Best-effort stop did not land (host offline / timeout /
                    # failure): no tunnel drop will follow, so the relay won't
                    # reach the disconnect handler that consumes the marker.
                    # Discard it now so it can't outlive this turn on the
                    # reused per-session relay task and later swallow a genuine
                    # runner_disconnected as a quiet idle.
                    _intentional_stop_sessions.discard(session_id)
            if not stop_delivered:
                # False-success backstop. The stop reached NO live runner
                # (``_stop_session_via_runner`` returned False, so there was
                # no tunnel to deliver to). That path treats "no runner bound"
                # as a no-op success — correct for an already-idle session,
                # but WRONG for an orphaned one whose persisted status is
                # still running/waiting: the runner died (a server replica
                # outlived it, a crashed host, a graceful disconnect
                # mid-turn), so nothing is left to emit the terminal edge, and
                # returning 2xx below would report a stop that never actually
                # settled the session. Reconcile that exact case to idle so
                # the success we return is honest. (A host-spawned session
                # whose runner is still live hits ``stop_delivered=True``
                # above and its tunnel-drop disconnect handler publishes the
                # terminal edge, so it never reaches here.)
                #
                # Only fire when the runner is confirmed gone from every
                # replica: no live tunnel here (that's WHY the stop couldn't
                # be delivered) AND ``runner_last_seen`` stale past the TTL,
                # so a runner merely mid-reconnect — or alive on another
                # replica — inside the grace window is left untouched.
                stop_connectivity = await asyncio.to_thread(
                    conversation_store.get_session_connectivity, [session_id]
                )
                stop_conn = stop_connectivity.get(session_id)
                if (
                    stop_conn is not None
                    and stop_conn.runner_id is not None
                    and not runner_seen_is_fresh(stop_conn.runner_last_seen)
                    and _session_status_from_cache(
                        session_id,
                        stop_conv.live_status if stop_conv is not None else None,
                    )
                    == "running"
                ):
                    await asyncio.to_thread(
                        reconcile_orphaned_running_status,
                        session_id,
                        conversation_store,
                        int(time.time()) - RUNNER_LIVENESS_TTL_S,
                    )
            # Stop is non-sticky: no persistent marker is written. The
            # runner tunnel dropping above flips ``runner_online`` to false
            # honestly, and the next message auto-relaunches the session on
            # its (still-online) host via the normal message-dispatch
            # relaunch path below.
            try:
                _srv_id = _get_installation_id()
                _anon = _tel_anon_user_id(user_id, _srv_id)
                _tel_emit(
                    _TelSessionStoppedEvent(
                        session_id=session_id,
                        installation_id=_srv_id,
                        anon_user_id=_anon,
                    )
                )
            except Exception:
                pass
            return {"queued": False}
        if body.type == _APPROVAL_TYPE:
            # Deliver the verdict through the shared resolver: it
            # sets any server-side harness Future (owner-checked),
            # clears the sidebar badge, and forwards
            # to the runner for runner-side (policy) elicitations.
            # The dedicated URL endpoint (``.../elicitations/{eid}/
            # resolve``) routes through the same helper.
            await _resolve_elicitation(session_id, body.data, runner_router, conversation_store)
            await _signal_elicitation_answered(session_id, body.data, user_id)
            # Apply any policy writes deferred by the relay tool-call ASK gate
            # (e.g. a cost-budget checkpoint) now that the verdict is in.
            await _apply_pending_policy_ask_writes(
                session_id, conv, conversation_store, agent_store, body.data
            )
            return {"queued": False}
        if body.type == _MCP_ELICITATION_TYPE:
            # The runner's inline MCP elicitation callback fires when
            # an external MCP server sends ``elicitation/create``
            # during a ``tools/call``. Publish the elicitation as an
            # SSE event (approval card in web UI, y/a/n prompt in
            # REPL) and return the elicitation_id immediately so the
            # runner can park on ``pending_approvals``. The user's
            # verdict arrives later via ``type: "approval"`` →
            # ``_resolve_elicitation`` → ``_forward_approval_to_runner``
            # → runner's ``pending_approvals`` resolves.
            elicit_data = body.data or {}
            elicit_id = f"elicit_{secrets.token_hex(16)}"
            elicit_params = ElicitationRequestParams(
                mode="form",
                message=elicit_data.get("message", ""),
                requestedSchema=elicit_data.get("requestedSchema"),
            )
            event = ElicitationRequestEvent(
                type="response.elicitation_request",
                elicitation_id=elicit_id,
                params=elicit_params,
            )
            _mcp_elicit_payload = event.model_dump()
            from omnigent.server.routes import sessions as _sessions_facade

            _sessions_facade.session_stream.publish(session_id, _mcp_elicit_payload)
            # Mirror the prompt into ancestor streams so a sub-agent MCP
            # elicitation surfaces in the parent (polly) chat with a
            # ``target_session_id`` pointing back at this child. The
            # verdict still arrives via the generic ``approval`` event,
            # which mirrors the resolved signal back up through
            # ``_resolve_elicitation``.
            await asyncio.to_thread(
                _publish_elicitation_request_to_ancestors,
                conversation_store,
                session_id,
                _mcp_elicit_payload,
            )
            # Bound the runner's wait by the session's approval window.
            from omnigent.server.routes._sessions.orchestration import (
                _load_agent_spec_for_session,
            )
            from omnigent.spec.types import DEFAULT_ASK_TIMEOUT

            spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
            guardrails = spec.guardrails if spec is not None else None
            ask_timeout = guardrails.ask_timeout if guardrails else DEFAULT_ASK_TIMEOUT
            return {"queued": False, "elicitation_id": elicit_id, "ask_timeout": ask_timeout}
        if body.type == _COMPACT_TYPE:
            # Unified control dispatch (designs/CLAUDE_NATIVE.md
            # "Control events dispatch on the runner"): forward /compact
            # to the bound runner first, regardless of harness. The
            # runner dispatches by harness — native harnesses inject
            # /compact into the vendor TUI and return 200 on success or
            # 5xx on failure. SDK harnesses return 204 (no-op) because
            # their context is controlled entirely by the vendor harness.
            # A 4xx/5xx from the runner is surfaced as an error.
            # TUI budget, not the 5s default: the claude-native handler
            # drives a delivery-verified slash-command inject, and a timeout
            # here would surface as an error mid-TUI-compact.
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": _COMPACT_TYPE},
                timeout_s=_TUI_INJECT_FORWARD_TIMEOUT_S,
            )
            if runner_result is not None and runner_result.status_code == 200:
                return {"queued": False}
            if runner_result is not None and runner_result.status_code != 204:
                raise OmnigentError(
                    f"Compaction failed: runner returned {runner_result.status_code}",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            raise OmnigentError(
                "/compact is not available for this session type.",
                code=ErrorCode.INVALID_INPUT,
            )
        if body.type == "compaction":
            import uuid as _uuid

            # Cross-process single-flight guard (two runner processes for the
            # same session both deciding to roll over): the caller names the
            # compaction item it summarized from. If the session's latest
            # compaction item has since moved, someone else already rolled
            # this session over on top of the one this summary is stale
            # against — reject rather than overwrite it with a summary that
            # silently drops whatever that other rollover captured. Absent
            # (older callers, or a harness's own native compaction) skips
            # the check entirely.
            # ``in`` (not ``.get() is not None``): the guard's own valid
            # value for "no prior compaction" IS ``None``, distinct from an
            # older caller that never sends this key at all.
            if "expected_previous_compaction_id" in body.data:
                expected_previous_compaction_id = body.data["expected_previous_compaction_id"]
                latest_page = await asyncio.to_thread(
                    conversation_store.list_items,
                    session_id,
                    limit=1,
                    order="desc",
                    type="compaction",
                )
                latest_id = latest_page.data[0].id if latest_page.data else None
                if latest_id != expected_previous_compaction_id:
                    raise OmnigentError(
                        f"session {session_id!r} already rolled over past "
                        f"{expected_previous_compaction_id!r}",
                        code=ErrorCode.CONFLICT,
                    )

            item = NewConversationItem(
                type="compaction",
                response_id=f"compact_{_uuid.uuid4().hex}",
                data=parse_item_data(
                    "compaction",
                    {k: v for k, v in body.data.items() if k != "expected_previous_compaction_id"},
                ),
            )
            await asyncio.to_thread(
                conversation_store.append,
                session_id,
                [item],
            )
            # Memory upkeep trigger (docs/context.md):
            # a superside-chat session's own compaction is the signal that
            # there is new, settled conversation to learn from. Native-CLI
            # rollover sessions are untouched — upkeep for them is out of
            # scope for this slice. The session's *owner* (never the
            # authenticated caller, which may be the runner/system on a
            # Sub-agent's own session) is whose memory gets updated — the
            # same resolution ``memory_*`` and the memory routes use.
            if is_superside_chat(conv.labels):
                coordinator = getattr(request.app.state, "memory_upkeep_coordinator", None)
                if coordinator is not None:
                    owner = await asyncio.to_thread(
                        conversation_store.get_session_owner, session_id
                    )
                    if owner:
                        coordinator.schedule(owner)
            return {"queued": True}
        if body.type == _EXTERNAL_ASSISTANT_MESSAGE_TYPE:
            item_id = await _persist_external_assistant_message(
                session_id,
                body,
                conversation_store,
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_CONVERSATION_ITEM_TYPE:
            item_id = await _persist_external_conversation_item(
                session_id,
                conv,
                body,
                conversation_store,
                created_by=created_by,
                background_title_coordinator=background_title_coordinator,
                enabled=background_session_titles_enabled(request.headers),
            )
            return {"queued": False, "item_id": item_id}
        if body.type == _EXTERNAL_OUTPUT_TEXT_DELTA_TYPE:
            _publish_external_output_text_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_TOOL_OUTPUT_DELTA_TYPE:
            _publish_external_tool_output_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_OUTPUT_REASONING_DELTA_TYPE:
            _publish_external_output_reasoning_delta(session_id, body)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_INTERRUPTED_TYPE:
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    "external_session_interrupted data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_interrupted(session_id, response_id=response_id)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_SUPERSEDED_TYPE:
            target_conversation_id = body.data.get("target_conversation_id")
            if not isinstance(target_conversation_id, str) or not target_conversation_id.strip():
                raise OmnigentError(
                    "external_session_superseded requires a non-empty string "
                    "data.target_conversation_id",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_session_superseded(session_id, target_conversation_id.strip())
            return {"queued": False}
        if body.type == _EXTERNAL_BTW_SIDECHAT_TYPE:
            question = body.data.get("question")
            answer = body.data.get("answer")
            if not isinstance(question, str) or not isinstance(answer, str) or not answer:
                raise OmnigentError(
                    "external_btw_sidechat requires string data.question and a "
                    "non-empty string data.answer",
                    code=ErrorCode.INVALID_INPUT,
                )
            _publish_btw_sidechat(
                session_id,
                question=question,
                answer=answer,
                truncated=bool(body.data.get("truncated")),
            )
            return {"queued": False}
        if body.type == _EXTERNAL_BTW_DISMISS_TYPE:
            # The reader closed the web /btw overlay; forward an Escape to the
            # pane so the terminal's own overlay closes in lockstep. Transient
            # and best-effort: nothing is persisted, and the pane overlay also
            # auto-dismisses on the next injected message, so a runner that is
            # unreachable (or a non-claude-native harness that no-ops) is fine.
            await _forward_session_change_to_runner(
                session_id,
                runner_router,
                {"type": "btw_dismiss"},
            )
            return {"queued": False}
        if body.type == _EXTERNAL_ELICITATION_RESOLVED_TYPE:
            elicitation_id = body.data.get("elicitation_id")
            if not isinstance(elicitation_id, str):
                raise OmnigentError(
                    "external_elicitation_resolved requires string data.elicitation_id.",
                    code=ErrorCode.INVALID_INPUT,
                )
            _signal_harness_elicitation_resolved_by_id(session_id, elicitation_id)
            return {"queued": False}
        if body.type in (_EXTERNAL_SESSION_STATUS_TYPE, _SUBAGENT_STATUS_TYPE):
            status = body.data.get("status")
            if body.type == _SUBAGENT_STATUS_TYPE:
                if body.data.get("idle") is not True:
                    raise OmnigentError(
                        "subagent.status requires data.idle to be true",
                        code=ErrorCode.INVALID_INPUT,
                    )
            elif not isinstance(status, str) or status not in _EXTERNAL_SESSION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_session_status requires data.status in "
                    f"{sorted(_EXTERNAL_SESSION_STATUS_VALUES)}; got {status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            response_id = body.data.get("response_id")
            if response_id is not None and not isinstance(response_id, str):
                raise OmnigentError(
                    f"{body.type} data.response_id must be a string",
                    code=ErrorCode.INVALID_INPUT,
                )
            # ``None`` (field absent) = no information; leave the sticky
            # tally untouched (the PTY-activity ``idle`` carries none). An
            # explicit ``0`` from a ``Stop`` hook is authoritative and clears
            # the tally, so a finished background shell drops the indicator.
            raw_bg_count = body.data.get("background_task_count")
            bg_count = (
                raw_bg_count
                if isinstance(raw_bg_count, int)
                and not isinstance(raw_bg_count, bool)
                and raw_bg_count >= 0
                else None
            )
            # Per-shell detail behind that count, so the UI can name each
            # running shell. Best-effort: an older runner sends the count with no
            # detail, and the server keeps it in lockstep with the tally (see
            # ``_publish_status``).
            bg_tasks = _parse_background_tasks(body.data.get("background_tasks"))
            # Why a still-running session is parked, e.g. a permission prompt
            # the web UI does not mirror. Absent or blank = not parked, so the
            # indicator falls back to its ordinary working label.
            raw_blocked_on = body.data.get("blocked_on")
            blocked_on = (
                raw_blocked_on if isinstance(raw_blocked_on, str) and raw_blocked_on else None
            )
            if body.type == _SUBAGENT_STATUS_TYPE:
                # A transcript lull publishes idle but is not a runner completion.
                _publish_status(
                    session_id,
                    "idle",
                    None,
                    response_id=response_id,
                    background_task_count=bg_count,
                    background_tasks=bg_tasks,
                    blocked_on=blocked_on,
                )
                return {"queued": False}
            assert isinstance(status, str)
            # A background-task ``waiting`` marks an ended turn, so deliver it
            # as ``idle``: the session takes a new message now, and for a
            # sub-agent the terminal-delivery branch below must fire (otherwise
            # the orchestrator hangs). The tally still drives the indicator.
            # The claude-native forwarder no longer sends ``waiting`` at all —
            # this normalizes it for runners that predate that change.
            effective_status = _background_task_delivery_status(status, bg_count)
            if effective_status != status:
                status = effective_status
                body.data["status"] = status
            # Terminal edges carry the harness's own persisted text: the
            # child's result on ``idle``, and on ``failed`` — when the
            # forwarder attached no detail — its error report (claude-native's
            # StopFailure edge posts none). Enriched before the reason is
            # derived so the pill, ``last_task_error``, and the parent inbox
            # all see the same detail.
            data = await _enrich_terminal_status_with_subagent_output(
                body.data, status, session_id, conversation_store
            )
            # Surface the failure reason a native forwarder carries so a
            # top-level session sees it on its own status edge and persisted
            # last_task_error, not only the sub-agent parent-inbox path.
            output = data.get("output")
            status_error: ErrorDetail | None = None
            if status == "failed" and isinstance(output, str) and output.strip():
                status_error = ErrorDetail(
                    code=classify_native_turn_error("native_turn_error", output),
                    message=output.strip(),
                )
            if status_error is not None:
                failed_agent_name = await asyncio.to_thread(
                    _response_agent_name_from_store, conversation_store, session_id, response_id
                )
                await _persist_session_status_error_labels(
                    session_id,
                    status_error,
                    conversation_store,
                    agent_name=failed_agent_name,
                )
            elif status == "running":
                await _persist_session_status_error_labels(session_id, None, conversation_store)
            _publish_status(
                session_id,
                status,
                status_error,
                failure_origin="external_session_status",
                response_id=response_id,
                background_task_count=bg_count,
                background_tasks=bg_tasks,
                blocked_on=blocked_on,
            )
            # Emit a turn-end telemetry event for native harnesses. "idle"
            # means the turn completed normally; "failed" means it errored.
            # No latency or token deltas are available on this path.
            if status in {"idle", "failed"}:
                _tel_emit(
                    _TelTurnEndEvent(
                        installation_id=_get_installation_id(),
                        session_id=session_id,
                        anon_user_id=_tel_anon_user_id(user_id, _get_installation_id()),
                        host_installation_id=_tel_parse_installation_id(
                            request.headers.get(_TEL_INSTALLATION_ID_HEADER)
                        ),
                        status="completed" if status == "idle" else "failed",
                        latency_ms=None,
                        model=None,
                        input_tokens=None,
                        output_tokens=None,
                        cost_usd=None,
                    )
                )
            forward_body = body.model_dump()
            forward_body["data"] = data
            runner_result = await _forward_session_change_to_runner(
                session_id,
                runner_router,
                forward_body,
            )
            if conv.kind == "sub_agent" and status in {"idle", "failed"}:
                if runner_result is None:
                    # The child's pinned runner_id is stale — its runner was
                    # relaunched under a new id and only the parent was
                    # rebound, so the child points at a dead runner forever and
                    # this terminal status would 503 indefinitely while the
                    # parent hangs waiting for the child's inbox result. Heal
                    # the binding and re-deliver through the parent's live
                    # runner before failing.
                    from omnigent.server.routes import sessions as _sf

                    recovered = await _sf._recover_subagent_status_forward_via_parent(
                        conv,
                        runner_router,
                        getattr(request.app.state, "tunnel_registry", None),
                        conversation_store,
                        forward_body,
                    )
                    if recovered is not None:
                        runner_result = recovered
                _require_external_status_forward(
                    session_id,
                    status,
                    runner_result,
                )
            return {"queued": False}
        if body.type == _EXTERNAL_COMPACTION_STATUS_TYPE:
            # Terminal-observed compaction edge (claude-native forwarder):
            # republish as the standard compaction SSE so the web UI
            # spinner brackets Claude's real terminal compaction. No token
            # count is available here — the context ring is updated
            # separately by external_session_usage — so completed carries
            # total_tokens=None.
            compaction_status = body.data.get("status")
            if compaction_status not in _EXTERNAL_COMPACTION_STATUS_VALUES:
                raise OmnigentError(
                    f"external_compaction_status requires data.status in "
                    f"{sorted(_EXTERNAL_COMPACTION_STATUS_VALUES)}; got {compaction_status!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            if compaction_status == "in_progress":
                _publish_compaction_in_progress(session_id)
            elif compaction_status == "completed":
                _publish_compaction_completed(session_id, None)
            else:
                _publish_compaction_failed(session_id)
            return {"queued": False}
        if body.type == _EXTERNAL_MCP_STARTUP_TYPE:
            # Harness MCP-server startup progress (codex-native forwarder):
            # republish as a ``session.mcp_startup`` SSE so the web UI shows
            # per-server startup state while the harness boots. Malformed
            # entries are rejected at the boundary — a bogus map would only
            # strand the UI's startup band.
            raw_servers = body.data.get("servers")
            if not isinstance(raw_servers, dict):
                raise OmnigentError(
                    "external_mcp_startup requires data.servers to be an object "
                    f"mapping server names to startup records; got {raw_servers!r}",
                    code=ErrorCode.INVALID_INPUT,
                )
            mcp_servers: dict[str, McpServerStartup] = {}
            for server_name, record in raw_servers.items():
                record_status = record.get("status") if isinstance(record, dict) else None
                if not (
                    isinstance(server_name, str)
                    and server_name
                    and isinstance(record_status, str)
                    and record_status in _EXTERNAL_MCP_STARTUP_STATUS_VALUES
                ):
                    raise OmnigentError(
                        "external_mcp_startup server records require a status in "
                        f"{sorted(_EXTERNAL_MCP_STARTUP_STATUS_VALUES)}; got "
                        f"{server_name!r}: {record!r}",
                        code=ErrorCode.INVALID_INPUT,
                    )
                record_error = record.get("error")
                mcp_servers[server_name] = McpServerStartup(
                    status=cast(
                        Literal["starting", "ready", "failed", "cancelled"],
                        record_status,
                    ),
                    error=record_error if isinstance(record_error, str) and record_error else None,
                )
            _publish_mcp_startup(session_id, mcp_servers)
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_USAGE_TYPE:
            # Persist the harness-reported cumulative usage so the
            # tool-call cost gate can read the running
            # ``total_cost_usd`` on the next tool call. (Cost budgets
            # now enforce at ``tool_call`` via the PreToolUse hook, not
            # post-hoc here — a logged output cannot be un-logged.)
            await _persist_external_session_usage(
                session_id,
                body,
                conversation_store,
                conv,
                user_id,
                _tel_parse_installation_id(request.headers.get(_TEL_INSTALLATION_ID_HEADER)),
            )
            return {"queued": False}
        if body.type == _EXTERNAL_MODEL_CHANGE_TYPE:
            await _persist_external_model_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_SESSION_TITLE_TYPE:
            await _persist_external_session_title(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == _EXTERNAL_REASONING_EFFORT_CHANGE_TYPE:
            await _persist_external_reasoning_effort_change(
                session_id,
                conv,
                body,
                conversation_store,
            )
            return {"queued": False}
        if body.type == "function_call_output":
            # A client-side tool's result tunneling back to a parked turn.
            # The harness scaffold resolves the parked tool Future on a
            # ``tool_result`` event (ToolResultEvent {call_id, output}), so
            # translate the session-API ``function_call_output`` into that
            # wire shape and forward to the bound runner, which relays it
            # verbatim to the parked harness. Mirrors the runner's own
            # dispatch_tool_locally tool_result post; the output here came
            # from the caller (a client-side tool) instead of a local
            # dispatch. ``parse_item_data`` above already validated the
            # payload against ``FunctionCallOutputData`` (call_id: str,
            # output: str), so both fields are present strings. Stale
            # call_ids no-op at the scaffold; the harness re-emits the
            # completed function_call + output on resume, so history is
            # written through the normal stream path (no separate persist).
            runner_client = await _get_runner_client(session_id, runner_router)
            if runner_client is None:
                raise OmnigentError(
                    "No runner bound to this session; cannot deliver the tool result.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            try:
                await runner_client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "tool_result",
                        "call_id": body.data["call_id"],
                        "output": body.data["output"],
                    },
                    timeout=10.0,
                )
            except (httpx.HTTPError, ConnectionError) as exc:
                # Fail loud (503), not best-effort: unlike the advisory
                # interrupt-forward, a dropped tool_result leaves the parked
                # turn hanging until it times out. Surfacing the failure lets
                # the caller retry the delivery (the scaffold no-ops if a
                # retry double-delivers a now-stale call_id).
                raise OmnigentError(
                    "Failed to deliver the tool result to the session runner.",
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                ) from exc
            return {"queued": True, "item_id": body.data["call_id"]}
        # Whether the runner was initially unavailable or was woken below. In
        # that case the session-init handshake may still be racing the first
        # message, even if we reused the original binding instead of launching
        # a replacement.
        _runner_needs_session_init = False
        # A runner behind a managed sandbox may first need minutes of wake or
        # relaunch. Store the message and acknowledge it now; the wake and the
        # forward finish in the background, still ahead of the turn, so the
        # turn sees exactly what it would have seen inline. A failed wake is
        # then the turn's failure, never a lost message.
        if (
            persisted_item is None
            and body.type == "message"
            and conv.kind != "sub_agent"
            and (
                session_has_deferred_dispatch(session_id)
                or await managed_wake_may_precede_dispatch(conv, request.app.state)
            )
        ):
            if body.if_running:
                # The runner is not up, so no turn is running to take it.
                raise OmnigentError(
                    "No turn is running to take this message.", code=ErrorCode.CONFLICT
                )
            stored = await _persist_user_event_item(
                session_id, conv, body, conversation_store, created_by=created_by
            )

            async def _wake_and_forward() -> None:
                await _post_event_impl(request, session_id, body, persisted_item=stored)

            async def _fail_turn(exc: Exception) -> None:
                failure = ErrorDetail(
                    code=exc.code if isinstance(exc, OmnigentError) else "dispatch_failed",
                    message=str(exc),
                )
                await _persist_session_status_error_labels(session_id, failure, conversation_store)
                await _record_turn_failure(
                    conversation_store,
                    session_id,
                    failure,
                    failure_origin="runner_unavailable",
                )

            schedule_deferred_message_dispatch(
                session_id, _wake_and_forward, _fail_turn, item_id=stored.id
            )
            return {"queued": True, "item_id": stored.id}
        # Item event (message, function_call_output, etc.).
        if conv.host_id is not None and await _maybe_wake_stale_resumable_managed_sandbox(
            session_id=session_id,
            conv=conv,
            app_state=request.app.state,
            conversation_store=conversation_store,
        ):
            # A resumable managed wake may have re-launched the runner and
            # updated liveness while this handler was holding an old row.
            conv_after_wake = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conv_after_wake is None:
                raise _session_not_found()
            conv = conv_after_wake
            _runner_needs_session_init = True
        runner_client = await _get_runner_client(session_id, runner_router)
        # Managed-launch rendezvous: a ``host_type="managed"`` create
        # returns before the sandbox exists, so the first message (the
        # Web UI auto-sends the composer prompt right after navigate)
        # can land while the background provision is still running.
        # Instead of failing with "no runner bound", wait for the
        # launch to settle: success leaves the session host-bound with
        # its runner tunnel already up (the background task awaits
        # it), failure surfaces the recorded reason.
        if runner_client is None and conv.host_id is None:
            _managed_tracker = getattr(request.app.state, "managed_launches", None)
            _managed_launch = (
                _managed_tracker.get(session_id) if _managed_tracker is not None else None
            )
            if _managed_launch is not None:
                await _await_settled_managed_launch(_managed_launch)
                # The launch bound host_id / workspace / runner_id to
                # the row after this handler's fetch — re-read so the
                # resolution below sees the bound runner.
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise _session_not_found()
                runner_client = await _get_runner_client(session_id, runner_router)
        # Check for a wrong-replica routing miss before attempting healing or
        # dispatch. The runner tunnel is registered on the same replica as its
        # host; when the tunnel is absent here but the host is live elsewhere,
        # the request landed on the wrong replica (a hostless sub-agent child
        # resolves to its ancestor's host). Signal this to the client so it
        # re-addresses rather than healing against this replica's registry.
        if runner_client is None:
            await _raise_if_runner_on_another_replica(conv, request.app.state, conversation_store)
        if runner_client is None and conv.kind == "sub_agent":
            # A sub-agent copies its parent's runner_id at creation and is
            # never repointed when the parent's runner is relaunched.  If the
            # runner is dead but the parent has a live replacement, repair the
            # stale binding via the ancestor chain and continue through the
            # normal init+dispatch flow.
            _tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
            healed_client = await _heal_subagent_runner_binding_via_parent(
                conv,
                runner_router,
                _tunnel_registry,
                conversation_store,
            )
            if healed_client is not None:
                runner_client = healed_client
                conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
                if conv is None:
                    raise _session_not_found()
        # Track the host's launch verdict for the unavailable response below.
        relaunched_runner_id: str | None = None
        relaunched_launch_acknowledged = False
        relaunched_launch_refused = False
        # The host's launch-refusal reason and whether THIS requester may
        # see it verbatim. Kept out of runner_exit_reports on purpose: that
        # store also feeds the unscoped session-snapshot path
        # (last_task_error), which would hand every read-level collaborator
        # the raw host text the 503 below deliberately owner-scopes.
        relaunch_refusal_reason: str | None = None
        relaunch_refusal_visible = False
        if runner_client is None and conv.host_id is None:
            # A managed session whose first launch failed has no host yet. Launch it again the
            # way its create did; once a host is bound, the host path below waits for the runner.
            if await _maybe_relaunch_managed_sandbox(
                session_id=session_id,
                conv=conv,
                app_state=request.app.state,
                conversation_store=conversation_store,
            ):
                conv_after_launch = await asyncio.to_thread(
                    conversation_store.get_conversation, session_id
                )
                if conv_after_launch is None:
                    raise _session_not_found()
                conv = conv_after_launch
                runner_client = await _get_runner_client(session_id, runner_router)
        if runner_client is None and conv.host_id is not None:
            _tunnel_registry = getattr(request.app.state, "tunnel_registry", None)
            _grace_host_reg = cast(
                HostRegistry | None,
                getattr(request.app.state, "host_registry", None),
            )
            _grace_host_conn = (
                _grace_host_reg.get(conv.host_id) if _grace_host_reg is not None else None
            )
            # A just-created host session already has a runner_id before
            # the runner's tunnel is registered. The Web UI can post the
            # first message during that gap; wait briefly for the pinned
            # runner before treating it as dead and replacing it — but end
            # that wait early when the runner is not actually coming. The
            # host owns runner-process liveness (it holds the Popen), so we
            # race a ``host.runner_status`` query against the connect grace:
            # a booting runner connects (or reads "alive") and we forward,
            # while one that was stopped, crashed, or lost to a host restart
            # reads "dead"/"unknown" and cuts the wait short so the relaunch
            # below runs at once. A host that is offline, too old to answer,
            # or slow yields no verdict and the grace runs its normal
            # course, so the query only ever speeds up the cold path.
            from omnigent.server.routes import sessions as _sf

            if conv.runner_id is not None and _sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S > 0:
                _logger.info(
                    "Waiting up to %.1fs for host-bound runner %s to register "
                    "for session %s before relaunch",
                    _sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                    conv.runner_id,
                    session_id,
                    extra={"session_id": session_id},
                )
                if _grace_host_reg is not None and _grace_host_conn is not None:
                    runner_client = await _wait_for_host_bound_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                        host_conn=_grace_host_conn,
                        host_registry=_grace_host_reg,
                    )
                else:
                    # Host tunnel absent: no one to query, so this is the
                    # plain connect grace (unchanged pre-existing behavior).
                    runner_client = await _wait_for_runner_client(
                        session_id,
                        runner_router,
                        _tunnel_registry,
                        runner_id=conv.runner_id,
                        timeout_s=_sf._HOST_BOUND_RUNNER_CONNECT_GRACE_S,
                        runner_exit_reports=runner_exit_reports,
                    )
            # Runner is dead or still not spawned for a host-bound
            # session. Ask the host to launch one, then re-fetch the
            # runner client and wait briefly for it to connect before
            # forwarding the message. This is the relaunch path a
            # non-sticky Stop relies on: after Stop drops the runner
            # tunnel, the next message lands here and relaunches the
            # session on its still-online host. Gated only on host
            # presence — if the host is offline this falls through to
            # the RUNNER_UNAVAILABLE raise below, the same as a
            # disconnected CLI session.
            _host_reg = getattr(request.app.state, "host_registry", None)
            if runner_client is None and _host_reg is not None:
                _host_conn = _host_reg.get(conv.host_id)
                _row_store = getattr(request.app.state, "host_store", None)
                if (
                    _host_conn is None
                    and not multi_replica()
                    and _row_store is not None
                    and await asyncio.to_thread(_row_store.is_online, conv.host_id)
                ):
                    # A fresh "online" row with no connection here: this engine just
                    # restarted (or the tunnel just dropped) and the host is redialling.
                    # Give it its reconnect window before treating it as gone.
                    _host_conn = await _host_reg.wait_for(conv.host_id, _HOST_RECONNECT_GRACE_S)
                if _host_conn is not None:
                    launch_attempt = await _launch_runner_on_host(
                        conv,
                        conversation_store,
                        _host_reg,
                        _host_conn,
                    )
                    host_error_code = _classify_launch_refusal(
                        launch_attempt.error_code,
                        launch_attempt.error,
                        conv.workspace,
                    )
                    host_error: str | None = launch_attempt.error
                    if host_error_code == _WORKSPACE_MISSING_ERROR_CODE:
                        # Rebuild from the authorized session row instead
                        # of reflecting arbitrary host-provided text.
                        host_error = _workspace_missing_message(conv.workspace)
                    if host_error_code is not None:
                        # No runner can connect after either safe categorical
                        # refusal. Consume the message and record the actionable
                        # reason instead of waiting into a generic unavailable
                        # response. The binding stays for a later retry.
                        item_id = await _persist_host_launch_failure_turn(
                            session_id,
                            conv,
                            body,
                            conversation_store,
                            host_error,
                            runner_router,
                            created_by=created_by,
                            host_error_code=host_error_code,
                        )
                        return {"queued": True, "item_id": item_id}
                    relaunched_runner_id = launch_attempt.runner_id
                    relaunched_launch_acknowledged = launch_attempt.acknowledged
                    if launch_attempt.error or launch_attempt.error_code:
                        # The host answered "failed" without a recognized
                        # category: no runner is coming. Keep the host's
                        # reason in locals for the raise below — recording it
                        # in runner_exit_reports would leak it to non-owners
                        # via the unscoped snapshot read — and skip the
                        # pointless connect wait. Visibility mirrors
                        # RunnerExitReports.get_visible: unauthenticated
                        # deployments and the host owner see the reason;
                        # other session viewers get the phase-level cause.
                        relaunched_launch_refused = True
                        relaunch_refusal_reason = launch_attempt.error
                        relaunch_refusal_visible = (
                            user_id is None
                            or _host_conn.owner is None
                            or _host_conn.owner == user_id
                        )
                else:
                    # The host tunnel is gone entirely. A managed
                    # host's sandbox is relaunchable — provision a new
                    # generation under the same host identity and ride
                    # it; an external (laptop) host falls through to
                    # the unavailable raise below.
                    if await _maybe_relaunch_managed_sandbox(
                        session_id=session_id,
                        conv=conv,
                        app_state=request.app.state,
                        conversation_store=conversation_store,
                    ):
                        conv_after_relaunch = await asyncio.to_thread(
                            conversation_store.get_conversation, session_id
                        )
                        if conv_after_relaunch is None:
                            raise _session_not_found()
                        conv = conv_after_relaunch
                        runner_client = await _get_runner_client(session_id, runner_router)
            if runner_client is None and not relaunched_launch_refused:
                _logger.info(
                    "Waiting up to %.0fs for host %s to spawn a runner for session %s",
                    _HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    conv.host_id,
                    session_id,
                )
                runner_client = await _wait_for_runner_client(
                    session_id,
                    runner_router,
                    _tunnel_registry,
                    runner_id=relaunched_runner_id,
                    timeout_s=_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S,
                    runner_exit_reports=runner_exit_reports,
                )
            if runner_client is None:
                _runner_needs_session_init = False
            else:
                _runner_needs_session_init = True
        if runner_client is None:
            # Raise so the Omnigent server doesn't persist an item the
            # harness will never see. Other event paths (interrupt,
            # approval) are best-effort and silently skip when no
            # runner is bound — item events can't, because that
            # would desync conversation store and harness state.
            if relaunched_runner_id:
                # Name the failed phase to users and log a correlated ERROR.
                exit_report = (
                    runner_exit_reports.get(relaunched_runner_id)
                    if runner_exit_reports is not None
                    else None
                )
                # Host-produced log tails are owner-visible only; other
                # viewers receive the phase, while operators get the full log.
                visible_report = (
                    runner_exit_reports.get_visible(relaunched_runner_id, user_id)
                    if runner_exit_reports is not None
                    else None
                )
                if relaunched_launch_refused:
                    # The host answered "failed": no runner process started.
                    event_name = "runner_launch_failed"
                    log_detail = "the host reported the launch failed" + (
                        f": {relaunch_refusal_reason}" if relaunch_refusal_reason else ""
                    )
                    if relaunch_refusal_reason and relaunch_refusal_visible:
                        launch_detail = (
                            f"the host reported the launch failed: {relaunch_refusal_reason}"
                        )
                    else:
                        launch_detail = (
                            "the host reported the launch failed. The reason "
                            "is in the daemon log on the host, visible to the "
                            "host owner."
                        )
                    launch_message = (
                        f"The host could not start runner {relaunched_runner_id} "
                        f"for this session — {launch_detail}"
                    )
                elif exit_report:
                    event_name = "runner_never_connected"
                    log_detail = f"the runner exited before connecting: {exit_report}"
                    if visible_report:
                        launch_detail = f"the runner exited before connecting: {visible_report}"
                    else:
                        launch_detail = (
                            "the runner exited before connecting. The exit "
                            "report is in the runner log on the host, visible "
                            "to the host owner."
                        )
                    launch_message = (
                        f"The host launched runner {relaunched_runner_id} for "
                        f"this session, but {launch_detail}"
                    )
                elif relaunched_launch_acknowledged:
                    event_name = "runner_never_connected"
                    launch_detail = (
                        "it never connected to the server within "
                        f"{_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S:.0f}s — the "
                        "runner process may be hung or unable to reach the "
                        "server. Check the runner log on the host."
                    )
                    log_detail = launch_detail
                    launch_message = (
                        f"The host launched runner {relaunched_runner_id} for "
                        f"this session, but {launch_detail}"
                    )
                else:
                    # No host acknowledgment: do not claim a launch happened.
                    event_name = "runner_never_connected"
                    launch_detail = (
                        "the host never confirmed the launch and no runner "
                        "connected to the server within "
                        f"{_HOST_RELAUNCH_RUNNER_CONNECT_TIMEOUT_S:.0f}s. "
                        "Check the daemon and runner logs on the host."
                    )
                    log_detail = launch_detail
                    launch_message = (
                        f"The host was asked to launch runner "
                        f"{relaunched_runner_id} for this session, but "
                        f"{launch_detail}"
                    )
                _logger.error(
                    "Runner %s for session %s did not become available after a "
                    "relaunch on host %s; failing the send as runner_unavailable (%s)",
                    relaunched_runner_id,
                    session_id,
                    conv.host_id,
                    log_detail,
                    extra=debug_event(
                        event_name,
                        session_id=session_id,
                        runner_id=relaunched_runner_id,
                        host_id=conv.host_id,
                    ),
                )
                raise OmnigentError(
                    launch_message,
                    code=ErrorCode.RUNNER_UNAVAILABLE,
                )
            raise OmnigentError(
                "No runner bound for session",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        refreshed_conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if refreshed_conv is None:
            raise _session_not_found()
        conv = refreshed_conv
        if _runner_needs_session_init:
            # The runner was unavailable when this request began, so its
            # connect callback may still be racing us. Await the handshake
            # before forwarding the message.
            #
            # suppress_recovery_turn=True: the server already persisted the
            # message to DB before calling session-init, so the runner's
            # history load would see the pending message and start a
            # recovery turn.  The subsequent forward would then arrive to
            # an active turn, be buffered, and be processed a second time
            # once the recovery turn finishes.  Telling the runner to skip
            # recovery-turn detection here ensures the server's forward is
            # the sole trigger for the turn.
            await _ensure_runner_session_initialized(
                session_id,
                conv,
                runner_client,
                conversation_store,
                initializer=getattr(request.app.state, "runner_session_initializer", None),
                suppress_recovery_turn=True,
            )
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )
        _agent = agent_store.get(conv.agent_id) if conv.agent_id else None
        # Determine whether the agent has MCP servers so the runner's
        # proxy_stream handler knows to initialise ProxyMcpManager.
        # agent_cache.load() is O(1) on a warm in-memory cache; the
        # asyncio.to_thread wrapper covers the rare cold-cache path
        # where the bundle is extracted from disk for the first time.
        _has_mcp_servers = False
        if _agent is not None and agent_cache is not None and _agent.bundle_location:
            try:
                _loaded_agent = await asyncio.to_thread(
                    agent_cache.load,
                    _agent.id,
                    _agent.bundle_location,
                )
                _has_mcp_servers = bool(_loaded_agent.spec.mcp_servers)
            except Exception:
                _logger.warning(
                    "Failed to load agent spec for MCP hint for session=%s",
                    session_id,
                    exc_info=True,
                )
        pending_background_title = prepare_background_session_title(
            coordinator=background_title_coordinator,
            conversation=conv,
            event=body,
            enabled=background_session_titles_enabled(request.headers),
        )
        # Schedule display-name generation for child sessions (the
        # title coordinator skips children because their title is
        # the stable spawn-or-continue key).
        if (
            conv.parent_conversation_id is not None
            and conv.task_summary is None
            and background_title_coordinator is not None
        ):
            _prompt_for_display = background_title_prompt(body)
            if _prompt_for_display:
                schedule_background_child_task_summary(
                    coordinator=background_title_coordinator,
                    session_id=session_id,
                    prompt=_prompt_for_display,
                    agent_id=conv.agent_id,
                    sub_agent_name=conv.sub_agent_name,
                )
        if body.type == _SLASH_COMMAND_TYPE:
            if _agent is None:
                raise OmnigentError(
                    f"Session {session_id!r} has no agent; cannot run slash command",
                    code=ErrorCode.INVALID_INPUT,
                )
            item_id = await _dispatch_skill_slash_command_to_runner(
                session_id,
                conv,
                body,
                conversation_store,
                runner_client,
                agent=_agent,
                has_mcp_servers=_has_mcp_servers,
                created_by=created_by,
            )
            if pending_background_title is not None:
                pending_background_title.schedule(expected_seed_title=conv.title)
            return {"queued": True, "item_id": item_id}
        dispatch = await _dispatch_session_event_to_runner(
            session_id,
            conv,
            body,
            conversation_store,
            runner_client,
            agent_name=_agent.name if _agent else None,
            file_store=file_store,
            artifact_store=artifact_store,
            has_mcp_servers=_has_mcp_servers,
            created_by=created_by,
            # Read only for the gateway-backing check that decides which router
            # serves this turn; absent, routing keeps its default posture.
            host_store=getattr(request.app.state, "host_store", None),
            persisted_item=persisted_item,
            app_state=request.app.state,
        )
        if pending_background_title is not None:
            pending_background_title.schedule(expected_seed_title=conv.title)
        response: dict[str, Any] = {"queued": True}
        if dispatch.item_id is not None:
            response["item_id"] = dispatch.item_id
        if dispatch.turn is not None:
            # The runner's number for the turn the message started or joined: its
            # ``session.status`` edges carry it, so a sender can tell that turn's end.
            response["turn"] = dispatch.turn
        # Native-terminal web message: hand back the pending-input id. It
        # identifies the snapshot's replayed bubble on rebind and is the
        # cleared_pending_id the consume event carries to drop it. Clients
        # may adopt it onto their optimistic bubble for id-based dedupe;
        # the first-party web client keeps its client temp id (React-key
        # stability) and relies on stableKey + FIFO instead.
        if dispatch.pending_id is not None:
            response["pending_id"] = dispatch.pending_id
        return response

    # ── GET /sessions/{session_id}/stream ────────────────────────

    # Live-tail only. Clients reconnect via GET /v1/sessions/{id}
    # for snapshot, then open a new stream; events that fire
    # between are deduped client-side by item id (see API.md).
    @router.get(
        "/sessions/{session_id}/stream",
        # response_model=None: returns StreamingResponse, not a model.
        response_model=None,
        # responses=: surface the SSE union to OpenAPI. The
        # ``text/event-stream`` content entry's schema points at the
        # discriminated union so generated clients know what to
        # expect on the wire. ``scripts/dump_openapi.py`` rewrites
        # this in OpenAPI 3.2's ``itemSchema`` form (the OAS 3.2
        # mechanism for typing each item in a sequential stream)
        # before writing ``openapi.json`` to disk.
        responses={
            200: {
                "description": ("SSE stream of :data:`ServerStreamEvent` frames for the session."),
                "content": {
                    "text/event-stream": {
                        "schema": {"$ref": "#/components/schemas/ServerStreamEvent"},
                    },
                },
            },
        },
    )
    async def stream_session(
        request: Request,
        session_id: str,
        idle: bool = False,
    ) -> StreamingResponse:
        """
        Subscribe to the session's live SSE event stream.

        Does NOT replay history; clients reconcile via the snapshot
        endpoint. The generator emits ``[DONE]`` on normal completion
        and uses ``finally`` only for presence cleanup — see
        :func:`_stream_live_events`.

        Holding this stream open registers the caller as a session
        *viewer* (presence): co-viewers' streams receive
        ``session.presence`` events on join/leave/idle edges, and
        this stream's snapshot-on-connect includes the current
        viewer list. Presence is scoped to the session tree's root
        conversation, so viewers of different agents/sub-agents in
        one session see each other. See
        ``omnigent/server/presence.py``.

        :param request: The FastAPI request, used to detect
            disconnect.
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param idle: Presence idle flag computed by the web client
            at connect time (tab backgrounded ≥ its debounce). An
            idle *flip* mid-view arrives as a reconnect carrying the
            new value — there is no separate update endpoint.
        :returns: An SSE :class:`StreamingResponse`.
        :raises OmnigentError: 404 if no session exists.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        runner_client = await _get_runner_client(
            session_id,
            runner_router,
        )
        # Hostless children stream on their ancestor's replica too. Reject a
        # routing miss before accepting a stream that would only send heartbeats.
        if runner_client is None and conv.runner_id:
            await _raise_if_runner_on_another_replica(conv, request.app.state, conversation_store)
        await _ensure_runner_relay_ready(
            session_id,
            conv.runner_id,
            runner_client,
            conversation_store,
        )

        async def _resource_snapshot() -> list[dict[str, Any]]:
            """Gather current resource state to emit as snapshot-on-connect.

            Best-effort: every runner-touching gather is time-boxed and
            guarded so a slow/unavailable runner never blocks the live
            tail. Terminals arrive as ``session.resource.created`` (the
            same shape the web's live handler already consumes); child
            sessions as ``session.child_session.updated``; changed files
            as a single invalidate that triggers a client refetch.

            The in-flight assistant-text replay is NOT read here: it is
            dedup-sensitive and must be captured synchronously at slot
            registration via ``subscribe``'s ``pre_ready_snapshot`` hook,
            before ``ready_event`` suspends. The resource
            gathers below need awaits and are not dedup-sensitive, so they
            stay in this async hook.
            """
            events: list[dict[str, Any]] = []
            try:
                page = await asyncio.to_thread(
                    conversation_store.list_conversations,
                    limit=100,
                    kind="sub_agent",
                    parent_conversation_id=session_id,
                    order="desc",
                    sort_by="created_at",
                )
                summaries = await _child_session_summaries_from_conversations(
                    page.data,
                    session_id,
                    conversation_store,
                )
                for summary in summaries:
                    events.append(
                        {
                            "type": "session.child_session.updated",
                            "conversation_id": session_id,
                            "child_session_id": summary.id,
                            "child": summary.model_dump(mode="json"),
                        }
                    )
            except Exception:
                _logger.debug("snapshot: child sessions failed for %s", session_id, exc_info=True)
            if runner_client is not None:
                try:
                    resp = await asyncio.wait_for(
                        # order=asc: the web cache appends each replayed
                        # ``created`` event, so the replay must arrive in
                        # creation order or the session's own terminal (always
                        # created first) lands behind later agent-launched
                        # ones. limit=1000 (the runner endpoint max) keeps the
                        # oldest-first window from dropping the newest
                        # terminals past the default page of 20.
                        runner_client.get(
                            f"/v1/sessions/{session_id}/resources/terminals",
                            params={"order": "asc", "limit": "1000"},
                        ),
                        timeout=_SNAPSHOT_RUNNER_TIMEOUT_S,
                    )
                    if resp.status_code == 200:
                        for item in resp.json().get("data", []):
                            events.append({"type": "session.resource.created", "resource": item})
                except Exception:
                    _logger.debug("snapshot: terminals failed for %s", session_id, exc_info=True)
            # Tell the client to (re)fetch the changed-files list rather
            # than fetching it here (avoids a second runner round-trip).
            events.append(
                {
                    "type": "session.changed_files.invalidated",
                    "session_id": session_id,
                    "environment_id": "default",
                }
            )
            # Current viewer list (full state, includes this stream's own
            # registration) so a joiner never waits for the next presence
            # edge to learn who's here. Scoped to the session tree's root
            # so a sub-agent page sees viewers of every agent in the tree.
            events.append(presence.snapshot(conv.root_conversation_id, session_id))
            return events

        return StreamingResponse(
            _stream_live_events(
                request,
                session_id,
                _resource_snapshot,
                # Presence tracks distinct human actors only — the reserved
                # single-user "local" sentinel maps to None (no tracking),
                # same as message attribution.
                viewer_user_id=_attribution_user(user_id),
                viewer_idle=idle,
                # Scope presence to the tree's root: sub-agent pages open
                # the CHILD conversation's stream, and per-conversation
                # scoping would hide co-viewers on other agents.
                presence_root_id=conv.root_conversation_id,
            ),
            media_type="text/event-stream",
            headers={
                "X-Omnigent-Stream-Epoch": inflight_text.stream_epoch(),
                # Keep intermediaries from buffering the SSE stream:
                # ``X-Accel-Buffering: no`` disables nginx-style response
                # buffering so heartbeats and deltas reach the client as
                # they're written (a buffered proxy can delay the 15s
                # heartbeat past a client/idle timeout), and ``no-cache``
                # keeps the long-lived response out of any shared cache.
                # NOTE: this does NOT defeat the Databricks Apps ingress'
                # hard ~5-min HTTP/2 stream-duration cap — that drop is
                # handled by the client's transparent reconnect.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── DELETE /sessions/{session_id} ──────────────────────────────

    @router.delete(
        "/sessions/{session_id}",
        response_model=None,
        responses={200: {"model": ConversationDeleted}},
    )
    async def delete_session(
        request: Request,
        session_id: str,
        delete_branch: bool = False,
    ) -> ConversationDeleted:
        """Delete a session and all associated resources.

        Requires owner-level access. Tears down tasks, runner-side
        resources (environments, terminals), session files, and the
        conversation row.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param delete_branch: Opt-in git cleanup, as a query param
            (``?delete_branch=true``). When ``True`` and the session
            has a server-created worktree (``git_branch`` set), the
            host removes the worktree directory and deletes its branch
            (``git worktree remove --force`` then ``git branch -D``).
            Ignored for sessions with no worktree. If the host
            tunnel is down (typically ``runner_online: false``), the
            delete is rejected with 409 so the caller can retry
            without cleanup rather than receiving a misleading 404.
            A host-reported git failure is still logged and does not
            block the delete. Defaults to ``False`` (worktree and
            branch left untouched). See
            designs/SESSION_GIT_WORKTREE.md.
        :returns: A :class:`ConversationDeleted` confirmation.
        :raises OmnigentError: 404 if no session or no access,
            403 if insufficient permissions, 409 if
            ``delete_branch=true`` and the host is offline so
            worktree cleanup cannot run.
        """
        user_id = _require_user(request, auth_provider)
        if permission_store is not None and user_id is not None:
            is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
            if not is_admin:
                grant = await asyncio.to_thread(permission_store.get, user_id, session_id)
                if grant is None or grant.level < LEVEL_OWNER:
                    if grant is not None:
                        raise OmnigentError(
                            "Only the session owner can delete this session",
                            code=ErrorCode.FORBIDDEN,
                        )
                    raise OmnigentError(
                        "Conversation not found",
                        code=ErrorCode.NOT_FOUND,
                    )
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        await _best_effort_stop(session_id, conversation_store, runner_router)
        # Runner-side resource cleanup is best-effort: if the bound
        # runner is offline or unbound, the session must still be
        # deletable. Server-owned records (files and conversation row
        # below) live independently of the runner, and runner-side
        # resources are gone with the runner anyway.
        runner_client: httpx.AsyncClient | None = None
        try:
            runner_client = await _get_runner_client_for_resource_access(session_id)
        except OmnigentError as exc:
            _logger.info(
                "Skipping runner-side cleanup for %s; proceeding with server-side delete: %s",
                session_id,
                exc,
            )
        if runner_client is not None:
            try:
                # Allow initialization, forwarder, and resource cleanup to finish.
                await runner_client.delete(
                    f"/v1/sessions/{session_id}",
                    timeout=60.0,
                )
            except (httpx.HTTPError, ConnectionError):
                _logger.warning(
                    "Runner cleanup failed for %s, falling back",
                    session_id,
                )
        else:
            import contextlib

            from omnigent.runtime import get_terminal_registry

            with contextlib.suppress(RuntimeError):
                await get_terminal_registry().cleanup_conversation(session_id)
        # Opt-in git worktree cleanup: only when delete_branch=true and
        # the session has a server-created worktree. Runs after runner
        # teardown but before the irreversible file cleanup below: an
        # unreachable host fails the delete (409) with the session
        # retained, so nothing irrecoverable may be destroyed first.
        # Git errors on a reachable host stay best-effort.
        if (
            delete_branch
            and conv.git_branch is not None
            and conv.workspace is not None
            and conv.host_id is not None
        ):
            from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY

            await _remove_session_worktree_best_effort(
                host_id=conv.host_id,
                worktree_path=conv.workspace,
                branch=conv.git_branch,
                delete_branch=True,
                request=request,
                reason="session-delete",
                expected_root_fingerprint=conv.labels.get(WORKTREE_ROOT_LABEL_KEY),
                conversation_store=conversation_store,
                exclude_conversation_id=conv.id,
                fail_if_unavailable=True,
            )
        # Session file cleanup. delete_all_for_session returns only the blob
        # keys that became orphaned — a blob still shared by a fork in another
        # session is not returned, so the fork's attachment survives.
        if file_store is not None and artifact_store is not None:
            orphaned_blob_keys = await asyncio.to_thread(
                file_store.delete_all_for_session, session_id
            )
            for blob_key in orphaned_blob_keys:
                await asyncio.to_thread(artifact_store.delete, blob_key)
        _interrupt_fenced_sessions.discard(session_id)
        _intentional_stop_sessions.discard(session_id)
        deleted = await conversation_store.delete_conversation(session_id)
        if not deleted:
            raise _session_not_found()
        for hook in list(ON_SESSION_DELETED.values()):
            try:
                await asyncio.to_thread(hook, session_id)
            except Exception:  # a feature's leftovers never fail the delete
                _logger.warning("session delete hook failed for %s", session_id, exc_info=True)
        # The session is gone, so is its launch-progress state. Failed
        # launches are retained in the cache for reload visibility while
        # the session exists; without this eviction every deleted
        # failed-launch session would leak one entry for the process
        # lifetime.
        _session_sandbox_status_cache.pop(session_id, None)
        # Same for MCP startup state: failed/cancelled maps are retained
        # for reload visibility while the session exists, so a session
        # whose MCP startup never settled clean would leak its entry.
        _session_mcp_startup_cache.pop(session_id, None)
        # Drop the deleted session's per-user read-state from every user's
        # caches so they don't accumulate orphan entries for the process
        # lifetime.
        _prune_session_read_state(session_id)
        # Same for the tracker's entry — a deleted session's launch can
        # never be rendezvoused again (access checks 404 first), so a
        # retained failure is dead weight. ``finish`` also settles a
        # still-in-flight entry, releasing any parked message POST into
        # its session re-read (which now correctly 404s); the background
        # task's later ``fail`` on the popped entry is a no-op.
        managed_launches_for_delete = getattr(request.app.state, "managed_launches", None)
        if managed_launches_for_delete is not None:
            managed_launches_for_delete.finish(session_id)
        # Managed-host cleanup: when the session's host is backed by a
        # server-provisioned sandbox (host_type="managed"), terminate
        # the sandbox and delete the host row — which also revokes its
        # launch token. Best-effort by design — the provider's lifetime
        # cap reaps stragglers. External (laptop) hosts have no
        # sandbox_id and are never touched.
        host_store_for_managed = getattr(request.app.state, "host_store", None)
        if conv.host_id is not None and host_store_for_managed is not None:
            bound_host = await asyncio.to_thread(host_store_for_managed.get_host, conv.host_id)
            if bound_host is not None and bound_host.sandbox_provider is not None:
                from omnigent.server.hosts import terminate_host

                await terminate_host(
                    bound_host,
                    host_store_for_managed,
                    # Supplies the launcher for the provider-side
                    # terminate; None (config removed since launch)
                    # still deletes the row and revokes the token.
                    getattr(request.app.state, "sandbox_config", None),
                )
        try:
            import time as _time

            _srv_id = _get_installation_id()
            _anon_d = _tel_anon_user_id(user_id, _srv_id)
            _usage = conv.session_usage or {}
            _duration: float | None = None
            with contextlib.suppress(Exception):
                _duration = _time.time() - conv.created_at
            _tel_emit(
                _TelSessionDeletedEvent(
                    session_id=session_id,
                    installation_id=_srv_id,
                    anon_user_id=_anon_d,
                    duration_seconds=_duration,
                    input_tokens=_usage.get("input_tokens"),
                    output_tokens=_usage.get("output_tokens"),
                    total_cost_usd=_usage.get("total_cost_usd"),
                )
            )
        except Exception:
            pass
        return ConversationDeleted(id=session_id)
