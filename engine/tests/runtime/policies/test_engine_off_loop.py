"""
Policy evaluation keeps its database writes off the event loop (ADR 0026).

A policy that counts tool calls writes session state on every counted call.
The write takes the session row lock (``SELECT … FOR UPDATE``), so a slow or
contended write must wait on a worker thread, never on the loop, and the row
lock must be released inside that one thread call.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import engine as engine_mod
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PhaseSelector

_SESSION_ID = "conv_off_loop"


class _GatedStore:
    """
    A conversation store whose state write holds a row lock until the loop runs.

    ``set_session_state`` takes ``row_lock`` (standing in for the session's
    ``FOR UPDATE``) and then waits for ``loop_ran``, which only a task on the
    event loop sets. Were the write on the loop, that task could never run and
    the wait would time out.
    """

    def __init__(self) -> None:
        self.row_lock = threading.Lock()
        self.entered = threading.Event()
        self.loop_ran = threading.Event()
        self.write_threads: list[int] = []
        self.labels: dict[str, str] = {}
        self.state: dict[str, Any] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        assert conversation_id == _SESSION_ID
        self.labels.update(updates)

    def set_session_state(self, conversation_id: str, state: dict[str, Any]) -> None:
        assert conversation_id == _SESSION_ID
        with self.row_lock:
            self.write_threads.append(threading.get_ident())
            self.entered.set()
            if not self.loop_ran.wait(timeout=5):
                raise AssertionError("the state write blocked the event loop")
            self.state = dict(state)


def _counting_policy() -> FunctionPolicy:
    """A TOOL_CALL policy that counts calls in session state and labels the session."""

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        count = int(event["session_state"].get("calls", 0)) + 1
        return {
            "result": "ALLOW",
            "set_labels": {"counted": "yes"},
            "state_updates": [{"key": "calls", "action": "set", "value": count}],
        }

    spec = FunctionPolicySpec(
        name="count_calls",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


def _engine(store: _GatedStore) -> PolicyEngine:
    return PolicyEngine(
        policies=[_counting_policy()],
        label_defs={},
        ask_timeout=30,
        conversation_id=_SESSION_ID,
        initial_labels={},
        conversation_store=store,  # type: ignore[arg-type]
    )


def _tool_call() -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": "deck_build", "arguments": {}},
        tool_name="deck_build",
    )


async def _release_when_entered(store: _GatedStore) -> bool:
    """Set ``loop_ran`` from the loop once the write is in progress; report the lock state."""
    while not store.entered.is_set():
        await asyncio.sleep(0.001)
    held_while_loop_ran = store.row_lock.locked()
    store.loop_ran.set()
    return held_while_loop_ran


@pytest.mark.asyncio
async def test_slow_state_write_does_not_block_the_loop() -> None:
    """A write waiting on the row lock leaves the loop free to run other tasks."""
    store = _GatedStore()
    engine = _engine(store)

    releaser = asyncio.create_task(_release_when_entered(store))
    result = await engine.evaluate(_tool_call())

    assert await releaser is True
    assert result.action.name == "ALLOW"
    assert store.state == {"calls": 1}
    assert store.labels == {"counted": "yes"}
    assert engine.session_state == {"calls": 1}
    assert engine.labels == {"counted": "yes"}


@pytest.mark.asyncio
async def test_row_lock_is_released_inside_the_worker_thread() -> None:
    """The row lock is taken and released off the loop, before evaluate resumes."""
    store = _GatedStore()
    engine = _engine(store)
    loop_thread = threading.get_ident()

    releaser = asyncio.create_task(_release_when_entered(store))
    await engine.evaluate(_tool_call())
    await releaser

    assert store.write_threads and loop_thread not in store.write_threads
    assert not store.row_lock.locked()


@pytest.mark.asyncio
async def test_read_only_evaluation_writes_nothing() -> None:
    """A read-only evaluation makes no thread hop and touches no row."""
    store = _GatedStore()
    engine = _engine(store)

    result = await engine.evaluate(_tool_call(), read_only=True)

    assert result.state_updates
    assert store.write_threads == []
    assert engine.session_state == {}


@pytest.mark.asyncio
async def test_slow_evaluation_and_slow_write_are_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Over the thresholds: one INFO line per evaluation and one WARNING per write."""
    monkeypatch.setattr(engine_mod, "_SLOW_EVALUATION_MS", -1)
    monkeypatch.setattr(engine_mod, "_SLOW_WRITE_MS", -1)
    store = _GatedStore()
    store.loop_ran.set()
    engine = _engine(store)

    with caplog.at_level(logging.INFO, logger=engine_mod.__name__):
        await engine.evaluate(_tool_call())

    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(infos) == 1
    assert infos[0].getMessage().startswith(f"policy evaluation for {_SESSION_ID} ")
    assert infos[0].getMessage().endswith("ms (tool_call)")
    assert getattr(infos[0], "session_id", None) == _SESSION_ID
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith(f"policy state write for {_SESSION_ID} waited ")
    assert getattr(warnings[0], "session_id", None) == _SESSION_ID


@pytest.mark.asyncio
async def test_fast_evaluation_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Under the thresholds the policy path stays quiet."""
    store = _GatedStore()
    store.loop_ran.set()
    engine = _engine(store)

    with caplog.at_level(logging.INFO, logger=engine_mod.__name__):
        await engine.evaluate(_tool_call())

    assert [r for r in caplog.records if r.name == engine_mod.__name__] == []
