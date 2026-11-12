"""The Activity Feed's live "changed" signal (omnigent.superchat.family.changes)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.family.changes import (
    CHANGED_EVENT,
    HEARTBEAT_EVENT,
    watch_activity_changes,
)

_MODE_LABEL = "omnigent.context.mode"


class _Bus:
    """An in-memory stand-in for ``session_stream``: one queue per conversation."""

    def __init__(self) -> None:
        self.queues: dict[str, asyncio.Queue[dict]] = {}
        self.active: set[str] = set()

    def subscribe(self, conversation_id: str) -> AsyncIterator[dict]:
        queue = self.queues.setdefault(conversation_id, asyncio.Queue())

        async def events() -> AsyncIterator[dict]:
            self.active.add(conversation_id)
            try:
                while True:
                    yield await queue.get()
            finally:
                self.active.discard(conversation_id)

        return events()

    def publish(self, conversation_id: str, event: dict) -> None:
        self.queues.setdefault(conversation_id, asyncio.Queue()).put_nowait(event)


def _family(conv_store: SqlAlchemyConversationStore) -> tuple[str, str]:
    root = conv_store.create_conversation(
        kind="default", title="Super", labels={_MODE_LABEL: "superside-chat"}
    )
    child = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        title="researcher:japan",
        labels={_MODE_LABEL: "superside-chat"},
    )
    conv_store.set_session_live_status(child.id, "running")
    return root.id, child.id


async def _next(stream: AsyncIterator[dict]) -> dict:
    return await asyncio.wait_for(anext(stream), timeout=2)


async def test_signals_immediately_then_on_a_helper_step(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id, child_id = _family(conversation_store)
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.01, heartbeat_s=5, subscribe=bus.subscribe
    )
    assert await _next(stream) == CHANGED_EVENT
    await asyncio.sleep(0.05)  # the pumps are subscribed
    bus.publish(child_id, {"type": "response.output_item.done", "item": {}})
    assert await _next(stream) == CHANGED_EVENT
    await stream.aclose()


async def test_a_burst_of_events_is_one_signal_and_noise_is_ignored(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id, child_id = _family(conversation_store)
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.05, heartbeat_s=0.4, subscribe=bus.subscribe
    )
    await _next(stream)
    await asyncio.sleep(0.05)
    bus.publish(child_id, {"type": "response.output_text.delta", "delta": "x"})
    for _ in range(5):
        bus.publish(child_id, {"type": "session.status", "status": "running"})
    assert await _next(stream) == CHANGED_EVENT
    # Nothing else was queued: the next frame is the quiet-time heartbeat, not a repeat.
    assert await _next(stream) == HEARTBEAT_EVENT
    await stream.aclose()


async def test_a_helper_created_later_is_watched_after_its_creation_event(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id, child_id = _family(conversation_store)
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.01, heartbeat_s=5, subscribe=bus.subscribe
    )
    await _next(stream)
    await asyncio.sleep(0.05)
    grandchild = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=child_id,
        title="part:one",
        labels={_MODE_LABEL: "superside-chat"},
    )
    bus.publish(
        child_id,
        {"type": "session.created", "child_session_id": grandchild.id},
    )
    assert await _next(stream) == CHANGED_EVENT
    await asyncio.sleep(0.05)
    bus.publish(grandchild.id, {"type": "response.output_item.done", "item": {}})
    assert await _next(stream) == CHANGED_EVENT
    await stream.aclose()


async def test_not_a_super_chat_yields_nothing(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    plain = conversation_store.create_conversation(kind="default", title="Plain", labels={})
    stream = watch_activity_changes(conversation_store, plain.id, subscribe=_Bus().subscribe)
    assert [event async for event in stream] == []


def _root(conv_store: SqlAlchemyConversationStore) -> str:
    return conv_store.create_conversation(
        kind="default", title="Super", labels={_MODE_LABEL: "superside-chat"}
    ).id


def _helper(
    conv_store: SqlAlchemyConversationStore, parent_id: str, title: str, status: str | None
) -> str:
    helper = conv_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent_id,
        title=title,
        labels={_MODE_LABEL: "superside-chat"},
    )
    if status is not None:
        conv_store.set_session_live_status(helper.id, status)
    return helper.id


async def test_only_the_chats_and_unsettled_helpers_are_watched(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id = _root(conversation_store)
    settled = [_helper(conversation_store, root_id, f"old:{i}", "idle") for i in range(30)]
    failed = _helper(conversation_store, root_id, "bad:1", "failed")
    working = _helper(conversation_store, root_id, "now:1", "running")
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.01, heartbeat_s=5, subscribe=bus.subscribe
    )
    await _next(stream)
    await asyncio.sleep(0.05)
    assert bus.active == {root_id, working}
    assert not bus.active & {*settled, failed}
    await stream.aclose()
    await asyncio.sleep(0.05)
    assert bus.active == set()


async def test_a_new_helper_gets_a_stream_that_ends_when_it_settles(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id = _root(conversation_store)
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.01, heartbeat_s=5, subscribe=bus.subscribe
    )
    await _next(stream)
    await asyncio.sleep(0.05)
    newcomer = _helper(conversation_store, root_id, "new:1", None)

    bus.publish(root_id, {"type": "session.created", "child_session_id": newcomer})
    assert await _next(stream) == CHANGED_EVENT
    await asyncio.sleep(0.05)
    assert newcomer in bus.active

    # Its steps now reach the signal, and its settling drops the stream.
    bus.publish(newcomer, {"type": "response.output_item.done", "item": {}})
    assert await _next(stream) == CHANGED_EVENT
    bus.publish(
        root_id,
        {
            "type": "session.child_session.updated",
            "child_session_id": newcomer,
            "child": {"busy": False, "current_task_status": "completed"},
        },
    )
    assert await _next(stream) == CHANGED_EVENT
    await asyncio.sleep(0.05)
    assert bus.active == {root_id}
    await stream.aclose()


async def test_watched_streams_stay_bounded_across_many_helpers(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    root_id = _root(conversation_store)
    bus = _Bus()
    stream = watch_activity_changes(
        conversation_store, root_id, debounce_s=0.01, heartbeat_s=5, subscribe=bus.subscribe
    )
    await _next(stream)
    await asyncio.sleep(0.05)
    peak = 0
    for i in range(20):
        helper_id = _helper(conversation_store, root_id, f"run:{i}", None)
        bus.publish(root_id, {"type": "session.created", "child_session_id": helper_id})
        await _next(stream)
        await asyncio.sleep(0.02)
        peak = max(peak, len(bus.active))
        bus.publish(
            root_id,
            {
                "type": "session.child_session.updated",
                "child_session_id": helper_id,
                "child": {"busy": False, "current_task_status": "completed"},
            },
        )
        await _next(stream)
        await asyncio.sleep(0.02)
    assert peak <= 2
    assert bus.active == {root_id}
    await stream.aclose()
