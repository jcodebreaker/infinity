"""Decks: the deck marker, the ``deck_export`` tool/handler and the export route."""

from __future__ import annotations

import json
import re
import stat
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.decks import DECK_SUFFIX, deck_stem, is_deck_name
from omnigent.superchat.decks.feature import DECKS_FEATURE
from omnigent.superchat.decks.handlers import HELPER_ENV, handle_deck_tool
from omnigent.superchat.decks.routes import create_decks_router
from omnigent.superchat.decks.tools import DeckExportTool
from omnigent.superchat.feature import HandlerCtx

_CHAT = uuid.uuid4().hex
_OTHER_CHAT = uuid.uuid4().hex
_HTML = b"<!doctype html><html data-superchat-deck><body><section class='slide'>x</section></body>"


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


H = {"x-user": "alice"}


def test_deck_names() -> None:
    assert DECK_SUFFIX == ".deck.html"
    assert is_deck_name("Q3.deck.html") and is_deck_name("q3.DECK.HTML")
    assert not is_deck_name("q3.html") and not is_deck_name(".deck.html")
    assert deck_stem("q3.deck.html") == "q3" and deck_stem("q3.html") == "q3.html"


def test_tool_schema_and_gating() -> None:
    schema = DeckExportTool().get_schema()["function"]
    assert schema["name"] == "deck_export"
    assert schema["parameters"]["required"] == ["artifact_id", "format"]
    assert schema["parameters"]["properties"]["format"]["enum"] == ["pptx", "pdf"]
    assert DECKS_FEATURE.tools({}, None) == []  # type: ignore[arg-type]
    assert "deck_export" in DECKS_FEATURE.handlers


@pytest.fixture()
def store(db_uri: str, tmp_path: Path) -> SqlAlchemyArtifactStore:
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    return SqlAlchemyArtifactStore(db_uri, lambda: blobs)


def _save(
    store: SqlAlchemyArtifactStore,
    name: str,
    *,
    chat: str = _CHAT,
    user: str = "alice",
    data: bytes = _HTML,
):
    kind = name.rsplit(".", 1)[1]
    return store.create(
        user_id=user, parent_session_id=chat, name=name, title="Q3 review", kind=kind,
        mime=KIND_MIME[kind], data=data, source_path=None,
    )  # fmt: skip


def _helper(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "fake-helper"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


_OK_HELPER = r"""
while [ $# -gt 0 ]; do case "$1" in --format) F=$2;; --out) O=$2;; --html) H=$2;; esac; shift; done
grep -q "data-superchat-deck" "$H" || { echo '{"ok":false,"error":"not a deck"}'; exit 0; }
printf "FAKE-%s" "$F" > "$O"
echo "noise on stdout"
echo "{\"ok\":true,\"path\":\"$O\",\"slides\":1,\"width\":1920,\"height\":1080}"
"""


@pytest.fixture()
def server(store: SqlAlchemyArtifactStore, tmp_path: Path):
    """A fake server: the real artifacts router behind an httpx ASGI transport."""
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

    @app.get("/v1/sessions/{sid}")
    async def session(sid: str) -> dict[str, Any]:
        return {"kind": "chat", "parent_session_id": None}

    return app


def _ctx(server: FastAPI, chat: str = _CHAT) -> HandlerCtx:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server), base_url="http://t", headers=H
    )
    return HandlerCtx("deck_export", client, chat, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["pptx", "pdf"])
async def test_handler_exports_and_saves_new_artifact(
    store, server, tmp_path, monkeypatch, fmt: str
) -> None:
    monkeypatch.setenv(HELPER_ENV, str(_helper(tmp_path, _OK_HELPER)))
    deck = _save(store, "q3.deck.html")
    out = json.loads(await handle_deck_tool(_ctx(server), {"artifact_id": deck.id, "format": fmt}))
    assert out["type"] == "artifact" and out["name"] == f"q3.{fmt}" and out["kind"] == fmt
    assert out["title"] == f"Q3 review ({'PowerPoint' if fmt == 'pptx' else 'PDF'})"
    saved = store.get(out["id"], user_id="alice")
    assert store.read(saved) == f"FAKE-{fmt}".encode()
    assert saved.parent_session_id == _CHAT


@pytest.mark.asyncio
async def test_handler_second_export_is_a_new_version(
    store, server, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(HELPER_ENV, str(_helper(tmp_path, _OK_HELPER)))
    deck = _save(store, "q3.deck.html")
    args = {"artifact_id": deck.id, "format": "pptx"}
    json.loads(await handle_deck_tool(_ctx(server), args))
    second = json.loads(await handle_deck_tool(_ctx(server), args))
    assert second["version"] == 2


@pytest.mark.asyncio
async def test_handler_helper_failure_is_a_calm_error(
    store, server, tmp_path, monkeypatch
) -> None:
    helper = _helper(tmp_path, 'echo \'{"ok":false,"error":"Chromium crashed"}\'\n')
    monkeypatch.setenv(HELPER_ENV, str(helper))
    deck = _save(store, "q3.deck.html")
    out = json.loads(
        await handle_deck_tool(_ctx(server), {"artifact_id": deck.id, "format": "pdf"})
    )
    assert out == {"error": "Chromium crashed"}


@pytest.mark.asyncio
async def test_handler_missing_helper(store, server, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(HELPER_ENV, str(tmp_path / "nope"))
    deck = _save(store, "q3.deck.html")
    out = json.loads(
        await handle_deck_tool(_ctx(server), {"artifact_id": deck.id, "format": "pdf"})
    )
    assert "isn't available on this host" in out["error"]


@pytest.mark.asyncio
async def test_handler_refuses_non_deck_other_chat_and_bad_args(
    store, server, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(HELPER_ENV, str(_helper(tmp_path, _OK_HELPER)))
    page = _save(store, "page.html")
    other = _save(store, "z.deck.html", chat=_OTHER_CHAT)
    deck = _save(store, "q3.deck.html")
    ctx = _ctx(server)
    assert (
        "isn't a deck"
        in json.loads(await handle_deck_tool(ctx, {"artifact_id": page.id, "format": "pdf"}))[
            "error"
        ]
    )
    assert (
        "different chat"
        in json.loads(await handle_deck_tool(ctx, {"artifact_id": other.id, "format": "pdf"}))[
            "error"
        ]
    )
    assert (
        "valid artifact_id"
        in json.loads(await handle_deck_tool(ctx, {"artifact_id": "x", "format": "pdf"}))["error"]
    )
    assert (
        "pptx or pdf"
        in json.loads(await handle_deck_tool(ctx, {"artifact_id": deck.id, "format": "png"}))[
            "error"
        ]
    )


# ----------------------------------------------------------------------------- route


class _Runner:
    def __init__(self, payload: Any = None, raises: Exception | None = None) -> None:
        self.payload, self.raises, self.calls = payload, raises, []

    async def post(self, url: str, **kw: Any) -> Any:
        self.calls.append((url, kw))
        if self.raises:
            raise self.raises
        runner = self

        class _R:
            def json(self) -> Any:
                return runner.payload

        return _R()


class _Convs:
    def __init__(self, found: bool = True) -> None:
        self.found = found
        self.appended: list[tuple[str, list[Any]]] = []
        self.fail = False

    def get_conversation(self, sid: str) -> Any:
        return object() if self.found else None

    def append(self, sid: str, items: list[Any]) -> list[Any]:
        if self.fail:
            raise RuntimeError("db down")
        self.appended.append((sid, items))
        return [SimpleNamespace(id=f"item_{n}") for n, _ in enumerate(items)]


@pytest.fixture()
def route_client(store, monkeypatch):
    holder: dict[str, Any] = {"runner": _Runner(), "convs": _Convs()}

    async def fake_ensure(**kw: Any) -> Any:
        return holder["wake"] if "wake" in holder else (holder["runner"], kw["conv"])

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.ensure_runner_connected", fake_ensure
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.http_status)

    class _Proxy:
        def get_conversation(self, sid: str) -> Any:
            return holder["convs"].get_conversation(sid)

        def append(self, sid: str, items: list[Any]) -> list[Any]:
            return holder["convs"].append(sid, items)

    app.include_router(
        create_decks_router(
            store,
            conversation_store=_Proxy(),
            runner_router=object(),
            auth_provider=_Auth(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )
    return TestClient(app), holder


def _out(**kw: Any) -> dict[str, Any]:
    return {"result": {"output": json.dumps(kw)}}


def test_route_relays_runner_result(store, route_client) -> None:
    client, holder = route_client
    deck = _save(store, "q3.deck.html")
    holder["runner"] = _Runner(_out(type="artifact", id="abc", name="q3.pptx"))
    resp = client.post(f"/v1/decks/{deck.id}/export", json={"format": "pptx"}, headers=H)
    assert resp.status_code == 200 and resp.json() == {"id": "abc", "name": "q3.pptx"}
    url, kw = holder["runner"].calls[0]
    assert url == f"/v1/sessions/{_CHAT}/mcp/execute"
    assert kw["json"]["params"] == {
        "name": "deck_export",
        "arguments": {"artifact_id": deck.id, "format": "pptx"},
    }


def test_route_shows_the_export_as_a_plain_file_card_the_muse_never_called(
    store, route_client
) -> None:
    from omnigent.entities.conversation import NON_CONTENT_ITEM_TYPES
    from omnigent.superchat.transcript.blocks import project_items

    client, holder = route_client
    deck = _save(store, "q3.deck.html")
    saved = {"type": "artifact", "id": "abc", "name": "q3.pptx", "kind": "pptx", "title": "Q3"}
    holder["runner"] = _Runner(_out(**saved))
    assert client.post(
        f"/v1/decks/{deck.id}/export", json={"format": "pptx"}, headers=H
    ).is_success
    [(sid, items)] = holder["convs"].appended
    # One plain item: no deck_export call in the Muse's history, and replay skips the type.
    assert sid == _CHAT and [i.type for i in items] == ["resource_event"]
    assert "resource_event" in NON_CONTENT_ITEM_TYPES
    flat = [{"id": f"i{n}", "type": i.type, **i.data.model_dump()} for n, i in enumerate(items)]
    assert not any(i["type"] in ("function_call", "function_call_output") for i in flat)
    [message] = project_items(flat)
    [block] = message["blocks"]
    assert block["type"] == "file" and block["artifact_id"] == "abc" and block["name"] == "q3.pptx"
    assert block["by"] == "user" and block["title"] == "Q3"


def test_route_export_still_succeeds_when_the_chat_cannot_be_written(store, route_client) -> None:
    client, holder = route_client
    deck = _save(store, "q3.deck.html")
    holder["convs"].fail = True
    holder["runner"] = _Runner(_out(type="artifact", id="abc", name="q3.pdf"))
    resp = client.post(f"/v1/decks/{deck.id}/export", json={"format": "pdf"}, headers=H)
    assert resp.status_code == 200 and resp.json()["id"] == "abc"


def test_route_errors(store, route_client) -> None:
    client, holder = route_client
    deck = _save(store, "q3.deck.html")
    page = _save(store, "page.html")
    url = f"/v1/decks/{deck.id}/export"
    assert client.post(url, json={"format": "pptx"}).status_code in (401, 403, 404)
    assert client.post(url, json={"format": "pptx"}, headers={"x-user": "bob"}).status_code == 404
    assert (
        client.post(
            f"/v1/decks/{uuid.uuid4().hex}/export", json={"format": "pdf"}, headers=H
        ).status_code
        == 404
    )
    assert (
        client.post(f"/v1/decks/{page.id}/export", json={"format": "pdf"}, headers=H).status_code
        == 400
    )
    assert client.post(url, json={"format": "png"}, headers=H).status_code == 422
    holder["wake"] = (None, object())
    assert client.post(url, json={"format": "pdf"}, headers=H).status_code == 503
    del holder["wake"]
    holder["convs"] = _Convs(found=False)
    assert client.post(url, json={"format": "pdf"}, headers=H).status_code == 404
    holder["convs"] = _Convs()
    holder["runner"] = _Runner(raises=RuntimeError("tunnel closed"))
    assert client.post(url, json={"format": "pdf"}, headers=H).status_code == 503
    holder["runner"] = _Runner(_out(error="Chromium crashed"))
    bad = client.post(url, json={"format": "pdf"}, headers=H)
    assert bad.status_code == 400 and bad.json()["error"] == "Chromium crashed"
    holder["runner"] = _Runner({"error": {"code": -32000, "message": "No runner"}})
    assert client.post(url, json={"format": "pdf"}, headers=H).json()["error"] == "No runner"


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["pptx", "pdf"])
async def test_handler_exports_a_kit_deck_with_the_real_helper(
    store, server, monkeypatch, fmt: str
) -> None:
    """The whole path with nothing faked but the server: kit deck -> Chromium -> new artifact."""
    from tests.superchat.decks.test_export_fidelity import HELPER, _chromium_available

    if not HELPER.is_file() or not _chromium_available():
        pytest.skip("Chromium or the helper is missing")
    from omnigent.superchat.decks import kit

    monkeypatch.setenv(HELPER_ENV, str(HELPER))
    html = kit.build_deck(
        "blue-professional", "Q3 review", (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")
    )
    deck = store.create(
        user_id="alice", parent_session_id=_CHAT, name="q3.deck.html", title="Q3 review",
        kind="html", mime=KIND_MIME["html"], data=html.encode(), source_path=None,
    )  # fmt: skip
    out = json.loads(await handle_deck_tool(_ctx(server), {"artifact_id": deck.id, "format": fmt}))
    assert out["type"] == "artifact" and out["name"] == f"q3.{fmt}" and out["size"] > 10_000
    data = store.read(store.get(out["id"], user_id="alice"))
    assert data.startswith(b"%PDF" if fmt == "pdf" else b"PK")


def test_theme_route_lists_the_gallery(route_client) -> None:
    client, _ = route_client
    resp = client.get("/v1/decks/themes", headers=H)
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == "corporate-clean" and len(body["themes"]) == 18
    first = body["themes"][0]
    assert {"id", "name", "tagline", "mood", "category", "mode", "preview"} <= set(first)
    assert all(len(theme["tagline"].split()) <= 4 for theme in body["themes"])
    assert first["preview"].startswith("data:image/webp;base64,")


def test_sample_route_builds_the_preview_deck_in_a_theme(route_client) -> None:
    from omnigent.superchat.decks import kit

    client, _ = route_client
    resp = client.get("/v1/decks/themes/tokyo-night/sample", headers=H)
    assert resp.status_code == 200
    deck = resp.json()["html"]
    assert kit.deck_theme_id(deck) == "tokyo-night"
    labels = re.findall(r'data-screen-label="([^"]+)"', deck)
    assert labels == [
        "01 Cover", "02 Agenda", "03 Numbers", "04 Trend", "05 By team", "06 Voice", "07 Decision"
    ]  # fmt: skip
    assert "new Chart(" in deck and "<table" in deck
    assert client.get("/v1/decks/themes/nope/sample", headers=H).status_code == 404


def test_theme_route_names_the_decks_current_theme(store, route_client) -> None:
    from omnigent.superchat.decks import kit

    client, _ = route_client
    slides = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")
    themed = _save(store, "a.deck.html", data=kit.build_deck("nord", "T", slides).encode())
    by_hand = _save(store, "b.deck.html")
    assert client.get(f"/v1/decks/{themed.id}/theme", headers=H).json() == {"theme": "nord"}
    assert client.get(f"/v1/decks/{by_hand.id}/theme", headers=H).json() == {"theme": None}
    page = _save(store, "p.html")
    assert client.get(f"/v1/decks/{page.id}/theme", headers=H).status_code == 400
