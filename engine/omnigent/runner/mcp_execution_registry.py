"""Runner-owned MCP executions that survive a server-tunnel replacement.

The Omnigent server reaches local MCP processes through a tunneled runner
request.  A server restart cancels that request, but it must not cancel and
then replay an external tool that may already have side effects.  This
registry shields the actual execution and lets the next server generation
reattach with the same operation id and step. The originating proxy reserves
the operation until its call ends, so completed steps cannot expire mid-retry.
The shield is for the tunnel only: a Stop from the person (:meth:`cancel_turn`)
cancels the execution itself.

A paused Helper (ADR 0027, :meth:`pause`) is held by :meth:`hold`, which the runner awaits at
every tool boundary (``/mcp/execute``, a native tool's policy check, a runner-dispatched tool)
until :meth:`resume`. A pause left unresumed for :data:`PAUSE_LIMIT_S` expires: held calls are
told to stop and the runner's ``on_expired`` stops the Helper through the Stop path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic

from omnigent.util.json_types import JsonObject

_COMPLETED_TTL_S = 300.0
_MAX_COMPLETED = 1024

# Private runner/server protocol fields. The detached error is only actionable
# when the caller can prove it still owns the matching operation locally.
MCP_OPERATION_ID_PARAM = "_omnigent_operation_id"
RUNNER_MCP_EXECUTION_DETACHED_CODE = -32098
RUNNER_MCP_EXECUTION_DETACHED_MESSAGE = "Runner MCP execution detached."
# What a waiter gets for an execution the person stopped.
STOPPED_RESULT = {"error": {"code": -32000, "message": "Tool call stopped."}}
#: How long a pause holds before the Helper is stopped (ADR 0027).
PAUSE_LIMIT_S = 30 * 60.0
#: The ``session.status`` ``blocked_on`` a held Helper shows.
PAUSED_NOTE = "Paused"


class McpExecutionConflict(RuntimeError):
    """An operation id was retried with different execution parameters."""


@dataclass(frozen=True)
class McpExecutionResult:
    """Serializable response produced by one runner MCP execution."""

    status_code: int
    content: JsonObject


@dataclass
class _Execution:
    """One in-flight or recently completed operation step."""

    fingerprint: str
    task: asyncio.Task[McpExecutionResult]
    completed_at: float | None = None


@dataclass
class _Pause:
    """A paused session: its release, whether it expired, and its expiry timer."""

    released: asyncio.Event = field(default_factory=asyncio.Event)
    expired: bool = False
    held: bool = False
    timer: asyncio.Task[None] | None = None
    on_hold: Callable[[str], None] | None = None


def _fingerprint(params: JsonObject) -> str:
    """Return a stable digest for JSON request parameters."""
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class McpExecutionRegistry:
    """Deduplicate runner MCP work across tunneled request lifetimes."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries: dict[tuple[str, str, str], _Execution] = {}
        self._operation_leases: dict[tuple[str, str], int] = {}
        self._released_at: dict[tuple[str, str], float] = {}
        self._pauses: dict[str, _Pause] = {}
        self._sleep = sleep
        self._clock = clock

    def pause(
        self,
        session_id: str,
        *,
        on_hold: Callable[[str], None] | None = None,
        on_expired: Callable[[str], Awaitable[None]] | None = None,
        since: float | None = None,
    ) -> bool:
        """Hold *session_id*'s next tool call until :meth:`resume` (a paused Helper).

        :param since: When the pause began (epoch seconds, the label's value), for a pause
            restored after a runner restart; the expiry counts from it. Now by default.
        :param on_hold: Called once when a call is first held (the runner publishes the status).
        :param on_expired: Awaited when the pause expires unresumed after :data:`PAUSE_LIMIT_S`.
        :returns: ``False`` when it was already paused (the first pause's timer stands).
        """
        if session_id in self._pauses:
            return False
        pause = _Pause(on_hold=on_hold)
        self._pauses[session_id] = pause

        left = PAUSE_LIMIT_S if since is None else PAUSE_LIMIT_S - (self._clock() - since)

        async def _expire() -> None:
            if left > 0:
                await self._sleep(left)
            if self._pauses.get(session_id) is not pause:
                return
            del self._pauses[session_id]
            pause.expired = True
            pause.released.set()
            if on_expired is not None:
                await on_expired(session_id)

        pause.timer = asyncio.create_task(_expire(), name=f"pause-expiry:{session_id}")
        return True

    def resume(self, session_id: str) -> bool:
        """Release a paused session's held calls; the next ones run at once.

        :returns: Whether a call was held (the runner then clears the paused status).
        """
        pause = self._pauses.pop(session_id, None)
        if pause is None:
            return False
        if pause.timer is not None:
            pause.timer.cancel()
        pause.released.set()
        return pause.held

    def is_paused(self, session_id: str) -> bool:
        """Whether *session_id* is paused now."""
        return session_id in self._pauses

    async def hold(self, session_id: str) -> bool:
        """Wait while *session_id* is paused (a tool boundary).

        :returns: ``False`` when the pause expired: the call must not run.
        """
        pause = self._pauses.get(session_id)
        if pause is None:
            return True
        if not pause.held:
            pause.held = True
            if pause.on_hold is not None:
                pause.on_hold(session_id)
        await pause.released.wait()
        return not pause.expired

    def retain_operation(self, session_id: str, operation_id: str) -> None:
        """Keep an operation reattachable while its originating call is live."""
        key = (session_id, operation_id)
        self._operation_leases[key] = self._operation_leases.get(key, 0) + 1
        self._released_at.pop(key, None)
        self._prune()

    def release_operation(self, session_id: str, operation_id: str) -> None:
        """Release one live caller's retention lease."""
        key = (session_id, operation_id)
        leases = self._operation_leases.get(key, 0)
        if leases > 1:
            self._operation_leases[key] = leases - 1
            return
        if leases == 0:
            return
        self._operation_leases.pop(key, None)
        if any(entry_key[:2] == key for entry_key in self._entries):
            self._released_at[key] = monotonic()
        self._prune()

    def has_operation(self, session_id: str, operation_id: str) -> bool:
        """Return whether an operation is reserved or has a retained step."""
        self._prune()
        if (session_id, operation_id) in self._operation_leases:
            return True
        return any(
            stored_session == session_id and stored_operation == operation_id
            for stored_session, stored_operation, _step in self._entries
        )

    async def execute(
        self,
        *,
        session_id: str,
        operation_id: str,
        step: str,
        params: JsonObject,
        run: Callable[[], Awaitable[McpExecutionResult]],
    ) -> McpExecutionResult:
        """Run one logical step once, or attach to its retained task/result."""
        self._prune()
        key = (session_id, operation_id, step)
        fingerprint = _fingerprint(params)
        entry = self._entries.get(key)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                raise McpExecutionConflict(
                    "MCP operation parameters changed while reconnecting; "
                    "refusing to execute the external tool again"
                )
        else:

            async def _run() -> McpExecutionResult:
                return await run()

            task = asyncio.create_task(
                _run(),
                name=f"mcp-execution:{session_id}:{operation_id}:{step}",
            )
            entry = _Execution(fingerprint=fingerprint, task=task)
            self._entries[key] = entry

            def _mark_completed(_task: asyncio.Task[McpExecutionResult]) -> None:
                entry.completed_at = monotonic()

            task.add_done_callback(_mark_completed)

        # The surrounding ASGI dispatch belongs to one tunnel generation.
        # Its cancellation must not propagate into the external operation.
        try:
            return await asyncio.shield(entry.task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if entry.task.cancelled() and not (current is not None and current.cancelling()):
                # The execution itself was stopped (cancel_turn), not this waiter.
                return McpExecutionResult(status_code=200, content=STOPPED_RESULT)
            raise

    def cancel_turn(self, session_id: str) -> int:
        """Cancel *session_id*'s executions still running: the person stopped the turn.

        Cancellation lands at the tool's next await; a tool that cannot be interrupted
        (a worker thread) stops at its next boundary. Waiters get a stopped error result.

        :param session_id: Session/conversation identifier.
        :returns: How many executions were cancelled.
        """
        # A stopped session is no longer paused; its held calls are cancelled below.
        pause = self._pauses.pop(session_id, None)
        if pause is not None and pause.timer is not None:
            pause.timer.cancel()
        cancelled = 0
        for key, entry in self._entries.items():
            if key[0] == session_id and not entry.task.done():
                entry.task.cancel()
                cancelled += 1
        return cancelled

    async def cancel_session(self, session_id: str) -> None:
        """Cancel and forget retained operations for a deleted session."""
        self.resume(session_id)
        for key in tuple(self._operation_leases):
            if key[0] == session_id:
                self._operation_leases.pop(key, None)
        for key in tuple(self._released_at):
            if key[0] == session_id:
                self._released_at.pop(key, None)

        tasks: list[asyncio.Task[McpExecutionResult]] = []
        for key, entry in tuple(self._entries.items()):
            if key[0] != session_id:
                continue
            self._entries.pop(key, None)
            if not entry.task.done():
                entry.task.cancel()
                tasks.append(entry.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune(self) -> None:
        """Bound unleased completed results; never evict live caller work."""
        now = monotonic()
        completed: list[tuple[tuple[str, str, str], _Execution, float]] = []
        for key, entry in tuple(self._entries.items()):
            if entry.completed_at is None:
                continue
            operation_key = key[:2]
            if operation_key in self._operation_leases:
                continue
            retained_at = max(
                entry.completed_at,
                self._released_at.get(operation_key, entry.completed_at),
            )
            if now - retained_at >= _COMPLETED_TTL_S:
                self._entries.pop(key, None)
                continue
            completed.append((key, entry, retained_at))
        if len(completed) > _MAX_COMPLETED:
            completed.sort(key=lambda item: item[2])
            for key, _entry, _retained_at in completed[: len(completed) - _MAX_COMPLETED]:
                self._entries.pop(key, None)

        retained_operations = {key[:2] for key in self._entries}
        for key in tuple(self._released_at):
            if key not in retained_operations:
                self._released_at.pop(key, None)
