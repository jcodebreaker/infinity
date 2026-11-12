"""Endpoint output-token cap discovery and clamping for Pi-native config."""

from __future__ import annotations

import httpx

from omnigent.models import model_catalog


def _transport(status: int, text: str) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=text))


def test_probe_parses_cannot_exceed_format() -> None:
    t = _transport(400, '{"message":"max_tokens (1048576) cannot exceed 65536."}')
    cap = model_catalog.probe_output_token_cap(
        "https://ws/ai-gateway/mlflow/v1", "tok", "system.ai.kimi-k3", transport=t
    )
    assert cap == 65536


def test_probe_parses_max_output_tokens_format() -> None:
    t = _transport(
        400, "max_new_tokens 99999999 cannot be greater than max_output_tokens 25000.\n"
    )
    cap = model_catalog.probe_output_token_cap(
        "https://ws/ai-gateway/mlflow/v1", "tok", "system.ai.gpt-oss-120b", transport=t
    )
    assert cap == 25000


def test_probe_returns_none_when_accepted() -> None:
    t = _transport(200, '{"choices":[]}')
    assert model_catalog.probe_output_token_cap("https://ws/x", "tok", "m", transport=t) is None


def test_probe_returns_none_on_unparseable_error() -> None:
    t = _transport(400, '{"message":"some unrelated validation error"}')
    assert model_catalog.probe_output_token_cap("https://ws/x", "tok", "m", transport=t) is None
