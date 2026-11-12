"""Recall for the context brief and the ``recall_hint`` setting.

Layout: ``hint`` (the queries, the search, the pointer line; runner side, read by the context
brief), ``preference`` (the ``recall_hint`` setting and ``/v1/me/recall-hint``: on by default,
ADR 0027), this module (registration). Nothing goes in the turn prefix and it adds no tool: the
brief carries what it finds, and the pull path is the ``recall`` tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """The hint is a turn prefix, not a tool."""
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    """Mount ``/v1/me/recall-hint`` (needs the task store's DB, like the other settings)."""
    if deps.scheduled_task_store is None:
        return
    from omnigent.superchat.recall_hint.preference import (
        RecallHintStore,
        register_recall_hint_routes,
    )

    store = RecallHintStore(deps.scheduled_task_store.storage_location)
    app.include_router(
        register_recall_hint_routes(store, auth_provider=deps.auth_provider),
        prefix="/v1",
        tags=["recall_hint"],
    )


FEATURE = Feature(name="recall_hint", tools=_tools, install=_install)
