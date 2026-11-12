"""``/v1/artifacts/{id}/live`` routes."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import ON_GROUP_DELETED, SqlAlchemyArtifactStore
from omnigent.superchat.live.feature import LIVE_FEATURE
from omnigent.superchat.live.routes import create_live_router
from omnigent.superchat.live.service import LiveService
from omnigent.superchat.live.store import SqlAlchemyLiveStore

_SESSION = uuid.uuid4().hex
ALICE = {"x-user": "alice"}
BOB = {"x-user": "bob"}
PAGE = b'<html><body><p data-superchat-id="btc">Bitcoin: 100</p></body></html>'
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
    service = LiveService(
        artifacts, SqlAlchemyLiveStore(db_uri), SqlAlchemyScheduledTaskStore(db_uri), _Convs(),
        lambda: None,
    )  # fmt: skip
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": exc.message, "code": str(exc.code)}, exc.http_status)

    app.include_router(
        create_artifacts_router(artifacts, conversation_store=_Convs(), auth_provider=_Auth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    app.include_router(
        create_live_router(service, auth_provider=_Auth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    fired: list[str] = []

    async def run_now(_workspace: int, task_id: str) -> bool:
        fired.append(task_id)
        return True

    app.state.scheduled_task_run_now = run_now
    return SimpleNamespace(
        artifacts=artifacts, service=service, client=TestClient(app), fired=fired
    )


def _page(env: Any, name: str = "prices.html", data: bytes = PAGE, user: str = "alice"):
    kind = name.rsplit(".", 1)[1]
    return env.artifacts.create(
        user_id=user, parent_session_id=_SESSION, name=name, title="Prices", kind=kind,
        mime=KIND_MIME[kind], data=data,
    )  # fmt: skip


def _put(env: Any, artifact_id: str, **body: Any):
    return env.client.put(
        f"/v1/artifacts/{artifact_id}/live", json={"rrule": RRULE, **body},
        headers=ALICE,
    )  # fmt: skip


def test_put_get_delete_lifecycle(env) -> None:
    page = _page(env)
    assert env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).json() == {"live": None}
    live = _put(env, page.id, sources=["https://example.com"], tell="every").json()["live"]
    assert live["rrule"] == RRULE and live["schedule"] == "active" and live["tell"] == "every"
    assert live["sources"] == ["https://example.com"]
    task = env.service.tasks.get(live["task_id"])
    assert (task.kind, task.agent_type, task.parent_session_id) == (
        "live_artifact",
        "worker",
        _SESSION,
    )
    assert "live_update_context" in task.prompt and "prices.html" in task.prompt
    assert (
        env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).json()["live"]["tell"]
        == "every"
    )
    assert (live["failures"], live["last_failure"]) == (0, None)
    env.service.on_run_outcome(task, "it broke")
    seen = env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).json()["live"]
    assert seen["failures"] == 1 and seen["last_failure"]["reason"] == "it broke"
    assert env.client.delete(f"/v1/artifacts/{page.id}/live", headers=ALICE).json() == {
        "deleted": True
    }
    assert env.service.tasks.get(live["task_id"]) is None
    assert env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).json() == {"live": None}
    assert env.artifacts.get(page.id, user_id="alice") is not None


def test_only_html_pages_and_valid_schedules_are_accepted(env) -> None:
    deck = _page(env, "talk.deck.html")
    pdf = _page(env, "report.pdf", b"%PDF-1.4")
    page = _page(env)
    assert _put(env, deck.id).status_code == 400
    assert _put(env, pdf.id).status_code == 400
    assert _put(env, page.id, rrule="nonsense").status_code == 400
    assert _put(env, page.id, sources=[""]).status_code == 422
    assert _put(env, page.id, rrule=RRULE, tell="loud").status_code == 422
    # A sheet can be kept updated like a page (its cells are the parts).
    import omnigent.superchat.sheets.parts  # noqa: F401

    sheet = _page(env, "teas.csv", b"tea,price\nSencha,12.50\n")
    assert _put(env, sheet.id).status_code == 200
    ctx = env.client.get(f"/v1/artifacts/{sheet.id}/live/context", headers=ALICE).json()
    assert ctx["source"] == "A1: tea | B1: price\nA2: Sencha | B2: 12.50"


def test_another_owner_gets_a_404_everywhere(env) -> None:
    page = _page(env)
    _put(env, page.id)
    base = f"/v1/artifacts/{page.id}/live"
    calls = [
        ("GET", "", None), ("PUT", "", {"rrule": RRULE}), ("DELETE", "", None),
        ("POST", "/run", None), ("POST", "/undo", None), ("GET", "/context", None),
        ("POST", "/accept", {"element_id": "btc"}), ("POST", "/save", {"base_version": 1}),
    ]  # fmt: skip
    for method, path, body in calls:
        resp = env.client.request(method, base + path, json=body, headers=BOB)
        assert resp.status_code == 404, (method, path)


def test_single_user_mode_reads_and_writes_unowned_rows(env) -> None:
    from omnigent.server.auth import RESERVED_USER_LOCAL

    local = {"x-user": RESERVED_USER_LOCAL}
    page = _page(env, user=None)  # type: ignore[arg-type]
    put = env.client.put(f"/v1/artifacts/{page.id}/live", json={"rrule": RRULE}, headers=local)
    assert put.status_code == 200 and env.service.live.get(_SESSION, page.name).user_id is None
    ctx = env.client.get(f"/v1/artifacts/{page.id}/live/context", headers=local)
    assert ctx.status_code == 200 and ctx.json()["version"] == 1
    assert env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).status_code == 404


def test_run_now_fires_the_task(env) -> None:
    page = _page(env)
    task_id = _put(env, page.id).json()["live"]["task_id"]
    assert env.client.post(f"/v1/artifacts/{page.id}/live/run", headers=ALICE).status_code == 202
    assert env.fired == [task_id]


def test_helper_round_trip_then_undo_and_stale_base(env) -> None:
    page = _page(env)
    _put(env, page.id)
    ctx = env.client.get(f"/v1/artifacts/{page.id}/live/context", headers=ALICE).json()
    assert ctx["version"] == 1 and "Bitcoin: 100" in ctx["source"]
    change = {"id": "btc", "after": "Bitcoin: 150", "notable": True}
    body = {"base_version": 1, "changes": [change],
            "facts": {"https://example.com": {"hash": "a", "values": {"btc": 150}}}}  # fmt: skip
    saved = env.client.post(f"/v1/artifacts/{page.id}/live/save", json=body, headers=ALICE).json()
    assert saved["version"] == 2 and saved["tell"] == "notable" and saved["applied"][0]["notable"]
    stale = env.client.post(f"/v1/artifacts/{page.id}/live/save", json=body, headers=ALICE)
    assert stale.status_code == 409 and stale.json()["code"] == "artifact_stale"
    live = env.client.get(f"/v1/artifacts/{page.id}/live", headers=ALICE).json()["live"]
    assert live["last_update"]["version"] == 2 and live["checked_at"]
    undone = env.client.post(f"/v1/artifacts/{page.id}/live/undo", headers=ALICE).json()
    assert undone["origin"] == "restore" and undone["version"] == 3
    again = env.client.post(f"/v1/artifacts/{page.id}/live/undo", headers=ALICE)
    assert again.status_code == 409
    checked = env.client.post(
        f"/v1/artifacts/{page.id}/live/save",
        json={"base_version": 1, "changes": []},
        headers=ALICE,
    )
    assert checked.status_code == 200 and checked.json()["version"] is None


def test_accept_route_applies_the_source_value(env) -> None:
    page = _page(env)
    _put(env, page.id)
    facts: dict[str, Any] = {}
    env.client.post(f"/v1/artifacts/{page.id}/live/save", headers=ALICE, json={
        "base_version": 1, "changes": [{"id": "btc", "after": "Bitcoin: 150"}],
        "facts": facts})  # fmt: skip
    head = env.artifacts.newest(page)
    env.artifacts.create(
        user_id="alice", parent_session_id=_SESSION, name=page.name, title=page.title,
        kind="html", mime=page.mime, origin="manual",
        data=env.artifacts.read(head).replace(b"Bitcoin: 150", b"Bitcoin: mine"),
    )  # fmt: skip
    env.client.post(f"/v1/artifacts/{page.id}/live/save", headers=ALICE, json={
        "base_version": 3, "changes": [{"id": "btc", "after": "Bitcoin: 160"}],
        "facts": facts})  # fmt: skip
    done = env.client.post(
        f"/v1/artifacts/{page.id}/live/accept", json={"element_id": "btc"}, headers=ALICE
    )
    assert done.status_code == 200 and done.json()["version"] == 4
    assert (
        env.client.post(
            f"/v1/artifacts/{page.id}/live/accept", json={"element_id": "btc"}, headers=ALICE
        ).status_code
        == 404
    )


def test_tools_are_offered_by_role() -> None:
    from omnigent.context.labels import (
        CONTEXT_MODE_LABEL,
        SUBAGENT_LABEL_KEY,
        SUPERSIDE_CHAT_MODE_VALUE,
    )
    from omnigent.superchat.feature import ToolManagerCtx

    def names(labels: dict[str, str]) -> list[str]:
        return [t.name() for t in LIVE_FEATURE.tools(labels, ToolManagerCtx())]

    chat = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
    assert names(chat) == [
        "artifact_keep_updated", "artifact_stop_updating", "artifact_live_settings",
    ]  # fmt: skip
    assert names({**chat, SUBAGENT_LABEL_KEY: "worker"}) == [
        "live_update_context", "live_update_save",
    ]  # fmt: skip
    assert names({}) == []
    assert set(LIVE_FEATURE.handlers) >= {"artifact_keep_updated", "live_update_save"}


def test_deleting_the_page_drops_its_live_row_and_task(env, monkeypatch) -> None:
    monkeypatch.setitem(ON_GROUP_DELETED, "live", env.service.remove)
    page = _page(env)
    task_id = _put(env, page.id).json()["live"]["task_id"]
    assert env.client.delete(f"/v1/artifacts/{page.id}", headers=ALICE).status_code == 200
    assert env.service.live.get(_SESSION, "prices.html") is None
    assert env.service.tasks.get(task_id) is None


def test_on_change_is_a_setting_and_the_host_routes_need_its_launch_token(env) -> None:
    page = _page(env)
    live = _put(env, page.id, sources=["your_files/tea.csv"]).json()["live"]
    assert live["on_change"] is True
    assert _put(env, page.id, on_change=False).json()["live"]["on_change"] is False
    assert _put(env, page.id, on_change=True).json()["live"]["on_change"] is True

    app = FastAPI()
    seen: list[tuple[str, str]] = []

    class _Hosts:
        def resolve_launch_token(self, host_id: str, token: str) -> Any:
            seen.append((host_id, token))
            return SimpleNamespace(user_id="alice") if token == "good" else None

    app.include_router(
        create_live_router(env.service, auth_provider=_Auth(), host_store=lambda: _Hosts()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app, raise_server_exceptions=False)
    good = {"authorization": "Bearer h1:good"}
    assert (
        client.get("/v1/live/watch", headers={"authorization": "Bearer h1:bad"}).status_code != 200
    )
    assert client.get("/v1/live/watch").status_code != 200
    assert client.get("/v1/live/watch", headers=good).json() == {"paths": ["your_files/tea.csv"]}
    # The same call carries the host's load, which sizes its owner's room for runs.
    task = SimpleNamespace(workspace_id=0, user_id="alice")
    assert env.service.room(task) is None
    assert client.get("/v1/live/watch?mem_free=0.5&load=0.1", headers=good).is_success
    assert env.service.room(task) == 3
    armed: list[tuple[str, str]] = []
    env.service.arm = armed.append  # type: ignore[method-assign]
    body = {"paths": ["your_files/tea.csv"]}
    assert client.post("/v1/live/changed", json=body, headers=good).json() == {"pages": 1}
    assert armed == [(_SESSION, "prices.html")]


def test_note_and_start_fresh_routes(env) -> None:
    page = _page(env)
    live = _put(env, page.id, note="prices only").json()["live"]
    assert live["note"] == "prices only"
    assert _put(env, page.id, note="x" * 1001).status_code == 422
    base = f"/v1/artifacts/{page.id}/live"
    ctx = env.client.get(f"{base}/context", headers=ALICE).json()
    assert ctx["note"] == "prices only"
    body = {
        "base_version": 1,
        "changes": [{"id": "btc", "after": "Bitcoin: 9"}],
        "facts": {"s": 1},
    }
    env.client.post(f"{base}/save", json=body, headers=ALICE)
    fresh = env.client.post(f"{base}/reset", headers=ALICE).json()["live"]
    assert fresh["note"] == "prices only" and fresh["last_update"] is None
    assert (
        env.client.post(
            f"/v1/artifacts/{_page(env, 'b.html').id}/live/reset", headers=ALICE
        ).status_code
        == 404
    )


def test_the_local_host_watches_unowned_pages(env) -> None:
    # Single-user mode: rows are unowned while the host's token names the local person.
    from omnigent.server.auth import RESERVED_USER_LOCAL

    local = {"x-user": RESERVED_USER_LOCAL}
    page = _page(env, user=None)  # type: ignore[arg-type]
    body = {"rrule": RRULE, "sources": ["your_files/tea.csv"]}
    assert env.client.put(f"/v1/artifacts/{page.id}/live", json=body, headers=local).is_success

    class _Hosts:
        def resolve_launch_token(self, host_id: str, token: str) -> Any:
            return SimpleNamespace(user_id=RESERVED_USER_LOCAL)

    app = FastAPI()
    app.include_router(
        create_live_router(env.service, auth_provider=_Auth(), host_store=lambda: _Hosts()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)
    watch = client.get("/v1/live/watch", headers={"authorization": "Bearer h1:t"})
    assert watch.json() == {"paths": ["your_files/tea.csv"]}


def test_a_held_update_shows_in_the_settings_and_publishes_from_its_route(env) -> None:
    page = _page(env)
    _put(env, page.id)
    base = f"/v1/artifacts/{page.id}/live"
    assert env.client.post(f"{base}/publish", headers=ALICE).status_code == 409
    env.artifacts.publish(page, audience="link")
    changes = [{"id": "nope", "after": "x"}, {"id": "btc", "after": "Bitcoin: 150"}]
    body = {"base_version": 1, "changes": changes}
    assert env.client.post(f"{base}/save", json=body, headers=ALICE).json()["held"] is True
    live = env.client.get(base, headers=ALICE).json()["live"]
    assert live["held"] == {"version": 2, "reason": "unplaced"} and live["follow_publish"]
    done = env.client.post(f"{base}/publish", headers=ALICE)
    assert done.status_code == 200 and done.json()["live"]["held"] is None
    assert env.artifacts.publication_for(page).published_version == 2
    # Published from the dialog instead: the held update no longer shows either.
    again = [{"id": "nope", "after": "x"}, {"id": "btc", "after": "Bitcoin: 160"}]
    env.client.post(f"{base}/save", json={"base_version": 2, "changes": again}, headers=ALICE)
    assert env.client.get(base, headers=ALICE).json()["live"]["held"]["version"] == 3
    env.artifacts.publish(page, audience="link")
    assert env.client.get(base, headers=ALICE).json()["live"]["held"] is None
