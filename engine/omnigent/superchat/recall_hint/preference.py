"""The ``recall_hint`` setting: one value per person, ``on`` (the default) or ``off``.

``GET /v1/me/recall-hint`` returns ``{"recall_hint": "on" | "off"}``; ``PUT`` takes the same body
and returns it. Stored in the generic ``preferences`` table under the caller's user id, like the
model picks; no row means ``on`` (ADR 0027), and turning it off stores ``off``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user

PREFERENCE_KEY = "recall_hint"
ON = "on"
OFF = "off"


class RecallHintStore:
    """The setting per person, in ``preferences``."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.recall_hint_prefs"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )

    def get(self, owner: str) -> str:
        """``on`` or ``off``; only a stored ``off`` is off (unset or unreadable is ``on``)."""
        with self._session("select_recall_hint") as session:
            row = session.get(SqlPreference, (current_workspace_id(), owner, PREFERENCE_KEY))
            raw = row.value if row is not None else None
        try:
            value = json.loads(raw) if raw else ON
        except ValueError:
            return ON
        return OFF if value == OFF else ON

    def set(self, owner: str, value: str) -> None:
        """Store *value*; ``on`` (the default) removes the row."""

        def write(session: Any) -> None:
            pk = (current_workspace_id(), owner, PREFERENCE_KEY)
            row = session.get(SqlPreference, pk)
            if value == ON:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(
                    SqlPreference(user_id=owner, key=PREFERENCE_KEY, value=json.dumps(OFF))
                )
            else:
                row.value = json.dumps(OFF)

        run_write_transaction(self._session_immediate, "upsert_recall_hint", write)


class RecallHintBody(BaseModel):
    """Body of ``PUT /v1/me/recall-hint``."""

    model_config = ConfigDict(extra="forbid")

    recall_hint: Any = None


def register_recall_hint_routes(
    store: RecallHintStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """``GET`` / ``PUT /me/recall-hint`` (mount with ``prefix="/v1"``)."""
    router = APIRouter()

    def _owner(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    @router.get("/me/recall-hint")
    async def get_recall_hint(request: Request) -> dict[str, str]:
        """Whether the caller's turns get the recall hint."""
        return {PREFERENCE_KEY: await asyncio.to_thread(store.get, _owner(request))}

    @router.put("/me/recall-hint")
    async def put_recall_hint(request: Request, body: RecallHintBody) -> dict[str, str]:
        """Turn it ``on`` or ``off``."""
        if body.recall_hint not in (ON, OFF):
            raise OmnigentError('recall_hint must be "on" or "off"', code=ErrorCode.INVALID_INPUT)
        owner = _owner(request)
        await asyncio.to_thread(store.set, owner, body.recall_hint)
        return {PREFERENCE_KEY: await asyncio.to_thread(store.get, owner)}

    return router
