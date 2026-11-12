"""The Conversation's pi session file: written from the rendered window, read back by pi."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omnigent.context.window import WindowMessage
from omnigent.harnesses.pi_executor import (
    _pi_message,
    _pin_session_cwd,
    read_pi_session,
    write_pi_session,
)

_WINDOW: list[WindowMessage] = [
    {"role": "user", "content": [{"type": "input_text", "text": "[What came before ...]"}]},
    {"role": "user", "content": [{"type": "input_text", "text": "[Local time: 9:00]\n\nls"}]},
    {
        "role": "assistant",
        "text": "looking",
        "tool_calls": [
            {"id": "c1", "name": "shell", "arguments": {"cmd": "ls"}},
            {"id": "c2", "name": "shell", "arguments": {"cmd": "pwd"}},
        ],
    },
    {"role": "tool", "call_id": "c1", "name": "shell", "output": "a.txt"},
    {"role": "tool", "call_id": "c2", "name": "shell", "output": "/w"},
    {"role": "user", "content": [{"type": "input_text", "text": "and the logs"}]},
    {"role": "assistant", "text": "one file in /w", "tool_calls": []},
]


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_file_is_a_linear_v3_session_with_paired_calls(tmp_path: Path) -> None:
    path = tmp_path / "conv" / "epoch.jsonl"
    write_pi_session(path, cwd="/w", messages=_WINDOW)

    header, *entries = _entries(path)
    assert header["type"] == "session"
    assert header["version"] == 3
    assert header["cwd"] == "/w"
    assert os.stat(path).st_mode & 0o777 == 0o600
    ids = [e["id"] for e in entries]
    assert len(set(ids)) == len(ids)
    assert [e["parentId"] for e in entries] == [None, *ids[:-1]]
    messages = [e["message"] for e in entries]
    calls = [b["id"] for m in messages if m["role"] == "assistant" for b in m["content"][1:]]
    results = [m["toolCallId"] for m in messages if m["role"] == "toolResult"]
    assert calls == results == ["c1", "c2"]
    assert messages[2]["stopReason"] == "toolUse"
    assert messages[-1]["stopReason"] == "stop"
    assert messages[-1]["usage"]["totalTokens"] == 0

    _pin_session_cwd(path, "/spawn")
    assert _entries(path)[0]["cwd"] == "/spawn"
    assert _entries(path)[1:] == entries
    assert read_pi_session(path) == (
        True,
        [
            ("user", "[What came before ...]"),
            ("user", "[Local time: 9:00]\n\nls"),
            ("assistant", "looking"),
            ("user", "and the logs"),
            ("assistant", "one file in /w"),
        ],
    )


@pytest.mark.skipif(shutil.which("pi") is None, reason="needs the pi CLI")
def test_pi_loads_the_rendered_window(tmp_path: Path) -> None:
    path = tmp_path / "conv" / "epoch.jsonl"
    write_pi_session(path, cwd=str(tmp_path), messages=_WINDOW)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PI_CODING_AGENT_DIR": str(agent_dir),
    }
    proc = subprocess.Popen(
        [
            str(shutil.which("pi")),
            "--mode",
            "rpc",
            "--session",
            str(path),
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
        env=env,
        text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"id": "m", "type": "get_messages"}) + "\n")
        proc.stdin.flush()
        answer: dict[str, Any] = {}
        for line in proc.stdout:
            event = json.loads(line)
            if event.get("id") == "m":
                answer = event
                break
    finally:
        proc.kill()
        proc.wait(timeout=10)

    assert answer.get("success") is True
    loaded = answer["data"]["messages"]
    expected = [_pi_message(m, 0) for m in _WINDOW]
    assert [{**m, "timestamp": 0} for m in loaded] == expected
