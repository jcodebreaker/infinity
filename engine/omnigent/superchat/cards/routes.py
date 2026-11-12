"""``GET /v1/cards/schemas``: the card kinds a client must be able to render."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from omnigent.server.routes._auth_helpers import require_user
from omnigent.superchat.cards.tools import card_catalog


def create_cards_router(*, auth_provider: Any = None) -> APIRouter:
    """Build the cards router, mounted with ``prefix="/v1"``."""
    router = APIRouter()

    @router.get("/cards/schemas")
    async def card_schemas(request: Request) -> dict[str, Any]:
        """Every ``render_card`` kind with the JSON schema of its ``data``."""
        require_user(request, auth_provider)
        return {"cards": card_catalog()}

    return router
