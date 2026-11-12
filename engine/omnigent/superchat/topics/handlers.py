"""Runner-side handler for ``topics_update``: proxies ``POST /v1/topics/write``.

A Helper writes under its parent chat; the server checks the caller owns it. The calling
session goes with the write, so the server holds the Conversation itself to the topic the person
picked up (ADR 0035, amended 2026-10-23).
"""

from __future__ import annotations

from typing import Any

from omnigent.superchat._handler_http import error, finish, resolve_caller
from omnigent.superchat.feature import HandlerCtx


async def handle_topics_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Run ``topics_update``.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: Tool output JSON string.
    """
    tool_name = ctx.tool_name
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    chat_id = caller.chat_id
    if not chat_id:
        return error(f"{tool_name}: no chat to attach to")
    topics = args.get("topics")
    if not isinstance(topics, list) or not topics:
        return error(f"{tool_name}: pass `topics`, a list of topics")
    payload = {
        "parent_session_id": chat_id,
        "topics": topics,
        "session_id": caller.conversation_id,
    }
    try:
        resp = await caller.client.post("/v1/topics/write", json=payload, timeout=30.0)
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")
    return finish(resp)
