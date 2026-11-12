"""Embeddings for the host's index, made through the engine's model proxy.

The host never holds a provider key. It presents its own launch token (the credential
:mod:`omnigent.host.model_credential` keeps) to two engine routes:

* ``GET /v1/model/embeddings``: whether the owner can embed, with which provider and model, or why
  not (no connection, suspended, budget used up). Resolution, budgets and the choice of model
  are the
  model layer's (:class:`omnigent.superchat.models.embeddings.ModelEmbeddings`).
* ``POST /v1/model/{provider}/v1/embeddings``: the call itself, an OpenAI-compatible request the
  proxy forwards with the owner's key and whose cost it adds to the owner's daily rollup.

Without an embedding-capable connection the index stays keyword-only and says why; there is no
operator key to fall back to. Blocking by design: the indexer runs in its own thread.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

#: Texts per request (providers cap a request at 2048 inputs; this keeps bodies small).
BATCH_SIZE = 64
#: How long a plan (availability, model) is trusted before the engine is asked again.
PLAN_TTL_SECONDS = 60.0
_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)

REASON_NO_CONNECTION = "no_connection"
REASON_SUSPENDED = "suspended"
REASON_BUDGET = "budget"
REASON_ERROR = "error"


class EmbeddingUnavailable(Exception):
    """Embeddings cannot be made now; ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class EmbeddingPlan:
    """What the engine answered for ``GET /v1/model/embeddings``.

    :param provider: Upstream the call goes to (the ``{provider}`` of the proxy route).
    :param model: Model id as the provider spells it.
    :param dimensions: Vector size to request, or ``None`` for the model's own.
    :param tag: Identifies model and size (``provider:model:dims``); stored beside every vector.
    """

    provider: str
    model: str
    dimensions: int | None
    tag: str


@dataclass(frozen=True)
class Embedded:
    """Vectors for the texts asked, in order, and the tag they carry."""

    vectors: list[list[float]]
    tag: str


class Embedder:
    """Interface the indexer uses; :class:`ProxyEmbedder` is the real one."""

    def plan(self) -> EmbeddingPlan:
        """The model that would embed now.

        :raises EmbeddingUnavailable: with the reason when nothing can.
        """
        raise NotImplementedError

    def embed(self, texts: list[str]) -> Embedded:
        """Embed *texts* (any number; batched here).

        :raises EmbeddingUnavailable: when the call cannot be made.
        """
        raise NotImplementedError


class NoEmbedder(Embedder):
    """Keyword-only: used when the host cannot reach the model proxy at all."""

    def __init__(self, reason: str = REASON_NO_CONNECTION) -> None:
        self._reason = reason

    def plan(self) -> EmbeddingPlan:
        raise EmbeddingUnavailable(self._reason)

    def embed(self, texts: list[str]) -> Embedded:  # noqa: ARG002
        raise EmbeddingUnavailable(self._reason)


def proxy_client(
    base: str | None, credential: str | None, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An HTTP client for the engine's model proxy, signed with the host's launch token.

    :raises EmbeddingUnavailable: ``no_connection`` outside a managed host.
    """
    if not base or not credential:
        raise EmbeddingUnavailable(REASON_NO_CONNECTION, "no model proxy credential")
    return httpx.Client(
        base_url=base.rstrip("/"),
        headers={"Authorization": f"Bearer {credential}"},
        timeout=_TIMEOUT,
        transport=transport,
    )


class ProxyEmbedder(Embedder):
    """Embeds through the engine's model proxy with the host's launch token."""

    def __init__(
        self,
        server_url: Callable[[], str | None],
        credential: Callable[[], str | None],
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        :param server_url: The engine's base URL (``None`` until the runner knows it).
        :param credential: ``"<host_id>:<token>"``, or ``None`` outside a managed host.
        :param transport: Replaces the network (tests).
        """
        self._server_url = server_url
        self._credential = credential
        self._transport = transport
        self._clock = clock
        self._plan: tuple[float, EmbeddingPlan | EmbeddingUnavailable] | None = None

    # ------------------------------------------------------------------ plumbing

    def _client(self) -> httpx.Client:
        return proxy_client(self._server_url(), self._credential(), self._transport)

    def forget(self) -> None:
        """Drop the cached plan (a key was added: ask the engine again)."""
        self._plan = None

    # ------------------------------------------------------------------ the plan

    def plan(self) -> EmbeddingPlan:
        cached = self._plan
        if cached is not None and self._clock() - cached[0] < PLAN_TTL_SECONDS:
            found = cached[1]
        else:
            found = self._fetch_plan()
            self._plan = (self._clock(), found)
        if isinstance(found, EmbeddingUnavailable):
            raise found
        return found

    def _fetch_plan(self) -> EmbeddingPlan | EmbeddingUnavailable:
        try:
            with self._client() as client:
                response = client.get("/v1/model/embeddings")
        except EmbeddingUnavailable as exc:
            return exc
        except httpx.HTTPError as exc:
            _logger.warning("embedding plan unreachable (%s)", type(exc).__name__)
            return EmbeddingUnavailable(REASON_ERROR, "the engine could not be reached")
        if response.status_code == 401:
            return EmbeddingUnavailable(REASON_NO_CONNECTION, "not signed in to the model proxy")
        if response.status_code >= 400:
            return EmbeddingUnavailable(REASON_ERROR, f"engine answered {response.status_code}")
        body: Any = response.json()
        if not isinstance(body, dict) or not body.get("available"):
            reason = body.get("reason") if isinstance(body, dict) else None
            return EmbeddingUnavailable(
                reason if isinstance(reason, str) and reason else REASON_NO_CONNECTION
            )
        dims = body.get("dimensions")
        return EmbeddingPlan(
            provider=str(body["provider"]),
            model=str(body["model"]),
            dimensions=dims if isinstance(dims, int) and not isinstance(dims, bool) else None,
            tag=str(body["tag"]),
        )

    # ------------------------------------------------------------------ the call

    def embed(self, texts: list[str]) -> Embedded:
        plan = self.plan()
        vectors: list[list[float]] = []
        try:
            with self._client() as client:
                for start in range(0, len(texts), BATCH_SIZE):
                    vectors.extend(self._batch(client, plan, texts[start : start + BATCH_SIZE]))
        except EmbeddingUnavailable as exc:
            if exc.reason != REASON_ERROR:
                self.forget()  # a budget, suspension or key change: look again next time
            raise
        except httpx.HTTPError as exc:
            _logger.warning("embedding call failed (%s)", type(exc).__name__)
            raise EmbeddingUnavailable(
                REASON_ERROR, "the embedding provider could not be reached"
            ) from None
        return Embedded(vectors, plan.tag)

    @staticmethod
    def _batch(client: httpx.Client, plan: EmbeddingPlan, batch: list[str]) -> list[list[float]]:
        body: dict[str, Any] = {"model": plan.model, "input": batch}
        if plan.dimensions:
            body["dimensions"] = plan.dimensions
        response = client.post(f"/v1/model/{plan.provider}/v1/embeddings", json=body)
        if response.status_code == 402:
            raise EmbeddingUnavailable(REASON_BUDGET, "the model budget is used up")
        if response.status_code == 403:
            raise EmbeddingUnavailable(REASON_SUSPENDED, "the account is suspended")
        if response.status_code >= 400:
            _logger.warning("embedding call answered %s", response.status_code)
            raise EmbeddingUnavailable(REASON_ERROR, "the embedding provider refused the request")
        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingUnavailable(REASON_ERROR, "the provider returned too few vectors")
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [[float(x) for x in item["embedding"]] for item in ordered]
