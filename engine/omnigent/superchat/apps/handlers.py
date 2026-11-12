"""Runner-side handler for the ``artifact_publish`` tool (proxies the publish REST endpoint)."""

from __future__ import annotations

from typing import Any

from omnigent.superchat._handler_http import HEX_ID_RE, error, finish, resolve_caller
from omnigent.superchat.feature import HandlerCtx


async def handle_publish_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Publish a saved HTML artifact at its own address.

    :param ctx: The call context.
    :param args: Parsed tool arguments (``artifact_id``, ``audience``).
    :returns: Tool output JSON string.
    """
    tool_name = ctx.tool_name
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    if not caller.chat_id:
        return error(f"{tool_name}: no chat to attach to")
    try:
        artifact_id = args.get("artifact_id")
        audience = args.get("audience")
        if not isinstance(artifact_id, str) or not HEX_ID_RE.match(artifact_id):
            return error("artifact_publish requires a valid artifact_id")
        if audience not in ("owner", "org", "link"):
            return error("artifact_publish audience must be owner, org or link")
        resp = await caller.client.post(
            f"/v1/artifacts/{artifact_id}/publish", json={"audience": audience}, timeout=30.0
        )
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")
    return finish(resp)
