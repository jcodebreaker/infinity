"""In-process signals for family changes that no session stream carries.

Producers (a store-persisted assistant message, a rename or archive) call
``notify_*``; each family stream listening for that chat or root gets the event. Thread-safe:
producers may run on worker threads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Any

from omnigent.server import session_live_state
from omnigent.stores.conversation_store import ConversationStore
from omnigent.superchat.family.tree import resolve_super_chat_id

_lock = threading.Lock()
_listeners: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict]]]] = {}


def _listen(key: str, queue: asyncio.Queue[dict]) -> Callable[[], None]:
    entry = (asyncio.get_running_loop(), queue)
    with _lock:
        _listeners.setdefault(key, set()).add(entry)

    def stop() -> None:
        with _lock:
            _listeners.get(key, set()).discard(entry)

    return stop


def _notify(key: str, event: dict[str, Any]) -> None:
    with _lock:
        targets = list(_listeners.get(key, ()))
    for loop, queue in targets:
        loop.call_soon_threadsafe(queue.put_nowait, event)


def listen_chats_changed(root_id: str, queue: asyncio.Queue[dict]) -> Callable[[], None]:
    """Deliver ``chats.changed`` for *root_id* into *queue* (call from the queue's loop).

    :returns: A function that stops the delivery.
    """
    return _listen(f"chats:{root_id}", queue)


def notify_chats_changed(root_id: str) -> None:
    """Tell every family stream of *root_id* that its chats changed."""
    _notify(f"chats:{root_id}", {"type": "chats.changed", "root_id": root_id})


async def notify_session_changed(conv_store: ConversationStore, session_id: str) -> None:
    """A session was renamed or (un)archived: tell its family's streams, if it has one."""
    root_id = await asyncio.to_thread(resolve_super_chat_id, conv_store, session_id)
    if root_id is not None:
        notify_chats_changed(root_id)


def listen_message_done(chat_id: str, queue: asyncio.Queue[dict]) -> Callable[[], None]:
    """Deliver ``message.done`` for assistant messages stored in *chat_id* into *queue*."""
    return _listen(f"msg:{chat_id}", queue)


def notify_message_done(chat_id: str, item_id: str) -> None:
    """An assistant message was stored in *chat_id* under the store-assigned *item_id*."""
    _notify(f"msg:{chat_id}", {"type": "message.done", "chat_id": chat_id, "item_id": item_id})


def notify_chat_reset(chat_id: str, item_id: str) -> None:
    """*chat_id* was cleared (``POST .../reset``); *item_id* is its reset checkpoint."""
    _notify(f"msg:{chat_id}", {"type": "chat.reset", "chat_id": chat_id, "item_id": item_id})


def notify_item_updated(chat_id: str, item_id: str) -> None:
    """A stored item of *chat_id* changed (a person's message gained or changed its artifact link).

    Sent as ``message.delivery``: the frame the web already refetches the chat's transcript on.
    """
    _notify(f"msg:{chat_id}", {"type": "message.delivery", "chat_id": chat_id, "item_id": item_id})


def notify_chat_status(chat_id: str, status: str, blocked_on: str | None) -> None:
    """*chat_id* published a status edge (``session.status``: ``running``, ``waiting``, ``idle``,
    ``failed``; *blocked_on* the engine's words while a running turn waits for a slot).

    Sent as ``chat.status`` on its family's stream, so a page follows any family chat's status
    (a file's studio, a Helper) on the one stream it already holds instead of opening that
    chat's own stream. Resolving the family reads the store, so it runs on the ordered
    live-state worker (edges keep their order) and only while some family stream is open.
    """
    with _lock:
        if not any(key.startswith("chats:") and targets for key, targets in _listeners.items()):
            return
    store = session_live_state.conversation_store()
    if store is None:
        return

    def fan_out() -> None:
        # The family is the tree's root: a Helper's or a studio's root is its Super Chat.
        conversation = store.get_conversation(chat_id)
        if conversation is not None:
            _notify(
                f"chats:{conversation.root_conversation_id or conversation.id}",
                {
                    "type": "chat.status",
                    "chat_id": chat_id,
                    "status": status,
                    "blocked_on": blocked_on,
                },
            )

    session_live_state.submit("family_chat_status", fan_out)


def notify_artifacts_changed(
    root_id: str,
    group_id: str,
    *,
    version_id: str,
    version: int,
    origin: str,
    from_studio: bool,
    changed_ids: Sequence[str] = (),
) -> None:
    """The version *version_id* (number *version*) of the file *group_id* was saved in the
    Conversation *root_id*.

    Sent as ``artifacts.changed`` on the family stream (through the stream's chats listener), so
    an open view of the file (its studio) adopts it at once instead of at its next poll (ADR
    0030): *origin* is the version's (``ai``, ``manual``, ``restore``, ``update``),
    *from_studio* whether the file's own studio saved it, *changed_ids* the element ids its
    patches touched (empty for a whole save).
    """
    _notify(
        f"chats:{root_id}",
        {
            "type": "artifacts.changed",
            "root_id": root_id,
            "group_id": group_id,
            "version_id": version_id,
            "version": version,
            "origin": origin,
            "from_studio": from_studio,
            "changed_ids": list(changed_ids),
        },
    )


def listening(root_id: str) -> bool:
    """Whether a family stream of the Conversation *root_id* is open."""
    with _lock:
        return bool(_listeners.get(f"chats:{root_id}"))


def notify_round_updated(root_id: str, round_id: str, summary: dict[str, Any]) -> None:
    """The Round *round_id* of the Conversation *root_id* changed.

    Sent as ``round.updated`` on the family stream with the Round's summary
    (:func:`omnigent.superchat.rounds.fold.summary`); the client reads the full view from
    ``GET /v1/sessions/{root_id}/rounds/{round_id}``.
    """
    _notify(
        f"chats:{root_id}",
        {"type": "round.updated", "root_id": root_id, "round_id": round_id, **summary},
    )
