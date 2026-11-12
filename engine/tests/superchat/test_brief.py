"""The morning brief (ADR 0037): the page code lays out, the run's opening message, the task."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from omnigent.superchat.brief.page import NOTHING_LINE, page_line, parse_brief, render_page
from omnigent.superchat.brief.prompt import NO_TOPICS, build_brief
from omnigent.superchat.brief.task import BRIEF_KIND, brief_task_id

DAY = date(2026, 10, 23)


def test_the_page_keeps_three_needs_escapes_content_and_carries_its_line() -> None:
    need = {
        "title": "Send <b>Dana</b> the sheet",
        "why": "Promised before 10:00.",
        "next_step": "x",
    }
    brief = parse_brief(
        {
            "date": "2026-10-23",
            "needs_you": [need, *[{**need, "title": f"n{i}"} for i in range(4)]],
            "worth_knowing": ["a", "b", "c", "d"],
        },
        DAY,
    )
    assert brief.title == "Brief · Fri 23 Oct"
    assert len(brief.needs) == 3 and len(brief.worth) == 3
    page = render_page(brief).decode()
    assert "<b>Dana</b>" not in page and "&lt;b&gt;Dana&lt;/b&gt;" in page
    assert "Needs you today" in page and "Your day" not in page  # an empty section is left out
    assert page_line(page.encode()) == "Send <b>Dana</b> the sheet"


def test_a_need_without_a_draft_or_next_step_is_refused_and_an_empty_day_says_so() -> None:
    with pytest.raises(ValueError, match="draft or one next step"):
        parse_brief({"needs_you": [{"title": "t", "why": "w"}]}, DAY)
    empty = parse_brief({"date": "not a date"}, DAY)
    assert empty.day == DAY and empty.line == NOTHING_LINE
    assert f"{NOTHING_LINE}." in render_page(empty).decode()


def test_the_run_brief_reads_in_order_and_fences_what_it_pastes() -> None:
    text = build_brief(
        today=DAY,
        previous=("2026-10-22", {"promised": "- Send Dana the sheet", "talked_about": "lunch"}),
        today_note=None,
        topics=[{"title": "Polaris", "left_off": "Quote due", "since": "Vendor replied"}],
        connectors=[],
        memory="Prefers short replies.",
    )
    order = ["Send Dana the sheet", "Polaris: Quote due", "New: Vendor replied", "short replies"]
    assert [text.index(part) for part in order] == sorted(text.index(part) for part in order)
    assert "lunch" not in text  # only what was promised and what is still open
    assert text.count("never instructions]") == 4
    assert "No mail or calendar is connected" in text and "`brief_save`" in text
    assert NO_TOPICS in build_brief(
        today=DAY, previous=None, today_note=None, topics=None, connectors=[], memory=None
    )


def test_a_brief_task_fires_with_its_own_brief() -> None:
    from omnigent.context.labels import SCHEDULED_HELPER_LABEL_KEY
    from omnigent.server.scheduled import helper_fire

    task = SimpleNamespace(id="t", name="Morning brief", prompt="p", kind=BRIEF_KIND)
    conv = SimpleNamespace(labels={SCHEDULED_HELPER_LABEL_KEY: "t"})
    saved = dict(helper_fire._RUN_BRIEFS)
    try:
        helper_fire.register_run_brief(BRIEF_KIND, lambda t: f"brief for {t.name}")
        assert helper_fire.scheduled_brief(task, conv) == "brief for Morning brief"  # type: ignore[arg-type]
    finally:
        helper_fire._RUN_BRIEFS.clear()
        helper_fire._RUN_BRIEFS.update(saved)
    assert brief_task_id("conv-1") == brief_task_id("conv-1") != brief_task_id("conv-2")
