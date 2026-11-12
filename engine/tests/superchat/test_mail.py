"""Mail as a Lookout source (ADR 0039): the file mailbox, triage by the Lookout's rules, a poll."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.server import work_queue
from omnigent.superchat.lookout.judge import hint_of
from omnigent.superchat.lookout.prompt import build_wake, signal_lines
from omnigent.superchat.lookout.service import Lookout
from omnigent.superchat.lookout.store import SqlAlchemyLookoutStore
from omnigent.superchat.mail import poll as poll_module
from omnigent.superchat.mail.file_provider import FileMailbox, from_env, parse
from omnigent.superchat.mail.poll import MailPoll, next_poll_in
from omnigent.superchat.mail.routes import thread_view
from omnigent.superchat.mail.triage import EXCERPT_CHARS

FIXTURE = Path(__file__).resolve().parents[3] / "deployment" / "dev" / "mailbox.example.md"
#: Monday 2026-10-26 13:00 UTC: every fixture message is fresh.
NOW = int(datetime.fromisoformat("2026-10-26T13:00:00+00:00").timestamp())
TOPICS = [
    {
        "key": "morocco",
        "title": "Morocco landing pages",
        "watch": {"terms": ["Morocco", "landing pages"], "people": ["Dana Reyes"]},
    },
    {"key": "egypt", "title": "Egypt landing page", "watch": {"terms": ["Egypt"]}},
    {
        "key": "polaris",
        "title": "Polaris overview deck",
        "watch": {"terms": ["Polaris", "overview deck"]},
    },
]


class _Tasks:
    def get_owner_preferences(self, _owner: str) -> Any:
        return None


@pytest.fixture()
def lookout(db_uri: str) -> Lookout:
    service = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=None,
        own_terms=lambda _owner: [],
    )
    service.set_topic_seams(lambda _owner: TOPICS, lambda *_: True)
    return service


@pytest.fixture()
def box(tmp_path: Path) -> FileMailbox:
    path = tmp_path / "mailbox.md"
    shutil.copyfile(FIXTURE, path)
    return FileMailbox(path)


def _poll(lookout: Lookout) -> MailPoll:
    return MailPoll(
        lambda: None,
        add_signal=lookout.add_signal,
        remove_signal=lookout.remove_signal,
        check=lookout.check_item,
    )


def test_the_file_mailbox_reads_front_matter_sections_and_stable_ids(box: FileMailbox) -> None:
    keys, messages = parse(FIXTURE.read_text())
    assert keys == {"owner": "local", "me": "alex@example.com"}
    assert len(messages) == 7
    assert box.owner == "local" and box.me == "alex@example.com"
    newsletter = next(m for m in messages if m.thread == "growth-weekly-2026-43")
    assert newsletter.headers == {
        "list-id": "<growth-weekly.news.example.net>",
        "precedence": "bulk",
    }
    assert [m.id for m in parse(FIXTURE.read_text())[1]] == [m.id for m in messages]
    morocco = box.thread("morocco-landing-pages")
    assert morocco is not None and len(morocco.messages) == 3
    assert morocco.messages[0].sender.startswith("Alex Kim")  # oldest first
    # A section without a Date (or with one lacking a time zone) is skipped.
    assert parse("## x\nFrom: a@example.com\nThread: t\nDate: 2026-10-26T09:00\n\nhi")[1] == []
    assert from_env({}) is None and from_env({"OMNIGENT_DEV_MAILBOX": "  "}) is None


def test_the_fixture_triages_as_documented(lookout: Lookout, box: FileMailbox) -> None:
    decisions, seen = _poll(lookout).poll_once(box, set(), NOW)
    outcome = {d.thread: (d.action, d.reason) for d in decisions}
    assert outcome["morocco-landing-pages"][0] == "signal"  # (a) and (e): one signal
    assert outcome["growth-weekly-2026-43"] == ("drop", "noise: list")  # (b)
    assert outcome["drive-share-polaris"] == ("drop", "noise: no_reply")  # (c)
    assert outcome["polaris-numbers"][0] == "signal"  # (d) by its watched terms
    # (f) matches no topic: code no longer drops it, the Judge decides (ADR 0040).
    assert outcome["book-club-thursday"] == ("signal", "no topic: no topic match")
    assert len(seen) == 7

    waiting = {s.dedupe_key: s for s in lookout.store.waiting("local")}
    assert set(waiting) == {
        "mail:morocco-landing-pages",
        "mail:polaris-numbers",
        "mail:book-club-thursday",
    }
    assert waiting["mail:book-club-thursday"].topic_key is None
    # A topic match is handed on as the code's hint.
    assert waiting["mail:polaris-numbers"].detail["match_strength"] in ("medium", "weak")
    morocco = waiting["mail:morocco-landing-pages"]
    assert morocco.kind == "mail" and morocco.urgency == "normal"
    assert morocco.topic_key == "morocco" and morocco.detail["direct"] is True
    # The newest of Dana's two messages is the signal; the excerpt is short and unquoted.
    assert "FAQ mention the Egypt" in morocco.detail["excerpt"]
    assert morocco.detail["new_messages"] == 2
    assert {"sender", "subject", "thread", "date"} <= set(morocco.detail)
    assert waiting["mail:polaris-numbers"].topic_key == "polaris"
    assert all(len(s.detail["excerpt"]) <= EXCERPT_CHARS for s in waiting.values())
    # The wake run is told the thread and to read it, then draft, never send.
    text = signal_lines([morocco], {"morocco": "Morocco landing pages"})
    assert "thread id: morocco-landing-pages" in text and "from: Dana Reyes" in text
    assert "mail_read_thread" in build_wake(text, "(none)", urgent=False)

    # Nothing new: nothing happens.
    assert _poll(lookout).poll_once(box, seen, NOW)[0] == []


def test_a_newer_mail_updates_the_thread_signal_and_a_reply_removes_it(
    lookout: Lookout, box: FileMailbox
) -> None:
    _, seen = _poll(lookout).poll_once(box, set(), NOW)
    with box.path.open("a") as handle:
        handle.write(
            "\n## Sam again\nFrom: Sam Ortiz <sam@example.org>\nTo: alex@example.com\n"
            "Date: 2026-10-26T13:30:00+01:00\nSubject: Re: Numbers for the Polaris overview deck\n"
            "Thread: polaris-numbers\n\nAlso the Polaris churn figure, please.\n"
            "\n## Alex answers Dana\nFrom: Alex Kim <alex@example.com>\n"
            "To: dana@example.org\nDate: 2026-10-26T13:40:00+01:00\n"
            "Subject: Re: Morocco landing pages: three versions\nThread: morocco-landing-pages\n\n"
            "Keep the long form.\n"
        )
    decisions, _ = _poll(lookout).poll_once(box, seen, NOW + 3600)
    assert {(d.thread, d.action) for d in decisions} == {
        ("polaris-numbers", "signal"),
        ("morocco-landing-pages", "answered"),
    }
    waiting = [
        s for s in lookout.store.waiting("local") if s.dedupe_key != "mail:book-club-thursday"
    ]
    assert [s.dedupe_key for s in waiting] == ["mail:polaris-numbers"]  # still one
    assert "churn" in waiting[0].detail["excerpt"]
    # A settled mail signal is deleted, excerpt and all (ADR 0039).
    lookout.store.settle([s.id for s in lookout.store.waiting("local")], "stale")
    with lookout.store._session("t") as session:
        from sqlalchemy import select

        from omnigent.db.db_models import SqlLookoutSignal

        assert session.execute(select(SqlLookoutSignal)).scalars().all() == []


@pytest.mark.asyncio
async def test_mail_without_a_topic_is_kept_for_the_judge_with_its_hint(lookout: Lookout) -> None:
    lookout.set_topic_seams(lambda _owner: [], lambda *_: True)
    lookout.add_signal(
        {
            "owner": "local",
            "kind": "mail",
            "subject": "Lunch",
            "dedupe_key": "mail:t1",
            "due_at": NOW,
            "stale_after": NOW + 86400,
            "detail": {
                "source": "mail",
                "thread": "t1",
                "sender": "x@example.org",
                "direct": True,
            },
        }
    )
    lookout.add_signal(
        {
            "owner": "local",
            "kind": "mail",
            "subject": "Other",
            "dedupe_key": "mail:t2",
            "due_at": NOW,
            "stale_after": NOW + 86400,
            "detail": {"source": "mail", "thread": "t2", "sender": "y@example.org"},
        }
    )
    kept = await lookout._settle("local", lookout.store.waiting("local"), [], NOW)
    assert sorted(s.dedupe_key for s in kept) == ["mail:t1", "mail:t2"]
    # The code's hint: a thread the person wrote in is medium, an unmatched one has none.
    by_key = {s.dedupe_key: s for s in kept}
    assert hint_of(by_key["mail:t1"]) == ("medium", "a thread the person wrote in")
    assert hint_of(by_key["mail:t2"]) == (None, "no topic match")
    assert all(s.detail["match_reason"] == "no topic match" for s in kept)  # matched once


@pytest.mark.asyncio
async def test_the_poll_job_defers_itself_with_its_cursor(
    lookout: Lookout, box: FileMailbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    poll = _poll(lookout)
    poll._provider = lambda: box
    monkeypatch.setenv("OMNIGENT_MAIL_POLL_SECONDS", "30")
    job = SimpleNamespace(owner="local", payload={"seen": []})
    result = await poll.run_job(job)
    assert isinstance(result, work_queue.Defer)
    assert len(result.payload["seen"]) == 7
    poll._provider = lambda: None  # the env var unset: polling stops
    assert await poll.run_job(job) is None
    assert poll_module.job_key("local") == "mail_poll:local"


def test_the_poll_pace_follows_working_hours() -> None:
    monday_10 = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()
    sunday_10 = datetime.fromisoformat("2026-10-25T10:00:00+00:00").timestamp()
    assert 510 <= next_poll_in(monday_10, "UTC", {}) <= 690
    assert 3060 <= next_poll_in(sunday_10, "UTC", {}) <= 4140
    assert next_poll_in(sunday_10, "UTC", {"OMNIGENT_MAIL_POLL_SECONDS": "30"}) == 30


def test_the_read_tool_view_marks_the_persons_own_messages(box: FileMailbox) -> None:
    view = thread_view(box, "morocco-landing-pages")
    assert view is not None
    assert [m["from_the_person"] for m in view["messages"]] == [True, False, False]
    assert thread_view(box, "nope") is None
