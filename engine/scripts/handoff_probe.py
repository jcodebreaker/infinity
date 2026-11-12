"""Handoff probe and today's cache numbers (ADR 0031 step 0; dev only, stdlib only).

Two jobs:

``numbers TRACE_DIR``
    Today's baseline from model-call traces (``OMNIGENT_LLM_TRACE_DIR`` records, copied out of
    the host; sub-folders are read and duplicate copies of a record counted once), per harness
    and role (Conversation, Helper, studio, notes; told apart by the system prompt): tokens per
    Round, cache-read share, cache-write share of input dollars, the first call after a cold
    start, and the cache lifetime seen (gap since the previous request's *start* vs cache hit).

``run``
    Replays real Conversations into new throwaway Conversations on the running stack and asks
    the same probes in each arm:

    * ``warm@C``: replay the person's messages up to cut point C (25/50/75% of their turns),
      then probe. The baseline.
    * ``single@C``: replay to C, hand off once, then probe.
    * ``chain``: replay to 75% with 5 handoffs spread over it, the last at 75%, then probe.

    A handoff drops the warm harness so the next turn is a cold start from stored state. Today
    the only engine route that does that is a harness switch: ``POST
    /v1/sessions/{id}/switch-agent`` to the other built-in Assistant and straight back (each
    switch resets the runner-side harness). ``--handoff-cmd`` runs your own command instead
    (``{session}`` is replaced by the session id).

    Probes, one question and one task follow-up each: ``words`` (the person's earliest
    request), ``file`` (the newest file's id, version and last change), ``decision``, ``next``
    and ``open`` (an open ask or a pending job). Exact answers are scored by string match; the
    rest by ``--judge-cmd`` (a command of another model family that reads a prompt on stdin
    and prints ``{"score": 0-5}``; blind to the arm), or later with ``judge``. 10% of judged
    rows are marked for a human spot check. A reply that mentions a reload, a summary or
    catching up is a hard fail.

``report OUT_DIR``  paired difference of each arm against ``warm`` at the same cut point
    (same case, probe and repeat), with a 95% confidence interval; with ``--trace-dir`` the
    per-probe tool, ``recall``/``session_history`` counts and the first call after a handoff
    come from the model-call traces (request bodies, usage, tool_use blocks).
``judge OUT_DIR --judge-cmd CMD``  scores rows left unjudged by ``run``.
``cleanup OUT_DIR``  deletes the sessions this probe created (and only those: ids from
    ``OUT_DIR/state.json`` whose title still starts with ``probe:``). Deleting a session
    deletes its Helpers.

Source Conversations are read from the train Postgres inside ``BEGIN READ ONLY … ROLLBACK``
(``docker exec <container> psql``); nothing is ever posted into them. The engine runs in local
single-user mode (no token). ``--dry-run`` prints the plan and every HTTP call without sending
anything (it still reads the database).

Point in time: a replay builds its own Conversation, so its artifacts, Rounds, cards,
checkpoint and recall entries exist only up to the cut point. Person-level state (memory
profile, Projects, daily notes, ``memory/`` files) is shared and cannot be filtered over HTTP;
the plan prints how many such rows are newer than each cut point, and every reply is checked
for words that only appear in the source *after* the cut point (``leak``).

Billable: every replayed turn is a real model call (Helpers included). Start small::

    python3 -I engine/scripts/handoff_probe.py numbers ./llm-trace
    python3 -I engine/scripts/handoff_probe.py run --harness pi --dry-run
    python3 -I engine/scripts/handoff_probe.py run --harness pi --cases 1 --max-turns 8 \\
        --arms warm@50,single@50 --repeats 1 --probes words,file --out ./probe-out
    python3 -I engine/scripts/handoff_probe.py report ./probe-out/<run> --trace-dir ./llm-trace
    python3 -I engine/scripts/handoff_probe.py cleanup ./probe-out/<run>
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import hashlib
import json
import math
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENGINE = "http://localhost:8013"
DB_CONTAINER = "superchat-train-postgres-1"
BUNDLE = {"pi": "superchat-pi", "claude-sdk": "superchat-claude"}
OTHER = {"pi": "claude-sdk", "claude-sdk": "pi"}
CUTS = (25, 50, 75)
#: How often, and how far apart, a refused call is retried while the stack says "unavailable".
SEND_RETRIES, SEND_RETRY_S = 10, 30.0
CHAIN_HANDOFFS = 5
PROBE_PREFIX = "probe:"
#: Host owner key of each probe session's own host (container and home volume).
PROBE_HOST_PREFIX = "probe-"
TRACE_DIR_IN_HOST = "/home/superchat/.omnigent/llm-trace"
RUNNER_LOG_DIR_IN_HOST = "/home/superchat/.omnigent/logs/runner"
#: Source person messages that are dev pings, not work.
SKIP_DEFAULT = r"^(Reply with one word|Quick check after a restart|say hi$|hi$)"
#: Tools that read the past back.
RECALL_TOOLS = frozenset({"recall", "session_history", "sys_session_get_history"})
#: Hard fail: the reply talks about the machinery of a new session.
HARD_FAIL = re.compile(
    r"\b(reload(ed|ing)?|summar(y|ies|ised|ized) (of|from) (our|the|this) (chat|conversation|"
    r"session)|from (the|a|my) summary|catch(ing)? up|caught up|(fresh|new) session|"
    r"session (was )?(restart|reset)|after (the|a) restart|context (was )?(reset|cleared|lost))\b",
    re.I,
)
#: Prices relative to one uncached input token (Anthropic: write 1.25x for 5 minutes, 2x for
#: one hour, read 0.1x).
PRICE = {"input": 1.0, "w5m": 1.25, "w1h": 2.0, "read": 0.1}

PROBES: dict[str, tuple[str, str]] = {
    # kind: (question, task follow-up)
    "words": (
        "What was the first thing I asked you for in this chat? Quote my words exactly.",
        "Turn that first request of mine into a one-line title for this chat.",
    ),
    "file": (
        "Which file did we work on most recently? Give its id, its version and what changed "
        "last.",
        "Make that file a little darker in tone. Keep everything else as it is.",
    ),
    "decision": (
        "What is the most recent decision we made together, and why did we make it?",
        "Write that decision as a one-line note I can send to the team.",
    ),
    "next": (
        "What is the next step on what we are doing?",
        "Go ahead with that next step.",
    ),
    "open": (
        "Is anything still open: a question waiting for me, or work still running?",
        "Give me a one-line status update I can paste to the team.",
    ),
}


# ── small helpers ─────────────────────────────────────────────────────────────


def say(*parts: Any) -> None:
    print(*parts, flush=True)


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]


def t_crit(n: int) -> float:
    """Two-sided 95% Student t critical value for *n* - 1 degrees of freedom."""
    table = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26}
    df = n - 1
    if df in table:
        return table[df]
    return 2.23 if df <= 10 else 2.09 if df <= 20 else 2.04 if df <= 30 else 1.96


def paired(diffs: list[float]) -> str:
    if not diffs:
        return "n=0"
    mean = statistics.fmean(diffs)
    if len(diffs) < 2:
        return f"{mean:+.2f} (n=1, no CI)"
    half = t_crit(len(diffs)) * statistics.stdev(diffs) / math.sqrt(len(diffs))
    return f"{mean:+.2f} [{mean - half:+.2f}, {mean + half:+.2f}] n={len(diffs)}"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


# ── database (read only) ──────────────────────────────────────────────────────


def db_query(container: str, sql: str) -> Any:
    """One ``select json_agg(...)`` inside a read-only transaction that is rolled back."""
    script = f"BEGIN READ ONLY;\n{sql};\nROLLBACK;\n"
    out = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "omnigent", "-d", "omnigent", "-qAt",
         "-v", "ON_ERROR_STOP=1"],
        input=script, capture_output=True, text=True, timeout=60, check=False,
    )
    if out.returncode != 0:
        raise SystemExit(f"database read failed: {out.stderr.strip()[:300]}")
    text = out.stdout.strip()  # -q: only the one result; json_agg may span lines
    return json.loads(text) if text else None


def source_cases(container: str, n: int, only: str | None, skip: re.Pattern[str]) -> list[dict]:
    """The *n* top-level Conversations with the most person messages, each with its turns."""
    where = f"and encode(c.id,'hex') = '{only}'" if only else ""
    if only and not re.fullmatch(r"[0-9a-f]{32}", only):
        raise SystemExit("--source must be a 32-hex session id")
    convs = db_query(container, f"""
        select json_agg(x) from (
          select encode(c.id,'hex') as id, c.title, c.created_at,
            (select json_object_agg(l.key, l.value) from engine.conversation_labels l
              where l.conversation_id = c.id and l.key in
              ('omnigent.context.mode','omnigent.host.owner','omnigent.tenant',
               'omnigent.sandbox.provider')) as labels
          from engine.conversations c
          where c.parent_conversation_id is null and coalesce(c.title,'') not like 'probe:%'
            and exists (select 1 from engine.conversation_labels l where l.conversation_id=c.id
              and l.key='omnigent.context.mode' and l.value='superside-chat') {where}
          order by c.updated_at desc limit 50) x""") or []
    cases = []
    for conv in convs:
        items = db_query(container, f"""
            select json_agg(x order by x.position) from (
              select position, created_at, data from engine.conversation_items
              where conversation_id = decode('{conv["id"]}','hex') and type = 1) x""") or []
        person, said = [], []
        for row in items:
            data = json.loads(row["data"])
            text = "".join(
                p.get("text", "") for p in data.get("content") or [] if isinstance(p, dict)
            ).strip()
            if data.get("role") == "user":
                if text and not text.startswith("[System:") and not skip.search(text):
                    person.append({"at": row["created_at"], "text": text})
            elif data.get("role") == "assistant" and text:
                said.append({"at": row["created_at"], "text": text})
        if len(person) >= 4:
            cases.append({**conv, "person": person, "assistant": said})
    cases.sort(key=lambda c: -len(c["person"]))
    return cases[:n]


def person_state_newer(container: str, at: int) -> dict[str, int]:
    """Person-level rows written after *at* (they leak into a replay; not filterable)."""
    row = db_query(container, f"""
        select json_build_object(
          'memory_claims', (select count(*) from engine.memory_claims where updated_at > {at}),
          'projects', (select count(*) from engine.projects where updated_at > {at}),
          'daily_notes', (select count(*) from engine.daily_notes where updated_at > {at}))""")
    return dict(row or {})


# ── engine over HTTP ──────────────────────────────────────────────────────────


class Engine:
    """The engine routes the probe uses. With ``dry`` it prints each call and sends nothing."""

    def __init__(self, base: str, dry: bool) -> None:
        self.base, self.dry, self._fake = base.rstrip("/"), dry, 0

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        if self.dry:
            say(f"    HTTP {method} {path}" + (f"  {json.dumps(body)[:300]}" if body else ""))
            return None
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for attempt in range(SEND_RETRIES + 1):
            try:
                # A first message waits for its new host to launch (a cold container).
                with urllib.request.urlopen(req, timeout=60 if method == "GET" else 300) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                say(f"    HTTP {exc.code} on {method} {path}: {detail}")
                # 503 (runner or provider unreachable) and 429 pass; an older engine answered an
                # unreachable model provider with 400 "Model discovery is unavailable".
                busy = exc.code in (429, 503) or (
                    exc.code == 400 and "unavailable" in detail.lower())
                if not busy or attempt == SEND_RETRIES:
                    raise
                time.sleep(SEND_RETRY_S)
            except OSError:
                if method != "GET" or attempt:
                    raise
        return None

    def agent_ids(self) -> dict[str, str]:
        """``{harness: built-in agent id}`` of the two Assistant bundles."""
        if self.dry:
            self.call("GET", "/v1/agents?limit=200")
            return {h: f"<agent-id:{b}>" for h, b in BUNDLE.items()}
        page = self.call("GET", "/v1/agents?limit=200") or {}
        by_name = {a.get("name"): a.get("id") for a in page.get("data") or []}
        missing = [b for b in BUNDLE.values() if b not in by_name]
        if missing:
            raise SystemExit(f"built-in agents missing: {missing}")
        return {h: by_name[b] for h, b in BUNDLE.items()}

    def create(
        self,
        title: str,
        agent_id: str,
        labels: dict[str, str],
        host_owner: str,
        extra: dict[str, str] | None = None,
    ) -> str:
        """A new top-level Super Chat session on its own throwaway host, without Muse labels.

        Mirrors ``/v1/me/muse``'s create body minus ``omnigent.superchat.muse*``, so it never
        becomes the person's Super Chat. *host_owner* keys a fresh host container and home
        volume: a second managed session on the person's host would start a second host daemon
        there, which the first one refuses, and replays would write into the person's files.
        """
        keep = {
            k: v for k, v in labels.items() if k in ("omnigent.context.mode", "omnigent.tenant")
        }
        keep["omnigent.host.owner"] = host_owner
        # --label: e.g. a lower kept tail or handoff threshold for a live check.
        keep.update(extra or {})
        provider = labels.get("omnigent.sandbox.provider")
        body = {
            "agent_id": agent_id,
            "title": title,
            "labels": keep,
            "host_type": "managed" if provider else "external",
            "sandbox_provider": provider,
        }
        made = self.call("POST", "/v1/sessions", body)
        if self.dry:
            self._fake += 1
            return f"<new-session-{self._fake}>"
        return str(made["id"])

    def send(self, sid: str, text: str) -> None:
        self.call("POST", f"/v1/sessions/{sid}/events", {"type": "message", "data": {
            "role": "user", "content": [{"type": "input_text", "text": text}]}})

    def items(self, sid: str) -> list[dict]:
        return list((self.call("GET", f"/v1/sessions/{sid}?include_liveness=false") or {})
                    .get("items") or [])

    def status(self, sid: str) -> str:
        snap = self.call("GET", f"/v1/sessions/{sid}?include_items=false&include_liveness=false")
        return str((snap or {}).get("status") or "")

    def rounds_running(self, sid: str) -> bool:
        page = self.call("GET", f"/v1/sessions/{sid}/rounds?limit=3") or {}
        return any(r.get("status") == "running" for r in page.get("data") or [])

    def artifacts(self, sid: str) -> list[dict]:
        page = self.call("GET", f"/v1/artifacts?parent_session_id={sid}&limit=50") or {}
        return list(page.get("artifacts") or [])

    def switch(self, sid: str, agent_id: str) -> None:
        self.call("POST", f"/v1/sessions/{sid}/switch-agent", {"agent_id": agent_id})

    def delete(self, sid: str) -> None:
        self.call("DELETE", f"/v1/sessions/{sid}")

    def title(self, sid: str) -> str:
        snap = self.call("GET", f"/v1/sessions/{sid}?include_items=false&include_liveness=false")
        return str((snap or {}).get("title") or "")


def fields(item: dict) -> dict:
    data = item.get("data")
    return data if isinstance(data, dict) else item


def item_text(item: dict) -> str:
    parts = fields(item).get("content") or []
    if isinstance(parts, str):
        return parts
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def tool_name(item: dict) -> str:
    return str(fields(item).get("name") or "").rsplit("__", 1)[-1]


def turn(engine: Engine, sid: str, text: str, timeout: float, quiet_s: float) -> dict:
    """Send one person message and wait until the turn and any Helper it started settle."""
    if engine.dry:
        engine.send(sid, text)
        return {"reply": "", "tools": [], "first_word_s": None, "items": []}
    seen = {i.get("id") for i in engine.items(sid)}
    t0 = time.monotonic()
    engine.send(sid, text)
    first_word, still, last_n = None, 0.0, -1
    while time.monotonic() - t0 < timeout:
        time.sleep(2.0)
        new = [i for i in engine.items(sid) if i.get("id") not in seen]
        if first_word is None and any(
            i.get("type") == "message" and fields(i).get("role") == "assistant"
            and item_text(i).strip() for i in new
        ):
            first_word = round(time.monotonic() - t0, 1)
        settled = (
            any(fields(i).get("role") != "user" for i in new)
            and engine.status(sid) in ("idle", "waiting")
            and not engine.rounds_running(sid)
        )
        still = still + 2.0 if settled and len(new) == last_n else 0.0
        last_n = len(new)
        if still >= quiet_s:
            break
    else:
        say(f"      TIMEOUT after {timeout:.0f}s")
    new = [i for i in engine.items(sid) if i.get("id") not in seen]
    replies = [item_text(i) for i in new
               if i.get("type") == "message" and fields(i).get("role") == "assistant"]
    calls = [i for i in new if i.get("type") in ("function_call", "tool_call")]
    return {
        "reply": "\n".join(r for r in replies if r.strip()),
        "tools": [tool_name(c) for c in calls],
        "args": [str(fields(c).get("arguments") or "") for c in calls],
        "first_word_s": first_word,
        "sent_at": time.time(),
    }


# ── run ───────────────────────────────────────────────────────────────────────


def arm_plan(arm: str, n_person: int) -> tuple[int, list[int]]:
    """``(person turns replayed before probing, turn indices after which to hand off)``."""
    if arm == "chain":
        stop = max(1, round(n_person * 0.75))
        points = sorted({max(1, round(stop * k / CHAIN_HANDOFFS)) for k in
                         range(1, CHAIN_HANDOFFS + 1)})
        return stop, points
    kind, _, cut = arm.partition("@")
    stop = max(1, round(n_person * int(cut) / 100))
    return stop, ([stop] if kind == "single" else [])


def handoff(engine: Engine, sid: str, harness: str, agents: dict[str, str], cmd: str | None,
            settle_s: float) -> None:
    if cmd:
        line = cmd.replace("{session}", sid)
        say(f"    RUN {line}")
        if not engine.dry:
            subprocess.run(shlex.split(line), check=True, timeout=300)
    else:
        engine.switch(sid, agents[OTHER[harness]])
        engine.switch(sid, agents[harness])
    if not engine.dry:
        time.sleep(settle_s)  # the runner-side reset runs as a background task after the switch


def expected(engine: Engine, sid: str, case: dict, stop: int) -> dict[str, Any]:
    """What a string match can check: the earliest request and the newest file."""
    first = next((p["text"] for p in case["person"][:stop] if len(words(p["text"])) >= 4), "")
    files = [] if engine.dry else engine.artifacts(sid)
    newest = files[0] if files else {}
    return {"words": first, "file_id": newest.get("artifact_id") or newest.get("id"),
            "file_version": newest.get("version"), "file_name": newest.get("name") or "",
            "files": len(files)}


def leak_words(case: dict, stop: int) -> set[str]:
    before = {w for p in case["person"][:stop] for w in words(p["text"])}
    before |= {w for p in case["assistant"] for w in words(p["text"])
               if p["at"] <= case["person"][stop - 1]["at"]}
    after = {w for p in case["person"][stop:] for w in words(p["text"]) if len(w) >= 6}
    return after - before - set(" ".join(q for pair in PROBES.values() for q in pair).split())


def score_exact(kind: str, reply: str, exp: dict) -> int | None:
    if kind == "words" and exp["words"]:
        want = set(words(exp["words"]))
        return int(len(want & set(words(reply))) >= 0.8 * len(want))
    if kind == "file" and exp["file_id"]:
        has_id = exp["file_id"][:8] in reply
        # "v2", "version 2", "**Version:** 2"
        has_v = bool(re.search(rf"\bv(?:ersion)?\W{{0,4}}{exp['file_version']}\b", reply, re.I))
        return int(has_id and has_v)
    return None


def score_task(kind: str, res: dict, exp: dict, files_before: int, files_after: int) -> dict:
    tools = res.get("tools") or []
    out = {
        "tool_calls": len(tools),
        "recall_calls": sum(t in RECALL_TOOLS for t in tools),
        "asked_back": "ask_clarification" in tools,
        "redo": files_after > files_before and kind != "next",
    }
    if kind == "file" and exp["file_id"]:
        args = " ".join(res.get("args") or [])
        # Saves name the file by its path, edits and outlines by id.
        name = os.path.basename(exp.get("file_name") or "")
        out["right_file"] = exp["file_id"] in args or bool(name and name in args)
        out["right_version"] = bool(re.search(
            rf'"base_version":\s*{exp["file_version"]}\b', args))
    return out


def judge_prompt(kind: str, question: str, reply: str, case: dict, stop: int, exp: dict) -> str:
    cutoff_at = case["person"][stop - 1]["at"]
    lines = [f"PERSON: {p['text'][:400]}" for p in case["person"][:stop]]
    lines += [f"ASSISTANT: {a['text'][:400]}" for a in case["assistant"] if a["at"] <= cutoff_at]
    return (
        "You grade whether an assistant remembers a conversation. Score its answer 0-5:\n"
        "5 = fully right and specific, 3 = partly right or vague, 0 = wrong, invented or "
        "refuses. Reply with JSON only: {\"score\": <0-5>, \"why\": \"<one line>\"}.\n\n"
        f"Reference (the conversation so far, unordered excerpts):\n" + "\n".join(lines[-60:])
        + f"\nNewest file: id {exp.get('file_id')} version {exp.get('file_version')}\n\n"
        f"Question ({kind}): {question}\n\nAnswer to grade:\n{reply[:3000]}\n"
    )


def run_judge(cmd: str, prompt: str) -> int | None:
    out = subprocess.run(shlex.split(cmd), input=prompt, capture_output=True, text=True,
                         timeout=300, check=False)
    found = re.search(r'"score"\s*:\s*([0-5])', out.stdout)
    return int(found.group(1)) if found else None


def cmd_run(args: argparse.Namespace) -> int:
    if args.script:
        return cmd_run_script(args)
    skip = re.compile(args.skip, re.I)
    cases = source_cases(args.db_container, args.cases, args.source, skip)
    if not cases:
        raise SystemExit("no source Conversation with 4+ person messages")
    arms = (args.arms or ",".join(
        [f"warm@{c}" for c in CUTS] + [f"single@{c}" for c in CUTS] + ["chain"])).split(",")
    for arm in arms:
        if arm != "chain" and not re.fullmatch(r"(warm|single)@\d{1,2}", arm):
            raise SystemExit(f"bad arm {arm!r}: warm@C, single@C or chain")
    kinds = args.probes.split(",")
    run_id = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / run_id
    engine = Engine(args.engine, args.dry_run)
    extra_labels = dict(item.split("=", 1) for item in args.label)
    say(f"handoff probe {run_id}: harness={args.harness} arms={arms} repeats={args.repeats} "
        f"probes={kinds} handoff={'command' if args.handoff_cmd else 'switch-agent there and back'}")
    for case in cases:
        n = min(len(case["person"]), args.max_turns or len(case["person"]))
        case["person"] = case["person"][:n]
        say(f"source {case['id']} {case['title']!r}: {n} person turns replayed at most")
        for arm in arms:
            stop, points = arm_plan(arm, n)
            newer = person_state_newer(args.db_container, case["person"][stop - 1]["at"])
            say(f"  {arm}: replay {stop} turns, handoffs after turns {points or '-'}, then "
                f"{len(kinds)} probes + {len(kinds)} tasks; person-level rows newer than the "
                f"cut: {newer}")
    if args.dry_run:
        say("\nHTTP calls, repeat 1: the first arm in full, then each other arm's create and "
            "handoffs. After each send the probe polls GET /v1/sessions/{id}?include_liveness="
            "false, GET /v1/sessions/{id}?include_items=false&include_liveness=false and GET "
            "/v1/sessions/{id}/rounds?limit=3 every 2 s until settled, and GET "
            "/v1/artifacts?parent_session_id={id}&limit=50 around each task.")
    else:
        out.mkdir(parents=True, exist_ok=True)
    agents = engine.agent_ids()
    state: dict[str, Any] = {"run": run_id, "harness": args.harness, "sessions": [], "hosts": []}
    rows: list[dict] = []
    for case in cases:
        for arm in arms:
            stop, points = arm_plan(arm, len(case["person"]))
            for rep in range(1, (1 if args.dry_run else args.repeats) + 1):
                full = not args.dry_run or arm == arms[0]
                title = f"{PROBE_PREFIX}{run_id}:{case['id'][:8]}:{arm}:r{rep}"
                host_owner = f"{PROBE_HOST_PREFIX}{run_id}-{len(state['sessions']) + 1}"
                sid = engine.create(
                    title, agents[args.harness], case["labels"] or {}, host_owner, extra_labels
                )
                state["sessions"].append(sid)
                state["hosts"].append(host_owner)
                if not args.dry_run:
                    (out / "state.json").write_text(json.dumps(state, indent=1))
                say(f"  [{title}] session {sid}")
                # Trace session keys are not session ids: a ref on the first message (it is in
                # every later cold start's history too) ties this arm's traced calls to it.
                ref = hashlib.sha256(title.encode()).hexdigest()[:8]
                for i, msg in enumerate(case["person"][:stop], start=1):
                    if full:
                        text = f"{msg['text']} (ref {ref})" if i == 1 else msg["text"]
                        say(f"    turn {i}/{stop}: {text[:70]!r}")
                        turn(engine, sid, text, args.turn_timeout, args.quiet)
                    if i in points:
                        say(f"    handoff after turn {i}")
                        handoff(engine, sid, args.harness, agents, args.handoff_cmd,
                                args.handoff_settle)
                if not full:
                    say(f"    (the {stop} turns around these handoffs and the probes, as above)")
                    continue
                exp = expected(engine, sid, case, stop)
                avoid = leak_words(case, stop)
                for kind in kinds:
                    question, task = PROBES[kind]
                    asked = turn(engine, sid, question, args.turn_timeout, args.quiet)
                    before = exp["files"] if args.dry_run else len(engine.artifacts(sid))
                    done = turn(engine, sid, task, args.turn_timeout, args.quiet)
                    after = before if args.dry_run else len(engine.artifacts(sid))
                    replies = asked["reply"] + "\n" + done["reply"]
                    row = {
                        "run": run_id, "case": case["id"], "arm": arm, "repeat": rep,
                        "cut": 75 if arm == "chain" else int(arm.split("@")[1]),
                        "harness": args.harness, "session": sid, "ref": ref, "kind": kind,
                        "question": question, "task": task, "reply": asked["reply"],
                        "task_reply": done["reply"],
                        "exact": score_exact(kind, asked["reply"], exp),
                        "task_score": score_task(kind, done, exp, before, after),
                        "hard_fail": bool(HARD_FAIL.search(replies)),
                        "leak": sorted(avoid & set(words(replies)))[:10],
                        "first_word_s": asked["first_word_s"],
                        "item_tools": asked["tools"],
                        "expected": exp,
                        "judge_prompt": judge_prompt(kind, question, asked["reply"], case,
                                                     stop, exp),
                        "judge": None,
                    }
                    if args.judge_cmd and row["exact"] is None and not args.dry_run:
                        row["judge"] = run_judge(args.judge_cmd, row["judge_prompt"])
                    rows.append(row)
                if args.dry_run:
                    continue
                stored = stored_boilerplate(engine, sid)
                for row in rows[-len(kinds):]:
                    row["stored"] = stored
                with (out / "results.jsonl").open("a", encoding="utf-8") as fh:
                    for row in rows[-len(kinds):]:
                        fh.write(json.dumps(row) + "\n")
    if args.dry_run:
        say(f"\ndry run: {args.repeats} repeats of each arm would run; cleanup deletes only "
            f"the sessions listed in OUT/state.json")
        return 0
    mark_spot_checks(out)
    say(f"\nresults: {out}/results.jsonl")
    return report(out, args.trace_dir, args.engine_log)


# ── scripted cases (``run --script``) ─────────────────────────────────────────


def load_script(path: str) -> dict[str, Any]:
    """A scripted case: the person turns, the chain's handoff points and the probes.

    ``{"id", "title", "turns": [text], "chain": [turn numbers], "probes": [{"id", "question",
    "task" (or null), "truth", "must": [regex], "must_not": [check], "task_must": [regex],
    "task_must_not": [check]}], "pause_before_probes_s": seconds}``. A turn is a string or
    ``{"text", "pause_after_s"}`` (a sleep once it settles, e.g. past the cache lifetime). A
    check is ``{"re": regex, "unless": regex}``: a match counts only when *unless* is not found
    in the rest of its sentence, before or after it (so "was CAD 18,500" or "because the week of
    March 9 clashes" is no stale value). Regexes are case-insensitive and may
    name ``{file}`` (the newest file's id prefix or name), ``{version}`` (its version) and
    ``{option2}`` (the second option named in turn 3's reply), filled in at probe time.
    """
    script = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("id", "turns", "probes"):
        if key not in script:
            raise SystemExit(f"script {path}: missing {key!r}")
    return script


#: How far before a match its quoted context reaches in the results.
_UNLESS_WINDOW = 40
_SENTENCE_END = re.compile(r"[.!?\n]")


def _sentence_around(text: str, start: int, end: int) -> str:
    """The sentence a match sits in, without the match: what its exemption reads."""
    head = max((m.end() for m in _SENTENCE_END.finditer(text, 0, start)), default=0)
    tail = _SENTENCE_END.search(text, end)
    return text[head:start] + " " + text[end : tail.start() if tail else len(text)]


def _option2(reply: str) -> str | None:
    """The second option a list reply names: ``2. **Name**``, ``Option 2: Name``, ``### 2 Name``."""
    hit = re.search(
        r"^\s*(?:#+\s*)?(?:[-*]\s*)?(?:\*\*)?(?:option\s*)?2(?:\*\*)?[.):]?\s*(?:[—–:-]\s*)?"
        r"(?:\*\*)?([A-Z][^\n*—–:(|]{2,60}?)\s*(?:\*\*|[—–:(|]|\s-\s|$)",
        reply,
        re.I | re.M,
    )
    return hit.group(1).strip() if hit else None


def _fill(pattern: str, slots: dict[str, str | None]) -> str | None:
    """*pattern* with its slots filled, or ``None`` when a slot it names is unknown."""
    for name, value in slots.items():
        if "{" + name + "}" in pattern:
            if not value:
                return None
            pattern = pattern.replace("{" + name + "}", value)
    return pattern


def check_text(
    text: str, must: list[str], must_not: list[dict], slots: dict[str, str | None]
) -> dict[str, Any]:
    """Each ``must`` regex (all must match) and ``must_not`` check (any match fails)."""
    musts = []
    for raw in must:
        pattern = _fill(raw, slots)
        musts.append({"re": raw, "ok": None if pattern is None
                      else bool(re.search(pattern, text, re.I))})
    hits = []
    for check in must_not:
        pattern = _fill(check["re"], slots)
        if pattern is None:
            hits.append({"re": check["re"], "hit": None, "at": None})
            continue
        unless = check.get("unless")
        found = None
        for m in re.finditer(pattern, text, re.I):
            if unless and re.search(unless, _sentence_around(text, m.start(), m.end()), re.I):
                continue
            found = text[max(0, m.start() - _UNLESS_WINDOW) : m.end() + 20]
            break
        hits.append({"re": check["re"], "hit": found is not None, "at": found})
    ok = all(m["ok"] is not False for m in musts) and not any(h["hit"] for h in hits)
    return {"pass": ok, "must": musts, "must_not": hits}


def script_arm_plan(arm: str, script: dict) -> list[int]:
    """Handoff points for a scripted arm: ``warm`` none, ``single@N`` after turn N, ``chain``
    the script's ``chain`` list. The probes always follow the last turn."""
    if arm in ("warm", "warm@100"):
        return []
    if arm == "chain":
        return list(script.get("chain") or [])
    hit = re.fullmatch(r"single@(\d+)", arm)
    if hit and 1 <= int(hit.group(1)) <= len(script["turns"]):
        return [int(hit.group(1))]
    raise SystemExit(f"bad scripted arm {arm!r}: warm, single@<turn> or chain")


def cmd_run_script(args: argparse.Namespace) -> int:
    script = load_script(args.script)
    arms = args.arms.split(",") if args.arms else ["warm", "chain"]
    plans = {arm: script_arm_plan(arm, script) for arm in arms}
    run_id = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / run_id
    engine = Engine(args.engine, args.dry_run)
    extra_labels = dict(item.split("=", 1) for item in args.label)
    turns = script["turns"]
    say(f"handoff probe {run_id}: script {script['id']} ({len(turns)} turns, "
        f"{len(script['probes'])} probes), harness={args.harness} arms={plans} "
        f"repeats={args.repeats} labels={extra_labels or '-'}")
    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    agents = engine.agent_ids()
    # The mode, tenant and sandbox labels of the person's own Conversation, as a replay borrows
    # them: without them the session is no Super Chat and gets no managed host.
    found = source_cases(args.db_container, 1, args.source, re.compile(args.skip, re.I))
    base_labels = (found[0]["labels"] or {}) if found else {}
    state: dict[str, Any] = {"run": run_id, "harness": args.harness, "sessions": [], "hosts": []}
    for arm, points in plans.items():
        for rep_n in range(1, (1 if args.dry_run else args.repeats) + 1):
            title = f"{PROBE_PREFIX}{run_id}:{script['id']}:{arm}:r{rep_n}"
            host_owner = f"{PROBE_HOST_PREFIX}{run_id}-{len(state['sessions']) + 1}"
            sid = engine.create(title, agents[args.harness], base_labels, host_owner, extra_labels)
            state["sessions"].append(sid)
            state["hosts"].append(host_owner)
            if not args.dry_run:
                (out / "state.json").write_text(json.dumps(state, indent=1))
            say(f"  [{title}] session {sid}")
            ref = hashlib.sha256(title.encode()).hexdigest()[:8]
            said: list[str] = []
            for i, entry in enumerate(turns, start=1):
                text = entry["text"] if isinstance(entry, dict) else entry
                sent = f"{text} (ref {ref})" if i == 1 else text
                say(f"    turn {i}/{len(turns)}: {text[:70]!r}")
                said.append(turn(engine, sid, sent, args.turn_timeout, args.quiet)["reply"])
                pause = entry.get("pause_after_s") if isinstance(entry, dict) else None
                if pause:
                    say(f"    pause {pause}s")
                    if not args.dry_run:
                        time.sleep(float(pause))
                if i in points:
                    say(f"    handoff after turn {i}")
                    handoff(engine, sid, args.harness, agents, args.handoff_cmd,
                            args.handoff_settle)
            if script.get("pause_before_probes_s"):
                say(f"    pause {script['pause_before_probes_s']}s before the probes")
                if not args.dry_run:
                    time.sleep(float(script["pause_before_probes_s"]))
            # Any file the session saved: its id prefix, name, name without extension or title.
            files = [] if engine.dry else engine.artifacts(sid)
            handles: list[str] = []
            for f in files:
                file_id = str(f.get("artifact_id") or f.get("id") or "")
                name = os.path.basename(str(f.get("name") or ""))
                handles += [file_id[:8], name, name.split(".")[0], str(f.get("title") or "")]
            versions = sorted({str(f["version"]) for f in files if f.get("version") is not None})
            slots = {
                "file": "|".join(sorted({re.escape(h) for h in handles if len(h) >= 3})) or None,
                "version": "|".join(versions) or None,
                "option2": re.escape(o) if (o := _option2(said[2] if len(said) > 2 else ""))
                else None,
            }
            rows = []
            for probe in script["probes"]:
                asked = turn(engine, sid, probe["question"], args.turn_timeout, args.quiet)
                done = (turn(engine, sid, probe["task"], args.turn_timeout, args.quiet)
                        if probe.get("task") else {"reply": "", "args": [], "tools": []})
                # A task may write its answer into a file: its tool arguments count as its text.
                task_text = done["reply"] + "\n" + "\n".join(done.get("args") or [])
                checks = check_text(asked["reply"], probe.get("must", []),
                                    probe.get("must_not", []), slots)
                task_checks = (check_text(task_text, probe.get("task_must", []),
                                          probe.get("task_must_not", []), slots)
                               if probe.get("task") else None)
                passed = checks["pass"] and (task_checks is None or task_checks["pass"])
                replies = asked["reply"] + "\n" + done["reply"]
                row = {
                    "run": run_id, "case": script["id"], "arm": arm, "repeat": rep_n,
                    "cut": 100, "harness": args.harness, "session": sid, "ref": ref,
                    "kind": probe["id"], "question": probe["question"],
                    "task": probe.get("task"), "reply": asked["reply"],
                    "task_reply": done["reply"], "truth": probe.get("truth", ""),
                    "slots": slots, "checks": checks, "task_checks": task_checks,
                    "exact": int(passed),
                    "task_score": score_task(probe["id"], done, {}, 0, 0),
                    "hard_fail": bool(HARD_FAIL.search(replies)), "leak": [],
                    "first_word_s": asked["first_word_s"], "item_tools": asked["tools"],
                    "expected": {}, "judge": None,
                    "judge_prompt": (
                        "You grade whether an assistant remembers a conversation. Score its "
                        "answer 0-5 (5 = fully right and specific, 0 = wrong or invented). "
                        "Reply with JSON only: {\"score\": <0-5>, \"why\": \"<one line>\"}.\n\n"
                        "The person's turns:\n"
                        + "\n".join(f"- {t['text'] if isinstance(t, dict) else t}" for t in turns)
                        + f"\n\nThe right answer: {probe.get('truth', '')}\n\nQuestion: "
                        f"{probe['question']}\n\nAnswer to grade:\n{asked['reply'][:3000]}\n"
                    ),
                }
                if args.judge_cmd and not args.dry_run:
                    row["judge"] = run_judge(args.judge_cmd, row["judge_prompt"])
                say(f"    {probe['id']}: {'PASS' if passed else 'FAIL'}")
                rows.append(row)
            if args.dry_run:
                continue
            stored = stored_boilerplate(engine, sid)
            with (out / "results.jsonl").open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps({**row, "stored": stored}) + "\n")
    if args.dry_run:
        say("\ndry run: nothing was sent")
        return 0
    say(f"\nresults: {out}/results.jsonl")
    script_report(out)
    return report(out, args.trace_dir, args.engine_log)


def script_report(out: Path) -> None:
    """Per scripted probe and arm: pass or fail, and which regex decided it."""
    rows = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines()
            if ln.strip()]
    rows = [r for r in rows if r.get("checks")]
    if not rows:
        return
    say("\nscripted probes (must: all match; must_not: any match fails):")
    for row in rows:
        why = []
        for label, checks in (("reply", row["checks"]), ("task", row.get("task_checks"))):
            if not checks:
                continue
            why += [f"{label} missing /{m['re']}/" for m in checks["must"] if m["ok"] is False]
            why += [f"{label} unchecked /{m['re']}/" for m in checks["must"] if m["ok"] is None]
            why += [f"{label} stale /{h['re']}/ at {h['at']!r}" for h in checks["must_not"]
                    if h["hit"]]
        verdict = "PASS" if row["exact"] else "FAIL"
        say(f"  {row['arm']} r{row['repeat']} {row['kind']}: {verdict}"
            + (f"  ({'; '.join(why)})" if why else ""))


def mark_spot_checks(out: Path) -> None:
    path = out / "results.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    rng = random.Random(0)
    for row in rows:
        row["spot_check"] = row.get("exact") is None and rng.random() < 0.10
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# ── report ────────────────────────────────────────────────────────────────────


def score_of(row: dict) -> float | None:
    if row.get("exact") is not None:
        return 5.0 * row["exact"]
    return None if row.get("judge") is None else float(row["judge"])


def first_words_from_log(path: str | None) -> dict[str, list[float]]:
    """``{session: [first-word seconds, ...]}`` from the engine's turn-metrics lines."""
    found: dict[str, list[float]] = collections.defaultdict(list)
    if not path:
        return found
    pattern = re.compile(r"first word for (\S+) ([\d.]+)s after the message")
    for line in open(path, encoding="utf-8", errors="replace"):
        hit = pattern.search(re.sub(r"\x1b\[[0-9;]*m", "", line))
        if hit:
            found[hit.group(1)].append(float(hit.group(2)))
    return found


def trace_probe_turns(records: list[dict], ref: str, kinds: dict[str, tuple[str, str]]) -> dict:
    """Per probe question and task: tool_use names from the traced calls after its prompt.

    The arm's trace sessions are the ones whose prompts carry its ``ref``; the probes run last,
    so the newest prompt naming a probe is the one that counts.
    """
    keys = {r.get("session") for r in records
            if r.get("kind") == "prompt" and f"(ref {ref})" in json.dumps(r.get("data"))}
    mine = [r for r in records if r.get("session") in keys]
    out: dict[str, dict] = {}
    prompts = [i for i, r in enumerate(mine) if r.get("kind") == "prompt"]
    for n, start in enumerate(prompts):
        text = json.dumps(mine[start].get("data"))
        end = prompts[n + 1] if n + 1 < len(prompts) else len(mine)
        calls = [r for r in mine[start:end] if is_main_call(r)]
        tools = [b.get("name", "") for r in calls for b in (r.get("response") or {})
                 .get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        for kind, (q, t) in kinds.items():
            for label, needle in (("q", q), ("t", t)):
                if needle[:60] in text:
                    first = calls[0] if calls else {}
                    out[f"{kind}:{label}"] = {
                        "tools": len(tools),
                        "recall": sum(x.rsplit("__", 1)[-1] in RECALL_TOOLS for x in tools),
                        "first_call": usage_of(first) if first else None,
                    }
    return out


#: How a cold harness frames the replayed history (pi and Claude prompt builders).
HISTORY_OPEN = "Conversation so far:"
HISTORY_CLOSE = "Respond to the latest user message, using the conversation above as context."
_ENTRY = re.compile(r"^(user|assistant|tool): ", re.M)
#: A checkpoint's summary pair opens the history before the kept tail (outside its budget).
SUMMARY_OPENS = ("[This is an automatically generated summary", "[Context checkpoint inserted")
NOTICE_OPENS = ("[System", "[helper ")
#: The assembled block a new session starts from (ADR 0031 step 3), and its parts' headings.
BLOCK_OPENS = "[What came before, assembled when this session started."
BLOCK_PARTS = {"working_list": "Working list:\n", "turn_lines": "Earlier turns, one line each:\n",
               "words": "In their words (verbatim):\n", "recent": "Recent turns:\n"}
VIEW_START_LABEL = "omnigent.context.view_start"
_RECENT = re.compile(r"\n(?=They said: |You said: |You called |Notice: )")
#: The wake turn's reminder and the boilerplate older notices stored (ADR 0031 step 1): never
#: in a stored item, never in a replayed history.
BOILERPLATE = ("The notice below is the Result of work you started",
               "Use this Result to complete what the person", "After your check, show each file")
NOTICE_LIMIT = 600


def _texts(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(str(b.get("text", "")) for b in content or [] if isinstance(b, dict))


def history_shape(req: dict) -> dict | None:
    """The replayed history of a cold first call, as the model read it.

    ``rounds``: person's messages in the kept tail; ``tail_chars``/``tail_tok_est`` its size
    (chars / 4); ``summary_chars`` the checkpoint pair before it; ``notice_max`` the longest
    notice body after its outcome line; ``boilerplate`` how many entries carry the reminder or
    old boilerplate. The trailing user entries (the turn's own message, which pi lists inside
    the block with its prefix) are left out of the notice and boilerplate checks.

    With the assembled block (ADR 0031 step 3) the history opens with it: ``block_count`` how
    often it appears in the request (must be 1), ``block_chars``/``block_tok_est`` its size,
    ``parts`` each part's chars; its "They said" lines count as Rounds and its recent turns as
    tail, its notices ("Notice: ") are checked like replayed ones.
    """
    whole = json.dumps(req.get("messages") or [])
    text = next((t for m in req.get("messages") or [] if m.get("role") == "user"
                 for t in [_texts(m.get("content"))] if HISTORY_OPEN in t), "")
    if not text:
        return None
    block = text.split(HISTORY_OPEN, 1)[1].split(HISTORY_CLOSE, 1)[0]
    assembled = ""
    if BLOCK_OPENS in block:
        # The block is the history's first entry; the raw entries after it start at its end.
        head, _, rest = block.partition(BLOCK_OPENS)
        cut = _ENTRY.search(rest, rest.find("on the newest message.") + 1)
        assembled = BLOCK_OPENS + (rest[:cut.start()] if cut else rest)
        block = head.removesuffix("user: ") + (rest[cut.start():] if cut else "")
    marks = list(_ENTRY.finditer(block))
    entries = [(m.group(1), block[m.end():marks[i + 1].start() if i + 1 < len(marks)
                                  else len(block)].strip()) for i, m in enumerate(marks)]
    parts: dict[str, int] = {}
    recent = ""
    if assembled:
        starts = sorted((assembled.find(h), k, h) for k, h in BLOCK_PARTS.items()
                        if h in assembled)
        for i, (at, key, heading) in enumerate(starts):
            end = starts[i + 1][0] if i + 1 < len(starts) else len(assembled)
            body = assembled[at + len(heading):end]
            if i + 1 == len(starts):  # the last part runs up to the block's closing line
                body = body.rsplit("\n\n", 1)[0]
            parts[key] = len(body)
            if key == "recent":
                recent = body
    summary = 0
    if entries and entries[0][0] == "user" and entries[0][1].startswith(SUMMARY_OPENS):
        summary = sum(len(body) for _, body in entries[:2])
        entries = entries[2:]
    end = len(entries)
    while end and entries[end - 1][0] == "user":
        end -= 1
    older = entries[:end]
    notices = [body.partition("\n")[2] for role, body in older
               if role == "user" and body.startswith(NOTICE_OPENS)]
    notices += [line.partition("\n")[2] for line in re.split(_RECENT, recent)
                if line.startswith("Notice: ")]
    chars = sum(len(role) + 2 + len(body) for role, body in entries) + len(recent)
    return {
        "rounds": sum(role == "user" and not body.startswith(NOTICE_OPENS)
                      for role, body in older) + len(re.findall(r"^They said: ", recent, re.M)),
        "tail_chars": chars, "tail_tok_est": chars // 4, "summary_chars": summary,
        "notices": len(notices), "notice_max": max((len(n) for n in notices), default=0),
        "boilerplate": sum(any(b in body for b in BOILERPLATE) for _, body in older)
        + sum(b in recent for b in BOILERPLATE),
        "block_count": whole.count(json.dumps(BLOCK_OPENS)[1:-1]),
        "block_chars": len(assembled), "block_tok_est": len(assembled) // 4, "parts": parts,
    }


def stored_boilerplate(engine: Engine, sid: str) -> dict[str, int]:
    """Stored notices of *sid*, and how many stored items carry the reminder or boilerplate."""
    items = [] if engine.dry else engine.items(sid)
    texts = [item_text(i) for i in items if i.get("type") == "message"]
    labels = {} if engine.dry else (engine.call("GET", f"/v1/sessions/{sid}/labels") or {})
    view = (labels.get("labels") or {}).get(VIEW_START_LABEL)
    return {"notices": sum(t.lstrip().startswith(NOTICE_OPENS) for t in texts),
            "boilerplate": sum(any(b in t for b in BOILERPLATE) for t in texts),
            # The history load's view start (ADR 0031 step 3): set once a handoff assembled.
            "view_start": view if isinstance(view, str) else None}


def trace_cold_starts(records: list[dict], ref: str) -> list[dict]:
    """The arm's traced cold starts: a call with fewer messages than the one before it.

    For each: its input size and split, whether tools + system hashed the same as the call
    before (ADR 0032), the cache TTLs it asked for and the replayed history's shape
    (:func:`history_shape`).
    """
    keys = {r.get("session") for r in records
            if r.get("kind") == "prompt" and f"(ref {ref})" in json.dumps(r.get("data"))}
    calls = [r for r in records if r.get("session") in keys and is_main_call(r)
             and role_of(r["request"]) == "conversation"]
    found, prev = [], None
    for rec in calls:
        n = len(rec["request"].get("messages") or [])
        if prev is not None and n < len(prev["request"].get("messages") or []):
            u = usage_of(rec)
            found.append({**u, "same_prefix": prefix_hash(rec["request"]) == prefix_hash(
                prev["request"]), "ttl": sorted(set(ttls(rec["request"]))),
                "gap_s": round(rec["t"] - prev["t"], 1),
                "history": history_shape(rec["request"])})
        prev = rec
    return found


def report(out: Path, trace_dir: str | None, engine_log: str | None) -> int:
    rows = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines()
            if ln.strip()]
    records = load_traces(trace_dir) if trace_dir else []
    log_words = first_words_from_log(engine_log)
    if records:
        for ref in {r.get("ref") for r in rows if r.get("ref")}:
            per = trace_probe_turns(records, ref, PROBES)
            for row in rows:
                if row.get("ref") == ref:
                    row["trace_q"] = per.get(f"{row['kind']}:q")
                    row["trace_t"] = per.get(f"{row['kind']}:t")
    warm = {(r["case"], r["cut"], r["kind"], r["repeat"]): r for r in rows
            if r["arm"].startswith("warm@")}
    say(f"{len(rows)} probe rows; hard fails {sum(r['hard_fail'] for r in rows)}; "
        f"rows with leak words {sum(bool(r['leak']) for r in rows)}; "
        f"unjudged {sum(score_of(r) is None for r in rows)}")
    for arm in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == arm]
        scores = [s for s in (score_of(r) for r in mine) if s is not None]
        diffs, tool_diffs, recall_diffs, word_diffs = [], [], [], []
        from_traces = 0
        for r in mine:
            base = warm.get((r["case"], r["cut"], r["kind"], r["repeat"]))
            if base is None or base is r:
                continue
            a, b = score_of(r), score_of(base)
            if a is not None and b is not None:
                diffs.append(a - b)
            src_r = r.get("trace_t") or {}
            src_b = base.get("trace_t") or {}
            if src_r and src_b:  # from the model-call traces
                from_traces += 1
                tool_diffs.append(src_r["tools"] - src_b["tools"])
                recall_diffs.append(src_r["recall"] - src_b["recall"])
            else:  # from stored items
                ta, tb = r["task_score"], base["task_score"]
                tool_diffs.append(ta["tool_calls"] - tb["tool_calls"])
                recall_diffs.append(ta["recall_calls"] - tb["recall_calls"])
            fa = (log_words.get(r["session"]) or [None])[0] or r.get("first_word_s")
            fb = (log_words.get(base["session"]) or [None])[0] or base.get("first_word_s")
            if fa is not None and fb is not None:
                word_diffs.append(fa - fb)
        files = [r for r in mine if r["kind"] == "file" and r["exact"] is not None]
        say(f"\n{arm}: score mean {statistics.fmean(scores) if scores else float('nan'):.2f} "
            f"(n={len(scores)}); files exact {sum(r['exact'] for r in files)}/{len(files)}; "
            f"hard fails {sum(r['hard_fail'] for r in mine)}")
        if not arm.startswith("warm@"):
            src = f"{from_traces} of {len(tool_diffs)} pairs from traces, rest from items"
            say(f"  score vs warm (paired, 95% CI): {paired(diffs)}  target within -0.5")
            say(f"  tool calls per task vs warm ({src}): {paired(tool_diffs)}  target <= +1")
            say(f"  recall/session_history per task vs warm ({src}): {paired(recall_diffs)}")
            say(f"  first word vs warm, s: {paired(word_diffs)}  target <= +3")
        tasks = [r["task_score"] for r in mine]
        say(f"  tasks: asked back {sum(t['asked_back'] for t in tasks)}, redo "
            f"{sum(t['redo'] for t in tasks)}, right file "
            f"{sum(bool(t.get('right_file')) for t in tasks)}/"
            f"{sum('right_file' in t for t in tasks)}")
        if records:
            colds = [c for ref in sorted({r["ref"] for r in mine if r.get("ref")})
                     for c in trace_cold_starts(records, ref)]
            if colds:  # from the model-call traces: what each handoff's first call cost
                say(f"  traced cold starts: {len(colds)}; first-call input p50 "
                    f"{pct([c['total'] for c in colds], .5):,.0f} max "
                    f"{max(c['total'] for c in colds):,}; cache read on it "
                    f"{sum(c['read'] > 0 for c in colds)}/{len(colds)}; tools+system unchanged "
                    f"{sum(c['same_prefix'] for c in colds)}/{len(colds)}; TTLs "
                    f"{sorted({t for c in colds for t in c['ttl']})}")
                shapes = [c["history"] for c in colds if c.get("history")]
                for h in shapes:  # ADR 0031 step 1: the kept tail as the model read it
                    say(f"    history: {h['rounds']} Rounds, tail ~{h['tail_tok_est']:,} tok "
                        f"({h['tail_chars']:,} chars; budget 8k unless 1 Round), summary "
                        f"{h['summary_chars']:,} chars, {h['notices']} notices (longest body "
                        f"{h['notice_max']}, limit {NOTICE_LIMIT} + cut note), boilerplate "
                        f"{h['boilerplate']} (must be 0)")
                    if h.get("block_chars"):  # ADR 0031 step 3: the assembled block
                        say(f"      block x{h['block_count']} (must be 1), ~{h['block_tok_est']:,}"
                            f" tok; parts (chars) {h['parts']}")
        stored = [r["stored"] for r in mine if r.get("stored")]
        if stored:
            say(f"  stored notices {sum(s['notices'] for s in stored)}; stored items with the "
                f"reminder or boilerplate {sum(s['boilerplate'] for s in stored)} (must be 0)")
            views = [s.get("view_start") for s in stored]
            say(f"  view start label set on {sum(v is not None for v in views)}/{len(views)} rows"
                + ("" if arm.startswith("warm@") else " (must be all after a handoff)"))
    fails = [r for r in rows if r["hard_fail"]]
    for r in fails[:5]:
        say(f"  HARD FAIL {r['arm']} {r['kind']}: {r['reply'][:160]!r}")
    return 1 if fails else 0


def cmd_report(args: argparse.Namespace) -> int:
    script_report(Path(args.out_dir))
    return report(Path(args.out_dir), args.trace_dir, args.engine_log)


def cmd_judge(args: argparse.Namespace) -> int:
    path = Path(args.out_dir) / "results.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    order = list(range(len(rows)))
    random.Random(1).shuffle(order)  # blind to the arm: graded out of order, one at a time
    for i in order:
        if rows[i].get("exact") is None and rows[i].get("judge") is None:
            rows[i]["judge"] = run_judge(args.judge_cmd, rows[i]["judge_prompt"])
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return report(Path(args.out_dir), args.trace_dir, args.engine_log)


def cmd_cleanup(args: argparse.Namespace) -> int:
    state = json.loads((Path(args.out_dir) / "state.json").read_text())
    engine = Engine(args.engine, args.dry_run)
    for sid in state.get("sessions") or []:
        try:
            title = engine.title(sid) if not args.dry_run else PROBE_PREFIX
        except urllib.error.HTTPError as exc:
            say(f"  {sid}: {exc.code}, skipped")
            continue
        if not title.startswith(PROBE_PREFIX):
            say(f"  {sid}: title {title!r} is not a probe's, kept")
            continue
        engine.delete(sid)
        say(f"  {sid}: deleted")
    # Each session ran on its own host: keep its model-call traces, then drop container and home.
    for owner in state.get("hosts") or []:
        if not owner.startswith(PROBE_HOST_PREFIX):
            continue
        container = f"{args.host_prefix}-host-{owner}"
        volume = f"{args.host_prefix}-home-{owner}"
        if args.dry_run:
            say(f"  would copy traces from {container}, then remove it and {volume}")
            continue
        dest = Path(args.out_dir) / "llm-trace" / owner
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "cp", "-q", f"{container}:{TRACE_DIR_IN_HOST}", str(dest)],
                       check=False, capture_output=True)
        # The runner logs to files, not to docker logs: keep them beside the traces.
        logs = Path(args.out_dir) / "runner-logs" / owner
        logs.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "cp", "-q", f"{container}:{RUNNER_LOG_DIR_IN_HOST}", str(logs)],
                       check=False, capture_output=True)
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], check=False, capture_output=True)
        say(f"  {container}: traces kept in {dest}, runner logs in {logs}, container and home removed")
    return 0


# ── numbers ───────────────────────────────────────────────────────────────────


def load_traces(folder: str, since: float = 0) -> list[dict]:
    """Every trace record under *folder* (recursive), each copy of a record counted once."""
    seen: set[tuple] = set()
    recs: list[dict] = []
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.jsonl"), recursive=True)):
        pid = os.path.basename(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("kind") not in ("call", "prompt", "cli_init"):
                    continue
                key = (rec.get("session"), rec.get("t"), rec.get("kind"), rec.get("path"))
                if key in seen or (rec.get("t") or 0) < since:
                    continue
                seen.add(key)
                rec["_file"] = pid
                recs.append(rec)
    return sorted(recs, key=lambda r: r.get("t") or 0)


def is_main_call(rec: dict) -> bool:
    req = rec.get("request")
    return (rec.get("kind") == "call" and isinstance(req, dict) and bool(req.get("messages"))
            and bool(req.get("tools")))


def usage_of(rec: dict) -> dict[str, int]:
    usage = (rec.get("response") or {}).get("usage") or {}
    read = int(usage.get("cache_read_input_tokens") or 0)
    write = int(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation") or {}
    w1h = int(split.get("ephemeral_1h_input_tokens") or 0)
    plain = int(usage.get("input_tokens") or 0)
    return {"input": plain, "read": read, "write": write, "w1h": w1h, "w5m": write - w1h,
            "total": plain + read + write, "output": int(usage.get("output_tokens") or 0)}


def ttls(req: dict) -> list[str]:
    """The ``cache_control`` TTLs a request asks for (``5m`` when none is named)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if isinstance(cc, dict):
                found.append(str(cc.get("ttl") or "5m"))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk({k: req.get(k) for k in ("system", "tools", "messages")})
    return found


def prefix_hash(req: dict) -> str:
    # Claude Code's billing header changes per CLI session and does not affect the cache.
    blob = json.dumps([req.get("tools"), req.get("system")], sort_keys=True)
    blob = re.sub(r"x-anthropic-billing-header:[^\"]*", "", blob)
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


#: The role a traced call serves, from its system prompt (trace session keys are not session ids).
ROLE_MARKERS = (
    ("helper", "You are a worker Helper"),
    ("studio", "You are the chat of one file"),
    ("notes", "You keep the person's notes"),
    ("conversation", "You are superchat"),
)


def role_of(req: dict) -> str:
    system = req.get("system")
    text = system if isinstance(system, str) else "".join(
        b.get("text", "") for b in system or [] if isinstance(b, dict))
    return next((role for role, marker in ROLE_MARKERS if marker in text), "other")


GAP_BUCKETS = [(0, 60), (60, 240), (240, 300), (300, 360), (360, 600), (600, 1800),
               (1800, 3540), (3540, 3600), (3600, 4200), (4200, 10**9)]


def cmd_numbers(args: argparse.Namespace) -> int:
    recs = load_traces(args.trace_dir, args.since)
    by_session: dict[str, list[dict]] = collections.defaultdict(list)
    for rec in recs:
        by_session[str(rec.get("session"))].append(rec)
    harness = {
        s: "claude-sdk" if any(r.get("kind") == "cli_init" or (r.get("request") or {})
                               .get("metadata") for r in rs) else "pi"
        for s, rs in by_session.items()
    }
    role = {s: next((role_of(r["request"]) for r in rs if is_main_call(r)), "other")
            for s, rs in by_session.items()}
    calls = sum(is_main_call(r) for r in recs)
    side = sum(r.get("kind") == "call" for r in recs) - calls
    span = [datetime.datetime.fromtimestamp(r["t"], datetime.UTC) for r in recs if r.get("t")]
    say(f"{calls} main model calls (+{side} side calls without tools) in {len(by_session)} "
        f"sessions, {span[0]:%Y-%m-%d %H:%M} to {span[-1]:%Y-%m-%d %H:%M} UTC" if span
        else "no records")
    stats: dict[str, dict[str, Any]] = collections.defaultdict(lambda: collections.defaultdict(
        list))
    per_day: dict[tuple[str, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    for sid, rs in by_session.items():
        g = f"{harness[sid]}/{role[sid]}"
        st = stats[g]
        round_tokens = 0
        prev: dict | None = None
        prev_hash = None
        for rec in rs:
            if rec.get("kind") == "prompt":
                if round_tokens:
                    st["round"].append(round_tokens)
                round_tokens = 0
                continue
            if not is_main_call(rec):
                continue
            req, u = rec["request"], usage_of(rec)
            round_tokens += u["total"]
            for key in ("input", "read", "w5m", "w1h", "total"):
                st["sum_" + key].append(u[key])
            st["ttl"].extend(ttls(req))
            day = datetime.datetime.fromtimestamp(rec["t"], datetime.UTC).strftime("%Y-%m-%d")
            per_day[(g, day)][0] += u["read"]
            per_day[(g, day)][1] += u["total"]
            n_msgs = len(req.get("messages") or [])
            h = prefix_hash(req)
            cold = (prev is None or rec["_file"] != prev["_file"]
                    or n_msgs < len(prev["request"].get("messages") or []))
            if cold:
                st["cold"].append(u["total"])
                st["cold_read"].append(u["read"])
                if prev is not None:
                    gap = rec["t"] - prev["t"]
                    st["restart"].append((gap, u["read"] > 0, h == prev_hash))
            elif prev is not None:
                gap = rec["t"] - prev["t"]  # start to start: the cache TTL's own clock
                hit = u["read"] >= 0.5 * usage_of(prev)["total"]
                st["gap"].append((gap, hit))
            prev, prev_hash = rec, h
        if round_tokens:
            st["round"].append(round_tokens)
    for g in sorted(stats):
        st = stats[g]
        total = sum(st["sum_total"]) or 1
        read, w5, w1, plain = (sum(st["sum_read"]), sum(st["sum_w5m"]), sum(st["sum_w1h"]),
                               sum(st["sum_input"]))
        dollars = plain * PRICE["input"] + w5 * PRICE["w5m"] + w1 * PRICE["w1h"] \
            + read * PRICE["read"]
        write_d = w5 * PRICE["w5m"] + w1 * PRICE["w1h"]
        say(f"\n== {g}: {len(st['sum_total'])} calls, {total:,} input tokens")
        if st["round"]:
            say(f"  tokens per Round (input incl. cache): p50 {pct(st['round'], .5):,.0f}  "
                f"p90 {pct(st['round'], .9):,.0f}  max {max(st['round']):,}  "
                f"(n={len(st['round'])})")
        say(f"  cache-read share of input tokens: {read / total:.1%}   uncached "
            f"{plain / total:.1%}   written {(w5 + w1) / total:.1%} (1h: {w1:,} tokens)")
        say(f"  cache-write share of input $: {write_d / (dollars or 1):.1%}  "
            f"(reads {read * PRICE['read'] / (dollars or 1):.1%}, uncached "
            f"{plain / (dollars or 1):.1%})")
        say(f"  cache_control TTLs asked: {dict(collections.Counter(st['ttl']))}")
        if st["cold"]:
            say(f"  first call after a cold start: n={len(st['cold'])}  p50 "
                f"{pct(st['cold'], .5):,.0f}  max {max(st['cold']):,}  read>0 in "
                f"{sum(x > 0 for x in st['cold_read'])}/{len(st['cold'])}")
        if st["restart"]:
            same = sum(r[2] for r in st["restart"])
            hit = sum(r[1] for r in st["restart"])
            say(f"  across a restart: tools+system hash equal {same}/{len(st['restart'])}, "
                f"cache read on the first call {hit}/{len(st['restart'])}")
        if st["gap"]:
            cells = []
            for lo, hi in GAP_BUCKETS:
                inb = [hit for gap, hit in st["gap"] if lo <= gap < hi]
                if inb:
                    label = f"{lo // 60}-{hi // 60}m" if hi < 10**9 else f">{lo // 60}m"
                    cells.append(f"{label}: {sum(inb)}/{len(inb)}")
            hits = [gap for gap, hit in st["gap"] if hit]
            misses = [gap for gap, hit in st["gap"] if not hit]
            say("  warm calls, hits by gap since the previous request's start: "
                + "  ".join(cells))
            say(f"  longest gap with a hit: {f'{max(hits) / 60:.1f} min' if hits else 'none'}; "
                f"shortest gap with a miss: {f'{min(misses) / 60:.1f} min' if misses else 'none'}")
    if per_day:
        say("\ncache-read share per day:")
        for (g, day), (r, t) in sorted(per_day.items()):
            say(f"  {day} {g}: {r / (t or 1):.1%} of {t:,}")
    return 0


# ── cli ───────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default=ENGINE)
    ap.add_argument("--db-container", default=DB_CONTAINER)
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="replay, hand off, probe")
    run.add_argument("--harness", choices=list(BUNDLE), required=True)
    run.add_argument("--cases", type=int, default=1, help="source Conversations")
    run.add_argument("--source", help="one source session id instead of the largest")
    run.add_argument(
        "--script",
        help="a scripted case (JSON, e.g. engine/scripts/probe_cases/offsite.json) instead of "
        "replaying a stored Conversation; arms: warm, single@<turn>, chain (default warm,chain)",
    )
    run.add_argument("--max-turns", type=int, help="replay at most this many person turns")
    run.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a session label set on each probe session at create (repeatable)",
    )
    run.add_argument("--arms", help="default: warm@C and single@C for each cut, and chain "
                     "(with --script: warm,chain)")
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--probes", default=",".join(PROBES))
    run.add_argument("--skip", default=SKIP_DEFAULT, help="regex of source messages to skip")
    run.add_argument("--handoff-cmd", help="run this instead of a switch there and back")
    run.add_argument("--handoff-settle", type=float, default=8.0)
    run.add_argument("--turn-timeout", type=float, default=900.0)
    run.add_argument("--quiet", type=float, default=6.0, help="settled seconds ending a turn")
    run.add_argument("--judge-cmd")
    run.add_argument("--trace-dir")
    run.add_argument("--engine-log")
    run.add_argument("--out", default="probe-out")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(fn=cmd_run)

    rep = sub.add_parser("report", help="paired comparison of a finished run")
    rep.add_argument("out_dir")
    rep.add_argument("--trace-dir")
    rep.add_argument("--engine-log")
    rep.set_defaults(fn=cmd_report)

    jud = sub.add_parser("judge", help="score unjudged rows, then report")
    jud.add_argument("out_dir")
    jud.add_argument("--judge-cmd", required=True)
    jud.add_argument("--trace-dir")
    jud.add_argument("--engine-log")
    jud.set_defaults(fn=cmd_judge)

    cln = sub.add_parser("cleanup", help="delete the sessions this probe created")
    cln.add_argument("out_dir")
    cln.add_argument("--dry-run", action="store_true")
    cln.add_argument("--host-prefix", default=os.environ.get("OMNIGENT_HOST_NAME_PREFIX", "superchat-train"),
                     help="the engine's host container/volume name prefix (train: superchat-train)")
    cln.set_defaults(fn=cmd_cleanup)

    num = sub.add_parser("numbers", help="today's baseline from model-call traces")
    num.add_argument("trace_dir")
    num.add_argument("--since", type=float, default=0, help="epoch seconds")
    num.set_defaults(fn=cmd_numbers)

    args = ap.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
