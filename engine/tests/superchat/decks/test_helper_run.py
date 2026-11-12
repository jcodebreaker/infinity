"""The deck helper as the runner drives it: cancel kills it, each tool logs time and browser."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from omnigent.superchat.decks import kit
from omnigent.superchat.decks.handlers import HELPER_ENV, handle_deck_tool, run_helper
from omnigent.superchat.feature import HandlerCtx

SAMPLE = (kit.KIT_DIR / "sample-slides.html").read_text("utf-8")


def _script(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\nimport os, sys, time\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(root))
    return root


async def test_a_cancelled_tool_kills_its_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "pid"
    helper = _script(
        tmp_path / "sleepy-helper",
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)",
    )
    monkeypatch.setenv(HELPER_ENV, str(helper))
    task = asyncio.create_task(run_helper("lint", tmp_path / "d.deck.html"))
    for _ in range(200):
        if pid_file.is_file() and pid_file.read_text():
            break
        await asyncio.sleep(0.02)
    pid = int(pid_file.read_text())
    assert _alive(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _alive(pid)


async def test_each_helper_run_logs_its_wall_time_and_whether_the_browser_was_warm(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    answer = json.dumps({"ok": True, "slides": 8, "issues": [], "warm": False})
    helper = f"print('no warm browser', file=sys.stderr)\nprint({answer!r})"
    monkeypatch.setenv(HELPER_ENV, str(_script(tmp_path / "cold-helper", helper)))
    (workspace / "d.deck.html").write_text(kit.build_deck("corporate-clean", "T", SAMPLE), "utf-8")
    caplog.set_level(logging.INFO, logger="omnigent.superchat.decks.handlers")
    ctx = HandlerCtx(tool_name="deck_check", server_client=None, conversation_id="sess-1")
    out = json.loads(await handle_deck_tool(ctx, {"path": "d.deck.html"}))
    assert out["layout_checked"] is True
    messages = [r.getMessage() for r in caplog.records]
    runs = [m for m in messages if m.startswith("deck tool ")]
    assert len(runs) == 1
    assert runs[0].startswith("deck tool deck_check ")
    assert runs[0].endswith("s warm=False session_id=sess-1")
    assert "deck helper: no warm browser" in messages


async def test_deck_theme_set_saves_without_a_layout_check(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = tmp_path / "ran"
    helper = _script(tmp_path / "helper", f"open({str(ran)!r}, 'w').write('x')\nprint('{{}}')")
    monkeypatch.setenv(HELPER_ENV, str(helper))
    (workspace / "d.deck.html").write_text(kit.build_deck("corporate-clean", "T", SAMPLE), "utf-8")
    ctx = HandlerCtx(tool_name="deck_theme_set", server_client=None, conversation_id="s")
    out = json.loads(await handle_deck_tool(ctx, {"path": "d.deck.html", "theme_id": "nord"}))
    assert out == {
        "ok": True,
        "changed": True,
        "theme": "nord",
        "previous": "corporate-clean",
        "path": str((workspace / "d.deck.html").resolve()),
    }
    assert not ran.exists()
    assert (workspace / "d.deck.html").read_text("utf-8") == kit.build_deck("nord", "T", SAMPLE)


async def test_deck_new_without_a_template_builds_in_the_chosen_look_and_says_which(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.superchat.decks.authoring import handle_deck_new
    from omnigent.superchat.decks.look import choose_look

    from .look_fakes import ctx, view

    answer = json.dumps({"ok": True, "slides": 8, "issues": [], "warm": True})
    monkeypatch.setenv(HELPER_ENV, str(_script(tmp_path / "helper", f"print({answer!r})")))
    title = "Quarterly sales review"
    args = {"path": "q.deck.html", "title": title, "slides": SAMPLE}
    out = json.loads(await handle_deck_new(args, ctx(view(title))))
    chosen = choose_look(f"{title} {title}")
    assert out["written"] is True and out["template"] == chosen
    assert out["deck"]["look_id"] == chosen and out["deck"]["look_from"] == "chosen"
    assert kit.templates()[chosen].name in out["look_note"]
    written = (workspace / "q.deck.html").read_text("utf-8")
    assert written == kit.build_deck(chosen, title, SAMPLE)


def test_a_self_closed_element_does_not_take_the_text_after_it() -> None:
    from omnigent.superchat.decks.outline import deck_outline

    deck = (
        '<section class="slide"><h1 data-superchat-id="t">Title</h1>'
        '<svg data-superchat-id="icon"/><p data-superchat-id="p">Body</p>'
        '<span data-superchat-id="gap"/>tail</section>'
    )
    line = (deck_outline(deck) or "").splitlines()[1]
    assert line == 'Slide 1: t "Title" · icon · p "Body" · gap'
