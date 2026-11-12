"""Opt-in trace of every model call a harness CLI makes (off by default).

With ``OMNIGENT_LLM_TRACE_DIR`` set (dev only), or with spans exported
(:func:`~omnigent.harnesses.tracing.exports_spans`), the process that runs a Claude CLI starts a
small reverse proxy on ``127.0.0.1`` and points the CLI's ``ANTHROPIC_BASE_URL`` at it
(:func:`base_url_for`). The proxy forwards each request unchanged to the real upstream (the
direct API or a gateway, whatever the base URL was), streams the answer straight back, and
records each request: the session, the time, the request body (system, tools with every field,
messages, model, max_tokens), the status and the assembled answer (text, tool calls, stop
reason, usage). Header values are never recorded.

* With the trace directory set it writes one JSONL record per request, and :func:`event`
  records what the executor itself sees (the CLI's ``system/init``, each prompt it sends in).
  Read the records with ``engine/scripts/llm_trace.py <dir>``.
* With spans exported it emits one OpenInference ``LLM`` span per model call, a child of the
  turn span the executor adapter bound to the session (:func:`bind_turn`). The messages and
  the answer are on it only with content capture on.
* With ``OMNIGENT_LLM_CONTEXT_DIR`` set (an eval person's host, ADR 0042) it appends one
  context record per model call to ``<dir>/<conversation>.jsonl`` (:func:`_write_context`):
  the time, model, usage and what the model was sent. The system prompt and the tools are
  written only when they changed, and the messages only past the prefix the Conversation's
  previous call already wrote (``keep``), so the file grows with new messages, not with every
  call's whole history.

Stdlib, plus the OpenTelemetry API and the model catalog's prices for the spans.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from opentelemetry import trace

from omnigent.evals.identity import CONTEXT_DIR_ENV
from omnigent.harnesses.tracing import exports_spans
from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing
from omnigent.runtime.telemetry import should_capture_content

TRACE_DIR_ENV = "OMNIGENT_LLM_TRACE_DIR"
#: Never forwarded as-is to the upstream (hop-by-hop, or rewritten here).
_DROP_REQUEST = {"host", "content-length", "connection", "accept-encoding", "keep-alive"}
_DROP_RESPONSE = {"content-length", "transfer-encoding", "connection", "content-encoding"}
_UPSTREAM_TIMEOUT_S = 900

_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
#: ``session -> its running turn's span``, set by the executor adapter (:func:`bind_turn`) and
#: read on the proxy's threads, so a model call's span joins the turn that made it.
_turn_spans: dict[str, Any] = {}
#: ``session -> the Conversation its turns run for`` (:func:`name_session`): the context file.
_conversations: dict[str, str] = {}
#: ``context file -> (system digest, tools digest, message digests)`` of its last record.
_written: dict[Path, tuple[str, str, list[str]]] = {}
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def trace_dir() -> Path | None:
    """The trace directory, or ``None`` when tracing is off."""
    raw = os.environ.get(TRACE_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def context_dir() -> Path | None:
    """Where context records go (a relative one: under the runner's workspace), or ``None``."""
    raw = os.environ.get(CONTEXT_DIR_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return Path(os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or os.getcwd()) / path


def name_session(session: str, conversation_id: str) -> None:
    """Record that *session*'s model calls are made for *conversation_id* (its context file)."""
    _conversations[session] = conversation_id


def _plain(value: Any) -> Any:
    """*value* without ``cache_control`` marks, which move between calls of one history."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _usage(response: Any) -> dict[str, Any]:
    """A call's usage: a stream's input and cache counts from message_start, the final counts
    from message_delta."""
    response = response if isinstance(response, dict) else {}
    usage = dict(response.get("usage") or {})
    usage.update({k: v for k, v in (response.get("usage_delta") or {}).items() if v})
    return usage


def _write_context(record: dict[str, Any]) -> None:
    """Append *record*'s context record to its Conversation's file; no-op when off or for a
    request that is not a model call. Never raises."""
    folder = context_dir()
    request = record.get("request")
    if folder is None or not isinstance(request, dict) or "messages" not in request:
        return
    try:
        conversation = _conversations.get(record["session"], record["session"])
        path = folder / f"{_UNSAFE.sub('_', conversation)[:120]}.jsonl"
        messages = [_plain(m) for m in request.get("messages") or []]
        system, tools = _plain(request.get("system")), _plain(request.get("tools"))
        digests = [_digest(m) for m in messages]
        response = record.get("response")
        response = response if isinstance(response, dict) else {}
        line: dict[str, Any] = {
            "t": record["t"],
            "ms": record["ms"],
            "status": record["status"],
            "model": response.get("model") or request.get("model"),
            "usage": _usage(response),
        }
        with _lock:
            if not path.exists():
                _written.pop(path, None)  # removed between tries: write it all again
            system_was, tools_was, before = _written.get(path, ("", "", []))
            keep = 0
            while keep < min(len(before), len(digests)) and before[keep] == digests[keep]:
                keep += 1
            line["keep"] = keep
            line["messages"] = messages[keep:]
            if _digest(system) != system_was:
                line["system"] = system
            if _digest(tools) != tools_was:
                line["tools"] = tools
            folder.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            _written[path] = (_digest(system), _digest(tools), digests)
    except (OSError, TypeError, ValueError):
        pass  # a record never breaks a turn


def _write(record: dict[str, Any]) -> None:
    folder = trace_dir()
    if folder is None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _lock, open(folder / f"trace-{os.getpid()}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass  # a trace never breaks a turn


def event(session: str, kind: str, payload: Any) -> None:
    """Record something the executor saw for *session* (``cli_init``, ``prompt``); no-op off."""
    if trace_dir() is not None:
        _write({"kind": kind, "session": session, "t": time.time(), "data": payload})


def bind_turn(session: str, span: Any) -> None:
    """Make *span* the parent of *session*'s model-call spans; ``None`` unbinds it."""
    if span is None:
        _turn_spans.pop(session, None)
    else:
        _turn_spans[session] = span


def unbind_turn(session: str, span: Any) -> None:
    """Unbind *span* from *session*, only if it is still the bound one: a turn that finishes
    unwinding late never takes the next turn's span away."""
    if _turn_spans.get(session) is span:
        _turn_spans.pop(session, None)


def base_url_for(session: str, upstream: str) -> str | None:
    """A base URL for *session*'s CLI that traces through the proxy to *upstream*; ``None`` when
    tracing is off (the CLI keeps its own base URL)."""
    if trace_dir() is None and context_dir() is None and not exports_spans():
        return None
    port = _ensure_server()
    target = base64.urlsafe_b64encode(upstream.rstrip("/").encode()).decode().rstrip("=")
    return f"http://127.0.0.1:{port}/s/{urllib.parse.quote(session, safe='')}/u/{target}"


def _ensure_server() -> int:
    global _server
    with _lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="llm-trace", daemon=True).start()
        return int(_server.server_address[1])


def _assemble_sse(raw: bytes) -> dict[str, Any]:
    """The answer a Messages stream carried: model, text, tool calls, stop reason, usage."""
    out: dict[str, Any] = {"content": []}
    blocks: dict[int, dict[str, Any]] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            data = json.loads(line[5:].strip())
        except ValueError:
            continue
        kind = data.get("type")
        if kind == "message_start":
            message = data.get("message") or {}
            out["model"] = message.get("model")
            out["usage"] = message.get("usage")
        elif kind == "content_block_start":
            block = dict(data.get("content_block") or {})
            block.pop("input", None)
            block["_json"] = ""
            blocks[int(data.get("index", 0))] = block
        elif kind == "content_block_delta":
            block = blocks.setdefault(int(data.get("index", 0)), {"_json": ""})
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + str(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                block["_json"] += str(delta.get("partial_json", ""))
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + str(delta.get("thinking", ""))
        elif kind == "message_delta":
            out["stop_reason"] = (data.get("delta") or {}).get("stop_reason")
            out["usage_delta"] = data.get("usage")
        elif kind == "error":
            out["error"] = data.get("error")
    for index in sorted(blocks):
        block = blocks[index]
        partial = block.pop("_json", "")
        if partial:
            try:
                block["input"] = json.loads(partial)
            except ValueError:
                block["input_raw"] = partial
        out["content"].append(block)
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # the answer ends when the connection closes: no re-chunking

    def log_message(self, *_args: Any) -> None:
        return  # never the stdlib's access line: a path can carry a session id

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def _forward(self) -> None:
        started = time.time()
        parts = self.path.split("/", 5)  # "", "s", session, "u", upstream, rest
        if len(parts) < 5 or parts[1] != "s" or parts[3] != "u":
            self.send_error(404)
            return
        session = urllib.parse.unquote(parts[2])
        padded = parts[4] + "=" * (-len(parts[4]) % 4)
        upstream = urllib.parse.urlsplit(base64.urlsafe_b64decode(padded).decode())
        rest = "/" + (parts[5] if len(parts) > 5 else "")
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_REQUEST}
        headers["accept-encoding"] = "identity"
        secure = upstream.scheme == "https"
        connect = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        conn = connect(upstream.netloc, timeout=_UPSTREAM_TIMEOUT_S)
        captured = bytearray()
        status = 0
        try:
            target = upstream.path.rstrip("/") + rest
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in _DROP_RESPONSE:
                    self.send_header(key, value)
            self.end_headers()
            while chunk := resp.read1(65536):
                captured += chunk
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException) as exc:
            if not status:
                self.send_error(502, type(exc).__name__)
        finally:
            conn.close()
            record = _record(session, self.command, rest, started, status, body, bytes(captured))
            _write(record)
            _write_context(record)
            if exports_spans():
                _emit_span(record)


def _record(
    session: str, method: str, path: str, started: float, status: int, body: bytes, answer: bytes
) -> dict[str, Any]:
    try:
        request: Any = json.loads(body) if body else None
    except ValueError:
        request = {"bytes": len(body)}
    text = answer.lstrip()
    if text.startswith((b"event:", b"data:")):
        response: Any = _assemble_sse(answer)
    else:
        try:
            response = json.loads(answer) if answer else None
        except ValueError:
            response = {"bytes": len(answer)}
    return {
        "kind": "call",
        "session": session,
        "t": started,
        "ms": round((time.time() - started) * 1000),
        "method": method,
        "path": path,
        "status": status,
        "request": request,
        "response": response,
    }


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _emit_span(record: dict[str, Any]) -> None:
    """One OpenInference ``LLM`` span for a model call *record*, under its session's turn."""
    request, response = record.get("request"), record.get("response")
    if not isinstance(request, dict) or "messages" not in request:
        return  # not a model call (a token count, a model list)
    try:
        usage = _usage(response)
        response = response if isinstance(response, dict) else {}
        cached = _count(usage, "cache_read_input_tokens")
        written = _count(usage, "cache_creation_input_tokens")
        prompt = _count(usage, "input_tokens") + cached + written
        completion = _count(usage, "output_tokens")
        model = response.get("model") or request.get("model")
        attrs: dict[str, Any] = {
            "openinference.span.kind": "LLM",
            "session.id": record["session"],
            "llm.token_count.prompt": prompt,
            "llm.token_count.prompt_details.cache_read": cached,
            "llm.token_count.prompt_details.cache_write": written,
            "llm.token_count.completion": completion,
            "llm.token_count.total": prompt + completion,
            "llm.invocation_parameters": json.dumps(
                {"model": request.get("model"), "max_tokens": request.get("max_tokens")}
            ),
            "http.response.status_code": record["status"],
        }
        if isinstance(model, str) and model:
            attrs["llm.model_name"] = model
            pricing = fetch_model_pricing(model) if usage else None
            if pricing is not None:
                attrs["llm.cost.total"] = compute_llm_cost(usage, pricing)
        if should_capture_content():
            asked = {k: request[k] for k in ("system", "messages", "tools") if k in request}
            attrs["input.value"] = json.dumps(asked, ensure_ascii=False, default=str)
            attrs["input.mime_type"] = "application/json"
            attrs["output.value"] = json.dumps(response, ensure_ascii=False, default=str)
            attrs["output.mime_type"] = "application/json"
        parent = _turn_spans.get(record["session"])
        span = trace.get_tracer("omnigent").start_span(
            "llm",
            context=trace.set_span_in_context(parent) if parent is not None else None,
            attributes=attrs,
            start_time=int(record["t"] * 1e9),
        )
        if not 200 <= record["status"] < 400 or response.get("error"):
            span.set_status(trace.StatusCode.ERROR)
        span.end()
    except Exception:  # noqa: BLE001 - a trace never breaks a turn
        pass
