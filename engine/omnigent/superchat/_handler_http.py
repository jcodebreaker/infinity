"""Shared runner-handler plumbing: caller lookup and "POST to the server, map errors to JSON"."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from omnigent.superchat.feature import HandlerCtx

if TYPE_CHECKING:
    from collections.abc import Mapping


def error(message: str) -> str:
    """A tool-error JSON string."""
    return json.dumps({"error": message})


def finish(resp: httpx.Response) -> str:
    """Map a server response to tool output: JSON body, or a short error with details."""
    if resp.status_code >= 400:
        return json.dumps(
            {"error": f"server returned {resp.status_code}", "details": resp.text[:500]}
        )
    return json.dumps(resp.json())


async def session_kind_and_parent(
    session_id: str,
    server_client: httpx.AsyncClient,
) -> tuple[str, str | None] | None:
    """
    ``(kind, parent_session_id)`` lookup for one session, or ``None`` on any lookup failure.

    Callers must fail CLOSED on ``None``: a caller whose ``kind`` can't be verified must never
    be treated as "not a sub-agent" because the lookup came back empty.

    :param session_id: The session to inspect.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``(kind, parent_session_id)``, or ``None`` on a network error, non-200,
        unparseable body, or a response missing a string ``kind``.
    """
    try:
        resp = await server_client.get(f"/v1/sessions/{session_id}", timeout=10.0)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    data: Mapping[str, Any] | None = resp.json()
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    if not isinstance(kind, str):
        return None
    parent = data.get("parent_session_id")
    return kind, parent if isinstance(parent, str) else None


@dataclass(frozen=True)
class Caller:
    """The verified calling session.

    :param client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The calling session.
    :param is_helper: True when the caller is a sub-agent (Helper) session.
    :param parent_id: The Helper's parent chat, if any.
    """

    client: httpx.AsyncClient
    conversation_id: str
    is_helper: bool
    parent_id: str | None

    @property
    def chat_id(self) -> str | None:
        """The top-level chat a record attaches to: the parent for a Helper, else the caller."""
        return self.parent_id if self.is_helper else self.conversation_id


async def resolve_caller(ctx: HandlerCtx) -> Caller | str:
    """
    Check server access and verify the calling session.

    :returns: A :class:`Caller`, or a ready-to-return error JSON string.
    """
    if ctx.server_client is None or not ctx.conversation_id:
        return error(f"{ctx.tool_name} requires server access")
    lookup = await session_kind_and_parent(ctx.conversation_id, ctx.server_client)
    if lookup is None:
        return error(f"{ctx.tool_name}: cannot verify the calling session")
    return Caller(ctx.server_client, ctx.conversation_id, lookup[0] == "sub_agent", lookup[1])


#: A 32-character hex record id (objective, scheduled task).
HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _refusal_detail(resp: httpx.Response) -> str:
    """The server's refusal message (``{"error": {"message": ...}}``), else the raw body."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        err = None
    detail = err.get("message") if isinstance(err, dict) else err
    return str(detail or resp.text)[:300]


async def set_session_workspace(
    client: httpx.AsyncClient, session_id: str, path: str, project_name: str | None = None
) -> str | None:
    """Ask the server to move a session's working directory; an error message on failure."""
    body: dict[str, Any] = {"workspace": path}
    if project_name:
        body["project_name"] = project_name
    try:
        resp = await client.put(
            f"/v1/sessions/{session_id}/workspace",
            json=body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        return f"could not change the working directory: {exc}"
    if resp.status_code >= 400:
        return f"could not change the working directory: {_refusal_detail(resp)}"
    return None
