"""Embedding through the engine's model proxy, with a fake upstream (no network, no keys)."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.superchat.knowledge.embedder import (
    BATCH_SIZE,
    EmbeddingUnavailable,
    NoEmbedder,
    ProxyEmbedder,
)

PLAN = {
    "available": True,
    "reason": None,
    "provider": "openrouter",
    "model": "openai/text-embedding-3-small",
    "dimensions": 4,
    "tag": "openrouter:openai/text-embedding-3-small:4",
}


class Engine:
    """A fake engine: the plan route and the proxy's embeddings route."""

    def __init__(self, plan: dict | None = None, status: int = 200) -> None:
        self.plan = plan or PLAN
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/model/embeddings":
            return httpx.Response(200, json=self.plan)
        if request.url.path == "/v1/model/openrouter/v1/embeddings":
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "x"})
            texts = json.loads(request.content)["input"]
            data = [
                {"index": i, "embedding": [float(len(t)), 0, 0, 1]} for i, t in enumerate(texts)
            ]
            # out of order, to prove the embedder sorts by index
            return httpx.Response(200, json={"data": data[::-1], "usage": {"prompt_tokens": 3}})
        return httpx.Response(404)


def embedder(engine: Engine, credential: str | None = "host_1:tok") -> ProxyEmbedder:
    return ProxyEmbedder(
        lambda: "http://engine.test",
        lambda: credential,
        transport=httpx.MockTransport(engine),
    )


def test_embeds_through_the_proxy_with_the_launch_token_and_never_a_key() -> None:
    engine = Engine()
    result = embedder(engine).embed(["a", "bbb"])
    assert result.tag == PLAN["tag"]
    assert result.vectors == [[1.0, 0, 0, 1], [3.0, 0, 0, 1]]
    call = engine.requests[-1]
    assert call.headers["authorization"] == "Bearer host_1:tok"
    body = json.loads(call.content)
    assert body == {"model": PLAN["model"], "input": ["a", "bbb"], "dimensions": 4}


def test_batches_large_inputs_and_caches_the_plan() -> None:
    engine = Engine()
    emb = embedder(engine)
    emb.embed([f"t{i}" for i in range(BATCH_SIZE + 1)])
    emb.embed(["x"])
    paths = [r.url.path for r in engine.requests]
    assert paths.count("/v1/model/embeddings") == 1
    assert paths.count("/v1/model/openrouter/v1/embeddings") == 3


def test_no_connection_is_reported_with_the_engines_reason() -> None:
    engine = Engine({"available": False, "reason": "budget"})
    with pytest.raises(EmbeddingUnavailable) as raised:
        embedder(engine).plan()
    assert raised.value.reason == "budget"


def test_missing_credential_means_no_connection() -> None:
    with pytest.raises(EmbeddingUnavailable) as raised:
        embedder(Engine(), credential=None).plan()
    assert raised.value.reason == "no_connection"


@pytest.mark.parametrize(
    ("status", "reason"), [(402, "budget"), (403, "suspended"), (500, "error")]
)
def test_proxy_refusals_map_to_reasons(status: int, reason: str) -> None:
    with pytest.raises(EmbeddingUnavailable) as raised:
        embedder(Engine(status=status)).embed(["a"])
    assert raised.value.reason == reason


def test_no_embedder_is_always_keyword_only() -> None:
    with pytest.raises(EmbeddingUnavailable):
        NoEmbedder().embed(["a"])
