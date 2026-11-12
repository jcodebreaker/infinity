"""Runner side of ``open_project``: move the session's working directory into a Project."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omnigent.superchat._handler_http import error, set_session_workspace
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.projects.card import (
    CARD_FILE,
    is_slug,
    projects_root,
    read_card,
    workspace_root,
)
from omnigent.superchat.projects.choice import person_chose_workspace


def resolve_project(slug: str) -> tuple[Path, str] | None:
    """The folder and card name of Project ``slug``, or ``None`` when it has no card."""
    if not is_slug(slug):
        return None
    folder = projects_root() / slug
    if not (folder / CARD_FILE).is_file():
        return None
    card = read_card(slug)
    return folder, card.name if card else slug


async def handle_open_project(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Open a Project (``slug``) or return to the workspace root (``slug`` null).

    :param ctx: The call context.
    :param args: ``{"slug": str | None}``.
    :returns: A short receipt JSON, or an error JSON the model can act on.
    """
    if ctx.server_client is None or not ctx.conversation_id:
        return error("open_project requires server access")
    if await person_chose_workspace(ctx.server_client, ctx.conversation_id):
        return json.dumps(
            {
                "opened": None,
                "refused": "The person chose this chat's folder, so it stays where it is. "
                "Tell them which Project fits and let them move the chat themselves.",
            }
        )
    slug = args.get("slug")
    if slug is None:
        failure = await set_session_workspace(
            ctx.server_client, ctx.conversation_id, str(workspace_root())
        )
        if failure:
            return error(failure)
        return json.dumps({"opened": None, "path": str(workspace_root())})
    if not is_slug(slug):
        return error("open_project: 'slug' must be a folder name from the Projects list, or null")
    project = resolve_project(slug)
    if project is None:
        return error(f"open_project: no Project '{slug}' (it has no {CARD_FILE})")
    folder, name = project
    failure = await set_session_workspace(
        ctx.server_client, ctx.conversation_id, str(folder), name
    )
    if failure:
        return error(failure)
    return json.dumps({"opened": slug, "name": name, "path": str(folder)})
