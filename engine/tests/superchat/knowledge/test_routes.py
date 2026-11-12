"""The relays ask the host's runner for status, search, thumbnails; the engine keeps
nothing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat.artifacts import SqlAlchemyArtifactStore
from omnigent.superchat.knowledge import runtime as rt
from omnigent.superchat.knowledge.routes import create_knowledge_router
from tests.superchat.knowledge._support import PDF, SESSION, FakeRunner, make_runtime, put_pdf

BASE = f"/v1/sessions/{SESSION}/knowledge"


class _Conversations:
    def get_conversation(self, session_id: str):
        return SimpleNamespace(id=session_id) if session_id == SESSION else None


@pytest.fixture(autouse=True)
def _fresh_runtime():
    yield
    rt.reset_runtime()


class World:
    """An app over the relays, with the runner awake (a fake) or asleep."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        uri = f"sqlite:///{tmp_path / 'e.db'}"
        OmnigentBase.metadata.create_all(get_or_create_engine(uri))
        self.blobs = LocalArtifactStore(str(tmp_path / "blobs"))
        self.artifacts = SqlAlchemyArtifactStore(uri, lambda: self.blobs)
        self.runtime = make_runtime(tmp_path, monkeypatch)
        self.runner = FakeRunner()
        self.awake = True
        self.wake_ok = True

        async def probe(*_a, **_k):
            return self.runner.client() if self.awake else None

        async def wake(**_k):
            if self.wake_ok:
                self.awake = True
                return self.runner.client(), None
            return None, None

        monkeypatch.setattr("omnigent.server.routes._sessions.helpers._get_runner_client", probe)
        monkeypatch.setattr(
            "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", wake
        )
        app = FastAPI()
        app.include_router(
            create_knowledge_router(
                conversation_store=_Conversations(),  # type: ignore[arg-type]
                artifact_store=self.artifacts,
                runner_router=object(),
            ),
            prefix="/v1",
        )

        @app.exception_handler(OmnigentError)
        async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
            return JSONResponse(
                {"code": str(exc.code), "message": str(exc)}, status_code=exc.http_status
            )

        self.client = TestClient(app)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


def test_status_relays_the_index_and_marks_library_files(world: World) -> None:
    put_pdf(world.runtime)
    absolute = str(world.runtime.indexer.workspace / "your_files" / "finance.pdf")
    saved = world.artifacts.create(
        user_id=None,
        parent_session_id=SESSION,
        name="finance.pdf",
        title=None,
        kind="pdf",
        mime="application/pdf",
        data=PDF,
        source_path=absolute,
    )
    body = world.client.get(f"{BASE}/status").json()
    assert body["host"] == "awake" and body["updated_at"]
    (file,) = body["files"]
    assert (file["state"], file["artifact_id"], file["path"]) == (
        "searchable",
        saved.id,
        "your_files/finance.pdf",
    )
    assert "abs_path" not in file


def test_asleep_status_is_the_last_known_and_never_wakes(world: World) -> None:
    put_pdf(world.runtime)
    assert world.client.get(f"{BASE}/status").json()["files"]
    world.awake, world.wake_ok = False, False
    calls = len(world.runner.calls)
    asleep = world.client.get(f"{BASE}/status").json()
    assert asleep["host"] == "asleep" and asleep["files"][0]["state"] == "searchable"
    assert len(world.runner.calls) == calls and world.awake is False


def test_asleep_with_nothing_known_is_an_empty_status(world: World) -> None:
    world.awake = False
    body = world.client.get(f"{BASE}/status").json()
    assert body == {
        "files": [],
        "embeddings": {"available": False, "reason": None},
        "host": "asleep",
        "updated_at": None,
    }


def test_search_wakes_the_host_and_returns_passages(world: World) -> None:
    put_pdf(world.runtime)
    world.awake = False
    found = world.client.post(f"{BASE}/search", json={"query": "currency exposure"}).json()
    assert world.awake and found["mode"] == "hybrid"
    assert found["results"][0]["page"] == 1 and found["results"][0]["artifact_id"] is None
    assert "type" not in found and "abs_path" not in found["results"][0]


def test_ingest_wakes_the_host_and_answers_when_the_file_is_searchable(world: World) -> None:
    world.awake = False
    path = world.runtime.indexer.workspace / "your_files/uploads/2026-10-09/r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF)
    r = world.client.post(f"{BASE}/ingest", json={"path": "your_files/uploads/2026-10-09/r.pdf"})
    assert r.status_code == 200 and world.awake
    body = r.json()
    assert body["pages"] == 1 and "markdown" not in body
    assert world.runtime.indexer.db.file_by_path("your_files/uploads/2026-10-09/r.pdf").state == (
        "ready"
    )
    assert (
        world.client.post(f"{BASE}/ingest", json={"path": "your_files/none.pdf"}).status_code
        >= 400
    )
    assert world.client.post(f"{BASE}/ingest", json={"path": "x", "extra": 1}).status_code == 422


def test_search_when_the_host_wont_start_is_a_calm_503(world: World) -> None:
    world.awake, world.wake_ok = False, False
    r = world.client.post(f"{BASE}/search", json={"query": "x"})
    assert r.status_code == 503 and "still starting" in r.json()["message"]


def test_search_rejects_bad_ids(world: World) -> None:
    r = world.client.post(f"{BASE}/search", json={"query": "x", "file_ids": ["nope"]})
    assert r.status_code == 400 or r.status_code == 422


def test_thumbnail_is_served_cached_and_never_wakes(world: World) -> None:
    put_pdf(world.runtime)
    fid = world.client.get(f"{BASE}/status").json()["files"][0]["file_id"]
    url = f"{BASE}/files/{fid}/pages/1/thumbnail"
    first = world.client.get(url)
    assert first.status_code == 200 and first.headers["content-type"] == "image/webp"
    world.awake, world.wake_ok = False, False
    assert world.client.get(url).content == first.content  # remembered
    assert world.client.get(f"{BASE}/files/{fid}/pages/2/thumbnail").status_code == 404
    assert world.awake is False


def test_reindex_only_runs_on_an_awake_host(world: World) -> None:
    assert world.client.post(f"{BASE}/reindex").json() == {"queued": 0}
    world.awake = False
    assert world.client.post(f"{BASE}/reindex").json() == {"queued": 0}


def test_unknown_session_is_404(world: World) -> None:
    assert world.client.get("/v1/sessions/" + "b" * 32 + "/knowledge/status").status_code == 404


def test_ingest_needs_write_access_and_only_reads_uploads(world: World, monkeypatch) -> None:
    seen: list[int] = []

    async def level(_user, _sid, level, *_rest):
        seen.append(level)

    monkeypatch.setattr("omnigent.superchat.knowledge.routes._require_access", level)
    path = world.runtime.indexer.workspace / "your_files/uploads/2026-10-09/r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF)
    assert world.client.post(
        f"{BASE}/ingest", json={"path": "your_files/uploads/2026-10-09/r.pdf"}
    )
    assert seen == [2]  # LEVEL_EDIT
    for bad in ("your_files/other.pdf", "your_files/uploads/../x.pdf", "/etc/passwd"):
        r = world.client.post(f"{BASE}/ingest", json={"path": bad})
        assert r.status_code == 400, bad
    assert seen == [2]  # refused before any access check or wake


def test_an_ingest_timeout_is_its_own_error_not_a_starting_host(world: World) -> None:
    import httpx

    async def slow(_request):
        raise httpx.ReadTimeout("slow")

    world.runner.client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(slow), base_url="http://runner"
    )
    path = world.runtime.indexer.workspace / "your_files/uploads/d/r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF)
    r = world.client.post(f"{BASE}/ingest", json={"path": "your_files/uploads/d/r.pdf"})
    assert r.status_code == 504 and r.json()["code"] == "knowledge_timeout"
    assert "starting" not in r.json()["message"]


def test_every_relay_call_carries_the_relay_mark(world: World) -> None:
    import json

    bodies: list[dict] = []

    async def spy(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"output": json.dumps({"files": []})}})

    import httpx

    world.runner.client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(spy), base_url="http://runner"
    )
    world.client.get(f"{BASE}/status")
    assert bodies and all(b.get("_omnigent_relay") is True for b in bodies)


def test_find_answers_with_the_stored_upload_that_has_these_bytes(world: World) -> None:
    import hashlib

    path = world.runtime.indexer.workspace / "your_files/uploads/2026-10-09/r.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF)
    world.runtime.indexer.scan()
    from tests.superchat.knowledge._fixtures import run_all

    run_all(world.runtime.indexer)
    digest = hashlib.sha256(PDF).hexdigest()
    hit = world.client.post(f"{BASE}/find", json={"sha256": digest}).json()
    assert hit["found"] is True and hit["path"] == "your_files/uploads/2026-10-09/r.pdf"
    miss = world.client.post(f"{BASE}/find", json={"sha256": "0" * 64}).json()
    assert miss == {"found": False}
    assert world.client.post(f"{BASE}/find", json={"sha256": "xyz"}).status_code == 422


def test_only_a_silent_runner_is_a_timeout_a_runner_that_cannot_be_reached_is_not(
    world: World,
) -> None:
    import httpx

    async def refuse(_request):
        raise httpx.ConnectTimeout("no connection")

    world.runner.client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        transport=httpx.MockTransport(refuse), base_url="http://runner"
    )
    r = world.client.post(f"{BASE}/search", json={"query": "x"})
    assert r.status_code == 503 and r.json()["code"] == "runner_unavailable"
