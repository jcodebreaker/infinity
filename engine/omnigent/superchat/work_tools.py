"""Which tool calls count as the Assistant doing work (the Activity feed reads this)."""

from __future__ import annotations

# Artifact tools that only read never count.
_FREE_ARTIFACT_TOOLS = frozenset({"artifact_list", "artifact_outline"})
# Deck tools that build or write count (ADR 0027); the check and the theme list are reads.
_DECK_WORK_TOOLS = frozenset({"deck_new", "deck_theme_set", "deck_export"})


def is_work_tool(name: str) -> bool:
    """Whether a tool call counts as work (``mcp__<server>__`` prefixes are ignored).

    Search, browser, shell and files, artifact and deck writes and host actions are work;
    memory, cards, Goals, scheduling, vault and Helper management are not.
    """
    base = name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name
    if base in _FREE_ARTIFACT_TOOLS:
        return False
    return (
        base == "web_search"
        or base in _DECK_WORK_TOOLS
        or base.startswith(("browser_", "sys_os_", "artifact_"))
        or "computer" in name
    )
