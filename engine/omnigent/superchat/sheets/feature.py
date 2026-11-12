"""Registration of the sheets primitive (table REST routes)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx
from omnigent.superchat.sheets import parts as _parts  # noqa: F401  (registers the cell editor)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Sheets adds no tools: the Muse reads and writes the file itself."""
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store
    from omnigent.superchat.artifacts import SqlAlchemyArtifactStore
    from omnigent.superchat.sheets.routes import create_sheets_router

    if deps.scheduled_task_store is None:
        return
    store = SqlAlchemyArtifactStore(deps.scheduled_task_store.storage_location, get_artifact_store)
    app.include_router(
        create_sheets_router(store, auth_provider=deps.auth_provider),
        prefix="/v1",
        tags=["sheets"],
    )


SHEETS_FEATURE = Feature(
    name="sheets",
    tools=_tools,
    install=_install,
)
