"""``PATCH /v1/artifacts/{id}/edit``, the write-back note for a hand-edited deck, stale saves."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import omnigent.superchat.decks  # noqa: F401  (installs the set-theme restyler)
from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.superchat import prompt_prefix
from omnigent.superchat.artifact_kinds import KIND_MIME
from omnigent.superchat.artifacts.edit_routes import create_html_edit_router
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.artifacts.writeback import deliver_manual_edits

_SESSION = uuid.uuid4().hex
H = {"x-user": "alice"}
DECK = (
    '<html><body><section class="slide active" data-screen-label="01 A" data-superchat-id="s1">\n'
    '<h1 data-superchat-id="title" style="font-size: 64px">A</h1>\n</section></body></html>'
)


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
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.http_status)

    class _Convs:
        pass

    app.include_router(
        create_artifacts_router(store, conversation_store=_Convs(), auth_provider=_Auth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    app.include_router(create_html_edit_router(store, auth_provider=_Auth()), prefix="/v1")
    return store, TestClient(app)


def _save(store, name="q3.deck.html", data: bytes = DECK.encode(), **kw):
    kind = name.rsplit(".", 1)[1]
    return store.create(
        user_id="alice", parent_session_id=_SESSION, name=name, title=None, kind=kind,
        mime=KIND_MIME[kind], data=data, **kw,
    )  # fmt: skip


def _edit(client, item_id, base=1, patches=None, user="alice"):
    patches = patches or [{"kind": "set-text", "id": "title", "text": "B"}]
    return client.patch(
        f"/v1/artifacts/{item_id}/edit",
        json={"base_version": base, "patches": patches},
        headers={"x-user": user},
    )


def test_edit_saves_a_manual_version(env) -> None:
    store, client = env
    v1 = _save(store, source_path="/ws/q3.deck.html")
    resp = _edit(client, v1.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2 and body["origin"] == "manual"
    assert body["parent_version_id"] == v1.id
    assert body["edit_summary"] == 'slide 1 title "A" → "B"'
    # The studio's words for it, built from the edit: the element and a plain sentence.
    assert body["element_id"] == "title"
    assert body["what"] == "You changed the text of the title"
    new = store.get(body["id"], user_id="alice")
    assert new.source_path == "/ws/q3.deck.html"
    assert store.read(new) == DECK.replace(">A<", ">B<").encode()
    assert store.read(v1) == DECK.encode()  # the old version is untouched


def test_set_theme_is_a_manual_version_the_muse_is_told_about(env) -> None:
    from omnigent.superchat.decks import kit

    store, client = env
    slides = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")
    v1 = _save(store, data=kit.build_deck("corporate-clean", "Q3", slides).encode())
    resp = _edit(client, v1.id, patches=[{"kind": "set-theme", "theme": "tokyo-night"}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["origin"] == "manual" and body["version"] == 2
    assert body["edit_summary"] == "switched the theme to Tokyo Night (was Corporate Clean)"
    new = store.read(store.get(body["id"], user_id="alice")).decode()
    assert new == kit.build_deck("tokyo-night", "Q3", slides)
    unknown = _edit(client, body["id"], 2, [{"kind": "set-theme", "theme": "nope"}])
    assert unknown.status_code == 400


def test_an_edit_that_changes_nothing_saves_no_version(env) -> None:
    from omnigent.superchat.decks import kit

    store, client = env
    slides = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")
    v1 = _save(store, data=kit.build_deck("nord", "Q3", slides).encode())
    resp = _edit(client, v1.id, patches=[{"kind": "set-theme", "theme": "nord"}])
    assert resp.status_code == 200 and resp.json()["id"] == v1.id
    assert len(store.versions(v1)) == 1


def test_stale_base_is_a_409(env) -> None:
    store, client = env
    v1 = _save(store)
    assert _edit(client, v1.id).status_code == 200
    stale = _edit(client, v1.id, base=1)
    assert stale.status_code == 409 and stale.json()["code"] == "artifact_stale"


def test_ambiguous_patch_is_a_409_ask_superchat(env) -> None:
    store, client = env
    v1 = _save(store)
    resp = _edit(client, v1.id, patches=[{"kind": "remove-element", "id": "missing"}])
    assert resp.status_code == 409 and resp.json()["code"] == "conflict"
    assert resp.json()["error"].endswith("ask Superchat instead.")
    assert len(store.versions(v1)) == 1


def test_bad_patch_is_a_400_and_non_deck_is_refused(env) -> None:
    store, client = env
    v1 = _save(store)
    bad = _edit(
        client, v1.id, patches=[{"kind": "set-style", "id": "title", "style": {"color": "url(x)"}}]
    )
    assert bad.status_code == 400
    notes = _save(store, name="notes.md", data=b"# A")
    assert _edit(client, notes.id).status_code == 400
    assert _edit(client, v1.id, patches=[{"kind": "nope"}]).status_code == 422


PAGE = (
    '<!doctype html><html><body><main data-superchat-id="main">\n'
    '<button data-superchat-id="add-button">Add</button>\n</main>'
    "<script>document.title = 'x'</script></body></html>"
)


def test_a_page_is_edited_with_the_same_patches(env) -> None:
    store, client = env
    v1 = _save(store, name="tracker.html", data=PAGE.encode())
    resp = _edit(client, v1.id, patches=[{"kind": "set-text", "id": "add-button", "text": "Save"}])
    assert resp.status_code == 200 and resp.json()["version"] == 2
    saved = store.get(resp.json()["id"], user_id="alice")
    assert saved.origin == "manual"
    assert store.read(saved) == PAGE.replace(">Add<", ">Save<").encode()
    # Undo restores the source with its script (no script is added, so it is allowed).
    undo = _edit(client, saved.id, base=2, patches=[{"kind": "set-full-source", "source": PAGE}])
    assert (
        undo.status_code == 200
        and store.read(store.get(undo.json()["id"], user_id="alice")) == PAGE.encode()
    )


def test_a_page_has_no_theme_and_goes_stale_like_a_deck(env) -> None:
    store, client = env
    v1 = _save(store, name="tracker.html", data=PAGE.encode())
    theme = _edit(client, v1.id, patches=[{"kind": "set-theme", "theme": "x"}])
    assert theme.status_code == 400
    assert (
        _edit(
            client, v1.id, patches=[{"kind": "set-text", "id": "add-button", "text": "B"}]
        ).status_code
        == 200
    )
    stale = _edit(client, v1.id, base=1, patches=[{"kind": "set-text", "id": "main", "text": "C"}])
    assert stale.status_code == 409 and stale.json()["code"] == "artifact_stale"


def test_an_id_the_bridge_made_up_is_refused(env) -> None:
    """Elements without a data-superchat-id get a ``~`` path id in the editor; never applied."""
    store, client = env
    v1 = _save(store, name="tracker.html", data=PAGE.encode())
    resp = _edit(client, v1.id, patches=[{"kind": "set-text", "id": "~0.1", "text": "B"}])
    assert resp.status_code == 400
    assert len(store.versions(v1)) == 1


def test_restore_makes_an_earlier_version_newest_and_is_written_back(env) -> None:
    store, client = env
    v1 = _save(store)
    v2 = _edit(client, v1.id).json()
    restored = client.post(f"/v1/artifacts/{v1.id}/restore", headers=H)
    assert restored.status_code == 200
    body = restored.json()
    assert body["version"] == 3 and body["origin"] == "restore"
    assert body["parent_version_id"] == v2["id"] and body["edit_summary"] == "restored version 1"
    assert store.read(store.get(body["id"], user_id="alice")) == DECK.encode()
    pending = [
        row.version for row in store.pending_manual(user_id="alice", parent_session_id=_SESSION)
    ]
    assert 3 in pending
    # Restoring the newest version is a no-op.
    again = client.post(f"/v1/artifacts/{body['id']}/restore", headers=H)
    assert again.status_code == 200 and again.json()["version"] == 3
    assert (
        client.post(f"/v1/artifacts/{v1.id}/restore", headers={"x-user": "mallory"}).status_code
        == 404
    )


def test_other_owners_edit_is_not_found(env) -> None:
    store, client = env
    v1 = _save(store)
    assert _edit(client, v1.id, user="mallory").status_code == 404


def test_undo_as_full_source_is_a_new_version(env) -> None:
    store, client = env
    v1 = _save(store)
    v2 = _edit(client, v1.id).json()
    undo = _edit(client, v2["id"], base=2, patches=[{"kind": "set-full-source", "source": DECK}])
    assert undo.status_code == 200 and undo.json()["version"] == 3
    assert store.read(store.get(undo.json()["id"], user_id="alice")) == DECK.encode()


# ------------------------------------------------------------ write-back + note + stale save


class _FakeServer:
    def __init__(self, client: TestClient) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            r = client.request(
                request.method, request.url.raw_path.decode(),
                headers=H, content=request.content,
            )  # fmt: skip
            return httpx.Response(r.status_code, content=r.content, headers=r.headers)

        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://server"
        )


def test_deck_edit_is_written_back_and_noted_once(env, tmp_path: Path) -> None:
    store, client = env
    target = tmp_path / "q3.deck.html"
    target.write_bytes(DECK.encode())
    v1 = _save(store, source_path=str(target))
    patches = [
        {"kind": "set-text", "id": "title", "text": "B"},
        {"kind": "set-style", "id": "title", "style": {"font-size": "72px"}},
    ]
    assert _edit(client, v1.id, patches=patches).status_code == 200
    server = _FakeServer(client).client
    notes = asyncio.run(deliver_manual_edits(server, _SESSION, roots=[tmp_path.resolve()]))
    expected = DECK.replace(">A<", ">B<").replace("font-size: 64px", "font-size: 72px;")
    assert target.read_bytes() == expected.encode()
    assert notes == [
        "The person edited q3.deck.html by hand (v1 → v2): "
        'slide 1 title "A" → "B"; slide 1 title font-size 64px→72px. Treat v2 as current.'
    ]
    assert asyncio.run(deliver_manual_edits(server, _SESSION, roots=[tmp_path.resolve()])) == []


def test_turn_prefix_carries_deck_note(env, tmp_path: Path, monkeypatch) -> None:
    store, client = env
    target = tmp_path / "q3.deck.html"
    target.write_bytes(DECK.encode())
    _edit(client, _save(store, source_path=str(target)).id)
    import omnigent.superchat.artifacts.writeback as wb

    monkeypatch.setattr(wb, "workspace_roots", lambda: [tmp_path.resolve()])
    blocks = asyncio.run(
        prompt_prefix.turn_prefix_blocks(_FakeServer(client).client, _SESSION, None)
    )
    assert any("edited q3.deck.html by hand (v1 → v2)" in b for b in blocks)


def test_muse_save_over_an_unseen_hand_edit_is_refused(env, tmp_path: Path) -> None:
    store, client = env
    v1 = _save(store)
    _edit(client, v1.id)
    url = f"/v1/artifacts?parent_session_id={_SESSION}&name=q3.deck.html"
    resp = client.post(url, content=DECK.encode(), headers=H)
    assert resp.status_code == 409
    assert "edited q3.deck.html by hand (v2)" in resp.json()["error"]
    assert len(store.versions(v1)) == 2
    # Once the Muse was told (delivered), a save on top is fine.
    (pending,) = store.pending_manual(user_id="alice", parent_session_id=_SESSION)
    store.mark_delivered(pending.id, user_id="alice")
    assert client.post(url, content=DECK.encode(), headers=H).status_code == 201


BRIEF = "# Q3\n\n## Goals\n\nGrow revenue.\n\nHire later.\n"


def _full(source: str, summary: str | None = None) -> list[dict]:
    patch = {"kind": "set-full-source", "source": source}
    return [patch | {"summary": summary}] if summary else [patch]


def test_a_markdown_document_is_saved_whole_with_its_summary(env) -> None:
    store, client = env
    v1 = _save(store, name="brief.md", data=BRIEF.encode())
    edited = BRIEF.replace("Hire later.", "Hire in October.")
    resp = _edit(client, v1.id, patches=_full(edited, "edited paragraph 2"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 2 and body["origin"] == "manual"
    assert body["edit_summary"] == "edited paragraph 2"
    assert store.read(store.get(body["id"], user_id="alice")) == edited.encode()
    # Undo is the earlier text saved whole, without a summary.
    undo = _edit(client, body["id"], base=2, patches=_full(BRIEF))
    assert undo.status_code == 200 and undo.json()["version"] == 3
    assert undo.json()["edit_summary"] == "restored an earlier state of the file"
    # Markdown has no HTML rules: a script tag or a web address is just text here.
    links = BRIEF + "\n<script>x</script> https://example.com\n"
    assert _edit(client, undo.json()["id"], base=3, patches=_full(links)).status_code == 200


def test_a_text_document_goes_stale_and_takes_only_one_full_source(env) -> None:
    store, client = env
    v1 = _save(store, name="notes.txt", data=b"one")
    assert _edit(client, v1.id, patches=_full("two")).status_code == 200
    stale = _edit(client, v1.id, base=1, patches=_full("three"))
    assert stale.status_code == 409 and stale.json()["code"] == "artifact_stale"
    v2 = store.newest(v1)
    assert _edit(client, v2.id, base=2, patches=_full("a") + _full("b")).status_code == 400
    assert _edit(client, v2.id, base=2, patches=_full("  \n")).status_code == 400
    assert len(store.versions(v1)) == 2


def test_a_markdown_edit_is_written_back_and_noted(env, tmp_path: Path) -> None:
    store, client = env
    target = tmp_path / "brief.md"
    target.write_bytes(BRIEF.encode())
    v1 = _save(store, name="brief.md", data=BRIEF.encode(), source_path=str(target))
    edited = BRIEF.replace("Grow revenue.", "Grow revenue in two regions.")
    assert _edit(client, v1.id, patches=_full(edited, "edited paragraph 1")).status_code == 200
    server = _FakeServer(client).client
    notes = asyncio.run(deliver_manual_edits(server, _SESSION, roots=[tmp_path.resolve()]))
    assert target.read_bytes() == edited.encode()
    assert notes == [
        "The person edited brief.md by hand (v1 → v2): edited paragraph 1. Treat v2 as current."
    ]
