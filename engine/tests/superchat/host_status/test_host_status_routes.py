"""``GET /sessions/{id}/host``: ready is a connected runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from omnigent.superchat.host_status.routes import register_host_status_routes


class _Conversations:
    def __init__(self, conv: Any) -> None:
        self._conv = conv

    def get_conversation(self, _session_id: str) -> Any:
        return self._conv


class _Tunnels:
    def __init__(self, *runner_ids: str) -> None:
        self._ids = set(runner_ids)

    def get(self, runner_id: str) -> object | None:
        return object() if runner_id in self._ids else None


def _client(conv: Any, tunnels: _Tunnels) -> TestClient:
    router = APIRouter()
    register_host_status_routes(router, conversation_store=_Conversations(conv))  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(router)
    app.state.tunnel_registry = tunnels
    return TestClient(app)


def test_a_connected_runner_is_ready() -> None:
    conv = SimpleNamespace(runner_id="runner_1", host_id=None)
    body = _client(conv, _Tunnels("runner_1")).get("/sessions/conv_1/host").json()
    assert body == {"ready": True, "launch": None}


def test_a_runner_that_is_not_connected_is_not_ready() -> None:
    conv = SimpleNamespace(runner_id="runner_1", host_id=None)
    body = _client(conv, _Tunnels()).get("/sessions/conv_1/host").json()
    assert body == {"ready": False, "launch": None}
