"""Harness fit: a harness's wire picks connections; models are never filtered by name."""

from __future__ import annotations

import base64
import os
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
from omnigent.inference_config import HarnessInferenceBinding, parse_inference_config
from omnigent.onboarding.provider_config import ProviderEntry
from omnigent.server.inference_catalog import SandboxInferenceService
from omnigent.superchat.models.proxy import create_model_proxy_router
from omnigent.superchat.models.store import ModelConnectionStore
from omnigent.superchat.models.upstreams import (
    UPSTREAMS,
    harness_family,
    openrouter_model_id,
    preferred_providers,
)
from omnigent.superchat.vault.store import VAULT_KEY_ENV

from .test_inference_catalog import _state, _transport, credentials  # noqa: F401


def test_providers_that_cannot_serve_the_family_are_excluded() -> None:
    every = ("anthropic", "openrouter", "litellm")
    assert preferred_providers(harness_family("claude-sdk")) == every
    assert preferred_providers(harness_family("pi")) == every
    # A Messages-only upstream cannot serve a harness that speaks Chat Completions.
    assert preferred_providers("gpt") == ("openrouter", "litellm")


def _config(harness: str, **binding) -> dict:
    return {
        "providers": {"gw": {"kind": "gateway", "openai": {"base_url": "https://gw.example/v1"}}},
        "inference": {"harnesses": {harness: {"provider": "gw", **binding}}},
    }


@pytest.mark.parametrize("field", ["model_allowlist", "default_model"])
def test_parse_rejects_a_model_the_harness_cannot_serve(field) -> None:
    value = ["openai/gpt-5"] if field == "model_allowlist" else "openai/gpt-5"
    with pytest.raises(ValueError, match="cannot use model 'openai/gpt-5'"):
        parse_inference_config(_config("claude-sdk", **{field: value}))


def test_parse_keeps_claude_on_claude_multi_on_anything_and_unknown_aliases() -> None:
    assert parse_inference_config(_config("claude-sdk", model_allowlist=["claude-sonnet-5-5"]))
    assert parse_inference_config(_config("claude-sdk", model_allowlist=["gateway/fast"]))
    assert parse_inference_config(_config("pi", model_allowlist=["openai/gpt-5", "claude-x"]))


def test_static_catalog_filters_other_vendors_for_claude_not_for_pi() -> None:
    service = SandboxInferenceService(_state())
    provider = ProviderEntry(name="gw", kind="gateway")
    allow = ("claude-sonnet-5-5", "gpt-5-4", "gateway/fast")
    for harness, expected in (
        ("claude-sdk", ["claude-sonnet-5-5", "gateway/fast"]),
        ("pi", list(allow)),
    ):
        result = service._static_catalog(
            {"provider_label": "x"}, HarnessInferenceBinding("gw", None, allow), provider, harness
        )
        assert [row["id"] for row in result["models"]] == expected


@pytest.mark.asyncio
async def test_discovered_catalog_filters_other_vendors() -> None:
    ids = ("claude-sonnet-5-5", "gpt-5-4", "gateway/fast")
    state = _state(allowed=None, default=None, harness="claude-sdk")
    state.sandbox_config.default.host_config["providers"]["bifrost"]["anthropic"] = {
        "base_url": "https://inference.example",
        "api_key_ref": "env:POD_INFERENCE_KEY",
    }
    snapshot = await SandboxInferenceService(state, transport=_transport(ids)).prepare(
        "docker", "claude-sdk", "alice"
    )
    assert [m["id"] for m in snapshot["catalog"]["models"]] == [
        "claude-sonnet-5-5",
        "gateway/fast",
    ]


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
        ("claude-opus-4-8", "anthropic/claude-opus-4.8"),
        ("claude-sonnet-4-20250514", "anthropic/claude-sonnet-4"),
        ("claude-3-5-sonnet-20241022", "anthropic/claude-3.5-sonnet"),
        ("databricks-claude-haiku-4-5", "anthropic/claude-haiku-4.5"),
        ("anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5"),
        ("deepseek/deepseek-chat", "deepseek/deepseek-chat"),
        ("gpt-5-4", "gpt-5-4"),
    ],
)
def test_openrouter_model_ids(model, expected) -> None:
    assert openrouter_model_id(model) == expected
    assert UPSTREAMS["openrouter"].upstream_model(model) == expected
    assert UPSTREAMS["anthropic"].upstream_model(model) == model


class _Hosts:
    def resolve_launch_token(self, host_id, token):
        owner = {"host-a": "alice", "host-b": "bob"}.get(host_id)
        return SimpleNamespace(user_id=owner) if owner and token == "t" else None


@pytest.fixture
def proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    uri = f"sqlite:///{tmp_path / 'p.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    store = ModelConnectionStore(uri)
    store.put("user", "alice", "anthropic", "sk-ant-api03-ALICE")
    store.put("user", "bob", "openrouter", "sk-or-v1-BOB")
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    app = FastAPI()
    app.state.model_connection_store = store
    app.include_router(
        create_model_proxy_router(_Hosts(), transport=httpx.MockTransport(respond)),  # type: ignore[arg-type]
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"code": str(exc.code)}, status_code=exc.http_status)

    return TestClient(app), seen


def _post(client, provider, cred, body):
    return client.post(f"/v1/model/{provider}/v1/messages", json=body, headers={"x-api-key": cred})


def test_a_model_is_never_refused_by_its_name(proxy) -> None:
    """The proxy forwards whatever model the harness names; the upstream answers for it."""
    client, seen = proxy
    for model in ("team-model", "openai/gpt-5", "claude-sonnet-5-5"):
        assert _post(client, "anthropic", "host-a:t", {"model": model}).status_code == 200
        assert seen[-1].read() == f'{{"model":"{model}"}}'.encode()
    assert _post(client, "anthropic", "host-a:t", {"stream": True}).status_code == 200


def test_openrouter_serves_any_model_and_respells_claude_ids(proxy) -> None:
    client, seen = proxy
    for model, sent in (
        ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
        ("deepseek/deepseek-chat", "deepseek/deepseek-chat"),
        ("gpt-5-4", "gpt-5-4"),
    ):
        assert _post(client, "openrouter", "host-b:t", {"model": model, "n": 1}).status_code == 200
        assert seen[-1].read() == f'{{"model":"{sent}","n":1}}'.encode()
    # Not JSON / no model: forwarded byte for byte.
    client.post(
        "/v1/model/openrouter/v1/messages", content=b"raw", headers={"x-api-key": "host-b:t"}
    )
    assert seen[-1].read() == b"raw"
