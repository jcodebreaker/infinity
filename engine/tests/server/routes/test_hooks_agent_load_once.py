"""The policy hook loads a cold agent bundle once per process, off the loop (ADR 0026)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from omnigent.entities import Agent, LoadedAgent
from omnigent.server.routes.sessions import routes_hooks


class _CountingCache:
    """An agent cache that memoises like :class:`AgentCache` and counts extractions."""

    def __init__(self) -> None:
        self.extractions = 0
        self._loaded: dict[str, LoadedAgent] = {}

    def load(self, agent_id: str, bundle_location: str, *, expand_env: bool) -> LoadedAgent:
        del bundle_location, expand_env
        if agent_id in self._loaded:
            return self._loaded[agent_id]
        self.extractions += 1
        time.sleep(0.05)  # a tar extraction
        loaded = LoadedAgent(spec=object(), workdir=Path("/nonexistent"))  # type: ignore[arg-type]
        self._loaded[agent_id] = loaded
        return loaded


def test_concurrent_cold_loads_extract_once() -> None:
    cache = _CountingCache()
    agent = Agent(id="ag_cold_once", created_at=0, name="cold", bundle_location="ag_cold_once/abc")
    results: list[Any] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(routes_hooks._load_agent_once(cache, agent))  # type: ignore[arg-type]
        )
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert cache.extractions == 1
    assert len(results) == 4 and all(r is results[0] for r in results)
