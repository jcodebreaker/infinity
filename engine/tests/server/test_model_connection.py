"""Model connections: sealed per-user and per-org provider keys, masked on the wire, offline."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.superchat.models.routes import create_model_connection_router
from omnigent.superchat.models.store import (
    ConnectionInputError,
    ModelConnectionStore,
    resolve_model_connection,
)
from omnigent.superchat.models.upstreams import UPSTREAMS, preferred_providers
from omnigent.superchat.vault.store import (
    VAULT_KEY_ENV,
    VaultUnavailableError,
    seal,
    unseal,
)

KEY = "sk-ant-api03-SECRETSECRET-wxyz"
ORG_KEY = "sk-ant-api03-ORGORGORG-abcd"
OR_KEY = "or-key-1234"
BOTH = ("anthropic", "openrouter")


class _Auth:
    """The caller is whoever the X-User header names."""

    def get_user_id(self, request: Request) -> str:
        return request.headers.get("x-user", "u1")


class _Perms:
    """Only ``root`` is an admin."""

    def is_admin(self, user_id: str) -> bool:
        return user_id == "root"


class _Probe:
    """Offline transport: records requests, answers a fixed status or raises."""

    def __init__(self, status: int = 200, error: Exception | None = None) -> None:
        self.status, self.error, self.requests = status, error, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error:
            raise self.error
        return httpx.Response(self.status, json={})


def _app(uri: str, probe: _Probe) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_model_connection_router(
            ModelConnectionStore(uri),
            auth_provider=_Auth(),  # type: ignore[arg-type]
            permission_store=_Perms(),
            transport=httpx.MockTransport(probe),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": {"message": str(exc)}}, status_code=exc.http_status)

    return TestClient(app)


@pytest.fixture
def uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    u = f"sqlite:///{tmp_path / 'c.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(u))
    return u


ME = "/v1/me/model-connections"
ADMIN = "/v1/admin/model-connections"
ROOT = {"x-user": "root"}


def test_seal_aad_binds_scope_owner_and_provider(uri: str) -> None:
    from omnigent.superchat.models.store import seal_key, unseal_key

    token = seal_key("user", "u1", "anthropic", KEY)
    assert KEY not in token
    assert unseal_key("user", "u1", "anthropic", token) == KEY
    # the AAD is "<scope>:<owner_id>|<provider>"
    assert unseal(token, user_id="user:u1", secret_id="anthropic") == KEY
    for scope, owner, provider in (
        ("org", "u1", "anthropic"),
        ("user", "u2", "anthropic"),
        ("user", "u1", "openrouter"),
        ("org", "", "anthropic"),
    ):
        with pytest.raises(VaultUnavailableError):
            unseal_key(scope, owner, provider, token)
    # an org ciphertext cannot be replayed as a user's
    org_token = seal(KEY, user_id="org:", secret_id="anthropic")
    with pytest.raises(VaultUnavailableError):
        unseal_key("user", "", "anthropic", org_token)


def test_fails_closed_without_vault_key(uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VAULT_KEY_ENV)
    client = _app(uri, _Probe())
    r = client.put(f"{ME}/anthropic", json={"api_key": KEY})
    assert r.status_code == 503 and KEY not in r.text
    assert client.get(ME).json()["data"] == []


def test_put_stores_masked_and_get_lists_hint_only(uri: str) -> None:
    probe = _Probe()
    client = _app(uri, probe)
    r = client.put(f"{ME}/anthropic", json={"api_key": KEY, "label": "work"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "anthropic" and body["hint"] == "wxyz"
    assert body["status"] == "valid" and body["validated_at"] > 0
    assert body["label"] == "work" and body["scope"] == "user"
    assert KEY not in r.text
    req = probe.requests[0]
    assert str(req.url) == "https://api.anthropic.com/v1/models"
    assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    got = client.get(ME)
    assert got.json() == {"object": "list", "data": [body]} and KEY not in got.text


def test_probe_uses_the_registry(uri: str) -> None:
    probe = _Probe()
    client = _app(uri, probe)
    client.put(f"{ME}/openrouter", json={"api_key": OR_KEY})
    upstream = UPSTREAMS["openrouter"]
    assert str(probe.requests[-1].url) == upstream.probe_url == "https://openrouter.ai/api/v1/key"
    assert probe.requests[-1].headers["authorization"] == f"Bearer {OR_KEY}"
    assert upstream.auth == "bearer" and upstream.wire_apis == {"messages", "chat"}
    assert UPSTREAMS["anthropic"].wire_apis == {"messages"}


def test_one_key_per_provider_and_put_replaces(uri: str) -> None:
    store = ModelConnectionStore(uri)
    client = _app(uri, _Probe())
    client.put(f"{ME}/anthropic", json={"api_key": KEY})
    client.put(f"{ME}/openrouter", json={"api_key": OR_KEY})
    assert [c["provider"] for c in client.get(ME).json()["data"]] == ["anthropic", "openrouter"]
    client.put(f"{ME}/anthropic", json={"api_key": "sk-ant-NEW-0000"})
    rows = client.get(ME).json()["data"]
    assert len(rows) == 2 and rows[0]["hint"] == "0000"
    assert store.get_plaintext("user", "u1", ("anthropic",)) == ("anthropic", "sk-ant-NEW-0000")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_not_stored(uri: str, status: int) -> None:
    client = _app(uri, _Probe(status))
    r = client.put(f"{ME}/anthropic", json={"api_key": KEY})
    assert r.status_code == 400
    assert "This key was not accepted by Anthropic" in r.text and KEY not in r.text
    assert client.get(ME).json()["data"] == []


def test_network_error_is_distinct(uri: str) -> None:
    client = _app(uri, _Probe(error=httpx.ConnectError(f"boom {KEY}")))
    r = client.put(f"{ME}/anthropic", json={"api_key": KEY})
    assert r.status_code == 502
    assert "Could not reach Anthropic" in r.text and KEY not in r.text
    assert client.get(ME).json()["data"] == []


def test_bad_input(uri: str) -> None:
    client = _app(uri, _Probe())
    assert client.put(f"{ME}/nope", json={"api_key": KEY}).status_code == 400
    r = client.put(f"{ME}/anthropic", json={})
    assert r.status_code == 400
    r = client.put(f"{ME}/anthropic", json={"api_key": KEY, "x": 1})
    assert r.status_code == 422 and KEY not in r.text
    with pytest.raises(ConnectionInputError):
        ModelConnectionStore(uri).put("user", "", "anthropic", KEY)
    with pytest.raises(ConnectionInputError):
        ModelConnectionStore(uri).put("org", "u1", "anthropic", KEY)


def test_delete_and_other_user_isolation(uri: str) -> None:
    client = _app(uri, _Probe())
    client.put(f"{ME}/anthropic", json={"api_key": KEY})
    other = {"x-user": "u2"}
    assert client.get(ME, headers=other).json()["data"] == []
    assert client.delete(f"{ME}/anthropic", headers=other).status_code == 404
    assert len(client.get(ME).json()["data"]) == 1
    assert client.delete(f"{ME}/anthropic").status_code == 204
    assert client.get(ME).json()["data"] == []
    assert client.delete(f"{ME}/anthropic").status_code == 404


def test_admin_routes_need_an_admin(uri: str) -> None:
    client = _app(uri, _Probe())
    assert client.get(ADMIN).status_code == 403
    assert client.put(f"{ADMIN}/anthropic", json={"api_key": ORG_KEY}).status_code == 403
    assert client.delete(f"{ADMIN}/anthropic").status_code == 403
    assert ModelConnectionStore(uri).list("org", "") == []

    r = client.put(f"{ADMIN}/anthropic", json={"api_key": ORG_KEY}, headers=ROOT)
    assert r.status_code == 200 and r.json()["scope"] == "org" and ORG_KEY not in r.text
    assert client.get(ADMIN, headers=ROOT).json()["data"][0]["hint"] == "abcd"
    # the org connection is not the admin's personal one, and users cannot list it
    assert client.get(ME, headers=ROOT).json()["data"] == []
    assert client.get(ME).json()["data"] == []
    assert client.delete(f"{ADMIN}/anthropic", headers=ROOT).status_code == 204
    assert client.delete(f"{ADMIN}/anthropic", headers=ROOT).status_code == 404


def test_resolution_order_and_preference(uri: str) -> None:
    store = ModelConnectionStore(uri)
    assert resolve_model_connection(store, owner_id="u1", preferred=BOTH) is None

    store.put("org", "", "openrouter", OR_KEY)
    org = resolve_model_connection(store, owner_id="u1", preferred=BOTH)
    assert org is not None and (org.provider, org.plaintext, org.scope) == (
        "openrouter",
        OR_KEY,
        "org",
    )

    store.put("user", "u1", "openrouter", "or-user-9999")
    user = resolve_model_connection(store, owner_id="u1", preferred=BOTH)
    assert user is not None and (user.plaintext, user.scope) == ("or-user-9999", "user")
    # another user still falls back to the org's
    other = resolve_model_connection(store, owner_id="u2", preferred=BOTH)
    assert other is not None and other.scope == "org"

    # the user's scope wins over the org's even when the org holds the preferred provider
    store.put("org", "", "anthropic", ORG_KEY)
    mixed = resolve_model_connection(store, owner_id="u1", preferred=BOTH)
    assert mixed is not None and (mixed.provider, mixed.scope) == ("openrouter", "user")

    # the preference is a parameter
    store.put("user", "u1", "anthropic", KEY)
    first = resolve_model_connection(store, owner_id="u1", preferred=BOTH)
    swapped = resolve_model_connection(store, owner_id="u1", preferred=BOTH[::-1])
    assert first is not None and first.provider == "anthropic"
    assert swapped is not None and swapped.provider == "openrouter"
    only = resolve_model_connection(store, owner_id="u2", preferred=("openrouter",))
    assert only is not None and only.provider == "openrouter" and only.scope == "org"
    assert resolve_model_connection(store, owner_id="u1", preferred=("nope",)) is None
    assert "sk-ant" not in repr(first)
    assert preferred_providers("claude") == preferred_providers("multi") == (*BOTH, "litellm")


@pytest.mark.asyncio
async def test_prepare_falls_back_to_the_org_connection(uri: str) -> None:
    from omnigent.server.inference_catalog import SandboxInferenceService
    from tests.server.test_inference_catalog import _byok_state

    state = _byok_state({})
    state.model_connection_store = ModelConnectionStore(uri)
    state.model_connection_store.put("org", "", "anthropic", ORG_KEY)
    snapshot = await SandboxInferenceService(state).prepare("docker", "claude-sdk", "alice")
    assert snapshot is not None and snapshot["catalog"]["status"] == "ready"
    family = snapshot["runtime_config"]["providers"]["byok"]["anthropic"]
    assert family["base_url"] == "https://engine.example/v1/model/anthropic"
    assert ORG_KEY not in str(snapshot)


def test_plaintext_never_in_logs_or_db(uri: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    _app(uri, _Probe(401)).put(f"{ME}/anthropic", json={"api_key": KEY})
    client = _app(uri, _Probe())
    client.put(f"{ME}/anthropic", json={"api_key": KEY})
    client.put(f"{ADMIN}/anthropic", json={"api_key": ORG_KEY}, headers=ROOT)
    client.get(ME)
    assert KEY not in caplog.text and ORG_KEY not in caplog.text
    with get_or_create_engine(uri).connect() as conn:
        rows = conn.execute(sa.text("select * from model_connection")).fetchall()
    assert len(rows) == 2 and KEY not in repr(rows) and ORG_KEY not in repr(rows)
