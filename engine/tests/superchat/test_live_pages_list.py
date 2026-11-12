"""``GET /v1/me/live-pages``: the caller's live pages in one call."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.live.routes import create_live_router
from omnigent.superchat.live.service import LiveService
from omnigent.superchat.live.store import SqlAlchemyLiveStore

_SESSION = uuid.uuid4().hex
ALICE = {"x-user": "alice"}
BOB = {"x-user": "bob"}
PAGE = b'<html><body><p data-superchat-id="a">A</p></body></html>'
RRULE = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=0"


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


class _Convs:
    def get_conversation(self, _id: str) -> Any:
        return SimpleNamespace(agent_id=uuid.uuid4().hex)


@pytest.fixture()
def env(db_uri: str, tmp_path: Path):
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    artifacts = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    live = SqlAlchemyLiveStore(db_uri)
    service = LiveService(
        artifacts, live, SqlAlchemyScheduledTaskStore(db_uri), _Convs(), lambda: None
    )  # fmt: skip
    app = FastAPI()
    app.include_router(create_live_router(service, auth_provider=_Auth()), prefix="/v1")  # type: ignore[arg-type]
    return SimpleNamespace(artifacts=artifacts, service=service, live=live, client=TestClient(app))


def _page(env: Any, name: str, user: str = "alice"):
    return env.artifacts.create(
        user_id=user, parent_session_id=_SESSION, name=name, title=name, kind="html",
        mime=KIND_MIME["html"], data=PAGE,
    )  # fmt: skip


def test_lists_only_the_callers_live_pages_with_their_state(env) -> None:
    mine, other, plain = _page(env, "a.html"), _page(env, "b.html", "bob"), _page(env, "c.html")
    env.service.upsert(mine, rrule=RRULE)
    env.service.upsert(other, rrule=RRULE)
    assert plain
    row = env.live.get(_SESSION, "a.html")
    row.state.update(failures=3, last_failure={"at": 5, "reason": "no source"})
    env.live.save(row)
    env.service.tasks.update(row.task_id, state="paused")

    pages = env.client.get("/v1/me/live-pages", headers=ALICE).json()["pages"]

    assert [p["name"] for p in pages] == ["a.html"]
    assert pages[0]["artifact_id"] == mine.id
    assert pages[0]["paused"] is True
    assert pages[0]["failures"] == 3
    assert pages[0]["last_failure"]["reason"] == "no source"
    assert env.client.get("/v1/me/live-pages", headers=BOB).json()["pages"][0]["name"] == "b.html"
