"""Print one Round's (or Conversation's) timeline: what the web was shown and, with ``--trace``,
each model call and prompt of its sessions, merged by time (dev only).

The UX lines come from ``OMNIGENT_UX_LOG_DIR`` in the engine
(``omnigent.superchat.rounds.ux_log``), the trace from ``OMNIGENT_LLM_TRACE_DIR`` in the hosts
(``omnigent.harnesses.llm_trace``). A Round's sessions are its Conversation and the Helpers its
UX lines name. Copy both folders out first, e.g.::

    docker cp superchat-engine-1:/data/ux-log ./ux-log
    docker cp superchat-host-<id>:/home/superchat/.omnigent/llm-trace ./llm-trace
    python3 -I engine/scripts/ux_log.py <round or conversation id> ./ux-log --trace ./llm-trace

For a Round, trace records are kept from :data:`MARGIN_S` seconds before its first UX line to
:data:`MARGIN_S` after its last. ``engine/scripts/llm_trace.py`` prints a call in full.
Stdlib only.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import sys
from typing import Any

#: Seconds around a Round's UX lines whose trace records belong to it.
MARGIN_S = 30.0


def load(folder: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def stamp(t: Any) -> str:
    try:
        moment = datetime.datetime.fromtimestamp(float(t), datetime.UTC)
    except (TypeError, ValueError):
        return "?" * 12
    return moment.strftime("%H:%M:%S.%f")[:-3]


def clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def show_ux(rec: dict[str, Any]) -> str:
    helpers = ", ".join(
        f"{h.get('title') or h.get('id')}:{h.get('status')}" for h in rec.get("helpers") or []
    )
    checklist = ", ".join(
        f"{c.get('subject')}:{c.get('status')}" for c in rec.get("checklist") or []
    )
    now = rec.get("now") or {}
    words = " ".join(str(rec.get("live_words") or "").split())
    parts = [
        f"UX {rec.get('source')} round={rec.get('round_id')} status={rec.get('status')}",
        f"settled_at={rec.get('settled_at')}" if rec.get("settled_at") else "",
        f"delivered_at={rec.get('delivered_at')}" if rec.get("delivered_at") else "",
        f"helpers=[{helpers}]" if helpers else "",
        f"files={rec.get('files')}" if rec.get("files") else "",
        f"checklist=[{checklist}]" if checklist else "",
        f"now={now.get('name')}({clip(now.get('digest') or '', 60)})" if now else "",
        f"words=…{words[-80:]!r}" if words else "",
    ]
    return " ".join(p for p in parts if p)


def show_trace(rec: dict[str, Any]) -> str:
    session = rec.get("session")
    if rec.get("kind") == "call":
        req = rec.get("request") if isinstance(rec.get("request"), dict) else {}
        res = rec.get("response") if isinstance(rec.get("response"), dict) else {}
        blocks = [b for b in res.get("content") or [] if isinstance(b, dict)]
        tools = [str(b.get("name")) for b in blocks if b.get("type") == "tool_use"]
        text = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
        return (
            f"LLM call session={session} {rec.get('path')} -> {rec.get('status')} "
            f"in {rec.get('ms')} ms model={req.get('model')} stop={res.get('stop_reason')}"
            + (f" tools={tools}" if tools else "")
            + (f" error={res.get('error')}" if res.get("error") else "")
            + (f" text={clip(text, 80)!r}" if text else "")
        )
    data = rec.get("data")
    if rec.get("kind") == "prompt":
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return f"LLM prompt session={session} {clip(text, 120)!r}"
    return f"LLM {rec.get('kind')} session={session}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("id", help="a round id or a conversation id")
    ap.add_argument("ux_dir", help="the copied OMNIGENT_UX_LOG_DIR")
    ap.add_argument("--trace", help="the copied OMNIGENT_LLM_TRACE_DIR")
    args = ap.parse_args(argv)
    ux = [r for r in load(args.ux_dir) if args.id in (r.get("round_id"), r.get("conversation_id"))]
    if not ux:
        print(f"no UX lines for {args.id}", file=sys.stderr)
        return 1
    rows = [(float(r.get("t") or 0), show_ux(r)) for r in ux]
    if args.trace:
        sessions = {str(r.get("conversation_id")) for r in ux}
        sessions |= {str(h.get("id")) for r in ux for h in r.get("helpers") or []}
        first, last = min(t for t, _ in rows), max(t for t, _ in rows)
        is_round = any(r.get("round_id") == args.id for r in ux)
        for rec in load(args.trace):
            t = float(rec.get("t") or 0)
            if rec.get("session") not in sessions:
                continue
            if is_round and not first - MARGIN_S <= t <= last + MARGIN_S:
                continue
            rows.append((t, show_trace(rec)))
    for t, text in sorted(rows, key=lambda row: row[0]):
        print(f"{stamp(t)}  {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
