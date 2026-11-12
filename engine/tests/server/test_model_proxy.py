"""Engine model proxy: the owner's key is injected server-side, never visible to the caller."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.host import model_credential as host_side
from omnigent.superchat.models.proxy import create_model_proxy_router
from omnigent.superchat.models.store import ModelConnectionStore
from omnigent.superchat.vault.store import VAULT_KEY_ENV

ALICE_KEY = "sk-ant-api03-ALICE-SECRET-1111"
BOB_KEY = "sk-or-v1-BOB-SECRET-2222"
ORG_KEY = "sk-ant-api03-ORG-SECRET-3333"
CRED = "host-a:tok-a"


@dataclass
class _Managed:
    user_id: str


class _Hosts:
    """host-a/tok-a -> alice, host-b/tok-b -> bob, host-c/tok-c -> carol (org key only)."""

    _rows = {
        ("host-a", "tok-a"): "alice",
        ("host-b", "tok-b"): "bob",
        ("host-c", "tok-c"): "carol",
    }

    def resolve_launch_token(self, host_id: str, token: str) -> _Managed | None:
        owner = self._rows.get((host_id, token))
        return _Managed(owner) if owner else None


class _Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        for chunk in (b"event: a\n\n", b"event: b\n\n", b"event: c\n\n"):
            yield chunk


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModelConnectionStore:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    uri = f"sqlite:///{tmp_path / 'p.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    s = ModelConnectionStore(uri)
    s.put("user", "alice", "anthropic", ALICE_KEY)
    s.put("user", "bob", "openrouter", BOB_KEY)
    s.put("org", "", "anthropic", ORG_KEY)
    return s


def _client(store, upstream: list[httpx.Request], *, stream: bool = False) -> TestClient:
    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        if stream:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=_Chunks()
            )
        return httpx.Response(
            200, json={"ok": True}, headers={"request-id": "req_1", "set-cookie": "x=1"}
        )

    app = FastAPI()
    app.state.model_connection_store = store
    app.include_router(
        create_model_proxy_router(_Hosts(), transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"code": str(exc.code), "message": str(exc)}, status_code=exc.http_status
        )

    return TestClient(app)


def test_injects_owner_key_and_strips_caller_auth(store) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(
        "/v1/model/anthropic/v1/messages?beta=true",
        json={"model": "claude-sonnet-5-5"},
        headers={
            "x-api-key": CRED,
            "authorization": "Bearer other",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "x",
        },
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["request-id"] == "req_1" and "set-cookie" not in r.headers
    (req,) = seen
    assert str(req.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert req.headers["x-api-key"] == ALICE_KEY
    assert "authorization" not in req.headers and CRED not in str(req.headers)
    assert (
        req.headers["anthropic-version"] == "2023-06-01" and req.headers["anthropic-beta"] == "x"
    )
    assert req.read() == b'{"model":"claude-sonnet-5-5"}'


def test_openrouter_uses_bearer_and_api_base(store) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(
        "/v1/model/openrouter/v1/messages",
        json={},
        headers={"authorization": "Bearer host-b:tok-b"},
    )
    assert r.status_code == 200
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/messages"
    assert seen[0].headers["authorization"] == f"Bearer {BOB_KEY}"
    assert "x-api-key" not in seen[0].headers


@pytest.mark.parametrize("cred", ["host-a:wrong", "host-zzz:tok-a", "tok-a", ""])
def test_bad_or_foreign_token_is_rejected_without_upstream_call(store, cred) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(
        "/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": cred}
    )
    assert r.status_code == 401 and seen == []


def test_owner_cannot_use_another_owners_key(store) -> None:
    seen: list[httpx.Request] = []
    client = _client(store, seen)
    # bob's token on bob's provider with no org key for it: alice's key is never used
    r = client.post(
        "/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": "host-b:tok-b"}
    )
    # bob has no anthropic key of his own, so the org's anthropic key serves him, never alice's
    assert r.status_code == 200
    assert seen[0].headers["x-api-key"] == ORG_KEY and ALICE_KEY not in str(seen[0].headers)


def test_org_connection_serves_a_user_without_a_key(store) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(
        "/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": "host-c:tok-c"}
    )
    assert r.status_code == 200
    assert seen[0].headers["x-api-key"] == ORG_KEY


def test_users_own_key_wins_over_the_org_key(store) -> None:
    seen: list[httpx.Request] = []
    _client(store, seen).post(
        "/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": CRED}
    )
    assert seen[0].headers["x-api-key"] == ALICE_KEY


def test_provider_without_any_connection_is_model_key_required(store) -> None:
    seen: list[httpx.Request] = []
    # carol has only the org's anthropic key: openrouter has none
    r = _client(store, seen).post(
        "/v1/model/openrouter/v1/messages", json={}, headers={"x-api-key": "host-c:tok-c"}
    )
    assert r.status_code == 412
    assert r.json() == {
        "code": "model_key_required",
        "message": "Add a model API key in Settings to use Superchat.",
    }
    assert seen == []


@pytest.mark.parametrize(
    "path", ["anthropic/oauth/token", "anthropic/v1/../admin", "evil/v1/messages"]
)
def test_only_v1_paths_on_known_providers(store, path) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(f"/v1/model/{path}", json={}, headers={"x-api-key": CRED})
    assert r.status_code == 404 and seen == []


def test_streams_chunks_through(store) -> None:
    seen: list[httpx.Request] = []
    with _client(store, seen, stream=True).stream(
        "POST",
        "/v1/model/anthropic/v1/messages",
        json={"stream": True},
        headers={"x-api-key": CRED},
    ) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert body == b"event: a\n\nevent: b\n\nevent: c\n\n"


def test_upstream_unreachable_is_502(store) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.headers['x-api-key']}")

    app = FastAPI()
    app.state.model_connection_store = store
    app.include_router(
        create_model_proxy_router(_Hosts(), transport=httpx.MockTransport(boom)),  # type: ignore[arg-type]
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"code": str(exc.code), "message": str(exc)}, status_code=exc.http_status
        )

    r = TestClient(app).post(
        "/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": CRED}
    )
    assert r.status_code == 502 and r.json()["code"] == "model_provider_unreachable"
    assert ALICE_KEY not in r.text


def test_keys_and_credentials_never_reach_logs(store, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client(store, [])
    client.post(
        "/v1/model/anthropic/v1/messages", json={"secret": "BODY"}, headers={"x-api-key": CRED}
    )
    client.post("/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": "host-c:tok-c"})
    client.post("/v1/model/anthropic/v1/messages", json={}, headers={"x-api-key": "host-a:bad"})
    for secret in (ALICE_KEY, BOB_KEY, ORG_KEY, "tok-a", "tok-c", "BODY"):
        assert secret not in caplog.text


def test_in_host_auth_command_prints_only_the_launch_credential(tmp_path, monkeypatch) -> None:
    sidecar = tmp_path / "side.json"
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "tok-a")
    assert host_side.configure_host_model("host-a", sidecar) is True
    assert oct(sidecar.stat().st_mode & 0o777) == "0o600"
    assert host_side.read_credential(sidecar) == "host-a:tok-a"
    monkeypatch.delenv("IS_SANDBOX")
    assert host_side.configure_host_model("host-a", tmp_path / "other.json") is False
    assert host_side.read_credential(tmp_path / "missing.json") is None


def test_embedding_plan_route_answers_for_the_hosts_owner(store, monkeypatch) -> None:
    from omnigent.superchat.models import embeddings as emb

    seen: list[httpx.Request] = []
    client = _client(store, seen)
    layer = emb.ModelEmbeddings(store, emb.EmbeddingSettingStore(store.storage_location))
    client.app.state.model_embeddings = layer  # type: ignore[attr-defined]
    assert client.get("/v1/model/embeddings").status_code == 401
    # alice only has an Anthropic key: no embedding-capable connection
    no = client.get("/v1/model/embeddings", headers={"x-api-key": CRED})
    assert no.json() == {"available": False, "reason": "no_connection"}
    yes = client.get("/v1/model/embeddings", headers={"x-api-key": "host-b:tok-b"}).json()
    assert yes["available"] and yes["provider"] == "openrouter"
    assert yes["tag"].startswith("openrouter:") and not seen


def test_rerank_plan_uses_the_owners_light_model_only_with_a_chat_connection(store) -> None:
    from omnigent.superchat.models import embeddings as emb
    from omnigent.superchat.models.rerank import ModelRerank

    seen: list[httpx.Request] = []
    client = _client(store, seen)
    layer = emb.ModelEmbeddings(store, emb.EmbeddingSettingStore(store.storage_location))
    picked: list[str | None] = [None]
    asked: list[str | None] = []

    async def light_model(owner: str | None) -> str | None:
        asked.append(owner)
        return picked[0]

    client.app.state.model_rerank = ModelRerank(store, layer)  # type: ignore[attr-defined]
    client.app.state.model_background = SimpleNamespace(light_model=light_model)  # type: ignore[attr-defined]
    assert client.get("/v1/model/rerank").status_code == 401
    # alice only has an Anthropic key (Messages API, no Chat Completions): no rerank
    no = client.get("/v1/model/rerank", headers={"x-api-key": CRED})
    assert no.json() == {"available": False, "reason": "no_connection"}
    # bob's OpenRouter key speaks Chat Completions, but he has no Light model: none guessed
    unpicked = client.get("/v1/model/rerank", headers={"x-api-key": "host-b:tok-b"}).json()
    assert unpicked == {"available": False, "reason": "no_model"}
    picked[0] = "team-fast"
    yes = client.get("/v1/model/rerank", headers={"x-api-key": "host-b:tok-b"}).json()
    assert yes == {
        "available": True,
        "reason": None,
        "provider": "openrouter",
        "model": "team-fast",
    }
    assert asked[-1] == "bob"
    assert not seen


def test_embeddings_call_is_forwarded_with_the_owners_key(store) -> None:
    seen: list[httpx.Request] = []
    r = _client(store, seen).post(
        "/v1/model/openrouter/v1/embeddings",
        json={"model": "openai/text-embedding-3-small", "input": ["a"]},
        headers={"authorization": "Bearer host-b:tok-b"},
    )
    assert r.status_code == 200
    (req,) = seen
    assert str(req.url) == "https://openrouter.ai/api/v1/embeddings"
    assert req.headers["authorization"] == f"Bearer {BOB_KEY}"
