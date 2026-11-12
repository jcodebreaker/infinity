"""Vault routes and runner tools: values never echoed, logged or returned to the model."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.vault.handlers import handle_vault_tool
from omnigent.superchat.vault.routes import create_vault_router
from omnigent.superchat.vault.store import VAULT_KEY_ENV, VaultStore
from omnigent.tools import browser_backend
from omnigent.tools.browser_backend import LocalBrowserBackend

PASSWORD = "hunter2-Zq9!"
SESSION = "b" * 32


class _Auth:
    """Every caller is owner u1; a runner bearer is modelled by the X-Runner header."""

    def get_user_id(self, request: Request) -> str:
        return "u1"

    def runner_identity(self, request: Request) -> tuple[str, str] | None:
        runner = request.headers.get("x-runner")
        return ("u1", runner) if runner else None


class _Convs:
    def get_runner_ids(self, ids: list[str]) -> dict[str, str | None]:
        return {SESSION: "r1"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    uri = f"sqlite:///{tmp_path / 'v.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    app = FastAPI()
    app.include_router(
        create_vault_router(
            VaultStore(uri),
            conversation_store=_Convs(),
            auth_provider=_Auth(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    app.state.store = VaultStore(uri)

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    return TestClient(app)


def _body(**extra: Any) -> dict[str, Any]:
    return {
        "name": "acme",
        "site": "https://acme.test",
        "username": "ann",
        "password": PASSWORD,
        **extra,
    }


def test_save_never_echoes_and_list_is_metadata(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    saved = client.post("/v1/me/vault", json=_body())
    listed = client.get("/v1/me/vault")
    assert saved.status_code == 200 and listed.status_code == 200
    assert PASSWORD not in saved.text + listed.text + caplog.text
    entry = listed.json()["entries"][0]
    assert set(entry) == {"id", "name", "site", "username", "created_at", "last_used_at"}
    assert client.delete(f"/v1/me/vault/{entry['id']}").status_code == 204
    assert client.get("/v1/me/vault").json()["entries"] == []


def test_no_key_refuses_save_request_and_fill(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/v1/me/vault", json=_body())
    monkeypatch.delenv(VAULT_KEY_ENV)
    assert client.post("/v1/me/vault", json=_body(name="b")).status_code == 403
    assert (
        client.post(
            "/v1/me/vault/requests",
            json={"session_id": SESSION, "name": "b", "site": "https://b.test"},
        ).status_code
        == 403
    )
    fill = client.post(
        "/v1/me/vault/fill", json={"name": "acme", "field": "password", "session_id": SESSION}
    )
    assert fill.status_code == 403 and PASSWORD not in fill.text


def test_request_card_then_save_marks_saved(client: TestClient) -> None:
    req = client.post(
        "/v1/me/vault/requests",
        json={"session_id": SESSION, "name": "acme", "site": "https://acme.test", "reason": "x"},
    ).json()
    assert client.get(f"/v1/me/vault/requests/{req['id']}").json()["status"] == "pending"
    client.post("/v1/me/vault", json=_body(request_id=req["id"]))
    assert client.get(f"/v1/me/vault/requests/{req['id']}").json()["status"] == "saved"


def test_http_site_rejected(client: TestClient) -> None:
    assert client.post("/v1/me/vault", json=_body(site="http://acme.test")).status_code == 400


class _Client:
    """Stands in for the runner's server client by calling the TestClient."""

    def __init__(self, tc: TestClient) -> None:
        self.tc = tc
        self.posts: list[str] = []

    async def get(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(200, json={"kind": "session", "parent_session_id": None})

    async def post(self, url: str, json: Any = None, **_: Any) -> httpx.Response:
        self.posts.append(url)
        r = self.tc.post(url, json=json, headers={"x-runner": "r1"})
        return httpx.Response(r.status_code, content=r.content)


def _fill(client: TestClient, monkeypatch: pytest.MonkeyPatch, page_reply: dict[str, Any]):
    """Run vault_fill with a fake helper; returns (tool output, argv list, stdin list)."""
    monkeypatch.setenv("OMNIGENT_BROWSER_BACKEND", "local")
    backend = LocalBrowserBackend()
    backend._refs = {0: "e1"}
    argvs: list[list[str]] = []
    stdins: list[bytes | None] = []

    async def fake_run(argv: list[str], *, stdin: bytes | None = None, env: Any = None):
        argvs.append(argv)
        stdins.append(stdin)
        return json.dumps(page_reply).encode(), b""

    monkeypatch.setattr(backend, "_run", fake_run)
    monkeypatch.setattr(browser_backend, "get_local_browser_backend", lambda: backend)
    out = asyncio.run(
        handle_vault_tool(
            HandlerCtx("vault_fill", _Client(client), SESSION),  # type: ignore[arg-type]
            {"name": "acme", "field": "password", "ref": 0},
        )
    )
    return out, argvs, stdins


def test_fill_value_goes_over_stdin_only_and_never_into_the_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client.post("/v1/me/vault", json=_body())
    out, argvs, stdins = _fill(client, monkeypatch, {"ok": True, "tree": f"x {PASSWORD}"})
    assert json.loads(out) == {"ok": True, "result": "filled password"}
    assert PASSWORD not in out + caplog.text + json.dumps(argvs)
    sent = json.loads(stdins[0] or b"{}")["actions"][0]
    assert sent["text"] == PASSWORD and sent["origin"] == "https://acme.test"


def test_origin_mismatch_is_refused_and_error_is_scrubbed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/v1/me/vault", json=_body())
    err = f"The page is not on the site this login was saved for ({PASSWORD})"
    out, _, _ = _fill(client, monkeypatch, {"ok": False, "error": err})
    data = json.loads(out)
    assert data["ok"] is False and "not on the site" in data["error"]
    assert PASSWORD not in out


def test_fill_needs_local_browser_and_helper_cannot_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_BROWSER_BACKEND", raising=False)
    sc = _Client(client)
    out = asyncio.run(
        handle_vault_tool(
            HandlerCtx("vault_fill", sc, SESSION),  # type: ignore[arg-type]
            {"name": "acme", "field": "password", "ref": 0},
        )
    )
    assert "host's browser" in out and sc.posts == []

    async def helper_get(url: str, **_: Any) -> httpx.Response:
        return httpx.Response(200, json={"kind": "sub_agent", "parent_session_id": "p"})

    sc.get = helper_get  # type: ignore[method-assign]
    out = asyncio.run(
        handle_vault_tool(
            HandlerCtx("vault_request_secret", sc, SESSION),  # type: ignore[arg-type]
            {"name": "a", "site": "https://a.test"},
        )
    )
    assert "not available to sub-agent" in out


def _direct_fill(client: TestClient, **headers: str) -> Any:
    return client.post(
        "/v1/me/vault/fill",
        json={"name": "acme", "field": "password", "session_id": SESSION},
        headers=headers,
    )


def test_fill_refused_for_owner_without_runner_bearer_and_audited(client: TestClient) -> None:
    client.post("/v1/me/vault", json=_body())
    resp = _direct_fill(client)
    assert resp.status_code == 403 and PASSWORD not in resp.text
    entry = client.get("/v1/me/vault").json()["entries"][0]
    actions = [a for a, _, _ in client.app.state.store.audit(entry["id"], user_id="u1")]  # type: ignore[attr-defined]
    assert actions == ["create", "refuse"]


def test_fill_refused_for_runner_of_another_session(client: TestClient) -> None:
    client.post("/v1/me/vault", json=_body())
    resp = _direct_fill(client, **{"x-runner": "r2"})
    assert resp.status_code == 403 and PASSWORD not in resp.text
    assert _direct_fill(client, **{"x-runner": "r1"}).json()["value"] == PASSWORD


def test_request_is_single_use_and_expires(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def open_request() -> dict[str, Any]:
        return client.post(
            "/v1/me/vault/requests",
            json={"session_id": SESSION, "name": "acme", "site": "https://acme.test"},
        ).json()

    used = open_request()
    assert client.post("/v1/me/vault", json=_body(request_id=used["id"])).status_code == 200
    assert client.post("/v1/me/vault", json=_body(request_id=used["id"])).status_code == 400
    old = open_request()
    mismatch = open_request()
    from omnigent.superchat.vault import store as vault_store

    real = vault_store.now_epoch
    monkeypatch.setattr(vault_store, "now_epoch", lambda: real() + 16 * 60)
    assert client.get(f"/v1/me/vault/requests/{old['id']}").json()["status"] == "expired"
    assert client.post("/v1/me/vault", json=_body(request_id=old["id"])).status_code == 400
    monkeypatch.setattr(vault_store, "now_epoch", real)
    assert (
        client.post(
            "/v1/me/vault", json=_body(name="other", request_id=mismatch["id"])
        ).status_code
        == 400
    )
