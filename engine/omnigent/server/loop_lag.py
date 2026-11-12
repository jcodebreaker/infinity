"""The engine's event loop never stalls (ADR 0026): an explicit default executor and a lag probe.

* :func:`start` sizes the loop's default executor (every ``asyncio.to_thread`` and
  ``run_in_executor(None, ...)``) to :data:`EXECUTOR_THREADS`. AnyIO's thread limiter, which
  FastAPI's sync routes use, is a separate pool.
* :func:`watch_loop_lag` sleeps 100 ms in a loop and logs a WARNING ``event loop lag <ms>ms``
  when a tick comes back more than 250 ms late, at most once per 5 s. A watchdog thread samples
  the loop thread's stack *while* the loop is blocked, so the line names what held it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

_logger = logging.getLogger(__name__)

EXECUTOR_THREADS = 64
TICK = 0.1
LAG_WARN = 0.25
LOG_EVERY = 5.0
_STACK_LINES = 25


def start(*, probe: bool = True) -> asyncio.Task[None] | None:
    """
    Size the running loop's default executor and start the lag probe.

    :param probe: ``False`` skips the probe.
    :returns: The probe task, for the caller to cancel at shutdown, or ``None``.
    """
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=EXECUTOR_THREADS, thread_name_prefix="engine")
    )
    return asyncio.create_task(watch_loop_lag()) if probe else None


def _stack(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return ""
    return "".join(traceback.format_stack(frame)[-_STACK_LINES:])


class _Watchdog:
    """Samples the loop thread's stack once per stall, from its own thread."""

    def __init__(self, loop_thread: int) -> None:
        self.loop_thread = loop_thread
        self.beat = time.monotonic()
        self.sample = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-lag-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(TICK):
            if not self.sample and time.monotonic() - self.beat > LAG_WARN:
                self.sample = _stack(self.loop_thread)


async def watch_loop_lag() -> None:
    """Run until cancelled, logging each lag over :data:`LAG_WARN` (rate-limited)."""
    watchdog = _Watchdog(threading.get_ident())
    watchdog.start()
    last_logged = -LOG_EVERY
    try:
        while True:
            watchdog.sample = ""
            watchdog.beat = start = time.monotonic()
            await asyncio.sleep(TICK)
            now = time.monotonic()
            lag = now - start - TICK
            if lag > LAG_WARN and now - last_logged >= LOG_EVERY:
                last_logged = now
                _logger.warning(
                    "event loop lag %dms; loop thread was in:\n%s",
                    round(lag * 1000),
                    watchdog.sample or "(not sampled)",
                )
    finally:
        watchdog.stop()
