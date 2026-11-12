"""Secrets vault: encrypted logins, secure-entry requests and their audit trail.

Layout: ``store`` (SQLAlchemy store), ``routes`` (``/v1/me/vault``), ``tools`` (tool defs),
``handlers`` (runner side). Imports stay lazy so importing a submodule never loads the server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import (
    Feature,
    InstallDeps,
    ToolManagerCtx,
    is_helper,
    is_super_chat,
)
from omnigent.superchat.vault.handlers import handle_vault_tool
from omnigent.superchat.vault.tools import VAULT_TOOL_NAMES

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """A Super Chat may ask for a secret and fill; a Helper may only fill."""
    from omnigent.superchat.vault.tools import VaultFillTool, VaultRequestSecretTool

    if is_super_chat(labels):
        return [VaultRequestSecretTool(), VaultFillTool()]
    if is_helper(labels):
        return [VaultFillTool()]
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.superchat.vault.routes import create_vault_router
    from omnigent.superchat.vault.store import VaultStore

    if deps.scheduled_task_store is None:
        return
    vault_store = VaultStore(deps.scheduled_task_store.storage_location)
    app.state.vault_store = vault_store
    app.include_router(
        create_vault_router(
            vault_store,
            conversation_store=deps.conversation_store,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["vault"],
    )


FEATURE = Feature(
    name="vault",
    tools=_tools,
    handlers=dict.fromkeys(VAULT_TOOL_NAMES, handle_vault_tool),
    install=_install,
)
