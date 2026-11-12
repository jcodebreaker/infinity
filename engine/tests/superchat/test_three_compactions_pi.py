"""One work chat with big turns through three compactions, as the pi harness sees it.

Follows a single Super Chat Conversation through three checkpoints and checks what a cold
pi process is sent after each one. The layers that run for real are the ones the runner
drives: :func:`~omnigent.superchat.rollover.roll_over_session` (anchored reads, the
summarizer call, repair, closes, the budget), the ``on_stored`` copy into ``memory/``
(:func:`~omnigent.runner.memory.sync.record_checkpoint`), a settled Round's journal entry,
the ``state_close`` handler, the runner's cold-start history read (a recovery turn on a real
runner app, which goes through ``_load_history_as_input``), the executor adapter's
``_translate_input_to_messages(..., tool_digests=True)`` and pi's ``_build_pi_prompt``.

Faked: the server (an in-memory items route and labels), the summarizer model (three
scripted replies; the second drops the EUR decision line), the harness subprocess and its
HTTP stream. No model call.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context import rollover as context_rollover
from omnigent.context.labels import STATE_CLOSE_LABEL_PREFIX
from omnigent.context.rollover import CHECKPOINT_MARKER
from omnigent.context.tool_digest import FRAMING
from omnigent.harnesses.pi_executor import _build_pi_prompt
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runner import create_runner_app
from omnigent.runner.memory import files
from omnigent.runner.memory.sync import record_checkpoint, record_round
from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.superchat import rollover as rollover_mod
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.rollover import roll_over_session
from omnigent.superchat.state_file.handlers import handle_state_close
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse

CHAT_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
AGENT_ID = "a9e47a9e47a9e47a9e47a9e47a9e47a9"
MODEL = "claude-sonnet-4-5"

P1, P2, P3 = "2026-09-02", "2026-09-16", "2026-10-01"
EUR_LINE = f"- [d-finance-eur · {P1}] Anything for finance is in EUR, never USD."
#: A row that only ever appears in phase 1's pasted CSV.
CSV_MARKER = "Q3-0042,2026-07-15,APAC,Pune"


# ── fakes ──────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeServer:
    """The session's record and labels in memory, behind the routes the runner calls.

    ``GET .../items`` honours ``type``, ``order``, ``limit`` and the exclusive ``after`` cursor
    (400 for an unknown id). ``POST .../events`` with a compaction appends it to the record
    (409 when ``expected_previous_compaction_id`` is stale). ``PATCH`` upserts labels.
    Every items read is logged with the ids it served.
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {
            "omnigent.context.mode": "superside-chat",
            # A small tail budget: the newest turn or two stay after each anchor, the rest is folded.
            "omnigent.context.rollover_keep_tokens": "200",
        }
        self.reads: list[tuple[dict[str, str], list[str]]] = []
        self._next = 1

    def add(self, item: dict[str, Any]) -> str:
        item_id = f"it_{self._next:03d}"
        self._next += 1
        self.items.append({"id": item_id, **item})
        return item_id

    def compactions(self) -> list[dict[str, Any]]:
        return [it for it in self.items if it["type"] == "compaction"]

    async def get(
        self, url: str, *, params: dict[str, str] | None = None, timeout: float = 10.0
    ) -> _Resp:
        del timeout
        if not url.endswith("/items"):
            return _Resp(200, {"id": CHAT_ID, "title": "Main", "labels": dict(self.labels)})
        params = dict(params or {})
        order = [it["id"] for it in self.items]
        rows = [it for it in self.items if params.get("type") in (None, it["type"])]
        desc = params.get("order") == "desc"
        if params.get("after"):
            if params["after"] not in order:
                return _Resp(400)
            at = order.index(params["after"])
            rows = [
                it
                for it in rows
                if (order.index(it["id"]) < at if desc else order.index(it["id"]) > at)
            ]
        if desc:
            rows = rows[::-1]
        limit = int(params.get("limit", "100"))
        page = copy.deepcopy(rows[:limit])
        self.reads.append((params, [it["id"] for it in page]))
        return _Resp(200, {"data": page, "has_more": len(rows) > limit})

    async def post(self, url: str, *, json: dict[str, Any], timeout: float = 10.0) -> _Resp:
        del timeout
        if url.endswith("/events") and json.get("type") == "compaction":
            data = dict(json["data"])
            latest = self.compactions()[-1]["id"] if self.compactions() else None
            if data.pop("expected_previous_compaction_id") != latest:
                return _Resp(409)
            self.add({"type": "compaction", **data})
        return _Resp(200)

    async def patch(self, url: str, *, json: dict[str, Any], timeout: float = 10.0) -> _Resp:
        del url, timeout
        self.labels.update(json.get("labels") or {})
        return _Resp(200)


class _ReadOnlyServer(_FakeServer):
    """The runner app's view: the same record, nothing it writes lands in it."""

    def __init__(self, source: _FakeServer) -> None:
        super().__init__()
        self.items = copy.deepcopy(source.items)
        self.labels = dict(source.labels)

    async def post(self, url: str, *, json: dict[str, Any], timeout: float = 10.0) -> _Resp:
        del url, json, timeout
        return _Resp(200)


class _ScriptedSummarizer:
    """The summarizer model: one scripted reply per call, every request kept."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.responses = self

    async def create(self, **kwargs: Any) -> Response:
        self.requests.append(kwargs)
        return Response(
            output=[MessageOutput(content=[OutputText(text=self._replies.pop(0))])],
            model=MODEL,
        )


# ── the chat ───────────────────────────────────────────────────────────────


def _user(server: _FakeServer, text: str) -> str:
    return server.add(
        {
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [{"type": "input_text", "text": text}],
        }
    )


def _assistant(server: _FakeServer, text: str) -> str:
    return server.add(
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        }
    )


def _tool(server: _FakeServer, call_id: str, name: str, args: dict[str, Any], out: str) -> None:
    server.add(
        {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(args),
            "status": "completed",
        }
    )
    server.add({"type": "function_call_output", "call_id": call_id, "output": out})


_REGIONS = ("EMEA", "AMER", "APAC", "LATAM")
_CITIES = {
    "EMEA": ("Hamburg", "Lyon", "Milan", "Leeds"),
    "AMER": ("Austin", "Denver", "Boston", "Toronto"),
    "APAC": ("Osaka", "Perth", "Pune", "Seoul"),
    "LATAM": ("Lima", "Bogota", "Quito", "Santiago"),
}


def _q3_csv() -> str:
    rows = ["order_id,date,region,city,product,units,revenue_usd"]
    for n in range(1, 61):
        region = _REGIONS[n % 4]
        city = _CITIES[region][n // 4 % 4]
        day = 1 + (n * 7) % 28
        month = 7 + n % 3
        rows.append(
            f"Q3-{n:04d},2026-{month:02d}-{day:02d},{region},{city},"
            f"{('Pro plan', 'Team plan', 'Add-on seats')[n % 3]},{10 + n % 17},{1200 + n * 137}"
        )
    csv = "\n".join(rows)
    assert CSV_MARKER in csv
    return csv


def _report_text() -> str:
    paragraph = (
        "Weekly revenue report, week 34. Region totals are taken from the billing export "
        "and rounded to the nearest hundred. EMEA 412,300; AMER 655,100; APAC 301,900; "
        "LATAM 128,400. Commentary: EMEA softened after the Lyon reseller paused orders. "
    )
    return paragraph * 12


def _phase_1(server: _FakeServer) -> None:
    _user(
        server,
        "Here is the Q3 sales export I pulled this morning. Total it by region and save the "
        "totals as a CSV next to my files, please.\n\n" + _q3_csv(),
    )
    _tool(
        server,
        "call_totals",
        "files_write",
        {"path": "q3-sales-by-region.csv", "content": "region,revenue_usd\nEMEA,418950\n..."},
        "Wrote q3-sales-by-region.csv (5 lines, 96 bytes).",
    )
    _assistant(
        server,
        "Done. Q3 totals by region: EMEA 418,950; AMER 655,412; APAC 301,877; LATAM 128,390. "
        "Saved as q3-sales-by-region.csv.",
    )
    _user(server, "Can you check those against weekly-report.pdf? Finance sent it yesterday.")
    _tool(server, "call_report", "files_read", {"path": "weekly-report.pdf"}, _report_text())
    _assistant(
        server,
        "The report rounds, but EMEA does not match: the report says 412,300 and the export "
        "gives 418,950, a gap of 6,650. AMER, APAC and LATAM agree after rounding.",
    )
    _user(
        server,
        "Write an email to finance, cc Priya, explaining the EMEA gap and asking them to fix "
        "the report. Save it so I can send it myself.",
    )
    _tool(
        server,
        "call_email",
        "files_write",
        {"path": "finance-email.md", "content": "To: finance\nCc: Priya\n\nHi team, ..."},
        "Wrote finance-email.md (1.2 KB).",
    )
    _assistant(server, "Saved finance-email.md, addressed to finance with Priya in cc. Not sent.")
    _user(server, "From now on, anything for finance is in EUR, never USD.")
    _assistant(server, "Understood: everything for finance in EUR from now on, never USD.")


_SUMMARY_1 = f"""## Context checkpoint — {P1} 17:40 UTC

### Decisions
{EUR_LINE}

### Work
- [t-q3-sales · {P1}] Q3 sales totalled by region; totals written to q3-sales-by-region.csv.
- [o-report-mismatch · {P1}] weekly-report.pdf EMEA differs from the export (412,300 vs 418,950).
- [t-finance-email · {P1}] Email to finance cc Priya saved as finance-email.md; not sent.

Current position / next step: wait for finance to fix the report.
<journal>
{P1} · Totalled the pasted Q3 sales export by region into q3-sales-by-region.csv.
{P1} · Checked weekly-report.pdf against the totals; EMEA differs by 6,650.
{P1} · Wrote the finance email (cc Priya) to finance-email.md; not sent.
{P1} · The person set every finance figure to EUR, never USD.
</journal>"""


def _phase_2(server: _FakeServer) -> None:
    _user(
        server,
        "Finance fixed the report, EMEA matches now. Have a Helper build the Q4 forecast from "
        "q3-sales-by-region.csv: three scenarios, monthly, one sheet per scenario.",
    )
    _tool(
        server,
        "call_helper",
        "helpers_start",
        {"task": "Build the Q4 forecast from q3-sales-by-region.csv", "type": "worker"},
        '{"helper_id": "helper_q4", "status": "running"}',
    )
    _assistant(server, "A Helper is building the Q4 forecast; I will tell you when it is in.")
    _user(
        server,
        "[System: sub-agent worker/Q4 forecast finished (completed) — 1 result waiting in "
        "inbox. Call sys_read_inbox to collect.]",
    )
    _tool(
        server,
        "call_inbox",
        "sys_read_inbox",
        {},
        "Q4 forecast, three scenarios. Base 1.94M, low 1.71M, high 2.18M. " * 20,
    )
    _tool(
        server,
        "call_xlsx",
        "files_write",
        {"path": "q4-forecast.xlsx", "content": "<3 sheets>"},
        "Wrote q4-forecast.xlsx (3 sheets, 18 KB).",
    )
    _assistant(
        server,
        "The Q4 forecast is in q4-forecast.xlsx: base EUR 1.94M, low EUR 1.71M, high "
        "EUR 2.18M, one sheet per scenario.",
    )
    _user(server, "Great. The report mismatch is settled and the email went out this morning.")
    _assistant(server, "Noted: the mismatch is closed and the finance email was sent.")


# The rewrite DROPS the EUR decision, and closes the two finished topics.
_SUMMARY_2 = f"""## Context checkpoint — {P2} 11:05 UTC

### Work
- [t-q3-sales · {P1}] Q3 sales totalled by region; totals written to q3-sales-by-region.csv.
- [o-report-mismatch · {P2}] closed: finance fixed the report; EMEA matches.
- [t-finance-email · {P2}] closed: the email to finance (cc Priya) was sent.
- [t-q4-forecast · {P2}] Q4 forecast built by Helper helper_q4; written to q4-forecast.xlsx.

Current position / next step: share the Q4 numbers with finance when asked.
<journal>
{P2} · Finance fixed weekly-report.pdf; the EMEA mismatch is closed.
{P2} · Helper helper_q4 built the Q4 forecast; written to q4-forecast.xlsx.
{P2} · The finance email (cc Priya) was sent by the person.
</journal>"""


def _contract_entries() -> list[str]:
    return [
        f"- [o-contract-{n:02d} · {P3}] Supplier contract {n:02d} renews on "
        f"2026-12-{n % 28 + 1:02d}; owner procurement; notice period 60 days; the person "
        f"wants a reminder two weeks before the notice date and the renewal terms compared "
        f"with last year's before anything is signed; budget line OPEX-{n:03d}; "
        "status open; keep in EUR."
        for n in range(1, 48)
    ]


async def _phase_3(server: _FakeServer) -> None:
    _user(server, "The Q3 sales topic is done, take it off the list.")
    output = await handle_state_close(
        HandlerCtx(tool_name="state_close", server_client=server, conversation_id=CHAT_ID),  # type: ignore[arg-type]
        {"tag": "t-q3-sales", "reason": "the person said the Q3 sales topic is done"},
    )
    assert json.loads(output)["status"] == "will be closed at the next checkpoint"
    _tool(server, "call_close", "state_close", {"tag": "t-q3-sales"}, output)
    _assistant(server, "Done, the Q3 sales topic comes off the list at the next checkpoint.")
    contracts = "\n".join(
        f"{n:02d}. Supplier contract {n:02d}, renews 2026-12-{n % 28 + 1:02d}, 60 days notice, "
        f"budget line OPEX-{n:03d}, owner procurement."
        for n in range(1, 48)
    )
    _user(
        server,
        "Next big one: these are the 47 supplier contracts up for renewal. Track each one, "
        "remind me two weeks before every notice date, and save the tracker.\n\n" + contracts,
    )
    _tool(
        server,
        "call_tracker",
        "files_write",
        {"path": "contracts-tracker.md", "content": "| # | supplier | renews | ..."},
        "Wrote contracts-tracker.md (47 rows).",
    )
    _assistant(server, "All 47 contracts are tracked in contracts-tracker.md.")


_SUMMARY_3 = (
    f"## Context checkpoint — {P3} 09:12 UTC\n\n### Decisions\n{EUR_LINE}\n\n### Work\n"
    f"- [t-q3-sales · {P1}] Q3 sales totalled by region; totals in q3-sales-by-region.csv.\n"
    f"- [o-report-mismatch · {P2}] closed: finance fixed the report; EMEA matches.\n"
    f"- [t-finance-email · {P2}] closed: the email to finance (cc Priya) was sent.\n"
    f"- [t-q4-forecast · {P2}] Q4 forecast built by Helper helper_q4; in q4-forecast.xlsx.\n\n"
    "### Contracts\n"
    + "\n".join(_contract_entries())
    + "\n\nCurrent position / next step: send finance the Q4 numbers when asked.\n"
    f"<journal>\n{P3} · Put the 47 supplier contracts into contracts-tracker.md.\n</journal>"
)


# ── the cold start, as pi receives it ──────────────────────────────────────


async def _cold_start_input(server: _FakeServer) -> tuple[list[dict[str, Any]], _ReadOnlyServer]:
    """What a fresh runner sends the harness for this record (the record ends with the
    person's new message, so the runner starts the turn from its history read)."""
    view = _ReadOnlyServer(server)
    view.labels = {}
    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_cold"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_cold"}}),
        ]
    )
    spec = AgentSpec(
        spec_version=1,
        name="superchat-pi",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=view,  # type: ignore[arg-type]
    )
    async with _client(app) as client:
        resp = await client.post(
            "/v1/sessions", json={"session_id": CHAT_ID, "agent_id": AGENT_ID}
        )
        assert resp.status_code == 201
        for _ in range(100):
            if harness.posted_bodies:
                break
            await asyncio.sleep(0.02)
    assert len(harness.posted_bodies) == 1
    return harness.posted_bodies[0]["content"], view


@contextlib.asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            yield client


def _pi_prompt(history: list[dict[str, Any]]) -> str:
    """pi's first prompt on a new process for *history*, through the executor adapter."""
    prompt = _build_pi_prompt(
        _translate_input_to_messages(history, tool_digests=True), is_first_turn=True
    )
    assert isinstance(prompt, str)
    return prompt


def _asc_reads(server: _FakeServer) -> list[tuple[dict[str, str], list[str]]]:
    return [(p, ids) for p, ids in server.reads if p.get("order") == "asc"]


# ── the test ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_in_flight() -> None:
    rollover_mod._in_flight.clear()


def _frozen_at(day: str) -> type[datetime]:
    """A ``datetime`` whose ``now`` is *day*, so dates follow the scripted phase."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return datetime.fromisoformat(f"{day}T12:00:00+00:00")

    return _Frozen


async def test_three_compactions_reach_pi_on_cold_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeServer()
    summarizer = _ScriptedSummarizer([_SUMMARY_1, _SUMMARY_2, _SUMMARY_3])
    stored: list[Any] = []

    async def _compact(day: str) -> Any:
        monkeypatch.setattr(context_rollover, "datetime", _frozen_at(day))
        before = len(server.reads)
        labels = dict(server.labels)

        async def _on_stored(item: Any, _window: Any = None) -> None:
            stored.append(item)
            await record_checkpoint(CHAT_ID, labels, item, workspace=tmp_path)

        rolled = await roll_over_session(
            CHAT_ID,
            labels=labels,
            model=MODEL,
            server_client=server,  # type: ignore[arg-type]
            llm_client=summarizer,
            on_stored=_on_stored,
        )
        assert rolled is True
        return server.compactions()[-1], server.reads[before:]

    # Phase 1 → compaction 1: the first read covers the whole record.
    _phase_1(server)
    first_id = server.items[0]["id"]
    c1, reads = await _compact(P1)
    assert first_id in {i for p, ids in reads if p.get("order") == "asc" for i in ids}
    assert c1["repairs"] == 0 and c1["evicted_entries"] == []
    assert EUR_LINE in c1["summary"]
    # The checkpoint holds only the summary pair; the newest turns stay after its anchor.
    assert [m["role"] for m in c1["compacted_messages"]] == ["user", "assistant"]
    assert CSV_MARKER not in json.dumps(c1["compacted_messages"])

    # Cold start 1: checkpoint + the turns after its anchor + the new message.
    _user(server, "Morning. Anything I should know before the finance sync?")
    history, view = await _cold_start_input(server)
    assert all(p.get("after") == c1["last_item_id"] for p, _ in _asc_reads(view))
    prompt = _pi_prompt(history)
    assert EUR_LINE in prompt and "Anything I should know before the finance sync?" in prompt
    assert CSV_MARKER not in prompt
    server.items.pop()  # that message was a probe; phase 2 picks up from the checkpoint

    # Phase 2 → compaction 2: the rewrite drops the EUR decision; repair puts it back.
    _phase_2(server)
    await record_round(
        CHAT_ID,
        {
            "round_id": "msg_q4",
            "date": P2,
            "ask": "Have a Helper build the Q4 forecast",
            "outcome": "The Q4 forecast is in q4-forecast.xlsx.",
            "files": ["q4-forecast.xlsx"],
            "helpers": ["Q4 forecast"],
            "seconds": 95,
            "refs": {"helpers": ["helper_q4"]},
        },
        workspace=tmp_path,
    )
    c2, reads = await _compact(P2)
    assert all(p.get("after") == c1["last_item_id"] for p, _ in reads if p.get("order") == "asc")
    assert first_id not in {i for _, ids in reads for i in ids}
    assert c2["repairs"] == 1
    assert EUR_LINE in c2["summary"].splitlines(), "repair must restore the line word for word"
    assert f"[o-report-mismatch · {P2}] closed:" in c2["summary"]
    assert f"[t-finance-email · {P2}] closed:" in c2["summary"]
    assert c2["evicted_entries"] == []
    # The summarizer saw the previous state file, not the record before it.
    transcript_2 = json.dumps(summarizer.requests[1]["input"])
    assert "d-finance-eur" in transcript_2 and CSV_MARKER not in transcript_2

    # Phase 3 → compaction 3: the close is applied, the list is over budget, closed go first.
    await _phase_3(server)
    assert server.labels[f"{STATE_CLOSE_LABEL_PREFIX}t-q3-sales"]
    c3, reads = await _compact(P3)
    assert all(p.get("after") == c2["last_item_id"] for p, _ in reads if p.get("order") == "asc")
    assert first_id not in {i for _, ids in reads for i in ids}
    assert c3["applied_closes"] == [
        {"tag": "t-q3-sales", "reason": "the person said the Q3 sales topic is done"}
    ]
    assert server.labels[f"{STATE_CLOSE_LABEL_PREFIX}t-q3-sales"] == "", "close consumed"
    evicted = c3["evicted_entries"]
    assert evicted, "the list was over budget"
    assert all(" closed" in line for line in evicted), evicted
    assert evicted[0].startswith(f"- [o-report-mismatch · {P2}] closed:"), "oldest closed first"
    assert "[t-q3-sales" in c3["summary"] and "[o-contract-47" in c3["summary"]
    assert c3["repairs"] == 0
    assert EUR_LINE in c3["summary"].splitlines()
    for line in evicted:
        assert line not in c3["summary"]

    # Exactly three summarizer calls for three compactions.
    assert len(summarizer.requests) == 3
    assert len(server.compactions()) == 3

    # memory/: every journal line, the Round entry, the close and the evictions; STATE.md.
    journal = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((tmp_path / files.MEMORY_DIR / files.JOURNAL_DIR).glob("*.md"))
    )
    for item in stored:
        for line in item.journal_lines:
            assert line.split(" · ", 1)[1] in journal, line
    assert 'Helpers: "Q4 forecast" · Took 1m 35s · main' in journal
    assert "closed [t-q3-sales]: the person said the Q3 sales topic is done" in journal
    for line in evicted:
        assert files.EVICTED_PREFIX + line.removeprefix("- ") in journal, line
    state_md = (tmp_path / files.MEMORY_DIR / files.STATE_FILE).read_text(encoding="utf-8")
    assert state_md == f"{files.STATE_NOTE}\n\n{files.state_text_of(c3['summary'])}\n"

    # The end: "Send finance the Q4 numbers." on a cold start.
    _user(server, "Send finance the Q4 numbers.")
    history, view = await _cold_start_input(server)
    asc = _asc_reads(view)
    assert asc and all(p.get("after") == c3["last_item_id"] for p, _ in asc)
    assert first_id not in {i for _, ids in view.reads for i in ids}
    prompt = _pi_prompt(history)
    assert prompt.startswith("Conversation so far:")
    assert EUR_LINE in prompt, "the tagged list with the EUR decision reaches pi"
    assert "[o-contract-47 ·" in prompt
    assert f"tool: {FRAMING} files_write" in prompt and "contracts-tracker.md" in prompt
    assert prompt.rstrip().splitlines()[-3] == "user: Send finance the Q4 numbers."
    assert CSV_MARKER not in prompt and "Q3-0001" not in prompt


async def test_pi_replays_a_checkpoint_with_one_new_message() -> None:
    """A cold start right after a checkpoint with no kept turns: header, state, new message.

    pi replays history on a first turn only with two or more user messages. The checkpoint's
    header is a user-role message, so the state still reaches pi with a single new message.
    """
    history = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": CHECKPOINT_MARKER}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": EUR_LINE}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Send finance the Q4 numbers."}],
        },
    ]
    prompt = _pi_prompt(history)
    assert EUR_LINE in prompt and "Send finance the Q4 numbers." in prompt
