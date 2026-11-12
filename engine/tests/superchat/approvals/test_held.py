"""Stored approvals that no runner holds any more read expired (:mod:`.held`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from omnigent.superchat.approvals import held


@dataclass
class _Row:
    session_id: str
    elicitation_id: str


class _Store:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def decide(self, elicitation_id: str, status: str) -> None:
        assert status == "expired"
        self.deleted.append(elicitation_id)


def _runner(holds: dict[str, list[str]]) -> Any:
    """A fake runner answering ``GET /v1/sessions/{id}/pending-approvals``."""

    def handler(request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[3]
        if session_id not in holds:
            return httpx.Response(503)
        return httpx.Response(200, json={"elicitation_ids": holds[session_id]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://runner")

    async def get_client(session_id: str, _router: Any) -> httpx.AsyncClient | None:
        return None if session_id == "conv_no_runner" else client

    return get_client


@pytest.mark.asyncio
async def test_rows_the_runner_no_longer_holds_are_deleted(monkeypatch) -> None:
    monkeypatch.setattr(held, "_get_runner_client", _runner({"conv_a": ["e_live"]}))
    resolved: list[tuple[str, str]] = []
    monkeypatch.setattr(pending_elicitations, "resolve", lambda s, e: resolved.append((s, e)))
    store = _Store()
    rows = [_Row("conv_a", "e_live"), _Row("conv_a", "e_stale")]

    kept = await held.drop_unparked(store, rows, None)

    assert [r.elicitation_id for r in kept] == ["e_live"]
    assert store.deleted == ["e_stale"]
    assert resolved == [("conv_a", "e_stale")]


@pytest.mark.asyncio
async def test_rows_are_kept_when_the_runner_cannot_be_asked(monkeypatch) -> None:
    monkeypatch.setattr(held, "_get_runner_client", _runner({}))
    store = _Store()
    rows = [_Row("conv_down", "e1"), _Row("conv_no_runner", "e2")]

    kept = await held.drop_unparked(store, rows, None)

    assert kept == rows
    assert store.deleted == []
