"""The qmd-style tools: search, vsearch, query (+ rerank), get, multi_get, status, ingest."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.knowledge import handlers
from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.handlers import handle_knowledge_tool
from tests.superchat.knowledge._fixtures import FakeEmbedder, FakeReranker, make_pdf, run_all
from tests.superchat.knowledge._support import SESSION, make_runtime


@pytest.fixture(autouse=True)
def _fresh_runtime():
    yield
    rt.reset_runtime()


def call(tool: str, **args) -> dict:
    out = asyncio.run(handle_knowledge_tool(HandlerCtx(tool, None, SESSION), args))
    return json.loads(out)


def put(runtime, rel: str, data: bytes | str) -> None:
    path = runtime.indexer.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    runtime.indexer.scan()
    run_all(runtime.indexer)


NOTES = (
    "# Fleet\n\nThe vehicle policy covers every company car.\n\n"
    "# Cash\n\nCurrency exposure is reviewed each quarter by treasury.\n"
)


def test_search_is_bm25_and_takes_a_natural_language_question(tmp_path, monkeypatch) -> None:
    put(make_runtime(tmp_path, monkeypatch), "your_files/notes.md", NOTES)
    found = call("files_search", query="how often is the currency exposure reviewed?")
    assert found["type"] == "file_search" and found["mode"] == "keyword"
    assert found["results"][0]["heading"] == "Cash" and found["results"][0]["page"] == 1


def test_vsearch_finds_a_synonym_and_says_no_embeddings_without_a_key(
    tmp_path, monkeypatch
) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put(runtime, "your_files/notes.md", NOTES)
    found = call("files_vsearch", query="automobile")
    assert found["mode"] == "vector" and "company car" in found["results"][0]["passage"]
    assert call("files_search", query="automobile")["results"] == []
    rt.reset_runtime()
    keyless = make_runtime(tmp_path, monkeypatch, FakeEmbedder(reason="no_connection"))
    none = call("files_vsearch", query="automobile")
    assert none["reason"] == "no_embeddings" and none["results"] == []
    assert none["detail"] == "no_connection"
    assert keyless.indexer.db.files()


def test_query_reranks_the_fused_hits_with_the_model(tmp_path, monkeypatch) -> None:
    reranker = FakeReranker(prefer="treasury")
    runtime = make_runtime(tmp_path, monkeypatch, reranker=reranker)
    put(runtime, "your_files/notes.md", NOTES)
    plain = call("files_query", query="vehicle currency", rerank=False)
    assert "rerank" not in plain
    found = call("files_query", query="vehicle currency")
    assert found["rerank"] == "llm" and found["rerank_reason"] is None
    assert "treasury" in found["results"][0]["passage"]
    assert reranker.calls and reranker.calls[0][0] == "vehicle currency"
    scores = [r["score"] for r in found["results"]]
    assert scores == sorted(scores, reverse=True)


def test_query_keeps_the_fused_order_and_says_why_without_a_chat_connection(
    tmp_path, monkeypatch
) -> None:
    runtime = make_runtime(tmp_path, monkeypatch, reranker=FakeReranker(reason="no_connection"))
    put(runtime, "your_files/notes.md", NOTES)
    found = call("files_query", query="vehicle currency")
    assert found["rerank"] == "fused" and found["rerank_reason"] == "no_connection"
    assert found["results"]
    rt.reset_runtime()
    make_runtime(tmp_path, monkeypatch)  # no reranker at all
    assert call("files_query", query="vehicle currency")["rerank_reason"] == "no_connection"


def test_get_returns_markdown_with_page_markers_and_caps_it(tmp_path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    pdf = make_pdf([[("Intro", 20), ("First page text.", 11)], [("Second page text.", 11)]])
    put(runtime, "your_files/doc.pdf", pdf)
    whole = call("files_get", path="doc.pdf")
    assert "<!-- page 1 -->" in whole["markdown"] and "<!-- page 2 -->" in whole["markdown"]
    assert "# Intro" in whole["markdown"] and whole["pages"] == 2 and not whole["truncated"]
    second = call("files_get", path="doc.pdf", pages="2")
    assert "Second page" in second["markdown"] and "First page" not in second["markdown"]
    assert "Second page" in call("files_get", path="doc.pdf", pages=2)["markdown"]
    assert "pages 1 to 2" in call("files_get", path="doc.pdf", pages="9")["error"]
    big = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(400))
    put(runtime, "your_files/big.txt", big)
    cut = call("files_get", path="big.txt", max_chars=5000)
    assert cut["truncated"] and len(cut["markdown"]) <= 5000 and "Cut at" in cut["note"]
    assert call("files_get", path="big.txt", max_chars=10**9)["chars"] > 0
    assert call("files_get", path="nope.txt")["error"]


def test_multi_get_matches_a_glob_and_shares_the_cap(tmp_path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put(runtime, "your_files/a.md", "# A\n\nalpha\n")
    put(runtime, "your_files/b.md", "# B\n\nbeta\n")
    put(runtime, "your_files/c.txt", "gamma\n")
    found = call("files_multi_get", glob="your_files/*.md")
    assert found["matched"] == 2 and [f["name"] for f in found["files"]] == ["a.md", "b.md"]
    assert "alpha" in found["files"][0]["markdown"]
    assert call("files_multi_get", glob="c.txt,a.md")["returned"] == 2
    assert call("files_multi_get", glob="*.zip")["returned"] == 0
    assert call("files_multi_get", glob=" ")["error"]
    put(runtime, "your_files/long1.txt", "x " * 20000)
    put(runtime, "your_files/long2.txt", "y " * 20000)
    two = call("files_multi_get", glob="long*.txt", max_chars=4000)
    assert sum(len(f["markdown"]) for f in two["files"]) <= 4000 and two["files"][0]["truncated"]


def test_status_lists_collections_files_and_the_embedding_mode(tmp_path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put(runtime, "your_files/uploads/2026-10-09/n.md", NOTES)
    put(runtime, "goals/q3/files/plan.md", "# Plan\n\nship it\n")
    status = call("files_status")
    names = {c["name"]: c for c in status["collections"]}
    assert names["your_files"]["files"] == 1 and names["goals/q3"]["searchable"] == 1
    assert {f["collection"] for f in status["files"]} == {"your_files", "goals/q3"}
    assert status["embeddings"] == {"available": True, "reason": None}


def test_ingest_reads_the_file_now_and_never_returns_its_text_over_the_relay(
    tmp_path, monkeypatch
) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    path = runtime.indexer.workspace / "your_files/uploads/2026-10-09/r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(make_pdf([[("Risks", 20), ("Currency exposure.", 11)], [("More.", 11)]]))
    got = call("files_ingest", path="your_files/uploads/2026-10-09/r.pdf")
    assert got["pages"] == 2 and got["chars"] > 0
    assert "<!-- page 2 -->" in Path(got["markdown_path"]).read_text()
    assert Path(got["markdown_path"]).read_text().count("<!-- page") == 2
    assert Path(got["markdown_path"]).parent.name == "md"
    assert "markdown" not in got
    assert call("files_search", query="currency exposure")["results"]  # searchable at once
    assert call("files_ingest", path="your_files/uploads/nope.pdf")["error"]
    assert call("files_ingest", path="your_files/uploads/pic.png")["error"]


def test_ingest_of_a_long_file_leaves_the_markdown_out_of_the_message(
    tmp_path, monkeypatch
) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put(runtime, "your_files/uploads/2026-10-09/long.txt", "word " * 20000)
    got = call("files_ingest", path="your_files/uploads/2026-10-09/long.txt")
    assert "markdown" not in got and got["chars"] > 32000


def test_a_failed_file_reports_and_a_retry_reads_it_again(tmp_path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    rel = "your_files/uploads/2026-10-09/bad.pdf"
    path = runtime.indexer.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a pdf")
    assert "not a readable PDF" in call("files_ingest", path=rel)["error"]
    path.write_bytes(make_pdf([[("Fixed", 20), ("Now it reads.", 11)]]))
    assert call("files_ingest", path=rel)["pages"] == 1


def test_the_search_wait_and_its_polling_are_gone() -> None:
    source = Path(handlers.__file__).read_text()
    indexer = Path(rt.__file__).with_name("indexer.py").read_text()
    for gone in ("SEARCH_WAIT_SECONDS", "still_indexing", "unsearchable", "time.sleep"):
        assert gone not in source and gone not in indexer, gone
    assert not re.search(r"while .*deadline", source)


def test_ingest_only_reads_uploads_and_never_leaves_the_folder(tmp_path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put(runtime, "your_files/private.md", "# Secret\n\nSalary table.\n")
    put(runtime, "goals/q3/files/plan.md", "# Plan\n\nBudget.\n")
    outside = tmp_path / "workspace" / "your_files" / "uploads" / "2026-10-09"
    outside.mkdir(parents=True)
    (outside / "link.md").symlink_to(tmp_path / "workspace" / "your_files" / "private.md")
    for path in (
        "your_files/private.md",
        "goals/q3/files/plan.md",
        "your_files/uploads/../private.md",
        "your_files/uploads/2026-10-09/link.md",  # a symlink out of the folder
        "/etc/hosts",
    ):
        got = call("files_ingest", path=path)
        assert "uploaded file" in got.get("error", "") or got.get("error"), path
        assert "markdown" not in got, path
