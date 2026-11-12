"""Suggestion entity: something an agent could do for the person next (an Idea)."""

from __future__ import annotations

from dataclasses import dataclass

SUGGESTION_STATUSES = ("open", "done", "dismissed")


@dataclass
class Suggestion:
    """
    A concrete next step an agent proposes, acted on from a UI.

    :param id: Bare 32-char hex id.
    :param user_id: Owner, or ``None`` in single-user mode.
    :param parent_session_id: The session (Conversation) it belongs to.
    :param title: Short headline.
    :param why: One-line reason it is worth doing.
    :param message: The exact message to send to the parent session if accepted.
    :param status: One of :data:`SUGGESTION_STATUSES`.
    :param source_session_id: The run that recorded it, or ``None``.
    :param created_at: Epoch seconds.
    :param updated_at: Epoch seconds of the last status change, or ``None``.
    """

    id: str
    user_id: str | None
    parent_session_id: str
    title: str
    why: str
    message: str
    status: str
    source_session_id: str | None
    created_at: int
    updated_at: int | None = None
