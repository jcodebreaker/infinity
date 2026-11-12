"""Registration of the apps primitive (publish tool, publish/published REST routes, approval)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.context.labels import is_superside_chat
from omnigent.superchat.apps.handlers import handle_publish_tool
from omnigent.superchat.apps.tools import APPS_TOOL_NAMES
from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx
from omnigent.superchat.risk import Risk, register_classifier

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _classify_publish(tool_name: str, arguments: Mapping[str, Any]) -> Risk | None:
    """``artifact_publish`` is a ``post``-category action: the person is always asked."""
    base = tool_name.rsplit("__", 1)[-1]
    if base != "artifact_publish":
        return None
    target = str(arguments.get("artifact_id") or "a saved app")
    audience = str(arguments.get("audience") or "")
    who = {
        "owner": "only you",
        "org": "your organization",
        "link": "anyone with the link",
    }.get(audience, "the chosen audience")
    return Risk("post", (f"artifact:{target}",), f"Publish an app to its own address for {who}")


register_classifier(_classify_publish)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Offered to the Super Chat and to Helpers alike (a Helper saves under its parent)."""
    from omnigent.superchat.apps.tools import ArtifactPublishTool

    if not is_superside_chat(labels):
        return []
    return [ArtifactPublishTool()]


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store
    from omnigent.superchat.apps.routes import (
        create_apps_router,
        create_published_pages_router,
    )
    from omnigent.superchat.artifacts import SqlAlchemyArtifactStore

    if deps.scheduled_task_store is None:
        return
    store = SqlAlchemyArtifactStore(deps.scheduled_task_store.storage_location, get_artifact_store)
    app.include_router(
        create_apps_router(store, auth_provider=deps.auth_provider), prefix="/v1", tags=["apps"]
    )
    # The published page itself, at the origin root (before the SPA catch-all mount).
    app.include_router(create_published_pages_router(store, auth_provider=deps.auth_provider))


APPS_FEATURE = Feature(
    name="apps",
    tools=_tools,
    handlers=dict.fromkeys(APPS_TOOL_NAMES, handle_publish_tool),
    install=_install,
)
