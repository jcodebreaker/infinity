"""Runner side of the context brief: built when the ``context_brief`` tool is called.

The call reads the person's latest message and builds its brief within
:data:`~.brief.BUDGET_SECONDS`; past :data:`BRIEF_WAIT_SECONDS` of the call's own time it returns
an empty brief and leaves the build running for the next call. One brief per session: kept for
:data:`BRIEF_TTL_SECONDS` or until the turn ends, so a second call in the turn gets the same text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from omnigent.entities.conversation import is_system_notice_text
from omnigent.superchat._handler_http import error
from omnigent.superchat.context_brief.brief import BUDGET_SECONDS, Brief
from omnigent.superchat.context_brief.sources import RunnerSources
from omnigent.superchat.feature import HandlerCtx, is_helper

_logger = logging.getLogger(__name__)

#: How long a session's brief answers the tool after it was started.
BRIEF_TTL_SECONDS = 120.0
#: Longest the tool call waits: reading the message, the build's own budget, room to render.
BRIEF_WAIT_SECONDS = BUDGET_SECONDS + 1.0
_READ_TIMEOUT_SECONDS = 0.5
_RECENT_ITEMS = 10


@dataclass(frozen=True)
class _Entry:
    message: str
    started: float
    task: asyncio.Task[str]


#: Per session: the brief of the turn in flight.
_briefs: dict[str, _Entry] = {}


async def _render(client: httpx.AsyncClient, conversation_id: str, message: str) -> str:
    try:
        brief = await Brief.build(conversation_id, message, RunnerSources(client))
    except Exception:  # the brief is never a reason to fail a call
        _logger.warning("context brief failed; an empty one is returned", exc_info=True)
        brief = Brief()
    return brief.render()


def start_brief(
    client: httpx.AsyncClient, conversation_id: str, message: str
) -> asyncio.Task[str]:
    """The session's brief for *message*: the one already building, or a new build."""
    entry = _briefs.get(conversation_id)
    now = time.monotonic()
    if entry is None or entry.message != message or now - entry.started > BRIEF_TTL_SECONDS:
        task = asyncio.get_running_loop().create_task(_render(client, conversation_id, message))
        entry = _Entry(message, now, task)
        _briefs[conversation_id] = entry
    return entry.task


def forget_brief(conversation_id: str) -> None:
    """Drop the session's brief (its turn ended)."""
    _briefs.pop(conversation_id, None)


async def _latest_message(client: httpx.AsyncClient, conversation_id: str) -> str:
    """The person's latest message; a runtime notice (a Helper finished, a timer fired) is not
    one."""
    from omnigent.superchat.recall_hint.hint import item_text

    resp = await client.get(
        f"/v1/sessions/{conversation_id}/items",
        params={"limit": str(_RECENT_ITEMS), "order": "desc"},
        timeout=_READ_TIMEOUT_SECONDS,
    )
    items = resp.json().get("data", []) if resp.status_code == 200 else []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "user":
            text = item_text(item).strip()
            if text and not is_system_notice_text(text):
                return text
    return ""


async def handle_context_brief(ctx: HandlerCtx, _args: dict[str, object]) -> str:
    """The brief for the person's latest message, built now or by an earlier call this turn."""
    if ctx.server_client is None or not ctx.conversation_id or is_helper(ctx.labels):
        return error("context_brief is for the conversation only")
    started = time.monotonic()
    try:
        message = await _latest_message(ctx.server_client, ctx.conversation_id)
    except (httpx.HTTPError, ValueError, AttributeError):
        message = ""
    task = start_brief(ctx.server_client, ctx.conversation_id, message)
    left = max(0.0, BRIEF_WAIT_SECONDS - (time.monotonic() - started))
    # A call cancelled mid-wait, or out of time, leaves the brief building for the next one.
    try:
        return await asyncio.wait_for(asyncio.shield(task), left)
    except TimeoutError:
        _logger.info(
            "context brief not ready within %.1fs; an empty one is returned", BRIEF_WAIT_SECONDS
        )
        return Brief().render()
