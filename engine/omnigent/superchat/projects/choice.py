"""The person's own choice of where a chat works, which wins over the Muse's ``open_project``.

A client that moves a chat for the person (the "Work in" picker) calls the generic
``PUT /v1/sessions/{id}/workspace`` and then marks the session through the generic labels write
(``PATCH /v1/sessions/{id}``):

* :data:`WORKSPACE_CHOSEN_LABEL_KEY` ``= "person"`` while the person's folder stands; ``""``
  when they clear it (labels are upsert-only, so an empty value means "not set").
* :data:`WORKSPACE_MOVED_LABEL_KEY` ``= <the new working directory>``: a one-shot note the next
  turn tells the Muse (:func:`deliver_person_move`), then clears.

While the mark stands, ``open_project`` refuses to move the chat (:func:`person_chose_workspace`).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx

_logger = logging.getLogger(__name__)

#: ``"person"`` while the person's own folder choice stands.
WORKSPACE_CHOSEN_LABEL_KEY = "omnigent.superchat.workspace.chosen"
WORKSPACE_CHOSEN_BY_PERSON = "person"
#: The working directory the person just moved the chat to, until the next turn reads it.
WORKSPACE_MOVED_LABEL_KEY = "omnigent.superchat.workspace.moved"
#: The one-shot note :func:`deliver_person_move` writes, filled with the new working directory.
MOVED_NOTE = "The person moved this chat to {}."


def chosen_by_person(labels: Mapping[str, str] | None) -> bool:
    """True while the person's own folder choice stands on these session labels."""
    return (labels or {}).get(WORKSPACE_CHOSEN_LABEL_KEY) == WORKSPACE_CHOSEN_BY_PERSON


async def _session_labels(
    server_client: httpx.AsyncClient, conversation_id: str
) -> dict[str, str] | None:
    """The session's current labels (read fresh), or ``None`` when they cannot be read."""
    try:
        resp = await server_client.get(
            f"/v1/sessions/{conversation_id}",
            params={
                "include_items": "false",
                "include_liveness": "false",
                "include_usage": "false",
            },
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        labels = resp.json().get("labels")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return labels if isinstance(labels, dict) else {}


async def person_chose_workspace(server_client: httpx.AsyncClient, conversation_id: str) -> bool:
    """Whether the person chose this chat's folder (an unreadable session counts as no)."""
    return chosen_by_person(await _session_labels(server_client, conversation_id))


async def deliver_person_move(
    server_client: httpx.AsyncClient | None, conversation_id: str
) -> list[str]:
    """The one-shot "the person moved this chat" note, cleared once read. Never raises."""
    if server_client is None or not conversation_id:
        return []
    labels = await _session_labels(server_client, conversation_id)
    moved = " ".join((labels or {}).get(WORKSPACE_MOVED_LABEL_KEY, "").split())
    if not moved:
        return []
    try:
        await server_client.patch(
            f"/v1/sessions/{conversation_id}",
            json={"labels": {WORKSPACE_MOVED_LABEL_KEY: ""}},
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "could not clear the person-move note", extra={"session_id": conversation_id}
        )
    return [MOVED_NOTE.format(moved)]
