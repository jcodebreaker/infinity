"""Tests for :class:`SqlAlchemyDailyNoteStore` and the daily-note merge rules."""

from __future__ import annotations

import pytest

from omnigent.entities.daily_note import merge_writer
from omnigent.superchat.daily_notes.store import SqlAlchemyDailyNoteStore


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyDailyNoteStore:
    return SqlAlchemyDailyNoteStore(db_uri)


def test_writer_replaces_untouched_sections(store: SqlAlchemyDailyNoteStore) -> None:
    store.write_sections("alice", "2026-10-04", {"talked_about": "- budget"})
    note = store.write_sections("alice", "2026-10-04", {"talked_about": "- budget\n- hiring"})
    assert note.sections["talked_about"] == "- budget\n- hiring"
    assert not note.edited_by_person
    assert store.get("bob", "2026-10-04") is None


def test_person_edit_wins_and_writer_only_appends(store: SqlAlchemyDailyNoteStore) -> None:
    store.write_sections("alice", "2026-10-04", {"decisions": "- ship friday"})
    edited = store.person_edit("alice", "2026-10-04", {"decisions": "- ship monday"})
    assert edited.edited_by_person and edited.edited_sections == ["decisions"]
    note = store.write_sections(
        "alice",
        "2026-10-04",
        {"decisions": "- ship friday\n- Ship Monday\n- hire one", "promised": "- send deck"},
    )
    # the person's line is untouched; only genuinely new lines are appended
    assert note.sections["decisions"] == "- ship monday\n- ship friday\n- hire one"
    assert note.sections["promised"] == "- send deck"  # untouched section still written


def test_merge_ignores_unknown_keys() -> None:
    merged = merge_writer({"decisions": "a"}, [], {"nope": "x"})
    assert merged["decisions"] == "a" and "nope" not in merged


def test_quiet_pass_cap_and_finalize_once(store: SqlAlchemyDailyNoteStore) -> None:
    session = "a" * 32
    for _ in range(2):
        assert store.claim_quiet_pass(
            "alice", "2026-10-04", max_per_day=2, parent_session_id=session
        )
    assert not store.claim_quiet_pass(
        "alice", "2026-10-04", max_per_day=2, parent_session_id=session
    )
    assert [n.owner for n in store.pending_finalize()] == ["alice"]
    assert store.claim_finalize("alice", "2026-10-04")
    assert not store.claim_finalize("alice", "2026-10-04")  # the lease is held
    assert [n.owner for n in store.pending_finalize()] == ["alice"]  # claimed, not yet done
    store.complete_pass("alice", "2026-10-04", "finalized_at")
    assert not store.claim_finalize("alice", "2026-10-04")
    assert store.pending_finalize() == []


def test_list_range_newest_first(store: SqlAlchemyDailyNoteStore) -> None:
    for day in ("2026-10-02", "2026-10-03", "2026-10-04"):
        store.write_sections("alice", day, {"talked_about": day})
    days = [n.note_date for n in store.list_range("alice", date_from="2026-10-03", date_to=None)]
    assert days == ["2026-10-04", "2026-10-03"]


def test_a_lease_older_than_the_timeout_can_be_claimed_again(
    store: SqlAlchemyDailyNoteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.superchat.daily_notes import store as note_store

    assert store.claim_finalize("alice", "2026-10-04")
    assert not store.claim_finalize("alice", "2026-10-04")
    later = note_store.now_epoch() + note_store.PASS_LEASE_SECONDS + 1
    monkeypatch.setattr(note_store, "now_epoch", lambda: later)
    assert store.claim_finalize("alice", "2026-10-04")


def test_nightly_passes_share_the_lease_and_need_content(
    store: SqlAlchemyDailyNoteStore,
) -> None:
    parent = "b" * 32
    store.write_sections("alice", "2026-10-04", {}, parent_session_id=parent)
    store.claim_finalize("alice", "2026-10-04")
    store.complete_pass("alice", "2026-10-04", "finalized_at")
    window = {"finalized_after": 0, "finalized_before": 2**31 - 1}
    assert store.pending_nightly(**window) == []  # empty sections are skipped
    store.write_sections("alice", "2026-10-04", {"talked_about": "- x"})
    assert [n.note_date for n in store.pending_nightly(**window)] == ["2026-10-04"]
    assert store.claim_pass("alice", "2026-10-04", "dreamed_at")
    assert not store.claim_pass("alice", "2026-10-04", "people_at")  # one lease per note
    store.complete_pass("alice", "2026-10-04", "dreamed_at")
    assert store.claim_pass("alice", "2026-10-04", "people_at")
    note = store.get("alice", "2026-10-04")
    assert note.dreamed_at is not None and note.people_at is None and note.claimed_at is not None
