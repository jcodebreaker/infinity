"""Tasks: the lines in front of the person's message and the picks on cards.

ADR 0025 kept one record per deliverable in progress; ADR 0029 moved that to the Round record.
What the turn reads is now the Rounds still working and the pick the Round's message carried.

Layout: ``record`` (the lines, the card answer, the rule for a turn that ends well; pure),
``store`` (``card_answers``), ``service`` (server side: the view a turn reads), ``routes``
(``/v1/sessions/{id}/tasks``), ``runtime`` (runner side: the turn's lines and its Round). No
tools. Conversation-only: a Helper's turn reads no task lines.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx
from omnigent.superchat.tasks.runtime import message_prefix, turn_ended

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.server.routes._sessions.orchestration import (
        ON_MESSAGE_CONTEXT,
        ON_SESSION_DELETED,
    )
    from omnigent.superchat.rounds.store import SqlAlchemyRoundStore
    from omnigent.superchat.tasks.routes import create_tasks_router
    from omnigent.superchat.tasks.service import TaskService
    from omnigent.superchat.tasks.store import SqlAlchemyTaskStore

    if deps.storage_location is None:
        return
    service = TaskService(
        SqlAlchemyTaskStore(deps.storage_location),
        deps.conversation_store,
        SqlAlchemyRoundStore(deps.storage_location),
    )
    ON_MESSAGE_CONTEXT["tasks"] = service.card_answered
    ON_SESSION_DELETED["tasks"] = service.conversation_deleted
    app.include_router(
        create_tasks_router(
            service, permission_store=deps.permission_store, auth_provider=deps.auth_provider
        ),
        prefix="/v1",
        tags=["tasks"],
    )


FEATURE = Feature(
    name="tasks",
    tools=_tools,
    message_prefix=message_prefix,
    conversation_only=True,
    on_turn_end=turn_ended,
    install=_install,
)
