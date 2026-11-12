"""A Super Chat turn's setup reads once and its wait is logged in four chunks (ADR 0026)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import turn_timing
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

_SUPER_CHAT = {"omnigent.context.mode": "superside-chat"}


class _CountingServer(NullServerClient):
    """Answers ``/labels`` with a Super Chat's and counts the reads."""

    def __init__(self) -> None:
        self.labels_reads = 0

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        if url.endswith("/labels"):
            self.labels_reads += 1

            class _Labels(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"labels": _SUPER_CHAT}

            return _Labels()
        return await super().get(url, **kwargs)


_FRAMES = [
    _sse({"type": "response.created", "response": {"id": "resp_1"}}),
    _sse({"type": "response.output_text.delta", "delta": "Hi"}),
    _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
]


async def _turn(client: Any, app: Any, text: str) -> None:
    resp = await client.post(
        "/v1/sessions/conv_t/events",
        json={
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": [{"type": "input_text", "text": text}],
            "harness": "claude-sdk",
        },
    )
    assert resp.status_code == 202, resp.text
    for _ in range(300):
        if "conv_t" not in app.state.active_turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the turn did not finish")


@pytest.fixture
def _no_prefix_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_blocks(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr("omnigent.runner.app.turn_prefix_blocks", no_blocks)


@pytest.mark.usefixtures("_no_prefix_reads")
async def test_a_turn_reads_the_session_labels_once(caplog: pytest.LogCaptureFixture) -> None:
    """The hard-limit check, the idle return and the memory sync share one read."""
    server = _CountingServer()
    harness = _ScriptedHarnessClient(_FRAMES)
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")
    async with _runner_client(app) as client:
        await _turn(client, app, "first")
        before = server.labels_reads
        await _turn(client, app, "second")
        assert server.labels_reads - before == 1

    timings = [r.getMessage() for r in caplog.records if "turn timing for" in r.getMessage()]
    assert len(timings) == 2
    assert timings[-1].startswith("turn timing for conv_t: setup ")
    for chunk in ("prefix ", "client ", "first token "):
        assert chunk in timings[-1]


def test_the_four_chunks_sum_to_the_wait() -> None:
    marks = {"prefix": 10.5, "prefixed": 10.75, "client": 13.0}
    assert turn_timing(10.0, marks, 14.0) == (0.5, 0.25, 2.25, 1.0)


def test_a_missing_mark_reads_zero_and_nothing_goes_negative() -> None:
    # No client mark (the harness asked no LLM_REQUEST verdict): its chunk is zero.
    assert turn_timing(10.0, {"prefix": 10.5, "prefixed": 11.0}, 12.0) == (0.5, 0.5, 0.0, 1.0)
    # A plain turn has no prefix at all.
    assert turn_timing(10.0, {}, 12.0) == (0.0, 0.0, 0.0, 2.0)
