"""Runner-side handler for ``reach_rules``: proxies ``/v1/me/reach-rules``.

Only the Conversation itself is offered the tool; a Helper that reaches it anyway is refused
here, and the server checks the calling session again.
"""

from __future__ import annotations

from typing import Any

from omnigent.superchat._handler_http import error, finish, resolve_caller
from omnigent.superchat.feature import HandlerCtx

_FIELDS = ("kind", "params", "said", "expires_at", "id")


async def handle_reach_rules_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Run ``reach_rules``.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: Tool output JSON string.
    """
    tool_name = ctx.tool_name
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    if caller.is_helper:
        return error(f"{tool_name}: only the person's own Conversation keeps reach rules")
    action = args.get("action")
    try:
        if action == "list":
            resp = await caller.client.get("/v1/me/reach-rules", timeout=30.0)
        elif action in ("add", "remove"):
            payload = {
                "session_id": caller.conversation_id,
                "action": action,
                **{k: args[k] for k in _FIELDS if args.get(k) is not None},
            }
            resp = await caller.client.post("/v1/me/reach-rules", json=payload, timeout=30.0)
        else:
            return error(f"{tool_name}: action is add, remove or list")
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")
    return finish(resp)
