"""Posting a run's Result by code (ADR 0038, "One run, posted by code").

The Result becomes an assistant message in the person's Conversation, recorded outside any turn
the way a picked topic's card is (a ``recorded_`` response id: no Activity of its own), with its
outbox row written before it is published so the proactive label and the web's dot are there
when a client refetches. The message's id is the wake's id, so a repeat (a restart, a second
report of one run) stores and publishes nothing new. The file the run saved (the draft, the
brief's page) is linked to the message (ADR 0021's message link, ``made``), so the transcript
shows it as the file's card under the message instead of a name in the text.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from omnigent.superchat.feature import (
    RECORDED_RESPONSE_PREFIX,
    link_made_file,
    record_proactive,
)

_logger = logging.getLogger(__name__)


def result_text(conversation_store: Any, run_conversation_id: str) -> tuple[str, str] | None:
    """The run's Result: ``(text, agent)`` of the last visible assistant message of its session
    (blocking), the agent being the model that wrote it."""
    from omnigent.entities import MessageData

    page = conversation_store.list_items(run_conversation_id, limit=50, order="desc")
    for item in page.data:
        data = item.data
        if (
            item.type == "message"
            and isinstance(data, MessageData)
            and data.role == "assistant"
            and not data.is_meta
        ):
            text = "\n".join(
                b["text"]
                for b in data.content
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ).strip()
            if text:
                return text, data.agent or "assistant"
    return None


#: Text a policy or the system writes, never the assistant's own words: a deny notice handed to
#: the model ("Blocked by the session cost-budget policy ... relay this to the user verbatim"), a
#: budget or pricing notice. A Result that carries one is never posted.
_POLICY_TEXT = re.compile(
    r"blocked by (?:the )?[\w '\-]*polic|relay this to the user verbatim|cost[- ]budget"
    r"|over (?:the )?budget|raise the budget|no catalog pricing|blocked by policy",
    re.IGNORECASE,
)


def is_policy_text(text: str | None) -> bool:
    """Whether *text* carries a policy or system notice (never posted to the person)."""
    return bool(text) and _POLICY_TEXT.search(text or "") is not None


def run_error(conversation_store: Any, run_conversation_id: str) -> str | None:
    """Why the run did not finish (blocking): the error it recorded (a refused model call, a
    cap), so its last words are not its Result; ``None`` for a run that finished."""
    from omnigent.entities.conversation import ErrorData

    page = conversation_store.list_items(run_conversation_id, limit=200, order="desc")
    for item in page.data:
        if isinstance(item.data, ErrorData) and item.data.level != "info":
            return f"the run recorded an error ({item.data.code})"
    return None


def denied_call(conversation_store: Any, run_conversation_id: str) -> str | None:
    """The newest tool output that reads as a policy notice, for the log (blocking). Never a
    reason to drop a Result: a read's output can quote one (a recalled chat about a budget), and
    a run that was refused one call can still finish its work. ``None`` when there is none."""
    from omnigent.entities.conversation import FunctionCallOutputData

    page = conversation_store.list_items(run_conversation_id, limit=200, order="desc")
    for item in page.data:
        if (
            item.type == "function_call_output"
            and isinstance(item.data, FunctionCallOutputData)
            and is_policy_text(item.data.output)
        ):
            return " ".join(str(item.data.output).split())[:160]
    return None


def saved_file(conversation_store: Any, run_conversation_id: str) -> str | None:
    """The id of the last file version the run saved (an ``artifact_save``-shaped result,
    ``{"type": "artifact", "id", ...}``), or ``None`` (blocking)."""
    found = saved_file_info(conversation_store, run_conversation_id)
    return found[0] if found else None


def saved_file_info(conversation_store: Any, run_conversation_id: str) -> tuple[str, str] | None:
    """``(id, title)`` of the last file version the run saved, the title its ``title`` or
    ``name`` (``""`` when it has neither), or ``None`` (blocking)."""
    from omnigent.entities.conversation import FunctionCallOutputData

    page = conversation_store.list_items(run_conversation_id, limit=100, order="desc")
    for item in page.data:
        if item.type != "function_call_output" or not isinstance(
            item.data, FunctionCallOutputData
        ):
            continue
        try:
            result = json.loads(item.data.output)
        except (TypeError, ValueError):
            continue
        if (
            isinstance(result, dict)
            and result.get("type") == "artifact"
            and isinstance(result.get("id"), str)
        ):
            title = result.get("title") or result.get("name") or ""
            return result["id"], title if isinstance(title, str) else ""
    return None


def post_message(
    conversation_store: Any,
    *,
    conversation_id: str,
    owner: str,
    wake_id: str,
    text: str,
    agent: str,
    kind: str,
    urgency: str,
    topic_key: str | None,
    artifact_id: str | None = None,
) -> str:
    """Store *text* as an assistant message in the Conversation, write its outbox row, link the
    file the run made (*artifact_id*), publish it (blocking).

    :returns: The stored message's item id.
    """
    from omnigent.entities import MessageData, NewConversationItem
    from omnigent.server.routes._sessions.helpers import _publish_external_conversation_item

    response_id = f"{RECORDED_RESPONSE_PREFIX}lookout_{wake_id}"
    persisted = conversation_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant", agent=agent, content=[{"type": "output_text", "text": text}]
                ),
                stable_id=wake_id,
            )
        ],
    )
    item = persisted[0]
    try:
        record_proactive(
            owner=owner,
            conversation_id=conversation_id,
            item_id=item.id,
            response_id=response_id,
            text=text,
            kind=kind,
            urgency=urgency,
            topic_key=topic_key,
        )
    except Exception:  # the message is said either way; only its label is missing
        _logger.exception("lookout: outbox row for %s failed", item.id)
    if artifact_id:
        try:
            if not link_made_file(conversation_id, item.id, artifact_id):
                _logger.warning("lookout: file %s not linked to %s", artifact_id, item.id)
        except Exception:  # the message stands without its card
            _logger.exception("lookout: linking file %s to %s failed", artifact_id, item.id)
    if not getattr(item, "deduplicated", False):
        _publish_external_conversation_item(conversation_id, item)
    return item.id
