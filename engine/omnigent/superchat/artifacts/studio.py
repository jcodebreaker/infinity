"""A file's studio chat (ADR 0024): the one session where a file is edited by talking.

The studio is a child of the Conversation (``kind="sub_agent"``, helper type ``editor``) labelled
with the file's group id. Its id is derived from (owner, Conversation, group), so find-or-create
needs no lookup table and two racing creates land on one row. It never wakes the Conversation:
it is made without a dispatch and without the labels that register a Result for the parent.

Every version of the group saved by anyone but the studio (a hand edit, Super Chat, a Helper, a
restore, a live update) is recorded in the studio as one runtime line, so the editor knows what
changed under it: stored without a runner dispatch (it never starts a turn), or steered into the
studio's running turn. A version another session saves while the studio's turn runs, or within
:data:`STALE_WINDOW_SECONDS` of its last turn, is worded as what it is for the editor: its own
next save is now stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from omnigent.context.labels import (
    ADHOC_HELPER_LABEL_KEY,
    ARTIFACT_CHAT_LABEL_KEY,
    inheritable_context_labels,
)
from omnigent.context.rollover import _MID_TURN_LIVE_STATUSES
from omnigent.entities import Conversation, MessageData, NewConversationItem
from omnigent.entities.conversation import is_system_notice
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import (
    ConversationAlreadyExistsError,
    NameAlreadyExistsError,
)
from omnigent.superchat.artifacts.hint import kind_word
from omnigent.superchat.artifacts.store import Artifact
from omnigent.superchat.artifacts.studio_turn import FILE_LINE_OPEN

_logger = logging.getLogger(__name__)

#: The studio's helper type (``agents/editor/config.yaml``).
STUDIO_TYPE = "editor"
_ID_SALT = "superchat-artifact-chat-v1"

#: ``(session_id, text) -> True`` when the session's running turn took the line.
Steer = Callable[[str, str], Awaitable[bool]]


def group_of(item: Artifact) -> str:
    """The deliverable's stable id (a row saved before group ids existed: its own id)."""
    return item.group_id or item.id


def studio_session_id(owner: str | None, conversation_id: str, group_id: str) -> str:
    """The studio session id of a file: 32 hex chars derived from (owner, Conversation, group).

    :param owner: The artifact owner; ``None`` (single-user) is the local user.
    """
    key = f"{_ID_SALT}\0{owner or RESERVED_USER_LOCAL}\0{conversation_id}\0{group_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def studio_of(item: Artifact) -> str:
    """The studio session id of *item*'s file (whether or not it exists yet)."""
    return studio_session_id(item.user_id, item.parent_session_id, group_of(item))


def is_studio_of(conversation: Conversation | None, group_id: str) -> bool:
    """Whether *conversation* is the studio of the file *group_id*."""
    return (
        conversation is not None
        and conversation.labels.get(ARTIFACT_CHAT_LABEL_KEY) == group_id
        and conversation.sub_agent_name == STUDIO_TYPE
    )


def find_or_create_studio(
    conversations: ConversationStore,
    item: Artifact,
    *,
    model_override: str | None = None,
) -> tuple[str, bool]:
    """The studio session of *item*'s file, made on first use.

    :param item: Any version of the file.
    :param model_override: The person's Builder model for the editor, when known; the
        Conversation's model otherwise.
    :returns: ``(session_id, created)``.
    :raises LookupError: When the Conversation is gone, or the derived id is another session.
    """
    group = group_of(item)
    session_id = studio_of(item)
    found = conversations.get_conversation(session_id)
    if found is not None:
        if not is_studio_of(found, group):
            raise LookupError("studio id is taken")
        return session_id, False
    parent = conversations.get_conversation(item.parent_session_id)
    if parent is None:
        raise LookupError("conversation not found")
    labels = {
        **inheritable_context_labels(parent.labels),
        # Like a daily-note pass: no Result is registered for the parent, so nothing it does
        # ever wakes or steers the Conversation.
        ADHOC_HELPER_LABEL_KEY: group,
        ARTIFACT_CHAT_LABEL_KEY: group,
    }
    try:
        conversations.create_conversation(
            kind="sub_agent",
            title=f"{STUDIO_TYPE}:{group}",
            parent_conversation_id=parent.id,
            agent_id=parent.agent_id,
            runner_id=parent.runner_id,
            sub_agent_name=STUDIO_TYPE,
            labels=labels,
            conversation_id=session_id,
            model_override=model_override or parent.model_override,
        )
    except (ConversationAlreadyExistsError, NameAlreadyExistsError):
        # A concurrent first open won: read the row it wrote.
        found = conversations.get_conversation(session_id)
        if not is_studio_of(found, group):
            raise LookupError("studio id is taken") from None
        return session_id, False
    return session_id, True


async def studio_model(
    app_state: Any, agent_store: Any, agent_cache: Any, owner: str | None, parent: Conversation
) -> str | None:
    """The person's Builder model on the harness the editor runs on, or ``None`` (inherit)."""
    if agent_cache is None or agent_store is None or parent.agent_id is None:
        return None
    try:
        from omnigent.models.model_catalog import spec_harness
        from omnigent.runtime.workflow import _find_spec_by_name
        from omnigent.server.routes.sandbox_inference import snapshot_serves_model
        from omnigent.superchat.models.tiers import tier_models

        agent = await asyncio.to_thread(agent_store.get, parent.agent_id)
        if agent is None:
            return None
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        sub = _find_spec_by_name(loaded.spec, STUDIO_TYPE)
        harness = spec_harness(sub if sub is not None else loaded.spec)
        # A file editor writes, so it runs on the Builder pick (ADR 0027).
        model = (await tier_models(app_state, owner, harness)).get("builder")
    except Exception:  # the Conversation's model is a fine fallback
        _logger.warning("studio: could not resolve the Builder model", exc_info=True)
        return None
    if model and snapshot_serves_model(parent.inference_snapshot, model):
        return model
    return None


# ---------------------------------------------------------------------- what a turn reads


def saved_by(version: Artifact, studio_id: str) -> str:
    """Who saved *version*, in the studio's words."""
    if version.origin in ("manual", "restore"):
        return "you"
    if version.origin == "update":
        return "a live update"
    return "this chat" if version.chat_id == studio_id else "Super Chat"


def file_line(newest: Artifact, studio_id: str) -> str:
    """The studio turn's file line: which file, its newest version, who saved it, where it is."""
    label = " ".join((newest.title or newest.name).split())
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(newest.created_at))
    path = newest.source_path or "none yet"
    return (
        f"{FILE_LINE_OPEN}{label} ({newest.name}, {kind_word(newest)}, group "
        f"{group_of(newest)}). Newest: v{newest.version}, saved by "
        f"{saved_by(newest, studio_id)} on {when}. Workspace path: {path}. "
        f"Save with base_version {newest.version}."
    )


def notice_line(saved: Artifact, studio_id: str) -> str | None:
    """The line recorded in the studio for a version saved elsewhere; ``None`` for its own."""
    if saved.chat_id == studio_id:
        return None
    if saved.origin == "manual":
        return f"[you] {saved.edit_summary or 'edited by hand'} · v{saved.version}"
    if saved.origin == "restore":
        return f"[you] {saved.edit_summary or 'restored an earlier version'} · v{saved.version}"
    if saved.origin == "update":
        return f"[live update] refreshed from its sources · v{saved.version}"
    return f"[Super Chat] saved v{saved.version}"


#: A version saved elsewhere this soon after the studio's last turn may be one it worked over.
STALE_WINDOW_SECONDS = 300
#: How many of the studio's newest items are searched for its last turn.
_LAST_TURN_LOOKBACK = 20


def stale_line(saved: Artifact, studio_id: str) -> str | None:
    """The studio's line for a version another session saved while it was editing.

    ``None`` for the studio's own save and for the person's (a hand edit or a restore, which
    cannot land during a studio turn and are recorded as theirs).
    """
    if saved.chat_id == studio_id or saved.origin in ("manual", "restore"):
        return None
    who = "A live update" if saved.origin == "update" else "Super Chat"
    return (
        f"{who} saved v{saved.version} while you were editing; "
        f"v{saved.version + 1} is based on v{max(saved.version - 1, 1)}"
    )


def turn_ended_recently(
    conversations: ConversationStore, session_id: str, *, now: float | None = None
) -> bool:
    """Whether the studio's last turn (its newest item that is not a runtime notice) is recent."""
    page = conversations.list_items(session_id, limit=_LAST_TURN_LOOKBACK, order="desc")
    for item in page.data:
        if isinstance(item.data, MessageData) and is_system_notice(item.data):
            continue
        current = time.time() if now is None else now
        return current - item.created_at <= STALE_WINDOW_SECONDS
    return False


def append_notice(conversations: ConversationStore, session_id: str, text: str) -> None:
    """Store one runtime line in *session_id* without dispatching it (no turn starts)."""
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

    persisted = conversations.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"studio_{uuid.uuid4().hex}",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": text}],
                    is_system_notice=True,
                ),
            )
        ],
    )
    if persisted:
        _publish_external_conversation_item(session_id, persisted[0])


async def deliver_studio_notice(
    conversations: ConversationStore, saved: Artifact, *, steer: Steer | None
) -> str | None:
    """Tell *saved*'s studio (if the file has one) about a version it did not make.

    :returns: ``"steered"`` when a running studio turn took the line, ``"stored"`` when it was
        kept in the studio's transcript for its next turn, ``None`` when there is nothing to tell.
    """
    studio_id = studio_of(saved)
    studio = await asyncio.to_thread(conversations.get_conversation, studio_id)
    if not is_studio_of(studio, group_of(saved)) or studio is None or studio.archived:
        return None
    line = notice_line(saved, studio_id)
    if line is None:
        return None
    running = studio.live_status in _MID_TURN_LIVE_STATUSES
    stale = stale_line(saved, studio_id)
    if stale is not None and (
        running or await asyncio.to_thread(turn_ended_recently, conversations, studio_id)
    ):
        line = stale
    if steer is not None and running:
        if await steer(studio_id, line):
            return "steered"
    await asyncio.to_thread(append_notice, conversations, studio_id, line)
    return "stored"


class StudioNotices:
    """The save hook that records other-origin versions in a file's studio.

    Saves run on worker threads; the notice (which may steer a running turn over the runner's
    HTTP path) runs on the engine's loop, captured when the app starts (:meth:`start`).
    """

    def __init__(self, conversations: ConversationStore, steer: Steer | None) -> None:
        self._conversations = conversations
        self._steer = steer
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Capture the running loop (a feature job's start, inside the app's lifespan)."""
        self._loop = asyncio.get_running_loop()

    def shutdown(self) -> None:
        """Stop scheduling on the loop."""
        self._loop = None

    def on_saved(self, saved: Artifact) -> None:
        """``ON_VERSION_SAVED`` hook: never blocks or fails the save."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            deliver_studio_notice(self._conversations, saved, steer=self._steer), loop
        )
        future.add_done_callback(_log_failure)

    def on_deleted(self, item: Artifact) -> None:
        """``ON_GROUP_DELETED`` hook: a deleted file archives its studio."""
        studio_id = studio_of(item)
        if is_studio_of(self._conversations.get_conversation(studio_id), group_of(item)):
            self._conversations.update_conversation(studio_id, archived=True)


def _log_failure(future: Any) -> None:
    if not future.cancelled() and future.exception() is not None:
        _logger.warning("studio notice failed", exc_info=future.exception())
