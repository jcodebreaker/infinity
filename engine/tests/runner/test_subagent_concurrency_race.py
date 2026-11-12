"""Regression test: the superside-chat sub-agent concurrency cap must not
be overshoot-able by two concurrent dispatches from the same caller.

Both ``sys_session_send`` (new-child path) and ``sys_session_create`` fetch
the caller's live-sibling count, check it against the cap, and only then
create the new child. Without serializing that fetch-check-create span per
caller (``_subagent_concurrency_lock``), two concurrent calls can both read
the same (pre-create) count, both pass the check, and both create a child
— overshooting a cap of 1 to 2. This drives two real concurrent
``execute_tool`` calls against a mock server and asserts at most one
succeeds.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.superchat.subagents import DEFAULT_CONCURRENCY_ENV

CALLER = "conv_caller"
_SUPERSIDE_LABELS = {"omnigent.context.mode": "superside-chat"}


@pytest.mark.asyncio
async def test_concurrent_sys_session_create_calls_do_not_overshoot_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEFAULT_CONCURRENCY_ENV, "1")
    created: list[str] = []
    list_calls = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{CALLER}":
            # Top-level caller (not itself a sub-agent): nesting cap never
            # applies here, only the concurrency cap under test.
            return httpx.Response(200, json={"kind": "default"})
        if request.method == "GET" and path == f"/v1/sessions/{CALLER}/child_sessions":
            list_calls += 1
            # Widen the race window: without the per-caller lock, both
            # concurrent calls' list-children reads would interleave here
            # before either one's create POST lands.
            await asyncio.sleep(0.01)
            rows = [{"busy": True, "labels": {}, "title": f"child-{cid}"} for cid in created]
            return httpx.Response(200, json={"data": rows})
        if request.method == "POST" and path == "/v1/sessions":
            child_id = f"conv_child_{len(created)}"
            created.append(child_id)
            return httpx.Response(
                201,
                json={
                    "id": child_id,
                    "agent_id": "ag_x",
                    "agent_name": "worker",
                    "status": "idle",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://server"
    ) as server_client:
        outputs = await asyncio.gather(
            execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "ag_x", "title": "first"}),
                server_client=server_client,
                conversation_id=CALLER,
                labels=_SUPERSIDE_LABELS,
            ),
            execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "ag_x", "title": "second"}),
                server_client=server_client,
                conversation_id=CALLER,
                labels=_SUPERSIDE_LABELS,
            ),
        )

    parsed = [json.loads(o) for o in outputs]
    succeeded = [p for p in parsed if "error" not in p]
    refused = [p for p in parsed if "error" in p]
    # The cap (1) must hold: only one of the two concurrent calls may
    # actually create a child; the other is refused for concurrency.
    assert len(succeeded) == 1, parsed
    assert len(refused) == 1, parsed
    assert "concurrency" in refused[0]["error"] or "cap" in refused[0]["error"]
    assert len(created) == 1
