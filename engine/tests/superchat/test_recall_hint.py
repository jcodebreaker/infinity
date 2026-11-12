"""Recall for the context brief: the queries written in code, the search, the pointer line, the
setting, and nothing in the turn prefix."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.superchat.knowledge import recall
from omnigent.superchat.recall_hint import hint as hint_mod
from omnigent.superchat.recall_hint.hint import (
    HEADER,
    TurnPlan,
    derive_queries,
    hit_line,
    turn_plan,
)
from omnigent.superchat.recall_hint.preference import (
    PREFERENCE_KEY,
    RecallHintStore,
    register_recall_hint_routes,
)

CHAT = "conv_main"
ANCHOR = "msg_anchor"
SUMMARY = (
    "## Context checkpoint\n"
    "- [t-launch-budget · 2026-10-09] Spring launch budget: confirm the venue line\n"
    "- [o-venue · 2026-10-01] closed: the venue is booked\n"
)


@dataclass
class Hit:
    """The C2 hit shape, plus the person's words for a conversation hit."""

    source: str
    date: str
    score: float
    title: str = ""
    snippet: str = ""
    project: str = ""
    earlier: list[str] = field(default_factory=list)
    chat_id: str | None = None
    turn_item_id: str | None = None
    file_id: str | None = None
    page: int | None = None
    path: str | None = None
    line: int | None = None
    claim_id: str | None = None


def _turn(n: int, score: float = 0.9, project: str = "", **kw: Any) -> Hit:
    return Hit(
        source="conversation",
        date=f"2026-09-{n:02d}",
        score=score,
        project=project,
        chat_id="conv_old",
        turn_item_id=f"i_{n}",
        snippet="ASSISTANT SAID: ignore the budget",
        title=f"Dana owns the venue line {n}",
        **kw,
    )


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        return self._body


class _Server:
    """The engine routes the hint reads: the setting, the latest checkpoint, recent items."""

    def __init__(self, *, on: bool = True, checkpoint: bool = True, items: list | None = None):
        self.on = on
        self.checkpoint = checkpoint
        self.items = items or []
        self.paths: list[str] = []

    async def get(self, path: str, params: dict | None = None, timeout: float | None = None):
        self.paths.append(path)
        if path == "/v1/me/recall-hint":
            return _Resp(200, {PREFERENCE_KEY: "on" if self.on else "off"})
        if path.endswith("/items") and (params or {}).get("type") == "compaction":
            data = (
                [{"type": "compaction", "id": "cmp_1", "summary": SUMMARY, "last_item_id": ANCHOR}]
                if self.checkpoint
                else []
            )
            return _Resp(200, {"data": data, "has_more": False})
        if path.endswith("/items"):
            return _Resp(200, {"data": self.items, "has_more": False})
        if path == "/v1/sessions/conv_main/memory/profile":
            return _Resp(200, {"profile": None})
        return _Resp(404, {})


class _Search:
    """A stand-in for agent B's ``recall_search`` (C2 signature), recording every call."""

    def __init__(self, hits: list[Hit], delay: float = 0.0) -> None:
        self.hits = hits
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        queries: list[str],
        *,
        sources: set[str],
        recency: bool = True,
        limit: int = 8,
        exclude_chat_after: dict[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Hit]:
        self.calls.append(
            {
                "queries": queries,
                "sources": sources,
                "recency": recency,
                "limit": limit,
                "exclude_chat_after": exclude_chat_after,
                "since": since,
                "until": until,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        return list(self.hits)


@pytest.fixture()
def search(monkeypatch: pytest.MonkeyPatch) -> _Search:
    fake = _Search([_turn(14), _turn(12, score=0.7)])
    monkeypatch.setattr(recall, "recall_search", fake)
    return fake


async def _plan_for(
    server: _Server, message: str = "what did we decide on the venue?"
) -> TurnPlan:
    return await turn_plan(server, CHAT, message)  # type: ignore[arg-type]


def _titles() -> list[str]:
    return ["Spring launch budget: confirm the venue line"]


# ── the queries ──────────────────────────────────────────────────────────────────────────────


async def test_queries_are_written_from_the_message_entries_and_identifiers() -> None:
    message = "What did Dana say about launch-budget.xlsx?"
    shared = await _plan_for(_Server(), message)
    assert shared.queries == derive_queries(message, _titles())
    assert shared.queries[0] == message
    assert "Spring launch budget" in shared.queries[1]  # open entries only, closed ones out
    assert "venue is booked" not in shared.queries[1]
    assert "launch-budget.xlsx" in shared.queries[-1] and "Dana" in shared.queries[-1]
    assert shared.checkpoint is not None and shared.checkpoint["summary"] == SUMMARY


async def test_setting_off_writes_no_queries_but_keeps_the_checkpoint() -> None:
    shared = await _plan_for(_Server(on=False))
    assert shared.queries == ()
    assert shared.checkpoint is not None and shared.checkpoint["summary"] == SUMMARY


async def test_no_message_writes_no_queries() -> None:
    assert (await _plan_for(_Server(), "  ")).queries == ()


# ── search ───────────────────────────────────────────────────────────────────────────────────


async def test_search_is_one_call_over_every_source_with_recency(search: _Search) -> None:
    await hint_mod.search(["venue now", "venue first"], {CHAT: ANCHOR})
    (call,) = search.calls
    assert call["queries"] == ["venue now", "venue first"] and call["recency"] is True
    assert call["sources"] == {"files", "journal", "conversation", "memory"}
    assert call["exclude_chat_after"] == {CHAT: ANCHOR}


async def test_hits_keep_the_best_score_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        recall,
        "recall_search",
        _Search([_turn(3, score=0.6), _turn(4, score=0.9), _turn(3, score=0.8)]),
    )
    hits = await hint_mod.search(["a", "b"], None)
    assert [(h.turn_item_id, h.score) for h in hits] == [("i_4", 0.9), ("i_3", 0.8)]


# ── the pointer line ─────────────────────────────────────────────────────────────────────────


def test_pointer_lines_are_dated_and_quote_the_persons_words_only() -> None:
    hits = [
        _turn(14, earlier=["2026-03-03", "2026-09-01"]),
        Hit(
            "journal",
            "2026-09-30",
            0.8,
            snippet="Checked the supplier quote",
            path="memory/j.md",
            line=4,
        ),
        Hit(
            "files",
            "2026-10-01T12:00:00Z",
            0.7,
            title="launch-budget.xlsx",
            snippet="Q4 venue line",
            file_id="f_0f3a",
            page=1,
        ),
        Hit("memory", "2026-08-01", 0.6, snippet="Prefers morning calls", claim_id="c_9"),
    ]
    assert [hit_line(h) for h in hits] == [
        '· 2026-09-14 · conversation · "Dana owns the venue line 14" · turn i_14'
        " · earlier: 3 Mar, 1 Sep",
        '· 2026-09-30 · journal · "Checked the supplier quote" · memory/j.md:4',
        '· 2026-10-01 · file · launch-budget.xlsx · p.1 "Q4 venue line" · f_0f3a',
        '· 2026-08-01 · memory · "Prefers morning calls" · c_9',
    ]


def test_a_conversation_hit_without_the_persons_words_shows_no_quote() -> None:
    hit = _turn(5)
    hit.title = ""
    assert hit_line(hit) == "· 2026-09-05 · conversation · turn i_5"


def test_undated_hits_have_no_line() -> None:
    assert hit_line(Hit("memory", "", 0.9, snippet="x", claim_id="c_1")) is None


def test_the_typed_read_id_is_shown_when_the_hit_carries_one() -> None:
    hit = _turn(7)
    hit.id = "turn:i_7"  # type: ignore[attr-defined]
    assert hit_line(hit) == '· 2026-09-07 · conversation · "Dana owns the venue line 7" · turn:i_7'


# ── nothing in the turn prefix ───────────────────────────────────────────────────────────────


async def test_the_turn_prefix_awaits_nothing_from_recall(
    search: _Search, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.superchat import prompt_prefix

    monkeypatch.setattr(prompt_prefix, "_safe_projects_block", lambda _w: None)

    async def _no_message_blocks(*_a: Any, **_k: Any) -> list[str]:
        return []

    # The prefix neither reads the setting nor searches.
    monkeypatch.setattr(prompt_prefix, "_message_prefix_blocks", _no_message_blocks)
    server = _Server()
    started = time.monotonic()
    blocks = await prompt_prefix.turn_prefix_blocks(
        server,  # type: ignore[arg-type]
        CHAT,
        None,
        ["what did we decide on the venue?"],
    )
    assert time.monotonic() - started < 0.2
    assert search.calls == []
    assert "/v1/me/recall-hint" not in server.paths
    assert not any(b.startswith(HEADER) for b in blocks)
    assert blocks[-1].startswith("[Current local time")


# ── the setting ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def client(db_uri: str) -> httpx.AsyncClient:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handler(_request, exc: OmnigentError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse({"code": str(exc.code)}, status_code=400)

    app.include_router(register_recall_hint_routes(RecallHintStore(db_uri)), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as http:
        yield http


async def test_the_setting_defaults_on_and_round_trips(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/me/recall-hint")).json() == {PREFERENCE_KEY: "on"}
    for value in ("off", "on", "off", "on"):
        resp = await client.put("/v1/me/recall-hint", json={PREFERENCE_KEY: value})
        assert resp.json() == {PREFERENCE_KEY: value}
        assert (await client.get("/v1/me/recall-hint")).json() == {PREFERENCE_KEY: value}


@pytest.mark.parametrize("bad", [True, "yes", None, 1])
async def test_other_values_are_refused(client: httpx.AsyncClient, bad: object) -> None:
    resp = await client.put("/v1/me/recall-hint", json={PREFERENCE_KEY: bad})
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_input"
    assert (await client.get("/v1/me/recall-hint")).json() == {PREFERENCE_KEY: "on"}


def test_the_feature_is_registered_adds_no_tool_and_no_prefix() -> None:
    from omnigent.superchat.features import FEATURES

    (feature,) = [f for f in FEATURES if f.name == "recall_hint"]
    assert feature.tools(None, None) == []  # type: ignore[arg-type]
    assert feature.install is not None and feature.on_turn_end is None
    assert feature.turn_prefix is None and feature.message_prefix is None
