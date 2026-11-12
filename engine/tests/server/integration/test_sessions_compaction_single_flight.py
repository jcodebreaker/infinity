"""``POST /v1/sessions/{id}/events`` type ``"compaction"``: the cross-process
single-flight guard (``expected_previous_compaction_id``).

Two runner processes can both decide to roll the same session over around
the same turn. Each reads the session's current latest ``compaction`` item
(or finds none) before building its summary, then posts its result naming
the checkpoint it summarized from. If the session has moved on by the time
the POST lands — another process won the race — the server must reject it
(409) rather than silently overwrite a newer compaction with a stale one.
"""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, name: str) -> str:
    agent = await create_test_agent(client, name=name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _compaction_body(
    summary: str, *, expected_previous_compaction_id: object
) -> dict[str, object]:
    return {
        "type": "compaction",
        "data": {
            "summary": summary,
            "last_item_id": "msg_does_not_need_to_exist",
            "model": "test-model",
            "token_count": 3,
            "expected_previous_compaction_id": expected_previous_compaction_id,
        },
    }


async def test_first_compaction_with_no_prior_checkpoint_succeeds(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "compaction-single-flight-first")
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("first summary", expected_previous_compaction_id=None),
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": True}


async def test_second_racer_against_the_same_empty_checkpoint_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    """Two processes both read "no compaction yet" and both try to write
    the session's first checkpoint — only the first may win."""
    session_id = await _create_session(client, "compaction-single-flight-race-empty")
    winner = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("winner summary", expected_previous_compaction_id=None),
    )
    assert winner.status_code == 202, winner.text

    loser = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("loser summary", expected_previous_compaction_id=None),
    )
    assert loser.status_code == 409, loser.text


async def test_racer_against_a_moved_checkpoint_is_rejected(client: httpx.AsyncClient) -> None:
    """A process that summarized from checkpoint A loses when B has already
    landed by the time its own write arrives."""
    session_id = await _create_session(client, "compaction-single-flight-race-moved")
    first = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("checkpoint A", expected_previous_compaction_id=None),
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        f"/v1/sessions/{session_id}/events",
        # Still expecting "no checkpoint" — stale, checkpoint A already landed.
        json=_compaction_body("stale checkpoint", expected_previous_compaction_id=None),
    )
    assert second.status_code == 409, second.text


async def test_compaction_naming_the_current_checkpoint_succeeds(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "compaction-single-flight-progress")
    first = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("checkpoint A", expected_previous_compaction_id=None),
    )
    assert first.status_code == 202, first.text

    items_resp = await client.get(f"/v1/sessions/{session_id}/items")
    checkpoint_a_id = next(
        item["id"] for item in items_resp.json()["data"] if item["type"] == "compaction"
    )

    second = await client.post(
        f"/v1/sessions/{session_id}/events",
        json=_compaction_body("checkpoint B", expected_previous_compaction_id=checkpoint_a_id),
    )
    assert second.status_code == 202, second.text


async def test_compaction_without_the_guard_field_is_backwards_compatible(
    client: httpx.AsyncClient,
) -> None:
    """An older caller that never sends ``expected_previous_compaction_id``
    at all gets the pre-fix behavior: no guard, always accepted."""
    session_id = await _create_session(client, "compaction-single-flight-legacy")
    first = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "compaction",
            "data": {
                "summary": "checkpoint A",
                "last_item_id": "msg_does_not_need_to_exist",
                "model": "test-model",
                "token_count": 3,
            },
        },
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "compaction",
            "data": {
                "summary": "checkpoint B",
                "last_item_id": "msg_does_not_need_to_exist_either",
                "model": "test-model",
                "token_count": 3,
            },
        },
    )
    assert second.status_code == 202, second.text


# ── GET /items?type=compaction: the latest checkpoint, cheaply ────────────


async def test_items_type_filter_returns_the_latest_checkpoint(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "compaction-latest-lookup")
    for summary, expected in (("checkpoint A", None), ("checkpoint B", "A")):
        if expected is not None:
            listing = await client.get(
                f"/v1/sessions/{session_id}/items", params={"type": "compaction"}
            )
            expected = listing.json()["data"][0]["id"]
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json=_compaction_body(summary, expected_previous_compaction_id=expected),
        )
        assert resp.status_code == 202, resp.text

    latest = await client.get(
        f"/v1/sessions/{session_id}/items",
        params={"type": "compaction", "order": "desc", "limit": 1},
    )
    assert latest.status_code == 200, latest.text
    data = latest.json()["data"]
    assert [item["summary"] for item in data] == ["checkpoint B"]
    assert data[0]["last_item_id"] == "msg_does_not_need_to_exist"


async def test_items_type_filter_is_empty_without_a_checkpoint(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "compaction-latest-none")
    resp = await client.get(
        f"/v1/sessions/{session_id}/items", params={"type": "compaction", "order": "desc"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


async def test_items_type_filter_rejects_other_types(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "compaction-latest-bad-type")
    resp = await client.get(f"/v1/sessions/{session_id}/items", params={"type": "bogus"})
    assert resp.status_code == 422, resp.text
