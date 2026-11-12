"""Server side of tasks: what a turn reads in front of the person's message, and the picks on
the cards that asked.

The Round record is the source (ADR 0029): the work still under way is the Conversation's
Rounds not settled (:func:`~omnigent.superchat.rounds.read.open_rounds`), the one asking only
while its Helper runs, and a card
answer is kept on the message that carried it, the opener of its Round. Nothing creates, settles
or reads a task record any more.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omnigent.context.labels import BACKGROUND_LABELS
from omnigent.entities import Conversation, MessageData
from omnigent.stores import ConversationStore
from omnigent.superchat.artifacts.links import is_person_message
from omnigent.superchat.rounds.fold import fold, summary
from omnigent.superchat.rounds.read import open_rounds
from omnigent.superchat.rounds.record import round_of
from omnigent.superchat.rounds.store import SqlAlchemyRoundStore
from omnigent.superchat.tasks.record import parse_card_answer
from omnigent.superchat.tasks.store import SqlAlchemyTaskStore

#: Helpers nest one level (a worker's subworker); a deeper chain is not followed.
_MAX_HOPS = 4
#: Newest items read to find the person's latest message.
_LOOKBACK = 20


@dataclass(frozen=True)
class Place:
    """Where a session's work lands: the Conversation, and what kind of run this is.

    :param conversation: The person-facing Conversation (the session itself, or a Helper's root).
    :param helpers: The Helper sessions from this one up to the Conversation (empty for the
        Conversation itself).
    :param background: A schedule or an ad-hoc pass started this work, not a person.
    :param round_id: The Round the session is in (a Helper's: the one it was started in), else
        ``None``.
    """

    conversation: Conversation
    helpers: tuple[str, ...]
    background: bool
    round_id: str | None = None

    @property
    def is_helper(self) -> bool:
        """The session is a Helper."""
        return bool(self.helpers)


def _message_text(data: MessageData) -> str:
    return "\n\n".join(
        str(block.get("text"))
        for block in data.content
        if isinstance(block, dict)
        and block.get("type") == "input_text"
        and isinstance(block.get("text"), str)
    ).strip()


class TaskService:
    """What a Conversation's turn reads, and the picks its cards were answered with."""

    def __init__(
        self,
        store: SqlAlchemyTaskStore,
        conversations: ConversationStore,
        rounds: SqlAlchemyRoundStore,
    ) -> None:
        self.store = store
        self.conversations = conversations
        self.rounds = rounds

    # ------------------------------------------------------------------ where

    def place(self, session_id: str) -> Place | None:
        """The Conversation *session_id* works for, or ``None`` when it does not exist."""
        conv = self.conversations.get_conversation(session_id)
        if conv is None:
            return None
        background = any(key in conv.labels for key in BACKGROUND_LABELS)
        helpers: list[str] = []
        root = conv
        for _ in range(_MAX_HOPS):
            if root.kind != "sub_agent" or not root.parent_conversation_id:
                break
            parent = self.conversations.get_conversation(root.parent_conversation_id)
            if parent is None:
                break
            helpers.append(root.id)
            root = parent
            background = background or any(key in root.labels for key in BACKGROUND_LABELS)
        if conv.kind == "sub_agent" and not helpers:
            helpers.append(conv.id)
        return Place(root, tuple(helpers), background, round_of(conv.labels))

    def _latest_message(self, conversation_id: str) -> tuple[str | None, str, bool]:
        """``(item id, text, by a person)`` of the newest user-role message in the Conversation;
        the text is the person's latest words even when a runtime notice came after them."""
        page = self.conversations.list_items(conversation_id, limit=_LOOKBACK, order="desc")
        newest_by_person: bool | None = None
        for item in page.data:
            data = item.data
            if item.type != "message" or not isinstance(data, MessageData) or data.role != "user":
                continue
            person = is_person_message(item)
            if newest_by_person is None:
                newest_by_person = person
            if person:
                return item.id, _message_text(data), bool(newest_by_person)
        return None, "", False

    # ------------------------------------------------------------------ reads

    def view(self, session_id: str) -> dict[str, Any] | None:
        """What a turn and ``deck_new`` read: the person's latest message, whether a person
        opened this turn, the Round the session is in (``round_id``), the card answer that
        Round's message carried, and the Conversation's Rounds not settled (``rounds``,
        :meth:`_open_rounds`). A read changes nothing.
        """
        place = self.place(session_id)
        if place is None:
            return None
        conversation_id = place.conversation.id
        item_id, message, by_person = self._latest_message(conversation_id)
        round_id = place.round_id
        # The pick is on the message that opened the Round; a pick sent as a steer is on its own.
        answer = next(
            (
                found
                for at in (round_id, item_id if by_person else None)
                if at and (found := self.store.answer(conversation_id, at)) is not None
            ),
            None,
        )
        return {
            "conversation_id": conversation_id,
            "is_helper": place.is_helper,
            "background": place.background,
            "message": message,
            "by_person": by_person and not place.is_helper,
            "round_id": round_id,
            "answer": answer.to_wire() if answer else None,
            "rounds": self._open_rounds(conversation_id, round_id),
        }

    def _open_rounds(self, conversation_id: str, round_id: str | None) -> list[dict[str, Any]]:
        """The Conversation's Rounds not settled. The session's own Round is one of them only
        while a Helper of it runs (a status question asked mid-turn joins that Round), with what
        that work is doing now (``now``: the live tool's phrase) and its finished ``steps``."""
        rows = []
        for row in open_rounds(self.rounds, conversation_id):
            if row["round_id"] == round_id:
                if not any(h["status"] == "running" for h in row["helpers"]):
                    continue
                brief = summary(fold(self.rounds.events(round_id, conversation_id), round_id))
                row = {**row, "now": brief["live"], "steps": brief["steps"]}
            rows.append(row)
        return rows

    def card_answered(self, session_id: str, item_id: str, context: Mapping[str, Any]) -> None:
        """A message carried ``context.card_answer``: keep it on that message (the opener of the
        Round it starts), where the turn prefix and ``deck_new`` read it."""
        answer = parse_card_answer(context)
        if answer is not None:
            self.store.save_answer(session_id, item_id, answer)

    def conversation_deleted(self, session_id: str) -> None:
        """A deleted Conversation's tasks and card answers go with it."""
        self.store.delete_conversation(session_id)
