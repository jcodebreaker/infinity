"""Registration of the decks primitive: authoring and export tools plus the export route."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.context.labels import is_superside_chat
from omnigent.superchat.cards.tools import register_preview_kind
from omnigent.superchat.decks import kit
from omnigent.superchat.decks.handlers import handle_deck_tool
from omnigent.superchat.decks.tools import DECK_TOOL_NAMES
from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _distinct_looks(ids: list[str]) -> str | None:
    """Themes offered together must be real alternatives, never two of the same kind of look."""
    looks = [(kit.templates()[i].category, kit.templates()[i].mode) for i in ids]
    if len(set(looks)) < len(looks):
        return (
            "the theme options look alike: offer themes from different categories "
            "(for example one professional, one editorial, one bold or dark)"
        )
    return None


# An ask_clarification option may show a deck theme's thumbnail (the client has the dictionary).
register_preview_kind("deck-theme", lambda: set(kit.templates()), _distinct_looks)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Offered to the Super Chat and to Helpers alike (a Helper saves under its parent)."""
    from omnigent.superchat.decks.tools import (
        DeckCheckTool,
        DeckExportTool,
        DeckNewTool,
        DeckThemeSetTool,
        DeckThemesTool,
    )

    if not is_superside_chat(labels):
        return []
    return [DeckNewTool(), DeckCheckTool(), DeckThemesTool(), DeckThemeSetTool(), DeckExportTool()]


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store
    from omnigent.superchat.artifacts import SqlAlchemyArtifactStore
    from omnigent.superchat.decks.routes import create_decks_router

    if deps.scheduled_task_store is None:
        return
    store = SqlAlchemyArtifactStore(deps.scheduled_task_store.storage_location, get_artifact_store)
    app.include_router(
        create_decks_router(
            store,
            conversation_store=deps.conversation_store,
            runner_router=deps.runner_router,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["decks"],
    )


DECKS_FEATURE = Feature(
    name="decks",
    tools=_tools,
    handlers=dict.fromkeys(DECK_TOOL_NAMES, handle_deck_tool),
    install=_install,
)
