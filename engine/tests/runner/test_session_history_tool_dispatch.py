"""Tests for the runner-side ``session_history`` dispatch (native-relay path).

Native harnesses (claude-native / codex-native) call ``session_history``
through the runner's MCP relay, which has no in-process ConversationStore —
see ``_execute_session_history_tool`` in ``omnigent.runner.tool_dispatch``.
These tests mock the Omnigent server's REST endpoints it calls instead.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_session_history_tool,
    _granted_tool_names,
    _ungranted_tool_reason,
)
from omnigent.spec.types import AgentSpec

CONV = "conv_native_test"


def _make_spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1, skills=[], mcp_servers=[], local_tools=[], skills_filter="none"
    )


def _client(handler: httpx.MockTransport | None = None, **kwargs: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler) if handler else None,
        base_url="http://server",
        **kwargs,  # type: ignore[arg-type]
    )


def _item(item_id: str, role: str, text: str, created_at: int = 0) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [
            {"type": "output_text" if role == "assistant" else "input_text", "text": text}
        ],
        "created_at": created_at,
    }


# ── Missing server access / session id ──────────────────────


@pytest.mark.asyncio
async def test_requires_server_client() -> None:
    result = json.loads(
        await _execute_session_history_tool(
            {"action": "status"}, conversation_id=CONV, server_client=None
        )
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_requires_conversation_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "status"}, conversation_id=None, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── read ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_via_rest_groups_turns_and_ignores_conversation_id_arg() -> None:
    """The dispatch-context conversation_id is used for the URL; any
    ``conversation_id`` in args is inert (the function signature doesn't
    even read it)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/items"
        assert request.url.params["order"] == "desc"
        return httpx.Response(
            200,
            json={
                "data": [
                    _item("2", "assistant", "answer"),
                    _item("1", "user", "question"),
                ],
                "has_more": False,
            },
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "conversation_id": "conv_someone_else"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["turns"]) == 1
    assert [m["content"] for m in result["turns"][0]["messages"]] == ["question", "answer"]
    assert result["next_cursor"] is None


@pytest.mark.asyncio
async def test_read_uses_after_cursor_not_before() -> None:
    """Continuing a desc-order walk must use ``after=``, not ``before=``
    (see the identically-named regression this caught in the in-process
    tool's ``group_into_turns``)."""
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"data": [_item("1", "user", "q")], "has_more": False})

    client = _client(handler)
    try:
        await _execute_session_history_tool(
            {"action": "read", "cursor": "item_5"},
            conversation_id=CONV,
            server_client=client,
        )
    finally:
        await client.aclose()
    assert seen_params[0].get("after") == "item_5"
    assert "before" not in seen_params[0]


# ── search ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_via_rest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/items/search"
        assert request.url.params["query"] == "hello"
        return httpx.Response(200, json={"data": [_item("1", "user", "hello there")]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search", "query": "hello"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_search_requires_query() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── status ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_via_rest_reads_labels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/labels"
        return httpx.Response(
            200,
            json={
                "labels": {
                    "omnigent.last_context_tokens": "100",
                    "omnigent.last_context_window": "1000",
                }
            },
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "status"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["current_context_tokens"] == 100
    assert result["context_window_tokens"] == 1000


# ── list_chats ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_chats_via_rest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/related_chats"
        return httpx.Response(
            200,
            json={"data": [{"id": "conv_main_1", "title": "Main chat"}]},
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "list_chats"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert result["chats"] == [{"id": "conv_main_1", "title": "Main chat"}]


@pytest.mark.asyncio
async def test_list_chats_via_rest_reports_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "list_chats"}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── read / search with chat_id ───────────────────────────────


@pytest.mark.asyncio
async def test_read_with_chat_id_targets_the_related_chat() -> None:
    other_chat = "conv_main_1"
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == f"/v1/sessions/{CONV}/related_chats":
            return httpx.Response(200, json={"data": [{"id": other_chat}]})
        assert request.url.path == f"/v1/sessions/{other_chat}/items"
        return httpx.Response(
            200, json={"data": [_item("1", "user", "main chat question")], "has_more": False}
        )

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "chat_id": other_chat},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert [m["content"] for m in result["turns"][0]["messages"]] == ["main chat question"]
    assert f"/v1/sessions/{CONV}/related_chats" in seen_paths


@pytest.mark.asyncio
async def test_read_with_own_id_as_chat_id_skips_the_related_chats_lookup() -> None:
    """chat_id == conversation_id is a no-op; no related_chats round trip needed."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/items"
        return httpx.Response(200, json={"data": [], "has_more": False})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "chat_id": CONV},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert "error" not in result


@pytest.mark.asyncio
async def test_read_with_unrelated_chat_id_is_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{CONV}/related_chats"
        return httpx.Response(200, json={"data": [{"id": "conv_main_1"}]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "chat_id": "conv_not_related"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


@pytest.mark.asyncio
async def test_search_with_chat_id_targets_the_related_chat() -> None:
    other_chat = "conv_main_1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CONV}/related_chats":
            return httpx.Response(200, json={"data": [{"id": other_chat}]})
        assert request.url.path == f"/v1/sessions/{other_chat}/items/search"
        return httpx.Response(200, json={"data": [_item("1", "user", "found it")]})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search", "query": "found", "chat_id": other_chat},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_search_with_unrelated_chat_id_is_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "search", "query": "x", "chat_id": "conv_not_related"},
                conversation_id=CONV,
                server_client=client,
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


@pytest.mark.asyncio
async def test_read_rejects_non_string_chat_id() -> None:
    client = httpx.AsyncClient(base_url="http://server")
    try:
        result = json.loads(
            await _execute_session_history_tool(
                {"action": "read", "chat_id": 123}, conversation_id=CONV, server_client=client
            )
        )
    finally:
        await client.aclose()
    assert "error" in result


# ── Grant gate: advertised only where actually callable ──────


def test_granted_tool_names_includes_session_history_for_superside_chat() -> None:
    """The grant check is consistent with ToolManager's gate: superside-chat
    gets session_history too, not just rollover."""
    spec = _make_spec()
    granted = _granted_tool_names(
        spec, "claude-sdk", labels={"omnigent.context.mode": "superside-chat"}
    )
    assert "session_history" in granted


def test_ungranted_tool_reason_allows_with_superside_chat_label() -> None:
    spec = _make_spec()
    reason = _ungranted_tool_reason(
        "session_history",
        spec,
        "claude-sdk",
        labels={"omnigent.context.mode": "superside-chat"},
    )
    assert reason is None


def test_granted_tool_names_cache_does_not_leak_across_context_modes() -> None:
    """Same spec object, same harness: a plain session must not see the
    rollover/superside-chat grant from a cached entry keyed only on
    (spec, harness) — see the cache key in ``_granted_tool_names``."""
    spec = _make_spec()
    granted_plain = _granted_tool_names(spec, "claude-sdk")
    granted_superside = _granted_tool_names(
        spec, "claude-sdk", labels={"omnigent.context.mode": "superside-chat"}
    )
    assert "session_history" not in granted_plain
    assert "session_history" in granted_superside


# ── Native relay advertisement ────────────────────────────────
