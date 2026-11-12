"""A message to a managed-sandbox session is stored and acked before the wake.

Waking or relaunching a managed sandbox takes seconds. The POST must return as
soon as the person's message is persisted; the wake and the forward to the
runner finish afterwards, in order, and a failed wake is the turn's failure.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import omnigent.server.routes._sessions.orchestration as orchestration_mod
import omnigent.server.routes.sessions.routes_events as routes_events_mod
from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import Conversation, ConversationItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes.sessions import create_sessions_router

_SESSION_ID = "conv_early_ack"


class _Store:
    def __init__(self) -> None:
        self.conv = Conversation(
            id=_SESSION_ID,
            created_at=0,
            updated_at=0,
            root_conversation_id=_SESSION_ID,
            title="Titled",
            host_id="host_1",
        )
        self.appended: list[ConversationItem] = []

    def get_conversation(self, session_id: str) -> Conversation:
        return self.conv

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
    """The route wired to a store, a gated wake step and a recording dispatch."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, managed: bool = True) -> None:
        self.store = _Store()
        self.wake_gate = asyncio.Event()
        self.wake_error: Exception | None = None
        self.order: list[str] = []
        self.dispatched: list[Any] = []
        self.failures: list[Any] = []

        async def wake(**_: Any) -> bool:
            self.order.append("wake")
            await self.wake_gate.wait()
            if self.wake_error is not None:
                raise self.wake_error
            return False

        async def dispatch(*args: Any, **kwargs: Any) -> Any:
            self.order.append("dispatch")
            self.dispatched.append(kwargs.get("persisted_item"))
            return SimpleNamespace(item_id="x", pending_id=None, turn=1)

        async def runner(*_: Any, **__: Any) -> Any:
            return MagicMock()

        async def noop(*_: Any, **__: Any) -> bool:
            return False

        async def record_failure(session_id: str, error: Any, store: Any, **_: Any) -> None:
            self.failures.append(error)

        for name, fake in {
            "_maybe_wake_stale_resumable_managed_sandbox": wake,
            "_dispatch_session_event_to_runner": dispatch,
            "_get_runner_client": runner,
            "_ensure_runner_session_initialized": noop,
            "_ensure_runner_relay_ready": noop,
            "_persist_session_status_error_labels": record_failure,
            "_publish_status": lambda *a, **k: None,
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

        @app.exception_handler(OmnigentError)
        async def _handle(_req: Request, exc: OmnigentError) -> JSONResponse:
            return JSONResponse(status_code=exc.http_status, content={"error": str(exc)})

        app.include_router(router)
        provider = "docker" if managed else None
        app.state.host_store = SimpleNamespace(
            get_host=lambda host_id: SimpleNamespace(sandbox_provider=provider)
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def send(self, text: str = "hi") -> httpx.Response:
        return await self.client.post(
            f"/sessions/{_SESSION_ID}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        )

    async def settle(self) -> None:
        await asyncio.gather(*orchestration_mod._deferred_dispatch_tasks)


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch):
    h = _Harness(monkeypatch)
    yield h
    await h.client.aclose()


@pytest.mark.asyncio
async def test_post_acks_with_the_stored_item_while_the_wake_is_still_running(harness):
    # A screenshot's name carries U+202F before "PM"; the stored words have a plain space.
    resp = await asyncio.wait_for(harness.send("see Shot 4.40.50\u202fPM.png"), timeout=5)

    assert resp.status_code == 202
    assert resp.json() == {"queued": True, "item_id": harness.store.appended[0].id}
    assert len(harness.store.appended) == 1
    assert harness.store.appended[0].data.content[0]["text"] == "see Shot 4.40.50 PM.png"
    assert harness.dispatched == []  # the wake has not finished, nothing forwarded yet

    harness.wake_gate.set()
    await harness.settle()
    # The turn starts after the wake, with the item stored at ack time (no second copy).
    assert harness.order == ["wake", "dispatch"]
    assert harness.dispatched == [harness.store.appended[0]]
    assert len(harness.store.appended) == 1


@pytest.mark.asyncio
async def test_a_failed_wake_fails_the_turn_and_keeps_the_message(harness):
    harness.wake_error = OmnigentError("sandbox gone", code=ErrorCode.RUNNER_UNAVAILABLE)

    resp = await asyncio.wait_for(harness.send(), timeout=5)
    harness.wake_gate.set()
    await harness.settle()

    assert resp.status_code == 202
    assert len(harness.store.appended) == 1
    assert harness.dispatched == []
    assert [(f.code, f.message) for f in harness.failures] == [
        (ErrorCode.RUNNER_UNAVAILABLE, "sandbox gone")
    ]
    assert not orchestration_mod.session_has_deferred_dispatch(_SESSION_ID)


@pytest.mark.asyncio
async def test_a_second_message_queues_behind_the_first_and_forwards_in_order(harness):
    first = await asyncio.wait_for(harness.send("one"), timeout=5)
    second = await asyncio.wait_for(harness.send("two"), timeout=5)
    assert first.status_code == second.status_code == 202

    harness.wake_gate.set()
    await harness.settle()

    assert [i.id for i in harness.dispatched] == [i.id for i in harness.store.appended]
    assert len(harness.dispatched) == 2


@pytest.mark.asyncio
async def test_unmanaged_host_still_dispatches_inline(monkeypatch):
    h = _Harness(monkeypatch, managed=False)
    h.wake_gate.set()
    try:
        resp = await asyncio.wait_for(h.send(), timeout=5)
    finally:
        await h.client.aclose()

    assert resp.status_code == 202
    assert h.dispatched == [None]  # dispatch persisted the item itself, before the ack
    assert h.store.appended == []
