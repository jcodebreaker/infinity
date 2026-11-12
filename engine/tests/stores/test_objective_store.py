"""Tests for :class:`SqlAlchemyObjectiveStore`."""

from __future__ import annotations

import uuid

import pytest

from omnigent.stores.objective_store import ProposalNotOpenError
from omnigent.stores.objective_store.sqlalchemy_store import SqlAlchemyObjectiveStore


def _id() -> str:
    return uuid.uuid4().hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyObjectiveStore:
    return SqlAlchemyObjectiveStore(db_uri)


def _objective(store: SqlAlchemyObjectiveStore, **kw: object) -> str:
    oid = _id()
    store.create(oid, user_id="alice", parent_session_id=_id(), title="Ship it", **kw)  # type: ignore[arg-type]
    return oid


def _accept(store: SqlAlchemyObjectiveStore, oid: str, plan: list[dict[str, str | None]]):
    p = store.set_open_proposal(_id(), oid, reason="r", plan=plan)
    return store.resolve_proposal(oid, p.id, accept=True)


def test_create_get_list_and_scheduled_task_lookup(store: SqlAlchemyObjectiveStore) -> None:
    parent, task = _id(), _id()
    oid = _id()
    store.create(
        oid,
        user_id="alice",
        parent_session_id=parent,
        title="T",
        description="d",
        due="2026-12-01",
        scheduled_task_id=task,
    )
    got = store.get(oid)
    assert got is not None and got.status == "active" and got.tasks == []
    assert (got.description, got.due) == ("d", "2026-12-01")
    assert store.get_by_scheduled_task_id(task).id == oid  # type: ignore[union-attr]
    assert [o.id for o in store.list(parent_session_id=parent)] == [oid]
    assert store.list(owner_user_id="bob") == []


def test_update_fields_and_status(store: SqlAlchemyObjectiveStore) -> None:
    oid = _objective(store, due="2026-12-01")
    out = store.update(oid, title="New", status="paused", due=None)
    assert (out.title, out.status, out.due) == ("New", "paused", None)  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        store.update(oid, status="bogus")


def test_status_only_update_keeps_due_and_other_proposals(store: SqlAlchemyObjectiveStore) -> None:
    keep = _objective(store, due="2026-12-01")
    other = _objective(store)
    store.set_open_proposal(_id(), keep, reason="a", plan=[{"id": None, "title": "x"}])
    out = store.update(other, status="archived")
    assert out.status == "archived"  # type: ignore[union-attr]
    assert store.get(keep).open_proposal is not None  # type: ignore[union-attr]
    assert store.update(keep, status="archived").due == "2026-12-01"  # type: ignore[union-attr]


def test_one_open_proposal_new_replaces_old(store: SqlAlchemyObjectiveStore) -> None:
    oid = _objective(store)
    first = store.set_open_proposal(_id(), oid, reason="a", plan=[{"id": None, "title": "x"}])
    second = store.set_open_proposal(_id(), oid, reason="b", plan=[{"id": None, "title": "y"}])
    got = store.get(oid)
    assert got.open_proposal.id == second.id  # type: ignore[union-attr]
    with pytest.raises(ProposalNotOpenError):
        store.resolve_proposal(oid, first.id, accept=True)


def test_accept_applies_plan_keeping_ids_and_progress(store: SqlAlchemyObjectiveStore) -> None:
    oid = _objective(store)
    obj = _accept(store, oid, [{"id": None, "title": t} for t in ("a", "b", "c")])
    a, b, c = obj.tasks  # type: ignore[union-attr]
    store.update_task(oid, b.id, status="done", note="did b")
    obj = _accept(
        store,
        oid,
        [{"id": c.id, "title": "c2"}, {"id": None, "title": "new"}, {"id": b.id, "title": "b"}],
    )
    tasks = obj.tasks  # type: ignore[union-attr]
    assert [t.title for t in tasks] == ["c2", "new", "b"]
    assert tasks[0].id == c.id and tasks[2].id == b.id and a.id not in {t.id for t in tasks}
    assert (tasks[2].status, tasks[2].note) == ("done", "did b")
    assert tasks[1].status == "pending" and tasks[1].id not in {a.id, b.id, c.id}
    assert obj.open_proposal is None  # type: ignore[union-attr]


def test_unknown_kept_id_rejected_and_nothing_written(store: SqlAlchemyObjectiveStore) -> None:
    oid = _objective(store)
    with pytest.raises(ValueError):
        store.set_open_proposal(_id(), oid, reason="r", plan=[{"id": _id(), "title": "x"}])
    assert store.get(oid).open_proposal is None  # type: ignore[union-attr]


def test_dismiss_leaves_plan_and_update_task_scoped(store: SqlAlchemyObjectiveStore) -> None:
    oid = _objective(store)
    obj = _accept(store, oid, [{"id": None, "title": "a"}])
    task = obj.tasks[0]  # type: ignore[union-attr]
    p = store.set_open_proposal(_id(), oid, reason="r", plan=[{"id": None, "title": "z"}])
    out = store.resolve_proposal(oid, p.id, accept=False)
    assert [t.title for t in out.tasks] == ["a"]  # type: ignore[union-attr]
    other = _objective(store)
    assert store.update_task(other, task.id, status="done") is None
    with pytest.raises(ValueError):
        store.update_task(oid, task.id, status="nope")
    assert store.update_task(oid, task.id, status="blocked", note="need x").note == "need x"  # type: ignore[union-attr]
