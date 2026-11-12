"""Files a Helper saved with ``artifact_save``, carried in its result to the chat that started it.

The runner notes each successful save under the saving session; when a Helper finishes, the wake
notice lists its files (and its own Helpers' files, at any depth) with their artifact ids, so the
Muse shows the real file card instead of guessing by name.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from omnigent.superchat.feature import HandlerCtx, is_helper
from omnigent.superchat.subagents import FILES_OPEN

SAVE_TOOL_NAME = "artifact_save"

#: ``session id -> {file name -> saved record}``; a re-save of a name keeps only the newest.
_saved: dict[str, dict[str, dict[str, Any]]] = {}


def note_saved_file(ctx: HandlerCtx, output: str) -> None:
    """Feature result listener: note a Helper's ``artifact_save`` under its own session."""
    if ctx.tool_name == SAVE_TOOL_NAME and ctx.conversation_id and is_helper(ctx.labels):
        record_saved_file(ctx.conversation_id, output)


def record_saved_file(session_id: str, output: str) -> None:
    """Note one ``artifact_save`` result (its JSON output) under the session that saved it."""
    try:
        saved = json.loads(output)
    except ValueError:
        return
    if not isinstance(saved, dict) or saved.get("type") != "artifact":
        return
    artifact_id, name = saved.get("id"), saved.get("name")
    if not isinstance(artifact_id, str) or not isinstance(name, str):
        return
    record = {"id": artifact_id, "name": name, "title": saved.get("title")}
    version = saved.get("version")
    if isinstance(version, int):
        record["version"] = version
    _saved.setdefault(session_id, {})[name] = record


def _tree(session_id: str, children_of: Callable[[str], Iterable[str]]) -> list[str]:
    """``session_id`` and every session under it, breadth first."""
    seen: list[str] = []
    stack = [session_id]
    while stack:
        current = stack.pop(0)
        if current in seen:
            continue
        seen.append(current)
        stack.extend(children_of(current))
    return seen


def collect_saved_files(
    session_id: str, children_of: Callable[[str], Iterable[str]]
) -> list[dict[str, Any]]:
    """Files saved by ``session_id`` and every Helper under it, in the order they were saved."""
    found: dict[str, dict[str, Any]] = {}
    for current in _tree(session_id, children_of):
        for record in _saved.get(current, {}).values():
            found[record["id"]] = record
    return list(found.values())


def forget_saved_files(
    session_id: str, children_of: Callable[[str], Iterable[str]] = lambda _s: ()
) -> None:
    """Drop what ``session_id`` and its Helpers saved (their result has been delivered)."""
    for current in _tree(session_id, children_of):
        _saved.pop(current, None)


def format_saved_files(files: Iterable[Mapping[str, Any]]) -> str:
    """The wake-notice block naming a Helper's saved files ("" if none); data only."""
    lines = []
    for item in files:
        label = item.get("title") or item["name"]
        version = f", version {item['version']}" if item.get("version", 1) > 1 else ""
        lines.append(f'- "{label}" (file name {item["name"]}, artifact_id {item["id"]}{version})')
    if not lines:
        return ""
    return "\n".join([FILES_OPEN, *lines]) + "\n"
