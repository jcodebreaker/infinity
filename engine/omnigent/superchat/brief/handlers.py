"""Runner-side handler of ``brief_save``: lay the content out, save it as the next version.

The page goes through the same ``POST /v1/artifacts`` every saved file does, into the run's
Conversation, so versions, the file card and the Library behave as for any other file. Only the
brief run of that Conversation may save it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from omnigent.context.labels import background_task_id
from omnigent.superchat._handler_http import error, finish, resolve_caller
from omnigent.superchat.brief.page import BRIEF_NAME, parse_brief, render_page
from omnigent.superchat.brief.task import brief_task_id
from omnigent.superchat.feature import HandlerCtx


async def handle_brief_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """Save the brief page; the output is the saved version, as ``artifact_save`` returns it."""
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    chat_id = caller.chat_id
    run_task = background_task_id(ctx.labels)
    if not caller.is_helper or not chat_id or run_task != brief_task_id(chat_id):
        return error(f"{ctx.tool_name} is only for the morning brief run")
    try:
        brief = parse_brief(args, datetime.now(UTC).date())
    except ValueError as exc:
        return error(f"{ctx.tool_name}: {exc}")
    url = (
        f"/v1/artifacts?parent_session_id={chat_id}&name={quote(BRIEF_NAME)}"
        f"&title={quote(brief.title)}&chat_id={quote(caller.conversation_id)}"
    )
    data = render_page(brief)
    try:
        resp = await _post(caller.client, url, data)
        if resp.status_code == 409 and await _accept_hand_edit(caller.client, chat_id):
            resp = await _post(caller.client, url, data)
    except Exception as exc:  # noqa: BLE001
        return error(f"{ctx.tool_name} failed: {exc}")
    if resp.status_code >= 400:
        return finish(resp)
    return json.dumps({"type": "artifact", **resp.json(), "line": brief.line})


async def _post(client: Any, url: str, data: bytes) -> Any:
    return await client.post(
        url, content=data, headers={"Content-Type": "application/octet-stream"}, timeout=60.0
    )


async def _accept_hand_edit(client: Any, chat_id: str) -> bool:
    """Let a new day's brief go on top of a hand edit of the last one.

    The save route refuses to cover a hand edit the Conversation was not told about yet. A brief
    is a new page each day, not an edit of yesterday's: the person's edit stays in the version
    history, so the run marks it seen and saves. ``True`` when it did.
    """
    listing = await client.get(f"/v1/artifacts?parent_session_id={chat_id}", timeout=30.0)
    items = listing.json().get("artifacts", []) if listing.status_code == 200 else []
    head = next((a for a in items if a.get("name") == BRIEF_NAME), None)
    if head is None:
        return False
    done = await client.post(f"/v1/artifacts/{head['id']}/delivered", timeout=30.0)
    return done.status_code == 200 and bool(done.json().get("delivered"))
