"""State file (``state_close``): the person's exit for a wrong entry of the checkpoint.

Layout: ``tools`` (tool def), ``handlers`` (runner side, records a pending close on the session).
The tag format, repair and budget live in ``omnigent/context/state_file.py``; the next checkpoint
(``omnigent/superchat/rollover.py``) applies the pending closes. Super Chat only, never a Helper.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.superchat.feature import Feature, ToolManagerCtx, is_super_chat
from omnigent.superchat.state_file.handlers import handle_state_close
from omnigent.superchat.state_file.tools import STATE_CLOSE_TOOL_NAME

if TYPE_CHECKING:
    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """``state_close`` for the Super Chat itself (never a Helper)."""
    from omnigent.superchat.state_file.tools import StateCloseTool

    return [StateCloseTool()] if is_super_chat(labels) else []


FEATURE = Feature(
    name="state_file", tools=_tools, handlers={STATE_CLOSE_TOOL_NAME: handle_state_close}
)
