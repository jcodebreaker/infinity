"""Registration of the Projects primitive (``open_project`` and the person-move note)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, ToolManagerCtx, is_super_chat
from omnigent.superchat.projects.choice import deliver_person_move
from omnigent.superchat.projects.handlers import handle_open_project
from omnigent.superchat.projects.tools import OPEN_PROJECT_TOOL_NAME, OpenProjectTool

if TYPE_CHECKING:
    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """``open_project`` for the Super Chat itself (never a Helper, which inherits its folder)."""
    return [OpenProjectTool()] if is_super_chat(labels) else []


PROJECTS_FEATURE = Feature(
    name="projects",
    tools=_tools,
    handlers={OPEN_PROJECT_TOOL_NAME: handle_open_project},
    turn_prefix=deliver_person_move,
)
