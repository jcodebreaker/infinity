"""The work queue (ADR 0038): idempotent submit, claims within limits, and a restart."""

from __future__ import annotations

import time
from typing import Any

import pytest

from omnigent.server import work_queue
from omnigent.server.work_queue import Defer, SqlAlchemyWorkQueueStore, WorkQueue


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyWorkQueueStore:
    return SqlAlchemyWorkQueueStore(db_uri)


@pytest.fixture(autouse=True)
def _handlers() -> Any:
    saved = dict(work_queue.queue._HANDLERS)
    yield
    work_queue.queue._HANDLERS.clear()
    work_queue.queue._HANDLERS.update(saved)


def test_submit_is_idempotent_while_open_and_rearms_once_finished(
    store: SqlAlchemyWorkQueueStore,
) -> None:
    queue = WorkQueue(store)
    first, created = queue.put("ana", "k", "ana", {"n": 1})
    assert created
    assert queue.put("ana", "k", "ana", {"n": 2}) == (first, False)
    assert queue.put("ana", "other", "ana")[1]  # another kind, another job
    work_queue.register_handler("k", _noop)
    work_queue.register_handler("other", _noop)
    (job, _) = sorted(_claim(queue), key=lambda j: j.kind)
    assert job.payload == {"n": 1} and job.attempts == 1
    assert store.finish(job.workspace_id, job.id, "done", int(time.time()))
    again, created = queue.put("ana", "k", "ana", {"n": 3})
    assert (again, created) == (first, True)


def test_claims_respect_the_limits_and_never_hand_out_a_job_twice(
    store: SqlAlchemyWorkQueueStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    work_queue.register_handler("k", _noop)
    queue = WorkQueue(store, run_status=lambda cid: "running" if cid == "busy-chat" else "")
    for i in range(5):
        queue.submit("ana", "k", f"a{i}")
    queue.submit("bo", "k", "b0", conversation_id="busy-chat")
    queue.submit("bo", "k", "b1", conversation_id="busy-chat")
    queue.submit("cy", "k", "c0", owner_limit=1)
    queue.submit("cy", "k", "c1", owner_limit=1)
    claimed = _claim(queue)
    per_owner = {o: sum(j.owner == o for j in claimed) for o in ("ana", "bo", "cy")}
    # ana: the default three; bo: one while their turn runs; cy: the job's own limit.
    assert per_owner == {"ana": 3, "bo": 1, "cy": 1}
    assert _claim(queue) == []  # nothing is handed out twice, the limits still hold
    monkeypatch.setenv(work_queue.queue.GLOBAL_LIMIT_ENV, "1")
    for job in claimed:
        store.finish(job.workspace_id, job.id, "done", int(time.time()))
    assert len(_claim(queue)) == 1  # one across everyone


async def test_a_restart_keeps_waiting_jobs_frees_dead_claims_and_follows_bound_runs(
    store: SqlAlchemyWorkQueueStore,
) -> None:
    started: list[str] = []

    async def launch(job: work_queue.Job) -> str | Defer:
        started.append(job.key)
        if job.key == "later":
            return Defer(time.time() + 3600, {"tries": 1})
        return f"run-{job.key}"

    work_queue.register_handler("k", launch)
    before = WorkQueue(store)
    before.submit("ana", "k", "now")
    before.submit("ana", "k", "later")
    before.submit("ana", "k", "dead")
    await before.drain()
    assert sorted(started) == ["dead", "later", "now"]
    assert before.is_bound("run-now")

    # The process died while "dead" was still launching: put it back to claimed.
    dead = next(j for j in store.running() if j.key == "dead")
    with store._session("test") as session:
        from omnigent.db.db_models import SqlWorkJob

        row = session.get(SqlWorkJob, (dead.workspace_id, dead.id))
        assert row is not None
        row.state, row.run_id, row.lease_until = "claimed", None, int(time.time()) - 1

    # A new process over the same table.
    statuses = {"run-now": "running"}
    after = WorkQueue(store, run_status=lambda run_id: statuses.get(run_id, ""))
    assert after.reap() == 1  # the dead claim is queued again
    assert after.is_bound("run-now")  # learned from the table
    await after.drain()
    assert started.count("dead") == 2 and started.count("now") == 1
    assert started.count("later") == 1  # deferred an hour: still waiting, not lost
    later = next(j for j in _all(store) if j.key == "later")
    assert later.state == "queued" and later.payload == {"tries": 1}
    assert after.run_ended("run-now", ok=True)
    assert not after.run_ended("run-now", ok=True)  # finished once


async def _noop(_job: work_queue.Job) -> None:
    return None


def _claim(queue: WorkQueue) -> list[work_queue.Job]:
    now = int(time.time())
    return queue.store.claim(
        now=now,
        kinds=list(work_queue.queue._HANDLERS),
        global_limit=work_queue.global_limit(),
        owner_cap=queue.owner_cap,
        claimer=queue.claimer,
        lease_until=now + 600,
    )


def _all(store: SqlAlchemyWorkQueueStore) -> list[work_queue.Job]:
    from sqlalchemy import select

    from omnigent.db.db_models import SqlWorkJob
    from omnigent.server.work_queue.store import _to_job

    with store._session("test") as session:
        return [_to_job(r) for r in session.execute(select(SqlWorkJob)).scalars().all()]
