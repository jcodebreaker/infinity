"""The LLM rerank through the engine's model proxy, with a fake upstream (no network, no keys)."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.superchat.knowledge.markdown import parse_page_spec, select_pages, split_pages
from omnigent.superchat.knowledge.rerank import ProxyReranker, RerankUnavailable, parse_order

PLAN = {"available": True, "reason": None, "provider": "openrouter", "model": "openai/gpt-4o-mini"}


class Engine:
    def __init__(self, answer: str = "[2,1]", plan: dict | None = None, status: int = 200) -> None:
        self.answer, self.plan, self.status = answer, plan or PLAN, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/model/rerank":
            return httpx.Response(200, json=self.plan)
        if request.url.path == "/v1/model/openrouter/v1/chat/completions":
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "x"})
            return httpx.Response(200, json={"choices": [{"message": {"content": self.answer}}]})
        return httpx.Response(404)


def reranker(engine: Engine, credential: str | None = "host_1:tok") -> ProxyReranker:
    return ProxyReranker(
        lambda: "http://engine.test", lambda: credential, transport=httpx.MockTransport(engine)
    )


def test_reranks_with_the_launch_token_through_the_proxy() -> None:
    engine = Engine("Here you go: [2, 1]")
    assert reranker(engine).rerank("q", ["first", "second"]) == [1, 0]
    chat = engine.requests[-1]
    assert chat.headers["authorization"] == "Bearer host_1:tok"
    body = json.loads(chat.content)
    assert body["model"] == "openai/gpt-4o-mini" and "[2] second" in body["messages"][1]["content"]


def test_unavailable_reasons_are_the_plans() -> None:
    plan = {"available": False, "reason": "budget"}
    with pytest.raises(RerankUnavailable) as caught:
        reranker(Engine(plan=plan)).rerank("q", ["a", "b"])
    assert caught.value.reason == "budget"
    with pytest.raises(RerankUnavailable) as none:
        reranker(Engine(), credential=None).rerank("q", ["a", "b"])
    assert none.value.reason == "no_connection"
    for status, reason in ((402, "budget"), (403, "suspended"), (500, "error")):
        with pytest.raises(RerankUnavailable) as err:
            reranker(Engine(status=status)).rerank("q", ["a", "b"])
        assert err.value.reason == reason


def test_parse_order_drops_bad_and_repeated_entries() -> None:
    assert parse_order('[3,1,1,9,"x",2]', 3) == [2, 0, 1]
    for junk in ("no order here", "[]", "[9]"):
        with pytest.raises(RerankUnavailable):
            parse_order(junk, 3)


def test_page_helpers() -> None:
    md = "<!-- page 1 -->\n\none\n\n<!-- page 2 -->\n\ntwo\n\n<!-- page 3 -->\n\nthree\n"
    assert split_pages(md) == {1: "one", 2: "two", 3: "three"}
    assert select_pages(md, [3, 1]).startswith("<!-- page 1 -->")
    assert parse_page_spec("1,3-4", 3) == [1, 3]
    assert parse_page_spec([2, "3"], 5) == [2, 3]
    with pytest.raises(ValueError):
        parse_page_spec("two", 3)
