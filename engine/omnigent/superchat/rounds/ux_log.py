"""Opt-in log of each Round view the engine sends the web (dev only, off by default).

With ``OMNIGENT_UX_LOG_DIR`` set, :func:`shown` appends one JSONL line per Round view the
Announcer, the Round list or the Round view sends, when it differs from the last line written for
that Round: the time, where it was sent from, the conversation, the Round, its status,
``settled_at`` and ``delivered_at``, its Helpers (id, title, status), its file names, its
checklist (subject, status), the tool running now and the last :data:`_WORDS_TAIL` characters of
its live words. No other message text is written. Read it with ``engine/scripts/ux_log.py``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

UX_LOG_DIR_ENV = "OMNIGENT_UX_LOG_DIR"
#: Characters of the live words a line keeps, from the end.
_WORDS_TAIL = 200

_lock = threading.Lock()
_last: dict[str, dict[str, Any]] = {}  # round -> the last line written for it, without its time


def _log_dir() -> Path | None:
    raw = os.environ.get(UX_LOG_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def shown(source: str, conversation_id: str, round_id: str, view: Mapping[str, Any]) -> None:
    """Record that *source* sent the web *view* of *round_id*; no-op when the log is off."""
    folder = _log_dir()
    if folder is None:
        return
    now = view.get("now")
    line: dict[str, Any] = {
        "conversation_id": conversation_id,
        "round_id": round_id,
        "status": view.get("status"),
        "settled_at": view.get("settled_at"),
        "delivered_at": view.get("delivered_at"),
        "helpers": [
            {"id": helper_id, "title": h.get("title"), "status": h.get("status")}
            for helper_id, h in (view.get("helpers") or {}).items()
        ],
        "files": [f.get("name") for f in view.get("files") or []],
        "checklist": [
            {"subject": c.get("subject"), "status": c.get("status")}
            for c in view.get("checklist") or []
        ],
        "now": (
            {k: now.get(k) for k in ("actor", "helper_id", "name", "digest")}
            if isinstance(now, Mapping)
            else None
        ),
        "live_words": str(view.get("live_words") or "")[-_WORDS_TAIL:],
    }
    with _lock:
        if _last.get(round_id) == line:
            return
        _last[round_id] = line
        record = {"t": time.time(), "source": source, **line}
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"ux-{os.getpid()}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass  # a log line never breaks a read
