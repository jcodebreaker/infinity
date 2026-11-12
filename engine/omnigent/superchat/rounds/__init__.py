"""Rounds (ADR 0029): what happened to one message from the person, recorded as it happened.

Layout: ``fold`` (the Round view, a pure fold over its events), ``store`` (the ``round_events``
table), ``record`` (the Round rule and the writers, called where the engine sees each fact,
and Stop, which reads its Round back), ``read`` (what reads a Round once its events are stored:
the family stream's ``round.updated``, the Round each transcript message and Activity row is in,
the Rounds still working and the development shadow diff), ``routes`` (the Round reads),
``trace`` (a Round as one OpenTelemetry trace, when spans are exported). A
settled Round's journal entry goes to the host through the relay op :data:`JOURNAL_OP`, which
writes it with :func:`omnigent.runner.memory.sync.record_round`. No tool reads the record;
``checklist`` (the model's own list on a harness without one) is recorded from its calls.
The :class:`~omnigent.superchat.rounds.record.Sweep` job ends the turns a relay lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from omnigent.harnesses.executor import CHECKLIST_TOOL
from omnigent.superchat.feature import (
    RELAY_MARK,
    BackgroundJob,
    Feature,
    HandlerCtx,
    InstallDeps,
    ToolManagerCtx,
    is_helper,
    is_super_chat,
)
from omnigent.superchat.rounds.checklist import ChecklistTool, handle_checklist

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.superchat.rounds.store import RoundEvent
    from omnigent.tools.base import Tool

_logger = logging.getLogger(__name__)

#: The relay op that journals a settled Round in the host; never offered to a model.
JOURNAL_OP = "round_journal"
JOURNAL_TIMEOUT_S = 10.0


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """``checklist`` for the Conversation itself, never a Helper."""
    return [ChecklistTool()] if is_super_chat(labels) else []


async def _journal_round(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """Runner side of :data:`JOURNAL_OP`: write the entry into the Conversation's journal."""
    from omnigent.runner.memory.sync import record_round

    if not ctx.conversation_id or is_helper(ctx.labels):
        return json.dumps({"error": "a Round is journaled by its Conversation"})
    return json.dumps({"written": await record_round(ctx.conversation_id, args)})


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from fastapi import APIRouter

    from omnigent.server.routes._sessions.common import get_server_runner_router
    from omnigent.server.routes._sessions.helpers import (
        ON_TURN_FAILED,
        _get_runner_client,
        _session_status_from_cache,
    )
    from omnigent.server.routes._sessions.orchestration import ON_SESSION_DELETED
    from omnigent.superchat.activity.derive import ROUND_IDS, ROUND_VIEWS
    from omnigent.superchat.rounds import record
    from omnigent.superchat.rounds.fold import fold
    from omnigent.superchat.rounds.read import Announcer, Shadow, item_rounds, shadow_on
    from omnigent.superchat.rounds.routes import register_round_routes
    from omnigent.superchat.rounds.store import SqlAlchemyRoundStore

    if deps.storage_location is None:
        return

    async def journal(conversation_id: str, entry: dict[str, Any]) -> None:
        """Hand the entry to the Conversation's runner when it is connected; never wakes it."""
        client = await _get_runner_client(
            conversation_id, deps.runner_router or get_server_runner_router()
        )
        if client is None:
            _logger.info("round %s not journaled: no runner", entry["round_id"])
            return
        resp = await client.post(
            f"/v1/sessions/{conversation_id}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": JOURNAL_OP, "arguments": entry},
                RELAY_MARK: True,
            },
            timeout=JOURNAL_TIMEOUT_S,
        )
        if resp.status_code >= 400 or "error" in resp.json():
            _logger.warning("round %s not journaled: %s", entry["round_id"], resp.text[:300])

    store = SqlAlchemyRoundStore(deps.storage_location)
    readers = [Announcer(store)]
    if shadow_on():
        readers.append(Shadow(store, deps.conversation_store))

    def written(batch: Sequence[RoundEvent]) -> None:
        for reader in readers:
            reader.written(batch)

    record.RECORDER = record.Recorder(store, on_written=written, on_settled=journal)
    ROUND_IDS["rounds"] = lambda conversation_id, item_ids: item_rounds(
        store, deps.conversation_store, conversation_id, item_ids
    )

    def views(conversation_id: str, round_ids: list[str]) -> dict[str, dict[str, Any]]:
        read = store.events_of(conversation_id, round_ids)
        return {r: fold(events, r) for r, events in read.items()}

    ROUND_VIEWS["rounds"] = views
    ON_SESSION_DELETED["rounds"] = store.delete_conversation

    async def turn_failed(session_id: str, reason: str) -> None:
        await record.turn_failed(deps.conversation_store, session_id, reason)

    ON_TURN_FAILED["rounds"] = turn_failed

    async def idle(session_id: str) -> bool:
        """Whether the session runs no turn, as its relay last reported."""
        conv = await asyncio.to_thread(deps.conversation_store.get_conversation, session_id)
        if conv is None:
            return True
        return _session_status_from_cache(session_id, conv.live_status) != "running"

    app.state.round_sweep = record.Sweep(store, deps.conversation_store, idle)
    router = APIRouter()
    register_round_routes(
        router,
        store=store,
        conversation_store=deps.conversation_store,
        auth_provider=deps.auth_provider,
        permission_store=deps.permission_store,
    )
    app.include_router(router, prefix="/v1", tags=["rounds"])


def _jobs(app: FastAPI) -> list[BackgroundJob]:
    sweep = getattr(app.state, "round_sweep", None)
    return [sweep] if sweep is not None else []


FEATURE = Feature(
    name="rounds",
    tools=_tools,
    handlers={JOURNAL_OP: _journal_round, CHECKLIST_TOOL: handle_checklist},
    relay_ops=frozenset({JOURNAL_OP}),
    install=_install,
    jobs=_jobs,
)
