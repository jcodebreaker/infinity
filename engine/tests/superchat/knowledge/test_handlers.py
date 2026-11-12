"""The Muse's tools and the relay ops, answered from the host's index."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.knowledge import FEATURE
from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.handlers import handle_knowledge_tool
from tests.superchat.knowledge._support import SESSION, make_runtime, put_pdf


@pytest.fixture(autouse=True)
def _fresh_runtime():
    yield
    rt.reset_runtime()


def call(tool: str, **args) -> dict:
    out = asyncio.run(handle_knowledge_tool(HandlerCtx(tool, None, SESSION), args))
    return json.loads(out)


def test_search_returns_passages_with_file_page_and_thumbnail(tmp_path: Path, monkeypatch) -> None:
    put_pdf(make_runtime(tmp_path, monkeypatch))
    found = call("files_search", query="currency exposure", k=3)
    assert found["type"] == "file_search" and found["mode"] == "keyword"
    top = found["results"][0]
    assert (top["file_name"], top["page"], top["path"]) == (
        "finance.pdf",
        1,
        "your_files/finance.pdf",
    )
    assert top["thumbnail_url"] and len(top["file_id"]) == 32


def test_read_page_returns_text_and_an_image_envelope(tmp_path: Path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put_pdf(runtime)
    fid = call("files_search", query="currency")["results"][0]["file_id"]
    out = asyncio.run(
        handle_knowledge_tool(
            HandlerCtx("files_read_page", None, SESSION), {"file_id": fid, "page": 1}
        )
    )
    assert "image" in out and "Currency exposure" in out
    assert call("files_read_page", file_id=fid, page=9)["error"] == "Page not found"
    assert "page number" in call("files_read_page", file_id=fid, page=0)["error"]
    assert (
        "isn't in your files index" in call("files_read_page", file_id="f" * 32, page=1)["error"]
    )


def test_read_page_accepts_a_file_name_or_path(tmp_path: Path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    put_pdf(runtime)
    put_pdf(runtime, "your_files/uploads/2026-10-09/notes.pdf")
    for ref in (
        "your_files/finance.pdf",
        "finance.pdf",
        "Finance.PDF",
        "uploads/2026-10-09/notes.pdf",
    ):
        out = asyncio.run(
            handle_knowledge_tool(
                HandlerCtx("files_read_page", None, SESSION), {"path": ref, "page": 1}
            )
        )
        assert "Currency exposure" in out, ref
    # A name the model put in file_id is still read as a name.
    assert "Currency exposure" in json.dumps(call("files_read_page", file_id="notes.pdf", page=1))
    put_pdf(runtime, "your_files/uploads/other/notes.pdf")
    err = call("files_read_page", path="notes.pdf", page=1)["error"]
    assert "More than one file" in err and "uploads/2026-10-09/notes.pdf" in err
    assert "uploads/other/notes.pdf" in err
    assert "isn't in your files index" in call("files_read_page", path="nope.pdf", page=1)["error"]


def test_argument_errors_are_tool_errors(tmp_path: Path, monkeypatch) -> None:
    make_runtime(tmp_path, monkeypatch)
    assert call("files_search", query=" ")["error"]
    assert call("files_search", query="x", file_ids=["nope"])["error"]
    assert call("files_read_page", page=1)["error"]


def test_relay_ops_status_thumbnail_reindex(tmp_path: Path, monkeypatch) -> None:
    put_pdf(make_runtime(tmp_path, monkeypatch))
    status = call("files_status")
    assert status["files"][0]["state"] == "searchable" and status["files"][0]["abs_path"]
    fid = status["files"][0]["file_id"]
    assert call("files_thumbnail", file_id=fid, page=1)["image_mime"] == "image/webp"
    assert call("files_thumbnail", file_id=fid, page=4)["error"]
    assert call("files_reindex") == {"queued": 0}


def test_models_get_the_qmd_style_tools(tmp_path: Path) -> None:
    from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE

    labels = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
    names = [t.name() for t in FEATURE.tools(labels, None)]  # type: ignore[arg-type]
    assert names == [
        "files_search",
        "files_vsearch",
        "files_query",
        "files_get",
        "files_multi_get",
        "files_read_page",
        "files_status",
        "recall",
        "recall_get",
    ]
    assert FEATURE.tools({}, None) == []  # type: ignore[arg-type]
    assert {"files_ingest", "files_thumbnail", "files_reindex", "files_find"} <= set(
        FEATURE.handlers
    )


def test_a_saved_artifact_is_followed_through_on_result(tmp_path: Path, monkeypatch) -> None:
    runtime = make_runtime(tmp_path, monkeypatch)
    note = runtime.indexer.workspace / "projects" / "q3" / "summary.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Summary\nRevenue grew.\n")
    ctx = HandlerCtx("artifact_save", None, SESSION)
    FEATURE.on_result(ctx, json.dumps({"type": "artifact", "source_path": str(note)}))  # type: ignore[misc]
    assert runtime.indexer.db.file_by_path("projects/q3/summary.md") is not None
    FEATURE.on_result(HandlerCtx("other", None, SESSION), "{}")  # type: ignore[misc]


def test_relay_ops_pass_the_runners_granted_tool_gate_only_for_a_super_chat() -> None:
    """The relays reach the host through /mcp/execute; this gate once refused them, so every
    citation thumbnail 404ed."""
    from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE
    from omnigent.runner import tool_dispatch
    from omnigent.spec.types import AgentSpec

    spec = AgentSpec(spec_version=1)
    chat = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
    for name in ("files_ingest", "files_thumbnail", "files_reindex", "files_find"):
        relay = {"labels": chat, "relay": True}
        assert tool_dispatch._ungranted_tool_reason(name, spec, "claude-sdk", **relay) is None
        assert tool_dispatch._ungranted_tool_reason(
            name, spec, "claude-sdk", labels={}, relay=True
        )
        # a model's own call of the same name (no relay mark) is refused, even in a Super Chat
        assert tool_dispatch._ungranted_tool_reason(name, spec, "claude-sdk", labels=chat)


def test_a_journal_line_is_found_and_comes_back_as_file_data(tmp_path: Path, monkeypatch) -> None:
    from omnigent.runner.memory import files
    from tests.superchat.knowledge._fixtures import run_all

    runtime = make_runtime(tmp_path, monkeypatch)
    files.append_lines(
        runtime.indexer.workspace,
        [
            files.format_line(
                "2026-03-04", "Decided to move the launch to May", files.Origin("main", "c1")
            )
        ],
    )
    runtime.indexer.scan()
    run_all(runtime.indexer)

    found = call("files_query", query="move the launch", rerank=False)

    # The same file-search envelope as any other file: a tool result, never a turn instruction.
    assert found["type"] == "file_search"
    top = found["results"][0]
    assert top["path"] == "memory/journal/2026-03-04.md"
    assert "Decided to move the launch to May" in top["passage"]
    status = runtime.indexer.status()
    assert any(c["name"] == "memory" and c["searchable"] == 1 for c in status["collections"])
