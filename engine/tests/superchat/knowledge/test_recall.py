"""Recall's one search (ADR 0016, C2): sub-queries, sources, recency, collapse, dated hits."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnigent.runner.memory import files as journal
from omnigent.superchat.knowledge.recall import SOURCES, recall_search, recency_factor
from tests.superchat.knowledge._fixtures import (
    FakeEmbedder,
    build_indexer,
    put_turn,
    run_all,
)


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def _put(workspace: Path, rel: str, text: str) -> None:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def recall(indexer, queries, **kwargs):
    kwargs.setdefault("sources", {"files", "journal", "conversation"})
    return asyncio.run(recall_search(queries, indexer=indexer, **kwargs))


def test_a_passage_that_answers_two_sub_queries_ranks_first(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/a.md", "The venue is booked for the spring launch.")
    _put(ws, "your_files/b.md", "The catering menu has three courses.")
    _put(ws, "your_files/c.md", "Venue and catering were confirmed together.")
    indexer.scan()
    run_all(indexer)

    hits = recall(indexer, ["venue", "catering"], sources={"files"})

    assert hits[0].path == "your_files/c.md"
    assert {h.path for h in hits} == {"your_files/a.md", "your_files/b.md", "your_files/c.md"}
    assert hits[0].score > hits[1].score


def test_conversation_turns_are_found_only_when_asked(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/plan.md", "The budget for the offsite is set.")
    indexer.scan()
    run_all(indexer)
    put_turn(indexer, "c1", "t1", _day(3), "We agreed the offsite budget is 50k.")

    files_only = recall(indexer, ["offsite budget"], sources={"files", "journal"})
    assert [h.source for h in files_only] == ["files"]
    assert all(r["path"] == "your_files/plan.md" for r in indexer.search("budget")["results"])

    turns = recall(indexer, ["offsite budget"], sources={"conversation"})
    assert [(h.source, h.chat_id, h.turn_item_id, h.date) for h in turns] == [
        ("conversation", "c1", "t1", _day(3))
    ]
    assert turns[0].title == "We agreed the offsite budget is 50k"


def test_every_hit_is_dated_and_carries_its_read_ids(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/venue.md", "The venue contract is signed.")
    journal.append_lines(
        ws,
        [journal.format_line("2026-03-04", "Venue shortlist sent", journal.Origin("main", "c1"))],
    )
    indexer.scan()
    run_all(indexer)
    put_turn(indexer, "c1", "t1", "2026-09-30T10:00:00+00:00", "Which venue did we pick?")

    hits = recall(indexer, ["venue"], recency=False)

    by_source = {h.source: h for h in reversed(hits)}  # each source's best hit
    assert set(by_source) == {"files", "journal", "conversation"}
    file_hit = by_source["files"]
    assert (file_hit.page, file_hit.title, file_hit.path) == (1, "venue.md", "your_files/venue.md")
    assert file_hit.id == f"f_{file_hit.file_id}@1"
    assert file_hit.date == datetime.now(UTC).date().isoformat()
    line = by_source["journal"]
    assert (line.path, line.date) == ("memory/journal/2026-03-04.md", "2026-03-04")
    assert line.id.startswith("ep_") and line.id[3:].isdigit()
    text = (ws / line.path).read_text().splitlines()
    assert "Venue shortlist sent" in text[line.line - 1] and "Venue shortlist" in line.snippet
    turn = by_source["conversation"]
    assert (turn.chat_id, turn.turn_item_id, turn.date) == ("c1", "t1", "2026-09-30")
    assert turn.id == "i_t1"


def test_recency_lowers_an_old_turn_but_never_to_zero(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    # The old turn says "venue" twice, so it is the better keyword match.
    put_turn(indexer, "c1", "old", _day(400), "Venue venue choice: the old barn by the lake.")
    put_turn(indexer, "c1", "new", _day(1), "Venue choice: the hotel near the station.")

    fresh = recall(indexer, ["venue"], sources={"conversation"})
    assert [h.turn_item_id for h in fresh] == ["new", "old"]
    assert fresh[1].score > 0
    past = recall(indexer, ["venue"], sources={"conversation"}, recency=False)
    assert [h.turn_item_id for h in past] == ["old", "new"]
    assert recency_factor(_day(90), datetime.now(UTC).timestamp()) == pytest.approx(0.5, 0.01)
    assert recency_factor("1900-01-01", datetime.now(UTC).timestamp()) > 0


def test_versions_of_one_thing_collapse_into_the_newest(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path)
    text = "The offsite budget is fifty thousand for the whole team."
    put_turn(indexer, "c1", "t1", "2026-03-03", text)
    put_turn(indexer, "c1", "t2", "2026-09-14", text)
    put_turn(indexer, "c2", "t3", "2026-10-02", text.replace("fifty", "sixty two"))

    hits = recall(indexer, ["offsite budget"], recency=False)

    assert len(hits) == 2
    collapsed = next(h for h in hits if h.turn_item_id == "t2")
    assert (collapsed.date, collapsed.earlier) == ("2026-09-14", ["2026-03-03"])


def test_the_current_epoch_of_a_chat_is_left_out(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path)
    put_turn(indexer, "c1", "t1", "2026-10-01", "Draft the venue email.")
    put_turn(indexer, "c1", "t2", "2026-10-02", "Send the venue email today.")
    put_turn(indexer, "c1", "t3", "2026-10-02", "Venue email was sent to the hotel.")
    put_turn(indexer, "c2", "t4", "2026-10-03", "Venue for the dinner is the rooftop.")

    hits = recall(indexer, ["venue email"], exclude_chat_after={"c1": "t2"}, recency=False)

    assert {h.turn_item_id for h in hits} == {"t1", "t2", "t4"}
    unknown = recall(indexer, ["venue"], exclude_chat_after={"c1": "nope"}, recency=False)
    assert {h.turn_item_id for h in unknown} == {"t1", "t2", "t3", "t4"}


def test_a_checkpoint_anchor_on_a_reply_leaves_the_turns_after_it_out(tmp_path: Path) -> None:
    from omnigent.superchat.knowledge.turns import passage_of, split_turns

    def say(item_id: str, role: str, text: str, at: int) -> dict:
        kind = "input_text" if role == "user" else "output_text"
        return {"id": item_id, "type": "message", "role": role, "created_at": at,
                "response_id": f"r{at}", "content": [{"type": kind, "text": text}]}  # fmt: skip

    day = 1_791_800_000  # one day: only the order of the turns tells them apart
    record = [
        say("u1", "user", "Draft the venue email.", day),
        say("a1", "assistant", "Venue email drafted.", day),  # the checkpoint's anchor
        say("u2", "user", "Send the venue email now.", day + 60),  # the current epoch
        say("a2", "assistant", "Venue email sent.", day + 60),
    ]
    indexer, _ = build_indexer(tmp_path)
    # Indexed earlier by a backfill that still took the current epoch, newest stored first.
    turns = split_turns(record)[::-1]
    indexer.index_turns([passage_of(t, chat_id="c1", main_id="c1", origin="main") for t in turns])
    run_all(indexer)

    hits = recall(indexer, ["venue email"], exclude_chat_after={"c1": "a1"}, recency=False)
    assert [h.turn_item_id for h in hits] == ["u1"]
    whole = recall(indexer, ["venue email"], exclude_chat_after={"c1": ""}, recency=False)
    assert whole == []  # no checkpoint yet: the whole chat is the current epoch


def test_claims_are_fused_by_rank_with_the_other_sources(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/diet.md", "Lunch order notes for the offsite.")
    indexer.scan()
    run_all(indexer)
    asked: list[tuple[str, int]] = []
    vegetarian = {
        "claim_id": "k1",
        "text": "Is vegetarian",
        "kind": "fact",
        "last_confirmed": 1.79e9,
    }
    allergy = {
        "claim_id": "k2",
        "text": "Allergic to nuts",
        "kind": "fact",
        "last_confirmed": 1.7e9,
    }

    async def claims(query: str, limit: int) -> list[dict]:
        asked.append((query, limit))
        return [vegetarian, allergy] if query == "diet" else [vegetarian]

    hits = recall(indexer, ["diet", "lunch"], sources={"memory"}, claims=claims)

    assert [q for q, _ in asked] == ["diet", "lunch"]
    assert [(h.source, h.claim_id, h.id) for h in hits] == [
        ("memory", "k1", "c_k1"),
        ("memory", "k2", "c_k2"),
    ]
    assert hits[0].date == datetime.fromtimestamp(1.79e9, UTC).date().isoformat()
    assert recall(indexer, ["diet"], sources={"memory"}) == []  # no engine: no claims


BUDGET = "projects/spring-launch/launch-budget.xlsx"


def _journal_day(indexer, ws: Path, day: str, text: str, **refs) -> None:
    """One day file of one journal line, its refs in the trailing comment (C5), indexed."""
    pairs = "".join(f" {key}:{','.join(values)}" for key, values in refs.items())
    line = f"- {day} · {text} · main <!-- c1{pairs} -->"
    _put(ws, f"memory/journal/{day}.md", f"# {day}\n\n## Journal\n\n{line}\n")
    indexer.scan()
    run_all(indexer)


def _touching_the_budget(tmp_path: Path):
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "projects/spring-launch/notes.md", "Spring launch notes: venue and budget.")
    indexer.add(ws / "projects/spring-launch/notes.md", explicit=True)  # saved as an artifact
    _put(ws, "your_files/other.md", "Other budget notes for the winter party.")
    _journal_day(indexer, ws, "2026-09-02", "Built the launch budget", files=[BUDGET],
                 project=["spring-launch"])  # fmt: skip
    _journal_day(indexer, ws, "2026-09-30", "Checked the quote against the budget",
                 files=[BUDGET], artifacts=["art_7f2@v3"], project=["spring-launch"])  # fmt: skip
    _journal_day(indexer, ws, "2026-10-05", "Budget for the winter party drafted")
    put_turn(indexer, "c1", "t1", "2026-09-15", "Is the venue line in the budget too high?",
             project="spring-launch", refs={"files": [BUDGET]})  # fmt: skip
    put_turn(indexer, "c2", "t2", "2026-10-06", "Winter party budget is fine.")
    return indexer


def test_a_file_filter_returns_what_touched_it_newest_first(tmp_path: Path) -> None:
    indexer = _touching_the_budget(tmp_path)

    hits = recall(indexer, [], file=BUDGET, sources=SOURCES)

    assert [(h.source, h.date) for h in hits] == [
        ("journal", "2026-09-30"),
        ("conversation", "2026-09-15"),
        ("journal", "2026-09-02"),
    ]
    assert hits[0].refs == {"files": [BUDGET], "artifacts": ["art_7f2@v3"]}
    assert hits[0].project == "spring-launch" and hits[0].id.startswith("ep_")
    with_query = recall(indexer, ["quote"], file=BUDGET, sources=SOURCES)
    assert [h.date for h in with_query] == sorted((h.date for h in with_query), reverse=True)
    assert {h.date for h in with_query} <= {"2026-09-30", "2026-09-15", "2026-09-02"}


def test_project_and_period_filters(tmp_path: Path) -> None:
    indexer = _touching_the_budget(tmp_path)

    project = recall(indexer, ["budget"], project="spring-launch", recency=False, sources=SOURCES)
    assert {(h.source, h.path or h.chat_id) for h in project} == {
        ("files", "projects/spring-launch/notes.md"),
        ("journal", "memory/journal/2026-09-02.md"),
        ("journal", "memory/journal/2026-09-30.md"),
        ("conversation", "c1"),
    }
    late = recall(indexer, ["budget"], project="spring-launch", since="2026-09-14",
                  until="2026-09-30", recency=False, sources=SOURCES)  # fmt: skip
    assert {h.date for h in late} == {"2026-09-15", "2026-09-30"}
    autumn = recall(indexer, ["budget"], since="2026-10-01", recency=False)
    assert {h.date for h in autumn} >= {"2026-10-05", "2026-10-06"}
    assert all(h.date >= "2026-10-01" for h in autumn)
    chat = recall(indexer, ["budget"], chat="c2", recency=False, sources=SOURCES)
    assert [(h.source, h.turn_item_id) for h in chat] == [("conversation", "t2")]


def test_bad_arguments_are_refused(tmp_path: Path) -> None:
    indexer, _ = build_indexer(tmp_path)
    with pytest.raises(ValueError):
        recall(indexer, [])
    with pytest.raises(ValueError):
        recall(indexer, [], since="2026-01-01")  # a period alone lists nothing
    with pytest.raises(ValueError):
        recall(indexer, ["a", "b", "c", "d"])
    with pytest.raises(ValueError):
        recall(indexer, ["a"], sources={"web"})
