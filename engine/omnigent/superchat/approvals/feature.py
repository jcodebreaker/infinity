"""Registration of approvals: binds the store to the policy and mounts ``/v1/approvals``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Approvals are a policy, not a tool."""
    return []


def _install_asks(app: FastAPI, deps: InstallDeps) -> None:
    """Mount the decisions inbox (``/v1/me/asks``): everything waiting on the person."""
    from omnigent.superchat.approvals.inbox_routes import create_asks_router

    app.include_router(
        create_asks_router(
            objective_store=deps.objective_store,
            conversation_store=deps.conversation_store,
            agent_store=deps.agent_store,
            runner_router=deps.runner_router,
            permission_store=deps.permission_store,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["asks"],
    )


def _install(app: FastAPI, deps: InstallDeps) -> None:
    _install_asks(app, deps)
    from omnigent.superchat.approvals.policy import configure_store
    from omnigent.superchat.approvals.routes import (
        create_approvals_router,
        install_pending_persistence,
    )
    from omnigent.superchat.approvals.store import SqlAlchemyApprovalStore

    if deps.scheduled_task_store is None:
        return
    approval_store = SqlAlchemyApprovalStore(deps.scheduled_task_store.storage_location)
    app.state.approval_store = approval_store
    configure_store(approval_store)
    install_pending_persistence(approval_store)
    app.include_router(
        create_approvals_router(
            approval_store,
            conversation_store=deps.conversation_store,
            agent_store=deps.agent_store,
            runner_router=deps.runner_router,
            permission_store=deps.permission_store,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["approvals"],
    )


FEATURE = Feature(name="approvals", tools=_tools, install=_install)
