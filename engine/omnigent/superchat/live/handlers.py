"""Runner-side handlers of the live artifact tools: they call ``/v1/artifacts/{id}/live``."""

from __future__ import annotations

from typing import Any

from omnigent.superchat._handler_http import error, finish, resolve_caller
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.live.tools import (
    CONTEXT_TOOL_NAME,
    HELPER_TOOL_NAMES,
    LIVE_REQUIRED,
    SAVE_TOOL_NAME,
)

#: ``live`` action or Helper tool ``-> (method, path after /live, body keys from the arguments)``.
_CALLS = {
    "keep": ("PUT", "", ("rrule", "sources", "tell", "on_change", "note")),
    "settings": ("GET", "", ()),
    "stop": ("DELETE", "", ()),
    CONTEXT_TOOL_NAME: ("GET", "/context", ()),
    SAVE_TOOL_NAME: ("POST", "/save", ("base_version", "changes", "facts", "sources")),
}


async def handle_live_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """Run one live artifact tool on the page named in the caller's chat."""
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    tool, chat_id = ctx.tool_name, caller.chat_id
    if (tool in HELPER_TOOL_NAMES) != caller.is_helper:
        return error(f"{tool} is not available here")
    call = tool
    if tool not in HELPER_TOOL_NAMES:
        call = str(args.get("action") or "")
        if call not in LIVE_REQUIRED:
            return error(f"{tool} action must be one of {list(LIVE_REQUIRED)}")
        missing = next((f for f in LIVE_REQUIRED[call] if not args.get(f)), None)
        if missing is not None:
            return error(f"{tool} {call} needs '{missing}'")
        tool = f"{tool} {call}"
    name = str(args.get("name") or "").strip()
    if not chat_id or not name:
        return error(f"{tool} needs a page name in a chat")
    method, path, keys = _CALLS[call]
    try:
        listing = await caller.client.get(f"/v1/artifacts?parent_session_id={chat_id}")
        items = listing.json().get("artifacts", []) if listing.status_code == 200 else []
        found = next((a for a in items if a.get("name") == name), None)
        if found is None:
            return error(f"no saved file named {name} in this chat")
        body = {k: args[k] for k in keys if args.get(k) is not None} if keys else None
        url = f"/v1/artifacts/{found['id']}/live{path}"
        if tool == SAVE_TOOL_NAME:  # the version records the run that made it
            url += f"?chat_id={caller.conversation_id}"
        return finish(await caller.client.request(method, url, json=body, timeout=60.0))
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool} failed: {exc}")
