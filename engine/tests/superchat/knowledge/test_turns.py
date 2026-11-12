"""The ``conversation`` source: cutting a record into turn passages, storing them, keeping them
out of everything that lists files, and the references passages carry."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnigent.memory.service import render_profile_block
from omnigent.superchat.artifacts.writeback import _note
from omnigent.superchat.knowledge import refs as refs_mod
from omnigent.superchat.knowledge.attachment_context import build_block
from omnigent.superchat.knowledge.db import KIND_TURN, TurnPassage
from omnigent.superchat.knowledge.indexer import Indexer
from omnigent.superchat.knowledge.refs import journal_refs, line_refs, turn_refs
from omnigent.superchat.knowledge.turns import passage_of, split_turns
from omnigent.superchat.local_time import local_time_block
from omnigent.superchat.projects.block import PROJECTS_OPEN
from omnigent.superchat.projects.choice import MOVED_NOTE
from omnigent.superchat.prompt_prefix import PROFILE_UNCHANGED_LINE, idle_gap_line
from omnigent.superchat.recall_hint.hint import FOOTER as HINT_FOOTER
from omnigent.superchat.recall_hint.hint import HEADER as HINT_HEADER
from tests.superchat.knowledge._fixtures import TAG, FakeEmbedder, build_indexer, run_all

DAY = int(datetime(2026, 10, 12, 9, tzinfo=UTC).timestamp())


def _msg(item_id: str, role: str, text: str, rid: str, **extra: Any) -> dict[str, Any]:
    kind = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "response_id": rid,
        "type": "message",
        "role": role,
        "created_at": DAY,
        "content": [{"type": kind, "text": text}],
        **extra,
    }


def _call(call_id: str, name: str, output: str, rid: str) -> list[dict[str, Any]]:
    return [
        {"id": call_id, "response_id": rid, "type": "function_call", "call_id": call_id,
         "name": name, "arguments": "{}"},
        {"id": f"{call_id}o", "response_id": rid, "type": "function_call_output",
         "call_id": call_id, "output": output},
    ]  # fmt: skip


def _passage(turn_id: str, text: str, **kw: Any) -> TurnPassage:
    fields: dict[str, Any] = {
        "chat_id": "conv_main",
        "turn_item_id": turn_id,
        "date": "2026-10-12",
        "origin": "main",
        "project": "",
        "person_text": text[:40],
        "text": text,
        "main_id": "conv_main",
        **kw,
    }
    return TurnPassage(**fields)


def _turn_hits(indexer: Indexer, query: str) -> list[dict[str, str]]:
    found = indexer.db.search(query, limit=8, turns=True)
    meta = indexer.db.turn_meta([chunk.id for chunk, _ in found])
    return [meta[chunk.id] for chunk, _ in found]


# ── cutting a record into turns ──────────────────────────────────────────


def test_a_turn_is_the_person_the_final_reply_and_the_tool_digests() -> None:
    items = [
        _msg("u1", "user", "Book the venue", "r1"),
        *_call("c1", "web_search", "three venues", "r1"),
        _msg("a1", "assistant", "Looking.", "r1"),
        _msg("a2", "assistant", "Booked the Harbor Hall.", "r1"),
    ]
    (turn,) = split_turns(items)
    p = passage_of(turn, chat_id="conv_main", main_id="conv_main", origin="main", project="launch")
    assert p.text.startswith("Person: Book the venue\nAssistant: Booked the Harbor Hall.")
    assert "web_search" in p.text and "Looking." not in p.text
    assert (p.chat_id, p.turn_item_id, p.date, p.origin, p.project, p.person_text) == (
        "conv_main", "u1", "2026-10-12", "main", "launch", "Book the venue"
    )  # fmt: skip


def test_a_passage_is_clipped_to_the_passage_limit() -> None:
    items = [
        _msg("u1", "user", "x " * 2000, "r1"),
        _msg("a1", "assistant", "y " * 2000, "r1"),
        *_call("c1", "web_search", "z " * 500, "r1"),
    ]
    p = passage_of(split_turns(items)[0], chat_id="c", main_id="c", origin="main")
    assert len(p.text) <= 1600 and len(p.person_text) <= 300


def test_a_steer_joins_its_turn_and_notices_and_hidden_context_make_no_passage() -> None:
    items = [
        _msg("u1", "user", "Plan the launch", "r1"),
        _msg("a0", "assistant", "Starting.", "r1"),
        _msg("u2", "user", "also add a budget line", "r1"),  # steer: the running turn's id
        _msg("a1", "assistant", "Done, with a budget line.", "r1"),
        _msg("n1", "user", "[System: timer 1 fired]", "r2"),
        _msg("a2", "assistant", "The timer fired.", "r2"),
        _msg("n2", "user", "A Helper finished", "r3", is_system_notice=True),
        _msg("m1", "user", "Hidden context", "r3", is_meta=True),
        _msg("a3", "assistant", "Noted.", "r3"),
        _msg("u3", "user", "Next step?", "r4"),
    ]
    turns = split_turns(items)
    assert [(t.turn_item_id, t.notice) for t in turns] == [
        ("u1", False), ("n1", True), ("n2", True), ("u3", False)
    ]  # fmt: skip
    first = passage_of(turns[0], chat_id="c", main_id="c", origin="main")
    assert "budget line" in first.text and "Done, with a budget line." in first.text
    assert "timer" not in first.text and first.person_text == "Plan the launch"


def _prefixed(message: str) -> str:
    """*message* as a harness that keeps its own transcript hands it back: every block the runner
    can prepend, built by its producer, in the order they render (local time first)."""
    blocks = [
        local_time_block("America/Toronto", now=datetime(2026, 10, 12, 13, tzinfo=UTC)),
        idle_gap_line(3 * 86400),
        MOVED_NOTE.format("/home/me/workspace/projects/launch"),
        _note("brief.docx", 2, 3, ["retitled"], written=False),
        f"{PROJECTS_OPEN} Pick one.\n- launch: Launch [open now]\n- budget: Budget]",
        render_profile_block("- Prefers short answers\n\n- Works on the launch"),
        "\n".join([HINT_HEADER, "- 2026-10-01 conversation: venue list [t:abc]", HINT_FOOTER]),
        build_block([{"name": "a.txt", "path": "uploads/a.txt", "pages": 1, "file_id": "f1",
                      "tabular": False, "markdown": "Notes [draft]\n\nSecond para"}]),
    ]  # fmt: skip
    return "\n\n".join([*(b for b in blocks if b), message])


def test_only_the_persons_own_words_are_indexed() -> None:
    items = [
        _msg("u1", "user", _prefixed("Which venue did we pick?\n\nKeep it short."), "r1"),
        _msg("a1", "assistant", "Harbor Hall.", "r1"),
        _msg("u2", "user", _prefixed("and the date?"), "r1"),  # a steer, prefixed too
    ]
    (turn,) = split_turns(items)
    p = passage_of(turn, chat_id="c", main_id="c", origin="main")
    assert p.person_text == "Which venue did we pick? Keep it short."
    assert p.text == (
        "Person: Which venue did we pick?\n\nKeep it short.\nPerson: and the date?\n"
        "Assistant: Harbor Hall."
    )


def test_a_message_of_injected_blocks_only_opens_a_turn_that_is_never_indexed() -> None:
    # Nobody said anything: no person turn, and its reply is not taken for the turn before's.
    items = [
        _msg("u1", "user", "Plan the launch", "r1"),
        _msg("a1", "assistant", "Planned.", "r1"),
        _msg("u2", "user", _prefixed(PROFILE_UNCHANGED_LINE), "r2"),
        _msg("a2", "assistant", "Anything else?", "r2"),
    ]
    turns = split_turns(items)
    assert [(t.turn_item_id, t.notice) for t in turns] == [("u1", False), ("u2", True)]
    assert turns[1].person == ""
    assert passage_of(turns[0], chat_id="c", main_id="c", origin="main").text.endswith("Planned.")


def test_a_message_that_quotes_a_marker_later_keeps_every_word() -> None:
    text = "Why does my message start with [Current local time for the person: noon]?"
    (turn,) = split_turns([_msg("u1", "user", f"Hi.\n{text}", "r1")])
    assert turn.person == f"Hi.\n{text}"


# ── storing ──────────────────────────────────────────────────────────────


def test_a_turn_is_searchable_at_once_and_embedded_within_one_tick(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path)
    indexer.index_turns(
        [_passage("u1", "Person: venue deposit\nAssistant: paid", project="spring")]
    )
    (hit,) = _turn_hits(indexer, "venue deposit")
    assert hit == {
        "chat_id": "conv_main", "turn_item_id": "u1", "date": "2026-10-12", "origin": "main",
        "project": "spring", "person_text": "Person: venue deposit\nAssistant: paid"[:40],
    }  # fmt: skip
    run_all(indexer)
    (row,) = indexer.db.files(turns=True)
    assert row.kind == KIND_TURN and row.state == "ready" and row.embed_status == "embedded"
    assert {c.embed_tag for c in indexer.db.chunks_for(row.file_id)} == {TAG}
    vector = FakeEmbedder().embed(["venue deposit"]).vectors[0]
    found = indexer.db.search("venue", limit=5, query_vector=vector, keyword=False, turns=True)
    assert [c.text for c, _ in found] == ["Person: venue deposit\nAssistant: paid"]


def test_without_embeddings_a_turn_is_found_by_keywords(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    indexer.index_turns([_passage("u1", "Person: venue deposit")])
    run_all(indexer)
    (row,) = indexer.db.files(turns=True)
    assert row.state == "ready" and row.embed_reason == "no_connection"
    assert [h["turn_item_id"] for h in _turn_hits(indexer, "venue")] == ["u1"]


def test_indexing_a_turn_again_is_idempotent_and_a_changed_one_is_replaced(
    tmp_path: Path,
) -> None:
    indexer, _ = build_indexer(tmp_path)
    assert indexer.index_turns([_passage("u1", "Person: venue")]) == 1
    run_all(indexer)
    assert indexer.index_turns([_passage("u1", "Person: venue")]) == 0
    assert indexer.db.files(turns=True)[0].state == "ready"  # nothing to embed again
    indexer.index_turns([_passage("u1", "Person: venue\nAssistant: booked", project="spring")])
    (row,) = indexer.db.files(turns=True)
    assert [c.text for c in indexer.db.chunks_for(row.file_id)] == [
        "Person: venue\nAssistant: booked"
    ]
    assert _turn_hits(indexer, "booked")[0]["project"] == "spring"


def test_delete_chats_removes_their_passages_only(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path)
    indexer.index_turns(
        [
            _passage("u1", "Person: venue"),
            _passage("u2", "Person: venue again", chat_id="conv_b"),
            _passage("u3", "Person: venue other", chat_id="conv_x", main_id="conv_x"),
        ]
    )
    run_all(indexer)
    assert indexer.db.chats_of("conv_main") == ["conv_b", "conv_main"]
    assert indexer.delete_chats(["conv_main", "conv_b"]) == 2
    assert [h["turn_item_id"] for h in _turn_hits(indexer, "venue")] == ["u3"]
    assert [r.name for r in indexer.db.files(turns=True)] == ["conv_x"]


def test_passages_stay_out_of_file_listings_collections_and_file_searches(
    tmp_path: Path,
) -> None:
    indexer, workspace = build_indexer(tmp_path)
    (workspace / "your_files" / "notes.md").write_text("# Notes\n\nThe venue deposit is paid.\n")
    indexer.scan()
    run_all(indexer)
    indexer.index_turns([_passage("u1", "Person: venue deposit")])  # not embedded yet: pending
    status = indexer.status()
    assert [f["name"] for f in status["files"]] == ["notes.md"]
    assert status["pending"] == 0
    assert {c["name"]: c["files"] for c in status["collections"]} == {"your_files": 1}
    found = indexer.search("venue deposit", mode="hybrid")
    assert [r["file_name"] for r in found["results"]] == ["notes.md"]
    assert found["files_indexing"] == 0
    assert indexer.search("venue deposit", mode="vector")["results"][0]["file_name"] == "notes.md"
    turn_row = indexer.db.files(turns=True)[0]
    assert indexer.resolve(turn_row.file_id, None) is None
    assert indexer.resolve(None, turn_row.path) is None
    assert indexer.multi_get("*")["matched"] == 1
    indexer.scan()  # a reconcile with the disk leaves the passages alone
    assert len(indexer.db.files(turns=True)) == 2


def test_reindex_sends_passages_back_and_counts_files_only(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    indexer.index_turns([_passage("u1", "Person: venue")])
    run_all(indexer)
    indexer.embedder = FakeEmbedder()
    assert indexer.reindex() == 0
    run_all(indexer)
    assert indexer.db.files(turns=True)[0].embed_status == "embedded"


# ── references ───────────────────────────────────────────────────────────

LINE = (
    "- 2026-10-12 · Updated the budget · main <!-- conv_abc "
    "files:projects/spring-launch/launch-budget.xlsx,your_files/quote.pdf "
    "artifacts:art_7f2@v3 project:spring-launch helpers:hr_41 -->"
)


def test_a_journal_line_gives_its_references() -> None:
    assert line_refs(LINE) == {
        "files": ["projects/spring-launch/launch-budget.xlsx", "your_files/quote.pdf"],
        "artifacts": ["art_7f2@v3"],
        "project": ["spring-launch"],
        "helpers": ["hr_41"],
    }
    assert line_refs("- 2026-10-12 · Booked the venue · main <!-- conv_abc -->") == {}
    assert line_refs("- 2026-10-12 · no comment at all") == {}
    assert line_refs("- x <!-- chat:conv_abc project:a unknown:b -->") == {"project": ["a"]}
    encoded = "- x <!-- conv_abc files:My%20Notes.md,a%2Cb.csv -->"
    assert line_refs(encoded) == {"files": ["My Notes.md", "a,b.csv"]}


def test_a_passage_of_several_lines_carries_each_reference_once() -> None:
    more = "- 2026-10-12 · More · main <!-- conv_abc project:spring-launch helpers:hr_9 -->"
    text = f"{LINE}\n{more}"
    refs = journal_refs(text)
    assert refs["project"] == ["spring-launch"] and refs["helpers"] == ["hr_41", "hr_9"]


def test_journal_passages_carry_their_references_and_can_be_filtered(tmp_path: Path) -> None:
    indexer, workspace = build_indexer(tmp_path)
    journal = workspace / "memory" / "journal"
    journal.mkdir(parents=True)
    (journal / "2026-10-12.md").write_text(f"# 2026-10-12\n\n{LINE}\n", encoding="utf-8")
    (workspace / "your_files" / "plain.md").write_text("# Plain\n\nproject:spring-launch -->\n")
    indexer.scan()
    run_all(indexer)
    ids = indexer.db.chunks_with_ref("project", "spring-launch")
    (refs,) = indexer.db.refs_of(sorted(ids)).values()
    assert refs["artifacts"] == ["art_7f2@v3"] and refs["helpers"] == ["hr_41"]
    (journal / "2026-10-12.md").write_text("# 2026-10-12\n\n- 2026-10-12 · plain · main\n")
    indexer.scan()
    run_all(indexer)
    assert indexer.db.chunks_with_ref("project", "spring-launch") == set()


def test_a_turn_without_the_refs_module_has_no_references() -> None:
    items = [_msg("u1", "user", "Open the budget", "r1"), *_call("c1", "files_get", "ok", "r1")]
    if "omnigent.context.refs" not in sys.modules:
        assert turn_refs(items) == {}
    p = passage_of(split_turns(items)[0], chat_id="c", main_id="c", origin="main", project="x")
    assert p.project == "x"


def test_a_turn_carries_its_references_and_a_project_reference_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[dict[str, Any]]] = []

    def refs_from_items(items: list[dict[str, Any]]) -> dict[str, list[str]]:
        seen.append(items)
        return {"files": ["your_files/quote.pdf"], "project": ["spring-launch"], "helpers": []}

    monkeypatch.setattr(refs_mod, "refs_from_items", refs_from_items)
    items = [_msg("u1", "user", "Open the quote", "r1"), *_call("c1", "files_get", "ok", "r1")]
    p = passage_of(split_turns(items)[0], chat_id="c", main_id="c", origin="main", project="x")
    assert seen[0][0]["type"] == "function_call"
    assert p.project == "spring-launch" and p.refs == {"files": ["your_files/quote.pdf"]}
    indexer, _ = build_indexer(tmp_path)
    indexer.index_turns([p])
    (chunk_id,) = indexer.db.chunks_with_ref("files", "your_files/quote.pdf")
    assert indexer.db.refs_of([chunk_id])[chunk_id] == {
        "files": ["your_files/quote.pdf"], "project": ["spring-launch"]
    }  # fmt: skip
    assert indexer.db.turn_meta([chunk_id])[chunk_id]["project"] == "spring-launch"
