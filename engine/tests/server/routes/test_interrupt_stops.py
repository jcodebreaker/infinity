"""Stop stops (ADR 0026): the interrupt says whether it landed, holds through a relaunch,
and reaches the running Helpers of the Round it stops, and only those (ADR 0029)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

import omnigent.server.routes._sessions.orchestration as orchestration_mod
import omnigent.server.routes.sessions.routes_events as routes_events_mod
from omnigent.context.labels import CONTEXT_MODE_LABEL, ROUND_LABEL_KEY, SUPERSIDE_CHAT_MODE_VALUE
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import Conversation, ConversationItem
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.superchat.rounds import record
from omnigent.superchat.rounds.store import SqlAlchemyRoundStore

_PARENT = "a" * 32
_CHILD = "b" * 32
_DONE_CHILD = "c" * 32
#: A Helper still working for an earlier Round of the same Conversation.
_OTHER_CHILD = "d" * 32
#: The Conversation's running Round and an earlier one.
_NOW, _OLD = "e" * 32, "f" * 32


def _conv(conv_id: str, **extra: Any) -> Conversation:
    return Conversation(
        id=conv_id,
        created_at=0,
        updated_at=0,
        root_conversation_id=_PARENT,
        title="Titled",
        host_id="host_1",
        **extra,
    )


class _Store:
    def __init__(self) -> None:
        self.convs = {
            _PARENT: _conv(_PARENT),
            _CHILD: _conv(_CHILD, kind="sub_agent", parent_conversation_id=_PARENT),
            _DONE_CHILD: _conv(_DONE_CHILD, kind="sub_agent", parent_conversation_id=_PARENT),
        }
        self.appended: list[ConversationItem] = []
        #: Helpers whose summary reads as working.
        self.busy = {_CHILD}

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.convs.get(session_id)

    def list_conversations(self, *, parent_conversation_id: str, **_: Any) -> Any:
        children = [
            conv
            for conv in self.convs.values()
            if conv.parent_conversation_id == parent_conversation_id
        ]
        return SimpleNamespace(data=children)

    def append(self, session_id: str, items: list[NewConversationItem]) -> list[ConversationItem]:
        stored = [
            ConversationItem(
                id=f"item_{len(self.appended) + i}",
                type=it.type,
                status="completed",
                response_id=it.response_id,
                created_at=1000,
                data=it.data,
            )
            for i, it in enumerate(items)
        ]
        self.appended.extend(stored)
        return stored


class _Harness:
    """The events route with a gated wake, a recording dispatch and a recording runner."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = _Store()
        self.wake_gate = asyncio.Event()
        self.dispatched: list[Any] = []
        self.interrupted: list[str] = []
        self.interrupt_bodies: dict[str, Any] = {}
        # Which sessions have a runner bound (None: no runner at all).
        self.bound: set[str] = set()
        self.statuses: list[tuple[str, str]] = []

        async def wake(**_: Any) -> bool:
            await self.wake_gate.wait()
            return False

        async def dispatch(*args: Any, **kwargs: Any) -> Any:
            self.dispatched.append(kwargs.get("persisted_item"))
            return SimpleNamespace(item_id="x", pending_id=None, turn=1)

        def handler(request: httpx.Request) -> httpx.Response:
            session_id = request.url.path.split("/")[3]
            self.interrupted.append(session_id)
            self.interrupt_bodies[session_id] = json.loads(request.content)
            return httpx.Response(204)

        runner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://r")
        self.runner = runner

        async def get_runner(session_id: str, *_: Any, **__: Any) -> Any:
            return runner if session_id in self.bound else None

        async def noop(*_: Any, **__: Any) -> bool:
            return False

        async def summaries(children: list[Conversation], *_: Any) -> list[Any]:
            # The finished Helper reads as done; the other is still working.
            return [
                SimpleNamespace(
                    id=child.id,
                    model_dump=lambda mode="json", child=child: {
                        "busy": child.id in self.store.busy,
                        "current_task_status": "completed",
                    },
                )
                for child in children
            ]

        for name, fake in {
            "_maybe_wake_stale_resumable_managed_sandbox": wake,
            "_dispatch_session_event_to_runner": dispatch,
            "_get_runner_client": get_runner,
            "_ensure_runner_session_initialized": noop,
            "_ensure_runner_relay_ready": noop,
            "_child_session_summaries_from_conversations": summaries,
            "_publish_status": lambda sid, status, **_: self.statuses.append((sid, status)),
        }.items():
            monkeypatch.setattr(routes_events_mod, name, fake)

        router = create_sessions_router(
            conversation_store=self.store,  # type: ignore[arg-type]
            agent_store=MagicMock(),
            runner_router=None,
            auth_provider=None,
            permission_store=None,
        )
        app = FastAPI()
        app.include_router(router)
        app.state.host_store = SimpleNamespace(
            get_host=lambda host_id: SimpleNamespace(sandbox_provider="docker")
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def send(self) -> httpx.Response:
        return await self.client.post(
            f"/sessions/{_PARENT}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )

    async def stop(self, session_id: str = _PARENT) -> httpx.Response:
        return await self.client.post(f"/sessions/{session_id}/events", json={"type": "interrupt"})


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch):
    h = _Harness(monkeypatch)
    yield h
    await h.client.aclose()
    await h.runner.aclose()


@pytest.mark.asyncio
async def test_interrupt_with_no_runner_and_nothing_waiting_is_not_delivered(harness, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(routes_events_mod, "_logger", logger)
    resp = await harness.stop()

    assert resp.status_code == 202
    assert resp.json()["delivered"] is False
    logger.info.assert_any_call("interrupt session=%s delivered=%s", _PARENT, False)


@pytest.mark.asyncio
async def test_interrupt_during_a_relaunch_cancels_the_deferred_message(harness):
    sent = await harness.send()
    assert sent.status_code == 202
    assert orchestration_mod.session_has_deferred_dispatch(_PARENT)

    resp = await harness.stop()
    harness.wake_gate.set()
    await asyncio.gather(*orchestration_mod._deferred_dispatch_tasks, return_exceptions=True)

    # The stop held: the message is kept but never reaches a runner, and nothing reads as running.
    assert resp.json()["delivered"] is True
    assert harness.dispatched == []
    assert len(harness.store.appended) == 1
    assert not orchestration_mod.session_has_deferred_dispatch(_PARENT)
    assert (_PARENT, "idle") in harness.statuses


@pytest.mark.asyncio
async def test_interrupt_reaches_the_running_helpers(harness):
    """Outside a Round, Stop reaches every running Helper and sends no Stop line."""
    harness.bound = {_PARENT, _CHILD, _DONE_CHILD}

    resp = await harness.stop()

    assert resp.json()["delivered"] is True
    assert sorted(harness.interrupted) == sorted([_PARENT, _CHILD])
    assert harness.interrupt_bodies[_PARENT] == {"type": "interrupt"}


@pytest.mark.asyncio
async def test_stop_ends_its_round_and_leaves_the_other_rounds_helpers_alone(
    harness, db_uri, monkeypatch
):
    """Stop records ``stop.requested`` on the Conversation's running Round, stops that Round's
    Helper, sends the Stop line with the parent's interrupt, and drops the stopped Helper's late
    wake; a Helper still working for an earlier Round keeps working, and its wake still lands."""
    store = SqlAlchemyRoundStore(db_uri)
    monkeypatch.setattr(record, "RECORDER", record.Recorder(store))
    chat = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
    convs = harness.store.convs
    convs[_PARENT].labels = {**chat, ROUND_LABEL_KEY: _NOW}
    convs[_CHILD].labels = {**chat, ROUND_LABEL_KEY: _NOW}
    convs[_CHILD].title = "Uruguay deck"
    convs[_OTHER_CHILD] = _conv(
        _OTHER_CHILD,
        kind="sub_agent",
        parent_conversation_id=_PARENT,
        labels={**chat, ROUND_LABEL_KEY: _OLD},
    )
    harness.store.busy = {_CHILD, _OTHER_CHILD}
    harness.bound = {_PARENT, _CHILD, _OTHER_CHILD}
    # Each Round: the Assistant starts a Helper and ends its turn; the Helper still works.
    for round_id, helper_id, title in (
        (_OLD, _OTHER_CHILD, "Map"),
        (_NOW, _CHILD, "Uruguay deck"),
    ):
        record.RECORDER.record(round_id, _PARENT, "round.opened", "person", text="x")
        record.RECORDER.record(
            round_id, _PARENT, "helper.started", "assistant", helper_id=helper_id, title=title
        )
        record.RECORDER.record(round_id, _PARENT, "turn.ended", "assistant", status="done")

    resp = await harness.stop()

    assert resp.json()["delivered"] is True
    assert sorted(harness.interrupted) == sorted([_PARENT, _CHILD])
    line = "Stopped. 'Uruguay deck' was still working; nothing was saved."
    assert harness.interrupt_bodies[_PARENT] == {"type": "interrupt", "data": {"note": line}}
    assert harness.interrupt_bodies[_CHILD] == {"type": "interrupt"}
    await record.RECORDER.idle()
    assert [e["kind"] for e in store.events(_NOW)][-1] == "stop.requested"
    assert "stop.requested" not in [e["kind"] for e in store.events(_OLD)]

    async def wake(helper_id: str) -> httpx.Response:
        return await harness.client.post(
            f"/sessions/{_PARENT}/events",
            json={
                "type": "message",
                "data": {"role": "user", "is_system_notice": True, "content": []},
                "context": {"helper_id": helper_id},
            },
        )

    dropped = await wake(_CHILD)
    assert dropped.json() == {"queued": False, "delivered": False}
    assert harness.store.appended == []
    landed = await wake(_OTHER_CHILD)
    assert landed.json()["queued"] is True
    assert len(harness.store.appended) == 1
