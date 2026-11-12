"""Registration of the morning brief: its tool, route, run brief and saved-file listener."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.context.labels import background_task_id
from omnigent.superchat.brief.handlers import handle_brief_tool
from omnigent.superchat.brief.tools import BRIEF_TOOL_NAMES, SAVE_TOOL_NAME
from omnigent.superchat.feature import (
    Feature,
    HandlerCtx,
    InstallDeps,
    ToolManagerCtx,
    is_helper,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Only a scheduled run sees ``brief_save``; the handler checks it is the brief's."""
    from omnigent.superchat.brief.tools import BriefSaveTool

    if is_helper(labels) and background_task_id(labels) is not None:
        return [BriefSaveTool()]
    return []


def _note_saved(ctx: HandlerCtx, output: str) -> None:
    """List the saved page with the run's result, as a Helper's ``artifact_save`` is, so the
    Conversation shows the page's card with the run's line."""
    if ctx.tool_name != SAVE_TOOL_NAME or not ctx.conversation_id or not is_helper(ctx.labels):
        return
    from omnigent.superchat.helpers.saved_files import record_saved_file

    record_saved_file(ctx.conversation_id, output)


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store, get_memory_service
    from omnigent.server.scheduled.helper_fire import register_run_brief
    from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
    from omnigent.superchat.brief.routes import create_brief_router
    from omnigent.superchat.brief.service import BriefService
    from omnigent.superchat.brief.task import BRIEF_KIND

    if deps.scheduled_task_store is None:
        return
    service = BriefService(
        deps.scheduled_task_store,
        SqlAlchemyArtifactStore(deps.scheduled_task_store.storage_location, get_artifact_store),
        notes=lambda: getattr(app.state, "daily_note_store", None),
        memory=get_memory_service,
    )
    register_run_brief(BRIEF_KIND, service.run_brief)
    app.include_router(
        create_brief_router(
            service, conversation_store=deps.conversation_store, auth_provider=deps.auth_provider
        ),
        prefix="/v1",
        tags=["brief"],
    )


BRIEF_FEATURE = Feature(
    name="brief",
    tools=_tools,
    handlers=dict.fromkeys(BRIEF_TOOL_NAMES, handle_brief_tool),
    on_result=_note_saved,
    install=_install,
)
