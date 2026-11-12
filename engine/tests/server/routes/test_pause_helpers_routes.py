"""Pause and resume a reply's Helpers (ADR 0027): labels, runner events, never the Conversation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

import omnigent.server.routes.sessions.routes_events as routes_events_mod
from omnigent.context.labels import PAUSED_LABEL_KEY
from omnigent.entities.conversation import Conversation
from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions import create_sessions_router

_PARENT = "conv_pause_parent"
_LIVE = "conv_pause_live"
_LIVE_2 = "conv_pause_live_2"
_DONE = "conv_pause_done"


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
        child = {"kind": "sub_agent", "parent_conversation_id": _PARENT}
        self.convs = {
            _PARENT: _conv(_PARENT),
            _LIVE: _conv(_LIVE, **child),
            _LIVE_2: _conv(_LIVE_2, **child),
            _DONE: _conv(_DONE, **child),
        }

    def get_conversation(self, session_id: str) -> Conversation | None:
        return self.convs.get(session_id)

    def list_conversations(self, *, parent_conversation_id: str, **_: Any) -> Any:
        return SimpleNamespace(
            data=[
                c
                for c in self.convs.values()
                if c.parent_conversation_id == parent_conversation_id
            ]
        )

    def set_labels(self, conversation_id: str, updates: dict[str, str], **_: Any) -> None:
        conv = self.convs[conversation_id]
        conv.labels = {**(conv.labels or {}), **updates}


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch):
    store = _Store()
    events: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        events.append((request.url.path.split("/")[3], json.loads(request.content)["type"]))
        return httpx.Response(204)

    runner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://r")

    unbound: set[str] = set()

    async def get_runner(session_id: str, *_: Any, **__: Any) -> Any:
        return None if session_id in unbound else runner

    async def summaries(children: list[Conversation], *_: Any) -> list[Any]:
        return [
            SimpleNamespace(
                id=child.id,
                model_dump=lambda mode="json", child=child: {
                    "busy": child.id != _DONE,
                    "current_task_status": "completed",
                },
            )
            for child in children
        ]

    monkeypatch.setattr(routes_events_mod, "_get_runner_client", get_runner)
    monkeypatch.setattr(
        routes_events_mod, "_child_session_summaries_from_conversations", summaries
    )
    router = create_sessions_router(
        conversation_store=store,  # type: ignore[arg-type]
        agent_store=MagicMock(),
        runner_router=None,
        auth_provider=None,
        permission_store=None,
    )
    app = FastAPI()
    app.include_router(router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    yield SimpleNamespace(client=client, store=store, events=events, unbound=unbound)
    await client.aclose()
    await runner.aclose()


def _paused(store: _Store, conv_id: str) -> str | None:
    return (store.convs[conv_id].labels or {}).get(PAUSED_LABEL_KEY)


@pytest.mark.asyncio
async def test_pause_holds_every_live_helper_and_never_the_conversation(harness) -> None:
    resp = await harness.client.post(f"/sessions/{_PARENT}/helpers/pause")

    assert resp.status_code == 200
    assert sorted(resp.json()["paused"]) == sorted([_LIVE, _LIVE_2])
    assert sorted(harness.events) == sorted([(_LIVE, "pause"), (_LIVE_2, "pause")])
    assert _paused(harness.store, _LIVE) and _paused(harness.store, _LIVE_2)
    assert _paused(harness.store, _DONE) is None
    assert _paused(harness.store, _PARENT) is None


@pytest.mark.asyncio
async def test_named_helpers_only_and_the_conversation_id_is_ignored(harness) -> None:
    resp = await harness.client.post(
        f"/sessions/{_PARENT}/helpers/pause", json={"helper_ids": [_LIVE, _PARENT, _DONE]}
    )

    assert resp.json() == {"paused": [_LIVE]}
    assert harness.events == [(_LIVE, "pause")]


@pytest.mark.asyncio
async def test_resume_releases_the_paused_ones(harness) -> None:
    await harness.client.post(f"/sessions/{_PARENT}/helpers/pause", json={"helper_ids": [_LIVE]})
    resp = await harness.client.post(f"/sessions/{_PARENT}/helpers/resume", json={})

    assert resp.json() == {"resumed": [_LIVE]}
    assert harness.events[-1] == (_LIVE, "resume")
    assert _paused(harness.store, _LIVE) == ""


@pytest.mark.asyncio
async def test_a_helper_is_not_a_conversation_to_pause_from(harness) -> None:
    with pytest.raises(OmnigentError, match="conversation's helpers"):
        await harness.client.post(f"/sessions/{_LIVE}/helpers/pause")
    assert harness.events == []


@pytest.mark.asyncio
async def test_no_label_without_a_runner_to_hold_it_and_resume_clears_it_anyway(harness) -> None:
    harness.unbound.add(_LIVE_2)
    resp = await harness.client.post(f"/sessions/{_PARENT}/helpers/pause")

    assert resp.json() == {"paused": [_LIVE]}
    assert _paused(harness.store, _LIVE_2) is None

    harness.unbound.add(_LIVE)
    resumed = await harness.client.post(f"/sessions/{_PARENT}/helpers/resume")
    # A restarted runner must not pause it again from a stale label.
    assert resumed.json() == {"resumed": [_LIVE]}
    assert _paused(harness.store, _LIVE) == ""
