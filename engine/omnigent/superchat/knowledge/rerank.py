"""LLM rerank of the best search hits, through the engine's model proxy.

The reranking step of the qmd design (``qmd_query``: hybrid retrieval, then a model reorders the
top candidates). Like embeddings, it never holds a key: the host presents its launch token to
``GET /v1/model/rerank`` (which cheap chat model, or why not) and then calls
``POST /v1/model/{provider}/v1/chat/completions``; the model layer resolves the person's (or the
organization's) connection, enforces suspension and budgets, and records the cost. Without a
chat-capable connection :class:`RerankUnavailable` says why and the caller keeps the fused order.
Blocking by design: the handler runs it in a worker thread.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.superchat.knowledge.embedder import (
    REASON_BUDGET,
    REASON_ERROR,
    REASON_NO_CONNECTION,
    REASON_SUSPENDED,
    EmbeddingUnavailable,
    proxy_client,
)

_logger = logging.getLogger(__name__)

#: Characters of each passage the model reads.
PASSAGE_CHARS = 500

_SYSTEM = (
    "You rank passages by how well they answer a search query. Reply with only a JSON array of "
    "the passage numbers, most relevant first, e.g. [3,1,2]. Include every number once."
)


class RerankUnavailable(Exception):
    """The rerank cannot run now; ``reason`` is ``no_connection``, ``suspended``, ``budget`` or
    ``error``."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class Reranker:
    """Interface the indexer uses; :class:`ProxyReranker` is the real one."""

    def rerank(self, query: str, passages: list[str]) -> list[int]:
        """Indices of *passages*, most relevant first.

        :raises RerankUnavailable: when no model can do it now.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class RerankPlan:
    """What the engine answered for ``GET /v1/model/rerank``."""

    provider: str
    model: str


def parse_order(text: str, count: int) -> list[int]:
    """Zero-based indices from the model's ``[3,1,2]`` (one-based) answer; bad entries dropped."""
    match = re.search(r"\[[^\[\]]*\]", text)
    if match is None:
        raise RerankUnavailable(REASON_ERROR, "the model did not return an order")
    try:
        raw = json.loads(match.group(0))
    except ValueError:
        raise RerankUnavailable(REASON_ERROR, "the model did not return an order") from None
    order: list[int] = []
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool) and 1 <= item <= count:
            if item - 1 not in order:
                order.append(item - 1)
    if not order:
        raise RerankUnavailable(REASON_ERROR, "the model did not return an order")
    return order


class ProxyReranker(Reranker):
    """Reranks through the engine's model proxy with the host's launch token."""

    def __init__(
        self,
        server_url: Callable[[], str | None],
        credential: Callable[[], str | None],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._server_url = server_url
        self._credential = credential
        self._transport = transport

    def _client(self) -> httpx.Client:
        try:
            return proxy_client(self._server_url(), self._credential(), self._transport)
        except EmbeddingUnavailable as exc:
            raise RerankUnavailable(exc.reason, str(exc)) from None

    def plan(self) -> RerankPlan:
        """The model that would rerank now.

        :raises RerankUnavailable: with the reason when nothing can.
        """
        try:
            with self._client() as client:
                response = client.get("/v1/model/rerank")
        except httpx.HTTPError:
            raise RerankUnavailable(REASON_ERROR, "the engine could not be reached") from None
        if response.status_code == 401:
            raise RerankUnavailable(REASON_NO_CONNECTION, "not signed in to the model proxy")
        if response.status_code >= 400:
            raise RerankUnavailable(REASON_ERROR, f"engine answered {response.status_code}")
        body: Any = response.json()
        if not isinstance(body, dict) or not body.get("available"):
            reason = body.get("reason") if isinstance(body, dict) else None
            raise RerankUnavailable(reason if isinstance(reason, str) else REASON_NO_CONNECTION)
        return RerankPlan(provider=str(body["provider"]), model=str(body["model"]))

    def rerank(self, query: str, passages: list[str]) -> list[int]:
        plan = self.plan()
        listing = "\n\n".join(f"[{i}] {p[:PASSAGE_CHARS]}" for i, p in enumerate(passages, 1))
        body = {
            "model": plan.model,
            "temperature": 0,
            "max_tokens": 200,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Query: {query}\n\nPassages:\n{listing}"},
            ],
        }
        try:
            with self._client() as client:
                response = client.post(f"/v1/model/{plan.provider}/v1/chat/completions", json=body)
        except httpx.HTTPError as exc:
            _logger.warning("rerank call failed (%s)", type(exc).__name__)
            raise RerankUnavailable(REASON_ERROR, "the model could not be reached") from None
        if response.status_code == 402:
            raise RerankUnavailable(REASON_BUDGET, "the model budget is used up")
        if response.status_code == 403:
            raise RerankUnavailable(REASON_SUSPENDED, "the account is suspended")
        if response.status_code >= 400:
            raise RerankUnavailable(REASON_ERROR, "the model refused the request")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise RerankUnavailable(REASON_ERROR, "the model returned no answer") from None
        return parse_order(str(content), len(passages))
