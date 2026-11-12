"""Publishing an HTML artifact as a web app: state, audience, slug, pinning, views, gateway."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat.approvals.policy import classify_tool_call
from omnigent.superchat.apps import feature as _feature  # noqa: F401  (registers classifiers)
from omnigent.superchat.apps.routes import PUBLISHED_GATEWAY_USER_DEFAULT, create_apps_router
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts import store as store_mod
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore, make_slug

_SESSION = uuid.uuid4().hex
ALICE = {"x-user": "alice"}
BOB = {"x-user": "bob"}
GATEWAY = {"x-user": PUBLISHED_GATEWAY_USER_DEFAULT}
HTML = b"<!doctype html><title>Todo</title><script>1</script>"


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


@pytest.fixture()
def env(db_uri: str, tmp_path: Path):
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.http_status)

    class _Convs:
        pass

    app.include_router(
        create_artifacts_router(store, conversation_store=_Convs(), auth_provider=_Auth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    app.include_router(create_apps_router(store, auth_provider=_Auth()), prefix="/v1")
    return store, TestClient(app)


def _save(store: SqlAlchemyArtifactStore, name: str = "todo.html", data: bytes = HTML, **kw: Any):
    kind = name.rsplit(".", 1)[1]
    return store.create(
        user_id="alice", parent_session_id=_SESSION, name=name, title=kw.pop("title", "Todo list"),
        kind=kind, mime=KIND_MIME[kind], data=data, **kw,
    )  # fmt: skip


def _publish(client: TestClient, artifact_id: str, audience: str = "link", **extra: Any):
    return client.post(
        f"/v1/artifacts/{artifact_id}/publish", json={"audience": audience, **extra}, headers=ALICE
    )


def test_slug_is_url_safe_with_random_suffix() -> None:
    slug = make_slug("Budget Tracker: Q4 / 2026!", "x.html")
    assert slug.startswith("budget-tracker-q4-2026-")
    assert all(c.isalnum() or c == "-" for c in slug)
    assert make_slug(None, "My App.html").startswith("my-app-")
    assert make_slug("日本語", "a.html").startswith("a-") or make_slug(
        "日本語", "a.html"
    ).startswith("app-")
    assert make_slug("t", "a.html") != make_slug("t", "a.html")


def test_publish_returns_address_and_state(env) -> None:
    store, client = env
    art = _save(store)
    res = _publish(client, art.id, "org")
    assert res.status_code == 200
    body = res.json()
    assert body["audience"] == "org" and body["version"] == 1
    assert body["url_path"] == f"/apps/{body['slug']}"
    assert body["stats"] == {"opens_total": 0, "unique_viewers": 0, "opens_7d": 0}
    state = client.get(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json()
    assert state["published"] is True and state["publish"]["slug"] == body["slug"]
    meta = client.get(f"/v1/artifacts/{art.id}", headers=ALICE).json()
    assert meta["published"] is True and meta["publish"]["audience"] == "org"
    listed = client.get("/v1/artifacts", headers=ALICE).json()["artifacts"][0]
    assert listed["published"] is True and listed["publish"]["slug"] == body["slug"]


def test_only_html_and_known_audience(env) -> None:
    store, client = env
    csv = _save(store, "t.csv", b"a,b\n1,2\n")
    assert _publish(client, csv.id).status_code == 400
    html = _save(store)
    assert _publish(client, html.id, "everyone").status_code == 400
    assert _publish(client, html.id, "link", version=9).status_code == 400


def test_owner_only(env) -> None:
    store, client = env
    art = _save(store)
    for method in ("post", "get", "delete"):
        kw = {"json": {"audience": "link"}} if method == "post" else {}
        res = getattr(client, method)(f"/v1/artifacts/{art.id}/publish", headers=BOB, **kw)
        assert res.status_code == 404
    assert _publish(client, art.id).status_code == 200


def test_republish_keeps_slug_and_repins_version(env) -> None:
    store, client = env
    v1 = _save(store)
    first = _publish(client, v1.id, "link").json()
    v2 = _save(store, data=b"<html>second</html>")
    assert v2.version == 2
    # pinned to v1 until republished
    got = client.get(f"/v1/published/{first['slug']}", headers=GATEWAY)
    assert got.content == HTML
    again = _publish(client, v1.id, "owner").json()
    assert again["slug"] == first["slug"] and again["audience"] == "owner"
    assert again["version"] == 2
    assert (
        client.get(f"/v1/published/{first['slug']}", headers=GATEWAY).content
        == b"<html>second</html>"
    )
    pinned = _publish(client, v2.id, "owner", version=1).json()
    assert pinned["version"] == 1


def test_published_read_is_gateway_only_and_carries_audience(env) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id, "org").json()["slug"]
    ok = client.get(f"/v1/published/{slug}", headers=GATEWAY)
    assert ok.status_code == 200 and ok.content == HTML
    assert ok.headers["x-published-audience"] == "org"
    assert ok.headers["x-published-owner"] == "alice"
    assert ok.headers["x-published-workspace"] == "0"
    assert ok.headers["cache-control"] == "no-store"
    assert client.get(f"/v1/published/{slug}", headers=ALICE).status_code == 404
    assert client.get(f"/v1/published/{slug}", headers=BOB).status_code == 404
    assert client.get("/v1/published/nope-abc123", headers=GATEWAY).status_code == 404


def test_unpublish_takes_it_offline(env) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id).json()["slug"]
    client.post(f"/v1/published/{slug}/view", json={"viewer_key": "u1"}, headers=GATEWAY)
    assert client.delete(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json() == {
        "unpublished": True
    }
    assert client.get(f"/v1/published/{slug}", headers=GATEWAY).status_code == 404
    assert (
        client.get(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json()["published"] is False
    )
    assert client.delete(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json() == {
        "unpublished": False
    }
    assert client.post(
        f"/v1/published/{slug}/view", json={"viewer_key": "u1"}, headers=GATEWAY
    ).json() == {"counted": False}


def test_delete_artifact_removes_publication(env) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id).json()["slug"]
    assert client.delete(f"/v1/artifacts/{art.id}", headers=ALICE).status_code == 200
    assert client.get(f"/v1/published/{slug}", headers=GATEWAY).status_code == 404


def test_views_count_distinct_viewers_and_dedupe_rapid_repeats(env, monkeypatch) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id).json()["slug"]

    def view(key: str) -> bool:
        res = client.post(f"/v1/published/{slug}/view", json={"viewer_key": key}, headers=GATEWAY)
        return res.json()["counted"]

    assert view("acct-1") is True
    assert view("acct-1") is False  # rapid refresh
    assert view("hash-9") is True
    stats = client.get(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json()["publish"]["stats"]
    assert stats == {"opens_total": 2, "unique_viewers": 2, "opens_7d": 2}

    real = time.time()
    monkeypatch.setattr(store_mod, "now_epoch", lambda: int(real) + 60)
    assert view("acct-1") is True  # a later open counts again
    stats = client.get(f"/v1/artifacts/{art.id}/publish", headers=ALICE).json()["publish"]["stats"]
    assert stats["opens_total"] == 3 and stats["unique_viewers"] == 2


def test_opens_7d_excludes_old_days(env) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id).json()["slug"]
    from omnigent.db.db_models import SqlArtifactView

    old_day = store_mod._utc_day(int(time.time()) - 20 * 86400)
    with store._session_immediate("seed_old_view") as session:
        session.add(SqlArtifactView(slug=slug, day=old_day, viewer_key="old", opens=5, last_at=1))
    stats = store.stats([slug])[slug]
    assert (stats.opens_total, stats.unique_viewers, stats.opens_7d) == (5, 1, 0)


def test_view_requires_gateway(env) -> None:
    store, client = env
    art = _save(store)
    slug = _publish(client, art.id).json()["slug"]
    res = client.post(f"/v1/published/{slug}/view", json={"viewer_key": "x"}, headers=BOB)
    assert res.status_code == 404


def test_publish_tool_always_asks() -> None:
    risk = classify_tool_call("artifact_publish", {"artifact_id": "a" * 32, "audience": "link"})
    assert risk is not None and "anyone with the link" in risk.summary
    assert classify_tool_call("mcp__omnigent__artifact_publish", {"audience": "org"}) is not None


def test_publication_migration_round_trip(tmp_path: Path) -> None:
    import sqlalchemy as sa
    from alembic import command

    from omnigent.db.utils import _build_alembic_config, get_or_create_engine

    uri = f"sqlite:///{tmp_path / 'pub.db'}"
    engine = get_or_create_engine(uri)
    SqlAlchemyArtifactStore(uri, lambda: None)  # type: ignore[arg-type,return-value]
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "ve1b2c3d4e5f")
    tables = sa.inspect(engine).get_table_names()
    assert "artifact_publications" not in tables and "artifact_views" not in tables
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "vf1b2c3d4e5f")
    tables = sa.inspect(engine).get_table_names()
    assert "artifact_publications" in tables and "artifact_views" in tables
