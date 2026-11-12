"""A fire for an objective that is not active is skipped as ``objective_inactive``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.server.scheduled.fire import build_on_fire
from tests.server.scheduled.test_fire import _deps, _drain
from tests.server.scheduled.test_fire_parent_bound import (
    _bound_task,
    _clean_state,  # noqa: F401  (autouse fixture)
    _Parent,
    _ParentConversationStore,
    _Store,
)


@dataclass
class _Objective:
    status: str


class _Objectives:
    def __init__(self, objective: _Objective | None) -> None:
        self.objective = objective

    def get_by_scheduled_task_id(self, task_id: str) -> _Objective | None:
        return self.objective


async def _fire(status: str | None) -> tuple[_Store, list[Any]]:
    store = _Store({"task_1": _bound_task()})
    launches: list[Any] = []

    async def _launch(conv: Any, task: Any) -> None:
        launches.append(conv)

    deps = _deps(store, conversation_store=_ParentConversationStore(_Parent()))
    deps.objective_store = _Objectives(None if status is None else _Objective(status))
    await build_on_fire(deps, launch_dispatch=_launch)(0, "task_1")
    await _drain()
    return store, launches


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "done", "archived"])
async def test_inactive_objective_is_skipped(status: str) -> None:
    store, launches = await _fire(status)
    assert launches == []
    assert [(r["status"], r["error_code"]) for r in store.runs] == [
        ("skipped", "objective_inactive")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", None])
async def test_active_or_unlinked_objective_still_fires(status: str | None) -> None:
    store, launches = await _fire(status)
    assert len(launches) == 1
    assert all(r["error_code"] != "objective_inactive" for r in store.runs)
