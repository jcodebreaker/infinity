"""Memory capability: the ``memory_*`` tools and the Memory Profile delivery.

Only the tool defs, the runner handler and the profile block live here; the memory core stays in
``omnigent.memory`` (service, store, upkeep) and its server routes in
``omnigent.server.routes.session_memory``.
Layout: ``tools`` (tool defs), ``handlers`` (runner side), ``profile`` (``memory_profile_for``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.context.labels import uses_omnigent_context
from omnigent.superchat.feature import Feature, ToolManagerCtx
from omnigent.superchat.memory.handlers import handle_memory_tool
from omnigent.superchat.memory.profile import memory_profile_for

if TYPE_CHECKING:
    from omnigent.tools.base import Tool

__all__ = ["FEATURE", "MEMORY_TOOL_NAMES", "memory_profile_for"]

MEMORY_TOOL_NAMES = (
    "memory_remember",
    "memory_search",
    "memory_get",
    "memory_explain",
    "memory_forget",
)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Every rollover or superside-chat session gets long-term memory (same gate as history)."""
    from omnigent.superchat.memory.tools import (
        MemoryExplainTool,
        MemoryForgetTool,
        MemoryGetTool,
        MemoryRememberTool,
        MemorySearchTool,
    )

    if not uses_omnigent_context(labels):
        return []
    return [
        MemoryRememberTool(),
        MemorySearchTool(),
        MemoryGetTool(),
        MemoryExplainTool(),
        MemoryForgetTool(),
    ]


FEATURE = Feature(
    name="memory",
    tools=_tools,
    handlers=dict.fromkeys(MEMORY_TOOL_NAMES, handle_memory_tool),
)
