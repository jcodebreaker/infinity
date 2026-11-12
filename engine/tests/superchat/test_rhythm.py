"""The rhythm (ADR 0038): scoring, cold start, the ceilings and the store's tally moves."""

from __future__ import annotations

from datetime import UTC, datetime

from omnigent.superchat.rhythm.score import (
    ACTED_ON,
    NOT,
    OPENED,
    SHOWN,
    UNSEEN,
    Model,
    Prefs,
    Tally,
    outcome,
    summary,
)
from omnigent.superchat.rhythm.store import SqlAlchemyRhythmStore

# A Monday.
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
MON_20 = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)
SAT_10 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)


def test_outcome_takes_the_strongest_thing_that_happened() -> None:
    assert outcome({SHOWN, OPENED, ACTED_ON}) == (1.0, 0.0)
    assert outcome({UNSEEN, OPENED}) == (0.5, 0.0)
    assert outcome({UNSEEN, SHOWN}) == (0.0, 0.5)
    assert outcome({SHOWN}) == (0.0, 0.0)


def test_cold_start_is_weekdays_8_to_18_and_the_level_caps_the_day() -> None:
    model = Model(prefs=Prefs(level="low"))
    assert model.good(MON_10, NOT)
    assert not model.good(MON_20, NOT)
    assert not model.good(SAT_10, NOT)
    sent = Model(prefs=Prefs(level="low"), sent=[(MON_10.timestamp() - 3600, "topic_watch", "t")])
    assert not sent.good(MON_10, NOT)
    brief = Model(
        prefs=Prefs(level="low"), sent=[(MON_10.timestamp() - 3600, "daily_brief", None)]
    )
    assert brief.good(MON_10, NOT)
    assert not Model(prefs=Prefs(level="off")).good(MON_10, NOT)


def test_one_message_per_topic_per_three_hours() -> None:
    model = Model(prefs=Prefs(level="high"), sent=[(MON_10.timestamp() - 3600, "x", "deal")])
    assert not model.good(MON_10, NOT, "deal")
    assert model.good(MON_10, NOT, "other")
    slot = model.next_slot(MON_10, NOT, "deal")
    assert slot is not None and slot.astimezone(UTC).hour == 12


def test_learned_cells_only_ever_close() -> None:
    closed = {"morning:weekday:not": Tally(yes=0.0, no=3.0)}
    afternoon_yes = {**closed, "afternoon:weekday:not": Tally(yes=3.0)}
    model = Model(prefs=Prefs(), tallies=afternoon_yes, learned=True)
    assert not model.good(MON_10, NOT)
    slot = model.next_slot(MON_10, NOT)
    assert (
        slot is not None and slot.astimezone(UTC).hour == 11
    )  # midday: no evidence, today's rule
    # A good evening cell never opens the evening outside the default window.
    evening = Model(prefs=Prefs(), tallies={"evening:weekday:not": Tally(yes=5.0)}, learned=True)
    assert not evening.good(MON_20, NOT)
    # Not learned yet: the tallies do not count.
    assert Model(prefs=Prefs(), tallies=closed, learned=False).good(MON_10, NOT)
    assert summary(model).startswith("I usually reach out on weekday afternoons")


def test_store_moves_the_cell_by_the_change_in_outcome(db_uri: str) -> None:
    store = SqlAlchemyRhythmStore(db_uri)
    item = "0" * 31 + "1"
    cell = "morning:weekday:not"
    assert store.add_event("me", item, UNSEEN, 100, cell)
    assert store.tallies("me")[cell].no > 0.49
    assert store.add_event("me", item, OPENED, 200, None)
    assert not store.add_event("me", item, OPENED, 300, None)
    tally = store.tallies("me")[cell]
    assert round(tally.yes, 2) == 0.5 and round(tally.no, 2) == 0.0
    store.reset("me")
    assert store.tallies("me") == {}


def test_exploration_floor_reopens_the_best_closed_cell_after_three_quiet_workdays() -> None:
    closed = {
        "morning:weekday:not": Tally(no=4.0),
        "midday:weekday:not": Tally(no=6.0),
        "afternoon:weekday:not": Tally(no=6.0),
    }
    quiet = Model(prefs=Prefs(), tallies=closed, learned=True)
    assert quiet.good(MON_10, NOT)  # the morning is the best of the closed cells
    slot = quiet.next_slot(MON_10.replace(hour=11), NOT)
    assert slot is not None and slot.astimezone(UTC).day == 27  # back to a morning
    # A counted message two working days ago (Thursday) keeps the cells closed.
    thursday = MON_10.timestamp() - 4 * 86400
    recent = Model(prefs=Prefs(), tallies=closed, learned=True, sent=[(thursday, "x", None)])
    assert not recent.good(MON_10, NOT)


def test_a_message_on_several_topics_holds_each_for_three_hours(db_uri: str) -> None:
    store = SqlAlchemyRhythmStore(db_uri)
    item = "0" * 31 + "2"
    assert store.add_event("me", item, "sent", 100, None, topic_keys=["q3", "board"])
    assert store.sent_topics("me", [item]) == {item: ["board", "q3"]}
    sent = [(MON_10.timestamp() - 3600, "lookout_wake", frozenset({"q3", "board"}))]
    model = Model(prefs=Prefs(level="high"), sent=sent)
    assert not model.good(MON_10, NOT, "q3")
    assert not model.good(MON_10, NOT, "board")
    assert model.good(MON_10, NOT, "other")
