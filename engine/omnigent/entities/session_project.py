"""The Project a session has open, read from its working directory and one label.

Plain data over a :class:`~omnigent.entities.Conversation`, shared by the session listing in
``context/rollover.py`` and the Super Chat lineage (``superchat/lineage.py``, which re-exports
it). A Project is a folder ``/workspace/projects/<slug>`` on the session's host.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation

#: Session label holding the open Project's card name, stamped when ``open_project`` moves the
#: working directory (the card itself lives on the session's host, not the server).
PROJECT_NAME_LABEL_KEY = "omnigent.project.name"

# A working directory inside (or under) a Project folder; group 1 is the slug.
_PROJECT_DIR_RE = re.compile(r"/workspace/projects/([a-z0-9][a-z0-9_-]{0,63})(?:/|$)")


def project_slug_from_workspace(workspace: str | None) -> str | None:
    """The Project a working directory is in (or under), or ``None`` at the workspace root."""
    match = _PROJECT_DIR_RE.search(workspace or "")
    return match.group(1) if match else None


def open_project(conversation: Conversation) -> dict[str, str] | None:
    """The Project the session has open (``{"slug", "name"}``), read from its working directory.

    The name is the card name ``open_project`` recorded, else the slug.
    """
    slug = project_slug_from_workspace(conversation.workspace)
    if slug is None:
        return None
    name = conversation.labels.get(PROJECT_NAME_LABEL_KEY) or slug
    return {"slug": slug, "name": name}
