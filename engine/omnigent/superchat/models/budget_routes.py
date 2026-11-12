"""Budget routes: ``/v1/me/budget`` (a person's own) and ``/v1/admin/budget`` (the organization's).

Both ``GET`` and ``PUT`` take and return ``{monthly_limit_usd, at_limit}`` plus this month's spend.
``PUT`` replaces the whole budget: ``monthly_limit_usd: null`` removes the limit, ``at_limit`` is
``"stop"`` or ``"ask"`` (default). The person's response also carries the organization's cap
read-only; the organization's total spend is shown to admins only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_admin, require_user
from omnigent.superchat.models.budget import (
    DEFAULT_AT_LIMIT,
    WORKSPACE_OWNER,
    Budget,
    ModelBudgets,
    month_start,
    parse_budget,
)


class PutBudget(BaseModel):
    """Body of ``PUT .../budget``; values are validated in :func:`parse_budget`."""

    model_config = ConfigDict(extra="forbid")

    monthly_limit_usd: Any = None
    at_limit: Any = DEFAULT_AT_LIMIT


def _view(budget: Budget, spent: float) -> dict[str, Any]:
    return {
        "monthly_limit_usd": budget.monthly_limit_usd,
        "at_limit": budget.at_limit,
        "spent_month_usd": spent,
        "period_start": month_start(),
    }


def create_budget_router(
    budgets: ModelBudgets,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: Any = None,
) -> APIRouter:
    """Build the router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    async def _is_admin(user_id: str | None) -> bool:
        if permission_store is None or user_id is None:
            return permission_store is None
        return bool(await asyncio.to_thread(permission_store.is_admin, user_id))

    async def _me_view(owner: str, *, admin: bool) -> dict[str, Any]:
        def read() -> dict[str, Any]:
            view = _view(budgets.store.get(owner), budgets.spent(owner))
            org = budgets.store.get(WORKSPACE_OWNER)
            view["org_limit_usd"] = org.monthly_limit_usd
            view["org_at_limit"] = org.at_limit if org.active else None
            if admin:
                view["org_spent_month_usd"] = budgets.org_spent()
            return view

        return await asyncio.to_thread(read)

    @router.get("/me/budget")
    async def get_my_budget(request: Request) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        return await _me_view(user_id or RESERVED_USER_LOCAL, admin=await _is_admin(user_id))

    @router.put("/me/budget")
    async def put_my_budget(request: Request, body: PutBudget) -> dict[str, Any]:
        """Set the caller's budget (``INVALID_INPUT`` for a bad limit or action)."""
        user_id = require_user(request, auth_provider)
        budget = parse_budget(body.monthly_limit_usd, body.at_limit)
        owner = user_id or RESERVED_USER_LOCAL
        await asyncio.to_thread(budgets.store.set, owner, budget)
        return await _me_view(owner, admin=await _is_admin(user_id))

    async def _org_view() -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: _view(budgets.store.get(WORKSPACE_OWNER), budgets.org_spent())
        )

    async def _admin(request: Request) -> None:
        await require_admin(
            request,
            auth_provider,
            permission_store,
            message="Admin privileges required to manage the organization's model budget",
        )

    @router.get("/admin/budget")
    async def get_org_budget(request: Request) -> dict[str, Any]:
        await _admin(request)
        return await _org_view()

    @router.put("/admin/budget")
    async def put_org_budget(request: Request, body: PutBudget) -> dict[str, Any]:
        """Set the organization's budget (admin only)."""
        await _admin(request)
        budget = parse_budget(body.monthly_limit_usd, body.at_limit)
        await asyncio.to_thread(budgets.store.set, WORKSPACE_OWNER, budget)
        return await _org_view()

    return router
