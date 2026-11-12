"""Registration of the organization admin routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import BackgroundJob, Feature, InstallDeps, ToolManagerCtx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """No model-facing tools: administration is for people, not the model."""
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.evals.people import EvalPersons
    from omnigent.superchat.admin.routes import create_org_admin_router, person_remover

    state = app.state
    # The stores are built by the ``models`` feature (registered first); without them there is
    # nothing to administer (no database behind the server).
    if not hasattr(state, "model_budgets"):
        return
    # Deleting a person, for the eval people sweep (no request, no admin check).
    state.remove_person = person_remover(
        suspensions=state.model_suspensions,
        connections=state.model_connection_store,
        conversation_store=deps.conversation_store,
        scheduled_task_store=deps.scheduled_task_store,
        auth_provider=deps.auth_provider,
        permission_store=deps.permission_store,
    )
    state.eval_people_conversations = deps.conversation_store
    # The registry of eval persons (ADR 0043), written by ``/v1/me/muse`` and on release.
    state.eval_persons = EvalPersons(deps.scheduled_task_store.storage_location)
    app.include_router(
        create_org_admin_router(
            suspensions=state.model_suspensions,
            budgets=state.model_budgets,
            connections=state.model_connection_store,
            conversation_store=deps.conversation_store,
            scheduled_task_store=deps.scheduled_task_store,
            auth_provider=deps.auth_provider,
            permission_store=deps.permission_store,
            eval_persons=state.eval_persons,
        ),
        prefix="/v1",
        tags=["org-admin"],
    )


def _jobs(app: FastAPI) -> list[BackgroundJob]:
    """The daily sweep that deletes registered eval persons a week after their last activity
    (ADR 0043)."""
    from omnigent.evals.people import EvalPeopleSweep

    state = app.state
    if getattr(state, "remove_person", None) is None:
        return []
    return [EvalPeopleSweep(state, state.eval_people_conversations, state.eval_persons)]


FEATURE = Feature(name="admin", tools=_tools, install=_install, jobs=_jobs)
