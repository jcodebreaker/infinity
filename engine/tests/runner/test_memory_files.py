"""Tests for the ``memory/`` folder: journal lines, ``STATE.md``, rebuild from stored checkpoints.

No server and no model: the items route is a small in-memory fake.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.context.rollover import checkpoint_header
from omnigent.entities import CompactionData
from omnigent.runner.memory import files
from omnigent.runner.memory.sync import (
    rebuild,
    rebuild_if_missing,
    reconcile,
    record_checkpoint,
    record_daily_note,
    record_round,
)

MAIN = files.Origin("main", "conv_main")
STATE_TEXT = "## Working list\n- [t-launch · 2026-10-09] Spring launch budget"
SUMMARY = f"{checkpoint_header('size')}\n\n{STATE_TEXT}"


def _journal(workspace: Path, day: str) -> str:
    return (workspace / "memory" / "journal" / f"{day}.md").read_text(encoding="utf-8")


def _item(
    summary: str = SUMMARY,
    *,
    journal: list[str] | None = None,
    evicted: list[str] | None = None,
) -> CompactionData:
    return CompactionData(
        summary=summary,
        last_item_id="msg_1",
        token_count=1,
        journal_lines=journal,
        evicted_entries=evicted,
    )


# ── lines ────────────────────────────────────────────────────────────────


def test_lines_go_under_the_heading_with_origin_and_chat_id(tmp_path: Path) -> None:
    entries = files.checkpoint_lines(
        MAIN, ["2026-10-09 · Asked for the budget"], None, default_date="2026-10-12"
    )
    assert files.append_lines(tmp_path, entries)

    text = _journal(tmp_path, "2026-10-09")
    assert text == (
        "# 2026-10-09\n\n## Journal\n\n"
        "- 2026-10-09 · Asked for the budget · main <!-- conv_main -->\n"
    )


def test_a_line_without_a_date_gets_the_default_date(tmp_path: Path) -> None:
    entries = files.checkpoint_lines(MAIN, ["Wrote the memo"], None, default_date="2026-10-12")
    files.append_lines(tmp_path, entries)

    assert "- 2026-10-12 · Wrote the memo · main <!-- conv_main -->" in _journal(
        tmp_path, "2026-10-12"
    )


def test_a_line_cannot_end_the_trailing_comment() -> None:
    line = files.format_line("2026-10-12", "a --> b\nc", MAIN)[1]

    assert line.count("-->") == 1
    assert "\n" not in line


def test_evicted_entries_are_kept_word_for_word_and_marked() -> None:
    entry = "- [t-old · 2026-08-01] Old plan: keep the 4 sites"
    lines = files.checkpoint_lines(MAIN, None, [entry], default_date="2026-10-12")

    assert lines == [
        (
            "2026-10-12",
            "- 2026-10-12 · Moved out of the working list: "
            "[t-old · 2026-08-01] Old plan: keep the 4 sites · main <!-- conv_main -->",
        )
    ]


def test_two_appends_in_a_row_keep_both(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "first", MAIN)])
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "second", MAIN)])

    text = _journal(tmp_path, "2026-10-12")
    assert text.index("first") < text.index("second")
    assert text.count("## Journal") == 1


def test_concurrent_appends_keep_every_line(tmp_path: Path) -> None:
    def write(n: int) -> None:
        files.append_lines(tmp_path, [files.format_line("2026-10-12", f"line {n}", MAIN)])

    threads = [threading.Thread(target=write, args=(n,)) for n in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    text = _journal(tmp_path, "2026-10-12")
    assert text.count("## Journal") == 1
    assert sum(f"line {n} ·" in text for n in range(30)) == 30


def test_a_later_section_stays_below_new_lines(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "first", MAIN)])
    path = tmp_path / "memory" / "journal" / "2026-10-12.md"
    path.write_text(path.read_text() + "\n## Notes\n\nwritten by another section\n")

    files.append_lines(tmp_path, [files.format_line("2026-10-12", "second", MAIN)])

    text = path.read_text()
    assert text.index("first") < text.index("second") < text.index("## Notes")
    assert text.endswith("written by another section\n")


def test_a_failing_write_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "memory"
    blocker.write_text("a file where the folder should be")

    assert files.append_lines(tmp_path, [files.format_line("2026-10-12", "x", MAIN)]) is False
    assert files.write_state(tmp_path, "state") is False


ROUND_ENTRY: dict[str, Any] = {
    "round_id": "msg_r1",
    "date": "2026-10-12",
    "ask": "Build the Q4 forecast from the sales CSV",
    "outcome": "The forecast is in q4-forecast.xlsx with three scenarios.",
    "stopped": False,
    "files": ["q4-forecast.xlsx"],
    "helpers": ["Q4 forecast"],
    "folder": "projects/q3",
    "seconds": 134.4,
    "refs": {"artifacts": ["a1@v2"], "project": ["q3"], "helpers": ["conv_h1"]},
}


@pytest.mark.parametrize(
    ("entry", "line"),
    [
        (
            ROUND_ENTRY,
            '- 2026-10-12 · Asked "Build the Q4 forecast from the sales CSV" · The forecast is '
            "in q4-forecast.xlsx with three scenarios. · Files: q4-forecast.xlsx · Helpers: "
            '"Q4 forecast" · Folder: projects/q3 · Took 2m 14s · main <!-- conv_main '
            "round:msg_r1 artifacts:a1@v2 project:q3 helpers:conv_h1 -->",
        ),
        (
            {"round_id": "msg_r2", "date": "2026-10-12", "ask": "Stop that", "stopped": True},
            '- 2026-10-12 · Asked "Stop that" · Stopped · Took 0s · main '
            "<!-- conv_main round:msg_r2 -->",
        ),
        (
            {"round_id": "msg_r3", "date": "2026-10-12", "ask": "A deck", "checked": True},
            '- 2026-10-12 · Asked "A deck" · Done · Checked · Took 0s · main '
            "<!-- conv_main round:msg_r3 -->",
        ),
        (
            {"round_id": "msg_r4", "date": "2026-10-12", "ask": "A deck", "checked": False},
            '- 2026-10-12 · Asked "A deck" · Done · Not checked · Took 0s · main '
            "<!-- conv_main round:msg_r4 -->",
        ),
    ],
    ids=["full", "stopped", "deck checked", "deck not checked"],
)
async def test_a_settled_round_writes_one_entry_once(
    tmp_path: Path, entry: dict[str, Any], line: str
) -> None:
    """The fixed template; a second settlement of the same Round adds nothing."""
    assert await record_round("conv_main", entry, workspace=tmp_path) is True
    assert await record_round("conv_main", {**entry, "outcome": "x"}, workspace=tmp_path) is False
    assert await record_round("conv_main", {**entry, "date": "../x"}, workspace=tmp_path) is False

    lines = [ln for ln in _journal(tmp_path, "2026-10-12").splitlines() if ln.startswith("- ")]
    assert lines == [line]


REFS = {"files": ["projects/spring-launch/budget.xlsx"], "artifacts": ["a1@v3"]}


def test_journal_lines_carry_the_refs_after_the_chat_id() -> None:
    lines = files.checkpoint_lines(
        MAIN,
        ["2026-10-12 · Checked the quote", "2026-10-12 · Saved the budget"],
        ["- [t-old · 2026-08-01] Old plan"],
        default_date="2026-10-12",
        refs=REFS,
    )

    suffix = " · main <!-- conv_main files:projects/spring-launch/budget.xlsx artifacts:a1@v3 -->"
    assert lines[0][1] == "- 2026-10-12 · Checked the quote" + suffix
    assert lines[1][1].endswith(suffix)
    # An evicted entry is the state file's text, not this window's work.
    assert lines[2][1].endswith("· main <!-- conv_main -->")


def test_refs_cannot_end_the_trailing_comment() -> None:
    line = files.format_line("2026-10-12", "x", MAIN, {"files": ["a-->b c.md"]})[1]

    assert line.endswith("<!-- conv_main files:a--%3Eb%20c.md -->")
    assert line.count("-->") == 1


def test_an_old_checkpoint_without_refs_still_loads() -> None:
    item = CompactionData.model_validate(
        {"summary": SUMMARY, "last_item_id": "m", "token_count": 1, "journal_lines": ["x"]}
    )

    assert item.journal_refs is None


# ── STATE.md ─────────────────────────────────────────────────────────────


def test_state_file_is_the_state_text_under_a_note(tmp_path: Path) -> None:
    assert files.write_state(tmp_path, files.state_text_of(SUMMARY))

    text = (tmp_path / "memory" / "STATE.md").read_text(encoding="utf-8")
    assert text == f"{files.STATE_NOTE}\n\n{STATE_TEXT}\n"
    assert "Context checkpoint" not in text


# ── a stored checkpoint ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_main_checkpoint_writes_journal_and_state(tmp_path: Path) -> None:
    item = _item(journal=["2026-10-12 · Planned the launch"], evicted=["- [t-x · 2026-07-01] old"])

    await record_checkpoint("conv_main", {}, item, workspace=tmp_path)

    text = _journal(tmp_path, "2026-10-12")
    assert "Planned the launch · main <!-- conv_main -->" in text
    assert "Moved out of the working list: [t-x · 2026-07-01] old" in _journal(
        tmp_path, files.today()
    )
    assert STATE_TEXT in (tmp_path / "memory" / "STATE.md").read_text()


@pytest.mark.asyncio
async def test_a_helper_writes_nothing(tmp_path: Path) -> None:
    labels = {"omnigent.context.mode": "superside-chat", "omnigent.subagent": "1"}

    await record_checkpoint(
        "conv_h", labels, _item(journal=["2026-10-12 · x"]), workspace=tmp_path
    )

    assert not (tmp_path / "memory").exists()


# ── rebuild ──────────────────────────────────────────────────────────────

DAY = 1_791_800_000  # 2026-10-12 UTC


def _stored(item_id: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "compaction",
        "created_at": DAY,
        "summary": summary,
        "response_id": f"compact_{item_id}",
        **extra,
    }


class _Chats:
    """The routes a rebuild reads: snapshot and compaction items."""

    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self._items = items

    async def get(self, url: str, **kwargs: Any) -> Any:
        body: dict[str, Any] = {}
        if url.endswith("/items"):
            chat = url.split("/")[3]
            rows = self._items.get(chat, [])
            after = (kwargs.get("params") or {}).get("after")
            if after:
                ids = [r["id"] for r in rows]
                rows = rows[ids.index(after) + 1 :]
            body = {"data": rows[:1], "has_more": len(rows) > 1}
        else:
            body = {"title": "t", "labels": {}}

        class _Resp:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return body

        return _Resp()


def _chats() -> _Chats:
    return _Chats(
        {
            "conv_main": [
                _stored("c1", SUMMARY, journal_lines=["2026-10-11 · Asked for a budget"]),
                _stored(
                    "c2",
                    SUMMARY,
                    journal_lines=["2026-10-12 · Booked the venue"],
                    evicted_entries=["- [t-old · 2026-07-01] Old plan"],
                ),
            ],
        }
    )


@pytest.mark.asyncio
async def test_rebuild_regenerates_the_files_from_stored_checkpoints(tmp_path: Path) -> None:
    await rebuild(_chats(), "conv_main", workspace=tmp_path)

    first = _journal(tmp_path, "2026-10-11")
    second = _journal(tmp_path, "2026-10-12")
    assert "Asked for a budget · main <!-- conv_main -->" in first
    assert "Booked the venue · main" in second
    assert "Moved out of the working list: [t-old · 2026-07-01] Old plan" in second
    assert STATE_TEXT in (tmp_path / "memory" / "STATE.md").read_text()


@pytest.mark.asyncio
async def test_rebuild_twice_gives_the_same_files(tmp_path: Path) -> None:
    await rebuild(_chats(), "conv_main", workspace=tmp_path)
    before = {p.name: p.read_text() for p in (tmp_path / "memory").rglob("*.md")}

    await rebuild(_chats(), "conv_main", workspace=tmp_path)

    after = {p.name: p.read_text() for p in (tmp_path / "memory").rglob("*.md")}
    assert after == before


@pytest.mark.asyncio
async def test_rebuild_writes_the_refs_exactly_as_the_checkpoint_did(tmp_path: Path) -> None:
    item = CompactionData(
        summary=SUMMARY,
        last_item_id="msg_1",
        token_count=1,
        journal_lines=["2026-10-12 · Checked the quote"],
        journal_refs=REFS,
    )
    live, rebuilt = tmp_path / "live", tmp_path / "rebuilt"
    await record_checkpoint("conv_main", {}, item, workspace=live)
    stored = _stored("c1", SUMMARY, **item.model_dump(exclude={"summary"}))

    await rebuild(_Chats({"conv_main": [stored]}), "conv_main", workspace=rebuilt)

    assert "files:projects/spring-launch/budget.xlsx artifacts:a1@v3 -->" in _journal(
        live, "2026-10-12"
    )
    assert _journal(rebuilt, "2026-10-12") == _journal(live, "2026-10-12")


@pytest.mark.asyncio
async def test_rebuild_keeps_lines_that_exist_only_in_the_files(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "Helper line", MAIN)])

    await rebuild(_chats(), "conv_main", workspace=tmp_path)

    assert "Helper line" in _journal(tmp_path, "2026-10-12")


@pytest.mark.asyncio
async def test_a_failed_read_leaves_no_folder_so_a_later_turn_retries(tmp_path: Path) -> None:
    class _Down:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("server down")

    await rebuild(_Down(), "conv_main", workspace=tmp_path)

    assert not (tmp_path / "memory").exists()


@pytest.mark.asyncio
async def test_rebuild_if_missing_runs_only_without_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERCHAT_KNOWLEDGE_ROOT", str(tmp_path))
    await rebuild_if_missing(_chats(), "conv_main")
    assert (tmp_path / "memory" / "STATE.md").exists()

    (tmp_path / "memory" / "STATE.md").unlink()
    await rebuild_if_missing(_chats(), "conv_main")
    assert not (tmp_path / "memory" / "STATE.md").exists()


# ── daily note sections in the day file ──────────────────────────────────

NOTE = {
    "talked_about": "Launch budget",
    "decisions": "Book the venue",
    "promised": "",
    "open_loops": "Pick a caterer",
    "reflection": "A calm day.",
}


def _day(workspace: Path) -> Path:
    return workspace / "memory" / "journal" / "2026-10-12.md"


def test_a_note_projects_notes_and_reflection_and_leaves_the_journal_alone(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "Planned", MAIN)])
    before = [ln for ln in _day(tmp_path).read_text().splitlines() if ln.startswith("- ")]

    assert files.write_day_sections(tmp_path, "2026-10-12", NOTE)

    text = _day(tmp_path).read_text()
    assert [ln for ln in text.splitlines() if ln.startswith("- ")] == before
    assert "## Notes\n\n### What we talked about\nLaunch budget" in text
    assert "### Decisions\nBook the venue" in text
    assert "### Promised" not in text
    assert "## Reflection\n\nA calm day." in text
    assert text.index("## Journal") < text.index("## Notes") < text.index("## Reflection")


def test_a_second_projection_replaces_the_sections(tmp_path: Path) -> None:
    files.write_day_sections(tmp_path, "2026-10-12", NOTE)

    files.write_day_sections(tmp_path, "2026-10-12", {**NOTE, "decisions": "Skip the venue"})

    text = _day(tmp_path).read_text()
    assert text.count("## Notes") == 1
    assert text.count("## Reflection") == 1
    assert "Skip the venue" in text
    assert "Book the venue" not in text


def test_journal_appends_after_a_projection_stay_in_the_journal(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "first", MAIN)])
    files.write_day_sections(tmp_path, "2026-10-12", NOTE)

    files.append_lines(tmp_path, [files.format_line("2026-10-12", "second", MAIN)])

    text = _day(tmp_path).read_text()
    assert text.index("first") < text.index("second") < text.index("## Notes")


def test_a_projection_into_a_new_day_still_leaves_room_for_the_journal(tmp_path: Path) -> None:
    files.write_day_sections(tmp_path, "2026-10-12", NOTE)

    files.append_lines(tmp_path, [files.format_line("2026-10-12", "late", MAIN)])

    text = _day(tmp_path).read_text()
    assert text.index("## Journal") < text.index("late") < text.index("## Notes")


def test_an_empty_note_writes_no_section(tmp_path: Path) -> None:
    empty = dict.fromkeys(NOTE, "")
    files.write_day_sections(tmp_path, "2026-10-12", empty)
    assert not _day(tmp_path).exists()

    files.append_lines(tmp_path, [files.format_line("2026-10-12", "line", MAIN)])
    files.write_day_sections(tmp_path, "2026-10-12", empty)
    text = _day(tmp_path).read_text()
    assert "## Notes" not in text
    assert "## Reflection" not in text


def test_note_text_cannot_open_a_section(tmp_path: Path) -> None:
    files.write_day_sections(tmp_path, "2026-10-12", {"decisions": "## Journal\n- fake"})

    lines = _day(tmp_path).read_text().splitlines()
    assert lines.count("## Journal") == 1
    assert "\\## Journal" in lines


def test_a_bad_date_writes_nothing(tmp_path: Path) -> None:
    assert not files.write_day_sections(tmp_path, "../x", NOTE)
    assert not (tmp_path / "memory").exists()


@pytest.mark.asyncio
async def test_record_daily_note_projects_the_response_body(tmp_path: Path) -> None:
    await record_daily_note({"date": "2026-10-12", "sections": NOTE}, workspace=tmp_path)
    await record_daily_note({"nonsense": 1}, workspace=tmp_path)

    assert "A calm day." in _day(tmp_path).read_text()


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class _NoteServer:
    """Answers the caller lookup and the daily note write."""

    def __init__(self, status: int = 200) -> None:
        self._status = status

    async def get(self, url: str, **_kwargs: Any) -> Any:
        return _Resp(200, {"kind": "agent", "parent_session_id": None})

    async def post(self, url: str, **_kwargs: Any) -> Any:
        return _Resp(self._status, {"date": "2026-10-12", "sections": NOTE})


async def _update(server: Any) -> str:
    from omnigent.superchat.daily_notes.handlers import handle_daily_note_tool
    from omnigent.superchat.feature import HandlerCtx

    ctx = HandlerCtx("daily_note_update", server, "conv_main")
    return await handle_daily_note_tool(ctx, {"decisions": "Book the venue"})


@pytest.mark.asyncio
async def test_the_update_tool_projects_into_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERCHAT_KNOWLEDGE_ROOT", str(tmp_path))

    out = await _update(_NoteServer())

    assert '"date": "2026-10-12"' in out
    assert "Book the venue" in _day(tmp_path).read_text()


@pytest.mark.asyncio
async def test_a_failing_projection_does_not_fail_the_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERCHAT_KNOWLEDGE_ROOT", str(tmp_path))

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("disk")

    monkeypatch.setattr(files, "write_day_sections", boom)

    out = await _update(_NoteServer())

    assert '"date": "2026-10-12"' in out


@pytest.mark.asyncio
async def test_a_refused_write_projects_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPERCHAT_KNOWLEDGE_ROOT", str(tmp_path))

    out = await _update(_NoteServer(status=403))

    assert "error" in out
    assert not (tmp_path / "memory").exists()


# ── reset, owner ─────────────────────────────────────────────────────────


def _reset(item_id: str = "c_reset", created_at: int = DAY) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "compaction",
        "created_at": created_at,
        "summary": "reset",
        "response_id": f"reset_{item_id}",
    }


class _Convo:
    """Snapshot, newest-first compaction items and notes for a few chats."""

    def __init__(
        self,
        items: dict[str, list[dict[str, Any]]],
        labels: dict[str, dict[str, str]] | None = None,
        notes: list[dict[str, Any]] | None = None,
    ) -> None:
        self._items = items
        self._labels = labels or {}
        self._notes = notes or []
        #: Chats the server answers 404 for (deleted).
        self.deleted: set[str] = set()

    async def get(self, url: str, **kwargs: Any) -> Any:
        params = kwargs.get("params") or {}
        body: dict[str, Any]
        if url == "/v1/me/daily-notes":
            body = {"daily_notes": self._notes}
        elif url.endswith("/items"):
            rows = list(self._items.get(url.split("/")[3], []))
            if params.get("order") == "desc":
                rows.reverse()
            elif params.get("after"):
                ids = [r["id"] for r in rows]
                rows = rows[ids.index(params["after"]) + 1 :]
            body = {
                "data": rows[: int(params["limit"])]
                if params.get("order") == "desc"
                else rows[:1],
                "has_more": params.get("order") != "desc" and len(rows) > 1,
            }
        else:
            if url.split("/")[3] in self.deleted:
                return _Resp(404, {})
            body = {"title": "t", "labels": self._labels.get(url.split("/")[3], {})}
        return _Resp(200, body)


def _lines(workspace: Path) -> list[str]:
    return [ln for ln in _journal(workspace, "2026-10-12").splitlines() if ln.startswith("- ")]


def _tree(workspace: Path) -> dict[str, str]:
    return {
        str(p.relative_to(workspace)): p.read_text()
        for p in sorted((workspace / "memory").rglob("*"))
        if p.is_file() and p.name != ".lock"
    }


@pytest.mark.asyncio
async def test_a_reset_appends_one_line_and_empties_the_state_once(tmp_path: Path) -> None:
    chats = _Convo({"conv_main": [_stored("c1", SUMMARY), _reset()]})
    await rebuild(_Convo({"conv_main": [_stored("c1", SUMMARY)]}), "conv_main", workspace=tmp_path)
    assert STATE_TEXT in (tmp_path / "memory" / "STATE.md").read_text()

    await reconcile(chats, "conv_main", {}, workspace=tmp_path)
    await reconcile(chats, "conv_main", {}, workspace=tmp_path)

    assert [ln for ln in _lines(tmp_path) if "conversation reset" in ln] == [
        "- 2026-10-12 · conversation reset · main <!-- conv_main -->"
    ]
    state = (tmp_path / "memory" / "STATE.md").read_text()
    assert state == f"{files.STATE_NOTE}\n\nThe conversation was reset on 2026-10-12.\n"
    assert (tmp_path / "memory" / ".last_reset").read_text().strip() == "c_reset"


@pytest.mark.asyncio
async def test_a_second_reset_is_written_again_and_a_checkpoint_is_not_a_reset(
    tmp_path: Path,
) -> None:
    items = {"conv_main": [_stored("c1", SUMMARY), _reset("r1")]}
    await rebuild(_Convo({"conv_main": [_stored("c1", SUMMARY)]}), "conv_main", workspace=tmp_path)
    await reconcile(_Convo(items), "conv_main", {}, workspace=tmp_path)
    items["conv_main"] += [_stored("c2", SUMMARY), _reset("r2", DAY + 86_400)]
    await reconcile(_Convo(items), "conv_main", {}, workspace=tmp_path)
    await reconcile(
        _Convo({"conv_main": [_stored("c3", SUMMARY)]}), "conv_main", {}, workspace=tmp_path
    )

    resets = [
        ln for d in ("2026-10-12", "2026-10-13") for ln in _journal(tmp_path, d).splitlines()
    ]
    assert sum("conversation reset" in ln for ln in resets) == 2
    assert files.last_reset_of(tmp_path) == "r2"


@pytest.mark.asyncio
async def test_rebuild_after_a_reset_gives_the_same_files(tmp_path: Path) -> None:
    items = {
        "conv_main": [
            _stored("c1", SUMMARY, journal_lines=["2026-10-12 · Booked the venue"]),
            _reset(),
        ]
    }
    live, rebuilt = tmp_path / "live", tmp_path / "rebuilt"
    await rebuild(_Convo({"conv_main": items["conv_main"][:1]}), "conv_main", workspace=live)
    await reconcile(_Convo(items), "conv_main", {}, workspace=live)

    await rebuild(_Convo(items), "conv_main", workspace=rebuilt)

    assert _tree(rebuilt) == _tree(live)


@pytest.mark.asyncio
async def test_rebuild_covers_the_stored_daily_notes(tmp_path: Path) -> None:
    chats = _Convo(
        {"conv_main": [_stored("c1", SUMMARY)]},
        notes=[{"date": "2026-10-12", "sections": NOTE}, {"date": "2026-10-11", "sections": {}}],
    )

    await rebuild(chats, "conv_main", workspace=tmp_path)

    assert "Pick a caterer" in _day(tmp_path).read_text()
    assert not (tmp_path / "memory" / "journal" / "2026-10-11.md").exists()


@pytest.mark.asyncio
async def test_a_different_main_conversation_wipes_and_rebuilds(tmp_path: Path) -> None:
    await rebuild(_Convo({"conv_main": [_stored("c1", SUMMARY)]}), "conv_main", workspace=tmp_path)
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "old", MAIN)])
    new = _Convo({"conv_new": [_stored("n1", SUMMARY, journal_lines=["2026-10-12 · New start"])]})

    # While the old Conversation still exists, a second one leaves its folder alone.
    await reconcile(new, "conv_new", {}, workspace=tmp_path)
    assert files.owner_of(tmp_path) == "conv_main"
    assert "old" in _journal(tmp_path, "2026-10-12")

    new.deleted.add("conv_main")
    await reconcile(new, "conv_new", {}, workspace=tmp_path)

    assert files.owner_of(tmp_path) == "conv_new"
    assert "old" not in _journal(tmp_path, "2026-10-12")
    assert "New start" in _journal(tmp_path, "2026-10-12")


@pytest.mark.asyncio
async def test_a_folder_without_a_marker_is_adopted(tmp_path: Path) -> None:
    files.append_lines(tmp_path, [files.format_line("2026-10-12", "kept", MAIN)])

    await reconcile(_Convo({"conv_main": []}), "conv_main", {}, workspace=tmp_path)

    assert files.owner_of(tmp_path) == "conv_main"
    assert "kept" in _journal(tmp_path, "2026-10-12")


@pytest.mark.asyncio
async def test_a_helper_touches_nothing(tmp_path: Path) -> None:
    await rebuild(_Convo({"conv_main": [_stored("c1", SUMMARY)]}), "conv_main", workspace=tmp_path)
    before = _tree(tmp_path)
    helper = {"omnigent.context.mode": "superside-chat", "omnigent.subagent": "1"}
    chats = _Convo({"conv_other": [_stored("x", SUMMARY)]})

    await reconcile(chats, "conv_h", helper, workspace=tmp_path)

    assert _tree(tmp_path) == before
    assert files.owner_of(tmp_path) == "conv_main"


@pytest.mark.asyncio
async def test_a_checkpoint_marks_a_new_folder_with_its_main_conversation(tmp_path: Path) -> None:
    await record_checkpoint("conv_main", {}, _item(), workspace=tmp_path)

    assert files.owner_of(tmp_path) == "conv_main"


# ── the index's conversation source ──────────────────────────────────────


def _say(item_id: str, role: str, text: str, rid: str, **extra: Any) -> dict[str, Any]:
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


def _turns(prefix: str, count: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n in range(count):
        rows += [
            _say(f"{prefix}u{n}", "user", f"question {n} about the venue", f"{prefix}r{n}"),
            _say(f"{prefix}a{n}", "assistant", f"answer {n}", f"{prefix}r{n}"),
        ]
    return rows


class _Record:
    """The routes the conversation source reads: snapshot and items (both orders)."""

    def __init__(self, items: dict[str, list[dict[str, Any]]]) -> None:
        self.items = items
        self.deleted: set[str] = set()
        self.reads: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        params = dict(kwargs.get("params") or {})
        chat = url.split("/")[3]
        if url.endswith("/items"):
            self.reads.append((chat, params))
            rows = [r for r in self.items.get(chat, []) if params.get("type") in (None, r["type"])]
            if params.get("order") == "desc":
                rows = rows[::-1]
            if params.get("after"):
                ids = [r["id"] for r in rows]
                if params["after"] not in ids:
                    return _Resp(400, {"error": "stale_cursor"})
                rows = rows[ids.index(params["after"]) + 1 :]
            limit = int(params.get("limit", 100))
            return _Resp(200, {"data": rows[:limit], "has_more": len(rows) > limit})
        if chat in self.deleted:
            return _Resp(404, {})
        return _Resp(200, {"title": "t", "labels": {}})

    async def post(self, url: str, **kwargs: Any) -> Any:
        """A stored compaction joins the record (the rollover's ``POST .../events``)."""
        rows = self.items.setdefault(url.split("/")[3], [])
        data = dict(kwargs["json"]["data"])
        rows.append({**data, "id": f"cmp{len(rows)}", "created_at": DAY})
        return _Resp(200, {})


def _service(tmp_path: Path) -> Any:
    from omnigent.superchat.knowledge.runtime import KnowledgeRuntime
    from tests.superchat.knowledge._fixtures import FakeEmbedder

    return KnowledgeRuntime(tmp_path / "index", tmp_path / "workspace", FakeEmbedder())


def _passages(service: Any) -> dict[str, dict[str, str]]:
    db = service.indexer.db
    found = db.search("venue question about", limit=25, turns=True)
    return {m["turn_item_id"]: m for m in db.turn_meta([c.id for c, _ in found]).values()}


@pytest.mark.asyncio
async def test_retired_turns_become_one_passage_each_with_their_metadata(tmp_path: Path) -> None:
    from omnigent.runner.memory.sync import index_retired_turns

    service = _service(tmp_path)
    window = [*_turns("m", 2), _say("n1", "user", "[System: x]", "rn")]
    record = _Record({})

    await index_retired_turns(record, "conv_main", {}, window, project="spring", runtime=service)

    found = _passages(service)
    assert sorted(found) == ["mu0", "mu1"]  # a notice is never one
    assert found["mu1"] == {
        "chat_id": "conv_main", "turn_item_id": "mu1", "date": "2026-10-12", "origin": "main",
        "project": "spring", "person_text": "question 1 about the venue",
    }  # fmt: skip
    assert record.reads == []  # the window is in hand: nothing is read
    assert service.indexer.process_next()  # one indexer tick embeds them
    assert service.indexer.db.files(turns=True)[0].embed_status == "embedded"


@pytest.mark.asyncio
async def test_a_turn_in_the_current_epoch_is_indexed_by_the_compaction_that_retires_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.memory import sync
    from omnigent.superchat import rollover

    async def summarize(items: list[dict[str, Any]], **_: Any) -> CompactionData:
        return CompactionData(summary=SUMMARY, last_item_id=items[-1]["id"], token_count=1)

    monkeypatch.setattr(rollover, "build_rollover_item", summarize)
    service = _service(tmp_path)
    record = _Record({"conv_main": [_say("mu0", "user", "question 0 about the venue", "mr0")]})

    async def on_stored(_item: CompactionData, window: list[dict[str, Any]]) -> None:
        await sync.index_retired_turns(record, "conv_main", {}, window, runtime=service)

    async def compact() -> None:
        assert await rollover.roll_over_session(
            "conv_main",
            labels={"omnigent.context.rollover_keep_tokens": "1"},  # only the newest turn is kept
            model="m",
            server_client=record,
            on_stored=on_stored,
        )

    await sync.backfill_turns(record, "conv_main", {}, runtime=service)  # a new Conversation
    record.items["conv_main"] += [_say("ma0", "assistant", "answer 0", "mr0"), *_turns("m", 2)[2:]]
    await sync.backfill_turns(record, "conv_main", {}, runtime=service)
    assert _passages(service) == {}  # still in the window: not a passage yet

    await compact()  # the newest turn stays after the anchor
    assert sorted(_passages(service)) == ["mu0"]

    record.items["conv_main"] += _turns("n", 1)
    assert sorted(_passages(service)) == ["mu0"]
    await compact()  # the next window only: the turns before its anchor are not read again
    assert sorted(_passages(service)) == ["mu0", "mu1"]


@pytest.mark.asyncio
async def test_a_helper_turn_is_not_indexed_and_a_failing_write_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.context.labels import (
        CONTEXT_MODE_LABEL,
        SUBAGENT_LABEL_KEY,
        SUPERSIDE_CHAT_MODE_VALUE,
    )
    from omnigent.runner.memory.sync import backfill_turns, index_retired_turns

    service = _service(tmp_path)
    record = _Record({"conv_h": _turns("h", 2), "conv_main": _turns("m", 2)})
    helper = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE, SUBAGENT_LABEL_KEY: "1"}
    await index_retired_turns(record, "conv_h", helper, _turns("h", 2), runtime=service)
    await backfill_turns(record, "conv_h", helper, runtime=service)
    assert _passages(service) == {} and record.reads == []

    def broken(*_args: Any, **_kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(service.indexer, "index_turns", broken)
    await index_retired_turns(record, "conv_main", {}, _turns("m", 2), runtime=service)
    await backfill_turns(record, "conv_main", {}, runtime=service)
    assert _passages(service) == {}


@pytest.mark.asyncio
async def test_outside_a_host_nothing_is_indexed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.memory.sync import backfill_turns, index_retired_turns

    monkeypatch.delenv("IS_SANDBOX", raising=False)
    record = _Record({"conv_main": _turns("m", 2)})
    await index_retired_turns(record, "conv_main", {}, _turns("m", 2))
    await backfill_turns(record, "conv_main", {})
    assert record.reads == []


@pytest.mark.asyncio
async def test_backfill_is_bounded_resumes_and_never_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.memory import sync

    monkeypatch.setattr(sync, "_BACKFILL_PAGE", "10")
    monkeypatch.setattr(sync, "_BACKFILL_PAGES", 2)
    monkeypatch.setattr(sync, "_BACKFILL_TURNS", 6)
    service = _service(tmp_path)
    main = _turns("m", 12)
    main.insert(23, _stored("c1", SUMMARY, last_item_id="ma10"))  # mu11 ran while it summarized
    record = _Record({"conv_main": main})

    await sync.backfill_turns(record, "conv_main", {}, runtime=service)
    assert sorted(_passages(service)) == sorted(f"mu{n}" for n in range(6))  # six turns a run

    restarted = _service(tmp_path)  # a restart: the progress is in the index file
    for _ in range(4):
        await sync.backfill_turns(record, "conv_main", {}, runtime=restarted)
    found = _passages(restarted)
    # Every retired turn once; the current epoch waits for the compaction that retires it.
    assert sorted(found) == sorted(f"mu{n}" for n in range(11))
    db = restarted.indexer.db
    assert sum(len(db.chunks_for(r.file_id)) for r in db.files(turns=True)) == 11
    record.reads.clear()
    await sync.backfill_turns(record, "conv_main", {}, runtime=restarted)
    # At the anchor: only the checkpoint is read, and nothing is written twice.
    assert {params.get("type") for _chat, params in record.reads} == {"compaction"}
    assert sum(len(db.chunks_for(r.file_id)) for r in db.files(turns=True)) == 11


@pytest.mark.asyncio
async def test_backfill_starts_over_when_its_cursor_item_is_gone(tmp_path: Path) -> None:
    from omnigent.runner.memory import sync

    service = _service(tmp_path)
    service.indexer.db.set_meta("turns_cursor.v3:conv_main", "gone")
    record = _Record({"conv_main": [*_turns("m", 3), _stored("c1", SUMMARY, last_item_id="ma1")]})
    await sync.backfill_turns(record, "conv_main", {}, runtime=service)
    await sync.backfill_turns(record, "conv_main", {}, runtime=service)
    assert sorted(_passages(service)) == ["mu0", "mu1"]


@pytest.mark.asyncio
async def test_backfill_indexes_every_retired_turn_and_none_of_the_current_epoch(
    tmp_path: Path,
) -> None:
    from omnigent.runner.memory import sync

    service = _service(tmp_path)
    # The record's newest turn is after its anchor: the current epoch.
    main = _turns("m", 3)
    main.insert(4, _stored("c1", SUMMARY, last_item_id="ma1"))
    record = _Record({"conv_main": main})

    await sync.backfill_turns(record, "conv_main", {}, runtime=service)

    assert sorted(_passages(service)) == ["mu0", "mu1"]
    assert service.indexer.db.meta("turns_cursor.v3:conv_main") == "ma1"  # read up to the anchor


@pytest.mark.asyncio
async def test_a_deleted_conversation_takes_its_passages_with_it(
    tmp_path: Path,
) -> None:
    from omnigent.runner.memory.sync import index_retired_turns

    service = _service(tmp_path)
    record = _Record(
        {
            "conv_main": [_stored("c1", SUMMARY), *_turns("m", 2)],
            "conv_new": [_stored("n1", SUMMARY), *_turns("n", 2)],
        }
    )
    await rebuild(record, "conv_main", workspace=tmp_path)
    await index_retired_turns(record, "conv_main", {}, _turns("m", 2), runtime=service)
    await index_retired_turns(record, "conv_new", {}, _turns("n", 2), runtime=service)

    await reconcile(record, "conv_new", {}, workspace=tmp_path, runtime=service)
    assert len(_passages(service)) == 4  # the old Conversation still exists: nothing goes

    record.deleted.add("conv_main")
    await reconcile(record, "conv_new", {}, workspace=tmp_path, runtime=service)

    assert files.owner_of(tmp_path) == "conv_new"
    assert sorted(_passages(service)) == ["nu0", "nu1"]
    assert [r.name for r in service.indexer.db.files(turns=True)] == ["conv_new"]


@pytest.mark.asyncio
async def test_a_reset_keeps_the_passages(tmp_path: Path) -> None:
    from omnigent.runner.memory.sync import index_retired_turns

    service = _service(tmp_path)
    record = _Record({"conv_main": [_stored("c1", SUMMARY), *_turns("m", 2)]})
    await rebuild(record, "conv_main", workspace=tmp_path)
    await index_retired_turns(record, "conv_main", {}, _turns("m", 2), runtime=service)
    record.items["conv_main"].append(_reset())

    await reconcile(record, "conv_main", {}, workspace=tmp_path, runtime=service)

    assert files.last_reset_of(tmp_path) == "c_reset"
    assert sorted(_passages(service)) == ["mu0", "mu1"]


# ── hand edits ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delivered_hand_edit_becomes_one_journal_line_with_its_version(
    _isolated_knowledge_workspace: Path,
) -> None:
    from omnigent.superchat.artifacts.writeback import deliver_manual_edits
    from omnigent.superchat.knowledge.refs import line_refs

    edit = {"id": "art_7f2", "name": "budget.xlsx", "version": 3, "origin": "manual",
            "edit_summary": "B4 1200 → 1500", "created_at": DAY, "write_back": False}  # fmt: skip
    refresh = {**edit, "id": "art_9", "name": "brief.md", "origin": "update"}

    class _Edits:
        posted: list[str] = []

        async def get(self, url: str, **_kwargs: Any) -> Any:
            if url.startswith("/v1/artifacts/pending-edits"):
                return _Resp(200, {"edits": [edit, refresh]})
            return _Resp(200, {"title": "t", "labels": {}})

        async def post(self, url: str, **_kwargs: Any) -> Any:
            self.posted.append(url)
            return _Resp(200, {})

    server = _Edits()
    notes = await deliver_manual_edits(server, "conv_main", roots=[])  # type: ignore[arg-type]

    assert len(notes) == 2 and len(server.posted) == 2
    (line,) = _lines(_isolated_knowledge_workspace)  # a live update's refresh is not the person's
    assert line == (
        "- 2026-10-12 · The person edited budget.xlsx by hand (v3): B4 1200 → 1500 · main "
        "<!-- conv_main artifacts:art_7f2@v3 -->"
    )
    assert line_refs(line) == {"artifacts": ["art_7f2@v3"]}
