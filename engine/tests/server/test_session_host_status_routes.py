"""Tests for the session host status route (ready, launch stage).

Offline: no host provider, supervisor or Docker.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.superchat.host_status.routes import register_host_status_routes

pytestmark = pytest.mark.asyncio


def _app(conv: SimpleNamespace) -> FastAPI:
    router = APIRouter(prefix="/v1")
    register_host_status_routes(
        router,
        conversation_store=SimpleNamespace(get_conversation=lambda _s: conv),  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(router)
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_status_has_only_ready_and_launch() -> None:
    async with await _client(_app(SimpleNamespace(runner_id=None))) as c:
        r = await c.get("/v1/sessions/conv_1/host")
    assert r.json() == {"ready": False, "launch": None}


async def test_status_ready_follows_the_runner_tunnel() -> None:
    live: set[str] = set()
    bound = _app(SimpleNamespace(runner_id="runner_1"))
    bound.state.tunnel_registry = SimpleNamespace(get=lambda rid: rid if rid in live else None)
    async with await _client(bound) as c:
        assert (await c.get("/v1/sessions/conv_1/host")).json()["ready"] is False
        live.add("runner_1")
        assert (await c.get("/v1/sessions/conv_1/host")).json()["ready"] is True


async def test_status_reports_the_launch_stage_while_the_host_starts() -> None:
    from omnigent.server.routes.sessions import _session_sandbox_status_cache
    from omnigent.server.schemas import SandboxStatus

    _session_sandbox_status_cache["conv_1"] = SandboxStatus(stage="starting")
    try:
        async with await _client(_app(SimpleNamespace(runner_id=None))) as c:
            body = (await c.get("/v1/sessions/conv_1/host")).json()
    finally:
        _session_sandbox_status_cache.pop("conv_1", None)
    assert body["launch"] == {"stage": "starting", "error": None}
