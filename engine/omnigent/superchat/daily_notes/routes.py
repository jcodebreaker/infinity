"""Daily notes: one note per owner per local day.

* ``GET /v1/me/daily-notes?from&to`` lists the owner's notes (newest first).
* ``GET /v1/me/daily-notes/{date}`` reads one (``today`` is the owner's local day).
* ``PUT /v1/me/daily-notes/{date}`` is the person's edit: given sections win and become
  protected, so background writers only append to them.
* ``POST /v1/daily-notes/write`` is what the Helper's ``daily_note_update`` tool calls: it merges
  into today's note and never overwrites text the person edited.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date as date_cls
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.entities.daily_note import SECTION_KEYS, DailyNote
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.superchat.daily_notes.notes import local_date
from omnigent.superchat.daily_notes.store import SqlAlchemyDailyNoteStore

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SectionsBody(BaseModel):
    """Body of ``PUT /v1/me/daily-notes/{date}``."""

    model_config = ConfigDict(extra="forbid")

    sections: dict[str, str] = Field(default_factory=dict)


class WriteNoteBody(BaseModel):
    """Body of ``POST /v1/daily-notes/write``."""

    model_config = ConfigDict(extra="forbid")

    parent_session_id: str
    sections: dict[str, str] = Field(default_factory=dict)
    date: str | None = None


def note_to_response(note: DailyNote) -> dict[str, Any]:
    """Serialize a note for the REST API."""
    return {
        "date": note.note_date,
        "sections": note.sections,
        "edited_by_person": note.edited_by_person,
        "edited_sections": note.edited_sections,
        "finalized": note.finalized_at is not None,
        "dreamed": note.dreamed_at is not None,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def _check_sections(sections: dict[str, str]) -> None:
    unknown = sorted(set(sections) - set(SECTION_KEYS))
    if unknown:
        raise OmnigentError(
            f"unknown section(s) {unknown}; use {list(SECTION_KEYS)}",
            code=ErrorCode.INVALID_INPUT,
        )


def create_daily_notes_router(
    store: SqlAlchemyDailyNoteStore,
    *,
    scheduled_task_store: Any,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the daily notes router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    async def _today(owner: str) -> str:
        prefs = await asyncio.to_thread(scheduled_task_store.get_owner_preferences, owner)
        return local_date(prefs.timezone if prefs else None)

    async def _resolve_date(owner: str, value: str) -> str:
        if value == "today":
            return await _today(owner)
        if not _DATE_RE.match(value):
            raise OmnigentError("date must be YYYY-MM-DD", code=ErrorCode.INVALID_INPUT)
        return value

    @router.get("/me/daily-notes")
    async def list_notes(
        request: Request,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
        limit: int = Query(default=60, ge=1, le=366),
    ) -> dict[str, Any]:
        """The owner's notes within an inclusive date range, newest first."""
        owner = _owner(request)
        for value in (date_from, date_to):
            if value is not None and not _DATE_RE.match(value):
                raise OmnigentError("from/to must be YYYY-MM-DD", code=ErrorCode.INVALID_INPUT)
        notes = await asyncio.to_thread(
            store.list_range, owner, date_from=date_from, date_to=date_to, limit=limit
        )
        return {"daily_notes": [note_to_response(n) for n in notes]}

    @router.get("/me/daily-notes/{date}")
    async def get_note(request: Request, date: str) -> dict[str, Any]:
        """One day's note; an empty one (not stored) when nothing was written yet."""
        owner = _owner(request)
        day = await _resolve_date(owner, date)
        note = await asyncio.to_thread(store.get, owner, day)
        return note_to_response(
            note or DailyNote(owner=owner, note_date=day, sections=dict.fromkeys(SECTION_KEYS, ""))
        )

    @router.put("/me/daily-notes/{date}")
    async def put_note(request: Request, date: str, body: SectionsBody) -> dict[str, Any]:
        """The person's edit: given sections replace stored text and are protected."""
        owner = _owner(request)
        day = await _resolve_date(owner, date)
        _check_sections(body.sections)
        note = await asyncio.to_thread(store.person_edit, owner, day, body.sections)
        return note_to_response(note)

    @router.post("/daily-notes/write")
    async def write_note(request: Request, body: WriteNoteBody) -> dict[str, Any]:
        """A background writer merges sections into today's note (edited text is kept)."""
        owner = _owner(request)
        try:
            uuid_to_bytes(body.parent_session_id)
        except InvalidUuidError as exc:
            raise OmnigentError("invalid parent_session_id", code=ErrorCode.INVALID_INPUT) from exc
        await require_access(
            owner,
            body.parent_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        _check_sections(body.sections)
        day = await _today(owner)
        if body.date is not None and body.date != day:
            # The nightly Dreaming pass reflects on the day that just ended: today or yesterday.
            if (
                not _DATE_RE.match(body.date)
                or body.date != (date_cls.fromisoformat(day) - timedelta(days=1)).isoformat()
            ):
                raise OmnigentError(
                    "date must be today or yesterday", code=ErrorCode.INVALID_INPUT
                )
            day = body.date
        note = await asyncio.to_thread(
            store.write_sections,
            owner,
            day,
            body.sections,
            parent_session_id=body.parent_session_id,
        )
        return note_to_response(note)

    return router
