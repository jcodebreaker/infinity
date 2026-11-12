"""A harness turn that ends releases the tool calls it left parked.

When the CLI behind a harness dies mid-tool-call, ``run_turn`` ends with an error while the
SDK's tool handler is still parked on ``dispatch_tool``. Tearing the turn down must release
that handler so nothing outlives the turn.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext


@pytest.mark.asyncio
async def test_turn_teardown_cancels_tool_calls_still_parked() -> None:
    ctx = TurnContext("resp_cli_died", asyncio.Queue(), asyncio.Event())
    handler = asyncio.create_task(ctx.dispatch_tool("call_1", "sys_os_shell", "{}", "muse"))
    await asyncio.sleep(0)
    assert not handler.done()

    async def _run_turn_failed() -> None:
        return None

    run_task = asyncio.create_task(_run_turn_failed())
    await run_task
    heartbeat = asyncio.create_task(asyncio.sleep(3600))

    await HarnessApp()._teardown_turn(ctx, run_task, heartbeat)

    with pytest.raises(asyncio.CancelledError):
        await handler
