"""Project a session's raw items into the typed blocks a chat renders.

Pure: items in (the flat ``ConversationItem.to_api_dict()`` shape, oldest first), messages
out. The rules a client would otherwise copy:

* a message item is a ``text`` block; the runtime's own notices and hidden context are dropped;
* ``render_card`` / ``ask_clarification`` / ``suggest_follow_ups`` / ``vault_request_secret`` /
  ``artifact_save`` / ``deck_export`` / ``display_chart`` calls become
  ``card`` / ``secure_entry`` / ``file`` messages of their own (a call whose output says it
  failed shows nothing; a card still running is ``pending``), as does a ``deck_new`` that
  answered with its "Which look?" card;
* a delivered file (a ``resource_event`` whose ``resource_type`` is ``artifact``: something the
  person did, such as exporting a deck from the panel) becomes a ``file`` message marked
  ``"by": "user"``; it is not a tool call, and the Muse's history never replays it;
* a ``start_helper`` call becomes a ``helper`` block under the next assistant message;
* the pages a ``files_search`` / ``files_vsearch`` / ``files_query`` call found become one
  ``passages`` card (citation chips) under the next assistant message;
* a turn shows one assistant text: its last (a user message starts a turn). Text the
  Assistant wrote earlier in the same turn is neither hidden nor promoted: it is the Round's
  trail, which the Round view carries, so it is not a message here. The Helper rows and
  citation chips it would have carried move to the turn's last text. No word count and no card
  decides which text is the reply; a turn still running shows its newest text;
* a ``suggest_follow_ups`` card comes last in its turn, after the reply, whenever it was called;
* the person's latest turn that made a page from a data file (a ``.csv``, ``.xlsx``… named in
  their message or the turn's calls) and showed no chips ends with one "Keep this page updated
  when <file> changes" chip;
* one ``call_id`` is one block, however often the call repeats;
* an ``error`` item is an ``error`` block carrying its code, never its text; the code is read
  through ``public_error_code`` so legacy rows (exception names, old codes) show the closed set;
* in a file's studio (``keep_notices``) a runtime line is a ``system`` message with one
  ``notice`` block whose ``kind`` says what it records: ``hand_edit`` (a version the person made
  by hand, with its ``element_id``, ``element`` and ``what``), ``version`` (one saved elsewhere or
  restored), ``stale`` (saved elsewhere while the studio was editing) or ``queued``; the line's
  own text, written for the model, never travels.

Wording stays with the client: nothing here is copy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from omnigent.context.attachments import strip_legacy_attachment_context
from omnigent.entities.conversation import is_system_notice_text
from omnigent.harnesses.executor import ENDS_TURN_TOOLS
from omnigent.runtime.public_error_codes import public_error_code
from omnigent.superchat.artifact_kinds import KIND_MIME, is_deck_name
from omnigent.superchat.cards.tools import (
    CARD_TOOL_NAME,
    FOLLOW_UP_MAX_CHARS,
    FOLLOW_UPS_TOOL_NAME,
    TERMINAL_CARDS,
    build_follow_ups,
)
from omnigent.superchat.feature import RECORDED_RESPONSE_PREFIX
from omnigent.superchat.helpers.tools import START_HELPER_TOOL_NAME
from omnigent.superchat.subagents import STUDIO_QUEUED_NOTE

VAULT_REQUEST_TOOL_NAME = "vault_request_secret"
ARTIFACT_SAVE_TOOL_NAME = "artifact_save"
#: Returns the same ``{"type": "artifact", ...}`` result, so its file shows as the same card.
DECK_EXPORT_TOOL_NAME = "deck_export"
#: Also returns the ``{"type": "artifact", ...}`` result: the saved chart shows as a file card.
#: The tool is retired; the name stays so older conversations keep their charts.
CHART_TOOL_NAME = "display_chart"
#: Same result for a dashboard: one file card for the whole dashboard.
DASHBOARD_TOOL_NAME = "display_dashboard"
#: The calls whose ``{"type": "artifact", ...}`` result shows as a file card.
FILE_RESULT_TOOL_NAMES = (
    ARTIFACT_SAVE_TOOL_NAME,
    DECK_EXPORT_TOOL_NAME,
    CHART_TOOL_NAME,
    DASHBOARD_TOOL_NAME,
)

#: ``ResourceEventData`` fields of a file a person's own action delivered into the chat.
DELIVERED_ARTIFACT_RESOURCE = "artifact"
DELIVERED_EVENT = "session.resource.created"
FILES_SEARCH_TOOL_NAMES = ("files_search", "files_vsearch", "files_query")
#: Most pages shown as citation chips under one answer.
_MAX_CITATIONS = 6

Item = Mapping[str, Any]


def _is_call(item: Item, tool: str) -> bool:
    """Whether *item* calls *tool*, bare or behind a harness prefix (``mcp__omnigent__...``)."""
    name = item.get("name")
    return (
        item.get("type") == "function_call"
        and isinstance(name, str)
        and (name == tool or name.endswith(f"__{tool}"))
    )


def _object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def tool_outputs_by_call_id(items: list[Item]) -> dict[str, str]:
    """``call_id`` -> output text of every ``function_call_output`` in *items*."""
    return {
        item["call_id"]: item["output"] if isinstance(item.get("output"), str) else ""
        for item in items
        if item.get("type") == "function_call_output" and isinstance(item.get("call_id"), str)
    }


def item_text(item: Item) -> str:
    """The text of a message item's ``input_text`` / ``output_text`` parts, joined and trimmed.

    A user message stored by an older gateway carried the attached files' text in an
    ``<attachment_context>`` block; that is document data, not the person's words, so it is cut.
    """
    content = item.get("content")
    parts = [
        block["text"]
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict)
        and block.get("type") in ("output_text", "input_text")
        and isinstance(block.get("text"), str)
    ]
    text = "\n\n".join(parts).strip()
    return strip_legacy_attachment_context(text) if item.get("role") == "user" else text


def _card_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    card, fallback, data = payload.get("card"), payload.get("fallback"), payload.get("data")
    if not isinstance(card, str) or not isinstance(fallback, str) or not fallback.strip():
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {"card": card}
    for key in ("id", "title"):
        if isinstance(payload.get(key), str) and payload[key]:
            out[key] = payload[key]
    out["data"] = data
    out["fallback"] = fallback.strip()
    message = payload.get("message")
    if out["card"] in TERMINAL_CARDS and isinstance(message, str) and message.strip():
        out["message"] = message.strip()
    return out


def _card_block(call: Item, output: str | None) -> dict[str, Any] | None:
    """The ``card`` block of a ``render_card`` call; pending while it has no output."""
    pending = output is None
    if pending:
        args = _object(call.get("arguments"))
        payload = _card_payload({**args, "data": {}}) if args else None
    else:
        result = _object(output)
        payload = _card_payload(result) if result and result.get("type") == "card" else None
    if payload is None:
        return None
    return {
        "type": "card",
        "card_id": payload.get("id"),
        "card": payload,
        **({"pending": True} if pending else {}),
    }


def _secure_entry_block(output: str | None) -> dict[str, Any] | None:
    result = _object(output)
    nested = result.get("secure_entry") if result else None
    if not isinstance(nested, dict):
        return None
    request_id, name, site = nested.get("id"), nested.get("name"), nested.get("site")
    if not (isinstance(request_id, str) and isinstance(name, str) and isinstance(site, str)):
        return None
    reason = nested.get("reason")
    return {
        "type": "secure_entry",
        "request_id": request_id,
        "name": name,
        "site": site,
        **({"reason": reason} if isinstance(reason, str) else {}),
    }


def _file_block(output: str | None) -> dict[str, Any] | None:
    return file_block(_object(output))


def _delivered_file_block(item: Item) -> dict[str, Any] | None:
    """The ``file`` block of a file the person's own action delivered, marked ``by: user``."""
    if (
        item.get("event_type") != DELIVERED_EVENT
        or item.get("resource_type") != DELIVERED_ARTIFACT_RESOURCE
    ):
        return None
    block = file_block(item.get("resource"))
    return {**block, "by": "user"} if block else None


def file_block(saved: Any) -> dict[str, Any] | None:
    """The ``file`` block of a saved version (``{"type": "artifact", "id", "name", ...}``)."""
    if not isinstance(saved, dict) or saved.get("type") != "artifact":
        return None
    artifact_id, name = saved.get("id"), saved.get("name")
    if not isinstance(artifact_id, str) or not isinstance(name, str):
        return None
    kind = saved.get("kind")
    block: dict[str, Any] = {
        "type": "file",
        "artifact_id": artifact_id,
        "name": name,
        "mime": KIND_MIME.get(kind) if isinstance(kind, str) else None,
    }
    if isinstance(saved.get("title"), str) and saved["title"]:
        block["title"] = saved["title"]
    for key in ("kind", "size", "version", "versions"):
        if saved.get(key) is not None:
            block[key] = saved[key]
    return block


def _citations(output: str | None) -> list[dict[str, Any]]:
    """The distinct file pages a ``files_search`` result found, best first."""
    result = _object(output)
    if not result or result.get("type") != "file_search":
        return []
    found: list[dict[str, Any]] = []
    for hit in result.get("results") if isinstance(result.get("results"), list) else []:
        if not isinstance(hit, dict):
            continue
        file_id, name, page = hit.get("file_id"), hit.get("file_name"), hit.get("page")
        if not (isinstance(file_id, str) and isinstance(name, str) and isinstance(page, int)):
            continue
        artifact_id, path = hit.get("artifact_id"), hit.get("path")
        found.append(
            {
                "fileId": file_id,
                **({"artifactId": artifact_id} if isinstance(artifact_id, str) else {}),
                **({"path": path} if isinstance(path, str) and path else {}),
                "name": name,
                "page": page,
                **({"hasThumbnail": True} if hit.get("thumbnail_url") else {}),
            }
        )
    return found


def _citation_block(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One ``card`` block of citation chips for the pages searched this turn."""
    unique = list({(i["fileId"], i["page"]): i for i in items}.values())[:_MAX_CITATIONS]
    if not unique:
        return None
    label = ", ".join(f"{i['name']} p. {i['page']}" for i in unique)
    return {
        "type": "card",
        "card_id": None,
        "card": {
            "card": "passages",
            "data": {"items": unique},
            "fallback": f"Sources: {label}",
        },
    }


def _helper_block(
    call_id: str, output: str | None, statuses: Mapping[str, str]
) -> dict[str, Any] | None:
    """A started Helper; a refused or still-running call has none to show."""
    receipt = _object(output)
    if not receipt or receipt.get("started") is not True:
        return None
    session_id = receipt.get("helper_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    title = receipt.get("title")
    return {
        "type": "helper",
        "call_id": call_id,
        "session_id": session_id,
        "title": title if isinstance(title, str) else "",
        "status": statuses.get(session_id, "unknown"),
    }


def helper_session_ids(items: list[Item]) -> list[str]:
    """The Helper sessions *items* started, so a caller can look their status up."""
    outputs = tool_outputs_by_call_id(items)
    found: list[str] = []
    for item in items:
        if _is_call(item, START_HELPER_TOOL_NAME):
            receipt = _object(outputs.get(str(item.get("call_id") or "")))
            if receipt and receipt.get("started") is True:
                helper_id = receipt.get("helper_id")
                if isinstance(helper_id, str) and helper_id:
                    found.append(helper_id)
    return found


#: The studio's runtime lines (``artifacts/studio.py``: ``notice_line`` and ``stale_line``).
_HAND_LINE = re.compile(r"^\[you\] (?P<summary>.*) · v(?P<version>\d+)$", re.S)
_RESTORED = "restored version "
_VERSION_LINE = re.compile(
    r"^\[[^\]\n]+\] (?:.* · v(?P<version>\d+)|saved v(?P<saved>\d+))$", re.S
)
_STALE_LINE = re.compile(
    r"^(?P<who>Super Chat|A live update) saved v(?P<version>\d+) while you were editing; "
)


def notice_block(text: str) -> dict[str, Any] | None:
    """A studio runtime line as a ``notice`` block; ``None`` for a line it does not record."""
    # ``artifacts`` imports this module at load (its routes project transcripts).
    from omnigent.superchat.artifacts.patches import edit_note

    hand = _HAND_LINE.match(text)
    if hand and not hand["summary"].startswith(_RESTORED):
        note = edit_note(hand["summary"])
        block: dict[str, Any] = {
            "type": "notice",
            "kind": "hand_edit",
            "version": int(hand["version"]),
            "what": note.what,
        }
        if note.element_id:
            block["element_id"] = note.element_id
            block["element"] = note.element
        return block
    stale = _STALE_LINE.match(text)
    if stale:
        origin = "superchat" if stale["who"] == "Super Chat" else "update"
        return {
            "type": "notice",
            "kind": "stale",
            "version": int(stale["version"]),
            "origin": origin,
        }
    version = _VERSION_LINE.match(text)
    if version:
        number = version["version"] or version["saved"]
        return {"type": "notice", "kind": "version", "version": int(number)}
    if text.strip() == STUDIO_QUEUED_NOTE:
        return {"type": "notice", "kind": "queued"}
    return None


def turn_activity_id(conversation_id: str, response_id: str) -> str:
    """The id of the Activity a turn is (``activity/derive.py``): ``turn:<chat>:<response>``."""
    return f"turn:{conversation_id}:{response_id}"


def _message(item: Item, role: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "role": role,
        "created_at": item.get("created_at"),
        "blocks": blocks,
    }


#: Instant card tools: the card appears with its result, never as a skeleton (``deck_new``
#: shows its "Which look?" card this way).
_INSTANT_CARD_TOOLS = (*sorted(ENDS_TURN_TOOLS), FOLLOW_UPS_TOOL_NAME)


def _last_text_ids(items: list[Item]) -> dict[int, str]:
    """Turn number -> the id of that turn's last assistant text (a user message starts a turn).

    Nothing else decides which text is the reply: no word count, no card that closes the turn.
    A turn still running shows its newest text until a later one replaces it. A turn with no
    text of its own shows its answer card's ``message`` as its words.
    """
    last: dict[int, str] = {}
    turn = 0
    for item in items:
        if item.get("type") != "message" or item.get("is_meta") is True:
            continue
        if item.get("role") == "user":
            turn += 1
        elif item.get("role") == "assistant" and item_text(item) and "id" in item:
            last[turn] = item["id"]
    return last


def project_items(
    items: list[Item],
    *,
    helper_statuses: Mapping[str, str] | None = None,
    keep_notices: bool = False,
    conversation_id: str | None = None,
) -> list[dict[str, Any]]:
    """Turn *items* (oldest first) into transcript messages.

    :param items: ``ConversationItem.to_api_dict()`` dicts in ascending order.
    :param helper_statuses: Helper session id -> status, for the ``helper`` blocks.
    :param keep_notices: Keep the studio's runtime lines as ``system`` messages with one
        ``notice`` block (a file's studio, ADR 0024); dropped everywhere else.
    :param conversation_id: The session *items* belong to: each assistant message then carries
        ``activity_id``, the Activity of the turn that wrote it (:func:`turn_activity_id`).
    :returns: ``[{"id", "role", "created_at", "blocks", "activity_id"?}, ...]``.
    """
    statuses = helper_statuses or {}
    outputs = tool_outputs_by_call_id(items)
    messages: list[dict[str, Any]] = []
    helpers: list[dict[str, Any]] = []
    cited: list[dict[str, Any]] = []
    seen_calls: set[str] = set()
    said = _last_text_ids(items)
    final_text = set(said.values())
    turn = 0  # counted as :func:`_last_text_ids` counts
    chips: list[dict[str, Any]] = []  # the turn's follow-ups, shown after its last message
    for item in items:
        kind = item.get("type")
        if kind == "message" and item.get("role") == "user" and item.get("is_meta") is not True:
            turn += 1
        if kind == "function_call":
            call_id = str(item.get("call_id") or "")
            if call_id and call_id in seen_calls:
                continue
            if call_id:
                seen_calls.add(call_id)
            output = outputs.get(call_id)
            block: dict[str, Any] | None = None
            if _is_call(item, START_HELPER_TOOL_NAME):
                helper = _helper_block(call_id, output, statuses)
                if helper:
                    helpers.append(helper)
                continue
            if any(_is_call(item, name) for name in FILES_SEARCH_TOOL_NAMES):
                cited.extend(_citations(output))
                continue
            if _is_call(item, CARD_TOOL_NAME):
                block = _card_block(item, output)
                words = block["card"].get("message") if block else None
                if words and turn not in said:
                    messages.append(
                        _message(item, "assistant", [{"type": "text", "text": words}, block])
                    )
                    continue
            elif any(_is_call(item, name) for name in _INSTANT_CARD_TOOLS):
                block = _card_block(item, output) if output is not None else None
                if block and _is_call(item, FOLLOW_UPS_TOOL_NAME):
                    chips.append(_message(item, "assistant", [block]))
                    continue
            elif _is_call(item, VAULT_REQUEST_TOOL_NAME):
                block = _secure_entry_block(output)
            elif any(_is_call(item, name) for name in FILE_RESULT_TOOL_NAMES):
                block = _file_block(output)
            if block:
                messages.append(_message(item, "assistant", [block]))
        elif kind == "resource_event":
            delivered = _delivered_file_block(item)
            if delivered:
                messages.append(_message(item, "assistant", [delivered]))
        elif kind == "error":
            code = item.get("code")
            if isinstance(code, str) and code:
                block = {"type": "error", "code": public_error_code(code)}
                if item.get("level") == "info":
                    block["level"] = "info"
                messages.append(_message(item, "assistant", [block]))
        elif kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant") or item.get("is_meta") is True:
                continue
            if role == "user":
                messages.extend(chips)  # chips come last in the turn they were offered in
                chips = []
            text = item_text(item)
            if not text:
                continue
            if role == "user" and (
                item.get("is_system_notice") is True or is_system_notice_text(text)
            ):
                notice = notice_block(text) if keep_notices else None
                if notice:
                    messages.append(_message(item, "system", [notice]))
                continue
            if role == "assistant" and item.get("id") not in final_text:
                continue  # earlier text of the turn: the Round's trail carries it
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
            if role == "user":
                cited = []
            if role == "assistant" and helpers:
                blocks.extend(helpers)
                helpers = []
            if role == "assistant" and cited:
                citations = _citation_block(cited)
                if citations:
                    blocks.append(citations)
                cited = []
            messages.append(_message(item, role, blocks))
    messages.extend(chips)
    _offer_keep_updated(items, messages)
    if conversation_id:
        turns = {i.get("id"): i.get("response_id") for i in items if i.get("response_id")}
        for message in messages:
            response_id = turns.get(message["id"])
            if (
                message["role"] == "assistant"
                and isinstance(response_id, str)
                and not response_id.startswith(RECORDED_RESPONSE_PREFIX)
            ):
                message["activity_id"] = turn_activity_id(conversation_id, response_id)
    return messages


#: A file with one of these endings holds data a page can show (and so can go stale).
_DATA_FILE = re.compile(r"[\w.-]+\.(?:csv|tsv|xlsx|xls|json|parquet)\b", re.IGNORECASE)


def _new_page(block: Mapping[str, Any]) -> bool:
    """Whether *block* shows a page made this turn: a first version, or a Helper's file card."""
    if block.get("type") == "file":
        name, version = block.get("name"), block.get("version")
    elif block.get("type") == "card" and block["card"].get("card") == "file":
        name, version = block["card"]["data"].get("name"), None
    else:
        return False
    return (
        isinstance(name, str)
        and name.lower().endswith(".html")
        and not is_deck_name(name)
        and version in (None, 1)
    )


def _offer_keep_updated(items: list[Item], messages: list[dict[str, Any]]) -> None:
    """Add the "Keep this page updated" chip to the person's latest turn when it made a page from
    a data file and showed no chips of its own.

    The engine offers it instead of the Muse, which forgets it at the end of long turns. Chips
    show on the latest answer only, so the offer is gone after the person's next message.
    """
    asked = next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "user"), -1
    )
    turn = messages[asked + 1 :]
    blocks = [block for message in turn for block in message["blocks"]]
    if (
        asked < 0
        or not turn
        or turn[-1]["role"] != "assistant"
        or not any(_new_page(block) for block in blocks)
        or any(b.get("type") == "card" and b["card"].get("card") == "follow_ups" for b in blocks)
    ):
        return
    start = next(i for i, item in enumerate(items) if item.get("id") == messages[asked]["id"])
    said = [item_text(items[start])] + [
        str(item.get("arguments") or "")
        for item in items[start + 1 :]
        if item.get("type") == "function_call"
    ]
    found = next((m.group(0) for text in said for m in _DATA_FILE.finditer(text)), None)
    if found is None:
        return
    offer = f"Keep this page updated when {found} changes"
    if len(offer) > FOLLOW_UP_MAX_CHARS:
        offer = "Keep this page updated when the file changes"
    payload = _card_payload(build_follow_ups({"suggestions": [offer]}))
    if payload:
        turn[-1]["blocks"].append({"type": "card", "card_id": None, "card": payload})
