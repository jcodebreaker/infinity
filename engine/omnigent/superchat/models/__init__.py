"""Model connections: encrypted provider API keys, per user or per organization.

Layout: ``upstreams`` (the provider registry), ``store`` (SQLAlchemy store, sealed with the vault's
AES-GCM), ``probe`` (one cheap validation call), ``routes`` (``/v1/me/model-connections`` and
``/v1/admin/model-connections``), ``budget``/``budget_routes`` (monthly limits and
``/v1/me/budget``, ``/v1/admin/budget``), ``org`` (account suspensions), ``selection`` (a
person's catalog and their Default and Light picks), ``tiers`` (how Default and Light resolve),
``background`` (the model the engine's own background work runs on: the owner's Light),
``listing`` (what an upstream reports about its models), ``spend`` (what a proxied call cost),
``proxy`` (the engine model proxy).
The key goes in once, is probed, sealed and never shown back; the run path reads it server-side
through :func:`omnigent.superchat.models.store.resolve_model_connection`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx
from omnigent.superchat.models import budget
from omnigent.superchat.models.budget import ModelBudgets
from omnigent.superchat.models.embeddings import (
    DEFAULT_EMBEDDING_DIMENSIONS,
    REASON_ERROR,
    REASON_NO_CONNECTION,
    EmbeddingUnavailable,
    ModelEmbeddings,
    get_embeddings,
)
from omnigent.superchat.models.org import SuspensionStore
from omnigent.superchat.models.store import SCOPE_USER, ModelConnectionStore

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.tools.base import Tool


def _tools(_labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """No model-facing tools: the key never reaches the model."""
    return []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.superchat.models.routes import create_model_connection_router
    from omnigent.superchat.models.store import ModelConnectionStore

    if deps.scheduled_task_store is None:
        return
    store = ModelConnectionStore(deps.scheduled_task_store.storage_location)
    app.state.model_connection_store = store
    from omnigent.superchat.models.selection import (
        ModelPreferenceStore,
        create_model_selection_router,
    )

    preferences = ModelPreferenceStore(deps.scheduled_task_store.storage_location)
    app.state.model_preference_store = preferences
    from omnigent.superchat.models.selection import foundation_resolver

    # The Super Chat's Foundation pick, read by the core create and model-change paths.
    app.state.foundation_model = foundation_resolver(preferences)
    app.include_router(
        create_model_selection_router(
            preferences,
            auth_provider=deps.auth_provider,
            permission_store=deps.permission_store,
            conversation_store=deps.conversation_store,
        ),
        prefix="/v1",
        tags=["model-selection"],
    )
    from omnigent.superchat.models.budget import ModelBudgetStore, bind_budgets
    from omnigent.superchat.models.budget_routes import create_budget_router

    budgets = ModelBudgets(
        ModelBudgetStore(deps.scheduled_task_store.storage_location), deps.conversation_store
    )
    app.state.model_budgets = budgets
    bind_budgets(budgets)
    app.include_router(
        create_budget_router(
            budgets, auth_provider=deps.auth_provider, permission_store=deps.permission_store
        ),
        prefix="/v1",
        tags=["model-budget"],
    )
    from omnigent.superchat.models.org import bind_suspensions

    suspensions = SuspensionStore(deps.scheduled_task_store.storage_location)
    app.state.model_suspensions = suspensions
    bind_suspensions(suspensions)
    from omnigent.superchat.models.embeddings import (
        EmbeddingSettingStore,
        ModelEmbeddings,
        bind_embeddings,
    )

    embedding_settings = EmbeddingSettingStore(deps.scheduled_task_store.storage_location)
    app.state.model_embeddings = ModelEmbeddings(store, embedding_settings)
    bind_embeddings(app.state.model_embeddings)
    from omnigent.superchat.models.rerank import ModelRerank

    app.state.model_rerank = ModelRerank(store, app.state.model_embeddings)
    from omnigent.llms.background import register_background_llm, register_session_llm
    from omnigent.superchat.models.background import BackgroundModels

    app.state.model_background = BackgroundModels(app.state)
    register_background_llm(app.state.model_background.resolve)

    async def _session_llm(owner: str | None, snapshot: Any, model: str | None) -> Any:
        return await app.state.model_background.for_session(owner, snapshot, model=model)

    register_session_llm(_session_llm)
    app.include_router(
        create_model_connection_router(
            store,
            auth_provider=deps.auth_provider,
            permission_store=deps.permission_store,
            embedding_settings=embedding_settings,
        ),
        prefix="/v1",
        tags=["model-connections"],
    )


FEATURE = Feature(name="models", tools=_tools, install=_install)

#: What ``superchat/admin`` and ``superchat/knowledge`` (the declared dependents) may read through
#: the package entry point.
__all__ = [
    "DEFAULT_EMBEDDING_DIMENSIONS",
    "FEATURE",
    "REASON_ERROR",
    "REASON_NO_CONNECTION",
    "SCOPE_USER",
    "EmbeddingUnavailable",
    "ModelBudgets",
    "ModelConnectionStore",
    "ModelEmbeddings",
    "SuspensionStore",
    "budget",
    "get_embeddings",
]
