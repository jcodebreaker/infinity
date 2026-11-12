"""The transcript projection (omnigent.superchat.transcript): rules on item fixtures, then the
route's paging, reset cut and access over a real store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.entities import (
    ErrorData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.errors import OmnigentError
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.artifact_kinds import DECK_SUFFIX, KIND_MIME
from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore
from omnigent.superchat.rounds.fold import fold
from omnigent.superchat.rounds.store import RoundEvent, SqlAlchemyRoundStore
from omnigent.superchat.transcript.blocks import project_items
from omnigent.superchat.transcript.routes import read_transcript, register_transcript_routes

_MODE = {"omnigent.context.mode": "superside-chat"}


def _msg(item_id: str, role: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "created_at": 100,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
        **extra,
    }


def _call(item_id: str, name: str, call_id: str, args: dict | None = None) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "function_call",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(args or {}),
        "created_at": 100,
    }


def _out(item_id: str, call_id: str, output: Any) -> dict[str, Any]:
    text = output if isinstance(output, str) else json.dumps(output)
    return {"id": item_id, "type": "function_call_output", "call_id": call_id, "output": text}


_CARD = {
    "type": "card",
    "card": "progress",
    "id": "progress-1",
    "data": {"steps": ["a"]},
    "fallback": " the progress ",
    "note": "internal",
}


def test_text_messages_keep_role_and_order() -> None:
    out = project_items([_msg("m1", "user", "hi"), _msg("m2", "assistant", "hello")])
    assert [(m["id"], m["role"], m["blocks"]) for m in out] == [
        ("m1", "user", [{"type": "text", "text": "hi"}]),
        ("m2", "assistant", [{"type": "text", "text": "hello"}]),
    ]
    assert out[0]["created_at"] == 100


def test_a_turn_shows_only_its_last_text_and_keeps_cards_and_chips() -> None:
    found = {
        "type": "file_search",
        "results": [{"file_id": "f" * 32, "file_name": "a.pdf", "page": 3}],
    }
    card = {"type": "card", "card": "note", "fallback": "Note", "data": {}}
    items = [
        _msg("u1", "user", "find the budget"),
        _msg("a1", "assistant", "Search found nothing, so I'll read the file."),
        _call("c1", "files_search", "k1"),
        _out("o1", "k1", found),
        _msg("a2", "assistant", "Four pages. Extract text with Python."),
        _call("c2", "render_card", "k2", card),
        _out("o2", "k2", card),
        _msg("a3", "assistant", "The budget is 4.7 million (a.pdf p. 3)."),
        _msg("u2", "user", "thanks"),
        _msg("a4", "assistant", "Anytime."),
        _msg("n1", "user", "[system notice]", is_system_notice=True),
        _msg("a5", "assistant", "A Helper finished."),
    ]
    out = project_items(items)
    assert [m["id"] for m in out] == ["u1", "c2", "a3", "u2", "a4", "a5"]
    assert out[1]["blocks"][0]["type"] == "card"
    assert [b["type"] for b in out[2]["blocks"]] == ["text", "card"]
    assert out[2]["blocks"][1]["card"]["card"] == "passages"


def test_the_turns_last_text_is_its_reply_however_short() -> None:
    check = {"slides": 5, "errors": ["Slide 2: runs past the edge"]}
    running = [
        _msg("u1", "user", "Academic Paper"),
        _msg("a0", "assistant", "Building the deck now."),
        _call("c1", "deck_check", "k1"),
        _out("o1", "k1", check),
        _msg("a1", "assistant", "Shorten."),
        _call("c2", "sys_os_edit", "k2"),
        _out("o2", "k2", {"replacements": 1}),
    ]
    # mid-turn: the newest text shows, however short, until a later one replaces it
    assert [m["id"] for m in project_items(running)] == ["u1", "a1"]
    done = [*running, _msg("a2", "assistant", "Your 5-slide deck is ready.")]
    assert [m["id"] for m in project_items(done)] == ["u1", "a2"]


def test_chips_come_last_and_text_after_them_is_the_reply() -> None:
    card = {"type": "card", "card": "follow_ups", "data": {"suggestions": ["x"]}, "fallback": "x"}
    answered = [
        _msg("u1", "user", "hi"),
        _msg("a1", "assistant", "Done, it is saved."),
        _call("c1", "suggest_follow_ups", "k1"),
        _out("o1", "k1", card),
    ]
    assert [m["id"] for m in project_items(answered)] == ["u1", "a1", "c1"]
    # the model kept going after the chips: its last text is the reply, the chips still last
    later = [*answered, _msg("a2", "assistant", "I added a chart."), _msg("u2", "user", "ok")]
    assert [m["id"] for m in project_items(later)] == ["u1", "a2", "c1", "u2"]


def test_system_notice_and_hidden_context_are_dropped() -> None:
    items = [
        _msg("n1", "user", "timer fired", is_system_notice=True),
        _msg("n2", "user", "[System: old notice]"),
        _msg("n3", "user", "skill text", is_meta=True),
        _msg("m1", "user", "real"),
    ]
    assert [m["id"] for m in project_items(items)] == ["m1"]


def _studio_artifact(**fields: Any) -> Any:
    from omnigent.superchat.artifacts.store import Artifact

    base = {
        "id": "v",
        "user_id": None,
        "parent_session_id": "chat",
        "name": "q3.deck.html",
        "title": None,
        "kind": "html",
        "mime": "text/html",
        "size": 1,
        "version": 4,
        "blob_key": "k",
        "published": False,
        "created_at": 0,
        "updated_at": None,
    }
    return Artifact(**{**base, **fields})


def test_a_studios_runtime_lines_are_notices_with_a_kind_and_never_their_text() -> None:
    from omnigent.superchat.artifacts.studio import notice_line, stale_line
    from omnigent.superchat.subagents import STUDIO_QUEUED_NOTE

    # The lines are the ones the studio writes, so a change to their wording fails here.
    hand = notice_line(
        _studio_artifact(origin="manual", edit_summary="slide 1 cover-title font-size 44px→52px"),
        "studio",
    )
    restored = notice_line(
        _studio_artifact(origin="restore", edit_summary="restored version 2", version=5), "studio"
    )
    elsewhere = notice_line(_studio_artifact(chat_id="chat", version=3), "studio")
    update = notice_line(_studio_artifact(origin="update"), "studio")
    stale = stale_line(_studio_artifact(chat_id="chat", version=9), "studio")
    lines = [hand, restored, elsewhere, update, stale, STUDIO_QUEUED_NOTE, "timer fired"]
    items = [
        _msg(f"n{i}", "user", line or "", is_system_notice=True) for i, line in enumerate(lines)
    ]
    messages = project_items(items, keep_notices=True)
    assert [m["role"] for m in messages] == ["system"] * 6  # an unknown line is not shown
    assert [m["blocks"] for m in messages] == [
        [
            {
                "type": "notice",
                "kind": "hand_edit",
                "version": 4,
                "what": "You made the title larger",
                "element_id": "cover-title",
                "element": "the title",
            }
        ],
        [{"type": "notice", "kind": "version", "version": 5}],
        [{"type": "notice", "kind": "version", "version": 3}],
        [{"type": "notice", "kind": "version", "version": 4}],
        [{"type": "notice", "kind": "stale", "version": 9, "origin": "superchat"}],
        [{"type": "notice", "kind": "queued"}],
    ]
    assert project_items(items) == []  # dropped everywhere but a studio


def test_a_hand_edit_with_no_element_reads_as_the_person_did_it() -> None:
    from omnigent.superchat.transcript.blocks import notice_block

    block = notice_block("[you] switched the theme to Pitch (was Ocean) · v7")
    assert block == {
        "type": "notice",
        "kind": "hand_edit",
        "version": 7,
        "what": "You switched the theme to Pitch (was Ocean)",
    }


def test_card_call_becomes_a_card_message_without_internal_fields() -> None:
    items = [_call("c1", "mcp__omnigent__render_card", "k1"), _out("o1", "k1", _CARD)]
    (message,) = project_items(items)
    assert message["role"] == "assistant"
    (block,) = message["blocks"]
    assert block["type"] == "card" and block["card_id"] == "progress-1"
    assert block["card"] == {
        "card": "progress",
        "id": "progress-1",
        "data": {"steps": ["a"]},
        "fallback": "the progress",
    }
    assert "pending" not in block


def test_card_without_output_is_pending_and_failed_card_is_skipped() -> None:
    args = {"card": "progress", "fallback": "wait", "data": {"x": 1}}
    (pending,) = project_items([_call("c1", "render_card", "k1", args)])
    assert pending["blocks"][0]["pending"] is True
    assert pending["blocks"][0]["card"]["data"] == {}
    failed = [_call("c2", "render_card", "k2", args), _out("o2", "k2", {"error": "bad card"})]
    assert project_items(failed) == []


def test_repeated_call_id_is_one_block() -> None:
    items = [
        _call("c1", "render_card", "k1"),
        _call("c1b", "render_card", "k1"),
        _out("o1", "k1", _CARD),
    ]
    assert len(project_items(items)) == 1


def test_helper_row_follows_the_hand_off_message() -> None:
    receipt = {"started": True, "helper_id": "conv_h1", "title": "Research", "message": "x"}
    items = [
        _call("h1", "start_helper", "kh"),
        _out("ho", "kh", receipt),
        _call("h1b", "start_helper", "kh"),  # repeated call: still one row
        _msg("m1", "assistant", "On it."),
    ]
    (message,) = project_items(items, helper_statuses={"conv_h1": "in_progress"})
    assert message["blocks"] == [
        {"type": "text", "text": "On it."},
        {
            "type": "helper",
            "call_id": "kh",
            "session_id": "conv_h1",
            "title": "Research",
            "status": "in_progress",
        },
    ]


def test_refused_helper_call_shows_nothing() -> None:
    items = [
        _call("h1", "start_helper", "kh"),
        _out("ho", "kh", "Error: start_helper: no"),
        _msg("m1", "assistant", "Sorry."),
    ]
    (message,) = project_items(items)
    assert message["blocks"] == [{"type": "text", "text": "Sorry."}]


def test_deck_export_becomes_a_file_block() -> None:
    saved = {"type": "artifact", "id": "art_2", "name": "q3.pptx", "kind": "pptx", "size": 5}
    items = [_call("d1", "deck_export", "kd"), _out("do", "kd", saved)]
    [message] = project_items(items)
    assert message["blocks"][0]["type"] == "file"
    assert message["blocks"][0]["artifact_id"] == "art_2"
    assert (
        project_items([_call("d2", "deck_export", "ke"), _out("eo", "ke", {"error": "x"})]) == []
    )


def test_display_chart_becomes_a_file_block() -> None:
    saved = {
        "type": "artifact", "id": "art_9", "name": "rev.chart.json", "kind": "json", "size": 90,
        "title": "Revenue", "chart": {"chart_type": "bar"},
    }  # fmt: skip
    [message] = project_items([_call("c1", "display_chart", "kc"), _out("co", "kc", saved)])
    block = message["blocks"][0]
    assert block["type"] == "file" and block["artifact_id"] == "art_9"
    assert block["name"] == "rev.chart.json" and block["title"] == "Revenue"
    assert (
        project_items([_call("c2", "display_chart", "kf"), _out("cf", "kf", {"error": "x"})]) == []
    )


def test_display_dashboard_becomes_one_file_block() -> None:
    saved = {
        "type": "artifact", "id": "art_d", "name": "sales.dashboard.json", "kind": "json",
        "size": 900, "title": "Sales", "dashboard": {"kpis": 1, "charts": ["bar", "line"]},
    }  # fmt: skip
    [message] = project_items([_call("d1", "display_dashboard", "kd"), _out("do", "kd", saved)])
    [block] = message["blocks"]
    assert block["type"] == "file" and block["artifact_id"] == "art_d"
    assert block["name"] == "sales.dashboard.json" and block["title"] == "Sales"


def test_delivered_artifact_is_a_user_file_block_and_other_resources_show_nothing() -> None:
    saved = {"type": "artifact", "id": "art_3", "name": "q3.pdf", "kind": "pdf", "size": 7}
    event = {
        "id": "r1",
        "type": "resource_event",
        "event_type": "session.resource.created",
        "resource_id": "art_3",
        "resource_type": "artifact",
        "resource": saved,
        "created_at": 100,
    }
    [message] = project_items([event])
    assert message["blocks"] == [
        {"type": "file", "artifact_id": "art_3", "name": "q3.pdf", "mime": KIND_MIME["pdf"],
         "kind": "pdf", "size": 7, "by": "user"}
    ]  # fmt: skip
    upload = {**event, "id": "r2", "resource_type": "file", "resource_id": "file_1"}
    assert project_items([upload, {**event, "id": "r3", "event_type": "x"}]) == []


def test_artifact_save_becomes_a_file_block_and_secret_request_a_secure_entry() -> None:
    saved = {"type": "artifact", "id": "art_1", "name": "r.pdf", "kind": "pdf", "size": 9}
    request = {"secure_entry": {"id": "req_1", "name": "Login", "site": "example.com"}}
    items = [
        _call("a1", "artifact_save", "ka"),
        _out("ao", "ka", saved),
        _call("v1", "vault_request_secret", "kv"),
        _out("vo", "kv", request),
        _call("a2", "artifact_save", "kb"),
        _out("bo", "kb", {"error": "nope"}),
    ]
    file_msg, secure_msg = project_items(items)
    assert file_msg["blocks"] == [
        {
            "type": "file",
            "artifact_id": "art_1",
            "name": "r.pdf",
            "mime": "application/pdf",
            "kind": "pdf",
            "size": 9,
        }
    ]
    assert secure_msg["blocks"] == [
        {"type": "secure_entry", "request_id": "req_1", "name": "Login", "site": "example.com"}
    ]


def _page_turn(ask: str, saved: dict[str, Any], *extra: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _msg("u1", "user", ask),
        *extra,
        _call("s1", "artifact_save", "ks", {"path": "/w/your_files/p.html"}),
        _out("so", "ks", saved),
        _msg("m1", "assistant", "Here is your page with every price from the file."),
    ]


_PAGE = {"type": "artifact", "id": "art_p", "name": "p.html", "kind": "html", "version": 1}


def _chips(message: dict[str, Any]) -> list[str]:
    return [
        s
        for b in message["blocks"]
        if b["type"] == "card" and b["card"]["card"] == "follow_ups"
        for s in b["card"]["data"]["suggestions"]
    ]


def test_a_page_made_from_a_data_file_ends_with_the_keep_updated_chip() -> None:
    read = _call("r1", "sys_os_read", "kr", {"path": "/w/your_files/tea-prices.csv"})
    messages = project_items(_page_turn("Make a page of my teas", _PAGE, read))
    assert _chips(messages[-1]) == ["Keep this page updated when tea-prices.csv changes"]
    # A Helper's page (shown with a file card) named in the person's own message counts too.
    card = {"type": "card", "card": "file", "data": {"name": "p.html"}, "fallback": "p"}
    items = [
        _msg("u1", "user", "Make a page from your_files/hours.xlsx"),
        _call("c1", "render_card", "kc"),
        _out("co", "kc", card),
        _msg("m1", "assistant", "Your page is in the Library with all the hours."),
    ]
    assert _chips(project_items(items)[-1]) == ["Keep this page updated when hours.xlsx changes"]


@pytest.mark.parametrize(
    ("ask", "saved"),
    [
        ("Make a landing page", _PAGE),  # no data file: a static page
        ("Fix the page from prices.csv", {**_PAGE, "version": 3}),  # not a new page
        ("Make a deck from prices.csv", {**_PAGE, "name": "p.deck.html"}),
    ],
)
def test_no_keep_updated_chip_for_static_pages_edits_or_decks(ask: str, saved: dict) -> None:
    assert _chips(project_items(_page_turn(ask, saved))[-1]) == []


def test_keep_updated_chip_only_on_the_latest_turn_and_never_beside_the_muses_chips() -> None:
    items = [
        *_page_turn("Make a page from prices.csv", _PAGE),
        _msg("u2", "user", "thanks"),
        _msg("m2", "assistant", "Glad it helps, tell me if anything else should change."),
    ]
    assert [_chips(m) for m in project_items(items)] == [[], [], [], [], []]
    chips = _call("f1", "suggest_follow_ups", "kf", {"suggestions": ["Add a chart"]})
    chips_out = _out(
        "fo",
        "kf",
        {
            "type": "card",
            "card": "follow_ups",
            "data": {"suggestions": ["Add a chart"]},
            "fallback": "- Add a chart",
        },
    )
    own = [*_page_turn("Make a page from prices.csv", _PAGE), chips, chips_out]
    assert [c for m in project_items(own) for c in _chips(m)] == ["Add a chart"]


@pytest.mark.parametrize(
    ("stored", "public"),
    [
        ("RuntimeError", "internal"),
        ("ConnectionClosedException", "internal"),
        ("model_unavailable", "internal"),
        ("rate_limit_exceeded", "rate_limited"),
        ("connection_error", "provider_unavailable"),
        ("server_error", "provider_unavailable"),
        ("context_length_exceeded", "context_too_long"),
        ("budget_exhausted", "insufficient_credit"),
        ("managed_sandbox_workspace_reset", "workspace_reset"),
        ("timeout", "timeout"),
        ("insufficient_credit", "insufficient_credit"),
    ],
)
def test_legacy_error_codes_read_as_the_closed_set(stored: str, public: str) -> None:
    (message,) = project_items([{"id": "e1", "type": "error", "code": stored, "message": "x"}])
    assert message["blocks"] == [{"type": "error", "code": public}]


def test_error_item_carries_a_code_and_no_text() -> None:
    item = {"id": "e1", "type": "error", "code": "rate_limited", "message": "Too many requests"}
    (message,) = project_items([item])
    assert message["blocks"] == [{"type": "error", "code": "rate_limited"}]
    assert "Too many" not in json.dumps(message)


# ── route: paging, access ────────────────────────────────────────────


def _new(kind: str, data: Any, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(type=kind, response_id=response_id, data=data)


def _say(role: str, text: str) -> NewConversationItem:
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    return _new(
        "message",
        MessageData(role=role, content=content, agent="brain" if role == "assistant" else None),
    )


@pytest.fixture()
async def client(conversation_store: SqlAlchemyConversationStore) -> httpx.AsyncClient:
    app = FastAPI()
    router = APIRouter()
    register_transcript_routes(router, conversation_store=conversation_store)
    app.include_router(router, prefix="/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


_RECORDED = Path(__file__).parent / "rounds"


def _replay_recorded(
    conversations: SqlAlchemyConversationStore,
    rounds: SqlAlchemyRoundStore,
    artifacts: SqlAlchemyArtifactStore,
    root: str,
) -> list[str]:
    """Replay the recorded Round streams five times into one Conversation: its items, its
    Helpers, its Round events and its saved versions. In every other pass the Assistant saves
    the file a Helper saved in the recording, with an ``artifact_save`` call in the Conversation.

    :returns: The Round ids, oldest first.
    """
    opened: list[str] = []
    for n, path in enumerate(sorted(_RECORDED.glob("*.jsonl")) * 5):
        base = n * 100_000
        ids: dict[str, str] = {}  # recorded Round or Helper id -> stored id
        events: list[RoundEvent] = []
        said: list[str] = []
        names: dict[str, str] = {}
        for line in path.read_text().splitlines():
            ev = json.loads(line)
            kind, actor = ev.pop("kind"), ev.pop("actor")
            at, recorded = base + int(ev.pop("t") * 1000), ev.pop("round_id")
            if kind == "round.opened":
                (asked,) = conversations.append(root, [_say("user", ev["text"])])
                ids[recorded] = asked.id
                opened.append(asked.id)
            rid = ids[recorded]
            if "helper_id" in ev or (kind == "tool.result" and ev.get("name") == "start_helper"):
                key = ev.get("helper_id", "h1")
                if key not in ids:
                    ids[key] = conversations.create_conversation(
                        kind="sub_agent",
                        parent_conversation_id=root,
                        title=f"Deck {n}",
                        labels=_MODE,
                    ).id
                ev["helper_id"] = ids[key]
            if kind == "tool.started" and actor == "assistant":
                names[ev["call_id"]] = ev["name"]
            elif kind == "tool.result" and actor == "assistant":
                call_id, output = f"{n}{ev['call_id']}", "{}"
                if ev["name"] == "start_helper":
                    output = json.dumps({"started": True, "helper_id": ev.pop("helper_id")})
                conversations.append(root, _tool(call_id, names[ev["call_id"]], output))
            elif kind == "words" and actor == "assistant":
                said.append(ev["text"])
            elif kind == "turn.ended" and actor == "assistant" and said:
                conversations.append(root, [_say("assistant", "".join(said))])
                said = []
            elif kind == "file.saved":
                by_assistant = n // 4 % 2 == 1
                saver = root if by_assistant else ev["helper_id"]
                name = f"{ev['name']}{DECK_SUFFIX}"
                saved = artifacts.create(
                    user_id=None,
                    parent_session_id=root,
                    name=name,
                    title="Uruguay",
                    kind="html",
                    mime=KIND_MIME["html"],
                    data=b"<p>deck</p>",
                    chat_id=saver,
                    round_id=rid,
                )
                ev.update(name=name, file_kind="html", artifact_id=saved.id, version=saved.version)
                if by_assistant:
                    actor = "assistant"
                    del ev["helper_id"]
                    result = {"type": "artifact", "id": saved.id, "name": name, "kind": "html"}
                    result["version"] = saved.version
                    conversations.append(
                        root, _tool(f"{n}save", "artifact_save", json.dumps(result))
                    )
            events.append(RoundEvent(rid, root, kind, actor, at, ev))
        rounds.append(events)
    return opened


def _tool(call_id: str, name: str, output: str) -> list[NewConversationItem]:
    call = FunctionCallData(agent="brain", name=name, arguments="{}", call_id=call_id)
    return [
        _new("function_call", call),
        _new("function_call_output", FunctionCallOutputData(call_id=call_id, output=output)),
    ]


def cards(messages: list[dict[str, Any]]) -> list[tuple[str, int | None]]:
    """The file cards of transcript *messages* in order, as ``(artifact_id, version)``."""
    return [
        (b["artifact_id"], b.get("version"))
        for m in messages
        for b in m["blocks"]
        if b["type"] == "file" and b.get("by") != "user"
    ]


def _file_card(saved: Any) -> str:
    data = {"name": saved.name, "artifactId": saved.id, "version": saved.version}
    return json.dumps({"type": "card", "card": "file", "data": data, "fallback": saved.name})


def _resave_and_show(
    conversations: SqlAlchemyConversationStore,
    rounds: SqlAlchemyRoundStore,
    artifacts: SqlAlchemyArtifactStore,
    root: str,
) -> tuple[str, str]:
    """A Round in which the Assistant saves a deck twice, shows it with a file card, and shows
    an older file with another: three cards for one file before the Round drew them.

    :returns: The Round id and the older file's artifact id.
    """
    (asked,) = conversations.append(root, [_say("user", "Make a deck on the history of jazz")])
    older = artifacts.create(
        user_id=None,
        parent_session_id=root,
        name="notes.md",
        title="Notes",
        kind="md",
        mime=KIND_MIME["md"],
        data=b"notes",
        chat_id=root,
    )
    events = [RoundEvent(asked.id, root, "round.opened", "person", 9_000_000, {"text": "jazz"})]
    for n in (1, 2):
        saved = artifacts.create(
            user_id=None,
            parent_session_id=root,
            name=f"history-of-jazz{DECK_SUFFIX}",
            title="Jazz",
            kind="html",
            mime=KIND_MIME["html"],
            data=b"<p>v%d</p>" % n,
            chat_id=root,
            round_id=asked.id,
        )
        result = {"type": "artifact", "id": saved.id, "name": saved.name, "kind": "html"}
        conversations.append(
            root, _tool(f"jazz{n}", "artifact_save", json.dumps({**result, "version": n}))
        )
        body = {"name": saved.name, "file_kind": "html", "artifact_id": saved.id, "version": n}
        events.append(RoundEvent(asked.id, root, "file.saved", "assistant", 9_000_000 + n, body))
    conversations.append(root, _tool("jazz3", "render_card", _file_card(saved)))
    conversations.append(root, _tool("jazz4", "render_card", _file_card(older)))
    conversations.append(root, [_say("assistant", "Your jazz deck is ready.")])
    rounds.append(events)
    return asked.id, older.id


async def test_pages_walk_back_and_draw_each_rounds_cards_at_any_page_size(
    client: httpx.AsyncClient,
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """The recorded Rounds, replayed into one Conversation, page back to their first message at
    9, 40, 100 and 500 items a page. A Round's file cards are every file of its view, the
    Assistant's and its Helper's, in order on the Round's last reply, one per file at its latest
    version, and a page's size never moves a card. A file card the Assistant drew for a file of
    its Round is dropped; one for a file outside it stays. Every message carries the Round it is
    in (``round_id``, the id of the message that opened it), whatever the page size."""
    rounds = SqlAlchemyRoundStore(db_uri)
    blobs = LocalArtifactStore(str(tmp_path / "blobs"))
    artifacts = SqlAlchemyArtifactStore(db_uri, lambda: blobs)
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    opened = _replay_recorded(conversation_store, rounds, artifacts, root.id)
    jazz, older = _resave_and_show(conversation_store, rounds, artifacts, root.id)
    opened.append(jazz)
    assert len(conversation_store.list_items(root.id, limit=500).data) > 100

    def read_all(limit: int) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        before = None
        while True:
            page = read_transcript(
                conversation_store,
                root,
                before=before,
                limit=limit,
                rounds=rounds,
                artifacts=artifacts,
            )
            messages = page["data"] + messages
            if not page["has_more"]:
                assert page["older_cursor"] is None and page["lineage"]["kind"] == "super"
                return messages
            before = page["older_cursor"]

    def by_message(messages: list[dict[str, Any]]) -> list[tuple[str, list[Any]]]:
        return [(m["id"], cards([m])) for m in messages if cards([m])]

    whole = read_all(500)
    assert [m["id"] for m in whole if m["role"] == "user"] == opened
    in_round, current = {}, None
    for message in whole:
        current = message["id"] if message["role"] == "user" else current
        in_round[message["id"]] = current
    assert {m["id"]: m["round_id"] for m in whole} == in_round
    for limit in (9, 40, 100):
        paged = read_all(limit)
        if limit >= 40:  # a smaller page can split a turn from its last text, so shows more
            assert [m["id"] for m in paged] == [m["id"] for m in whole]
        assert by_message(paged) == by_message(whole)
        assert all(in_round.get(m["id"], m["round_id"]) == m["round_id"] for m in paged)
    views = {rid: fold(rounds.events(rid, root.id), rid)["files"] for rid in opened}
    recorded = [(f["artifact_id"], f["version"]) for rid in opened for f in views[rid]]
    assistants = [f for rid in opened for f in views[rid] if f["helper_id"] is None]
    assert (len(assistants), len(recorded)) == (5, 11)
    assert cards(whole) == recorded
    current, per_round = None, {}
    for message in whole:
        if message["role"] == "user":
            current = message["id"]
        elif cards([message]):
            assert message["blocks"][0]["type"] == "text"  # after the reply's words
            per_round.setdefault(current, []).extend(cards([message]))
    assert per_round == {
        rid: [(f["artifact_id"], f["version"]) for f in views[rid]] for rid in opened if views[rid]
    }
    (deck,) = views[jazz]
    assert per_round[jazz] == [(deck["artifact_id"], 2)]
    shown = [
        b["card"]["data"]["artifactId"]
        for m in whole
        if m["round_id"] == jazz
        for b in m["blocks"]
        if b["type"] == "card" and b["card"]["card"] == "file"
    ]
    assert shown == [older]
    with pytest.raises(OmnigentError, match="not found"):
        await client.get("/v1/sessions/ad563e906854634c49e1a6fd2fbb31d4/transcript")


async def test_store_items_project_end_to_end_with_error_code_and_helper_status(
    client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    helper = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id, title="Job", labels=_MODE
    )
    conversation_store.set_session_live_status(helper.id, "running")
    receipt = json.dumps({"started": True, "helper_id": helper.id, "title": "Job"})
    conversation_store.append(
        root.id,
        [
            _say("user", "do it"),
            _new(
                "function_call",
                FunctionCallData(agent="brain", name="start_helper", arguments="{}", call_id="kh"),
            ),
            _new("function_call_output", FunctionCallOutputData(call_id="kh", output=receipt)),
            _say("assistant", "Started."),
            _new(
                "error",
                ErrorData(source="llm", code="RuntimeError", message="Raw English failure"),
            ),
        ],
    )
    body = (await client.get(f"/v1/sessions/{root.id}/transcript")).json()
    assistant, error = body["data"][1], body["data"][2]
    assert assistant["blocks"][1]["status"] == "in_progress"
    assert assistant["blocks"][1]["session_id"] == helper.id
    assert error["blocks"] == [{"type": "error", "code": "internal"}]
    helper_view = (await client.get(f"/v1/sessions/{helper.id}/transcript")).json()
    assert helper_view["lineage"]["kind"] == "helper"
    assert helper_view["lineage"]["root_id"] == root.id


# ── reset, live flag, redaction ──────────────────────────────────────────


@pytest.fixture()
async def reset_client(conversation_store: SqlAlchemyConversationStore) -> httpx.AsyncClient:
    from omnigent.superchat.transcript.reset import register_reset_routes

    app = FastAPI()
    router = APIRouter()
    register_transcript_routes(router, conversation_store=conversation_store)
    register_reset_routes(router, conversation_store=conversation_store)
    app.include_router(router, prefix="/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


def _texts(body: dict[str, Any]) -> list[str]:
    return [m["blocks"][0]["text"] for m in body["data"]]


async def test_reset_starts_the_transcript_after_it_and_pages_the_history_on_request(
    reset_client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    from omnigent.context.rollover import RESET_HEADER, split_at_latest_compaction

    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.append(root.id, [_say("user", "old q"), _say("assistant", "old a")])
    url = f"/v1/sessions/{root.id}/transcript"
    assert (await reset_client.get(url)).json()["reset"] is None

    reset = (await reset_client.post(f"/v1/sessions/{root.id}/reset")).json()
    assert reset["reset_item_id"] and reset["session_id"] == root.id
    # Idempotent while nothing happened since.
    again = (await reset_client.post(f"/v1/sessions/{root.id}/reset")).json()
    assert again["reset_item_id"] == reset["reset_item_id"]
    conversation_store.append(root.id, [_say("user", "new q"), _say("assistant", "new a")])

    body = (await reset_client.get(url)).json()
    assert _texts(body) == ["new q", "new a"]
    assert body["has_more"] is False and body["older_cursor"] is None
    assert body["reset"] == {"item_id": reset["reset_item_id"], "created_at": reset["created_at"]}
    assert (await reset_client.get(url, params={"limit": 1})).json()["has_more"] is True

    history = (await reset_client.get(url, params={"before_reset": "true"})).json()
    assert _texts(history) == ["old q", "old a"]

    # The model's context after the reset: the reset framing, then only the new turns.
    items = [i.to_api_dict() for i in conversation_store.list_items(root.id, limit=50).data]
    after, checkpoint = split_at_latest_compaction(items)
    assert checkpoint is not None and checkpoint["summary"] == RESET_HEADER
    assert [i["content"][0]["text"] for i in after] == ["new q", "new a"]
    assert checkpoint["compacted_messages"][0]["content"][0]["text"] == RESET_HEADER


async def test_reset_is_refused_mid_turn_and_outside_a_super_chat(
    reset_client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.append(root.id, [_say("user", "q")])
    conversation_store.set_session_live_status(root.id, "running")
    with pytest.raises(OmnigentError, match="turn is running"):
        await reset_client.post(f"/v1/sessions/{root.id}/reset")
    helper = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id, title="job", labels=_MODE
    )
    plain = conversation_store.create_conversation(kind="default", title="P")
    for other in (helper, plain):
        with pytest.raises(OmnigentError, match="Only a Super Chat"):
            await reset_client.post(f"/v1/sessions/{other.id}/reset")


async def test_before_reset_is_empty_without_a_reset_and_live_follows_the_turn(
    reset_client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore
) -> None:
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.append(root.id, [_say("user", "q")])
    url = f"/v1/sessions/{root.id}/transcript"
    empty = (await reset_client.get(url, params={"before_reset": "true"})).json()
    assert empty["data"] == [] and empty["has_more"] is False
    assert (await reset_client.get(url)).json()["live"] is False
    conversation_store.set_session_live_status(root.id, "running")
    assert (await reset_client.get(url)).json()["live"] is True


async def test_transcript_redacts_deployment_secrets(
    reset_client: httpx.AsyncClient,
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-test-0123456789abcdef"
    monkeypatch.setenv("ACME_API_KEY", secret)
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    card = {**_CARD, "fallback": f"key {secret}", "data": {"k": secret}}
    conversation_store.append(
        root.id,
        [
            _say("assistant", f"your key is {secret}"),
            _new(
                "function_call",
                FunctionCallData(agent="brain", name="render_card", arguments="{}", call_id="k"),
            ),
            _new(
                "function_call_output",
                FunctionCallOutputData(call_id="k", output=json.dumps(card)),
            ),
        ],
    )
    raw = (await reset_client.get(f"/v1/sessions/{root.id}/transcript")).text
    assert secret not in raw and "[redacted]" in raw


async def test_a_message_waiting_behind_the_running_turn_reads_as_queued(
    client: httpx.AsyncClient,
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.superchat.transcript import routes as transcript_routes

    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    first, waiting = conversation_store.append(
        root.id, [_say("user", "redo"), _say("user", "and blue")]
    )
    asked: list[str] = []

    async def runner_queue(request: object, session_id: str) -> set[str]:
        asked.append(session_id)
        return {waiting.id}

    monkeypatch.setattr(transcript_routes, "_waiting_item_ids", runner_queue)
    url = f"/v1/sessions/{root.id}/transcript"

    # Not mid-turn: nothing waits, and the runner is not asked.
    messages = {m["id"]: m for m in (await client.get(url)).json()["data"]}
    assert "delivered" not in messages[waiting.id]
    assert asked == []

    # Mid-turn: the runner's live queue is the only source.
    conversation_store.set_session_live_status(root.id, "running")
    messages = {m["id"]: m for m in (await client.get(url)).json()["data"]}
    assert messages[waiting.id]["delivered"] == "queued"
    assert "delivered" not in messages[first.id]
