"""
Shared :class:`HarnessApp` adapter that drives any inner :class:`Executor`.

The per-harness wraps (claude-sdk, pi) both use this adapter —
they differ only in the executor they construct. The adapter handles lazy executor construction,
per-turn message/event translation, tool/elicitation/policy bridging, cancellation propagation,
injection forwarding, and shutdown cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import Response

from omnigent.context.labels import SUPERSIDE_CHAT_MODE_VALUE
from omnigent.context.tool_digest import digest_tool_items
from omnigent.debug_logging import phase_scope
from omnigent.errors import ElicitationDeclinedError, ErrorPhase
from omnigent.harnesses import llm_trace
from omnigent.harnesses.executor import (
    CompactionComplete,
    CompactionStarted,
    ContextFill,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    SteerOutcome,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStarted,
    ToolInputChunk,
    TurnCancelled,
    TurnComplete,
)
from omnigent.harnesses.tracing import TracingContext, is_tracing_enabled
from omnigent.policies.types import FAIL_CLOSED_PHASES
from omnigent.runtime.harnesses._scaffold import HarnessApp, PolicyVerdictPayload, TurnContext
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server.schemas import (
    CreateResponseRequest,
    ElicitationRequestParams,
    InjectionConsumedEvent,
    InjectionOfferedEvent,
    InjectionRefusedEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
    ToolInputDeltaEvent,
)

_logger = logging.getLogger(__name__)

# Observed tool calls use "in_progress" (distinct from "action_required" for server-dispatched).
_OBSERVED_TOOL_CALL_STATUS = "in_progress"
_COMPLETED_TOOL_CALL_STATUS = "completed"


# Prefix for Claude SDK MCP-registered tool names (e.g. ``mcp__omnigent__sys_terminal_launch``).
# Inline observed events are emitted at ToolCallRequest; the ``tool_use_id`` is queued so the
# post-stream dispatch reuses the same ``call_id``, letting BlockStream dedupe the two renders.
_MCP_TOOL_NAME_PREFIX = "mcp__"


# Interrupt and reap use SEPARATE budgets: the short slice keeps a wedged interrupt from
# starving the reap (subprocess-backed executors terminate only in close/close_session).
# _INTERRUPT_SLICE_S must exceed _PiRpcSession.close()'s inner 2.0s process.wait() so the
# slice never fires first and inject a CancelledError that bypasses the SIGKILL fallback.
INTERRUPT_TIMEOUT_S = 3.0
_INTERRUPT_SLICE_S = 3.0

# Interrupting an executor abandoned by an abnormal exit usually fails because the
# harness is already gone -- that absence is what abandoned it. Its socket refuses or
# resets the connection, or the slice expires with nothing left to answer. The reap
# below still tears the subprocess down and reports its own failures, so these are
# teardown facts rather than faults. Any other error keeps ERROR.
_HARNESS_ALREADY_GONE_ERRORS = (TimeoutError, ConnectionError)

# Consecutive orphaned tool callbacks (no active turn context) before forcing a Tier-1 SDK reset.
# Reset to zero at each ``run_turn`` start so a single late straggler never trips it.
_ORPHAN_RESYNC_THRESHOLD = 3

# A turn with a context ceiling (``CreateResponseRequest.turn_context_ceiling``) is asked once
# to wrap up when a model call passes it; one that keeps going to this multiple of the ceiling
# is stopped, so a model that ignores the request cannot run on to the window.
TURN_CONTEXT_HARD_STOP_FACTOR = 1.25


def turn_ceiling_notice(tokens: int, ceiling: int) -> str:
    """The message steered into a turn whose context passed its ceiling."""
    return (
        f"[Context limit: this job's working context is at {tokens:,} tokens, past its limit "
        f"of {ceiling:,}. Start no new steps. Save any unsaved work to its files now, then "
        "end this turn with a short status: what is done, where it is saved, and what is "
        f"left. At {int(ceiling * TURN_CONTEXT_HARD_STOP_FACTOR):,} tokens the turn is "
        "stopped.]"
    )


def _strip_mcp_tool_prefix(name: str) -> str:
    """Strip the ``mcp__{server}__`` prefix from a Claude SDK tool name.

    Kept local (not imported from workflow) to avoid a cross-module dependency.
    Only ``mcp__<server>__<tool>`` shapes are stripped; bare names pass through.
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


# Prefix for local host-tool-bridge calls (``sys_os_*``). An orphaned host-tool callback
# is the deterministic respawn-desync signature, so it triggers a Tier-1 reset on the first
# occurrence rather than waiting for the consecutive-orphan threshold.
_HOST_TOOL_PREFIX = "sys_os_"


def _is_host_tool(tool_name: str) -> bool:
    """Return ``True`` for ``sys_os_*`` host-tool-bridge calls (bare or MCP-prefixed)."""
    return _strip_mcp_tool_prefix(tool_name).startswith(_HOST_TOOL_PREFIX)


class InnerExecutorError(RuntimeError):
    """
    An executor-reported failure that names its own semantic error code.

    Raised by :class:`ExecutorAdapter` when an :class:`ExecutorError` event
    carries a ``code``, so the terminal ``response.failed`` reports that code,
    headline and next step instead of the exception class name.

    :param message: Human-readable failure text shown to the user.
    :param code: Semantic failure code, e.g. ``"databricks_sign_in_pending"``.
    :param title: Short headline for the error card, or ``None``.
    :param remediation: Concrete next step for the user, or ``None``.
    :param undelivered: ``True`` when the harness never received the message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        title: str | None = None,
        remediation: str | None = None,
        undelivered: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.title = title
        self.remediation = remediation
        self.undelivered = undelivered


class ExecutorAdapter(HarnessApp):
    """
    :class:`HarnessApp` subclass that drives any inner :class:`Executor`.

    Lazily constructs the executor on the first turn, then reuses it for the conversation lifetime.
    Handles per-turn message/event translation, tool/elicitation/policy bridging, cancellation,
    injection forwarding, and tracing. Per-harness wraps differ only in the ``executor_factory``.
    """

    def __init__(
        self,
        executor_factory: Callable[[], Executor],
        session_key: str | None = None,
        harness_label: str = "Claude",
    ) -> None:
        super().__init__()
        self._executor_factory = executor_factory
        self._session_key = session_key or uuid.uuid4().hex
        # Harness label used in elicitation messages, e.g. "Claude wants to call **Bash**".
        self._harness_label = harness_label
        # Lazily-constructed inner executor, reused across turns for the conversation lifetime.
        self._executor: Executor | None = None
        # Per-turn turn context. The stable tool/elicitation/policy bridges read this at call
        # time so MCP handlers that closure-captured the bridge on turn 1 dispatch into the
        # current turn's ctx, not turn 1's dead ctx.
        self._current_ctx: TurnContext | None = None
        self._current_agent: str | None = None
        # FIFO queue of inner-SDK tool-use ids from ToolCallRequest events. Drained by
        # _stable_tool_executor so each dispatch reuses the same call_id as the inline
        # observed event, letting BlockStream dedupe the two renders into one.
        self._pending_mcp_call_ids: deque[str] = deque()
        # Per-session tracing context; created lazily on first turn and reused across turns.
        self._tracing_ctx: TracingContext | None = None
        # Detached bounded background tasks (abnormal-exit _safe_interrupt). Strongly referenced
        # until completion; drained before recovery and on shutdown.
        self._bg_tasks: set[asyncio.Task[bool]] = set()
        self._abandoned_executor_cleanup: asyncio.Task[bool] | None = None
        # Consecutive orphaned-callback counter and reset guard for the Tier-1 SDK watchdog.
        self._orphan_callback_count = 0
        self._resyncing = False
        # Call ids round-tripped via _stable_tool_executor → dispatch_tool this turn.
        # dispatch_tool emits the function_call_output, so ToolCallComplete for these ids
        # must be suppressed in _translate_event to avoid duplicates.
        self._dispatched_call_ids: set[str] = set()
        # Observed (harness-run) tool calls → (name, arguments) from the inline
        # ToolCallRequest, so the matching ToolCallComplete can re-emit a *completed*
        # function_call. The initial observed emission is status "in_progress", which
        # the turn-persist filter drops (only completed function_calls are durable), so
        # without the re-emission an observed tool card renders live but vanishes on
        # reload. Dispatched calls never reach this — their ToolCallComplete
        # short-circuits — so it is observed-only.
        self._observed_tool_calls: dict[str, tuple[str, str]] = {}
        self._pr_tool_calls: dict[str, tuple[str, dict[str, Any]]] = {}

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        """Drive the inner executor for one turn, translating its events to Omnigent SSE.

        Lazily constructs the executor on the first call; subsequent calls reuse the cached
        instance. Installs stable tool/elicitation/policy bridges once on first use.
        """
        executor = self._ensure_executor()
        messages = _translate_input_to_messages(
            request.input, tool_digests=request.context_mode == SUPERSIDE_CHAT_MODE_VALUE
        )
        # Stamp session_key on every message so the inner executor keys its client consistently,
        # matching the key used by enqueue_session_message (steering delivery depends on this).
        for message in messages:
            message["session_id"] = self._session_key
        if not messages:
            # An empty message (the runner launching the harness ahead of the first turn,
            # ADR 0026) still names the session, so what it launches is keyed as the turns are.
            messages = [{"role": "user", "content": "", "session_id": self._session_key}]
        extra: dict[str, Any] = {}
        if request.reasoning is not None:
            effort = request.reasoning.get("effort")
            if effort:
                extra["reasoning_effort"] = effort
        if request.max_output_tokens is not None:
            extra["max_tokens"] = int(request.max_output_tokens)
        # The session's omnigent.context.mode label (e.g. "superside-chat"),
        # when the runner forwarded one. Executors that don't look for this
        # key (every inner executor but claude-sdk, today) simply ignore it.
        if request.context_mode:
            extra["context_mode"] = request.context_mode
        if request.session_file:
            extra["session_file"] = request.session_file
        # model_override is the per-request override; takes precedence over the spec default.
        tools = _normalize_tool_schemas(request.tools or [])
        config = ExecutorConfig(model=request.model_override, extra=extra)
        system_prompt = request.instructions or ""
        # Install stable bridges once on first use — the SDK closure-captures them from the first
        # turn; per-turn rebinding would leave later tool calls dispatching into a dead ctx.
        if getattr(executor, "_tool_executor", None) is None:
            executor._tool_executor = self._stable_tool_executor  # type: ignore[attr-defined]
        if getattr(executor, "_elicitation_handler", None) is None:
            executor._elicitation_handler = self._stable_elicitation_handler  # type: ignore[attr-defined]
        if getattr(executor, "_policy_evaluator", None) is None:
            executor._policy_evaluator = self._stable_policy_evaluator  # type: ignore[attr-defined]
        self._current_ctx = ctx
        self._current_agent = request.model
        # Clear per-turn state so stale entries from an errored prior turn don't bleed through.
        self._pending_mcp_call_ids.clear()
        # Reset orphan counter: measures only post-bind orphans (no intervening clean turn).
        self._orphan_callback_count = 0
        # Set True before each genuine-completion return so the finally can schedule a
        # bounded interrupt only on abnormal exits (CancelledError, ExecutorError, etc.).
        clean_exit = False
        self._dispatched_call_ids.clear()
        self._observed_tool_calls.clear()
        self._pr_tool_calls.clear()

        tracing = is_tracing_enabled()
        from omnigent.runtime.telemetry import current_session_id, session_scope

        turn_session_id = ctx.session_id or current_session_id() or self._session_key
        # The model-call proxy files this session's context records under its Conversation.
        if llm_trace.context_dir() is not None:  # only eval hosts keep context records
            llm_trace.name_session(self._session_key, turn_session_id)
        if tracing and self._tracing_ctx is None:
            self._tracing_ctx = TracingContext(session_id=turn_session_id)
        tctx = self._tracing_ctx if tracing else None
        agent_span = None
        bound_span = None  # the span the model-call proxy parents on, until this turn unbinds it
        # Active tool span for correlating ToolCallRequest → ToolCallComplete.
        _active_tool_span = None
        _active_tool_parent = None

        user_message = _extract_last_user_message(request.input)

        # Steers offered to the executor whose outcome it has not reported yet, by injection id.
        offered: set[str] = set()
        # The in-turn context bound (background jobs only); asked to wrap up at most once.
        ceiling = request.turn_context_ceiling
        wrap_up_asked = False
        # Watcher forwards mid-turn steering injections into the in-flight executor session.
        injection_watcher = asyncio.create_task(
            self._watch_injections(ctx, executor, offered),
            name=f"executor-adapter-injection-watch:{ctx.response_id}",
        )
        try:
            # Entered inside the guard: the context parses the response id on entry, and an id
            # that is not ``resp_<hex>`` must not fail the turn.
            trace_cm = contextlib.ExitStack()
            if tctx:
                try:
                    from omnigent.runtime.telemetry import (
                        extract_trace_context,
                        trace_context_for_response,
                    )

                    if request.traceparent:
                        # The turn of a Round: a child of the Round's span (ADR 0041).
                        from opentelemetry import context as otel_context

                        token = otel_context.attach(
                            extract_trace_context({"traceparent": request.traceparent})
                        )
                        trace_cm.callback(otel_context.detach, token)
                    else:
                        trace_cm.enter_context(
                            trace_context_for_response(response_id=ctx.response_id)
                        )
                except Exception:
                    _logger.debug("trace_context_for_response unavailable", exc_info=True)
            with trace_cm, session_scope(turn_session_id), phase_scope(ErrorPhase.TURN):
                if tctx is not None:
                    agent_span = tctx.start_agent_span(
                        agent_name=request.model or "unknown",
                        user_message=user_message,
                        model=request.model_override or request.model,
                    )
                    # The model-call proxy parents each call's span on this turn.
                    llm_trace.bind_turn(self._session_key, agent_span)
                    bound_span = agent_span

                response_text: str | None = None
                async for event in executor.run_turn(
                    messages=messages,
                    tools=tools,
                    system_prompt=system_prompt,
                    config=config,
                ):
                    if ctx.cancelled.is_set():
                        if tctx is not None and agent_span is not None:
                            from omnigent.runtime.telemetry import record_cancellation

                            record_cancellation(agent_span)
                            tctx.end_agent_span(agent_span, response=None)
                            agent_span = None
                        # Mark clean_exit AFTER the interrupt completes: if it raises,
                        # the finally's _safe_interrupt fallback still fires.
                        await executor.interrupt_session(self._session_key)
                        clean_exit = True
                        return
                    if isinstance(event, SteerOutcome):
                        if event.steer_id in offered:
                            offered.discard(event.steer_id)
                            _report_injection(ctx, event.steer_id, taken=event.taken)
                        continue
                    if isinstance(event, ContextFill):
                        if ceiling and event.tokens >= ceiling and not wrap_up_asked:
                            wrap_up_asked = True
                            _logger.warning(
                                "turn context at %d tokens passed its ceiling of %d for %s; "
                                "asking the model to wrap up",
                                event.tokens,
                                ceiling,
                                self._session_key,
                            )
                            try:
                                offer = await executor.steer_session_message(
                                    self._session_key,
                                    turn_ceiling_notice(event.tokens, ceiling),
                                    steer_id=f"ceiling_{uuid.uuid4().hex[:12]}",
                                )
                            except Exception:
                                _logger.exception("steering the context-ceiling notice failed")
                                offer = "refused"
                            if offer == "refused":
                                _logger.warning(
                                    "the context-ceiling notice was refused for %s",
                                    self._session_key,
                                )
                        elif ceiling and event.tokens >= ceiling * TURN_CONTEXT_HARD_STOP_FACTOR:
                            _logger.warning(
                                "turn context at %d tokens is past %.2fx its ceiling of %d "
                                "for %s; stopping the turn",
                                event.tokens,
                                TURN_CONTEXT_HARD_STOP_FACTOR,
                                ceiling,
                                self._session_key,
                            )
                            # Handled by the cancelled check at the top of the loop.
                            ctx.cancelled.set()
                        continue
                    # --- Tracing: emit spans per event ---
                    if tctx is not None:
                        if isinstance(event, ToolCallRequest):
                            _active_tool_parent = tctx._current_span
                            _active_tool_span = tctx.start_tool_span(
                                _strip_mcp_tool_prefix(event.name),
                                event.args or {},
                            )
                        elif isinstance(event, ToolCallComplete):
                            if _active_tool_span is not None:
                                tctx.end_tool_span(
                                    _active_tool_span,
                                    result=event.result,
                                    error=event.error,
                                    duration_ms=event.duration_ms,
                                    parent_span=_active_tool_parent,
                                )
                                _active_tool_span = None
                                _active_tool_parent = None
                        elif isinstance(event, TurnComplete):
                            response_text = event.response
                            if event.usage is not None and agent_span is not None:
                                from omnigent.runtime.telemetry import record_llm_usage

                                record_llm_usage(agent_span, event.usage)
                    # --- End tracing ---
                    self._translate_event(event, ctx)
                    if isinstance(event, TurnComplete):
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(agent_span, response=response_text)
                            agent_span = None
                        clean_exit = True
                        return
                    if isinstance(event, TurnCancelled):
                        ctx.cancelled.set()
                        if tctx is not None and agent_span is not None:
                            from omnigent.runtime.telemetry import record_cancellation

                            record_cancellation(agent_span)
                            tctx.end_agent_span(agent_span, response=None, error="cancelled")
                        # Inner executor wound down cleanly.
                        clean_exit = True
                        return
                    if isinstance(event, ExecutorError):
                        clean_exit = event.preserve_session
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(
                                agent_span,
                                response=None,
                                error=event.message,
                            )
                            agent_span = None
                        # Keep any usage the executor observed before the
                        # failure: the scaffold's terminal-event builder reads
                        # ctx.provider_usage, so the response.failed event still
                        # carries context_tokens and the occupancy meter doesn't
                        # freeze at the previous turn's value.
                        if event.usage is not None:
                            ctx.provider_usage = event.usage
                        # Guard: empty message surfaces as "inner executor error: " with no detail.
                        detail = event.message or "no detail reported (see runner/harness logs)"
                        if event.code:
                            # The executor named the failure: keep its code, headline
                            # and next step so the card reads as that failure rather
                            # than as a bare exception class.
                            raise InnerExecutorError(
                                detail,
                                code=event.code,
                                title=event.title,
                                remediation=event.remediation,
                                undelivered=event.undelivered,
                            )
                        raise RuntimeError(f"inner executor error: {detail}")
        except ElicitationDeclinedError:
            # Fallback for non-SDK executors; SDK-based paths use ctx.cancelled.set() instead.
            _logger.info(
                "elicitation explicitly declined for response %s — aborting turn",
                ctx.response_id,
            )
            if tctx is not None and agent_span is not None:
                from omnigent.runtime.telemetry import record_cancellation

                record_cancellation(agent_span)
                tctx.end_agent_span(agent_span, response=None)
            ctx.cancelled.set()
            if self._executor is not None:
                await self._executor.interrupt_session(self._session_key)
        except BaseException:
            # Close the span so it doesn't leak on the OTel provider.
            if tctx is not None and agent_span is not None:
                tctx.end_agent_span(agent_span, response=None, error="unhandled exception")
                agent_span = None
            raise
        finally:
            # Cancel the injection watcher (long-polls a queue; cancellation is the only way out).
            injection_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await injection_watcher
            # The turn is over: a steer it never reported taking was not read, so the runner
            # keeps its copy for the next turn.
            for injection_id in sorted(offered):
                _report_injection(ctx, injection_id, taken=False)
            offered.clear()
            if tctx is not None:
                llm_trace.unbind_turn(self._session_key, bound_span)
                try:
                    from opentelemetry import trace as otel_trace

                    provider = otel_trace.get_tracer_provider()
                    if hasattr(provider, "force_flush"):
                        provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass
            # Compare-and-clear: only null the slot when it still points at THIS turn's ctx
            # so a late-unwinding finally doesn't clobber a newer turn's binding.
            if self._current_ctx is ctx:
                self._current_ctx = None
                self._current_agent = None
            # Detach before background cleanup so a continuation cannot reuse this executor.
            if not clean_exit:
                abandoned_executor = self._executor
                self._executor = None
                interrupt_task = asyncio.create_task(
                    self._safe_interrupt(abandoned_executor, self._session_key)
                )
                self._abandoned_executor_cleanup = interrupt_task
                self._bg_tasks.add(interrupt_task)
                interrupt_task.add_done_callback(self._bg_tasks.discard)

    async def _handle_interrupt_event(self) -> Response:
        """Cancel the turn and drop the inner executor session synchronously.

        Extends the base handler to also call ``interrupt_session`` — ensuring the in-flight
        generation stops even when the run loop is blocked awaiting the first token. A Stop
        between turns has no turn to cancel but still drops the warm session, so the next turn
        replays the history and reads the Stop line the runner wrote: a warm Pi process would
        otherwise be sent only the person's next message.
        """
        if not self._in_flight and self._executor is not None:
            await self._executor.interrupt_session(self._session_key)
            return Response(status_code=204)
        response = await super()._handle_interrupt_event()
        if self._executor is not None:
            await self._executor.interrupt_session(self._session_key)
        return response

    async def _prepare_turn_retry(self) -> bool:
        """Wait for confirmed teardown without cancelling the background reap."""
        cleanup = self._abandoned_executor_cleanup
        return cleanup is not None and await asyncio.shield(cleanup)

    async def _safe_interrupt(self, executor: Executor | None, session_key: str) -> bool:
        """Best-effort bounded interrupt + close of a detached abandoned executor.

        Scheduled from run_turn's finally and awaited before any recovery retry. The interrupt
        gets a short slice; the reap (close_session + close) always runs under its own budget so
        a wedged interrupt can never starve the subprocess reap.

        An interrupt that only proves the harness is already gone
        (:data:`_HARNESS_ALREADY_GONE_ERRORS`) logs a warning naming the cause; every other
        failure stays at ERROR.
        """
        if executor is None:
            return True
        try:
            await asyncio.wait_for(
                executor.interrupt_session(session_key), timeout=_INTERRUPT_SLICE_S
            )
        except _HARNESS_ALREADY_GONE_ERRORS as exc:
            _logger.warning(
                "abnormal-exit interrupt of inner session %s found the harness already gone (%s)",
                session_key,
                type(exc).__name__,
                exc_info=True,
            )
        except Exception:  # best-effort: a failed/timed-out interrupt is logged, not raised
            _logger.error(
                "abnormal-exit interrupt of inner session %s failed or timed out",
                session_key,
                exc_info=True,
            )

        async def _reap() -> bool:
            closed = True
            try:
                await executor.close_session(session_key)
            except Exception:
                _logger.exception("abandoned executor close_session failed")
                closed = False
            try:
                await executor.close()
            except Exception:
                _logger.exception("abandoned executor close failed")
                closed = False
            return closed

        try:
            return await asyncio.wait_for(_reap(), timeout=INTERRUPT_TIMEOUT_S)
        except Exception:
            _logger.exception("abandoned executor teardown failed or timed out")
            return False

    async def _maybe_resync_on_orphan(self, *, force: bool = False) -> None:
        """Tier-1 SDK reset after repeated orphan callbacks (or immediately when ``force=True``).

        Drops the cached executor so the next turn rebuilds a fresh client. ``force=True`` is used
        for orphaned ``sys_os_*`` callbacks — the deterministic respawn-desync signature — to reset
        on the first one rather than waiting for the threshold.
        Idempotent: ``_resyncing`` ensures a burst of orphans triggers exactly one reset.
        """
        if not force and self._orphan_callback_count < _ORPHAN_RESYNC_THRESHOLD:
            return
        if self._resyncing:
            return
        self._resyncing = True
        try:
            async with self._lock:
                # Re-check under lock: a concurrent reset may have already zeroed the counter.
                if not force and self._orphan_callback_count < _ORPHAN_RESYNC_THRESHOLD:
                    return
                _logger.error(
                    "runner_turn_context_desync: %d consecutive orphan callbacks; "
                    "forcing Tier-1 SDK reset for session %s",
                    self._orphan_callback_count,
                    self._session_key,
                )
                executor = self._executor
                self._executor = None
                self._current_ctx = None
                self._current_agent = None
                # Do NOT touch _in_flight or _active_turn_ctx — those are scaffold-owned and
                # back pending tool futures; clearing them would strand or hang dispatches.
                self._orphan_callback_count = 0
            # Close outside the lock — close_session can block on subprocess teardown.
            if executor is not None:

                async def _close(exc: Executor = executor) -> None:
                    with contextlib.suppress(Exception):
                        await exc.close_session(self._session_key)
                    with contextlib.suppress(Exception):
                        await exc.close()

                with contextlib.suppress(Exception):
                    await asyncio.wait_for(_close(), timeout=INTERRUPT_TIMEOUT_S)
        finally:
            self._resyncing = False

    async def _watch_injections(
        self, ctx: TurnContext, executor: Executor, offered: set[str]
    ) -> None:
        """Offer mid-turn steering injections to the in-flight executor session.

        Loops until cancelled by run_turn's finally. The runner is told how each one ended:
        ``injection.consumed`` once the executor's running turn took it up,
        ``injection.refused`` when it did not (the runner then keeps it for the next turn).
        An executor that learns the outcome later answers ``"pending"``: the id stays in
        *offered* until its :class:`SteerOutcome` arrives on the turn's events, or the turn
        ends and run_turn refuses it.
        """
        while True:
            try:
                injection = await ctx.next_injection(timeout=None)
            except asyncio.CancelledError:
                return
            if injection is None:
                # None signals a sentinel or protocol change; bail to avoid spinning.
                return
            # Only a runner-stamped injection has a buffered copy to report on; any other
            # (e.g. an async completion) is delivered best-effort under a local id.
            injection_id = getattr(injection, "injection_id", None)
            steer_id = injection_id or f"local_{uuid.uuid4().hex}"
            text = _extract_user_text(injection.input)
            if not text or not _is_text_only(injection.input):
                # Offered only whole: one that is not plain text runs as a turn instead.
                _logger.warning(
                    "not steering an in-band injection that is not plain text: %r",
                    injection.input,
                )
                if injection_id:
                    _report_injection(ctx, injection_id, taken=False)
                continue
            if ctx.cancelled.is_set():
                return
            if injection_id:
                # Tracked before the call: a cancellation mid-write leaves it for run_turn.
                offered.add(injection_id)
            try:
                offer = await executor.steer_session_message(
                    self._session_key, text, steer_id=steer_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("inner executor.steer_session_message failed")
                offer = "refused"
            if injection_id and offer == "pending" and injection_id in offered:
                offered_event = InjectionOfferedEvent(
                    type="injection.offered", injection_id=injection_id
                )
                ctx.emit(offered_event)
            if not injection_id or offer == "pending" or injection_id not in offered:
                continue  # nothing to report, or settled by its SteerOutcome / the turn's end
            offered.discard(injection_id)
            if offer == "refused":
                _logger.info(
                    "inner executor refused in-band injection %s; it waits for the next turn",
                    injection_id,
                )
            _report_injection(ctx, injection_id, taken=offer == "taken")

    async def _stable_tool_executor(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """Bridge installed once on the executor; dispatches tool calls into the current turn ctx.

        Reads ``_current_ctx`` at call time. Returns a structured error for orphaned callbacks.
        """
        ctx = self._current_ctx
        agent = self._current_agent
        if ctx is None or agent is None:
            # Orphaned callback — safe-fail to avoid parking a Future on a dead ctx.
            # Host-tool orphans are the deterministic respawn-desync signature, so force
            # Tier-1 reset immediately (only when the scaffold also has no live turn).
            self._orphan_callback_count += 1
            host_tool = _is_host_tool(tool_name) and self._active_turn_ctx is None
            _logger.error(
                "runner_turn_context_desync: tool callback fired with no active "
                "turn context (tool=%s, host_tool=%s, consecutive_orphans=%d); "
                "returning error",
                tool_name,
                host_tool,
                self._orphan_callback_count,
            )
            await self._maybe_resync_on_orphan(force=host_tool)
            return {
                "error": "no active turn context for tool dispatch",
                "code": "runner_turn_context_desync",
            }
        # Explicit IDs survive callbacks arriving before, or out of order with,
        # stdout observations. SDKs without callback IDs retain FIFO correlation.
        correlated_call_id = call_id
        if call_id:
            with contextlib.suppress(ValueError):
                self._pending_mcp_call_ids.remove(call_id)
        elif self._pending_mcp_call_ids:
            correlated_call_id = self._pending_mcp_call_ids.popleft()
        # Allocate id here to record in _dispatched_call_ids; matching ToolCallComplete
        # is suppressed in _translate_event (dispatch_tool already emits its output).

        dispatch_call_id = correlated_call_id or f"call_{uuid.uuid4().hex[:12]}"
        self._dispatched_call_ids.add(dispatch_call_id)
        return await _bridge_one_dispatch(
            ctx,
            agent,
            tool_name,
            args,
            call_id=dispatch_call_id,
        )

    async def _stable_elicitation_handler(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Stable bridge for tool-permission elicitation (``can_use_tool``).

        Reads ``_current_ctx`` at call time; denies by default when no turn is active.
        """
        ctx = self._current_ctx
        if ctx is None:
            # No active turn — deny by default (fail safe, never grant unreviewed permission).
            _logger.error(
                "elicitation callback fired with no active turn context "
                "(tool=%s); denying by default",
                tool_name,
            )
            return False

        elicitation_id = f"elicit_{secrets.token_hex(16)}"
        params = self._permission_card(tool_name, tool_input)
        result = await ctx.elicit(elicitation_id, params)
        if result.action == "decline":
            # Signal via ctx.cancelled (the SDK swallows exceptions from control-request tasks).
            ctx.cancelled.set()
        return result.action == "accept"

    def _permission_card(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> ElicitationRequestParams:
        """Build the Approve/Reject card params for a tool-permission elicitation."""
        # Build a concise preview: truncate long args so the UI widget
        # stays readable. 300 chars matches AP's policy-engine preview.
        try:
            preview = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            preview = repr(tool_input)
        preview = preview[:300]

        label = self._harness_label
        return ElicitationRequestParams(
            mode="form",
            message=f"{label} wants to use **{tool_name}**",
            requestedSchema=None,
            url=None,
            phase="tool_call",
            policy_name=f"{label.lower()}_sdk_permission",
            content_preview=f"{tool_name}({preview})",
        )

    async def _stable_policy_evaluator(
        self,
        phase: str,
        data: dict[str, Any],
    ) -> PolicyVerdictPayload:
        """Stable bridge for policy evaluation; routes through ``ctx.evaluate_policy``.

        Fails closed (DENY) for tool-call phases when no turn context is active.
        """
        ctx = self._current_ctx
        if ctx is None:
            # Orphaned callback — fail closed for tool-call phases; ALLOW for advisory ones.
            fail_closed = phase in FAIL_CLOSED_PHASES
            action = "POLICY_ACTION_DENY" if fail_closed else "POLICY_ACTION_ALLOW"
            self._orphan_callback_count += 1
            _logger.error(
                "runner_turn_context_desync: policy evaluator fired with no active "
                "turn context (phase=%s, consecutive_orphans=%d); defaulting to %s",
                phase,
                self._orphan_callback_count,
                action,
            )
            await self._maybe_resync_on_orphan()
            return PolicyVerdictPayload(
                action=action,
                reason=(
                    f"No active turn context; failing closed for {phase}." if fail_closed else None
                ),
            )
        evaluation_id = f"poleval_{secrets.token_hex(16)}"
        return await ctx.evaluate_policy(evaluation_id, phase, data)

    def _ensure_executor(self) -> Executor:
        """
        Construct the inner executor on first use; return cached
        instance thereafter.

        :returns: The per-conversation inner :class:`Executor`.
        """
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def _translate_event(self, event: ExecutorEvent, ctx: TurnContext) -> None:
        """Translate one inner ExecutorEvent into Omnigent SSE events via ``ctx.emit``."""
        if isinstance(event, TextChunk):
            ctx.emit(
                OutputTextDeltaEvent(
                    type="response.output_text.delta",
                    delta=event.text,
                )
            )
        elif isinstance(event, ReasoningChunk):
            if event.event_type == "reasoning_started":
                ctx.emit(
                    ReasoningStartedEvent(type="response.reasoning.started"),
                )
            elif event.event_type == "reasoning_summary":
                ctx.emit(
                    ReasoningSummaryTextDeltaEvent(
                        type="response.reasoning_summary_text.delta",
                        delta=event.delta,
                    ),
                )
            else:
                # ``reasoning_text`` and any future flavor land here.
                ctx.emit(
                    ReasoningTextDeltaEvent(
                        type="response.reasoning_text.delta",
                        delta=event.delta,
                    ),
                )
        elif isinstance(event, (ToolCallStarted, ToolInputChunk)):
            # A call still being written: the relay records it, clients never see it.
            call_id = _call_id_from_metadata(event.metadata)
            if call_id:
                ctx.emit(
                    ToolInputDeltaEvent(
                        type="response.function_call_arguments.delta",
                        call_id=call_id,
                        name=_strip_mcp_tool_prefix(event.name),
                        delta=event.delta if isinstance(event, ToolInputChunk) else "",
                    )
                )
        elif isinstance(event, ToolCallRequest):
            # Emit observed function_call INLINE (interleaved with text). Queue the tool_use_id
            # so the post-stream dispatch reuses the same call_id for deduplication.
            # Emit bare names (strip MCP prefix) to match the Omnigent wire shape.
            tool_use_id = _call_id_from_metadata(event.metadata)
            if tool_use_id in self._dispatched_call_ids:
                # Dispatch already owns this card; a late observation must not
                # downgrade it or queue an ID for a different tool.
                return
            # Observed native tools already ran inside their harness. They must
            # never enter the queue consumed by Omnigent's dispatch bridge.
            if tool_use_id is not None and event.metadata.get("internally_executed") is not True:
                self._pending_mcp_call_ids.append(tool_use_id)
            call_id = tool_use_id or f"call_{uuid.uuid4().hex[:12]}"
            self._pr_tool_calls[call_id] = (event.name, event.args or {})
            bare_name = _strip_mcp_tool_prefix(event.name)
            arguments_json = _serialize_args(event.args)
            if event.metadata.get("observed_call_completed") is True:
                # The executor already emits this call's durable completed form
                # itself (e.g. a codex built-in re-emitted at completion). Emit it
                # completed in one shot and drop any live entry cached from an
                # earlier in_progress emit of the same call, so the ToolCallComplete
                # below does not re-emit a second, duplicate completed card.
                status = _COMPLETED_TOOL_CALL_STATUS
                self._observed_tool_calls.pop(call_id, None)
            else:
                # A live observation. The turn-persist filter drops "in_progress",
                # so remember name/args; the matching ToolCallComplete re-emits it as
                # a durable completed function_call (see that branch) — which is what
                # keeps an observed card from vanishing on reload.
                status = _OBSERVED_TOOL_CALL_STATUS
                self._observed_tool_calls[call_id] = (bare_name, arguments_json)
            ctx.emit(
                OutputItemDoneEvent(
                    type="response.output_item.done",
                    item={
                        "id": f"fc_{uuid.uuid4().hex[:12]}",
                        "type": "function_call",
                        "status": status,
                        "name": bare_name,
                        "arguments": arguments_json,
                        "call_id": call_id,
                        "agent": ctx.response_id,
                    },
                )
            )
        elif isinstance(event, ToolCallComplete):
            # Suppress dispatched ids (dispatch_tool already emitted their output) and empty ids
            # (unpaiable — they'd render a ghost card). Internally-run tools
            # stamp real ids and are the sole output source; they must not be suppressed.
            call_id = _call_id_from_metadata(getattr(event, "metadata", None)) or ""
            if not call_id:
                return
            # A call that completes before any dispatch claimed it never will be
            # dispatched (the CLI rejected an unknown tool name, a permission was
            # denied, or the harness ran it itself). Drop its id from the FIFO, or
            # the next un-ided bridge callback pops it and records the following
            # tool call under this stale id, duplicating both cards.
            with contextlib.suppress(ValueError):
                self._pending_mcp_call_ids.remove(call_id)
            if call_id in self._dispatched_call_ids:
                self._observed_tool_calls.pop(call_id, None)
                self._pr_tool_calls.pop(call_id, None)
                return
            # A live observed call cached at ToolCallRequest is re-emitted here as a
            # durable COMPLETED function_call so it survives reload — the in_progress
            # one is dropped by the turn-persist filter. Same call_id → the web dedupes
            # this with the live render into one card. A call already emitted completed
            # (observed_call_completed) was never cached, so it is not re-emitted — that
            # is what prevents a duplicate card. This mirrors how a dispatched call
            # re-emits completed once its dispatch resolves.
            observed = self._observed_tool_calls.pop(call_id, None)
            pr_call = self._pr_tool_calls.pop(call_id, None)
            if pr_call is not None:
                from omnigent.runner.pr_observer import observe_tool_completion

                session_id = ctx.session_id
                if session_id:
                    observe_tool_completion(
                        session_id,
                        tool_name=pr_call[0],
                        arguments=pr_call[1],
                        result=event.result,
                        successful=event.status == "success",
                        call_id=call_id,
                        source="sdk",
                    )
            if observed is not None:
                observed_name, observed_args = observed
                ctx.emit(
                    OutputItemDoneEvent(
                        type="response.output_item.done",
                        item={
                            "id": f"fc_{uuid.uuid4().hex[:12]}",
                            "type": "function_call",
                            "status": _COMPLETED_TOOL_CALL_STATUS,
                            "name": observed_name,
                            "arguments": observed_args,
                            "call_id": call_id,
                            "agent": ctx.response_id,
                        },
                    )
                )
            raw_args = getattr(event, "metadata", {}).get("arguments")
            item: dict[str, Any] = {
                "id": f"fco_{uuid.uuid4().hex[:12]}",
                "type": "function_call_output",
                "call_id": call_id,
                # Cap the mirror; the inner SDK already consumed the full result.
                "output": cap_tool_output(_serialize_tool_result(event)),
            }
            if isinstance(raw_args, dict):
                item["arguments"] = raw_args
            ctx.emit(OutputItemDoneEvent(type="response.output_item.done", item=item))
        elif isinstance(event, TurnComplete):
            if event.response is not None:
                pass
            # Capture provider-reported usage for the response.completed payload.
            if event.usage is not None:
                ctx.provider_usage = event.usage
        elif isinstance(event, CompactionStarted):
            from omnigent.server.schemas import CompactionInProgressEvent

            ctx.emit(
                CompactionInProgressEvent(
                    type="response.compaction.in_progress",
                )
            )
        elif isinstance(event, CompactionComplete):
            from omnigent.server.schemas import CompactionCompletedEvent

            ctx.emit(
                CompactionCompletedEvent(
                    type="response.compaction.completed",
                    total_tokens=event.token_count,
                    summary=event.summary,
                    summary_model=event.model,
                    compacted_messages=event.compacted_messages,
                )
            )
        # ExecutorError handled by the caller (re-raises so the
        # scaffold can build a response.failed terminal event).

    def _build_error_detail(self, exception: BaseException) -> Any:
        """Map an exception to a semantic code the Omnigent retry allowlist recognizes.

        OmnigentError uses its own ``code``; others go through ``classify_inner_exception``.
        Unknown types fall back to base class (``type(exception).__name__``).
        """
        from omnigent.errors import OmnigentError
        from omnigent.server.schemas import ErrorDetail

        if isinstance(exception, InnerExecutorError):
            return ErrorDetail(
                code=exception.code,
                message=str(exception),
                title=exception.title,
                remediation=exception.remediation,
                undelivered=True if exception.undelivered else None,
            )
        if isinstance(exception, OmnigentError):
            return ErrorDetail(code=exception.code, message=str(exception))

        code = classify_inner_exception(exception)
        if code is not None:
            return ErrorDetail(code=code, message=str(exception))

        return super()._build_error_detail(exception)

    async def on_shutdown(self) -> None:
        """Drain background interrupt tasks, then close the executor session and executor."""
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            self._bg_tasks.clear()
        if self._executor is not None:
            await self._executor.close_session(self._session_key)
            await self._executor.close()
            self._executor = None


def _classify_openai_exception(exception: BaseException) -> str | None:
    """Map an OpenAI SDK exception to a semantic code. Returns ``None`` when not recognized."""
    try:
        import openai
    except ImportError:
        return None

    if isinstance(exception, openai.RateLimitError):
        return "rate_limit_exceeded"
    if isinstance(exception, openai.APITimeoutError):
        return "timeout"
    if isinstance(exception, openai.APIConnectionError):
        return "connection_error"
    if isinstance(exception, openai.InternalServerError):
        return "server_error"
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _classify_claude_sdk_exception(exception: BaseException) -> str | None:
    """Map a ``claude_agent_sdk`` exception to a code; only ``CLIConnectionError`` is retryable."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None

    # CLINotFoundError is a CLIConnectionError subclass — match first to keep it non-retryable.
    if isinstance(exception, claude_agent_sdk.CLINotFoundError):
        return None
    if isinstance(exception, claude_agent_sdk.CLIConnectionError):
        return "connection_error"
    return None


def _classify_httpx_exception(exception: BaseException) -> str | None:
    """Map a raw ``httpx`` exception to a semantic code for transports that don't wrap it."""
    try:
        import httpx
    except ImportError:
        return None

    if isinstance(exception, httpx.TimeoutException):
        return "timeout"
    if isinstance(exception, httpx.ConnectError):
        return "connection_error"
    # NetworkError covers ReadError/WriteError/CloseError — peer closed mid-stream, broken pipe.
    if isinstance(exception, httpx.NetworkError):
        return "connection_error"
    # RemoteProtocolError sits under ProtocolError, not NetworkError — the prior branch misses it.
    if isinstance(exception, httpx.RemoteProtocolError):
        return "connection_error"
    return None


def _classify_anthropic_exception(exception: BaseException) -> str | None:
    """Map an Anthropic SDK exception to a semantic code. Returns ``None`` when not recognized."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None

    if isinstance(exception, anthropic.RateLimitError):
        return "rate_limit_exceeded"
    if isinstance(exception, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exception, anthropic.APIConnectionError):
        return "connection_error"
    if isinstance(exception, anthropic.InternalServerError):
        return "server_error"
    return None


def classify_inner_exception(exception: BaseException) -> str | None:
    """Fan out across per-SDK classifiers; first match wins. Returns ``None`` when unrecognized.

    New classifiers plug in here once. Order matters if SDK hierarchies ever overlap —
    more-specific classifiers should come first.
    """
    for classifier in (
        _classify_openai_exception,
        _classify_anthropic_exception,
        _classify_claude_sdk_exception,
        _classify_httpx_exception,
    ):
        code = classifier(exception)
        if code is not None:
            return code
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _normalize_tool_schemas(
    schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten Chat-Completions ``{"type":"function","function":{...}}`` to inner flat shape.

    Idempotent: schemas with ``"name"`` at top level pass through unchanged.
    """
    flat: list[dict[str, Any]] = []
    for schema in schemas:
        # Flat-form: validate ``name`` is non-empty even on the
        # pass-through branch. The inner SDK's
        # ``_build_mcp_tools`` reads ``schema.get("name")``
        # directly; an entry like ``{"name": ""}`` would slip
        # through and produce the "Tool name cannot be empty"
        # warning + render a phantom MCP tool the LLM can't
        # call. Drop those with the same warning the chat-
        # completions branch uses.
        if "name" in schema:
            name = schema.get("name")
            if not isinstance(name, str) or not name:
                _logger.warning(
                    "skipping flat-form tool schema with empty name: %r",
                    schema,
                )
                continue
            flat.append(schema)
            continue
        function = schema.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            _logger.warning("skipping tool schema with no name: %r", schema)
            continue
        translated: dict[str, Any] = {"name": name}
        description = function.get("description")
        if isinstance(description, str):
            translated["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            translated["parameters"] = parameters
        flat.append(translated)
    return flat


async def _bridge_one_dispatch(
    ctx: TurnContext,
    agent: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
) -> dict[str, Any]:
    """Round-trip one tool call through ``ctx.dispatch_tool``; returns an MCP-compatible dict.

    When ``call_id`` matches the inline observed event's id, BlockStream dedupes into one render.
    """
    import json

    if call_id is None:
        call_id = f"call_{uuid.uuid4().hex[:12]}"
    try:
        output = await ctx.dispatch_tool(
            call_id=call_id,
            name=tool_name,
            arguments=json.dumps(args),
            agent=agent,
        )
    except Exception as exc:
        _logger.exception("dispatch_tool failed for %s", tool_name)
        return {"error": str(exc)}
    # Parse as JSON if possible; fall back to ``{"result": output}`` for non-JSON payloads.
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"result": output}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _extract_last_user_message(
    input_value: str | list[dict[str, Any]],
) -> str:
    """Extract the last user message text for tracing (history shape or single-turn fallback)."""
    if isinstance(input_value, str):
        return input_value
    # Conversation-history shape: find last user message
    last_user_text = ""
    for item in input_value:
        role = item.get("role")
        if role == "user":
            content = item.get("content")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                if parts:
                    last_user_text = "\n".join(parts)
        # Single-turn content-block shape (no role key)
        elif role is None:
            text = item.get("text")
            if isinstance(text, str):
                last_user_text = text
    return last_user_text


def _report_injection(ctx: TurnContext, injection_id: str, *, taken: bool) -> None:
    """Tell the runner whether the running turn took steering injection *injection_id* up."""
    if taken:
        ctx.emit(InjectionConsumedEvent(type="injection.consumed", injection_id=injection_id))
    else:
        ctx.emit(InjectionRefusedEvent(type="injection.refused", injection_id=injection_id))


def _is_text_only(input_value: str | list[dict[str, Any]]) -> bool:
    """Whether an injection is plain text, so a steer carries all of it."""
    if isinstance(input_value, str):
        return True
    return all(
        isinstance(block, dict) and block.get("type") in ("input_text", "text")
        for block in input_value
    )


def _extract_user_text(
    input_value: str | list[dict[str, Any]],
) -> str:
    """Extract concatenated text from an injection input for ``enqueue_session_message``."""
    if isinstance(input_value, str):
        return input_value
    parts: list[str] = []
    for block in input_value:
        text_value = block.get("text")
        if isinstance(text_value, str):
            parts.append(text_value)
    return "\n".join(parts)


def _translate_input_to_messages(
    input_value: str | list[dict[str, Any]],
    *,
    tool_digests: bool = False,
) -> list[Message]:
    """Convert a CreateResponseRequest input to inner Message list.

    Accepts conversation-history shape (role-keyed messages; tool items dropped unless
    *tool_digests*) and single-turn fallback shape (plain string or content blocks, collapsed
    to one user message).
    """
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    history_messages = _extract_role_keyed_messages(input_value, tool_digests=tool_digests)
    if history_messages:
        return history_messages

    content = _normalize_message_content(input_value)
    return [{"role": "user", "content": content}]


def _extract_role_keyed_messages(
    input_value: list[dict[str, Any]],
    *,
    tool_digests: bool = False,
) -> list[Message]:
    """Extract role-keyed message items from an Omnigent input list.

    Tool-call items (function_call, function_call_output, etc.) are skipped — the inner SDK
    reconstructs its own tool state. With *tool_digests* (Super Chat) each call and its result
    become one ``tool``-role message holding a framed digest line instead, so a cold replay
    still says what was done; the role keeps tool output apart from the person's and the
    assistant's words in the ``role: text`` history both the Claude SDK and Pi prompts render.
    Returns an empty list for non-history inputs.
    """
    messages: list[Message] = []
    digests = digest_tool_items(input_value) if tool_digests else {}
    for index, item in enumerate(input_value):
        if not isinstance(item, dict):
            continue
        if index in digests:
            messages.append({"role": "tool", "content": digests[index]})
            continue
        if item.get("type") != "message" or "role" not in item:
            continue
        role = item["role"]
        content = _normalize_message_content(item.get("content"))
        if not content:
            continue
        messages.append({"role": role, "content": content})
    if digests and not any(message["role"] != "tool" for message in messages):
        return []
    return messages


_MULTIMODAL_BLOCK_TYPES: frozenset[str] = frozenset({"input_image", "input_file", "input_audio"})


def _normalize_message_content(
    content: Any,
) -> str | list[dict[str, Any]]:
    """Normalize message content: plain string for text-only; full block list for multimodal."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    has_multimodal = any(
        isinstance(block, dict) and block.get("type") in _MULTIMODAL_BLOCK_TYPES
        for block in content
    )
    if has_multimodal:
        return content

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text_value = block.get("text")
        if isinstance(text_value, str):
            parts.append(text_value)
    return "\n".join(parts)


def _serialize_args(args: dict[str, Any]) -> str:
    """JSON-encode a tool-call arguments dict."""
    import json

    encoded: str = json.dumps(args)
    return encoded


def _serialize_tool_result(event: ToolCallComplete) -> str:
    """Stringify a ToolCallComplete for function_call_output (result preferred, error fallback)."""
    if event.result is not None:
        return _stringify_tool_payload(event.result)
    if event.error is not None:
        return f"[error] {event.error}"
    _logger.warning(
        "ToolCallComplete for %s had neither result nor error",
        event.name,
    )
    return ""


def _stringify_tool_payload(value: Any) -> str:
    """Coerce a result payload to string (str pass-through, content-block join, JSON fallback)."""
    import json

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Anthropic-style content blocks: join text fields; skip non-text (image, tool_use, etc.).
        text_parts: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            block_text = block.get("text")
            if isinstance(block_text, str):
                text_parts.append(block_text)
        if text_parts:
            return "".join(text_parts)
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def _call_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Extract the ``call_id`` string from a metadata dict, or ``None`` if absent."""
    if not metadata:
        return None
    value = metadata.get("call_id")
    if isinstance(value, str):
        return value
    return None
