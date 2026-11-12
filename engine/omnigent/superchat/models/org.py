"""Account suspension: a person whose usage an organization admin stopped.

A **suspension** (``account_suspended``, one row under the suspended user's id in the generic
``preferences`` table) stops that person's usage. The model proxy and the session create/turn
routes read it through :func:`refuse_if_suspended`. Deleting the account deletes its preference
rows, flag included. The workspace itself is :data:`WORKSPACE_OWNER`, the identity no person can
hold.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import cachetools

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_WORKSPACE

SUSPENDED_KEY = "account_suspended"
WORKSPACE_OWNER = RESERVED_USER_WORKSPACE
SUSPENDED_MESSAGE = "Your account is suspended. Contact an admin to continue."
_CACHE_TTL_SECONDS = 5


class SuspensionStore:
    """Who is suspended: a flag row per user in ``preferences`` with a short read cache."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.account_suspension"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )
        # The proxy checks every model call: a TTL keeps that to a dict lookup. Changes evict on
        # this replica at once; other replicas see them within the TTL.
        self._cache: WorkspaceScopedCache[str, bool] = WorkspaceScopedCache(
            lambda: cachetools.TTLCache(maxsize=4096, ttl=_CACHE_TTL_SECONDS)
        )

    def is_suspended(self, user_id: str) -> bool:
        cached = self._cache.get(user_id)
        if cached is not None:
            return cached
        with self._session("select_account_suspended") as session:
            found = (
                session.get(SqlPreference, (current_workspace_id(), user_id, SUSPENDED_KEY))
                is not None
            )
        self._cache[user_id] = found
        return found

    def suspend(self, user_id: str, by: str | None) -> None:
        """Flag *user_id* suspended (idempotent; keeps the first timestamp)."""

        def write(session: Any) -> None:
            pk = (current_workspace_id(), user_id, SUSPENDED_KEY)
            if session.get(SqlPreference, pk) is None:
                value = json.dumps({"at": now_epoch(), "by": by})
                session.add(SqlPreference(user_id=user_id, key=SUSPENDED_KEY, value=value))

        run_write_transaction(self._session_immediate, "suspend_account", write)
        self._cache.pop(user_id, None)

    def resume(self, user_id: str) -> None:
        """Lift the suspension (idempotent)."""

        def write(session: Any) -> None:
            row = session.get(SqlPreference, (current_workspace_id(), user_id, SUSPENDED_KEY))
            if row is not None:
                session.delete(row)

        run_write_transaction(self._session_immediate, "resume_account", write)
        self._cache.pop(user_id, None)

    def suspended_since(self) -> dict[str, int | None]:
        """Every suspended user with the epoch they were suspended at (uncached)."""
        from sqlalchemy import select

        with self._session("select_suspended_accounts") as session:
            rows = session.execute(
                select(SqlPreference.user_id, SqlPreference.value)
                .where(SqlPreference.workspace_id == current_workspace_id())
                .where(SqlPreference.key == SUSPENDED_KEY)
            ).all()
        found: dict[str, int | None] = {}
        for user_id, raw in rows:
            try:
                at = json.loads(raw).get("at")
            except (ValueError, AttributeError):
                at = None
            found[user_id] = at if isinstance(at, int) else None
        return found


_suspensions: SuspensionStore | None = None


def bind_suspensions(store: SuspensionStore | None) -> None:
    """Bind the server's suspensions so routes and the proxy can find them (``None`` unbinds)."""
    global _suspensions
    _suspensions = store


def get_suspensions() -> SuspensionStore | None:
    """The bound suspensions, or ``None`` when the server has none (nobody can be suspended)."""
    return _suspensions


def suspended_error() -> OmnigentError:
    """The refusal a suspended person gets, as an engine error."""
    return OmnigentError(SUSPENDED_MESSAGE, code=ErrorCode.ACCOUNT_SUSPENDED)


async def refuse_if_suspended(user_id: str | None) -> None:
    """Raise ``ACCOUNT_SUSPENDED`` when *user_id* is suspended; a no-op otherwise.

    :raises OmnigentError: ``ACCOUNT_SUSPENDED`` (HTTP 403).
    """
    store = _suspensions
    if store is None or not user_id:
        return
    if await asyncio.to_thread(store.is_suspended, user_id):
        raise suspended_error()
