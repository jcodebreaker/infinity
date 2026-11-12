"""The outbox (ADR 0036): a scheduled run's turn writes a labelled message and one row."""

from __future__ import annotations

import json
from types import SimpleNamespace

from omnigent.context.labels import (
    CONTEXT_MODE_LABEL,
    PROACTIVE_LABEL_KEY,
    SCHEDULED_HELPER_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.outbox.cut import cut
from omnigent.superchat.outbox.record import Outbox
from omnigent.superchat.outbox.store import WEB_CHANNEL, SqlAlchemyOutboxStore

SUPER = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}


def test_cut_takes_the_heading_or_first_sentence() -> None:
    assert cut("## Polaris deal moved\n\nLegal **signed** the NDA today. More later.") == (
        "Polaris deal moved",
        "Legal signed the NDA today.",
    )
    assert cut("The deck is ready. I kept your colours.") == (
        "The deck is ready.",
        "I kept your colours.",
    )
    title, line = cut("word " * 60)
    assert len(title) <= 80 and title.endswith("…") and line == ""


class _Tasks:
    def get(self, _task_id: str) -> SimpleNamespace:
        return SimpleNamespace(kind="topic_watch")


def _message(store: SqlAlchemyConversationStore, chat: str, role: str, text: str, rid: str):
    data = (
        MessageData(role="user", content=[{"type": "input_text", "text": text}])
        if role == "user"
        else MessageData(
            role="assistant", agent="a", content=[{"type": "output_text", "text": text}]
        )
    )
    return store.append(chat, [NewConversationItem(type="message", response_id=rid, data=data)])[0]


def test_a_scheduled_runs_turn_is_labelled_and_read_once(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    chat = conversation_store.create_conversation(labels=SUPER).id
    helper = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=chat,
        labels={
            **SUPER,
            SCHEDULED_HELPER_LABEL_KEY: "b" * 32,
            PROACTIVE_LABEL_KEY: json.dumps(
                {"urgency": "now", "topic_key": "polaris", "sender": "ana@example.com"}
            ),
        },
    ).id
    store = SqlAlchemyOutboxStore(db_uri)
    outbox = Outbox(store, conversation_store, _Tasks())

    notice = _message(conversation_store, chat, "user", "result", "turn_1")
    outbox.notice(chat, notice.id, {"helper_id": helper})
    first = _message(conversation_store, chat, "assistant", "Checking.", "resp_1")
    outbox.message(chat, first)
    reply = _message(
        conversation_store, chat, "assistant", "# NDA signed\nLegal is done.", "resp_1"
    )
    outbox.message(chat, reply)
    # The person's next turn is theirs: its message is not labelled.
    _message(conversation_store, chat, "user", "thanks", "turn_2")
    later = _message(conversation_store, chat, "assistant", "You're welcome.", "resp_2")
    outbox.message(chat, later)

    assert store.labels(chat, [first.id, reply.id, later.id]) == {
        reply.id: {"kind": "topic_watch", "topic_key": "polaris", "urgency": "now"}
    }
    [row] = store.list_after("local", None)
    assert (row.item_id, row.title, row.line) == (reply.id, "NDA signed", "Legal is done.")
    assert store.unread("local") == {
        "count": 1,
        "first_item_id": reply.id,
        "last_seen_item_id": None,
    }
    assert store.advance(WEB_CHANNEL, "local", reply.id)
    assert store.unread("local")["count"] == 0
    assert store.not_useful("local", reply.id, "not now") is not None
    assert store.list_mutes("local") == [{"topic_key": "polaris", "sender": "ana@example.com"}]


def test_a_row_another_replica_opened_is_bound_from_the_store(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    chat = conversation_store.create_conversation(labels=SUPER).id
    helper = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=chat,
        labels={**SUPER, SCHEDULED_HELPER_LABEL_KEY: "c" * 32},
    ).id
    store = SqlAlchemyOutboxStore(db_uri)
    here = Outbox(store, conversation_store, _Tasks())
    there = Outbox(store, conversation_store, _Tasks())

    notice = _message(conversation_store, chat, "user", "result", "turn_1")
    there.notice(chat, notice.id, {"helper_id": helper})
    reply = _message(conversation_store, chat, "assistant", "Done.", "resp_1")
    here.message(chat, reply)

    assert store.labels(chat, [reply.id]) == {
        reply.id: {"kind": "topic_watch", "urgency": "today"}
    }


def test_a_notice_steered_into_a_running_turn_labels_nothing(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    chat = conversation_store.create_conversation(labels=SUPER).id
    helper = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=chat,
        labels={**SUPER, SCHEDULED_HELPER_LABEL_KEY: "b" * 32},
    ).id
    conversation_store.set_session_live_status(chat, "running")
    store = SqlAlchemyOutboxStore(db_uri)
    outbox = Outbox(store, conversation_store, _Tasks())
    notice = _message(conversation_store, chat, "user", "result", "turn_1")
    outbox.notice(chat, notice.id, {"helper_id": helper})
    outbox.message(chat, _message(conversation_store, chat, "assistant", "Done.", "resp_1"))
    assert store.list_after("local", None) == []


def test_engine_code_can_wake_the_conversation_with_its_own_label(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    chat = conversation_store.create_conversation(labels=SUPER).id
    store = SqlAlchemyOutboxStore(db_uri)
    outbox = Outbox(store, conversation_store, _Tasks())
    notice = _message(conversation_store, chat, "user", "signal", "turn_1")
    context = {"proactive": {"kind": "topic_watch", "topic_key": "polaris", "urgency": "soon"}}
    outbox.notice(chat, notice.id, context)
    reply = _message(conversation_store, chat, "assistant", "Ana replied.", "resp_1")
    outbox.message(chat, reply)
    # A bad urgency is the default, never repaired into something else.
    assert store.labels(chat, [reply.id]) == {
        reply.id: {"kind": "topic_watch", "topic_key": "polaris", "urgency": "today"}
    }


def test_a_posted_event_cannot_claim_to_be_proactive() -> None:
    from omnigent.server.routes._sessions.orchestration import _drop_engine_only_context
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        context={"proactive": {"kind": "topic_watch"}, "open_artifact": {"artifact_id": "a"}},
    )
    _drop_engine_only_context(body)
    assert body.context == {"open_artifact": {"artifact_id": "a"}}
