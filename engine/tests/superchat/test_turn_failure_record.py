"""A turn that fails before reaching a runner leaves an error item and a ``turn.done failed``."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.runtime import session_stream
from omnigent.server.routes._sessions.helpers import _record_turn_failure
from omnigent.server.routes._sessions.orchestration import schedule_deferred_message_dispatch
from omnigent.server.schemas import ErrorDetail
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.family.stream import _derive
from omnigent.superchat.transcript.routes import register_transcript_routes

_MODE = {"omnigent.context.mode": "superside-chat"}
_LAUNCH_FAILURE = "The session's managed sandbox failed to launch: did not come online within 120s"


def _user(text: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="r1",
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


async def _transcript(store: SqlAlchemyConversationStore, session_id: str) -> list[dict]:
    app = FastAPI()
    router = APIRouter()
    register_transcript_routes(router, conversation_store=store)
    app.include_router(router, prefix="/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        return (await http.get(f"/v1/sessions/{session_id}/transcript")).json()["data"]


async def _fail_deferred_dispatch(store: SqlAlchemyConversationStore, session_id: str) -> None:
    async def dispatch() -> None:
        raise OmnigentError(_LAUNCH_FAILURE, code="runner_unavailable")

    async def on_error(exc: Exception) -> None:
        failure = ErrorDetail(code=getattr(exc, "code", "dispatch_failed"), message=str(exc))
        await _record_turn_failure(store, session_id, failure, failure_origin="runner_unavailable")

    schedule_deferred_message_dispatch(session_id, dispatch, on_error)
    while session_id in _pending():
        await asyncio.sleep(0.01)


def _pending() -> dict:
    from omnigent.server.routes._sessions import orchestration

    return orchestration._deferred_dispatch_pending


def _errors(store: SqlAlchemyConversationStore, session_id: str) -> list:
    return [i for i in store.list_items(session_id, limit=50).data if i.type == "error"]


async def test_failed_deferred_dispatch_stores_one_sandbox_error_and_ends_the_turn(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    chat = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.append(chat.id, [_user("hello")])
    seen: list[dict] = []

    async def listen() -> None:
        async for event in session_stream.subscribe(chat.id):
            seen.append(event)
            if event.get("type") == "response.failed":
                return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await _fail_deferred_dispatch(conversation_store, chat.id)
    await asyncio.wait_for(listener, timeout=2)

    (error,) = _errors(conversation_store, chat.id)
    assert error.data.code == "sandbox_unavailable"
    messages = await _transcript(conversation_store, chat.id)
    assert messages[-1]["blocks"] == [{"type": "error", "code": "sandbox_unavailable"}]
    failed = next(e for e in seen if e["type"] == "response.failed")
    assert _derive(chat.id, chat.id, failed) == {
        "type": "turn.done",
        "chat_id": chat.id,
        "status": "failed",
    }


async def test_repeat_failure_without_a_new_message_is_deduplicated(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    chat = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.append(chat.id, [_user("hello")])
    await _fail_deferred_dispatch(conversation_store, chat.id)
    await _fail_deferred_dispatch(conversation_store, chat.id)
    assert len(_errors(conversation_store, chat.id)) == 1

    conversation_store.append(chat.id, [_user("again")])
    await _fail_deferred_dispatch(conversation_store, chat.id)
    assert len(_errors(conversation_store, chat.id)) == 2


@pytest.mark.parametrize("code", ["runner_unavailable", "runner_disconnected"])
async def test_runner_codes_read_as_sandbox_unavailable(
    conversation_store: SqlAlchemyConversationStore, code: str
) -> None:
    chat = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    await _record_turn_failure(
        conversation_store, chat.id, ErrorDetail(code=code, message="x"), failure_origin="t"
    )
    (error,) = _errors(conversation_store, chat.id)
    assert error.data.code == "sandbox_unavailable"
