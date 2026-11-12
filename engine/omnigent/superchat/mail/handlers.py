"""Runner-side handler of ``mail_read_thread``: it asks the engine, which holds the mailbox."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from omnigent.superchat._handler_http import error, finish, resolve_caller
from omnigent.superchat.feature import HandlerCtx


async def handle_mail_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """Read one thread; the engine checks the calling run may (the Lookout's or the brief's)."""
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    if not caller.is_helper:
        return error(f"{ctx.tool_name} is only for the Lookout's and the brief's runs")
    thread_id = str(args.get("thread_id") or "").strip()
    if not thread_id or len(thread_id) > 255:
        return error(f"{ctx.tool_name} needs a thread_id")
    url = f"/v1/mail/threads/{quote(thread_id, safe='')}?chat_id={quote(caller.conversation_id)}"
    try:
        return finish(await caller.client.get(url, timeout=30.0))
    except Exception as exc:  # noqa: BLE001
        return error(f"{ctx.tool_name} failed: {exc}")
