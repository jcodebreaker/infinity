"""The Lookout (ADR 0038): a signal wakes it, code checks, one run, its Result posted by code."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE
from omnigent.entities import MessageData, NewConversationItem, OwnerPreferences
from omnigent.entities.conversation import FunctionCallOutputData
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat import feature
from omnigent.superchat.lookout import service as service_module
from omnigent.superchat.lookout.prompt import parse_result, parse_statuses
from omnigent.superchat.lookout.service import Lookout
from omnigent.superchat.lookout.signals import due_words, promise_times, zone
from omnigent.superchat.lookout.store import SqlAlchemyLookoutStore
from omnigent.superchat.muse import MUSE_LABEL_KEY
from omnigent.superchat.outbox.record import Outbox
from omnigent.superchat.outbox.store import SqlAlchemyOutboxStore

SUPER = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE, MUSE_LABEL_KEY: "true"}
TZ = zone("Europe/Paris")


def test_time_rules() -> None:
    # A promise due on a Saturday is reminded Friday 9:00, stale at the end of Saturday.
    due_at, stale = promise_times(date(2026, 10, 31), TZ)
    assert time.strftime("%a %H:%M", time.gmtime(due_at)) == "Fri 08:00"  # 9:00 Paris (CET)
    assert stale - due_at == 39 * 3600


def test_the_result_says_its_value_and_nothing_posts_nothing() -> None:
    assert parse_result("kind_of_value: draft\nI drafted the reply. Send it?") == (
        "draft",
        "I drafted the reply. Send it?",
    )
    assert parse_result("kind_of_value: none") == ("none", None)
    result = "kind_of_value: draft\nstatus egypt-page: Dana is waiting for the link\nSend it?"
    assert parse_result(result) == ("draft", "Send it?")
    assert parse_statuses(result) == {"egypt-page": "Dana is waiting for the link"}


def test_a_new_line_words_its_date() -> None:
    today = date(2026, 10, 25)  # a Sunday
    assert due_words("2026-10-25", today) == "due today"
    assert due_words("2026-10-26", today) == "due tomorrow"
    assert due_words("2026-10-30", today) == "due Friday"
    assert due_words("2026-10-23", today) == "was due Friday"
    assert due_words("2026-11-20", today) == "due 20 Nov"
    assert parse_result("NOTHING") == ("none", None)
    assert parse_result("") == ("none", None)


class _Rhythm:
    """The rhythm seam: one message per topic, and every ``sent`` it hears."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.recorded: list[tuple[str, str, str | None]] = []

    def good_moment(self, _owner: str, _at: Any, _state: str, **kw: Any) -> bool:
        return kw.get("topic_key") not in self.busy

    def next_good_slot(self, _owner: str, at: Any, _state: str, **_kw: Any) -> Any:
        return at.replace(hour=23)

    def record(self, _owner: str, item_id: str, event: str, _at: Any, **kw: Any) -> bool:
        self.recorded.append((item_id, event, kw.get("state")))
        self.topics = kw.get("topic_keys")
        return True


class _Tasks:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def get_owner_preferences(self, owner: str) -> OwnerPreferences:
        # A one-minute quiet window in the small hours: the test runs inside its hours.
        return OwnerPreferences(
            user_id=owner, quiet_start="03:00", quiet_end="03:01", timezone="UTC"
        )

    def get(self, task_id: str) -> Any:
        return self.rows.get(task_id)

    def create(self, *, scheduled_task_id: str, **fields: Any) -> Any:
        row = SimpleNamespace(id=scheduled_task_id, workspace_id=0, **fields)
        self.rows[row.id] = row
        return row

    def update(self, task_id: str, **fields: Any) -> Any:
        vars(self.rows[task_id]).update(fields)
        return self.rows[task_id]


@pytest.mark.asyncio
async def test_a_live_refresh_is_checked_run_once_and_posted_by_code(
    conversation_store: SqlAlchemyConversationStore, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = conversation_store.create_conversation(agent_id=uuid.uuid4().hex, labels=SUPER).id
    outbox_store = SqlAlchemyOutboxStore(db_uri)
    outbox = Outbox(outbox_store, conversation_store, None)
    monkeypatch.setattr(feature, "_proactive_recorder", outbox.record)
    linked: list[tuple[str, str, str]] = []
    monkeypatch.setattr(feature, "_file_linker", lambda *args: linked.append(args) is None)
    fired: list[str] = []

    def launch(task: Any, wake_id: str) -> bool:
        fired.append(task.id)
        return True

    monkeypatch.setattr(service_module, "launch_wake", launch)
    rhythm = _Rhythm()

    tasks = _Tasks()
    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=tasks,
        conversations=conversation_store,
        own_terms=lambda _owner: [],
    )
    lookout.set_rhythm(rhythm)
    topic = {"key": "q3-plan", "title": "Q3 plan", "watch": {"refs": ["q3.html"]}}
    statuses: list[tuple[Any, ...]] = []
    lookout.set_topic_seams(
        lambda _owner: [topic], lambda *_: True, lambda *args: statuses.append(args)
    )
    now = int(time.time())
    for _ in range(3):  # three refreshes of one page are one signal
        lookout.add_signal(
            {
                "owner": "local",
                "kind": "library_file",
                "subject": "Q3 plan",
                "dedupe_key": "file:g1",
                "due_at": now - 300,
                "stale_after": 0,
                "conversation_id": chat,
                "detail": {"refs": ["q3.html"], "excerpt": "Revenue moved to 4.2M"},
            }
        )
    lookout.store.set_next_wake("local", now)  # past the settle delay

    assert (await lookout.wake("local")).startswith("launched 1")
    task = tasks.rows[fired[0]]
    assert task.kind == "lookout_wake" and task.state == "paused"
    assert getattr(task, "max_cost_usd", None) is None  # no per-run cost cap (ADR 0040)
    # The run starts: its opening message carries the claimed signal, fenced as data.
    assert "Revenue moved to 4.2M" in lookout.run_brief(task)
    run = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=chat, labels=SUPER
    ).id
    lookout.on_run_result(task, run, "running")
    conversation_store.append(
        run,
        [
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(
                    role="assistant",
                    agent="builder-model",
                    content=[
                        {
                            "type": "output_text",
                            "text": "kind_of_value: analysis\n"
                            "status q3-plan: Slide 3 update is drafted\n"
                            "Revenue is now 4.2M, so slide 3 "
                            "is wrong; I drafted the update. Open the draft?",
                        }
                    ],
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="r1",
                data=FunctionCallOutputData(
                    call_id="c1",
                    output='{"type": "artifact", "id": "draft-1", "name": "q3-update.html"}',
                ),
            ),
        ],
    )
    wake = lookout.store.wake_for_task(task.id, run_conversation_id=run)
    assert wake is not None and wake.status == "running"
    await lookout._post(wake, task, run)  # what the succeeded edge schedules

    posted = [
        i
        for i in conversation_store.list_items(chat, limit=20).data
        if isinstance(i.data, MessageData) and i.data.role == "assistant"
    ]
    assert len(posted) == 1 and posted[0].response_id.startswith("recorded_lookout_")
    assert posted[0].data.content[0]["text"].startswith("Revenue is now 4.2M")
    labels = outbox_store.labels(chat, [posted[0].id])
    assert labels[posted[0].id] == {
        "kind": "lookout_wake",
        "topic_key": "q3-plan",
        "urgency": "today",
    }
    assert outbox_store.unread("local")["count"] == 1
    assert outbox_store.unseen("local", posted[0].id)
    # The topic's status line is the run's, and Home can lead with the draft it prepared.
    [(owner, key, line, prepared, message)] = statuses
    assert (owner, key, line) == ("local", "q3-plan", "Slide 3 update is drafted")
    assert message.startswith("Revenue is now 4.2M")  # where the topic stands now
    assert prepared["item_id"] == posted[0].id and prepared["artifact_id"] == "draft-1"
    assert prepared["title"] == "q3-update.html"
    assert lookout.store.waiting("local") == [] and lookout.store.in_flight("local") == []
    # The draft the run saved is the message's card; the rhythm hears it went out.
    assert linked == [(chat, posted[0].id, "draft-1")]
    assert rhythm.recorded == [(posted[0].id, "sent", "not")]
    assert rhythm.topics == ["q3-plan"]  # every topic the message covers
    # The rhythm owns the per-topic limit: a new refresh on the topic is held, not run.
    rhythm.busy.add("q3-plan")
    lookout.add_signal(
        {
            "owner": "local",
            "kind": "library_file",
            "subject": "Q3 plan",
            "dedupe_key": "file:g1",
            "due_at": now - 60,
            "stale_after": 0,
            "detail": {"refs": ["q3.html"]},
        }
    )
    lookout.store.set_next_wake("local", now)
    assert await lookout.wake("local") == "held:rhythm"
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_the_queue_job_runs_only_a_claimed_wake_and_a_failure_releases_it(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.scheduled import helper_fire

    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=None,
        own_terms=lambda _owner: [],
    )
    now = int(time.time())
    signal_id = lookout.add_signal(
        {
            "owner": "local",
            "kind": "promise",
            "subject": "Send the deck",
            "dedupe_key": "promise:board:p1",
            "due_at": now - 60,
            "stale_after": now + 3600,
            "topic_key": "board",
        }
    )
    wake_id, claimed = lookout.store.claim(
        owner="local",
        kind="wake",
        signal_ids=[signal_id],
        urgency="today",
        topic_keys=["board"],
        conversation_id=None,
        scheduled_task_id=None,
        now=now,
        state="not",
    )
    assert claimed == 1 and lookout.store.get_wake(wake_id).state == "not"
    job = SimpleNamespace(key=wake_id, payload={"wake_id": wake_id})

    async def broken(_job: Any) -> Any:
        raise RuntimeError("launch failed")

    monkeypatch.setattr(helper_fire, "_FIRE_HANDLER", [broken])
    with pytest.raises(RuntimeError):
        await lookout.run_job(job)
    assert lookout.store.get_wake(wake_id).status == "released"
    assert [s.id for s in lookout.store.waiting("local")] == [signal_id]  # waits again

    # A released wake's job (a retry after the failure) starts nothing.
    started: list[Any] = []

    async def fire(job: Any) -> Any:
        started.append(job)
        return "run-1"

    monkeypatch.setattr(helper_fire, "_FIRE_HANDLER", [fire])
    assert await lookout.run_job(job) is None and started == []


def test_dated_promises_follow_topic_writes(db_uri: str) -> None:
    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=None,
        own_terms=lambda _owner: [],
    )
    far = date.fromordinal(datetime.now(UTC).date().toordinal() + 30)
    later = date.fromordinal(far.toordinal() + 7)
    line = {"id": "p1", "kind": "Promised", "text": "Send the deck to the board"}

    def topic(*lines: dict[str, Any], status: str = "open") -> dict[str, Any]:
        return {"key": "board", "status": status, "open": list(lines)}

    lookout.on_topic_write("local", None, topic({**line, "due": far.isoformat()}))
    (signal,) = lookout.store.waiting("local")
    assert signal.dedupe_key == "promise:board:p1" and signal.topic_key == "board"
    assert signal.due_at == promise_times(far, zone("UTC"))[0]
    # Reworded with the same id and date: the reminder is kept, not added twice.
    lookout.on_topic_write(
        "local",
        topic({**line, "due": far.isoformat()}),
        topic({**line, "text": "Send the board deck", "due": far.isoformat()}),
    )
    assert len(lookout.store.waiting("local")) == 1
    # The date moves: the one reminder moves with it.
    lookout.on_topic_write(
        "local", topic({**line, "due": far.isoformat()}), topic({**line, "due": later.isoformat()})
    )
    (moved,) = lookout.store.waiting("local")
    assert moved.id == signal.id and moved.due_at == promise_times(later, zone("UTC"))[0]
    # The topic closes: the reminder goes.
    lookout.on_topic_write(
        "local", topic({**line, "due": later.isoformat()}), topic(status="closed")
    )
    assert lookout.store.waiting("local") == []


def test_a_capped_run_on_an_unpriced_model_is_not_posted_and_releases_at_once(
    db_uri: str,
) -> None:
    usage = {"input_tokens": 900, "output_tokens": 40}  # spent, never priced
    conversations = SimpleNamespace(
        get_conversation=lambda _id: SimpleNamespace(session_usage=usage)
    )
    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=conversations,
        own_terms=lambda _owner: [],
    )
    now, task_id, run = int(time.time()), uuid.uuid4().hex, uuid.uuid4().hex
    signal_id = lookout.add_signal(
        {
            "owner": "local",
            "kind": "promise",
            "subject": "Send the deck",
            "dedupe_key": "promise:board:p1",
            "due_at": now - 60,
            "stale_after": now + 3600,
            "topic_key": "board",
        }
    )
    wake_id, _ = lookout.store.claim(
        owner="local",
        kind="wake",
        signal_ids=[signal_id],
        urgency="today",
        topic_keys=["board"],
        conversation_id=None,
        scheduled_task_id=task_id,
        now=now,
    )
    task = SimpleNamespace(
        id=task_id, kind="lookout_wake", user_id=None, parent_session_id=None, max_cost_usd=0.5
    )
    lookout.on_run_result(task, run, "running")
    lookout.on_run_result(task, run, "succeeded")
    assert lookout.store.get_wake(wake_id).status == "released"
    assert [s.id for s in lookout.store.waiting("local")] == [signal_id]


def test_a_blocked_run_posts_nothing_and_a_signal_is_given_up_after_two_tries(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    marked: list[tuple[str, str]] = []
    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=conversation_store,
        own_terms=lambda _owner: [],
    )
    lookout.set_topic_seams(lambda _o: [], lambda _o, key, line, _at: marked.append((key, line)))
    chat = conversation_store.create_conversation(agent_id=uuid.uuid4().hex, labels=SUPER).id
    now = int(time.time())
    signal_id = lookout.add_signal(
        {
            "owner": "local",
            "kind": "promise",
            "subject": "Send the deck",
            "dedupe_key": "promise:board:p1",
            "due_at": now - 60,
            "stale_after": now + 3600,
            "topic_key": "board",
        }
    )
    for attempt in (1, 2):
        wake_id, _ = lookout.store.claim(
            owner="local",
            kind="wake",
            signal_ids=[signal_id],
            urgency="today",
            topic_keys=["board"],
            conversation_id=chat,
            scheduled_task_id=None,
            now=now,
        )
        run = conversation_store.create_conversation(
            kind="sub_agent", parent_conversation_id=chat, labels=SUPER
        ).id
        lookout.store.bind_run(wake_id, run)
        conversation_store.append(
            run,
            [
                NewConversationItem(
                    type="function_call_output",
                    response_id="r1",
                    data=FunctionCallOutputData(
                        call_id="c1",
                        output="Blocked by the session cost-budget policy: spend $13.72 "
                        "reached the $0.50 limit.",
                    ),
                ),
                NewConversationItem(
                    type="message",
                    response_id="r1",
                    data=MessageData(
                        role="assistant",
                        agent="builder",
                        content=[
                            {
                                "type": "output_text",
                                "text": "kind_of_value: reminder\nYou've hit the $0.50 cost "
                                "budget. Raise the budget if you want me to continue.",
                            }
                        ],
                    ),
                ),
            ],
        )
        wake = lookout.store.get_wake(wake_id)
        asyncio.run(lookout._post(wake, SimpleNamespace(kind="lookout_wake"), run))
        assert lookout.store.get_wake(wake_id).status == "released"
        waiting = [s.id for s in lookout.store.waiting("local")]
        assert waiting == ([signal_id] if attempt == 1 else [])  # retried once, then given up
    posted = [
        i
        for i in conversation_store.list_items(chat, limit=20).data
        if isinstance(i.data, MessageData) and i.data.role == "assistant"
    ]
    assert posted == []  # never the policy's words
    assert marked == [("board", "Send the deck")]  # given up: the topic shows it as New
    # Nothing waits: the Lookout never wakes on an empty tray.
    assert lookout.store.due_owners(now + 10**6) == []


def test_a_read_that_quotes_a_policy_notice_does_not_drop_a_good_result(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    # A recall over an old chat quoted a budget notice: the run was never refused, its Result
    # is clean, so it is posted (the train run lost two wakes to this).
    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=_Tasks(),
        conversations=conversation_store,
        own_terms=lambda _owner: [],
    )
    chat = conversation_store.create_conversation(agent_id=uuid.uuid4().hex, labels=SUPER).id
    now = int(time.time())
    signal_id = lookout.add_signal(
        {
            "owner": "local",
            "kind": "promise",
            "subject": "Send the deck",
            "dedupe_key": "promise:board:p1",
            "due_at": now - 60,
            "stale_after": now + 3600,
            "topic_key": "board",
        }
    )
    wake_id, _ = lookout.store.claim(
        owner="local",
        kind="wake",
        signal_ids=[signal_id],
        urgency="today",
        topic_keys=["board"],
        conversation_id=chat,
        scheduled_task_id=None,
        now=now,
    )
    run = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=chat, labels=SUPER
    ).id
    lookout.store.bind_run(wake_id, run)
    conversation_store.append(
        run,
        [
            NewConversationItem(
                type="function_call_output",
                response_id="r1",
                data=FunctionCallOutputData(
                    call_id="c1",
                    output='{"type": "recall", "results": [{"snippet": "Assistant: You\'ve hit '
                    'the $0.50 cost budget. All model calls are blocked over budget."}]}',
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="r1",
                data=MessageData(
                    role="assistant",
                    agent="builder",
                    content=[
                        {
                            "type": "output_text",
                            "text": "kind_of_value: draft\nThe deck is ready to send. "
                            "Review the draft",
                        }
                    ],
                ),
            ),
        ],
    )
    asyncio.run(
        lookout._post(lookout.store.get_wake(wake_id), SimpleNamespace(kind="lookout_wake"), run)
    )
    assert lookout.store.get_wake(wake_id).status == "sent"
    posted = [
        i.data.content[0]["text"]
        for i in conversation_store.list_items(chat, limit=20).data
        if isinstance(i.data, MessageData) and i.data.role == "assistant"
    ]
    assert posted == ["The deck is ready to send. Review the draft"]
