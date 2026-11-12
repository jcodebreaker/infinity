"""Context brief (ADR 0027): what already exists on the subject of the person's message.

Layout: ``brief`` (the parts, the caps, the render; :meth:`Brief.build`), ``sources`` (the
runner's reads), ``runtime`` (the tool's handler: built when called, cached per turn),
``tools`` (the ``context_brief`` def). The Conversation only: a Helper or a file's studio never
gets the tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.context_brief.runtime import (
    forget_brief,
    handle_context_brief,
)
from omnigent.superchat.context_brief.tools import CONTEXT_BRIEF_TOOL_NAME
from omnigent.superchat.feature import Feature, ToolManagerCtx, is_super_chat

if TYPE_CHECKING:
    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """``context_brief`` for the Conversation itself (never a Helper or a studio)."""
    from omnigent.superchat.context_brief.tools import ContextBriefTool

    return [ContextBriefTool()] if is_super_chat(labels) else []


FEATURE = Feature(
    name="context_brief",
    tools=_tools,
    handlers={CONTEXT_BRIEF_TOOL_NAME: handle_context_brief},
    on_turn_end=forget_brief,
)
