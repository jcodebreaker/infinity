"""Eval persons (ADR 0043): who they are, their release after a run, and their deletion.

* :class:`EvalPersons`, the registry: one ``eval_person`` row per run's eval person in the
  generic ``preferences`` table (``{"created_at", "released_at"}`` under their user id). Written
  when the person first asks for their Conversation (``GET /v1/me/muse``) and on release;
  deleting the person deletes the row.
* :func:`release_person`, when a run ends (``POST /v1/eval-persons/{email}/release`` from
  evalution): the person's host is stopped and removed through the
  engine's host provider (:func:`omnigent.server.hosts.remove_host`, home kept), their scheduled
  tasks are paused so nothing they made (a live page, a brief) keeps spending, and the registry
  marks them released.
* :class:`EvalPeopleSweep`, a daily background job: every registered eval person whose newest
  activity (registered, released, their sessions' last update) is older than :data:`KEEP_DAYS`
  days is deleted, with their host and home, Conversations, memory, artifacts and every other
  row, by the admin capability's person delete (``app.state.remove_person``). Only people in the
  registry who are also ``eval+<run>@<eval domain>`` identities are ever touched, never an address
  that merely looks like one, and every removal is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

from sqlalchemy import select

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.evals.identity import run_of
from omnigent.server.hosts import remove_host

_logger = logging.getLogger(__name__)

KEEP_DAYS = 7
SWEEP_SECONDS = 24 * 3600
#: The first sweep waits this long after start, so a restart loop never deletes in a burst.
FIRST_SWEEP_SECONDS = 600
#: The registry's ``preferences`` key.
REGISTRY_KEY = "eval_person"


class EvalPersons:
    """The registry of eval persons: an ``eval_person`` row per person in ``preferences``."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.eval_persons"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )
        #: People registered by this replica, so a repeated ``/v1/me/muse`` costs no write.
        self._known: set[tuple[int, str]] = set()

    def register(self, person: str) -> None:
        """Add *person* (a run's eval person; anyone else is ignored), keeping the first time."""
        if run_of(person) is None or (current_workspace_id(), person) in self._known:
            return

        def write(session: Any) -> None:
            if session.get(SqlPreference, (current_workspace_id(), person, REGISTRY_KEY)) is None:
                value = json.dumps({"created_at": now_epoch(), "released_at": None})
                session.add(SqlPreference(user_id=person, key=REGISTRY_KEY, value=value))

        run_write_transaction(self._session_immediate, "register_eval_person", write)
        self._known.add((current_workspace_id(), person))

    def release(self, person: str) -> None:
        """Mark *person* released now, registering them first if they were not."""
        if run_of(person) is None:
            return

        def write(session: Any) -> None:
            row = session.get(SqlPreference, (current_workspace_id(), person, REGISTRY_KEY))
            now = now_epoch()
            if row is None:
                value = {"created_at": now, "released_at": now}
                session.add(
                    SqlPreference(user_id=person, key=REGISTRY_KEY, value=json.dumps(value))
                )
            else:
                row.value = json.dumps({**_value(row.value), "released_at": now})

        run_write_transaction(self._session_immediate, "release_eval_person", write)
        self._known.add((current_workspace_id(), person))

    def all(self) -> dict[str, dict[str, Any]]:
        """Every registered person with ``{"created_at", "released_at"}`` (epoch or ``None``)."""
        with self._session("select_eval_persons") as session:
            rows = session.execute(
                select(SqlPreference.user_id, SqlPreference.value)
                .where(SqlPreference.workspace_id == current_workspace_id())
                .where(SqlPreference.key == REGISTRY_KEY)
            ).all()
        return {user_id: _value(raw) for user_id, raw in rows}


def _value(raw: str) -> dict[str, Any]:
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


async def release_person(state: Any, person: str) -> None:
    """Remove *person*'s host (home kept), pause their scheduled tasks and mark them released
    in the registry (``state.eval_persons``); never raises.

    A no-op for anyone who is not a run's eval person, or without app state.
    """
    if state is None or run_of(person) is None:
        return
    try:
        host_store = getattr(state, "host_store", None)
        hosts = await asyncio.to_thread(host_store.list_hosts, person) if host_store else []
        removed = 0
        for host in hosts:
            if host.sandbox_provider is not None:
                ok = await remove_host(
                    host, host_store, getattr(state, "sandbox_config", None), home=False
                )
                removed += int(ok)
        paused = await _pause_tasks(state, person)
        registry = getattr(state, "eval_persons", None)
        if registry is not None:
            await asyncio.to_thread(registry.release, person)
        _logger.info(
            "evals: %s released: %d host(s) removed, %d task(s) paused", person, removed, paused
        )
    except Exception:
        _logger.warning("evals: could not release %s", person, exc_info=True)


async def _pause_tasks(state: Any, person: str) -> int:
    deps = getattr(state, "fire_deps", None)
    store = getattr(deps, "scheduled_task_store", None)
    scheduler = getattr(state, "scheduled_task_scheduler", None)
    if store is None:
        return 0
    paused = 0
    for task in await asyncio.to_thread(store.list, owner_user_id=person):
        if scheduler is not None:
            scheduler.remove(task.id)
        if task.state == "active":
            await asyncio.to_thread(store.update, task.id, state="paused")
            paused += 1
    return paused


def expired_people(
    registry: dict[str, dict[str, Any]],
    activity: dict[str, float],
    now: float,
    keep_days: int = KEEP_DAYS,
) -> list[str]:
    """The registered eval persons whose newest activity (registered, released, their sessions'
    last update) is older than *keep_days*. Anyone not in the registry is never listed."""
    cutoff = now - keep_days * 86400
    expired = []
    for person, row in registry.items():
        if run_of(person) is None:
            continue
        times = (row.get("created_at"), row.get("released_at"), activity.get(person))
        if max(float(t or 0) for t in times) < cutoff:
            expired.append(person)
    return sorted(expired)


class EvalPeopleSweep:
    """Deletes expired eval persons once a day (a feature background job)."""

    def __init__(self, state: Any, conversation_store: Any, registry: EvalPersons) -> None:
        self._state = state
        self._conversations = conversation_store
        self._registry = registry
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="eval-people-sweep")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_SWEEP_SECONDS)
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("evals: the eval people sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)

    async def sweep_once(self, now: float | None = None) -> list[str]:
        """Delete every expired eval person; returns who was deleted."""
        remove_person = getattr(self._state, "remove_person", None)
        if remove_person is None:
            return []
        stats = await asyncio.to_thread(self._conversations.owner_session_stats)
        activity = {user: float(latest or 0) for user, (_count, latest) in stats.items()}
        registry = await asyncio.to_thread(self._registry.all)
        gone = []
        for person in expired_people(registry, activity, now or time.time()):
            if run_of(person) is None:  # the guard, again, right before the delete
                continue
            await self._remove_hosts(person)
            removed = await remove_person(self._state, person)
            _logger.info("evals: deleted eval person %s: %s", person, removed)
            gone.append(person)
        return gone

    async def _remove_hosts(self, person: str) -> None:
        """The person's hosts with their homes, through the host provider (a plain delete only
        terminates, which a per-person host outlives)."""
        host_store = getattr(self._state, "host_store", None)
        if host_store is None:
            return
        for host in await asyncio.to_thread(host_store.list_hosts, person):
            if host.sandbox_provider is not None:
                await remove_host(
                    host, host_store, getattr(self._state, "sandbox_config", None), home=True
                )
