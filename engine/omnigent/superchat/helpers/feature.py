"""Registration of the Helpers primitive: ``start_helper`` and ``message_helper`` for the chat that
starts Helpers, ``report_progress`` and ``raise_flag`` for the Helpers themselves."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.context.labels import is_superside_chat
from omnigent.superchat.feature import Feature, ToolManagerCtx, is_helper
from omnigent.superchat.helpers.handlers import (
    handle_message_helper,
    handle_start_helper,
    helper_type_for,
)
from omnigent.superchat.helpers.notices import (
    RAISE_FLAG_TOOL_NAME,
    REPORT_PROGRESS_TOOL_NAME,
    handle_raise_flag,
    handle_report_progress,
)
from omnigent.superchat.helpers.saved_files import note_saved_file
from omnigent.superchat.helpers.tools import (
    MESSAGE_HELPER_TOOL_NAME,
    START_HELPER_TOOL_NAME,
    MessageHelperTool,
    RaiseFlagTool,
    ReportProgressTool,
    StartHelperTool,
)

if TYPE_CHECKING:
    from omnigent.tools.base import Tool


def _tools(labels: Mapping[str, str] | None, ctx: ToolManagerCtx) -> list[Tool]:
    """``start_helper`` / ``message_helper`` where a Helper type is declared (the Muse, or a
    coordinating worker); ``report_progress`` / ``raise_flag`` for a Helper only."""
    if not is_superside_chat(labels):
        return []
    tools: list[Tool] = []
    if helper_type_for(ctx.spec) is not None:
        tools += [StartHelperTool(), MessageHelperTool()]
    if is_helper(labels):
        tools += [ReportProgressTool(), RaiseFlagTool()]
    return tools


HELPERS_FEATURE = Feature(
    name="helpers",
    tools=_tools,
    handlers={
        START_HELPER_TOOL_NAME: handle_start_helper,
        MESSAGE_HELPER_TOOL_NAME: handle_message_helper,
        REPORT_PROGRESS_TOOL_NAME: handle_report_progress,
        RAISE_FLAG_TOOL_NAME: handle_raise_flag,
    },
    on_result=note_saved_file,
)
