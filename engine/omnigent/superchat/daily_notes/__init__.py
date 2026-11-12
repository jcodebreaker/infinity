"""Daily notes (one per owner per local day) and the quiet-moment job that tends them.

Layout: ``notes`` (pure rendering + local-day helpers), ``store``, ``routes`` (daily-note
endpoints), ``tools`` (tool defs), ``handlers`` (runner side), ``quiet_moment`` (background job).
Imports stay lazy so importing a submodule never loads the server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.daily_notes.handlers import handle_daily_note_tool
from omnigent.superchat.daily_notes.tools import DAILY_NOTE_TOOL_NAMES
from omnigent.superchat.feature import (
    BackgroundJob,
    Feature,
    InstallDeps,
    ToolManagerCtx,
    is_helper,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Only a Super Chat's Helpers read and write the daily note."""
    from omnigent.superchat.daily_notes.tools import DailyNoteGetTool, DailyNoteUpdateTool

    return [DailyNoteGetTool(), DailyNoteUpdateTool()] if is_helper(labels) else []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.superchat.daily_notes.routes import create_daily_notes_router
    from omnigent.superchat.daily_notes.store import SqlAlchemyDailyNoteStore

    if deps.scheduled_task_store is None:
        return
    daily_note_store = SqlAlchemyDailyNoteStore(deps.scheduled_task_store.storage_location)
    app.state.daily_note_store = daily_note_store
    app.include_router(
        create_daily_notes_router(
            daily_note_store,
            scheduled_task_store=deps.scheduled_task_store,
            conversation_store=deps.conversation_store,
            permission_store=deps.permission_store,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["daily_notes"],
    )


def _jobs(app: FastAPI) -> list[BackgroundJob]:
    """The quiet-moment worker (daily note, Dreaming, People), when the app has fire deps."""
    from omnigent.superchat.daily_notes.quiet_moment import QuietMoment

    note_store = getattr(app.state, "daily_note_store", None)
    fire_deps = getattr(app.state, "fire_deps", None)
    if fire_deps is None or note_store is None:
        return []
    quiet_moment = QuietMoment(deps=fire_deps, note_store=note_store)
    app.state.quiet_moment = quiet_moment
    return [quiet_moment]


FEATURE = Feature(
    name="daily_notes",
    tools=_tools,
    handlers=dict.fromkeys(DAILY_NOTE_TOOL_NAMES, handle_daily_note_tool),
    install=_install,
    jobs=_jobs,
)
