"""Unit tests for ``omnigent.context.state_file``: the tagged entry format, repair after a
rewrite, the size budget and the journal split. Pure: no model, no I/O."""

from __future__ import annotations

import random

from omnigent.context import state_file as sf
from omnigent.context.state_file import (
    Entry,
    close_journal_line,
    entries,
    entry_label_key,
    fit_budget,
    normalize_tag,
    parse,
    pending_closes,
    pending_entries,
    reconcile,
    render,
    split_journal,
)

TODAY = "2026-10-12"
BIG = 10_000


def _count(text: str) -> int:
    return len(text) // 4


def _run(previous: str | None, new: str, **kwargs) -> object:
    kwargs.setdefault("today", TODAY)
    kwargs.setdefault("count", _count)
    return reconcile(previous, new, **kwargs)


def _ids(text: str) -> set[str]:
    return set(entries(parse(text)))


# ── format ─────────────────────────────────────────────────────────────────


def test_parse_and_render_round_trip() -> None:
    text = "\n".join(
        [
            "## Topics",
            "- [t-launch-budget · 2026-10-09] Spring launch budget: confirm the venue line",
            "- [o-venue · 2026-10-09] closed: the venue is booked",
            "- [t-old · 2026-09-30] merged into [t-launch-budget]: same topic",
            "",
            "Current position / next step: wait.",
        ]
    )
    assert render(parse(text)) == text
    found = entries(parse(text))
    assert list(found) == ["t-launch-budget", "o-venue", "t-old"]
    assert found["t-launch-budget"].date == "2026-10-09"
    assert found["o-venue"].closed
    assert found["t-old"].merged_into == "t-launch-budget"
    assert found["t-old"].body == "same topic"


def test_free_text_and_look_alikes_are_not_entries() -> None:
    text = "## Context checkpoint — 2026-10-12\n[Not a tag] hello\n[x-1] odd\nclosed: nothing"
    assert not entries(parse(text))
    assert render(parse(text)) == text


def test_a_body_starting_with_the_word_closed_is_not_a_close() -> None:
    (entry,) = entries(parse("- [t-a · 2026-10-01] closed the deal with the vendor")).values()
    assert not entry.closed
    assert entry.body == "closed the deal with the vendor"


def test_attribute_change_renders_again_and_keeps_identity() -> None:
    (entry,) = entries(parse("- [t-a · 2026-10-01] body")).values()
    touched = entry.with_attrs(date=TODAY, closed=True)
    assert touched.line == f"- [t-a · {TODAY}] closed: body"
    assert touched.id == entry.id


def test_normalize_tag_accepts_brackets_and_dates() -> None:
    assert normalize_tag("[t-launch-budget]") == "t-launch-budget"
    assert normalize_tag(" t-launch-budget ") == "t-launch-budget"
    assert normalize_tag("[T-Launch-Budget · 2026-10-09]") == "t-launch-budget"
    assert normalize_tag("launch") is None
    assert normalize_tag("t-" + "a" * 80) is None


def test_pending_closes_read_from_labels_skip_cleared_ones() -> None:
    labels = {
        "omnigent.state_close.t-a": "wrong",
        "omnigent.state_close.t-b": "",
        "omnigent.context.mode": "superside-chat",
    }
    assert pending_closes(labels) == {"t-a": "wrong"}
    assert pending_closes(None) == {}


def test_a_pending_entry_lands_in_the_working_list() -> None:
    labels = {entry_label_key("d-acme-vs-globex"): "Acme vs Globex (deck): checked", "other": "x"}
    adds = pending_entries(labels)
    assert adds == {"d-acme-vs-globex": "Acme vs Globex (deck): checked"}
    previous = (
        "Working list\n- [t-budget · 2026-10-01] Spring budget\n- [d-acme-vs-globex · 2026-10-01] old"
    )
    out = reconcile(previous, previous, adds=adds, today=TODAY, count=len)
    entry = entries(parse(out.text))["d-acme-vs-globex"]
    assert entry.body == "Acme vs Globex (deck): checked" and entry.date == TODAY
    assert "t-budget" in entries(parse(out.text))
    fresh = reconcile(None, "Working list", adds=adds, today=TODAY, count=len)
    assert f"- [d-acme-vs-globex · {TODAY}] Acme vs Globex (deck): checked" in fresh.text


# ── repair ─────────────────────────────────────────────────────────────────

PREVIOUS = "\n".join(
    [
        "## Topics",
        "- [t-a · 2026-10-01] Alpha: step one",
        "- [d-b · 2026-10-02] Beta decided",
        "- [o-c · 2026-10-03] Gamma open",
        "Current position: here.",
    ]
)


def test_a_changed_date_is_not_a_loss() -> None:
    new = PREVIOUS.replace("2026-10-01", "2026-10-12")
    out = _run(PREVIOUS, new, budget=BIG)
    assert out.repairs == 0
    assert out.text == new


def test_a_dropped_tag_is_repaired_word_for_word_and_counted() -> None:
    new = PREVIOUS.replace("- [d-b · 2026-10-02] Beta decided\n", "")
    out = _run(PREVIOUS, new, budget=BIG)
    assert out.repairs == 1
    assert "- [d-b · 2026-10-02] Beta decided" in out.text.split("\n")
    assert _ids(out.text) == {"t-a", "d-b", "o-c"}


def test_merged_into_a_present_tag_is_accepted() -> None:
    new = PREVIOUS.replace(
        "- [d-b · 2026-10-02] Beta decided", f"- [d-b · {TODAY}] merged into [t-a]: same thing"
    )
    out = _run(PREVIOUS, new, budget=BIG)
    assert out.repairs == 0
    assert out.text == new


def test_merged_into_an_absent_tag_is_repaired() -> None:
    new = PREVIOUS.replace(
        "- [d-b · 2026-10-02] Beta decided", f"- [d-b · {TODAY}] merged into [t-nowhere]: oops"
    )
    out = _run(PREVIOUS, new, budget=BIG)
    assert out.repairs == 1
    assert "- [d-b · 2026-10-02] Beta decided" in out.text.split("\n")
    assert "t-nowhere" not in out.text


def test_a_dangling_merge_already_in_the_previous_file_is_not_repaired_again() -> None:
    previous = PREVIOUS + "\n- [t-x · 2026-10-04] merged into [t-gone]: folded earlier"
    out = _run(previous, previous, budget=BIG)
    assert out.repairs == 0


def test_a_closed_entry_stays_and_is_not_resurrected() -> None:
    new = PREVIOUS.replace("- [o-c · 2026-10-03] Gamma open", f"- [o-c · {TODAY}] closed: done")
    out = _run(PREVIOUS, new, budget=BIG)
    assert out.repairs == 0
    assert entries(parse(out.text))["o-c"].closed


def test_a_tag_back_without_a_date_gets_its_previous_date() -> None:
    new = PREVIOUS.replace("[t-a · 2026-10-01]", "[t-a]")
    out = _run(PREVIOUS, new, budget=BIG)
    assert entries(parse(out.text))["t-a"].date == "2026-10-01"


def test_a_new_entry_without_a_date_gets_today() -> None:
    out = _run(PREVIOUS, PREVIOUS + "\n- [t-new] Fresh", budget=BIG)
    assert entries(parse(out.text))["t-new"].date == TODAY


def test_an_untagged_previous_checkpoint_is_carried_over_without_repairs() -> None:
    previous = "## Topics\nAlpha: step one\nBeta decided\nCurrent position: here."
    out = _run(
        previous, "## Topics\n- [t-alpha] Alpha: step one\n- [d-beta] Beta decided", budget=BIG
    )
    assert out.repairs == 0
    assert _ids(out.text) == {"t-alpha", "d-beta"}


def test_no_previous_file_means_nothing_to_repair() -> None:
    out = _run(None, "- [t-a · 2026-10-12] A", budget=BIG)
    assert out.repairs == 0 and out.evicted == []


# ── pending closes ─────────────────────────────────────────────────────────


def test_a_pending_close_marks_the_entry_and_is_reported() -> None:
    out = _run(PREVIOUS, PREVIOUS, closes={"t-a": "wrong"}, budget=BIG)
    assert out.applied_closes == [{"tag": "t-a", "reason": "wrong"}]
    entry = entries(parse(out.text))["t-a"]
    assert entry.closed and entry.date == TODAY
    assert out.repairs == 0


def test_two_closes_both_apply() -> None:
    out = _run(PREVIOUS, PREVIOUS, closes={"t-a": "wrong", "o-c": "dropped"}, budget=BIG)
    found = entries(parse(out.text))
    assert found["t-a"].closed and found["o-c"].closed and not found["d-b"].closed
    assert [c["tag"] for c in out.applied_closes] == ["t-a", "o-c"]


def test_a_closed_entry_the_rewrite_dropped_is_not_resurrected_open() -> None:
    new = PREVIOUS.replace("- [t-a · 2026-10-01] Alpha: step one\n", "")
    out = _run(PREVIOUS, new, closes={"t-a": "wrong"}, budget=BIG)
    assert entries(parse(out.text))["t-a"].closed
    assert out.repairs == 0


def test_an_unknown_close_is_skipped() -> None:
    out = _run(PREVIOUS, PREVIOUS, closes={"t-zzz": "x"}, budget=BIG)
    assert out.applied_closes == []


def test_close_journal_line_names_tag_and_reason() -> None:
    line = close_journal_line({"tag": "t-a", "reason": "wrong"}, today=TODAY)
    assert line == "2026-10-12 · closed [t-a]: wrong"


# ── budget ─────────────────────────────────────────────────────────────────


def _file(*lines: str) -> list:
    return parse("\n".join(lines))


def test_eviction_order_done_entries_first_oldest_first_then_oldest_open() -> None:
    lines = _file(
        "## Topics",
        "- [t-open-old · 2026-01-01] " + "x" * 100,
        "- [t-closed-new · 2026-09-01] closed: " + "x" * 100,
        "- [t-closed-old · 2026-02-01] closed: " + "x" * 100,
        "- [t-merged · 2026-03-01] merged into [t-open-old]: " + "x" * 100,
        "- [t-open-new · 2026-10-01] " + "x" * 100,
        "Position.",
    )
    total = _count(render(lines))
    kept, evicted = fit_budget(lines, total - 1, _count)
    assert [parse(e)[0].id for e in evicted] == ["t-closed-old"]
    kept, evicted = fit_budget(lines, 1, _count)
    assert [parse(e)[0].id for e in evicted] == [
        "t-closed-old",
        "t-merged",
        "t-closed-new",
        "t-open-old",
        "t-open-new",
    ]
    assert render(kept) == "## Topics\nPosition."


def test_evicted_entries_come_back_word_for_word() -> None:
    lines = _file("- [t-a · 2026-01-01] closed: " + "y" * 200, "- [t-b · 2026-02-01] " + "z" * 200)
    originals = [line.line for line in lines if isinstance(line, Entry)]
    kept, evicted = fit_budget(lines, 60, _count)
    assert evicted == originals[:1]
    assert originals[0] not in render(kept)


def test_a_file_over_budget_with_no_closed_entries_still_fits() -> None:
    text = "## Topics\n" + "\n".join(
        f"- [t-e{i:03d} · 2026-{1 + i % 9:02d}-01] " + "word " * 40 for i in range(80)
    )
    out = _run(text, text, budget=500)
    assert _count(out.text) <= 500
    assert out.evicted
    assert all(parse(line)[0].id not in _ids(out.text) for line in out.evicted)
    # the longest untouched go first
    kept_dates = [e.date for e in entries(parse(out.text)).values()]
    evicted_dates = [parse(line)[0].date for line in out.evicted]
    assert max(evicted_dates) <= min(kept_dates)


def test_free_text_alone_over_budget_stops_without_looping() -> None:
    out = _run(None, "word " * 2000, budget=100)
    assert out.evicted == []


# ── journal ────────────────────────────────────────────────────────────────


def test_split_journal_takes_the_section_off() -> None:
    reply = (
        "## Context checkpoint\n- [t-a · 2026-10-12] A\n\n<journal>\n"
        "- 2026-10-12 · asked for A\n2026-10-12 · wrote a file\n\n</journal>"
    )
    state, lines = split_journal(reply)
    assert "journal" not in state and state.endswith("A")
    assert lines == ["2026-10-12 · asked for A", "2026-10-12 · wrote a file"]


def test_split_journal_without_a_section_is_zero_lines() -> None:
    assert split_journal("state") == ("state", [])


def test_split_journal_without_a_closing_tag_and_capped() -> None:
    body = "\n".join(f"2026-10-12 · line {i}" for i in range(30))
    state, lines = split_journal(f"state\n<journal>\n{body}")
    assert state == "state"
    assert len(lines) == 10


# ── 400 compactions ────────────────────────────────────────────────────────


def _fake_summarizer(previous: str, rng: random.Random, serial: list[int]) -> str:
    """Rewrite *previous*: drop, close, merge (sometimes into nothing), re-date, add."""
    out: list[str] = []
    present = list(entries(parse(previous)))
    for line in parse(previous):
        if not isinstance(line, Entry):
            out.append(line)
            continue
        roll = rng.random()
        if roll < 0.10:
            continue
        if roll < 0.18:
            out.append(line.with_attrs(date=TODAY, closed=True).line)
        elif roll < 0.24 and present:
            target = rng.choice([*present, "t-never-existed"])
            if target != line.id:
                out.append(line.with_attrs(date=TODAY, merged_into=target).line)
                continue
            out.append(line.line)
        elif roll < 0.34:
            out.append(line.with_attrs(date=TODAY).line)
        elif roll < 0.38:
            out.append(f"{line.prefix}[{line.id}] {line.body}")
        else:
            out.append(line.line)
    for _ in range(rng.choice([0, 1, 1, 2])):
        serial[0] += 1
        out.append(f"- [t-n{serial[0]}] topic number {serial[0]} " + "text " * rng.randint(1, 12))
    return "\n".join(out)


def test_400_compactions_never_lose_an_open_tag() -> None:
    rng = random.Random(7)
    serial = [0]
    state = "## Topics\nPosition."
    total_evicted = 0
    for _ in range(400):
        new = _fake_summarizer(state, rng, serial)
        before = entries(parse(state))
        out = _run(state, new, budget=400)
        after = entries(parse(out.text))
        evicted = {parse(line)[0].id: line for line in out.evicted}
        for tag, old in before.items():
            if tag in after or tag in evicted:
                continue
            raise AssertionError(f"{tag} vanished without being evicted")
        # every evicted entry was returned as it stood just before the drop
        untrimmed = _run(state, new, budget=BIG).text.split("\n")
        for tag, line in evicted.items():
            assert tag not in after
            assert line in untrimmed
        total_evicted += len(evicted)
        assert _count(out.text) <= 400 or not after
        state = out.text
    assert total_evicted > 0


def test_an_overlong_entry_is_cut_so_it_cannot_crowd_out_the_rest() -> None:
    long = "- [t-long · 2026-10-01] " + "word " * 200
    out = sf.reconcile(
        None, long + "\n- [t-short · 2026-10-01] kept", today="2026-10-12", count=len
    )
    by_id = sf.entries(sf.parse(out.text))
    assert len(by_id["t-long"].body) == sf.ENTRY_MAX_CHARS and by_id["t-long"].body.endswith("…")
    assert by_id["t-short"].body == "kept" and out.repairs == 0


def test_likely_duplicates_point_at_same_kind_entries_with_heavy_overlap_only() -> None:
    text = "\n".join(
        [
            "- [t-venue · 2026-10-01] Venue for the spring launch: choosing between two halls",
            "- [t-hall · 2026-10-05] Spring launch venue: choosing between two halls downtown",
            "- [d-venue · 2026-10-05] Venue for the spring launch: choosing between two halls",
            "- [t-offsite · 2026-10-05] Offsite catering order for the engineering team",
            "- [t-old · 2026-09-01] closed: Venue for the spring launch: choosing two halls",
        ]
    )
    # Same kind, nearly the same words. A decision, another topic and a closed entry are not.
    assert sf.likely_duplicates(text) == [("t-venue", "t-hall")]
    assert sf.likely_duplicates(None) == [] and sf.likely_duplicates("no tags here") == []


# ── stale? (a rendering of the date) ───────────────────────────────────────


def test_an_open_entry_30_days_old_renders_stale_and_29_does_not() -> None:
    text = "\n".join(
        [
            "- [o-dana · 2026-09-12] waiting on Dana's reply",
            "- [o-quotes · 2026-09-13] three quotes requested",
            "- [t-venue · 2026-09-01] closed: the venue is booked",
            "- [t-old · 2026-09-01] merged into [o-dana]: same thing",
        ]
    )
    out = _run(None, text)
    assert out.text.splitlines() == [
        "- [o-dana · 2026-09-12 · stale?] waiting on Dana's reply",
        "- [o-quotes · 2026-09-13] three quotes requested",
        "- [t-venue · 2026-09-01] closed: the venue is booked",
        "- [t-old · 2026-09-01] merged into [o-dana]: same thing",
    ]


def test_a_stale_mark_coming_back_is_the_same_entry_with_no_repair() -> None:
    previous = _run(None, "- [o-dana · 2026-09-01] waiting on Dana's reply").text
    assert "· stale?]" in previous
    # The summarizer keeps the mark in the tag, moves it after the tag, or touches the entry.
    for new in (
        previous,
        "- [o-dana · 2026-09-01] · stale? waiting on Dana's reply",
        "- [o-dana · 2026-10-12 · stale?] waiting on Dana's reply",
    ):
        lines = parse(new)
        (entry,) = entries(lines).values()
        assert entry.id == "o-dana" and entry.body == "waiting on Dana's reply"
        assert "stale" not in render(lines)
        out = _run(previous, new)
        assert out.repairs == 0 and len(entries(parse(out.text))) == 1
    assert _run(previous, previous).text == previous
    touched = _run(previous, "- [o-dana · 2026-10-12 · stale?] waiting on Dana's reply")
    assert touched.text == "- [o-dana · 2026-10-12] waiting on Dana's reply"


def test_a_stale_closed_entry_parses_closed_and_never_renders_stale() -> None:
    out = _run(None, "- [o-dana · 2026-09-01 · stale?] closed: Dana replied")
    (entry,) = entries(parse(out.text)).values()
    assert entry.closed and out.text == "- [o-dana · 2026-09-01] closed: Dana replied"


def test_evicted_entries_go_to_the_journal_without_the_stale_mark() -> None:
    text = "- [o-old · 2026-08-01] " + "word " * 40 + "\n- [o-new · 2026-10-12] kept"
    out = _run(None, text, budget=20)
    assert out.evicted and "stale" not in out.evicted[0]


# ── preferences (p-) ───────────────────────────────────────────────────────

PREFS = "\n".join(
    [
        "- [d-venue · 2026-10-02] Hall B for the spring launch",
        "- [p-finance-eur · 2026-08-01] Amounts in EUR, never USD",
        "- [p-nosend · 2026-08-01] Never send an email without asking first",
    ]
)


def test_a_dropped_preference_is_repaired_word_for_word() -> None:
    new = PREFS.replace("- [p-finance-eur · 2026-08-01] Amounts in EUR, never USD\n", "")
    out = _run(PREFS, new, budget=BIG)
    assert out.repairs == 1
    assert "- [p-finance-eur · 2026-08-01] Amounts in EUR, never USD" in out.text.split("\n")


def test_a_preference_can_be_closed_and_its_tag_normalized() -> None:
    assert normalize_tag("[p-nosend · 2026-08-01]") == "p-nosend"
    out = _run(PREFS, PREFS, closes={"p-nosend": "the person lifted it"}, budget=BIG)
    assert out.applied_closes == [{"tag": "p-nosend", "reason": "the person lifted it"}]
    assert entries(parse(out.text))["p-nosend"].closed


def test_an_old_open_preference_is_evicted_after_every_other_open_entry() -> None:
    lines = _file(
        "- [p-old · 2026-01-01] " + "x" * 100,
        "- [d-new · 2026-10-01] " + "x" * 100,
        "- [p-closed · 2026-09-01] closed: " + "x" * 100,
        "- [o-mid · 2026-05-01] " + "x" * 100,
    )
    _, evicted = fit_budget(lines, 1, _count)
    assert [parse(e)[0].id for e in evicted] == ["p-closed", "o-mid", "d-new", "p-old"]


def test_an_old_preference_never_renders_stale() -> None:
    out = _run(None, PREFS)
    assert out.text == PREFS


def test_a_decision_restated_as_a_preference_is_hinted_as_a_duplicate() -> None:
    text = "\n".join(
        [
            "- [d-finance-eur · 2026-10-01] Report every amount in euros, never dollars",
            "- [p-finance-eur · 2026-10-01] Report every amount in euros, never dollars",
            "- [t-finance · 2026-10-01] Report every amount in euros, never dollars",
        ]
    )
    assert sf.likely_duplicates(text) == [("d-finance-eur", "p-finance-eur")]
