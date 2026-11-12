"""Tests for the runner-side ``memory_*`` dispatch (native-relay path).

Native harnesses (claude-native / codex-native) call the memory tools
through the runner's MCP relay, which has no in-process ``MemoryService`` —
see ``_execute_memory_tool`` in ``omnigent.runner.tool_dispatch``. These
tests mock the Omnigent server's REST endpoints it calls instead, mirroring
``tests/runner/test_session_history_tool_dispatch.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.spec.types import AgentSpec
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.memory.handlers import handle_memory_tool

CONV = "conv_native_memory_test"


async def _execute_memory_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Run the memory handler the way the runner dispatch does."""
    return await handle_memory_tool(HandlerCtx(tool_name, server_client, conversation_id), args)


_MEMORY_TOOL_NAMES = (
    "memory_remember",
    "memory_search",
    "memory_get",
    "memory_explain",
    "memory_forget",
)


def _make_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1, skills=[], mcp_servers=[], local_tools=[], skills_filter="none"
    )


def _client(handler: httpx.MockTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler) if handler else None,
        base_url="http://server",
    )


# ── Missing server access / session id ──────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_server_client() -> None:
    result = json.loads(
        await _execute_memory_tool(
            "memory_search", {"query": "x"}, conversation_id=CONV, server_client=None
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_conversation_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search", {"query": "x"}, conversation_id=None, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_remember ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remember_posts_to_remember_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/remember"
        body = json.loads(request.content)
        assert body == {
            "text": "Prefers CAD",
            "kind": "preference",
            "quote": None,
            "replaces_claim_id": None,
            "explicitness": None,
        }
        return httpx.Response(
            200, json={"action": "added", "claim": {"claim_id": "c1", "text": "Prefers CAD"}}
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_remember",
                {"text": "Prefers CAD", "kind": "preference"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert result["action"] == "added"


@pytest.mark.asyncio
async def test_remember_requires_non_empty_text() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_remember", {"text": "  "}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_search ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_gets_from_search_endpoint_with_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/search"
        assert request.url.params["query"] == "currency"
        assert request.url.params["kind"] == "preference"
        return httpx.Response(200, json={"results": [{"claim_id": "c1"}]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search",
                {"query": "currency", "kind": "preference"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_search_requires_non_empty_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search", {"query": ""}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_get / memory_explain ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_fetches_claim_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/memory/claims/c1"
        return httpx.Response(200, json={"claim_id": "c1", "text": "hello"})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["claim_id"] == "c1"


@pytest.mark.asyncio
async def test_explain_fetches_explain_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/memory/claims/c1/explain"
        return httpx.Response(200, json={"claim": {"claim_id": "c1"}, "supersedes": []})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_explain", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["claim"]["claim_id"] == "c1"


@pytest.mark.asyncio
async def test_get_requires_claim_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── memory_forget ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_posts_to_forget_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1/sessions/{CONV}/memory/forget"
        body = json.loads(request.content)
        assert body == {"claim_id": "c1", "query": None, "confirm": True}
        return httpx.Response(200, json={"status": "forgotten", "claim": {"claim_id": "c1"}})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_forget",
                {"claim_id": "c1", "confirm": True},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert result["status"] == "forgotten"


@pytest.mark.asyncio
async def test_forget_requires_claim_id_or_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_forget", {}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── Error passthrough ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_2xx_response_surfaces_as_a_clean_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"error": "Claim not found"})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_get", {"claim_id": "c1"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "Claim not found" in result["error"]


# ── Grant gate: advertised only where actually callable ──────────────────────


# ── Native relay advertisement ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmounted_memory_route_reports_not_configured() -> None:
    """A bare 404 (router unmounted, no memory extra) is explained."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_memory_tool(
                "memory_search", {"query": "x"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result == {"error": "memory_search: long-term memory is not configured on this server"}


@pytest.mark.asyncio
async def test_remember_forwards_inferred_explicitness() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"action": "added", "claim": {}})

    client = _client(handler)
    try:
        await _execute_memory_tool(
            "memory_remember",
            {"text": "The user wants French", "kind": "instruction", "explicitness": "inferred"},
            conversation_id=CONV,
            server_client=client,
        )
    finally:
        await client.aclose()
    assert seen["explicitness"] == "inferred"
