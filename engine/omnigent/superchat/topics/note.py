"""The one-shot note after a pick (ADR 0035, amended): a warm session holds its own history and
never reads the card the engine appended between turns, so the next turn is told in one line.

The pick marks the Conversation with :data:`PICKED_LABEL_KEY` ``= "<key>\\t<title>"``;
:func:`deliver_topic_pick` (the feature's ``turn_prefix``) turns it into :data:`PICKED_NOTE`,
with the topic as last written fenced as data, and clears it through the generic labels write
(``PATCH /v1/sessions/{id}``). The card stays the record.

The pick also marks the topic active (:data:`ACTIVE_LABEL_KEY` ``= "<key>\\t<picked at>"``)
until another pick replaces it or :data:`ACTIVE_SECONDS` pass: the Conversation may update
that topic, and only that one, with ``topics_update`` (ADR 0035, amended 2026-10-23).
"""

from __future__ import annotations

import logging

import httpx

from omnigent.superchat.topics.topic import fenced_body

_logger = logging.getLogger(__name__)

#: The topic the person just picked up, until the next turn reads it.
PICKED_LABEL_KEY = "omnigent.superchat.topics.picked"
#: The topic the Conversation is working on, and when it was picked.
ACTIVE_LABEL_KEY = "omnigent.superchat.topics.active"
#: How long after a pick the Conversation may still update the picked topic.
ACTIVE_SECONDS = 3 * 3600
#: The one-shot note, filled with the topic's title and key: data only; what to do with an
#: active topic is the Conversation's prompt (``agents/templates/AGENTS.md``).
PICKED_NOTE = (
    "The person picked up the topic {title} (`{key}`): it is the topic this conversation is "
    "working on."
)


def picked_value(key: str, title: str) -> str:
    """The label value a pick writes."""
    return f"{key}\t{' '.join(title.split())}"


def active_value(key: str, at: int) -> str:
    """The active-topic label value a pick writes (*at*: epoch seconds of the pick)."""
    return f"{key}\t{int(at)}"


def active_key(value: object, now: float) -> str:
    """The active topic's key from its label value, ``""`` when unset, unreadable or older than
    :data:`ACTIVE_SECONDS`."""
    key, _, at = value.partition("\t") if isinstance(value, str) else ("", "", "")
    try:
        picked_at = int(at)
    except ValueError:
        return ""
    return key if key and 0 <= now - picked_at <= ACTIVE_SECONDS else ""


def conversation_refusal(keys: list[object], active: str) -> str:
    """Why the Conversation may not write these ``topics_update`` keys, ``""`` when it may: only
    the active topic, and only while it is active."""
    if not active:
        return "no topic was picked up in the last 3 hours; leave topics as they are"
    if any(key != active for key in keys):
        return f"only the topic picked up (`{active}`) can be updated here"
    return ""


async def _topic_body(server_client: httpx.AsyncClient, key: str) -> str:
    """The picked topic as last written, fenced as data; ``""`` when it cannot be read."""
    try:
        resp = await server_client.get("/v1/me/topics", params={"days": 7}, timeout=10.0)
        topics = resp.json().get("topics") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return ""
    for topic in topics if isinstance(topics, list) else []:
        if isinstance(topic, dict) and topic.get("key") == key:
            return fenced_body(
                left_off=str(topic.get("left_off") or ""),
                covered=[c for c in topic.get("covered") or [] if isinstance(c, dict)],
                open_=[o for o in topic.get("open") or [] if isinstance(o, dict)],
            )
    return ""


async def deliver_topic_pick(
    server_client: httpx.AsyncClient | None, conversation_id: str
) -> list[str]:
    """The one-shot "the person picked up this topic" note, cleared once read. Never raises."""
    if server_client is None or not conversation_id:
        return []
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
        labels = resp.json().get("labels") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    picked = labels.get(PICKED_LABEL_KEY, "") if isinstance(labels, dict) else ""
    key, _, title = picked.partition("\t") if isinstance(picked, str) else ("", "", "")
    if not key:
        return []
    try:
        await server_client.patch(
            f"/v1/sessions/{conversation_id}",
            json={"labels": {PICKED_LABEL_KEY: ""}},
            timeout=10.0,
        )
    except httpx.HTTPError:
        _logger.warning(
            "could not clear the topic pick note", extra={"session_id": conversation_id}
        )
    note = PICKED_NOTE.format(title=title or key, key=key)
    body = await _topic_body(server_client, key)
    return [f"{note} Its state as last written:\n{body}" if body else note]
