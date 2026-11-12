"""Hand edits -> the host workspace, plus the one-shot "you changed X" note.

Runs on the runner at the start of the Muse's turn (the runner sits next to the host's
files, so this works whenever a turn can run at all, including after the host was asleep
when the person saved). For every hand-edited version the server still lists as pending:

1. write the newest manual version's bytes to its ``source_path`` (idempotent: skipped when the
   file already holds exactly those bytes) so the Muse's next read sees the person's edits;
2. ack it (``POST /v1/artifacts/{id}/delivered``) so the note is shown once;
3. return one compact note per file for the turn prefix;
4. journal one line per delivered hand-edited version (``memory/journal``), so the edit outlives
   the note.

A failed write is not acked: the next turn retries, and the note says the file was not updated.
Generic over artifact kinds: sheets cell edits and the deck editor both save ``origin="manual"``
versions, and this one pass delivers them all (a live update's ``update`` version too).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import httpx

from omnigent.superchat.artifacts.handlers import workspace_roots

_logger = logging.getLogger(__name__)


def _safe_target(raw: str, roots: list[Path]) -> Path | None:
    """The real path for ``raw`` when it lies under a workspace root, else ``None``."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    try:
        real = path.parent.resolve() / path.name
        if real.is_symlink():
            real = real.resolve()
    except OSError:
        return None
    return real if any(real.is_relative_to(root) for root in roots) else None


def write_bytes_idempotent(target: Path, data: bytes) -> bool:
    """Atomically write ``data`` to ``target``; ``False`` when it already holds exactly that."""
    try:
        if target.is_file() and target.read_bytes() == data:
            return False
    except OSError:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".superchat-edit-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if target.exists():
            os.chmod(tmp, target.stat().st_mode & 0o777)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


#: A note :func:`_note` writes (one line), for readers that cut it from a stored message.
NOTE_LINE = re.compile(
    r"(The person edited .+ by hand|.+ was refreshed from its sources) \(v\d+ → v\d+\)"
    r".* Treat v\d+ as current\.( \(Your workspace copy could not be updated yet; .*\))?"
)


def _note(
    name: str, first: int, last: int, summaries: list[str], *, written: bool, by_hand: bool = True
) -> str:
    detail = " ".join("; ".join(s for s in summaries if s).split())
    who = "The person edited {} by hand" if by_hand else "{} was refreshed from its sources"
    text = f"{who.format(name)} (v{first - 1} → v{last})"
    text += f": {detail}." if detail else "."
    text += f" Treat v{last} as current."
    if not written:
        text += " (Your workspace copy could not be updated yet; do not overwrite their edits.)"
    return text


async def deliver_manual_edits(
    server_client: httpx.AsyncClient | None,
    conversation_id: str,
    *,
    roots: list[Path] | None = None,
    group_id: str | None = None,
    noted: set[str] | None = None,
) -> list[str]:
    """Write pending hand edits back into the workspace; return the one-shot notes.

    :param group_id: Only this file's edits: a file's studio (ADR 0024) writes back its own file
        at the start of each of its turns. It does not acknowledge them, so the Conversation's
        next turn still gets its own note; *noted* (the version ids this studio was already told
        about) keeps the studio's note to once per version.
    """
    if server_client is None or not conversation_id:
        return []
    url = f"/v1/artifacts/pending-edits?parent_session_id={conversation_id}"
    if group_id is not None:
        url += f"&group_id={group_id}"
    try:
        resp = await server_client.get(url, timeout=10.0)
        if resp.status_code != 200:
            return []
        edits: list[dict[str, Any]] = resp.json().get("edits", [])
    except (httpx.HTTPError, ValueError):
        return []
    if noted is not None:
        edits = [edit for edit in edits if edit["id"] not in noted]
    if not edits:
        return []
    allowed = roots if roots is not None else workspace_roots()
    groups: dict[str, list[dict[str, Any]]] = {}
    for edit in edits:
        groups.setdefault(edit["name"], []).append(edit)
    notes: list[str] = []
    delivered: list[dict[str, Any]] = []
    for name, items in groups.items():
        items.sort(key=lambda e: e["version"])
        head = items[-1]
        written = True
        if head.get("write_back") and head.get("source_path"):
            try:
                content = await server_client.get(
                    f"/v1/artifacts/{head['id']}/content", timeout=60.0
                )
                content.raise_for_status()
                target = _safe_target(head["source_path"], allowed)
                if target is None:
                    written = False
                else:
                    await asyncio.to_thread(write_bytes_idempotent, target, content.content)
            except (httpx.HTTPError, OSError):
                _logger.warning(
                    "write-back of %s failed",
                    name,
                    exc_info=True,
                    extra={"session_id": conversation_id},
                )
                written = False
        elif head.get("write_back"):
            written = False  # no known workspace path (saved before paths were recorded)
        notes.append(
            _note(
                name,
                items[0]["version"],
                head["version"],
                [str(i.get("edit_summary") or "") for i in items],
                written=written,
                by_hand=any(i.get("origin") != "update" for i in items),
            )
        )
        if noted is not None:  # a studio: told once, acknowledged by the Conversation
            if written or not head.get("source_path"):
                noted.update(item["id"] for item in items)
            continue
        if written or not head.get("source_path"):
            delivered.extend(items)
            for item in items:
                try:
                    await server_client.post(f"/v1/artifacts/{item['id']}/delivered", timeout=10.0)
                except httpx.HTTPError:
                    _logger.warning("could not ack edit %s", item["id"], exc_info=True)
    if delivered:
        from omnigent.runner.memory.sync import record_hand_edits

        await record_hand_edits(server_client, conversation_id, delivered)
    return notes
