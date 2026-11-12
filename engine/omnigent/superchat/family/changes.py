"""A live "something changed" signal for one Super Chat's Activity Feed.

The feed itself is derived on read (``activity/derive.py``), so a client that wants it live only
needs to know *when* to read again. This watches the session streams that can change a row
and says ``activities.changed`` whenever one of them starts, steps, settles or is renamed.
The payload carries no content: the client refetches the feed, which stays the one source of
truth.

What is watched stays small however long the history is: the chat a person talks in (the
Super Chat), whose stream already carries its Helpers' lifecycle
(``session.created``, ``session.child_session.updated``), plus the Helpers that are not
settled yet, for their steps and for the parts they hand on. A Helper's stream is dropped the
moment it settles.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from omnigent.runtime import session_stream
from omnigent.stores.conversation_store import ConversationStore
from omnigent.superchat.family.tree import (
    is_helper_live,
    list_chat_family,
    list_chat_roots,
    resolve_super_chat_id,
)

CHANGED_EVENT = {"type": "activities.changed"}
HEARTBEAT_EVENT = {"type": "session.heartbeat"}

#: Stream events that can change a row of the feed: a Helper being created or changing
#: status, a finished item (a new Step or reply), a status edge, or a new title.
_FEED_EVENT_TYPES = frozenset(
    {
        "session.created",
        "session.child_session.updated",
        "session.status",
        "session.title",
        "response.output_item.done",
    }
)
#: A Helper's own status edges that mean it is no longer working.
_SETTLED_SESSION_STATUSES = frozenset({"idle", "failed"})

#: A feed read costs a family read: one per second at most, however fast the steps land.
_DEBOUNCE_SECONDS = 1.0
_HEARTBEAT_SECONDS = 15.0


def _child_id(event: dict[str, Any]) -> str | None:
    child = event.get("child_session_id")
    return child if isinstance(child, str) and child else None


async def watch_activity_changes(
    conv_store: ConversationStore,
    session_id: str,
    *,
    debounce_s: float = _DEBOUNCE_SECONDS,
    heartbeat_s: float = _HEARTBEAT_SECONDS,
    subscribe: Callable[[str], AsyncIterator[dict]] = session_stream.subscribe,
) -> AsyncIterator[dict]:
    """Yield ``activities.changed`` each time the family's feed may have changed.

    Yields one immediately (the subscription is live; the client reads the feed now),
    then one per burst of relevant events (coalesced over ``debounce_s``), and a heartbeat
    after ``heartbeat_s`` of quiet.

    :param conv_store: Store used to resolve the chats to watch.
    :param session_id: The Super Chat.
    :param debounce_s: How long a burst of events is coalesced into one signal.
    :param heartbeat_s: Quiet time before a heartbeat.
    :param subscribe: Per-conversation live event source (injectable for tests).
    """
    super_chat_id = await asyncio.to_thread(resolve_super_chat_id, conv_store, session_id)
    if super_chat_id is None:
        return
    events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    pumps: dict[str, asyncio.Task[None]] = {}
    roots: set[str] = set()

    async def pump(conversation_id: str) -> None:
        with contextlib.suppress(Exception):
            async for event in subscribe(conversation_id):
                kind = event.get("type")
                if isinstance(kind, str) and kind in _FEED_EVENT_TYPES:
                    await events.put((conversation_id, event))

    def watch(conversation_id: str) -> None:
        if conversation_id not in pumps:
            pumps[conversation_id] = asyncio.create_task(pump(conversation_id))

    def unwatch(conversation_id: str) -> None:
        if conversation_id not in roots and (task := pumps.pop(conversation_id, None)):
            task.cancel()

    async def sync_roots() -> None:
        chats = await asyncio.to_thread(list_chat_roots, conv_store, super_chat_id)
        roots.update(chat.id for chat in chats)
        for chat in chats:
            watch(chat.id)

    async def seed_live_helpers() -> None:
        # Once, on connect: Helpers already working when the page opens.
        family = await asyncio.to_thread(list_chat_family, conv_store, super_chat_id)
        for conversation in family:
            if is_helper_live(conversation):
                watch(conversation.id)

    def follow_lifecycle(conversation_id: str, event: dict[str, Any]) -> None:
        kind = event.get("type")
        child = _child_id(event)
        if kind == "session.created" and child:
            watch(child)
        elif kind == "session.child_session.updated" and child:
            info = event.get("child")
            info = info if isinstance(info, dict) else {}
            status = info.get("current_task_status")
            if info.get("busy") is True or status == "launching":
                watch(child)
            elif info.get("busy") is False and status is not None:
                unwatch(child)
        elif kind == "session.status" and event.get("status") in _SETTLED_SESSION_STATUSES:
            unwatch(conversation_id)

    try:
        await sync_roots()
        if not pumps:  # not a Super Chat family: nothing to watch
            return
        await seed_live_helpers()
        yield CHANGED_EVENT
        while True:
            try:
                first = await asyncio.wait_for(events.get(), timeout=heartbeat_s)
            except TimeoutError:
                await sync_roots()
                yield HEARTBEAT_EVENT
                continue
            await asyncio.sleep(debounce_s)
            batch = [first]
            while not events.empty():
                batch.append(events.get_nowait())
            for conversation_id, event in batch:
                follow_lifecycle(conversation_id, event)
            yield CHANGED_EVENT
    finally:
        tasks = list(pumps.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
