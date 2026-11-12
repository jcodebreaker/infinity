"""What reads a Round's record once a batch of its events is stored (ADR 0029).

* :class:`Announcer` folds a Round whose record grew and sends its summary as ``round.updated``
  on the Conversation's family stream, at most once per :data:`ANNOUNCE_EVERY` seconds per Round
  and only while a family stream is open.
* :func:`item_rounds` cuts a run of a Conversation's items into the Rounds they are in, which
  :func:`attach_round_ids` puts on each transcript message (``round_id``) and the Activity rows
  read; :func:`open_rounds` lists the Rounds not settled, from their views (the turn prefix
  and the context brief).
* :func:`attach_round_files` draws a transcript page's file cards from each recorded Round's
  view: each file once, at its latest version, where the Round last showed it.
* :func:`shadow_diff`, on when :data:`SHADOW_ENV` is set, folds a Round after a turn of it ends
  and logs at INFO each way the fold disagrees with what the engine derives today from the
  stored items and Helper sessions: status, start time, files and Helpers. Development only;
  nothing it finds reaches a person.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from omnigent.entities.conversation import is_system_notice
from omnigent.server.auth import env_var_is_truthy
from omnigent.stores.conversation_store import ConversationStore
from omnigent.superchat.activity.derive import _turn_status
from omnigent.superchat.family import signals
from omnigent.superchat.family.tree import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    sub_agent_status,
)
from omnigent.superchat.rounds.fold import TURN_ENDED, fold, summary
from omnigent.superchat.rounds.record import round_of
from omnigent.superchat.rounds.store import RoundEvent, SqlAlchemyRoundStore
from omnigent.superchat.rounds.ux_log import shown
from omnigent.superchat.transcript.blocks import file_block, helper_session_ids, project_items

if TYPE_CHECKING:
    from omnigent.superchat.artifacts.store import SqlAlchemyArtifactStore

_logger = logging.getLogger(__name__)

#: Seconds between two ``round.updated`` of one Round.
ANNOUNCE_EVERY = 0.25
#: Set (``1``, ``true``) to log where the fold and today's derivation disagree.
SHADOW_ENV = "OMNIGENT_ROUND_SHADOW"
#: Seconds after a turn ends before the shadow diff reads, so its items are stored.
SHADOW_DELAY = 2.0
#: Items of a Round the shadow diff reads at most.
_SHADOW_ITEMS = 500
#: Seconds two start times may differ by and still agree.
_START_SLACK = 2.0
#: Messages searched back from a page for the Round its first items are in.
_OPENER_LOOKBACK = 50
#: Latest Rounds :func:`open_rounds` looks through for the ones not settled.
_OPEN_LOOKBACK = 10
#: Today's statuses (an Activity's, a Helper's) as the fold names them.
_OLD_STATUS = {
    STATUS_IN_PROGRESS: "running",
    STATUS_CANCELLED: "stopped",
    STATUS_FAILED: "failed",
}


class Announcer:
    """Sends ``round.updated`` for each Round whose record grew, coalesced per Round."""

    def __init__(self, store: SqlAlchemyRoundStore, every: float = ANNOUNCE_EVERY) -> None:
        self._store = store
        self._every = every
        self._dirty: dict[str, str] = {}  # round -> conversation, written since last sent
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def written(self, batch: Sequence[RoundEvent]) -> None:
        """Note the Rounds *batch* wrote to; call on the loop."""
        for event in batch:
            if not signals.listening(event.conversation_id):
                continue
            self._dirty[event.round_id] = event.conversation_id
            if event.round_id not in self._tasks:
                self._tasks[event.round_id] = asyncio.get_running_loop().create_task(
                    self._send(event.round_id)
                )

    async def _send(self, round_id: str) -> None:
        try:
            while (conversation_id := self._dirty.pop(round_id, None)) is not None:
                events = await asyncio.to_thread(self._store.events, round_id)
                if events:
                    view = fold(events, round_id)
                    shown("announcer", conversation_id, round_id, view)
                    signals.notify_round_updated(conversation_id, round_id, summary(view))
                await asyncio.sleep(self._every)
        except Exception:
            _logger.warning("round %s not announced", round_id, exc_info=True)
        finally:
            self._tasks.pop(round_id, None)


def shadow_on() -> bool:
    """Whether the shadow diff runs (:data:`SHADOW_ENV`)."""
    return env_var_is_truthy(SHADOW_ENV, default=False)


def _is_card(block: Mapping[str, Any]) -> bool:
    """A file card the Assistant's work drew; a file the person delivered is not one."""
    return block.get("type") == "file" and block.get("by") != "user"


def _shown_file(block: Mapping[str, Any], ids: Mapping[str, str]) -> str | None:
    """The artifact id of the Round's file *block* shows: a save call's ``file`` block or a
    ``render_card`` file card. *ids* maps each of the Round's file ids and names to its id."""
    if _is_card(block):
        return ids.get(str(block.get("artifact_id")))
    if block.get("type") != "card" or block["card"].get("card") != "file":
        return None
    data = block["card"].get("data") or {}
    shown = data.get("artifactId")
    return ids.get(shown) if isinstance(shown, str) else ids.get(str(data.get("name")))


class _Views:
    """The fold of each Round asked for, read once; ``None`` for an id no Round has."""

    def __init__(self, store: SqlAlchemyRoundStore, conversation_id: str) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._views: dict[str, dict[str, Any] | None] = {}

    def __call__(self, round_id: str) -> dict[str, Any] | None:
        if round_id not in self._views:
            events = self._store.events(round_id, self._conversation_id)
            self._views[round_id] = fold(events, round_id) if events else None
        return self._views[round_id]


def _round_before(
    store: SqlAlchemyRoundStore, conv_store: ConversationStore, conversation_id: str, item_id: str
) -> str | None:
    """The Round the items after *item_id* are in: that of the newest message before them that
    opened or woke one."""
    page = conv_store.list_items(
        conversation_id, limit=_OPENER_LOOKBACK, after=item_id, order="desc", type="message"
    )
    ids = [item.id for item in page.data]
    known = store.item_rounds(conversation_id, ids)
    return next((known[i] for i in ids if i in known), None)


def item_rounds(
    store: SqlAlchemyRoundStore,
    conv_store: ConversationStore,
    conversation_id: str,
    item_ids: Sequence[str],
) -> list[str | None]:
    """The Round each of a Conversation's *item_ids* (oldest first, one run of its items) is in.

    A person's message that opened a Round starts it and a Helper's notice joins the Round of
    that Helper; every other item (a steer, a reply, a tool call) is in the Round of the last of
    those before it, looked up before the first item when the run starts inside a Round. An item
    before any recorded Round is in none (``None``).
    """
    known = store.item_rounds(conversation_id, item_ids)
    current = None
    if item_ids and item_ids[0] not in known:
        current = _round_before(store, conv_store, conversation_id, item_ids[0])
    out: list[str | None] = []
    for item_id in item_ids:
        current = known.get(item_id, current)
        out.append(current)
    return out


def attach_round_ids(
    store: SqlAlchemyRoundStore,
    conv_store: ConversationStore,
    conversation: Any,
    items: Sequence[Mapping[str, Any]],
    messages: list[dict[str, Any]],
) -> None:
    """Give each transcript message the ``round_id`` of the Round it is in, in place.

    A Conversation's *items* (the page's, oldest first: a hidden notice still moves the Round)
    are cut by :func:`item_rounds`, and a message is in the Round of the item it was drawn from;
    every message of a Helper is in the Round the Helper was started in. ``None`` outside any
    Round.
    """
    if conversation.kind == "sub_agent":
        own = round_of(conversation.labels)
        for message in messages:
            message["round_id"] = own
        return
    ids = [str(item["id"]) for item in items]
    by_item = dict(zip(ids, item_rounds(store, conv_store, conversation.id, ids), strict=True))
    for message in messages:
        message["round_id"] = by_item.get(message["id"])


def open_rounds(store: SqlAlchemyRoundStore, conversation_id: str) -> list[dict[str, Any]]:
    """The Conversation's Rounds not settled, newest first, read only from their views: opened
    by the person (not hidden), each with its Helpers as the view has them. A Round whose
    sessions went idle is settled by the :class:`~omnigent.superchat.rounds.record.Sweep`, so
    nothing reads as working once its work is gone.

    :returns: ``[{"round_id", "ask", "helpers": [{"id", "title", "status"}]}]``, at most from
        the :data:`_OPEN_LOOKBACK` latest Rounds.
    """
    rows: list[dict[str, Any]] = []
    for round_id in store.recent(conversation_id, _OPEN_LOOKBACK):
        view = fold(store.events(round_id, conversation_id), round_id)
        if view["settled_at"] is not None or view["hidden"] or view["status"] == "idle":
            continue
        helpers = [
            {"id": h["id"], "title": h["title"], "status": h["status"]}
            for h in view["helpers"].values()
        ]
        rows.append({"round_id": round_id, "ask": view["ask"], "helpers": helpers})
    return rows


def attach_round_files(
    store: SqlAlchemyRoundStore,
    conversation_id: str,
    messages: list[dict[str, Any]],
    *,
    newer_round_id: str | None,
    artifacts: SqlAlchemyArtifactStore | None = None,
) -> None:
    """Draw each recorded Round's file cards from its view, in place on transcript *messages*.

    The messages are in the Rounds their ``round_id`` names (:func:`attach_round_ids`). Each of
    a recorded Round's view files shows once, at its latest version: where the Round last showed
    it (a save call's card, or a ``render_card`` file card such as a wake turn's), so every turn
    keeps its own card in the order it was stored; a file the Round never showed (a Helper's,
    saved without a card) goes after the words of the Round's last assistant text. A save call's
    card of a file the view does not hold is dropped; a ``render_card`` card of any other file
    stays. A Round that goes on past the page (*newer_round_id*, the Round of the oldest item of
    the page after this one, is the page's last Round) draws the files it never showed on the
    page that holds its end. A Round with no record, such as one from before Rounds, keeps the
    cards its calls drew.

    :param newer_round_id: The Round of the oldest item of the page after this one; ``None`` on
        the newest page.
    :param artifacts: Where a card's title and size are read; without it a card has neither.
    """
    views = _Views(store, conversation_id)
    rounds: list[tuple[str | None, list[dict[str, Any]]]] = []
    for message in messages:
        round_id = message.get("round_id")
        if not rounds or rounds[-1][0] != round_id:
            rounds.append((round_id, []))
        rounds[-1][1].append(message)
    saved = (
        artifacts.for_rounds(conversation_id, [r for r, _ in rounds if r is not None])
        if artifacts is not None
        else {}
    )
    for index, (round_id, mine) in enumerate(rounds):
        view = views(round_id) if round_id is not None else None
        if view is None:
            continue
        rows = {a.id: a for a in saved.get(round_id, [])}
        cards: dict[str, dict[str, Any]] = {}
        for f in view["files"]:
            row = rows.get(f["artifact_id"])
            block = file_block(
                {
                    "type": "artifact",
                    "id": f["artifact_id"],
                    "name": f["name"],
                    "kind": f["kind"],
                    "version": f["version"],
                    "title": row.title if row else None,
                    "size": row.size if row else None,
                }
            )
            if block:
                cards[f["artifact_id"]] = block
        ids = {k: f["artifact_id"] for f in view["files"] for k in (f["artifact_id"], f["name"])}
        # Where the Round last showed each file: (message, block index).
        last: dict[str, tuple[int, int]] = {}
        for at, message in enumerate(mine):
            for place, block in enumerate(message["blocks"]):
                shown = _shown_file(block, ids)
                if shown in cards:
                    last[shown] = (at, place)
        for at, message in enumerate(mine):
            kept: list[dict[str, Any]] = []
            for place, block in enumerate(message["blocks"]):
                shown = _shown_file(block, ids)
                if shown is None:
                    if not _is_card(block):
                        kept.append(block)
                elif shown in cards and last[shown] == (at, place):
                    kept.append(cards[shown] if _is_card(block) else block)
            message["blocks"] = kept
        if index == len(rounds) - 1 and newer_round_id == round_id:
            continue
        replies = [m for m in mine if m["role"] == "assistant"]
        closing = next(
            (m for m in reversed(replies) if any(b.get("type") == "text" for b in m["blocks"])),
            replies[-1] if replies else None,
        )
        if closing is not None:
            closing["blocks"].extend(cards[f] for f in cards if f not in last)
    messages[:] = [m for m in messages if m["blocks"]]


def _round_items(conv_store: ConversationStore, conversation_id: str, round_id: str) -> list[Any]:
    """The Conversation's items from the Round's message up to the person's next message."""
    opener = conv_store.get_item(conversation_id, round_id)
    if opener is None:
        return []
    after = conv_store.list_items(conversation_id, limit=_SHADOW_ITEMS, after=round_id).data
    items = [opener]
    for item in after:
        data = item.data
        if item.type == "message" and data.role == "user" and not is_system_notice(data):
            if item.response_id != opener.response_id:  # a steer stays in its turn
                break
        items.append(item)
    return items


def _file_names(items: list[Any]) -> set[str]:
    names: set[str] = set()
    for message in project_items([item.to_api_dict() for item in items]):
        names |= {b["name"] for b in message["blocks"] if b.get("type") == "file"}
    return names


def derive_today(
    conv_store: ConversationStore, conversation_id: str, round_id: str
) -> dict[str, Any] | None:
    """The Round as the engine derives it today: from the Conversation's stored items (the
    Activity's turn status, the transcript's file and Helper blocks) and the Helper sessions.

    :returns: ``status`` (fold names), ``started_at``, ``files`` and ``helpers`` (sets), or
        ``None`` when the Round's message is not stored.
    """
    conversation = conv_store.get_conversation(conversation_id)
    items = _round_items(conv_store, conversation_id, round_id)
    if conversation is None or not items:
        return None
    latest = conv_store.list_items(conversation_id, limit=1, order="desc").data
    last_group = [i for i in items if i.response_id == items[-1].response_id]
    status = _turn_status(
        conversation,
        last_group,
        is_latest_group=bool(latest) and latest[0].response_id == items[-1].response_id,
    )
    helper_ids = helper_session_ids([item.to_api_dict() for item in items])
    helpers = conv_store.get_conversations(helper_ids) if helper_ids else {}
    if any(sub_agent_status(h) == STATUS_IN_PROGRESS for h in helpers.values()):
        status = STATUS_IN_PROGRESS
    files = _file_names(items)
    for helper_id in helpers:
        files |= _file_names(conv_store.list_items(helper_id, limit=_SHADOW_ITEMS).data)
    return {
        "status": _OLD_STATUS.get(status, "done"),
        "started_at": float(items[0].created_at),
        "files": files,
        "helpers": set(helpers),
    }


def shadow_diff(
    store: SqlAlchemyRoundStore,
    conv_store: ConversationStore,
    conversation_id: str,
    round_id: str,
) -> list[str]:
    """Fold the Round, derive it today's way, and log each field they disagree on.

    :returns: The fields that disagree, e.g. ``["files"]``.
    """
    events = store.events(round_id, conversation_id)
    today = derive_today(conv_store, conversation_id, round_id)
    if not events or today is None:
        return []
    view = fold(events, round_id)
    folded = {
        "status": view["status"],
        "started_at": view["started_at"],
        "files": {f["name"] for f in view["files"]},
        "helpers": set(view["helpers"]),
    }
    differ = [
        key
        for key in ("status", "started_at", "files", "helpers")
        if (
            abs((folded[key] or 0) - today[key]) > _START_SLACK
            if key == "started_at"
            else folded[key] != today[key]
        )
    ]
    for key in differ:
        fold_value, today_value = (
            (sorted(v) if isinstance(v, set) else v) for v in (folded[key], today[key])
        )
        _logger.info(
            "round %s shadow %s: fold=%r today=%r", round_id, key, fold_value, today_value
        )
    return differ


class Shadow:
    """Runs :func:`shadow_diff` :data:`SHADOW_DELAY` seconds after a turn of a Round ends."""

    def __init__(self, store: SqlAlchemyRoundStore, conv_store: ConversationStore) -> None:
        self._store = store
        self._conv_store = conv_store
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def written(self, batch: Sequence[RoundEvent]) -> None:
        """Schedule a diff for each Round in *batch* a turn of which ended."""
        for event in batch:
            if event.kind == TURN_ENDED and event.round_id not in self._tasks:
                self._tasks[event.round_id] = asyncio.get_running_loop().create_task(
                    self._diff(event.conversation_id, event.round_id)
                )

    async def _diff(self, conversation_id: str, round_id: str) -> None:
        try:
            await asyncio.sleep(SHADOW_DELAY)
            await asyncio.to_thread(
                shadow_diff, self._store, self._conv_store, conversation_id, round_id
            )
        except Exception:
            _logger.warning("round %s shadow diff failed", round_id, exc_info=True)
        finally:
            self._tasks.pop(round_id, None)
