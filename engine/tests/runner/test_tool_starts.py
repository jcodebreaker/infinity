"""A tool call is stored when it starts, not when it finishes (ADR 0026)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx

from omnigent.runner.app import _session_event_queues_ref, create_runner_app
from omnigent.runner.tool_starts import ToolStarts
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

_TOOL_SECONDS = 5.0


def _call(status: str, call_id: str = "call_1") -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "status": status,
            "name": "deck_new",
            "arguments": '{"title": "Acme vs Globex"}',
            "call_id": call_id,
        },
    }


_OUTPUT = {
    "type": "response.output_item.done",
    "item": {"type": "function_call_output", "call_id": "call_1", "output": "built"},
}


def test_the_first_frame_of_a_call_is_stored_once() -> None:
    starts = ToolStarts()

    stored = starts.started(_call("in_progress"))
    assert stored == {**_call("completed")}
    # The dispatch frame that follows, and the completed repeat, add nothing.
    assert starts.started(_call("action_required")) is None
    assert not starts.is_repeat(_call("action_required"))
    assert starts.is_repeat(_call("completed"))
    # Other frames pass through untouched.
    assert starts.started(_OUTPUT) is None
    assert not starts.is_repeat(_OUTPUT)


def test_a_call_reported_only_when_finished_passes_through() -> None:
    starts = ToolStarts()

    assert starts.started(_call("completed", "call_2")) is None
    assert not starts.is_repeat(_call("completed", "call_2"))
    assert starts.is_repeat(_call("completed", "call_2"))


class _TimedHarnessStream:
    """Harness stream whose tool runs for :data:`_TOOL_SECONDS` between its start and output."""

    status_code = 200

    async def __aenter__(self) -> _TimedHarnessStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def aiter_text(self) -> AsyncIterator[str]:
        def frame(event: dict[str, Any]) -> str:
            return f"data: {json.dumps(event)}\n\n"

        yield frame({"type": "response.created", "response": {"id": "resp_1"}})
        yield frame(_call("in_progress"))
        await asyncio.sleep(_TOOL_SECONDS)
        yield frame(_call("completed"))
        yield frame(_OUTPUT)
        yield frame({"type": "response.completed", "response": {"id": "resp_1"}})


class _TimedHarnessClient:
    def stream(self, method: str, url: str, *, json: object, timeout: object) -> Any:
        del method, url, json, timeout
        return _TimedHarnessStream()


class _ProcessManager:
    async def get_client(self, *_: object, **__: object) -> _TimedHarnessClient:
        return _TimedHarnessClient()

    def mark_in_flight(self, *_: object) -> None:
        return None

    def clear_in_flight(self, *_: object) -> None:
        return None

    async def release(self, *_: object, **__: object) -> None:
        return None


async def test_a_running_tool_is_stored_before_its_output_by_the_tool_time() -> None:
    conv = "conv_tool_starts"
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, _ProcessManager()),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    queue = _session_event_queues_ref.setdefault(conv, asyncio.Queue())
    seen: list[tuple[float, dict[str, Any]]] = []

    async def watch() -> None:
        while True:
            event = await queue.get()
            if isinstance(event, dict):
                seen.append((time.monotonic(), event))

    watcher = asyncio.create_task(watch())
    try:
        async with _runner_client(app) as http:
            response = await http.post(
                f"/v1/sessions/{conv}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "harness": "runner-test-default",
                    "model": "x",
                    "content": [],
                },
                timeout=httpx.Timeout(30.0),
            )
        await asyncio.sleep(0)
    finally:
        watcher.cancel()
        _session_event_queues_ref.pop(conv, None)

    assert response.status_code == 200
    stored = [
        (at, event)
        for at, event in seen
        if event.get("item", {}).get("type") == "function_call"
        and event["item"].get("status") == "completed"
    ]
    outputs = [
        at for at, event in seen if event.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(stored) == 1, "the completed repeat must not be stored a second time"
    assert stored[0][1]["item"]["arguments"] == '{"title": "Acme vs Globex"}'
    assert len(outputs) == 1
    assert outputs[0] - stored[0][0] >= _TOOL_SECONDS - 0.1
    # The stream the engine reads carries the same single stored call.
    assert response.text.count('"status": "completed"') == 1


def test_a_call_that_never_returned_is_closed_as_stopped() -> None:
    from omnigent.runner.tool_starts import STOPPED_OUTPUT

    starts = ToolStarts()
    starts.started(_call("in_progress", "call_done"))
    starts.started(_call("in_progress", "call_cut"))
    starts.is_repeat(
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call_output", "call_id": "call_done", "output": "ok"},
        }
    )

    closing = starts.unfinished()
    assert [frame["item"]["call_id"] for frame in closing] == ["call_cut"]
    assert closing[0]["item"]["output"] == STOPPED_OUTPUT
    assert starts.unfinished() == []
