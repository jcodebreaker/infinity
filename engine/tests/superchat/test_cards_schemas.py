"""``GET /v1/cards/schemas``: the render_card catalog served from its single source."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.superchat.cards.routes import create_cards_router
from omnigent.superchat.cards.tools import CARD_DATA_SCHEMAS


class _Auth:
    def __init__(self, user: str | None) -> None:
        self._user = user

    def get_user_id(self, _request: object) -> str | None:
        return self._user


def _client(user: str | None) -> TestClient:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: object, exc: OmnigentError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(exc)}, status_code=401)

    app.include_router(create_cards_router(auth_provider=_Auth(user)), prefix="/v1")
    return TestClient(app)


def test_lists_every_kind_with_its_schema() -> None:
    body = _client("u1").get("/v1/cards/schemas").json()
    assert [c["kind"] for c in body["cards"]] == list(CARD_DATA_SCHEMAS)
    by_kind = {c["kind"]: c for c in body["cards"]}
    assert by_kind["quote"]["data_schema"] == CARD_DATA_SCHEMAS["quote"]
    assert by_kind["progress"]["ends_reply"] is False
    assert by_kind["sources"]["ends_reply"] is True


def test_requires_auth() -> None:
    assert _client(None).get("/v1/cards/schemas").status_code == 401
