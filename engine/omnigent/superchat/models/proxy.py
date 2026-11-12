"""Engine-side model proxy: a host's model calls are made with its owner's key.

The agent can run shell commands inside its host, so any key placed in that host's env or
files is readable by it. Instead the host's harness points its model base URL at this route and
presents a *credential* that is only the host's own launch token (``<host_id>:<token>``, the
same token the host tunnel and :mod:`omnigent.server.routes.host_credentials` use). The route
resolves token -> host -> owner server-side, looks up the owner's sealed key, strips whatever auth
the caller sent, injects the owner's key and streams the provider's answer back. The key never
leaves the engine process.

Route: ``/v1/model/{provider}/{path}``. There is no host id in the path: the base URL is fixed in
the session snapshot before the host exists, so the host id travels inside the credential.
``provider`` is an upstream from :mod:`omnigent.superchat.models.upstreams` (base URL and auth
style come from there) and is resolved to the owner's connection for exactly that provider: their
own key first, else the organization's. Auth headers and
bodies are never logged.

Before forwarding, a suspended owner is refused with ``account_suspended`` (HTTP 403; the error
type, not the status, is what harnesses classify on); an owner (or organization) at a monthly
budget set to stop is refused with
``model_budget_exhausted`` (HTTP 402); after it, the call's cost is read from the response as it
streams through (:mod:`omnigent.superchat.models.spend`) and added to the owner's daily rollup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse, StreamingResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.host_store import HostStore
from omnigent.superchat.models.org import SUSPENDED_MESSAGE, get_suspensions
from omnigent.superchat.models.spend import UsageTap, cost_usd
from omnigent.superchat.models.store import resolve_model_connection
from omnigent.superchat.models.upstreams import UPSTREAMS, Upstream

_logger = logging.getLogger(__name__)

KEY_REQUIRED_MESSAGE = "Add a model API key in Settings to use Superchat."
# connect/write/pool are short; a long read covers a model that thinks silently for minutes.
_TIMEOUT = httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=10.0)
# Request headers never forwarded: the caller's auth, hop-by-hop and transport framing.
_DROP_REQUEST = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "cookie",
        "x-omnigent-host-token",
        "transfer-encoding",
    }
)
_DROP_RESPONSE = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection", "set-cookie"}
)


def split_credential(request: Request) -> tuple[str, str] | None:
    """:returns: ``(host_id, token)`` from ``x-api-key`` or a Bearer header, or ``None``."""
    value = request.headers.get("x-api-key")
    if not value:
        auth = request.headers.get("authorization", "")
        value = auth[7:] if auth.lower().startswith("bearer ") else ""
    host_id, sep, token = value.strip().partition(":")
    if not sep or not host_id or not token:
        return None
    return host_id, token


def _checked_body(content: bytes, upstream_cfg: Upstream, path: str = "") -> tuple[bytes, str]:
    """Respell a request body's ``model`` the way the upstream expects it.

    The model is not judged by its name: the upstream itself answers for a model it does not
    serve. OpenRouter Chat Completions also get ``usage.include`` so the response reports the
    charge.

    Only the (small) request body is parsed; a body that is not a JSON object with a string
    ``model`` is forwarded untouched, and so is one whose id the upstream already spells right.

    :returns: ``(body, requested_model)``; the model is ``""`` when the body names none.
    """
    try:
        payload = json.loads(content)
    except ValueError:
        return content, ""
    model = payload.get("model") if isinstance(payload, dict) else None
    if not isinstance(model, str):
        return content, ""
    mapped = upstream_cfg.upstream_model(model)
    reports_cost = upstream_cfg.name == "openrouter" and path.endswith("chat/completions")
    if mapped == model and not reports_cost:
        return content, model
    payload = {**payload, "model": mapped}
    if reports_cost:
        payload["usage"] = {**(payload.get("usage") or {}), "include": True}
    return json.dumps(payload, separators=(",", ":")).encode(), model


def _suspended_refusal() -> JSONResponse:
    """403 in the provider error shape; the ``account_suspended`` type is what harnesses read."""
    code = ErrorCode.ACCOUNT_SUSPENDED
    return JSONResponse(
        {"type": "error", "error": {"type": code, "code": code, "message": SUSPENDED_MESSAGE}},
        status_code=403,
    )


def _budget_refusal(message: str) -> JSONResponse:
    """402 in the provider error shape, so a harness reads ``model_budget_exhausted``."""
    code = ErrorCode.MODEL_BUDGET_EXHAUSTED
    return JSONResponse(
        {"type": "error", "error": {"type": code, "code": code, "message": message}},
        status_code=402,
    )


async def _record_spend(budgets: Any, owner: str, tap: UsageTap, requested_model: str) -> None:
    """Add what the call cost to the owner's daily rollup; a failure here never fails the call."""
    try:
        usage = tap.finish()
        if usage is None:
            return
        cost = await asyncio.to_thread(cost_usd, usage, requested_model)
        if cost:
            await asyncio.to_thread(budgets.record_spend, owner, cost)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("model proxy: spend not recorded (%s)", type(exc).__name__)


def create_model_proxy_router(
    host_store: HostStore, *, transport: httpx.AsyncBaseTransport | None = None
) -> APIRouter:
    """Build the router, mounted with ``prefix="/v1"``. *transport* makes upstream injectable."""
    router = APIRouter()

    @router.get("/model/embeddings", response_model=None)
    async def embedding_plan(request: Request) -> JSONResponse:
        """How the calling host's owner embeds (provider, model, size), or why they cannot."""
        credential = split_credential(request)
        if credential is None:
            raise HTTPException(status_code=401, detail="missing host credential")
        managed = await asyncio.to_thread(host_store.resolve_launch_token, *credential)
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        embeddings = getattr(request.app.state, "model_embeddings", None)
        if embeddings is None:
            return JSONResponse({"available": False, "reason": "no_connection"})
        from omnigent.superchat.models.embeddings import EmbeddingUnavailable

        try:
            plan = await asyncio.to_thread(embeddings.plan, managed.user_id)
        except EmbeddingUnavailable as exc:
            return JSONResponse({"available": False, "reason": exc.reason})
        return JSONResponse(
            {
                "available": True,
                "reason": None,
                "provider": plan.provider,
                "model": plan.model,
                "dimensions": plan.dimensions,
                "tag": plan.tag,
            }
        )

    @router.get("/model/rerank", response_model=None)
    async def rerank_plan(request: Request) -> JSONResponse:
        """Which cheap chat model reorders the calling host's search hits, or why none can."""
        credential = split_credential(request)
        if credential is None:
            raise HTTPException(status_code=401, detail="missing host credential")
        managed = await asyncio.to_thread(host_store.resolve_launch_token, *credential)
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        rerank = getattr(request.app.state, "model_rerank", None)
        if rerank is None:
            return JSONResponse({"available": False, "reason": "no_connection"})
        from omnigent.superchat.models.rerank import RerankUnavailable

        background = getattr(request.app.state, "model_background", None)
        model = await background.light_model(managed.user_id) if background else None
        try:
            plan = await asyncio.to_thread(rerank.plan, managed.user_id, model)
        except RerankUnavailable as exc:
            return JSONResponse({"available": False, "reason": exc.reason})
        return JSONResponse(
            {"available": True, "reason": None, "provider": plan.provider, "model": plan.model}
        )

    # An internal relay for hosts and harnesses (launch token), not a documented API: left out
    # of the schema so one route with two methods does not produce two operations with one id.
    @router.api_route(
        "/model/{provider}/{path:path}",
        methods=["GET", "POST"],
        response_model=None,
        include_in_schema=False,
    )
    async def model_proxy(
        provider: str, path: str, request: Request
    ) -> StreamingResponse | JSONResponse:
        """Forward a model call with the session owner's key; stream the answer back."""
        credential = split_credential(request)
        if credential is None:
            raise HTTPException(status_code=401, detail="missing host credential")
        managed = await asyncio.to_thread(host_store.resolve_launch_token, *credential)
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        upstream_cfg = UPSTREAMS.get(provider)
        segments = path.split("/")
        if (
            upstream_cfg is None
            or not upstream_cfg.configured
            or segments[0] != "v1"
            or ".." in segments
            or "" in segments
        ):
            raise HTTPException(status_code=404, detail="unknown model route")
        suspensions = get_suspensions()
        if suspensions is not None and await asyncio.to_thread(
            suspensions.is_suspended, managed.user_id
        ):
            return _suspended_refusal()
        budgets = getattr(request.app.state, "model_budgets", None)
        if budgets is not None:
            breach = await asyncio.to_thread(budgets.stop_breach, managed.user_id)
            if breach is not None:
                return _budget_refusal(breach.message)
        store = getattr(request.app.state, "model_connection_store", None)
        connection = await asyncio.to_thread(
            resolve_model_connection, store, owner_id=managed.user_id, preferred=(provider,)
        )
        if connection is None:
            raise OmnigentError(KEY_REQUIRED_MESSAGE, code=ErrorCode.MODEL_KEY_REQUIRED)
        content = await request.body()
        requested_model = ""
        if request.method == "POST":
            content, requested_model = _checked_body(content, upstream_cfg, path)
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _DROP_REQUEST
        }
        headers.update(upstream_cfg.key_header(connection.plaintext))
        client = httpx.AsyncClient(transport=transport, timeout=_TIMEOUT, follow_redirects=False)
        upstream_request = client.build_request(
            request.method,
            f"{upstream_cfg.origin}/{path}",
            params=request.query_params,
            headers=headers,
            content=content,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            # Only the exception type is logged: its text could carry request details.
            _logger.warning("model proxy: %s unreachable (%s)", provider, type(exc).__name__)
            raise OmnigentError(
                f"Could not reach {provider}.", code=ErrorCode.MODEL_PROVIDER_UNREACHABLE
            ) from None

        tap = (
            UsageTap(upstream.headers.get("content-type", ""))
            if budgets is not None and upstream.status_code < 400
            else None
        )

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    if tap is not None:
                        tap.feed(chunk)
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
                if tap is not None:
                    await _record_spend(budgets, managed.user_id, tap, requested_model)

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() not in _DROP_RESPONSE
            },
        )

    return router
