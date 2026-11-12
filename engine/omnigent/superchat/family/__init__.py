"""Family: one event stream for a Super Chat and its Helpers.

Layout: ``stream`` (the fan-in over the per-session streams), ``signals`` (changes no session
stream carries), ``routes`` (the SSE route), this module (registration).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, InstallDeps

if TYPE_CHECKING:
    from fastapi import FastAPI


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from fastapi import APIRouter

    from omnigent.superchat.family.routes import register_family_routes

    router = APIRouter()
    register_family_routes(
        router,
        conversation_store=deps.conversation_store,
        auth_provider=deps.auth_provider,
        permission_store=deps.permission_store,
    )
    app.include_router(router, prefix="/v1", tags=["family"])


FEATURE = Feature(name="family", tools=lambda _labels, _ctx: [], install=_install)
