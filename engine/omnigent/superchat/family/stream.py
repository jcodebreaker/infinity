"""One live stream for a Conversation and its Helpers.

Events carry ids only; the client refetches what changed:

* ``message.done {chat_id, item_id}``: an assistant message was stored in a family chat
  (``item_id`` is the store id; published where the message is persisted);
* ``turn.done {chat_id, status}``: a turn in a family chat ended (completed, failed,
  incomplete or cancelled); a failed turn's error item is stored before this is sent;
* ``message.delivery {chat_id, item_id}``: a message sent during a turn started or stopped
  waiting behind it, or a person's message gained or changed its artifact link; refetch to see
  it;
* ``chat.reset {chat_id, item_id}``: the chat was cleared (``POST .../reset``); its transcript
  now starts after ``item_id``;
* ``chats.changed {root_id}``: the Conversation was renamed or archived, or a Helper started;
* ``activities.changed {root_id}``: the Activity Feed may have changed;
* ``artifacts.changed {root_id, group_id, version_id, version, origin, from_studio,
  changed_ids}``: a version of a file was saved (``changed_ids``: the element ids its patches
  touched, empty for a whole save);
* ``round.updated {root_id, round_id, status, started_at, settled_at, delivered_at, live, helpers,
  files, steps}``: a Round's record grew (at most four a second per Round); its view is
  ``GET .../rounds/{id}``;
* ``chat.status {chat_id, status, blocked_on}``: a family chat's status edge (``running``,
  ``waiting``, ``idle``, ``failed``; ``blocked_on`` while a running turn waits for a slot), for
  every family chat whether or not this stream follows it (published where the status is);
* ``asks.changed {root_id}``: a followed chat asked the person for a decision, or the ask was
  answered; ``GET /v1/me/asks`` reads them;
* ``session.heartbeat``: sent after ~15s of quiet.

It reuses the Activity Feed watcher (``family/changes.py``) for *which* sessions to follow
and how Helpers come and go, and reads the very same per-session streams through it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from omnigent.runtime import session_stream
from omnigent.stores.conversation_store import ConversationStore
from omnigent.superchat.family.changes import (
    CHANGED_EVENT,
    HEARTBEAT_EVENT,
    watch_activity_changes,
)
from omnigent.superchat.family.signals import listen_chats_changed, listen_message_done
from omnigent.superchat.family.tree import list_chat_roots

#: Session events that mean the chat list changed: a rename, a Helper or part being started.
_CHATS_EVENT_TYPES = frozenset({"session.title", "session.created"})
#: Session events that open or close a decision waiting on the person (an approval).
_ASKS_EVENT_TYPES = frozenset({"response.elicitation_request", "response.elicitation_resolved"})
#: Terminal response events: the turn ended, however it ended.
_TURN_END_STATUS = {
    "response.completed": "completed",
    "response.failed": "failed",
    "response.incomplete": "incomplete",
    "response.cancelled": "cancelled",
}


def _derive(chat_id: str, root_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
    """The family event one session-stream event stands for, if any."""
    kind = event.get("type")
    if kind in _CHATS_EVENT_TYPES:
        return {"type": "chats.changed", "root_id": root_id}
    if kind == "session.input.delivery" and isinstance(data := event.get("data"), dict):
        return {
            "type": "message.delivery",
            "chat_id": chat_id,
            "item_id": data.get("item_id"),
        }
    if kind in _ASKS_EVENT_TYPES:
        return {"type": "asks.changed", "root_id": root_id}
    if isinstance(kind, str) and kind in _TURN_END_STATUS:
        return {"type": "turn.done", "chat_id": chat_id, "status": _TURN_END_STATUS[kind]}
    return None


def _chat_signature(conv_store: ConversationStore, root_id: str) -> dict[str, tuple[Any, ...]]:
    return {chat.id: (chat.title, chat.archived) for chat in list_chat_roots(conv_store, root_id)}


async def watch_family(
    conv_store: ConversationStore,
    root_id: str,
    *,
    debounce_s: float = 0.25,
    heartbeat_s: float = 15.0,
    subscribe: Callable[[str], AsyncIterator[dict]] = session_stream.subscribe,
) -> AsyncIterator[dict[str, Any]]:
    """Yield the family's events for the Super Chat *root_id*, until the caller stops.

    :param conv_store: Store used to resolve the chats to follow.
    :param root_id: The Super Chat.
    :param debounce_s: How long the Activity Feed signal coalesces a burst.
    :param heartbeat_s: Quiet time before a heartbeat (which also re-reads the chat list).
    :param subscribe: Per-session live event source (injectable for tests).
    """
    out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def tee(chat_id: str) -> AsyncIterator[dict]:
        async def events() -> AsyncIterator[dict]:
            # message.done and chat.reset come from their persist seams, not from this stream.
            stop = listen_message_done(chat_id, out)
            try:
                async for event in subscribe(chat_id):
                    if (derived := _derive(chat_id, root_id, event)) is not None:
                        out.put_nowait(derived)
                    yield event
            finally:
                stop()

        return events()

    async def run_activity_watch() -> None:
        known = await asyncio.to_thread(_chat_signature, conv_store, root_id)
        async for event in watch_activity_changes(
            conv_store, root_id, debounce_s=debounce_s, heartbeat_s=heartbeat_s, subscribe=tee
        ):
            if event == CHANGED_EVENT:
                out.put_nowait({"type": "activities.changed", "root_id": root_id})
            elif event == HEARTBEAT_EVENT:
                current = await asyncio.to_thread(_chat_signature, conv_store, root_id)
                if current != known:
                    known = current
                    out.put_nowait({"type": "chats.changed", "root_id": root_id})
                out.put_nowait(HEARTBEAT_EVENT)

    stop_listening = listen_chats_changed(root_id, out)
    watcher = asyncio.create_task(run_activity_watch())
    getter: asyncio.Task[dict[str, Any]] | None = None
    try:
        out.put_nowait({"type": "chats.changed", "root_id": root_id})
        while True:
            getter = asyncio.create_task(out.get())
            done, _ = await asyncio.wait({getter, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield getter.result()
            else:
                getter.cancel()
                if out.empty():
                    return  # the watcher ended: not a Super Chat family
    finally:
        if getter is not None:
            getter.cancel()
        stop_listening()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
