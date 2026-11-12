"""Registration of the artifacts primitive (tools, REST routes, delete approval)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.context.labels import is_artifact_chat, is_superside_chat
from omnigent.superchat.artifacts.handlers import handle_artifact_tool
from omnigent.superchat.artifacts.tools import ARTIFACT_TOOL_NAMES
from omnigent.superchat.artifacts.writeback import deliver_manual_edits
from omnigent.superchat.feature import (
    Feature,
    InstallDeps,
    ToolManagerCtx,
    register_file_linker,
)
from omnigent.superchat.risk import Risk, register_classifier, register_target_namer

if TYPE_CHECKING:
    import httpx
    from fastapi import FastAPI

    from omnigent.superchat.feature import BackgroundJob
    from omnigent.tools.base import Tool


def _classify_delete(tool_name: str, arguments: Mapping[str, Any]) -> Risk | None:
    """``artifact_delete`` is a ``delete``-category action: the person is always asked."""
    base = tool_name.rsplit("__", 1)[-1]
    if base != "artifact_delete":
        return None
    target = str(arguments.get("artifact_id") or "a saved file")
    return Risk("delete", (f"artifact:{target}",), "Delete a saved file and all its versions")


register_classifier(_classify_delete)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Offered to the Super Chat and to Helpers alike (a Helper saves under its parent).

    ``artifact_patch`` only in a file's studio (ADR 0024): it edits that one file.
    ``artifact_outline`` everywhere: a file is read by element, never by the shell (ADR 0031).
    """
    from omnigent.superchat.artifacts.tools import (
        ArtifactDeleteTool,
        ArtifactListTool,
        ArtifactOutlineTool,
        ArtifactPatchTool,
        ArtifactSaveTool,
    )

    if not is_superside_chat(labels):
        return []
    tools: list[Tool] = [
        ArtifactSaveTool(),
        ArtifactListTool(),
        ArtifactDeleteTool(),
        ArtifactOutlineTool(),
    ]
    if is_artifact_chat(labels):
        tools.append(ArtifactPatchTool())
    return tools


async def _turn_prefix(server_client: httpx.AsyncClient | None, conversation_id: str) -> list[str]:
    """Hand edits written back, one note each. The file handles are the prefix's own block
    (``prompt_prefix.files_block``): it knows what this harness session was already sent."""
    return await deliver_manual_edits(server_client, conversation_id)


#: The studio's save hook (ADR 0024), made at install; its loop is captured by :func:`_jobs`.
_STUDIO_NOTICES: list[Any] = []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store
    from omnigent.server.routes._sessions.orchestration import ON_MESSAGE_CONTEXT
    from omnigent.superchat.activity.derive import FILE_NAMES, FILE_ROUNDS
    from omnigent.superchat.artifacts import store as store_module
    from omnigent.superchat.artifacts.edit_routes import create_html_edit_router
    from omnigent.superchat.artifacts.links import link_made, record_open_artifact
    from omnigent.superchat.artifacts.routes import create_artifacts_router, steer_session
    from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
    from omnigent.superchat.artifacts.studio import StudioNotices, studio_model, studio_of
    from omnigent.superchat.family.signals import notify_artifacts_changed

    if deps.scheduled_task_store is None:
        return
    store = SqlAlchemyArtifactStore(deps.scheduled_task_store.storage_location, get_artifact_store)
    conversations = deps.conversation_store

    async def _steer_studio(session_id: str, text: str) -> bool:
        return await steer_session(deps.runner_router, conversations, session_id, text)

    notices = StudioNotices(conversations, _steer_studio)
    _STUDIO_NOTICES[:] = [notices]
    store_module.ON_VERSION_SAVED["studio"] = notices.on_saved
    store_module.ON_GROUP_DELETED["studio"] = notices.on_deleted
    # Every open view of a file (the card, its studio) adopts the new version on this (ADR 0030).
    store_module.ON_VERSION_SAVED["family"] = lambda saved: notify_artifacts_changed(
        saved.parent_session_id,
        saved.group_id or saved.id,
        version_id=saved.id,
        version=saved.version,
        origin=saved.origin,
        from_studio=saved.chat_id is not None and saved.chat_id == studio_of(saved),
        changed_ids=saved.changed_ids,
    )

    async def _studio_model(request: Any, owner: str | None, parent: Any) -> str | None:
        return await studio_model(
            request.app.state, deps.agent_store, deps.agent_cache, owner, parent
        )

    def _name(artifact_id: str, owner: str | None) -> str | None:
        item = store.get(artifact_id, user_id=owner)
        return (item.title or item.name) if item is not None else None

    register_target_namer("artifact", _name)
    # A studio turn's Activity names its file (ADR 0024).
    FILE_NAMES["artifacts"] = store.group_name
    # ...and the Round that created it, so later edits sit under that request.
    FILE_ROUNDS["artifacts"] = store.group_round

    def _open_artifact(session_id: str, item_id: str, context: dict[str, Any]) -> None:
        record_open_artifact(store, session_id, item_id, context)

    # Before the message reaches the runner, so the turn's hint already names it as open.
    ON_MESSAGE_CONTEXT["artifacts"] = _open_artifact
    # A message engine code posts (ADR 0038) carries the file its run made as a card.
    register_file_linker(
        lambda session_id, item_id, artifact_id: link_made(store, session_id, item_id, artifact_id)
    )
    app.include_router(
        create_artifacts_router(
            store,
            conversation_store=deps.conversation_store,
            permission_store=deps.permission_store,
            auth_provider=deps.auth_provider,
            studio_model=_studio_model,
        ),
        prefix="/v1",
        tags=["artifacts"],
    )
    # After the artifacts router, as before: hand edits of an HTML file (a deck, a page).
    app.include_router(
        create_html_edit_router(
            store,
            auth_provider=deps.auth_provider,
            conversation_store=deps.conversation_store,
            permission_store=deps.permission_store,
        ),
        prefix="/v1",
        tags=["artifacts"],
    )


def _jobs(_app: FastAPI) -> list[BackgroundJob]:
    """The studio notices schedule on the engine's loop, captured when the app starts."""
    return list(_STUDIO_NOTICES)


ARTIFACTS_FEATURE = Feature(
    name="artifacts",
    tools=_tools,
    handlers=dict.fromkeys(ARTIFACT_TOOL_NAMES, handle_artifact_tool),
    turn_prefix=_turn_prefix,
    install=_install,
    jobs=_jobs,
)
