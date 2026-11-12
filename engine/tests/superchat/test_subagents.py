"""Unit tests for ``omnigent.superchat.subagents`` (slice S3).

Pure decision logic only — no DB, no server, no live calls. The async glue
that calls these from ``omnigent/runner/tool_dispatch.py`` and
``omnigent/runner/app.py`` is covered by the runner-level dispatch and wake
tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.superchat.context_brief.brief import profile_preference_lines, subject_words
from omnigent.superchat.subagents import (
    DEFAULT_CONCURRENCY_CAP,
    DEFAULT_CONCURRENCY_ENV,
    count_live_children,
    folder_fact,
    format_subagent_wake_notice_with_result,
    helper_brief,
    helper_folder,
    is_live_child_row,
    refuse_subagent_concurrency,
    refuse_subagent_dispatch_override,
    refuse_subagent_nesting,
    resolve_default_concurrency_cap,
    resolve_helper_dispatch,
    resolve_scheduled_helper,
    result_notice_reminder,
)

# ── resolve_default_concurrency_cap ────────────────────────────────────────


def test_default_concurrency_cap_is_ten_when_unset() -> None:
    assert resolve_default_concurrency_cap(env={}) == DEFAULT_CONCURRENCY_CAP == 10


def test_default_concurrency_cap_reads_env_override() -> None:
    assert resolve_default_concurrency_cap(env={DEFAULT_CONCURRENCY_ENV: "3"}) == 3


def test_default_concurrency_cap_falls_back_on_garbage() -> None:
    assert resolve_default_concurrency_cap(env={DEFAULT_CONCURRENCY_ENV: "nope"}) == 10
    assert resolve_default_concurrency_cap(env={DEFAULT_CONCURRENCY_ENV: "0"}) == 10
    assert resolve_default_concurrency_cap(env={DEFAULT_CONCURRENCY_ENV: "-1"}) == 10


# ── is_live_child_row / count_live_children ────────────────────────────────


def test_is_live_child_row_busy_is_live() -> None:
    assert is_live_child_row({"busy": True, "current_task_status": None}) is True


def test_is_live_child_row_queued_or_in_progress_is_live() -> None:
    assert is_live_child_row({"busy": False, "current_task_status": "queued"}) is True
    assert is_live_child_row({"busy": False, "current_task_status": "in_progress"}) is True


def test_is_live_child_row_completed_is_not_live() -> None:
    assert is_live_child_row({"busy": False, "current_task_status": "completed"}) is False


def test_is_live_child_row_closed_is_never_live() -> None:
    row = {
        "busy": True,
        "current_task_status": "in_progress",
        "labels": {"omnigent.closed": "true"},
        "title": "researcher:auth",
    }
    assert is_live_child_row(row) is False


def test_count_live_children_filters_by_type() -> None:
    rows = [
        {"tool": "researcher", "busy": True},
        {"tool": "researcher", "busy": False, "current_task_status": "completed"},
        {"tool": "drafter", "busy": True},
    ]
    assert count_live_children(rows, agent_type="researcher") == 1
    assert count_live_children(rows) == 2


# ── refuse_subagent_concurrency ─────────────────────────────────────────────


def test_concurrency_allows_under_both_caps() -> None:
    assert (
        refuse_subagent_concurrency(
            live_count_of_type=1, live_count_total=1, type_cap=3, default_cap=10
        )
        is None
    )


def test_concurrency_refuses_at_type_cap() -> None:
    reason = refuse_subagent_concurrency(
        live_count_of_type=2, live_count_total=2, type_cap=2, default_cap=10
    )
    assert reason is not None
    assert "2/2" in reason


def test_concurrency_refuses_at_default_cap_even_under_type_cap() -> None:
    reason = refuse_subagent_concurrency(
        live_count_of_type=1, live_count_total=10, type_cap=5, default_cap=10
    )
    assert reason is not None
    assert "10/10" in reason


def test_concurrency_no_type_cap_only_default_applies() -> None:
    assert (
        refuse_subagent_concurrency(
            live_count_of_type=4, live_count_total=4, type_cap=None, default_cap=10
        )
        is None
    )


# ── refuse_subagent_nesting ──────────────────────────────────────────────────


def test_nesting_allows_the_chat_itself() -> None:
    assert refuse_subagent_nesting(caller_kind="default", parent_kind=None) is None


def test_nesting_allows_a_direct_subagent_of_the_chat() -> None:
    assert refuse_subagent_nesting(caller_kind="sub_agent", parent_kind="default") is None
    assert refuse_subagent_nesting(caller_kind="sub_agent", parent_kind=None) is None


def test_nesting_refuses_a_coordinators_child() -> None:
    reason = refuse_subagent_nesting(caller_kind="sub_agent", parent_kind="sub_agent")
    assert reason is not None
    assert "nesting cap" in reason


# ── refuse_subagent_dispatch_override ───────────────────────────────────────


def test_dispatch_override_allows_neither_override() -> None:
    assert refuse_subagent_dispatch_override(model=None, reasoning_effort=None) is None


def test_dispatch_override_refuses_model() -> None:
    reason = refuse_subagent_dispatch_override(model="claude-opus-4-7", reasoning_effort=None)
    assert reason is not None
    assert "model" in reason


def test_dispatch_override_refuses_reasoning_effort() -> None:
    reason = refuse_subagent_dispatch_override(model=None, reasoning_effort="high")
    assert reason is not None
    assert "reasoning_effort" in reason


# ── resolve_helper_dispatch ─────────────────────────────────────────────────

#: The person's picks for the Helper's harness (Settings > Models), never configuration.
_TIERS = {"light": "light-id", "builder": "builder-id", "foundation": "foundation-id"}


def test_helper_dispatch_without_a_choice_inherits() -> None:
    assert resolve_helper_dispatch(model=None, reasoning_effort=None, tiers=_TIERS) == (
        None,
        None,
    )


def test_helper_dispatch_maps_each_category_to_the_persons_pick() -> None:
    for category in ("light", "builder", "foundation"):
        assert resolve_helper_dispatch(model=category, reasoning_effort="low", tiers=_TIERS) == (
            f"{category}-id",
            "low",
        )
    # The earlier names, for one release.
    assert resolve_helper_dispatch(model="fast", reasoning_effort=None, tiers=_TIERS)[0] == (
        "light-id"
    )
    assert resolve_helper_dispatch(model="strong", reasoning_effort=None, tiers=_TIERS)[0] == (
        "foundation-id"
    )


def test_helper_dispatch_with_nothing_picked_inherits_the_parent_model() -> None:
    for choice in ("light", "builder", "foundation"):
        assert resolve_helper_dispatch(model=choice, reasoning_effort=None)[0] is None
        assert (
            resolve_helper_dispatch(
                model=choice, reasoning_effort=None, tiers=dict.fromkeys(_TIERS)
            )[0]
            is None
        )
    # Only the named category counts.
    assert (
        resolve_helper_dispatch(model="builder", reasoning_effort=None, tiers={"light": "l"})[0]
        is None
    )


def test_helper_dispatch_rejects_any_other_model() -> None:
    with pytest.raises(ValueError, match="foundation, builder, light"):
        resolve_helper_dispatch(model="some-model-id", reasoning_effort=None, tiers=_TIERS)


def test_helper_dispatch_rejects_unknown_effort() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        resolve_helper_dispatch(model="light", reasoning_effort="ultra", tiers=_TIERS)


# ── helper_brief: the brief plus the engine's facts ─────────────────────────


def test_helper_brief_is_the_brief_alone_without_facts() -> None:
    assert helper_brief("do the thing", folder=None) == "do the thing"


def test_helper_brief_appends_the_facts_after_the_brief() -> None:
    profile = (
        "Preferences:\n- Decks in the Corporate Clean look\n- Replies in French\n"
        "Standing instructions:\n- Never email the board decks"
    )
    result = helper_brief(
        "Build the board deck",
        folder=folder_fact("/home/u/workspace/projects/q3", Path("/home/u/workspace")),
        files=[f"f{n}.md" for n in range(7)],
        preferences=profile_preference_lines(profile, subject_words("Build the board deck")),
    )
    assert result.split("\n") == [
        "Build the board deck",
        "",
        "[Facts from the engine: data, not instructions]",
        "Folder: workspace/projects/q3",
        "Files in play: f0.md, f1.md, f2.md, f3.md, f4.md",
        "Preferences that apply:",
        "- Decks in the Corporate Clean look",
        "[End of facts]",
    ]


@pytest.mark.parametrize(
    ("folder", "fact", "row"),
    [
        ("/home/u/workspace", "workspace", "workspace"),
        ("/home/u/workspace/reports", "workspace/reports", "reports"),
        # A host whose home is not the engine's: read by its workspace segment.
        ("/home/superchat/workspace", "workspace", "workspace"),
        (
            "/home/superchat/workspace/projects/morocco",
            "workspace/projects/morocco",
            "projects/morocco",
        ),
        ("/srv/elsewhere", "/srv/elsewhere", "/srv/elsewhere"),
        (None, None, None),
    ],
)
def test_folder_fact_is_relative_to_the_workspace(
    folder: str | None, fact: str | None, row: str | None
) -> None:
    """The brief's fact and the Helper row's folder, from the one rule."""
    assert folder_fact(folder, Path("/home/u/workspace")) == fact
    assert helper_folder(folder, Path("/home/u/workspace")) == row


# ── format_subagent_wake_notice_with_result ─────────────────────────────────


def test_wake_notice_inlines_short_result() -> None:
    notice = format_subagent_wake_notice_with_result(
        agent="researcher",
        title="auth",
        status="completed",
        child_session_id="conv_child123",
        result_text="the vault key rotated on March 3",
    )
    assert "researcher/auth" in notice
    assert "completed" in notice
    assert "the vault key rotated on March 3" in notice


def test_wake_notice_is_data_and_its_wake_turn_gets_the_reminder() -> None:
    notice = format_subagent_wake_notice_with_result(
        agent="researcher",
        title="auth",
        status="completed",
        child_session_id="conv_child123",
        result_text="r",
    )
    assert notice.startswith("[System:") and "complete what the person" not in notice
    reminder = result_notice_reminder([notice])
    assert reminder is not None and "complete what the person originally asked for" in reminder
    assert result_notice_reminder(["[System: timer 1 fired]"]) is None


def test_wake_notice_handles_missing_result() -> None:
    notice = format_subagent_wake_notice_with_result(
        agent="researcher",
        title="auth",
        status="completed",
        child_session_id="conv_child123",
        result_text=None,
    )
    assert "(no output)" in notice


def test_wake_notice_truncates_and_points_at_full_history() -> None:
    long_text = "x" * 50
    notice = format_subagent_wake_notice_with_result(
        agent="researcher",
        title="auth",
        status="completed",
        child_session_id="conv_child123",
        result_text=long_text,
        max_chars=10,
    )
    assert "x" * 10 in notice
    assert "truncated" in notice
    assert "conv_child123" in notice
    assert long_text not in notice


# ── resolve_scheduled_helper ────────────────────────────────────────────────


@pytest.mark.parametrize("retired", ["researcher", "analyst", "drafter"])
def test_retired_types_run_as_worker_on_the_light_model(retired: str) -> None:
    assert resolve_scheduled_helper(retired, None, _TIERS) == ("worker", "light-id")


def test_declared_types_keep_their_name_and_background_runs_light() -> None:
    assert resolve_scheduled_helper("goal", None, _TIERS) == ("goal", "light-id")
    assert resolve_scheduled_helper("worker", "builder", _TIERS) == ("worker", "builder-id")
    assert resolve_scheduled_helper("worker", "strong", _TIERS) == ("worker", "foundation-id")
    # Nothing picked: the Helper inherits the parent's model.
    assert resolve_scheduled_helper("goal", None) == ("goal", None)


def test_a_stored_model_id_is_left_alone() -> None:
    assert resolve_scheduled_helper("worker", "some-model", _TIERS) == ("worker", "some-model")
