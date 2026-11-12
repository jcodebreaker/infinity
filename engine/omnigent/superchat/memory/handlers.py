"""Runner-side handler for the memory_* tools (the memory core stays in omnigent.memory)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from omnigent.superchat._handler_http import error
from omnigent.superchat.feature import HandlerCtx


def _memory_error_body(resp: httpx.Response) -> str:
    """Extract a readable message from a non-2xx memory-route response."""
    try:
        body = resp.json()
        # A bare FastAPI "Not Found" means the route itself is unmounted: the
        # server runs without the memory extra (no txtai), so the router is
        # never included. Say that instead of an opaque "HTTP 404".
        if (
            resp.status_code == 404
            and isinstance(body, dict)
            and body.get("detail") == "Not Found"
        ):
            return "long-term memory is not configured on this server"
    except Exception:  # noqa: BLE001
        return resp.text or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        message = body.get("error") or body.get("detail") or body.get("message")
        if isinstance(message, str) and message:
            return message
    return f"HTTP {resp.status_code}"


async def handle_memory_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Runner-local handler for the ``memory_*`` family (native-relay dispatch).

    The runner has no in-process :class:`~omnigent.memory.service.MemoryService`
    (same constraint as ``_execute_session_history_tool``), so every action
    dispatches to the Omnigent server's ``/v1/sessions/{id}/memory/*``
    endpoints over ``server_client``. ``conversation_id`` is the runner's own
    dispatch context, never read from *args*; the server resolves the acting
    user from that session's owner, never from the request body.

    :param ctx: The call context (tool is one of ``memory_remember``/``memory_search``/
        ``memory_get``/``memory_explain``/``memory_forget``).
    :param args: Parsed tool arguments.
    :returns: JSON output matching the in-process tools' shape.
    """
    tool_name = ctx.tool_name
    server_client = ctx.server_client
    conversation_id = ctx.conversation_id
    if server_client is None:
        return error(f"{tool_name} requires server access")
    if conversation_id is None:
        return error(f"{tool_name} requires a session id")
    base = f"/v1/sessions/{conversation_id}/memory"

    try:
        if tool_name == "memory_remember":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip():
                return json.dumps({"error": "text must be a non-empty string"})
            resp = await server_client.post(
                f"{base}/remember",
                json={
                    "text": text.strip(),
                    "kind": args.get("kind"),
                    "quote": args.get("quote"),
                    "replaces_claim_id": args.get("replaces_claim_id"),
                    "explicitness": "inferred" if args.get("explicitness") == "inferred" else None,
                },
                timeout=30.0,
            )
        elif tool_name == "memory_search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return json.dumps({"error": "query must be a non-empty string"})
            params: dict[str, Any] = {"query": query.strip()}
            if args.get("kind") is not None:
                params["kind"] = args["kind"]
            if args.get("limit") is not None:
                params["limit"] = args["limit"]
            resp = await server_client.get(f"{base}/search", params=params, timeout=30.0)
        elif tool_name == "memory_get":
            claim_id = args.get("claim_id")
            if not isinstance(claim_id, str) or not claim_id:
                return json.dumps({"error": "claim_id must be a non-empty string"})
            resp = await server_client.get(f"{base}/claims/{claim_id}", timeout=30.0)
        elif tool_name == "memory_explain":
            claim_id = args.get("claim_id")
            if not isinstance(claim_id, str) or not claim_id:
                return json.dumps({"error": "claim_id must be a non-empty string"})
            resp = await server_client.get(f"{base}/claims/{claim_id}/explain", timeout=30.0)
        else:  # memory_forget
            if args.get("claim_id") is None and not args.get("query"):
                return json.dumps({"error": "forget requires claim_id or query"})
            resp = await server_client.post(
                f"{base}/forget",
                json={
                    "claim_id": args.get("claim_id"),
                    "query": args.get("query"),
                    "confirm": bool(args.get("confirm", False)),
                },
                timeout=30.0,
            )
    except Exception as exc:  # noqa: BLE001 — a failed call is reported to the model
        return json.dumps({"error": f"{tool_name} failed: {exc}"})

    if resp.status_code >= 400:
        return json.dumps({"error": f"{tool_name}: {_memory_error_body(resp)}"})
    return json.dumps(resp.json())
