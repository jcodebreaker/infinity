"""A file's studio, slice 2 (ADR 0024): patches, the outline, notices, activities, its runner."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.labels import (
    ARTIFACT_CHAT_LABEL_KEY,
    CONTEXT_MODE_LABEL,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.activity import derive
from omnigent.superchat.activity.derive import (
    SOURCE_STUDIO,
    activity_to_dict,
    get_activity,
    list_activities,
    step_title_for_call,
)
from omnigent.superchat.artifacts import edit_routes, links
from omnigent.superchat.artifacts import routes as artifact_routes
from omnigent.superchat.artifacts import studio as studio_module
from omnigent.superchat.artifacts.edit_routes import create_html_edit_router
from omnigent.superchat.artifacts.feature import _tools
from omnigent.superchat.artifacts.hint import EDITING_MARK
from omnigent.superchat.artifacts.routes import create_artifacts_router
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.artifacts.studio import deliver_studio_notice, stale_line
from omnigent.superchat.artifacts.studio_turn import _NOTED, studio_turn_blocks
from omnigent.superchat.decks.outline import HEADER, MAX_CHARS, deck_outline
from omnigent.superchat.live.service import STUDIO_RECHECK_SECONDS, LiveService
from omnigent.superchat.prompt_prefix import person_words
from omnigent.superchat.subagents import STUDIO_QUEUED_NOTE, StudioRunners

CHAT_LABELS = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
DECK = b"""<!doctype html><html><head><style>.slide{}</style></head><body><div class="deck">
<section class="slide active" data-superchat-id="cover">
  <h1 data-superchat-id="cover-title">Shipping got faster</h1>
  <p data-superchat-id="cover-lead">What moved &amp; what stalled</p>
  <div class="foot" data-superchat-id="cover-foot">
    <span data-superchat-id="cover-page">01</span></div>
</section>
<section class="slide" data-superchat-id="trend">
  <h2 data-superchat-id="trend-title">Releases per month</h2>
  <div data-superchat-id="trend-chart"><canvas data-superchat-id="trend-canvas"></canvas></div>
  <script>var label = "<b>not text</b>";</script>
</section>
</div></body></html>
"""


class _LocalAuth:
    def get_user_id(self, request: Request) -> str | None:
        return RESERVED_USER_LOCAL


@pytest.fixture()
def env(db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _allow(*_args: Any, **_kwargs: Any) -> Any:
        return type("Access", (), {"conversation": None})()

    monkeypatch.setattr(artifact_routes, "require_access", _allow)
    monkeypatch.setattr(edit_routes, "require_access", _allow)
    monkeypatch.setattr(links, "notify_item_updated", lambda c, i: None)
    monkeypatch.setattr(studio_module, "append_notice", _append_without_publish)
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    store = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    conversations = SqlAlchemyConversationStore(db_uri)
    warmed: list[str] = []

    async def _keep_warm(_request: Request, session_id: str) -> bool:
        warmed.append(session_id)
        return True

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _h(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}}, status_code=exc.http_status
        )

    auth = _LocalAuth()
    app.include_router(
        create_artifacts_router(
            store,
            conversation_store=conversations,
            auth_provider=auth,  # type: ignore[arg-type]
            permission_store=object(),  # type: ignore[arg-type]
            keep_warm=_keep_warm,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_html_edit_router(
            store,
            auth_provider=auth,  # type: ignore[arg-type]
            conversation_store=conversations,
        ),
        prefix="/v1",
    )
    chat = conversations.create_conversation(labels=dict(CHAT_LABELS)).id
    return SimpleNamespace(
        client=TestClient(app),
        app=app,
        store=store,
        conversations=conversations,
        chat=chat,
        warmed=warmed,
    )


def _append_without_publish(conversations: Any, session_id: str, text: str) -> None:
    conversations.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"studio_{uuid.uuid4().hex}",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": text}],
                    is_system_notice=True,
                ),
            )
        ],
    )


def _ask(conversations: Any, chat: str, text: str, response_id: str = "resp_1") -> str:
    (item,) = conversations.append(
        chat,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
        ],
    )
    return item.id


def _save(env: Any, name: str, body: bytes, **query: Any) -> dict[str, Any]:
    extra = "".join(f"&{k}={v}" for k, v in query.items())
    resp = env.client.post(
        f"/v1/artifacts?parent_session_id={env.chat}&name={name}{extra}", content=body
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _deck_with_studio(env: Any) -> tuple[dict[str, Any], str]:
    v1 = _save(env, "pitch.deck.html", DECK)
    studio = env.client.post(f"/v1/artifacts/{v1['id']}/chat").json()["session_id"]
    return v1, studio


def _texts(conversations: Any, session_id: str) -> list[str]:
    page = conversations.list_items(session_id, limit=50, order="asc")
    return [
        block["text"]
        for item in page.data
        if isinstance(item.data, MessageData)
        for block in item.data.content
        if isinstance(block.get("text"), str)
    ]


# ---------------------------------------------------------------- the slide outline


def test_the_outline_names_each_slides_elements_and_their_text() -> None:
    outline = deck_outline(DECK)
    assert outline == "\n".join(
        [
            HEADER,
            'Slide 1: cover-title "Shipping got faster" · cover-lead "What moved & what stalled"',
            'Slide 2: trend-title "Releases per month" · trend-canvas',
        ]
    )
    assert deck_outline(b"<p data-superchat-id='x'>no slides</p>") is None


def test_the_outline_is_capped_in_slides_and_characters() -> None:
    long = "word " * 40
    slides = "".join(
        f'<section class="slide"><h2 data-superchat-id="s{n}">{long}</h2></section>'
        for n in range(60)
    )
    outline = deck_outline(slides)
    assert outline is not None and len(outline) <= MAX_CHARS
    first = outline.splitlines()[1]
    assert first.startswith('Slide 1: s0 "word') and first.endswith('…"')
    assert len(first.split('"')[1]) == 60
    assert outline.splitlines()[-1].startswith("…")
    few = "".join(
        f'<section class="slide"><b data-superchat-id="s{n}">x</b></section>' for n in range(45)
    )
    lines = (deck_outline(few) or "").splitlines()
    assert lines[40] == 'Slide 40: s39 "x"' and lines[41].startswith("…")
    assert len(lines) == 42


def test_a_studio_deck_turn_starts_with_its_outline(
    env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    _v1, studio = _deck_with_studio(env)
    _NOTED.pop(studio, None)

    async def run() -> tuple[list[str], list[str]]:
        transport = httpx.ASGITransport(app=env.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            return await studio_turn_blocks(client, studio)

    (line, outline), _notes = asyncio.run(run())
    assert line.startswith("The file of this chat: ")
    assert outline == deck_outline(DECK)
    # A reader of the stored message cuts the outline as it cuts the file line.
    assert person_words(f"{outline}\n\n{line}\n\nmake it blue") == "make it blue"


# ---------------------------------------------------------------- artifact_patch


def test_the_patch_tool_is_offered_in_a_studio_only() -> None:
    def names(labels: dict[str, str]) -> set[str]:
        return {tool.name() for tool in _tools(labels, None)}  # type: ignore[arg-type]

    assert "artifact_patch" not in names(CHAT_LABELS)
    assert "artifact_patch" in names({**CHAT_LABELS, ARTIFACT_CHAT_LABEL_KEY: "g"})


def test_a_studio_patch_saves_the_next_version_from_its_turn(env: Any) -> None:
    v1, studio = _deck_with_studio(env)
    asked = _ask(env.conversations, studio, "shorter title")
    patch = {"kind": "set-text", "id": "cover-title", "text": "Faster"}
    resp = env.client.post(
        f"/v1/artifacts/{v1['group_id']}/patch",
        json={"chat_id": studio, "base_version": 1, "patches": [patch]},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["version"] == 2 and saved["origin"] == "ai" and saved["chat_id"] == studio
    assert "Faster" in saved["summary"]
    version = env.client.get(f"/v1/artifacts/{saved['id']}").json()["all_versions"][0]
    assert version["item_id"] == asked and version["from_studio"] is True
    content = env.client.get(f"/v1/artifacts/{saved['id']}/content").content
    assert b">Faster</h1>" in content and b"What moved &amp; what stalled" in content

    stale = env.client.post(
        f"/v1/artifacts/{v1['id']}/patch",
        json={"chat_id": studio, "base_version": 1, "patches": [patch]},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "artifact_stale"
    assert "v2" in stale.json()["error"]["message"]


def test_only_the_files_studio_may_patch_and_refusals_say_what_to_do(env: Any) -> None:
    v1, studio = _deck_with_studio(env)
    other = _save(env, "memo.md", b"# memo")
    patch = {"kind": "set-text", "id": "cover-title", "text": "x"}

    def post(artifact_id: str, chat_id: str, patches: list[dict[str, Any]]) -> Any:
        return env.client.post(
            f"/v1/artifacts/{artifact_id}/patch",
            json={"chat_id": chat_id, "base_version": 1, "patches": patches},
        )

    assert post(v1["id"], env.chat, [patch]).status_code == 400  # Super Chat is not the studio
    assert post(other["id"], studio, [patch]).status_code == 400  # another file
    missing = post(v1["id"], studio, [{"kind": "set-text", "id": "nope", "text": "x"}])
    assert missing.status_code in (400, 409)
    duplicate = DECK.replace(b'data-superchat-id="cover-lead"', b'data-superchat-id="cover-title"')
    v2 = _save(env, "pitch.deck.html", duplicate, chat_id=studio, base_version=1)
    ambiguous = env.client.post(
        f"/v1/artifacts/{v2['id']}/patch",
        json={"chat_id": studio, "base_version": 2, "patches": [patch]},
    )
    assert ambiguous.status_code == 409
    assert "artifact_save" in ambiguous.json()["error"]["message"]


def test_the_patch_tool_mirrors_the_saved_version_in_the_workspace(
    env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.superchat.artifacts import handlers

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    deck = workspace / "pitch.deck.html"
    deck.write_bytes(DECK)
    v1 = _save(env, "pitch.deck.html", DECK, source_path=str(deck))
    studio = env.client.post(f"/v1/artifacts/{v1['id']}/chat").json()["session_id"]
    _ask(env.conversations, studio, "bigger")
    patches = [{"kind": "set-style", "id": "cover-title", "style": {"font-size": "64px"}}]

    async def run() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=env.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
            args = {"artifact_id": v1["group_id"], "base_version": 1, "patches": patches}
            done = await handlers._patch(client, studio, args)
            stale = await handlers._patch(client, studio, args)
            return done, stale

    done, stale = asyncio.run(run())
    assert json.loads(done)["version"] == 2
    assert b"font-size: 64px" in deck.read_bytes()
    refused = json.loads(stale)
    assert refused["error"] == "artifact_stale" and refused["newest_version"] == 2


def test_the_activity_panel_names_a_patch_in_plain_words() -> None:
    assert step_title_for_call("artifact_patch", json.dumps({"patches": [{}]})) == (
        "Edited the file"
    )
    assert step_title_for_call("artifact_patch", json.dumps({"patches": [{}, {}]})) == (
        "Made 2 edits to the file"
    )


# ---------------------------------------------------------------- what others are told


def test_the_conversations_hint_marks_a_file_being_edited_in_its_studio(env: Any) -> None:
    v1, studio = _deck_with_studio(env)
    _save(env, "memo.md", b"# memo")
    hint = env.client.get(f"/v1/artifacts/hint?session_id={env.chat}").json()["hint"]
    assert EDITING_MARK not in hint
    env.conversations.set_session_live_status(studio, "running")
    hint = env.client.get(f"/v1/artifacts/hint?session_id={env.chat}").json()["hint"]
    (marked,) = [line for line in hint.splitlines() if EDITING_MARK in line]
    assert v1["id"] in marked and "memo" not in marked


def test_a_save_elsewhere_during_a_studio_turn_is_a_stale_notice(env: Any) -> None:
    _v1, studio = _deck_with_studio(env)
    env.conversations.set_session_live_status(studio, "running")
    from_chat = env.store.create(
        user_id=None,
        parent_session_id=env.chat,
        name="pitch.deck.html",
        title=None,
        kind="html",
        mime="text/html",
        data=DECK + b"<!-- v2 -->",
        chat_id=env.chat,
    )
    steered: list[tuple[str, str]] = []

    async def steer(session_id: str, text: str) -> bool:
        steered.append((session_id, text))
        return True

    assert asyncio.run(deliver_studio_notice(env.conversations, from_chat, steer=steer)) == (
        "steered"
    )
    assert steered == [(studio, "Super Chat saved v2 while you were editing; v3 is based on v1")]
    # The person's own versions are never worded as stale.
    hand = SimpleNamespace(chat_id=None, origin="manual", version=3)
    assert stale_line(hand, studio) is None  # type: ignore[arg-type]


def test_a_save_soon_after_a_studio_turn_is_stale_and_later_is_plain(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _v1, studio = _deck_with_studio(env)
    _ask(env.conversations, studio, "bigger title")

    def save(n: int) -> Any:
        return env.store.create(
            user_id=None,
            parent_session_id=env.chat,
            name="pitch.deck.html",
            title=None,
            kind="html",
            mime="text/html",
            data=DECK + f"<!-- {n} -->".encode(),
            chat_id=env.chat,
        )

    assert asyncio.run(deliver_studio_notice(env.conversations, save(2), steer=None)) == "stored"
    assert _texts(env.conversations, studio)[-1].startswith("Super Chat saved v2 while you")
    # Ten minutes on (the runtime line just stored does not count as a turn): a plain line.
    later = time.time() + 600
    original = studio_module.turn_ended_recently
    monkeypatch.setattr(
        studio_module, "turn_ended_recently", lambda c, s: original(c, s, now=later)
    )
    asyncio.run(deliver_studio_notice(env.conversations, save(3), steer=None))
    assert _texts(env.conversations, studio)[-1] == "[Super Chat] saved v3"


def test_a_live_update_waits_while_the_studio_has_a_turn_running(env: Any) -> None:
    v1, studio = _deck_with_studio(env)
    live = SimpleNamespace(user_id=None, parent_session_id=env.chat, name=v1["name"])
    service = LiveService.__new__(LiveService)
    service.live = SimpleNamespace(by_task=lambda _task_id: live)  # type: ignore[assignment]
    service.artifacts = env.store
    service.conversations = env.conversations
    task = SimpleNamespace(id="task")
    assert service.defer_until(task) is None
    env.conversations.set_session_live_status(studio, "running")
    wait = service.defer_until(task)
    assert wait is not None and wait >= time.time() + STUDIO_RECHECK_SECONDS - 5


# ---------------------------------------------------------------- studio turns as activities


def test_each_studio_turn_is_an_activity_grouped_under_its_file(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, studio = _deck_with_studio(env)
    monkeypatch.setitem(derive.FILE_NAMES, "test", env.store.group_name)
    _append_without_publish(env.conversations, studio, "[you] made the title larger · v2")
    _ask(env.conversations, studio, "Make the title shorter", response_id="resp_a")
    env.conversations.append(
        studio,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_b",
                data=FunctionCallData(
                    agent="editor",
                    name="artifact_patch",
                    arguments=json.dumps({"artifact_id": v1["id"], "patches": [{}]}),
                    call_id="call_1",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_b",
                data=MessageData(
                    role="assistant",
                    agent="editor",
                    content=[{"type": "output_text", "text": "Shortened the title."}],
                ),
            ),
        ],
    )
    env.conversations.set_session_live_status(studio, "idle")
    rows = [a for a in list_activities(env.conversations, env.chat) if a.chat_id == studio]
    (row,) = rows
    assert row.kind == "turn" and row.source == SOURCE_STUDIO
    assert row.title == "Make the title shorter"
    assert row.parent_chat_id is None
    assert row.artifact == {"group_id": v1["group_id"], "name": "pitch.deck.html"}
    wire = activity_to_dict(row)
    assert wire["source"] == "studio" and wire["artifact"] == row.artifact
    assert [s.title for s in row.steps] == ["Edited the file"]
    full = get_activity(env.conversations, env.chat, row.id)
    assert full is not None and full.artifact == row.artifact
    # The studio itself is never a row.
    assert get_activity(env.conversations, env.chat, f"sub_agent:{studio}") is None
    assert "artifact" not in activity_to_dict(
        derive.Activity(
            id="x",
            kind="turn",
            chat_id="c",
            title="t",
            outcome=None,
            status="done",
            started_at=0,
            finished_at=0,
        )
    )


# ---------------------------------------------------------------- warm runner and cap


def test_the_studio_open_signal_keeps_its_runner_warm_and_never_makes_a_studio(env: Any) -> None:
    v1 = _save(env, "pitch.deck.html", DECK)
    assert env.client.post(f"/v1/artifacts/{v1['id']}/chat/open").status_code == 404
    studio = env.client.post(f"/v1/artifacts/{v1['id']}/chat").json()["session_id"]
    opened = env.client.post(f"/v1/artifacts/{v1['id']}/chat/open")
    assert opened.json() == {"session_id": studio, "warm": True}
    assert env.warmed == [studio]


def test_at_most_three_studio_turns_run_at_once_and_the_fourth_waits() -> None:
    async def run() -> tuple[list[str], list[str]]:
        runners = StudioRunners()
        release = asyncio.Event()
        order: list[str] = []
        queued: list[str] = []

        async def turn(name: str) -> None:
            async with runners.turn(name, lambda: queued.append(name)):
                order.append(name)
                await release.wait()

        tasks = [asyncio.create_task(turn(f"s{n}")) for n in range(4)]
        await asyncio.sleep(0.01)
        started = list(order)
        release.set()
        await asyncio.gather(*tasks)
        return started, queued

    started, queued = asyncio.run(run())
    assert started == ["s0", "s1", "s2"]
    assert queued == ["s3"]
    assert STUDIO_QUEUED_NOTE == "Finishing another file first"


def test_a_studio_goes_cold_ten_minutes_after_its_last_touch_unless_running() -> None:
    now = [1000.0]
    runners = StudioRunners(clock=lambda: now[0])
    runners.touch("open")
    now[0] += 599
    assert runners.cold() == []
    runners.touch("open")  # the studio is still open: another ten minutes

    async def long_turn() -> list[str]:
        async with runners.turn("busy", lambda: None):
            now[0] += 700
            return runners.cold()  # never the studio whose turn runs

    assert asyncio.run(long_turn()) == ["open"]
    assert runners.cold() == []  # the turn's end touched it
    now[0] += 600
    assert runners.cold() == ["busy"]
    assert runners.cold() == []  # forgotten until touched again


def test_a_studios_runtime_lines_show_in_its_thread_and_never_in_super_chat(env: Any) -> None:
    from omnigent.superchat.transcript.routes import read_transcript

    _v1, studio = _deck_with_studio(env)
    line = "Super Chat saved v2 while you were editing; v3 is based on v1"
    _append_without_publish(env.conversations, studio, line)
    _append_without_publish(env.conversations, env.chat, line)

    def thread(session_id: str) -> list[dict[str, Any]]:
        conversation = env.conversations.get_conversation(session_id)
        return read_transcript(env.conversations, conversation, before=None, limit=50)["data"]

    (notice,) = thread(studio)
    assert notice["role"] == "system"
    assert notice["blocks"] == [
        {"type": "notice", "kind": "stale", "version": 2, "origin": "superchat"}
    ]
    assert thread(env.chat) == []
