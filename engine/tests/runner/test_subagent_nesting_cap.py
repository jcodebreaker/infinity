"""Tests for the superside-chat sub-agent nesting cap's fail-closed lookup.

``_session_kind_and_parent`` / ``_subagent_nesting_refusal``
(``omnigent.runner.tool_dispatch``) decide whether a caller may launch a
sub-agent based on its ``kind`` (and its parent's, one level up). A caller
whose kind cannot be verified — a network error, a non-200, or a response
missing the ``kind`` field — must refuse the launch (fail closed), never
treat the unverifiable caller as "not a sub-agent" and let it through.
"""

from __future__ import annotations

import httpx

from omnigent.runner.tool_dispatch import _session_kind_and_parent, _subagent_nesting_refusal

CALLER = "conv_caller"
PARENT = "conv_parent"


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://server")


def _json_handler(by_path: dict[str, tuple[int, dict[str, object]]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = by_path[request.url.path]
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _raising_handler() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.MockTransport(handler)


# ── _session_kind_and_parent ──────────────────────────────────────────────


async def test_session_kind_and_parent_returns_kind_and_parent_on_success() -> None:
    transport = _json_handler(
        {f"/v1/sessions/{CALLER}": (200, {"kind": "sub_agent", "parent_session_id": PARENT})}
    )
    async with _client(transport) as client:
        result = await _session_kind_and_parent(CALLER, client)
    assert result == ("sub_agent", PARENT)


async def test_session_kind_and_parent_none_on_network_error() -> None:
    async with _client(_raising_handler()) as client:
        assert await _session_kind_and_parent(CALLER, client) is None


async def test_session_kind_and_parent_none_on_non_200() -> None:
    transport = _json_handler({f"/v1/sessions/{CALLER}": (500, {"error": "boom"})})
    async with _client(transport) as client:
        assert await _session_kind_and_parent(CALLER, client) is None


async def test_session_kind_and_parent_none_when_kind_field_is_missing() -> None:
    """A 200 with no (or non-string) ``kind`` is just as unverifiable as a
    network failure — it must not be treated as a known, non-sub_agent kind."""
    transport = _json_handler({f"/v1/sessions/{CALLER}": (200, {"parent_session_id": None})})
    async with _client(transport) as client:
        assert await _session_kind_and_parent(CALLER, client) is None


# ── _subagent_nesting_refusal ──────────────────────────────────────────────


async def test_nesting_allows_a_top_level_chat_caller() -> None:
    transport = _json_handler({f"/v1/sessions/{CALLER}": (200, {"kind": "default"})})
    async with _client(transport) as client:
        refusal = await _subagent_nesting_refusal(conversation_id=CALLER, server_client=client)
    assert refusal is None


async def test_nesting_allows_a_direct_subagent_caller() -> None:
    transport = _json_handler(
        {
            f"/v1/sessions/{CALLER}": (200, {"kind": "sub_agent", "parent_session_id": PARENT}),
            f"/v1/sessions/{PARENT}": (200, {"kind": "default"}),
        }
    )
    async with _client(transport) as client:
        refusal = await _subagent_nesting_refusal(conversation_id=CALLER, server_client=client)
    assert refusal is None


async def test_nesting_refuses_a_coordinator_subagents_child() -> None:
    transport = _json_handler(
        {
            f"/v1/sessions/{CALLER}": (200, {"kind": "sub_agent", "parent_session_id": PARENT}),
            f"/v1/sessions/{PARENT}": (200, {"kind": "sub_agent"}),
        }
    )
    async with _client(transport) as client:
        refusal = await _subagent_nesting_refusal(conversation_id=CALLER, server_client=client)
    assert refusal is not None
    assert "nesting cap reached" in refusal


async def test_nesting_fails_closed_when_caller_lookup_is_unverifiable() -> None:
    """The regression this fix closes: a lookup failure must refuse the
    launch (retryable), never silently allow it through as "not a
    sub-agent"."""
    async with _client(_raising_handler()) as client:
        refusal = await _subagent_nesting_refusal(conversation_id=CALLER, server_client=client)
    assert refusal is not None
    assert "cannot verify" in refusal


async def test_nesting_fails_closed_when_parent_lookup_is_unverifiable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{CALLER}":
            return httpx.Response(200, json={"kind": "sub_agent", "parent_session_id": PARENT})
        raise httpx.ConnectError("connection refused")

    async with _client(httpx.MockTransport(handler)) as client:
        refusal = await _subagent_nesting_refusal(conversation_id=CALLER, server_client=client)
    assert refusal is not None
    assert "cannot verify" in refusal
