"""Runner-side handlers for the vault tools.

``vault_request_secret`` opens a secure-entry request (the value is entered on a secure form,
never here). ``vault_fill`` fetches the value with the runner's session auth and types it into
the host's browser; the value stays in local variables, is never logged, and the result only
says it was filled.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from omnigent.superchat._handler_http import error, resolve_caller
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.vault.tools import VAULT_HELPER_TOOL_NAMES


def _vault_error(resp: httpx.Response) -> str:
    """A short, value-free reason from a vault API error response."""
    try:
        detail = resp.json()
        message = detail.get("error", {}).get("message") or detail.get("detail")
    except ValueError:
        message = None
    return str(message or f"server returned {resp.status_code}")[:200]


async def handle_vault_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Run one vault tool.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: Tool output JSON string, never containing a secret value.
    """
    from omnigent.tools.browser_backend import (
        get_local_browser_backend,
        local_browser_backend_enabled,
    )

    tool_name = ctx.tool_name
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    if caller.is_helper and tool_name not in VAULT_HELPER_TOOL_NAMES:
        return error(f"{tool_name} is not available to sub-agent sessions")
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return error(f"{tool_name} requires a name")
    client = caller.client
    try:
        if tool_name == "vault_request_secret":
            resp = await client.post(
                "/v1/me/vault/requests",
                json={
                    "session_id": caller.conversation_id,
                    "name": name,
                    "site": str(args.get("site") or ""),
                    "reason": str(args.get("reason") or ""),
                },
                timeout=30.0,
            )
            if resp.status_code >= 400:
                return error(_vault_error(resp))
            req = resp.json()
            return json.dumps(
                {
                    "secure_entry": {k: req[k] for k in ("id", "name", "site", "reason")},
                    "message": "A secure card is shown. Wait for the person to save it, "
                    "then use vault_fill.",
                }
            )
        field = args.get("field")
        ref = args.get("ref")
        if field not in ("username", "password") or not isinstance(ref, int):
            return error("vault_fill needs field (username|password) and ref")
        if not local_browser_backend_enabled():
            return error("vault_fill needs the host's browser")

        async def fetch() -> tuple[str, str]:
            resp = await client.post(
                "/v1/me/vault/fill",
                json={"name": name, "field": field, "session_id": caller.conversation_id},
                timeout=30.0,
            )
            if resp.status_code >= 400:
                raise RuntimeError(_vault_error(resp))
            body = resp.json()
            return body["value"], body["site"]

        return await get_local_browser_backend().fill_login(ref=ref, field=field, fetch=fetch)
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")
