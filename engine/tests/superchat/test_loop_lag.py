"""The engine's loop never stalls unseen (ADR 0026): the lag probe and the executor size."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from omnigent.server import loop_lag


def _blocker() -> None:
    time.sleep(0.4)


async def test_a_blocked_loop_is_logged_with_what_blocked_it(caplog) -> None:
    caplog.set_level(logging.WARNING, logger=loop_lag.__name__)
    probe = asyncio.create_task(loop_lag.watch_loop_lag())
    await asyncio.sleep(0.15)
    _blocker()
    await asyncio.sleep(0.15)
    _blocker()  # a second stall within 5 s is not logged again
    await asyncio.sleep(0.15)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    lines = [r.getMessage() for r in caplog.records if "event loop lag" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("event loop lag ") and "_blocker" in lines[0]


async def test_start_sizes_the_executor_and_runs_the_probe() -> None:
    loop = asyncio.get_running_loop()
    probe = loop_lag.start()
    executor = loop._default_executor  # type: ignore[attr-defined]
    assert executor._max_workers == loop_lag.EXECUTOR_THREADS == 64
    assert probe is not None and not probe.done()
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe


async def test_start_can_skip_the_probe() -> None:
    assert loop_lag.start(probe=False) is None
    assert asyncio.get_running_loop()._default_executor is not None  # type: ignore[attr-defined]
