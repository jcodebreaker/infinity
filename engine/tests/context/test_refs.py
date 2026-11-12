"""Tests for references written by code: what a window's tool calls touched."""

from __future__ import annotations

import json
from typing import Any

from omnigent.context.refs import MAX_REFS, format_refs, refs_from_items

_calls = 0


def _call(name: str, args: dict[str, Any], output: Any = None) -> list[dict[str, Any]]:
    """A ``function_call`` and, unless *output* is ``None``, its ``function_call_output``."""
    global _calls
    _calls += 1
    call_id = f"call_{_calls}"
    items = [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}
    ]
    if output is not None:
        text = output if isinstance(output, str) else json.dumps(output)
        items.append({"type": "function_call_output", "call_id": call_id, "output": text})
    return items


def test_file_writes_and_reads_are_workspace_paths() -> None:
    items = [
        *_call("sys_os_write", {"path": "notes/plan.md", "content": "x"}, "ok"),
        *_call("sys_os_read", {"path": "/home/me/workspace/data/q3.csv"}, "a,b"),
        *_call("sys_os_edit", {"path": "/tmp/scratch.txt"}, "ok"),
        *_call("files_get", {"path": "q3.csv"}, {"path": "your_files/uploads/q3.csv"}),
        *_call("files_multi_get", {"glob": "*.md"}, {"files": [{"path": "a.md"}]}),
        *_call("deck_new", {"path": "./board.deck.html"}, {"ok": True}),
    ]

    assert refs_from_items(items) == {
        "files": [
            "notes/plan.md",
            "board.deck.html",
            "data/q3.csv",
            "your_files/uploads/q3.csv",
            "a.md",
        ]
    }


def test_a_failed_call_names_nothing() -> None:
    items = [
        *_call("sys_os_write", {"path": "a.md"}, {"error": "denied"}),
        *_call("start_helper", {"task": "t"}, "Error: start_helper: busy"),
    ]

    assert refs_from_items(items) == {}


def test_artifact_saves_give_id_at_version_and_the_saved_file() -> None:
    saved = {
        "type": "artifact",
        "id": "a1",
        "version": 3,
        "source_path": "/home/me/workspace/report.html",
    }
    items = [
        *_call("artifact_save", {"path": "report.html"}, saved),
        *_call("deck_export", {"artifact_id": "d0", "format": "pdf"}, {**saved, "id": "p9"}),
    ]

    assert refs_from_items(items) == {
        "files": ["report.html"],
        "artifacts": ["a1@v3", "p9@v3"],
    }


def test_open_project_names_the_project_and_roots_later_relative_paths() -> None:
    items = [
        *_call("sys_os_write", {"path": "before.md"}, "ok"),
        *_call("open_project", {"slug": "spring-launch"}, {"opened": "spring-launch"}),
        *_call("sys_os_write", {"path": "budget.md"}, "ok"),
        *_call("open_project", {"slug": None}, {"opened": None}),
        *_call("sys_os_write", {"path": "after.md"}, "ok"),
        *_call("sys_os_read", {"path": "projects/q3-board/deck.md"}, "x"),
    ]

    assert refs_from_items(items) == {
        "files": [
            "before.md",
            "projects/spring-launch/budget.md",
            "after.md",
            "projects/q3-board/deck.md",
        ],
        "project": ["spring-launch", "q3-board"],
    }


def test_helpers() -> None:
    items = [
        *_call("start_helper", {"task": "count"}, {"started": True, "helper_id": "h1"}),
        *_call("message_helper", {"message": "also", "helper_id": "h1"}, {"ok": True}),
    ]

    assert refs_from_items(items) == {"helpers": ["h1"]}


def test_an_mcp_prefix_and_text_block_results_are_read() -> None:
    blocks = [{"type": "text", "text": json.dumps({"started": True, "helper_id": "h2"})}]
    items = _call("mcp__omnigent__start_helper", {"task": "t"}, None)
    items.append(
        {"type": "function_call_output", "call_id": items[0]["call_id"], "output": blocks}
    )

    assert refs_from_items(items) == {"helpers": ["h2"]}


def test_at_most_twelve_values_project_and_artifacts_first() -> None:
    items = [*_call("open_project", {"slug": "p1"}, {"opened": "p1"})]
    for n in range(20):
        items += _call("sys_os_write", {"path": f"/w/workspace/f{n}.md"}, "ok")
    items += _call(
        "artifact_save", {"path": "x.html"}, {"type": "artifact", "id": "a", "version": 1}
    )

    refs = refs_from_items(items)

    assert sum(len(v) for v in refs.values()) == MAX_REFS
    assert refs["project"] == ["p1"]
    assert refs["artifacts"] == ["a@v1"]
    assert list(refs) == ["files", "artifacts", "project"]


def test_values_never_hold_a_space_or_a_comma() -> None:
    items = _call("sys_os_write", {"path": "My Notes, v2.md"}, "ok")

    refs = refs_from_items(items)

    assert refs == {"files": ["My%20Notes%2C%20v2.md"]}
    assert format_refs(refs) == "files:My%20Notes%2C%20v2.md"


def test_format_keeps_the_key_order_and_skips_empty_keys() -> None:
    refs = {"helpers": ["h1"], "files": ["a.md", "b.md"], "project": []}

    assert format_refs(refs) == "files:a.md,b.md helpers:h1"
    assert format_refs(None) == ""
