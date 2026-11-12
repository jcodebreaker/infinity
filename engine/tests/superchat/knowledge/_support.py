"""Shared pieces of the knowledge relay tests: a runtime on a temp workspace, a fake runner."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.handlers import handle_knowledge_tool
from omnigent.superchat.knowledge.runtime import KnowledgeRuntime
from tests.superchat.knowledge._fixtures import FakeEmbedder, make_pdf, run_all

SESSION = "a" * 32
PDF = make_pdf([[("Risks", 20), ("Currency exposure is the main risk.", 11)]])


def make_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder=None, reranker=None
) -> KnowledgeRuntime:
    """A runtime over ``tmp_path/workspace`` that is never started (tests drive the indexer)."""
    rt.reset_runtime()
    monkeypatch.setattr(KnowledgeRuntime, "start", lambda self: None)
    runtime = rt.get_runtime(
        lambda: KnowledgeRuntime(
            tmp_path / "home" / "knowledge",
            tmp_path / "workspace",
            embedder or FakeEmbedder(),
            reranker=reranker,
        )
    )
    (tmp_path / "workspace" / "your_files").mkdir(parents=True, exist_ok=True)
    return runtime


def put_pdf(runtime: KnowledgeRuntime, rel: str = "your_files/finance.pdf") -> None:
    path = runtime.indexer.workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PDF)
    runtime.indexer.scan()
    run_all(runtime.indexer)


class FakeRunner:
    """The runner's ``/mcp/execute`` over httpx: dispatches to the real handler."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)["params"]
        self.calls.append(body["name"])
        out = await handle_knowledge_tool(
            HandlerCtx(body["name"], None, SESSION), body["arguments"]
        )
        return httpx.Response(200, json={"result": {"output": out}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), base_url="http://runner")
