"""References written by code: the files, artifacts, Project and Helpers a window touched.

A journal line is prose the summarizer writes; whether it names the file it is about depends on
the model that day. :func:`refs_from_items` reads the window's tool calls instead and returns the
objects they name exactly, in a fixed vocabulary:

* ``files``: workspace-relative paths written, read or saved (``projects/q3/budget.xlsx``);
* ``artifacts``: saved versions as ``<id>@v<version>``;
* ``project``: Project slugs (from ``open_project`` or a ``projects/<slug>/`` path);
* ``helpers``: Helper session ids (from ``start_helper`` and ``message_helper``).

:func:`format_refs` renders them as ``files:a,b artifacts:x@v2`` for a journal line's trailing
comment. A value never holds a space, a comma or ``<``/``>``: those (and ``%``) are
percent-encoded, so ``urllib.parse.unquote`` gives the value back. Pure: no I/O, no model call.
"""

from __future__ import annotations

import contextlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any

from omnigent.context.tool_digest import _short_name

#: The keys, in the order they are written.
REF_KEYS = ("files", "artifacts", "project", "helpers")
#: Most values one line carries, all keys together.
MAX_REFS = 12
#: Which keys keep their values first when a window names more than :data:`MAX_REFS`.
_PRIORITY = ("project", "artifacts", "helpers", "files")

_WRITE_TOOLS = frozenset({"sys_os_write", "sys_os_edit", "deck_new", "deck_theme_set"})
_READ_TOOLS = frozenset({"sys_os_read", "files_get", "files_read_page", "deck_check"})
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_IN_WORKSPACE = re.compile(r"(?:^|/)workspace/(.+)$")
_PROJECT_PATH = re.compile(r"^projects/([a-z0-9][a-z0-9_-]{0,63})(?:/|$)")
_UNSAFE = re.compile(r"[%,<>\s]")
#: What a stored value may still hold that a comment cannot carry (``%`` is already encoded).
_UNSAFE_IN_COMMENT = re.compile(r"[,<>\s]")


def _token(value: str, unsafe: re.Pattern[str] = _UNSAFE) -> str:
    """*value* with the *unsafe* characters percent-encoded."""
    return unsafe.sub(lambda m: "".join(f"%{b:02X}" for b in m.group(0).encode()), value)


def _parsed(raw: Any) -> dict[str, Any]:
    """A call's arguments or result as a dict: JSON text, a dict, or MCP text blocks."""
    if isinstance(raw, dict) and raw.get("type") in ("text", "input_text", "output_text"):
        raw = raw.get("text")
    if isinstance(raw, list):
        for block in raw:
            found = _parsed(block)
            if found:
                return found
        return {}
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            raw = json.loads(raw)
    return raw if isinstance(raw, dict) else {}


def _failed(output: Any) -> bool:
    """Whether a result says the call failed (``{"error": ...}`` or ``Error: ...``)."""
    if "error" in _parsed(output):
        return True
    text = output if isinstance(output, str) else ""
    return text.lstrip().lower().startswith("error")


def _workspace_path(raw: Any, project: str | None) -> str | None:
    """*raw* as a workspace-relative path, or ``None`` when it is not one.

    An absolute path counts only under a ``workspace/`` folder. A relative path is taken from the
    Project folder when ``open_project`` opened one earlier in the window, else from the root.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw.strip()
    if path.startswith(("/", "~")):
        match = _IN_WORKSPACE.search(path)
        if match is None:
            return None
        path = match.group(1)
    elif project:
        path = f"projects/{project}/{path}"
    path = posixpath.normpath(path)
    if path in (".", "") or path.startswith(".."):
        return None
    return path


def _add(bucket: list[str], value: Any) -> None:
    if isinstance(value, str) and value.strip() and value.strip() not in bucket:
        bucket.append(value.strip())


def refs_from_items(items: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """The references the tool calls of *items* name, at most :data:`MAX_REFS` values in all.

    Calls are read with their results (matched on ``call_id``); a call whose result says it
    failed names nothing. A tool name may carry an ``mcp__<server>__`` prefix.

    :param items: Conversation items as flat dicts, in order.
    :returns: ``{key: [value, ...]}`` for the keys of :data:`REF_KEYS` that have values, in that
        order; values deduplicated and percent-encoded (see the module docstring).
    """
    items = [item for item in items if isinstance(item, Mapping)]
    outputs = {
        str(item.get("call_id")): item.get("output")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id")
    }
    written: list[str] = []
    read: list[str] = []
    found: dict[str, list[str]] = {key: [] for key in REF_KEYS}
    cwd: str | None = None
    for item in items:
        if item.get("type") != "function_call":
            continue
        name = _short_name(item.get("name"))
        output = outputs.get(str(item.get("call_id") or ""))
        if output is not None and _failed(output):
            continue
        args, result = _parsed(item.get("arguments")), _parsed(output)
        if name == "open_project":
            slug = args.get("slug")
            cwd = slug if isinstance(slug, str) and _SLUG.match(slug) else None
            _add(found["project"], cwd)
        elif name in _WRITE_TOOLS:
            _add(written, _workspace_path(result.get("path") or args.get("path"), cwd))
        elif name in _READ_TOOLS:
            _add(read, _workspace_path(result.get("path") or args.get("path"), cwd))
        elif name == "files_multi_get":
            for got in result.get("files") or []:
                if isinstance(got, Mapping):
                    _add(read, _workspace_path(got.get("path"), None))
        elif name == "artifact_save":
            _add(written, _workspace_path(result.get("source_path") or args.get("path"), cwd))
        elif name in ("start_helper", "message_helper"):
            _add(found["helpers"], result.get("helper_id") or args.get("helper_id"))
            slug = args.get("project")
            if isinstance(slug, str) and _SLUG.match(slug):
                _add(found["project"], slug)
        # Any save that answers with an artifact version (artifact_save, deck_export).
        version = result.get("version")
        if result.get("type") == "artifact" and isinstance(version, int) and result.get("id"):
            _add(found["artifacts"], f"{result['id']}@v{version}")
    found["files"] = [*written, *(path for path in read if path not in written)]
    for path in found["files"]:
        match = _PROJECT_PATH.match(path)
        if match:
            _add(found["project"], match.group(1))
    room = MAX_REFS
    kept: dict[str, list[str]] = {}
    for key in _PRIORITY:
        values = [_token(v) for v in found[key]][: max(room, 0)]
        room -= len(values)
        if values:
            kept[key] = values
    return {key: kept[key] for key in REF_KEYS if key in kept}


def format_refs(refs: Mapping[str, Iterable[str]] | None) -> str:
    """``files:a,b artifacts:x@v2``: the keys of :data:`REF_KEYS` that have values, in order.

    Written for refs that came from :func:`refs_from_items` (or a stored payload of them); a
    value is encoded again only where it still holds a character a comment cannot carry.
    """
    if not isinstance(refs, Mapping):
        return ""
    parts = []
    for key in REF_KEYS:
        raw = refs.get(key)
        values = (
            [_token(str(v), _UNSAFE_IN_COMMENT) for v in raw if str(v).strip()]
            if isinstance(raw, list)
            else []
        )
        if values:
            parts.append(f"{key}:{','.join(values)}")
    return " ".join(parts)
