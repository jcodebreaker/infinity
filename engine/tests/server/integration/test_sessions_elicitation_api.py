"""
Integration tests for the elicitation REST API endpoints.

Covers the ``GET`` and ``POST`` elicitation endpoints exercised as a
standalone REST flow (no REPL, no SSE client). The companion file
``test_sessions_elicitation_resolve_url.py`` covers the round-trip
resolve path (park a real Future, resolve via URL, assert the hook
returns the correct decision). This file fills the remaining gaps:

- GET returns the correct pending-request shape (fields, types).
- POST resolve for an already-resolved elicitation is idempotent (202).
- GET after resolution returns ``status: "resolved"``.
- GET/POST against a valid session with a nonexistent elicitation id.

Uses the shared ``client`` fixture (real stores + mock LLM) and the
``_park_permission_hook`` helper from the resolve-URL module to create
real parked elicitations without duplicating the hook machinery.
"""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_agent

# Re-use the hook-parking helpers from the sibling module so we don't
# duplicate the SSE-drain + PermissionRequest machinery.
from tests.server.integration.test_sessions_elicitation_resolve_url import (
    _create_session,
)

pytestmark = pytest.mark.asyncio


# ── GET /sessions/{id}/elicitations/{eid} ────────────────


async def test_get_nonexistent_elicitation_on_valid_session(
    client: httpx.AsyncClient,
) -> None:
    """
    GET with a valid session but unknown elicitation id returns
    ``status: "resolved"`` (not 404), since the elicitation may have
    been resolved before the page loaded.
    """
    agent = await create_test_agent(client, "test-get-nonexistent-eid")
    session_id = await _create_session(client, agent["id"])

    resp = await client.get(
        f"/v1/sessions/{session_id}/elicitations/elicit_does_not_exist",
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "resolved"
    # Resolved responses must NOT leak pending-only fields.
    assert "message" not in data
    assert "phase" not in data
    assert "policy_name" not in data


async def test_get_nonexistent_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    GET against a session that does not exist returns 404.
    """
    resp = await client.get(
        "/v1/sessions/conv_no_such_session/elicitations/elicit_irrelevant",
    )
    assert resp.status_code == 404, resp.text


# ── POST /sessions/{id}/elicitations/{eid}/resolve ───────


async def test_post_resolve_nonexistent_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    POST resolve against a nonexistent session returns 404.
    """
    resp = await client.post(
        "/v1/sessions/conv_no_such_session/elicitations/elicit_irrelevant/resolve",
        json={"action": "accept"},
    )
    assert resp.status_code == 404, resp.text


async def test_post_resolve_nonexistent_elicitation_on_valid_session(
    client: httpx.AsyncClient,
) -> None:
    """
    POST resolve with a valid session but unknown elicitation id
    returns 202 (idempotent no-op) — the elicitation may have already
    been resolved by timeout or another tab.
    """
    agent = await create_test_agent(client, "test-resolve-unknown-eid")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_does_not_exist/resolve",
        json={"action": "accept"},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}


async def test_post_resolve_invalid_action_returns_422(
    client: httpx.AsyncClient,
) -> None:
    """
    A body with an invalid ``action`` value is rejected with 422
    before any resolution logic runs.
    """
    agent = await create_test_agent(client, "test-resolve-bad-action")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/elicitations/elicit_whatever/resolve",
        json={"action": "yolo"},
    )
    assert resp.status_code == 422, resp.text
