"""The single local user saves, restores and reads pending edits in its own sessions."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat.artifacts import routes as artifact_routes
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore, VersionConflictError

_SESSION = uuid.uuid4().hex


class _LocalAuth:
    """Header auth on a single-user runtime: a request with no identity header is "local"."""

    def get_user_id(self, request: Request) -> str | None:
        return RESERVED_USER_LOCAL


class _Convs:
    pass


@pytest.fixture()
def env(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    checked: list[Any] = []

    async def _require_access(user_id: str | None, *_args: Any, **_kwargs: Any) -> None:
        # The real check refuses ``None`` as unauthenticated whenever permissions are on.
        checked.append(user_id)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)

    monkeypatch.setattr(artifact_routes, "require_access", _require_access)
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.http_status)

    app.include_router(
        create_artifacts_router(
            store,
            conversation_store=_Convs(),  # type: ignore[arg-type]
            auth_provider=_LocalAuth(),  # type: ignore[arg-type]
            permission_store=object(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return checked, TestClient(app), store


def test_local_user_saves_an_artifact_and_reads_pending_edits(env) -> None:
    checked, client, _store = env
    saved = client.post(
        f"/v1/artifacts?parent_session_id={_SESSION}&name=tea.deck.html",
        content=b"<html><body>tea</body></html>",
        headers={"content-type": "text/html"},
    )
    assert saved.status_code == 201, saved.text
    pending = client.get(f"/v1/artifacts/pending-edits?parent_session_id={_SESSION}")
    assert pending.status_code == 200, pending.text
    assert pending.json() == {"edits": []}
    assert checked == [RESERVED_USER_LOCAL, RESERVED_USER_LOCAL]


def _save_two_versions(client: TestClient) -> list[dict[str, Any]]:
    url = f"/v1/artifacts?parent_session_id={_SESSION}&name=tea.html"
    saved = [
        client.post(url, content=body, headers={"content-type": "text/html"})
        for body in (b"<p>one</p>", b"<p>two</p>")
    ]
    assert [r.status_code for r in saved] == [201, 201], saved[-1].text
    return [r.json() for r in saved]


def test_local_user_restores_an_earlier_version(env) -> None:
    _checked, client, store = env
    v1, _v2 = _save_two_versions(client)
    restored = client.post(f"/v1/artifacts/{v1['id']}/restore")
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert (body["version"], body["origin"]) == (3, "restore")
    item = store.get(body["id"], user_id=None)
    assert (
        item is not None
        and item.user_id is None
        and store.read(item) == b'<p data-superchat-id="p-one">one</p>'
    )


def test_restore_over_a_version_that_arrived_meanwhile_is_a_409(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _checked, client, store = env
    v1, _v2 = _save_two_versions(client)

    def _raced(**_kwargs: Any) -> None:
        raise VersionConflictError(3)

    monkeypatch.setattr(store, "create", _raced)
    resp = client.post(f"/v1/artifacts/{v1['id']}/restore")
    assert resp.status_code == 409
    assert "newer version arrived" in resp.json()["error"]
