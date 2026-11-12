"""The index service: an upload is indexed at once, not on the next slow tick."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.db import STATE_READY, STATE_TEXT_READY
from omnigent.superchat.knowledge.runtime import KnowledgeRuntime
from tests.superchat.knowledge._fixtures import FakeEmbedder, make_pdf

PDF = make_pdf([[("Risks", 20), ("Currency exposure is the main risk.", 11)]])


@pytest.fixture(autouse=True)
def _fresh_runtime():
    rt.reset_runtime()
    yield
    rt.reset_runtime()


def _wait_for(check, seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def _read(runtime: KnowledgeRuntime, path: str) -> bool:
    row = runtime.indexer.db.file_by_path(path)
    return row is not None and row.state in (STATE_TEXT_READY, STATE_READY)


def test_an_upload_is_indexed_immediately_not_on_the_idle_tick(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "your_files").mkdir(parents=True)
    runtime = rt.get_runtime(
        # An idle tick far longer than the test: only the upload's nudge can explain the result.
        lambda: KnowledgeRuntime(tmp_path / "home", workspace, FakeEmbedder(), idle_seconds=600)
    )
    runtime.start()
    assert runtime.scanned.wait(8)
    upload = workspace / "your_files" / "vendor-review.pdf"
    upload.write_bytes(PDF)
    rt.notify_written(upload)
    assert _wait_for(
        lambda: (
            (row := runtime.indexer.db.file_by_path("your_files/vendor-review.pdf")) is not None
            and row.state in (STATE_TEXT_READY, STATE_READY)
        )
    )


def test_the_first_upload_starts_the_service_in_a_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "your_files").mkdir(parents=True)
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setattr(
        rt,
        "_build",
        lambda: KnowledgeRuntime(tmp_path / "home", workspace, FakeEmbedder(), idle_seconds=600),
    )
    upload = workspace / "your_files" / "a.pdf"
    upload.write_bytes(PDF)
    rt.notify_written(upload)  # no tool call has built the service yet
    runtime = rt.get_runtime()
    assert _wait_for(
        lambda: (
            (row := runtime.indexer.db.file_by_path("your_files/a.pdf")) is not None
            and row.state in (STATE_TEXT_READY, STATE_READY)
        )
    )
