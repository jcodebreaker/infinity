"""Print what each model call of a traced run received and returned (dev only).

The records come from ``OMNIGENT_LLM_TRACE_DIR`` (``omnigent.harnesses.llm_trace``): one JSONL
line per model call, plus what the executor saw (the CLI's ``system/init``, each prompt sent in).
Copy the directory out of the host first, e.g.::

    docker cp superchat-host-<id>:/home/superchat/.omnigent/llm-trace ./llm-trace
    python3 -I engine/scripts/llm_trace.py ./llm-trace [--session ID] [--turn N] [--full]

``--turn N`` keeps the Nth prompt the executor sent for the session (1-based) and the calls up
to the next one. Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
from typing import Any


def load(folder: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return sorted(records, key=lambda r: r.get("t") or 0)


def stamp(t: Any) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(t), datetime.UTC).strftime("%H:%M:%S UTC")
    except (TypeError, ValueError):
        return "?"


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text") or ""))
            elif kind == "tool_result":
                parts.append(
                    f"[tool_result {block.get('tool_use_id')}] {text_of(block.get('content'))}"
                )
            elif kind == "tool_use":
                parts.append(f"[tool_use {block.get('name')}]")
            else:
                parts.append(f"[{kind}]")
        return "\n".join(parts)
    return ""


def clip(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def show_call(rec: dict[str, Any], full: bool) -> None:
    req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    res = rec.get("response") if isinstance(rec.get("response"), dict) else {}
    print(
        f"\n== {stamp(rec.get('t'))} call {rec.get('method')} {rec.get('path')} "
        f"-> {rec.get('status')} in {rec.get('ms')} ms  session={rec.get('session')}"
    )
    if not req.get("messages"):
        print(f"   (not a messages call) {clip(json.dumps(req)[:400], 400)}")
        return
    print(f"   model: {req.get('model')}  max_tokens: {req.get('max_tokens')}")
    system = text_of(req.get("system"))
    lines = system.splitlines()
    print(f"   system: {len(system)} chars, {len(lines)} lines")
    for line in lines[: (len(lines) if full else 6)]:
        print(f"     | {clip(line, 160)}")
    tools = req.get("tools") or []
    names = [
        f"{t.get('name')}{' (deferred)' if t.get('defer_loading') else ''}"
        for t in tools
        if isinstance(t, dict)
    ]
    print(f"   tools ({len(names)}): {', '.join(names)}")
    messages = [m for m in req.get("messages") or [] if isinstance(m, dict)]
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    print(f"   messages: {len(messages)}; last user message:")
    for line in text_of((last_user or {}).get("content")).splitlines()[: (None if full else 12)]:
        print(f"     > {clip(line, 200)}")
    print(f"   returned: stop_reason={res.get('stop_reason')} usage={res.get('usage')}")
    if res.get("error"):
        print(f"     error: {res.get('error')}")
    for block in res.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            print(f"     text: {clip(str(block.get('text') or ''), 600 if not full else 100000)}")
        elif block.get("type") == "tool_use":
            args = json.dumps(block.get("input", block.get("input_raw")), ensure_ascii=False)
            print(f"     tool_use: {block.get('name')} {clip(args, 300 if not full else 100000)}")
        elif block.get("type") == "thinking":
            print(f"     thinking: {len(str(block.get('thinking') or ''))} chars")


def show_event(rec: dict[str, Any], full: bool) -> None:
    data = rec.get("data")
    if rec.get("kind") == "cli_init" and isinstance(data, dict):
        servers = [f"{s.get('name')}={s.get('status')}" for s in data.get("mcp_servers") or []]
        print(
            f"\n-- {stamp(rec.get('t'))} CLI init session={rec.get('session')} "
            f"model={data.get('model')} permission={data.get('permissionMode')}"
        )
        print(f"   mcp servers: {', '.join(servers)}")
        print(f"   tools ({len(data.get('tools') or [])}): {', '.join(data.get('tools') or [])}")
    elif rec.get("kind") == "prompt":
        text = text_of(data) if not isinstance(data, str) else data
        print(f"\n-- {stamp(rec.get('t'))} prompt sent session={rec.get('session')}:")
        for line in text.splitlines()[: (None if full else 20)]:
            print(f"   > {clip(line, 200)}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("dir")
    ap.add_argument("--session", help="only this session id")
    ap.add_argument("--turn", type=int, help="only the Nth prompt sent and the calls after it")
    ap.add_argument("--full", action="store_true", help="print whole prompts and answers")
    args = ap.parse_args(argv)
    records = load(args.dir)
    if args.session:
        records = [r for r in records if r.get("session") == args.session]
    if args.turn:
        prompts = [i for i, r in enumerate(records) if r.get("kind") == "prompt"]
        if args.turn > len(prompts):
            print(f"only {len(prompts)} prompt(s) recorded", file=sys.stderr)
            return 1
        start = prompts[args.turn - 1]
        end = prompts[args.turn] if args.turn < len(prompts) else len(records)
        records = records[start:end]
    for rec in records:
        if rec.get("kind") == "call":
            show_call(rec, args.full)
        else:
            show_event(rec, args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
