"""Tests for :class:`SqlAlchemyTopicStore`: upsert by key, cleaning, event merge, the window."""

from __future__ import annotations

import json
import time
from zoneinfo import ZoneInfo

import pytest

from omnigent.db.db_models import SqlTopic, current_workspace_id
from omnigent.superchat.topics import store as store_module
from omnigent.superchat.topics.routes import topic_card
from omnigent.superchat.topics.store import (
    SqlAlchemyTopicStore,
    register_topic_listener,
    unregister_topic_listener,
)
from omnigent.superchat.topics.topic import topic_lines

UTC = ZoneInfo("UTC")
NOW = 1_792_000_000  # 2026-10-14 17:46 UTC
DAY = 86400


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyTopicStore:
    return SqlAlchemyTopicStore(db_uri)


def test_upsert_cleans_trims_and_merges_events(store: SqlAlchemyTopicStore) -> None:
    first = store.update(
        "alice",
        [
            {
                "key": "Polaris Launch!",
                "title": "Polaris launch plan for the whole team",
                "left_off": "Waiting on legal.",
                "covered": [
                    {"date": "2026-10-14", "kind": "Decided", "text": "Ship Friday"},
                    {"date": "2026-10-14", "kind": "Wondered", "text": "dropped"},
                ],
                "open": [{"kind": "Promised", "text": "Send the deck"}] * 6,
                "starters": ["Draft the note", "draft the note", "Check legal", "Plan QA", "x"],
                "events": [{"at": NOW - DAY, "line": "Decided to ship Friday"}],
                "sources": {"chats": 3, "files": -2, "emails": True},
                "watch": {"people": ["dana@x.com"], "terms": [f"t{i}" for i in range(12)]},
            },
            {"key": "orphan", "left_off": "no title"},
        ],
        zone=UTC,
        now=NOW,
    )
    assert [(w["key"], w["created"]) for w in first["written"]] == [("polaris-launch", True)]
    assert first["skipped"] == [{"key": "orphan", "reason": "a new topic needs a title"}]
    topic = store.get("alice", "polaris-launch")
    assert topic is not None
    assert topic.title == "Polaris launch plan for the"
    assert [c["kind"] for c in topic.covered] == ["Decided"]
    assert len(topic.open) == 4
    assert topic.starters == ["Draft the note", "Check legal", "Plan QA"]
    assert topic.sources == {"chats": 3, "files": 0}
    assert len(topic.watch["terms"]) == 8 and topic.watch["web"] is False
    assert topic.last_touched_at == NOW - DAY

    second = store.update(
        "alice",
        [
            {
                "key": "polaris-launch",
                "left_off": "Legal signed off.",
                "events": [
                    {
                        "at": NOW - DAY + 60,
                        "line": "decided to ship  friday",
                    },  # same day, same line
                    {"at": "2026-10-14T16:30", "line": "Legal signed off"},
                    *({"at": NOW - 3 * DAY - i, "line": f"older {i}"} for i in range(14)),
                ],
            }
        ],
        zone=UTC,
        now=NOW,
    )
    assert [(w["key"], w["created"]) for w in second["written"]] == [("polaris-launch", False)]
    topic = store.get("alice", "polaris-launch")
    assert topic is not None
    assert topic.title == "Polaris launch plan for the"  # kept: not passed
    assert topic.left_off == "Legal signed off."
    assert len(topic.events) == 12
    assert topic.events[0]["line"] == "Legal signed off"
    assert sum(1 for e in topic.events if "friday" in e["line"].lower()) == 1
    assert topic.last_touched_at == topic.events[0]["at"]


def test_window_closed_and_mark_new(store: SqlAlchemyTopicStore) -> None:
    for key, at in (("fresh", NOW - DAY), ("stale", NOW - 8 * DAY), ("done", NOW - DAY)):
        store.update(
            "alice",
            [{"key": key, "title": key, "events": [{"at": at, "line": key}]}],
            zone=UTC,
            now=NOW,
        )
    store.update("alice", [{"key": "done", "status": "closed"}], zone=UTC, now=NOW)
    assert [t.key for t in store.list_recent("alice", now=NOW)] == ["fresh"]
    assert store.list_recent("alice", days=30, now=NOW)[0].key == "fresh"  # never past 7 days
    assert store.get("alice", "stale") is not None  # kept, just not listed
    assert store.list_recent("bob", now=NOW) == []

    assert store.mark_new("alice", "fresh", "Dana replied", NOW)
    assert not store.mark_new("alice", "missing", "x", NOW)
    topic = store.get("alice", "fresh")
    assert topic is not None and topic.since == "Dana replied" and topic.since_at == NOW


def test_open_lines_keep_ids_and_dates(store: SqlAlchemyTopicStore) -> None:
    """ADR 0038: code assigns ids, a writer passing one back keeps it (even reworded), a line
    without one matches a stored line by text, an invented id is replaced, ``due`` must be a real
    day and, with ``starter`` and ``verb``, survives a write that leaves it out."""
    first = store.update(
        "alice",
        [
            {
                "key": "deal",
                "title": "Deal",
                "open": [
                    {
                        "kind": "Promised",
                        "text": "Send the deck",
                        "due": "2026-10-28",
                        "starter": "Draft my note to Dana with the deck",
                        "verb": "Draft it now, please",
                    },
                    {"kind": "Open", "text": "Legal review", "due": "2026-02-30"},
                ],
            }
        ],
        zone=UTC,
        now=NOW,
    )
    deck, legal = first["written"][0]["open"]
    assert deck["due"] == "2026-10-28" and "due" not in legal
    assert deck["verb"] == "Draft it" and "starter" not in legal
    assert deck["id"] and legal["id"] and deck["id"] != legal["id"]

    store.update(
        "alice",
        [
            {
                "key": "deal",
                "open": [
                    {"id": deck["id"], "kind": "Promised", "text": "Send Dana the final deck"},
                    {"kind": "Open", "text": "legal  review"},
                    {"id": "made-up", "kind": "Open", "text": "Pricing sign-off"},
                ],
            }
        ],
        zone=UTC,
        now=NOW,
    )
    topic = store.get("alice", "deal")
    assert topic is not None
    reworded, same, new = topic.open
    assert reworded == {
        "id": deck["id"],
        "kind": "Promised",
        "text": "Send Dana the final deck",
        "due": "2026-10-28",
        "starter": "Draft my note to Dana with the deck",
        "verb": "Draft it",
    }
    assert same["id"] == legal["id"]
    assert new["id"] not in {deck["id"], legal["id"], "made-up"}
    lines = "\n".join(topic_lines(topic))
    assert f"(id {deck['id']}, due 2026-10-28)" in lines and "covered" not in lines


def test_old_rows_get_stable_ids_and_listeners_hear_every_write(
    store: SqlAlchemyTopicStore,
) -> None:
    store.update("alice", [{"key": "old", "title": "Old"}], zone=UTC, now=NOW)
    # A row written before ids existed.
    with store._session_immediate("seed") as session:
        row = session.get(SqlTopic, (current_workspace_id(), "alice", "old"))
        body = json.loads(row.body)
        body["open"] = [{"kind": "Promised", "text": "Call Dana"}]
        row.body = json.dumps(body)
    first, again = store.get("alice", "old"), store.get("alice", "old")
    assert first is not None and again is not None
    assert first.open[0]["id"] and first.open[0]["id"] == again.open[0]["id"]

    heard: list[tuple[str, object, object]] = []

    def broken(owner: str, before: object, after: object) -> None:
        raise RuntimeError("listener bug")

    def listener(owner: str, before: object, after: object) -> None:
        heard.append((owner, before, after))

    # The composition root registers its own (the Lookout's promise signals); keep those.
    wired = list(store_module._listeners)
    register_topic_listener(broken)
    register_topic_listener(listener)
    try:
        result = store.update(
            "alice",
            [{"key": "old", "open": [{"kind": "Promised", "text": "Call Dana"}]}],
            zone=UTC,
            now=NOW,
        )
        assert result["written"][0]["open"][0]["id"] == first.open[0]["id"]  # kept on write
        assert store.mark_new("alice", "old", "Dana replied", NOW)
        store.update("alice", [{"key": "fresh", "title": "Fresh"}], zone=UTC, now=NOW)
    finally:
        unregister_topic_listener(broken)
        unregister_topic_listener(listener)
    assert [(o, b is None, a.key) for o, b, a in heard] == [
        ("alice", False, "old"),
        ("alice", False, "old"),
        ("alice", True, "fresh"),
    ]
    assert heard[1][2].since == "Dana replied" and heard[1][1].since == ""
    assert store_module._listeners == wired


def test_a_lookout_post_is_where_the_topic_stands_until_a_newer_write(
    store: SqlAlchemyTopicStore,
) -> None:
    now = int(time.time())
    store.update(
        "alice",
        [{"key": "trip", "title": "Trip pages", "left_off": "Three pages built."}],
        zone=UTC,
        now=now - 3600,
    )
    pass_started = now - 60  # a quiet pass began reading before the Lookout posted
    draft = {"item_id": "m1", "artifact_id": "f1", "title": "reply.md", "at": 1}
    message = "## Topic update\nDana needs your answer tomorrow. I wrote one reply for her."
    assert store.set_status("alice", "trip", "Reply to Dana drafted", draft, message)
    topic = store.get("alice", "trip")
    assert topic is not None and topic.left_off == "Dana needs your answer tomorrow."
    assert topic.events[0]["line"] == topic.left_off and topic.last_touched_at >= now
    assert topic_card(topic)["data"]["draft"] == {"artifact_id": "f1", "title": "reply.md"}
    # The pass's older view never replaces where it stands; the rest of its write lands.
    store.update(
        "alice",
        [
            {
                "key": "trip",
                "left_off": "Three pages built.",
                "status_line": "Pick a page",
                "open": [{"kind": "Open", "text": "Pick a page"}],
            }
        ],
        zone=UTC,
        view_at=pass_started,
    )
    topic = store.get("alice", "trip")
    assert topic is not None and topic.left_off == "Dana needs your answer tomorrow."
    assert topic.status_line == "Reply to Dana drafted" and topic.open[0]["text"] == "Pick a page"
    # The Conversation takes the draft up: where it stands moves on, the card's draft row goes.
    store.update(
        "alice", [{"key": "trip", "left_off": "Reply sent."}], zone=UTC, now=int(time.time()) + 1
    )
    topic = store.get("alice", "trip")
    assert topic is not None and topic_card(topic)["data"]["draft"] is None
