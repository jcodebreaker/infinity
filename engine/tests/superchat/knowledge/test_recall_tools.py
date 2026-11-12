"""``recall`` and ``recall_get``: Recall's pull (ADR 0016), beside the old search tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.handlers import handle_knowledge_tool
from tests.superchat.knowledge._fixtures import put_turn, run_all
from tests.superchat.knowledge._support import SESSION, make_runtime

CLAIM = {"claim_id": "k1", "text": "Prefers the hotel venue", "kind": "preference",
         "last_confirmed": 1.79e9}  # fmt: skip
EXPLAIN = {"claim": CLAIM, "quote": "I like the hotel", "evidence": [],
           "supersedes": [{"claim_id": "k0"}], "superseded_by": None}  # fmt: skip
BUDGET = "your_files/launch-budget.md"
#: The calling chat's checkpoint: t1 left the window there; u2 is the current epoch.
CHECKPOINT = {"id": "cp1", "type": "compaction", "summary": "## Context checkpoint\n\nstate",
              "last_item_id": "a1"}  # fmt: skip


def _msg(item_id: str, role: str, text: str) -> dict:
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [{"type": "text", "text": text}],
    }


class Engine:
    """The engine routes recall reads through: memory search and explain, a chat's items."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        # the chat, oldest first: the turn t1 (person + reply), then the next turn u2
        self.items = [_msg("t1", "user", "Which venue?"), _msg("a1", "assistant", "The hotel."),
                      _msg("u2", "user", "Thanks"), CHECKPOINT]  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        self.paths.append(path)
        if path.endswith("/memory/search"):
            return httpx.Response(200, json={"results": [CLAIM]})
        if path.endswith("/claims/k1/explain"):
            return httpx.Response(200, json=EXPLAIN)
        if path == f"/v1/sessions/{SESSION}/items":
            ids = [i["id"] for i in self.items]
            at = ids.index(params["after"]) if params.get("after") in ids else None
            if params.get("order") == "asc":
                page = self.items[(at + 1 if at is not None else 0) :]
            else:
                page = list(reversed(self.items[: at if at is not None else len(ids)]))
            return httpx.Response(200, json={"data": page, "has_more": False})
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://engine")


@pytest.fixture(autouse=True)
def _fresh_runtime():
    yield
    rt.reset_runtime()


def call(tool: str, server: httpx.AsyncClient | None = None, **args) -> dict:
    out = asyncio.run(handle_knowledge_tool(HandlerCtx(tool, server, SESSION), args))
    return json.loads(out)


def _put(runtime, rel: str, text: str) -> None:
    path = runtime.indexer.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    runtime.indexer.scan()
    run_all(runtime.indexer)


def _stocked(tmp_path: Path, monkeypatch):
    runtime = make_runtime(tmp_path, monkeypatch)
    _put(runtime, BUDGET, "# Budget\n\nThe hotel venue costs 12k.\n")
    line = f"- 2026-09-30 · Checked the hotel venue quote · main <!-- c1 files:{BUDGET} -->"
    _put(runtime, "memory/journal/2026-09-30.md", f"# 2026-09-30\n\n## Journal\n\n{line}\n")
    put_turn(runtime.indexer, SESSION, "t1", "2026-10-01", "Which venue? The hotel venue.",
             refs={"files": [BUDGET]}, last_item_id="a1")  # fmt: skip
    return runtime


def test_recall_searches_every_source_by_default(tmp_path: Path, monkeypatch) -> None:
    runtime = _stocked(tmp_path, monkeypatch)
    put_turn(runtime.indexer, SESSION, "u2", "2026-10-02", "Thanks for the hotel venue.")
    engine = Engine()

    found = call("recall", engine.client(), queries=["hotel venue", "venue cost"], rerank=False)

    assert found["type"] == "recall"
    assert found["sources"] == ["conversation", "files", "journal", "memory"]
    best = {r["source"]: r for r in reversed(found["results"])}
    assert set(best) == {"files", "journal", "conversation", "memory"}
    assert best["conversation"]["id"] == "i_t1" and best["conversation"]["date"] == "2026-10-01"
    assert "i_u2" not in {r["id"] for r in found["results"]}  # the current epoch: in context
    assert best["journal"]["id"].startswith("ep_") and best["journal"]["line"] == 5
    assert best["journal"]["snippet"] == "- 2026-09-30 · Checked the hotel venue quote · main"
    assert best["files"]["id"] == f"f_{best['files']['file_id']}@1"
    assert best["memory"]["id"] == "c_k1"
    assert all(r["date"] for r in found["results"])


def test_recall_by_file_lists_what_touched_it_newest_first(tmp_path: Path, monkeypatch) -> None:
    _stocked(tmp_path, monkeypatch)

    found = call("recall", file=BUDGET, sources=["journal", "conversation"])

    assert [(r["source"], r["date"]) for r in found["results"]] == [
        ("conversation", "2026-10-01"),
        ("journal", "2026-09-30"),
    ]
    assert found["results"][1]["refs"] == {"files": [BUDGET]}
    assert "rerank" not in found


def test_recall_argument_errors(tmp_path: Path, monkeypatch) -> None:
    make_runtime(tmp_path, monkeypatch)
    assert "or a project, file or chat" in call("recall")["error"]
    assert "at most 3" in call("recall", queries=["a", "b", "c", "d"])["error"]
    assert "sources are" in call("recall", queries=["a"], sources=["web"])["error"]
    assert "since must be a date" in call("recall", queries=["a"], since="March")["error"]


def test_recall_get_reads_each_kind_of_id(tmp_path: Path, monkeypatch) -> None:
    runtime = _stocked(tmp_path, monkeypatch)
    engine = Engine()
    found = call("recall", engine.client(), queries=["hotel venue"], rerank=False)
    ids = {r["source"]: r["id"] for r in reversed(found["results"])}

    read = call("recall_get", engine.client(), ids=[*ids.values(), "x_1"])

    by_id = {r["id"]: r for r in read["results"]}
    turn = by_id["i_t1"]
    assert [m["id"] for m in turn["messages"]] == ["t1", "a1"]
    assert turn["chat_id"] == SESSION and turn["date"] == "2026-10-01"
    journal = by_id[ids["journal"]]
    assert journal["path"] == "memory/journal/2026-09-30.md" and journal["line"] == 5
    assert journal["refs"] == {"files": [BUDGET]}
    page = by_id[ids["files"]]
    assert page["path"] == BUDGET and "12k" in page["markdown"]
    claim = by_id["c_k1"]
    assert claim["quote"] == "I like the hotel" and claim["supersedes"] == [{"claim_id": "k0"}]
    assert "ids start with" in by_id["x_1"]["error"]
    assert f"/v1/sessions/{SESSION}/memory/claims/k1/explain" in engine.paths
    # one id alone, and a turn read without the engine comes from the index
    assert call("recall_get", id="i_t1")["results"][0]["note"] == "the turn as indexed"
    assert call("recall_get", id=f"f_{page['file_id']}")["results"][0]["pages"] == 1
    assert runtime.indexer.db.turn_chunks(["t1"])


def test_the_old_tools_never_return_a_turn(tmp_path: Path, monkeypatch) -> None:
    _stocked(tmp_path, monkeypatch)
    for tool in ("files_search", "files_query"):
        found = call(tool, query="hotel venue", rerank=False)
        assert {r["path"] for r in found["results"]} <= {BUDGET, "memory/journal/2026-09-30.md"}
        assert all("source" not in r for r in found["results"])
