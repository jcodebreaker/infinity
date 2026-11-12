"""Store-backed orchestration tests for omnigent.superchat.activity.derive.

Covers list_chat_family / list_activities / get_activity against a real
SqlAlchemyConversationStore (sqlite, per-test file) — the wiring between
the Super Chat and its Sub-agents, plus owner/family
scoping. Pure derivation logic is covered in test_activity.py.
"""

from __future__ import annotations

import json

import pytest

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.activity.derive import (
    STATUS_DONE,
    get_activity,
    list_activities,
    list_chat_family,
    resolve_super_chat_id,
)

_MODE_LABEL = "omnigent.context.mode"
_MODE_VALUE = "superside-chat"


def _superside_chat_labels(**extra: str) -> dict[str, str]:
    return {_MODE_LABEL: _MODE_VALUE, **extra}


def _add_turn_with_tool_call(
    conv_store: SqlAlchemyConversationStore,
    conversation_id: str,
    *,
    response_id: str = "resp_1",
) -> None:
    conv_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "research auth flows"}]
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=FunctionCallData(
                    agent="brain",
                    name="memory_search",
                    arguments=json.dumps({"query": "auth flow"}),
                    call_id="call_1",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="brain",
                    content=[{"type": "output_text", "text": "Researched auth flows"}],
                ),
            ),
        ],
    )


def _build_super_chat_family(conv_store: SqlAlchemyConversationStore, *, suffix: str) -> dict:
    super_chat = conv_store.create_conversation(
        kind="default",
        title=f"Super Chat {suffix}",
        labels=_superside_chat_labels(),
    )
    sub_agent = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=super_chat.id,
        title=f"researcher:{suffix}",
        labels=_superside_chat_labels(),
    )
    _add_turn_with_tool_call(conv_store, super_chat.id)
    conv_store.append(
        sub_agent.id,
        [
            NewConversationItem(
                type="function_call",
                response_id="resp_sub",
                data=FunctionCallData(
                    agent="researcher",
                    name="memory_search",
                    arguments=json.dumps({"query": suffix}),
                    call_id="call_sub",
                ),
            ),
        ],
    )
    conv_store.set_session_live_status(sub_agent.id, "idle")
    return {"super_chat": super_chat, "sub_agent": sub_agent}


def test_resolve_super_chat_id_of_a_super_chat_is_itself(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert (
        resolve_super_chat_id(conversation_store, family["super_chat"].id)
        == family["super_chat"].id
    )


def test_resolve_super_chat_id_rejects_sub_agent_id(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert resolve_super_chat_id(conversation_store, family["sub_agent"].id) is None


def test_list_chat_family_includes_super_chat_and_sub_agent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    chats = list_chat_family(conversation_store, family["super_chat"].id)
    ids = {chat.id for chat in chats}
    assert ids == {family["super_chat"].id, family["sub_agent"].id}


def test_list_chat_family_empty_for_non_superside_chat_session(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    plain = conversation_store.create_conversation(kind="default", title="plain chat")
    assert list_chat_family(conversation_store, plain.id) == []


def test_list_activities_covers_turn_and_sub_agent(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.superchat.activity.derive._MIN_TURN_STEPS", 1)
    family = _build_super_chat_family(conversation_store, suffix="a")
    activities = list_activities(conversation_store, family["super_chat"].id)
    kinds_by_chat = {(a.kind, a.chat_id) for a in activities}
    assert ("turn", family["super_chat"].id) in kinds_by_chat
    assert ("sub_agent", family["sub_agent"].id) in kinds_by_chat
    # List view never carries per-step detail — only the detail route does.
    assert all(step.detail is None for activity in activities for step in activity.steps)


def test_list_activities_covers_the_newest_turn_beyond_the_scan_limit(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation with more items than the scan limit must still surface
    its NEWEST turn — an ascending, limit-bounded scan would instead return
    only the oldest items and silently drop the current turn."""
    import omnigent.superchat.activity.derive as activity_mod

    monkeypatch.setattr(activity_mod, "_MIN_TURN_STEPS", 1)
    monkeypatch.setattr(activity_mod, "_ITEMS_SCAN_LIMIT", 15)  # 5 turns' worth

    super_chat = conversation_store.create_conversation(
        kind="default", title="Long super chat", labels=_superside_chat_labels()
    )
    # Many old turns — well beyond the patched scan limit (20 turns * 3
    # items = 60 items, vs. a 15-item limit).
    for i in range(20):
        _add_turn_with_tool_call(conversation_store, super_chat.id, response_id=f"resp_old_{i}")
    # The newest turn, distinguishable by its tool-call query.
    conversation_store.append(
        super_chat.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_newest",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "what's newest?"}]
                ),
            ),
            NewConversationItem(
                type="function_call",
                response_id="resp_newest",
                data=FunctionCallData(
                    agent="brain",
                    name="memory_search",
                    arguments=json.dumps({"query": "the newest marker"}),
                    call_id="call_newest",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_newest",
                data=MessageData(
                    role="assistant",
                    agent="brain",
                    content=[{"type": "output_text", "text": "ok"}],
                ),
            ),
        ],
    )

    activities = list_activities(conversation_store, super_chat.id, limit=100)
    newest = [a for a in activities if a.id == f"turn:{super_chat.id}:resp_newest"]
    assert len(newest) == 1
    assert newest[0].steps[0].title == "Searched memory for 'the newest marker'"


def test_get_activity_returns_full_step_detail(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    activity_id = f"sub_agent:{family['sub_agent'].id}"
    activity = get_activity(conversation_store, family["super_chat"].id, activity_id)
    assert activity is not None
    assert activity.status == STATUS_DONE
    [step] = activity.steps
    assert step.detail is not None
    assert step.detail["call"]["name"] == "memory_search"


def test_get_activity_unknown_id_returns_none(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family = _build_super_chat_family(conversation_store, suffix="a")
    assert (
        get_activity(conversation_store, family["super_chat"].id, "sub_agent:conv_missing") is None
    )


# ── Owner / family scoping ───────────────────────────────────────────────


def test_list_activities_never_leaks_an_unrelated_super_chats_activities(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    family_a = _build_super_chat_family(conversation_store, suffix="a")
    family_b = _build_super_chat_family(conversation_store, suffix="b")

    activities_a = list_activities(conversation_store, family_a["super_chat"].id)
    chat_ids_a = {a.chat_id for a in activities_a}
    assert family_b["super_chat"].id not in chat_ids_a
    assert family_b["sub_agent"].id not in chat_ids_a

    # And family B's sub-agent id is not resolvable through family A's feed.
    cross_activity_id = f"sub_agent:{family_b['sub_agent'].id}"
    assert get_activity(conversation_store, family_a["super_chat"].id, cross_activity_id) is None
