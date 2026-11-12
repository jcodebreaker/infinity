"""System jobs: a read-only list of the background work the engine does on its own.

Layout: ``routes`` (``GET /v1/me/system-jobs``). The feature reads the running jobs from
``app.state`` at request time, so it never imports another feature.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """The list is for the person's eyes; the model gets no tool for it."""
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.superchat.system_jobs.routes import create_system_jobs_router

    app.include_router(
        create_system_jobs_router(app, auth_provider=deps.auth_provider),
        prefix="/v1",
        tags=["system_jobs"],
    )


FEATURE = Feature(name="system_jobs", tools=_tools, install=_install)
