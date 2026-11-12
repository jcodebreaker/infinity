"""The person's wait, measured by the relay (ADR 0026).

Three INFO lines per Conversation turn, all counted from the moment the engine
accepted the person's message that started it: the first visible event (a word,
a tool call such as a plan or card starting, or the model starting to think: the
Working line is up from the turn's start, and a thinking block is the first sign the
model itself is on it), the first word, and a turn that
ended without a word. The events route stamps the acceptance in memory
(:func:`mark_accepted`, sub-second); without a stamp (another replica accepted
it, or the engine restarted) the clock falls back to the message's whole-second
``created_at``. A turn with no fresh person message (a Helper's notice, a second
response for the same message) logs nothing. Never raises.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from omnigent.server.routes._sessions.common import _TERMINAL_RESPONSE_EVENT_TYPES
from omnigent.superchat.artifacts.links import is_person_message

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)

# How far back to look for the person's message; a turn's own items come after it.
_LOOKBACK = 20
# A stamp further than this from the message's ``created_at`` belongs to another message.
_STAMP_MATCH_S = 2.0
# Sessions whose stamps are kept; past this the oldest half is dropped.
_MAX_STAMPS = 4096

# session id -> (wall time, monotonic time) of the person's latest accepted message.
_accepted: dict[str, tuple[float, float]] = {}


def mark_accepted(session_id: str) -> None:
    """Stamp the moment the engine accepted a message from the person in *session_id*."""
    if len(_accepted) >= _MAX_STAMPS:
        for stale in list(_accepted)[: _MAX_STAMPS // 2]:
            _accepted.pop(stale, None)
    _accepted.pop(session_id, None)
    _accepted[session_id] = (time.time(), time.monotonic())


def _is_visible(event: dict[str, Any]) -> bool:
    """A frame the person sees: a text delta, a tool call (plans and cards are tool calls),
    or a thinking block opening (both harnesses think before most answers)."""
    evt_type = event.get("type")
    if evt_type in (
        "response.output_text.delta",
        "response.function_call_output.delta",
        "response.reasoning.started",
    ):
        return True
    if not isinstance(evt_type, str) or not evt_type.startswith("response.output_item."):
        return False
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    if not isinstance(item_type, str):
        return False
    return item_type.endswith("_call") or (item_type == "message" and _has_text(item))


def _has_text(item: dict[str, Any]) -> bool:
    """Whether an assistant message item carries any text (a native harness's word)."""
    if item.get("role") != "assistant":
        return False
    content = item.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"]
        for block in content
    )


def _is_word(event: dict[str, Any]) -> bool:
    """A frame that carries the reply's text."""
    if event.get("type") == "response.output_text.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    item = event.get("item")
    return (
        isinstance(event.get("type"), str)
        and event["type"].startswith("response.output_item.")
        and isinstance(item, dict)
        and item.get("type") == "message"
        and _has_text(item)
    )


def _person_message(store: ConversationStore, session_id: str) -> tuple[str, int] | None:
    """The newest message the person wrote: ``(item id, created_at)``, or ``None``."""
    page = store.list_items(session_id, limit=_LOOKBACK, order="desc", type="message")
    return next(((i.id, i.created_at) for i in page.data if is_person_message(i)), None)


class TurnClock:
    """
    One relay's per-turn clock.

    :param session_id: The conversation whose turns are timed, e.g. ``"conv_abc123"``.
    """

    def __init__(self, session_id: str) -> None:
        """Start idle: nothing is timed until :meth:`start` finds a fresh message."""
        self._session_id = session_id
        self._message_id: str | None = None
        self._since: float | None = None
        # The clock ``_since`` is on: monotonic from a stamp, wall time from ``created_at``.
        self._clock = time.time
        self._visible = False
        self._worded = False

    async def start(self, store: ConversationStore) -> None:
        """Arm the clock from the person's newest message, unless an earlier turn used it."""
        try:
            found = await asyncio.to_thread(_person_message, store, self._session_id)
        except Exception:
            _logger.debug("turn metrics: message lookup failed", exc_info=True)
            found = None
        self.arm(found, _accepted.get(self._session_id))

    def arm(
        self, found: tuple[str, int] | None, accepted: tuple[float, float] | None = None
    ) -> None:
        """Arm the clock from ``(message id, created_at)``; ``None`` disarms it.

        *accepted* is the engine's ``(wall, monotonic)`` acceptance stamp: used when it
        matches the message's ``created_at``, else the whole-second ``created_at`` is. The
        message already timed leaves the clock as it is: still running mid-turn, idle after.
        """
        if found is None:
            self._since = None
            return
        message_id, created_at = found
        if message_id == self._message_id:
            return
        self._message_id = message_id
        if accepted is not None and abs(accepted[0] - created_at) <= _STAMP_MATCH_S:
            self._since, self._clock = accepted[1], time.monotonic
        else:
            self._since, self._clock = float(created_at), time.time
        self._visible = self._worded = False

    def observe(self, event: dict[str, Any], now: float | None = None) -> None:
        """Log the first visible event, the first word, and a turn that ended without one."""
        if self._since is None:
            return
        try:
            elapsed = (self._clock() if now is None else now) - self._since
            if not self._visible and _is_visible(event):
                self._visible = True
                self._log(
                    "first visible event for %s %.1fs after the message",
                    elapsed,
                    "first_visible_event_s",
                )
            if not self._worded and _is_word(event):
                self._worded = True
                self._log("first word for %s %.1fs after the message", elapsed, "first_word_s")
            if event.get("type") in _TERMINAL_RESPONSE_EVENT_TYPES:
                if not self._worded:
                    self._log(
                        "no text for %s: turn ended after %.1fs without a word",
                        elapsed,
                        "silent_turn_s",
                    )
                self._since = None
        except Exception:
            _logger.debug("turn metrics: observe failed", exc_info=True)

    def _log(self, message: str, elapsed: float, key: str) -> None:
        """One INFO line, with the session and the seconds in ``extra``."""
        _logger.info(
            message,
            self._session_id,
            elapsed,
            extra={"session_id": self._session_id, key: round(elapsed, 3)},
        )
