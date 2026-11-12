"""Runner side of ``state_close``: record a pending close on the session."""

from __future__ import annotations

import json
from typing import Any

import httpx

from omnigent.context.state_file import (
    CLOSE_REASON_MAX_CHARS,
    close_label_key,
    entries,
    normalize_tag,
    parse,
)
from omnigent.superchat._handler_http import error
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.rollover import ItemsPageFetchError, fetch_latest_checkpoint


async def handle_state_close(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """Record a close for the next checkpoint; it does not rewrite the checkpoint.

    The close is one session label per tag (``omnigent.state_close.<tag>`` = reason): an upsert,
    so two closes before one checkpoint both stay. The checkpoint applies and clears them.

    :returns: A JSON status, or a tool-error JSON for an unknown tag.
    """
    if ctx.server_client is None or ctx.conversation_id is None:
        return error("state_close is not available here")
    tag = normalize_tag(str(args.get("tag") or ""))
    reason = " ".join(str(args.get("reason") or "").split())[:CLOSE_REASON_MAX_CHARS]
    if tag is None or not reason:
        return error("pass the entry's tag (like t-launch-budget) and a short reason")
    try:
        checkpoint = await fetch_latest_checkpoint(
            ctx.server_client, ctx.conversation_id, skip_placeholders=True
        )
    except (httpx.HTTPError, ItemsPageFetchError):
        return error("could not read the state file; try again")
    entry = entries(parse((checkpoint or {}).get("summary") or "")).get(tag)
    if entry is None:
        return error(f"no entry with the tag {tag} in the state file")
    if entry.done:
        return json.dumps({"tag": tag, "status": "already closed"})
    try:
        resp = await ctx.server_client.patch(
            f"/v1/sessions/{ctx.conversation_id}",
            json={"labels": {close_label_key(tag): reason}},
            timeout=10.0,
        )
    except httpx.HTTPError:
        return error("could not record the close; try again")
    if resp.status_code >= 400:
        return error("could not record the close; try again")
    return json.dumps({"tag": tag, "status": "will be closed at the next checkpoint"})
