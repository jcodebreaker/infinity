"""``GET /apps/<slug>``: a published app, gated by audience and sandboxed by its headers."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat.apps.routes import PUBLISHED_APP_CSP, create_published_pages_router
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore

ALICE = {"x-user": "alice"}
BOB = {"x-user": "bob"}
HTML = b"<!doctype html><title>Todo</title><script>1</script>"


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


@pytest.fixture()
def env(db_uri: str, tmp_path: Path):
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    app = FastAPI()
    app.include_router(create_published_pages_router(store, auth_provider=_Auth()))
    return store, TestClient(app)


def _published(store: SqlAlchemyArtifactStore, audience: str) -> str:
    art = store.create(
        user_id="alice", parent_session_id=uuid.uuid4().hex, name="todo.html", title="Todo",
        kind="html", mime=KIND_MIME["html"], data=HTML,
    )  # fmt: skip
    return store.publish(art, audience=audience, version=None).slug


def _assert_locked(res, *, cache: str) -> None:
    assert res.headers["content-security-policy"] == PUBLISHED_APP_CSP
    assert "sandbox" in PUBLISHED_APP_CSP and "allow-same-origin" not in PUBLISHED_APP_CSP
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"
    assert res.headers["cache-control"] == cache


def test_link_app_is_public_with_security_headers(env) -> None:
    store, client = env
    slug = _published(store, "link")
    res = client.get(f"/apps/{slug}")
    assert res.status_code == 200 and res.content == HTML
    _assert_locked(res, cache="public, no-cache")
    head = client.head(f"/apps/{slug}")
    assert head.status_code == 200 and head.content == b""


def test_owner_audience_only_for_owner(env) -> None:
    store, client = env
    slug = _published(store, "owner")
    anon = client.get(f"/apps/{slug}")
    assert anon.status_code == 403 and b"Private app" in anon.content
    _assert_locked(anon, cache="no-store")
    other = client.get(f"/apps/{slug}", headers=BOB)
    assert other.status_code == 403 and b"Not available to you" in other.content
    mine = client.get(f"/apps/{slug}", headers=ALICE)
    assert mine.status_code == 200 and mine.content == HTML
    _assert_locked(mine, cache="no-store")


def test_org_audience_needs_a_signed_in_viewer(env) -> None:
    store, client = env
    slug = _published(store, "org")
    assert client.get(f"/apps/{slug}").status_code == 403
    assert client.get(f"/apps/{slug}", headers=BOB).status_code == 200


def test_unknown_or_malformed_slug_is_404_with_headers(env) -> None:
    _, client = env
    for slug in ("nope-12345", "BAD_SLUG", "x"):
        res = client.get(f"/apps/{slug}")
        assert res.status_code == 404
        _assert_locked(res, cache="no-store")


def test_views_count_for_non_owners_only(env) -> None:
    store, client = env
    slug = _published(store, "link")
    client.get(f"/apps/{slug}", headers=ALICE)
    assert slug not in store.stats([slug])
    client.get(f"/apps/{slug}", headers=BOB)
    client.get(f"/apps/{slug}")
    stats = store.stats([slug])[slug]
    assert stats.opens_total == 2 and stats.unique_viewers == 2
