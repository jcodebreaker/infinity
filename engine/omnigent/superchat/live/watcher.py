"""Polls the modified time of the files live pages read, and reports a change to the engine.

A thread of the runner in a managed host. Every ``INTERVAL_SECONDS`` it asks the engine which
files to watch (``GET /v1/live/watch``: only the sources of live pages, never the whole disk),
looks at their modified time and size, and posts the ones that changed (``POST /v1/live/changed``).
With the same call it says how busy the host is (free memory share, load per CPU), which sizes
how many update runs the engine starts at once. The engine debounces and starts the update. The
first look at a file only records it, so a restart starts nothing; nothing is seen while the
host sleeps (the schedule is the safety net). Files
change here without going through the engine (the AI or a shell writes them), which is why this
polls the disk instead of listening to uploads. Rules: ``docs/artifacts.md``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 30.0
ROOT_ENV = "SUPERCHAT_KNOWLEDGE_ROOT"

Stamp = tuple[int, int] | None


def stamp_of(path: Path) -> Stamp:
    """``(mtime_ns, size)``, or ``None`` when the file is not there."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _number(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):  # no such file, or "max"
        return None


def read_load() -> dict[str, float]:
    """``mem_free`` (share of memory free: the container's limit when it has one, else the
    machine's) and ``load`` (one-minute load per CPU); a reading it cannot take is left out."""
    out: dict[str, float] = {}
    limit, used = _number("/sys/fs/cgroup/memory.max"), _number("/sys/fs/cgroup/memory.current")
    if limit and used is not None:
        out["mem_free"] = round(max(0.0, 1 - used / limit), 3)
    else:
        try:
            info = dict(
                line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
            )
            total, free = (int(info[k].split()[0]) for k in ("MemTotal", "MemAvailable"))
            out["mem_free"] = round(free / total, 3)
        except (OSError, KeyError, ValueError, ZeroDivisionError):
            pass
    with contextlib.suppress(OSError):
        out["load"] = round(os.getloadavg()[0] / (os.cpu_count() or 1), 3)
    return out


class SourceWatcher:
    """Remembers each watched file's stamp and reports the ones that moved."""

    def __init__(
        self,
        server_url: Callable[[], str | None],
        credential: Callable[[], str | None],
        workspace: Path,
        *,
        interval: float = INTERVAL_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._server_url = server_url
        self._credential = credential
        self._workspace = workspace
        self._interval = interval
        self._transport = transport
        self._seen: dict[str, Stamp] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the thread (once)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="superchat-live", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end and wait for it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll()
            except Exception:
                _logger.debug("live: source poll failed", exc_info=True)

    def poll(self) -> list[str]:
        """One pass: the paths reported to the engine (none without a connection)."""
        base, credential = self._server_url(), self._credential()
        if not base or not credential:
            return []
        with httpx.Client(
            base_url=base.rstrip("/"),
            headers={"Authorization": f"Bearer {credential}"},
            timeout=15.0,
            transport=self._transport,
        ) as client:
            reply = client.get("/v1/live/watch", params=read_load()).raise_for_status()
            watched = reply.json()["paths"]
            changed = self.moved(watched)
            if changed:
                client.post("/v1/live/changed", json={"paths": changed}).raise_for_status()
        # Only after the engine took the report: a failed post is reported again next pass.
        for path in changed:
            self._seen[path] = stamp_of(self._workspace / path)
        return changed

    def moved(self, watched: list[str]) -> list[str]:
        """Paths whose stamp differs from the last look (a file that appeared counts; a first look
        and a file that went away do not). Records what it sees except for the changed ones."""
        self._seen = {p: v for p, v in self._seen.items() if p in watched}
        changed = []
        for path in watched:
            now = stamp_of(self._workspace / path)
            if path not in self._seen:
                self._seen[path] = now
            elif now != self._seen[path]:
                if now is None:
                    self._seen[path] = None
                else:
                    changed.append(path)
        return changed


def start_for_runner(server_url: str | None) -> SourceWatcher | None:
    """Start the watcher when this runner is a managed host (``IS_SANDBOX=1``)."""
    if os.environ.get("IS_SANDBOX") != "1" or not server_url:
        return None
    from omnigent.host.model_credential import read_credential

    root = Path(os.environ.get(ROOT_ENV) or Path.home() / "workspace")
    watcher = SourceWatcher(lambda: server_url, read_credential, root)
    watcher.start()
    return watcher
