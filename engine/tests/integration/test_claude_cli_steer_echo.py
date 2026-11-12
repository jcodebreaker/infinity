"""The real bundled Claude CLI echoes a mid-turn steer with our uuid, in both delivery paths.

``ClaudeSDKExecutor`` counts a steer as taken only when the CLI echoes its uuid
(``--replay-user-messages``). This drives the actual CLI binary shipped with
``claude_agent_sdk`` in stream-json mode against a local fake Messages API, so no network
or API key is used:

* a steer written while a tool runs is attached at the tool boundary: echoed before the
  turn's only result, and the next model request carries it;
* a steer written while a text-only reply streams misses the turn: the CLI answers it as
  its own next turn, echoing it after the first result and before the second.

Opt-in like the rest of ``tests/integration`` (it starts the ~200 MB CLI):
``pytest tests/integration/test_claude_cli_steer_echo.py``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")
CLI = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
pytestmark = pytest.mark.skipif(not CLI.exists(), reason="no bundled Claude CLI")


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if block.get("type") == "text":
            parts.append(block["text"])
        elif block.get("type") == "tool_result":
            parts.append("<tool_result>" + json.dumps(block.get("content")))
    return " ".join(parts)


class _FakeMessagesApi(BaseHTTPRequestHandler):
    """``TOOL`` prompts get a Bash ``sleep 3`` first; ``SLOW`` prompts a slowly streamed text."""

    protocol_version = "HTTP/1.1"
    requests: list[dict] = []

    def log_message(self, *args: object) -> None:
        return

    def _json(self, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._json({})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if "count_tokens" in self.path:
            self._json({"input_tokens": 10})
            return
        messages = body.get("messages", [])
        main = bool(body.get("tools"))
        if main:
            type(self).requests.append(body)
        first = _text_of(messages[0]["content"]) if messages else ""
        seen_tool_result = any("<tool_result>" in _text_of(m["content"]) for m in messages)
        start = {
            "type": "message_start",
            "message": {
                "id": f"msg_{time.time_ns()}",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-sonnet-4-5"),
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        }
        if main and "TOOL" in first and not seen_tool_result:
            block = {
                "type": "tool_use",
                "id": f"toolu_{time.time_ns()}",
                "name": "Bash",
                "input": {},
            }
            args = json.dumps({"command": "sleep 3", "description": "wait"})
            events = [
                start,
                {"type": "content_block_start", "index": 0, "content_block": block},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ]
            self._sse(events, delay=0)
            return
        user_turns = sum(1 for m in messages if m["role"] == "user")
        slow = main and "SLOW" in first and user_turns == 1
        events = [
            start,
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            *(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": w},
                }
                for w in ("an ", "answer ", "streamed ", "slowly")
            ),
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
        self._sse(events, delay=0.4 if slow else 0)

    def _sse(self, events: list[dict], *, delay: float) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(delay)
        self.close_connection = True


@pytest.fixture
def fake_api() -> Iterator[str]:
    _FakeMessagesApi.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMessagesApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


class _Cli:
    def __init__(self, base_url: str, home: Path) -> None:
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "sk-ant-local-fake",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                str(CLI),
                "--print",
                "--input-format=stream-json",
                "--output-format=stream-json",
                "--verbose",
                "--replay-user-messages",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                "--model=claude-sonnet-4-5",
                "--setting-sources=",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=home,
        )

    def write(self, text: str, *, message_uuid: str, steer: bool) -> None:
        message = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "default",
            "uuid": message_uuid,
            **({"priority": "next"} if steer else {}),
        }
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def read(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line, "the CLI exited"
        return json.loads(line)

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _until(cli: _Cli, done) -> list[dict]:
    seen: list[dict] = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        message = cli.read()
        seen.append(message)
        if done(message, seen):
            return seen
    raise AssertionError(f"timed out; saw {[m.get('type') for m in seen]}")


def _results(seen: list[dict]) -> int:
    return sum(m.get("type") == "result" for m in seen)


def test_a_steer_attached_at_a_tool_boundary_is_echoed_before_the_only_result(fake_api, tmp_path):
    cli = _Cli(fake_api, tmp_path)
    steer = str(uuid.uuid4())
    try:
        cli.write("TOOL run a tool", message_uuid=str(uuid.uuid4()), steer=False)
        _until(
            cli,
            lambda m, _: (
                m.get("type") == "assistant"
                and any(b.get("type") == "tool_use" for b in m["message"]["content"])
            ),
        )
        cli.write("also say banana", message_uuid=steer, steer=True)  # the tool is running
        seen = _until(cli, lambda m, _: m.get("type") == "result")
    finally:
        cli.close()

    echoes = [m for m in seen if m.get("type") == "user" and m.get("uuid") == steer]
    assert len(echoes) == 1 and echoes[0].get("isReplay") is True
    assert _results(seen) == 1
    assert seen.index(echoes[0]) < len(seen) - 1  # before the result
    # The model's next request carried it, in the same turn.
    assert "also say banana" in json.dumps(_FakeMessagesApi.requests[-1]["messages"][-1])


def test_a_steer_that_misses_the_turn_is_echoed_as_the_clis_own_next_turn(fake_api, tmp_path):
    cli = _Cli(fake_api, tmp_path)
    steer = str(uuid.uuid4())
    try:
        cli.write("SLOW answer slowly", message_uuid=str(uuid.uuid4()), steer=False)
        _until(cli, lambda m, _: m.get("type") == "system" and m.get("subtype") == "init")
        time.sleep(0.5)  # the text-only reply is streaming: no tool boundary is left
        cli.write("also say banana", message_uuid=steer, steer=True)
        seen = _until(cli, lambda _m, s: _results(s) == 2)
    finally:
        cli.close()

    first_result = next(i for i, m in enumerate(seen) if m.get("type") == "result")
    echo = next(
        i for i, m in enumerate(seen) if m.get("type") == "user" and m.get("uuid") == steer
    )
    assert first_result < echo  # missed the first turn, answered by the next
    assert seen[echo].get("isReplay") is True
