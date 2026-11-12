"""Attachment upload type/size enforcement on POST /v1/sessions/{id}/resources/files."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import HOST_CAPABILITIES, HostHelloFrame
from omnigent.runtime.content_resolver import (
    MAX_TEXT_UPLOAD_BYTES,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@pytest.fixture
def upload_client(db_uri: str, tmp_path) -> Iterator[tuple[TestClient, str]]:
    """A sessions route client with file + artifact stores and one session."""
    conversation_store = SqlAlchemyConversationStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store.create(
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
        name="test-agent",
        bundle_location="087b7cb7ac30abf4debfaa578d052ec6/bundle",
    )
    conv = conversation_store.create_conversation(
        title="upload session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    conversation_store.set_host_id(
        conv.id, "d75381f2c94b4e49a3c684946d4ddbc4", workspace=str(tmp_path)
    )
    host_registry = HostRegistry()
    host_registry.register(
        "d75381f2c94b4e49a3c684946d4ddbc4",
        Mock(),
        HostHelloFrame(
            version="0.15.0",
            frame_protocol_version=1,
            name="upload",
            capabilities=HOST_CAPABILITIES,
        ),
        owner=None,
    )

    app = FastAPI()
    app.state.host_registry = host_registry

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,
            agent_store=agent_store,
            file_store=file_store,
            artifact_store=artifact_store,
            host_registry=host_registry,
        ),
        prefix="/v1",
    )

    with TestClient(app) as client:
        yield client, conv.id


def _upload(
    client: TestClient,
    session_id: str,
    filename: str,
    data: bytes = b"data",
    content_type: str = "application/octet-stream",
) -> httpx.Response:
    """Post a multipart file through the actual upload route."""
    return client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (filename, data, content_type)},
    )


def test_upload_rejects_unsupported_type(upload_client: tuple[TestClient, str]) -> None:
    """Unsupported formats are rejected with 415."""
    client, session_id = upload_client
    resp = _upload(client, session_id, "clip.mp4", b"\x00\x00\x00 fake mp4 bytes", "video/mp4")
    assert resp.status_code == 415, resp.text
    assert "Unsupported attachment type" in resp.text


def test_upload_rejects_filesystem_types_for_an_unsupported_harness(
    upload_client: tuple[TestClient, str], db_uri: str
) -> None:
    """An SDK session refuses formats requiring filesystem tools."""
    client, _ = upload_client
    sdk_session = SqlAlchemyConversationStore(db_uri).create_conversation(
        title="sdk session", agent_id="087b7cb7ac30abf4debfaa578d052ec6"
    )
    resp = client.post(
        f"/v1/sessions/{sdk_session.id}/resources/files",
        files={"file": ("archive.zip", b"PK\x03\x04 fake zip", "application/zip")},
    )
    assert resp.status_code == 415, resp.text
    assert "cannot open this file type" in resp.text


@pytest.mark.parametrize(
    "block",
    [
        {
            "type": "input_file",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
        # A file_id alongside inline bytes would skip re-resolution, so it is refused too.
        {
            "type": "input_file",
            "file_id": "file_abc",
            "filename": "payload.zip",
            "file_data": "data:application/zip;base64,UEs=",
        },
    ],
)
def test_message_cannot_inline_a_filesystem_attachment(
    upload_client: tuple[TestClient, str], block: dict[str, str]
) -> None:
    """Inline bytes would reach the harness without the upload route's checks."""
    client, session_id = upload_client
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "message", "data": {"role": "user", "content": [block]}},
    )
    assert resp.status_code == 400, resp.text
    assert "payload.zip" in resp.text


def test_upload_rejects_undecodable_oversized_image(
    upload_client: tuple[TestClient, str],
) -> None:
    """Image bytes over the model budget that don't decode are rejected 413.

    Real images are downscaled under the budget; garbage that only claims to
    be an image can't be compressed, so the route surfaces a 413 instead of
    storing an oversized attachment.
    """
    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    oversized = b"\x00" * (IMAGE_MODEL_BUDGET_BYTES + 1)
    resp = _upload(client, session_id, "huge.png", oversized, "image/png")
    assert resp.status_code == 413, resp.status_code


def test_upload_large_image_is_compressed_under_budget(
    upload_client: tuple[TestClient, str],
) -> None:
    """A large but valid image uploads and is stored shrunk under the model budget."""
    import os
    from io import BytesIO

    from PIL import Image

    from omnigent.runtime.content_resolver import IMAGE_MODEL_BUDGET_BYTES

    client, session_id = upload_client
    side = 1600
    buffer = BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    assert len(payload) > IMAGE_MODEL_BUDGET_BYTES

    resp = _upload(client, session_id, "screenshot.png", payload, "image/png")
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["metadata"]["bytes"] <= IMAGE_MODEL_BUDGET_BYTES
    # Opaque image re-encodes (WebP preferred, JPEG fallback), so the stored
    # name is realigned to match the new type.
    assert body["name"] in ("screenshot.webp", "screenshot.jpg")


def test_upload_text_just_under_limit_succeeds(upload_client: tuple[TestClient, str]) -> None:
    """A text file just under the text cap is accepted."""
    client, session_id = upload_client
    payload = b"a" * (MAX_TEXT_UPLOAD_BYTES - 1024)
    resp = _upload(client, session_id, "big.txt", payload, "text/plain")
    assert resp.status_code in (200, 201), resp.status_code


@pytest.mark.parametrize("size,rejected", [(100, False), (101, True)])
async def test_read_upload_capped_boundary(size: int, rejected: bool) -> None:
    """The read cap admits the exact limit and rejects one byte more."""
    from io import BytesIO

    from fastapi import HTTPException, UploadFile

    from omnigent.server.routes.sessions import _read_upload_capped

    data = b"x" * size
    upload = UploadFile(file=BytesIO(data))
    if rejected:
        with pytest.raises(HTTPException) as error:
            await _read_upload_capped(upload, 100)
        assert error.value.status_code == 413
    else:
        assert await _read_upload_capped(upload, 100) == data


def test_quota_counts_filesystem_files_past_any_page_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota accounting walks beyond 20 pages of ordinary files."""
    from fastapi import HTTPException

    from omnigent.entities import StoredFile
    from omnigent.entities.pagination import PagedList
    from omnigent.server.routes._sessions.helpers import _enforce_filesystem_attachment_policy

    records = [
        StoredFile(id=f"f{i:03d}", created_at=i, filename=f"n{i}.txt", bytes=2) for i in range(30)
    ] + [StoredFile(id="f999", created_at=999, filename="one.zip", bytes=4)]

    class _OnePerPageStore:
        """Serves the session's files one record per page, oldest first."""

        def list(self, session_id: str, limit: int, after: str | None, order: str):
            del session_id, limit, order
            index = 0 if after is None else [r.id for r in records].index(after) + 1
            page = records[index : index + 1]
            return PagedList(
                data=page,
                first_id=page[0].id if page else None,
                last_id=page[-1].id if page else None,
                has_more=index + 1 < len(records),
            )

    monkeypatch.setattr(
        "omnigent.server.server_config.filesystem_attachment_file_limit",
        lambda: 1,
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_filesystem_attachment_policy(
            ["two.zip"],
            session_id="conv_1",
            file_store=_OnePerPageStore(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413


def test_old_host_keeps_existing_attachment_types(upload_client: tuple[TestClient, str]) -> None:
    """The upgrade requirement does not change existing text/image uploads."""
    from io import BytesIO

    from PIL import Image

    client, session_id = upload_client
    client.app.state.host_registry.get("d75381f2c94b4e49a3c684946d4ddbc4").hello.capabilities = []
    image = BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    for filename, data, mime in (
        ("table.csv", b"a,b\n1,2\n", "text/csv"),
        ("picture.png", image.getvalue(), "image/png"),
    ):
        response = _upload(client, session_id, filename, data, mime)
        assert response.status_code == 201, response.text
