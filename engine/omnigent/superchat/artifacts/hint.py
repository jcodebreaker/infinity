"""The handle lines naming this Conversation's files, put in front of a turn (ADR 0031, part 4).

Pure: the artifact store's newest versions and the artifact open beside the chat in, one handle
line per file out (title · kind · version · id, its state, its last change). No model call, no
tool and never file content. The engine renders the lines; the runner's turn prefix sends them
whole on a harness session's first turn and only the changed lines after that
(``prompt_prefix.files_block``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Collection, Sequence

import httpx

from omnigent.superchat.artifact_kinds import is_deck_name
from omnigent.superchat.artifacts.store import Artifact

_logger = logging.getLogger(__name__)

HEADER = "Files in this conversation (most recently changed first):"
#: The block a warm session gets instead when a file is new, has a new version or a new state.
DELTA_HEADER = "Files changed since the last message:"
OPEN_MARK = " — open beside the chat"
#: A file whose studio (ADR 0024) has a turn running: the person is changing it there now.
EDITING_MARK = " — being edited in its studio"
#: Most files the block names, and the characters its lines may take (about 0.8k tokens).
MAX_ARTIFACTS = 20
MAX_CHARS = 2800
#: The tool that lists every file, named when some are left out.
LIST_TOOL = "artifact_list"

_KIND_WORDS = {
    "html": "page",
    "md": "document",
    "docx": "document",
    "txt": "document",
    "pdf": "PDF",
    "xlsx": "sheet",
    "csv": "sheet",
    "pptx": "slides",
    "png": "image",
    "jpg": "image",
    "json": "data",
}


def kind_word(item: Artifact) -> str:
    """What the artifact is, in one word the person would use."""
    if is_deck_name(item.name):
        return "deck"
    return _KIND_WORDS.get(item.kind, item.kind)


_LAST_CHANGE = {
    "manual": "their hand edit",
    "restore": "they restored it",
    "update": "a live update",
}


def _line(item: Artifact, *, is_open: bool, editing: bool = False) -> str:
    label = " ".join((item.title or item.name).split())
    mark = (OPEN_MARK if is_open else "") + (EDITING_MARK if editing else "")
    when = time.strftime("%Y-%m-%d", time.gmtime(item.created_at))
    who = _LAST_CHANGE.get(item.origin or "")
    last = when
    if who:
        summary = " ".join((item.edit_summary or "").split())[:60]
        last = f"{when}, {who}" + (f": {summary}" if summary else "")
    return (
        f"- {label} · {kind_word(item)} · v{item.version} · {item.group_id or item.id}"
        f" · last change {last}{mark}"
    )


def artifact_hint(
    newest: Sequence[Artifact],
    open_artifact: Artifact | None = None,
    editing: Collection[str] = (),
) -> str | None:
    """The handle block, or ``None`` when the Conversation has no files.

    :param newest: The newest version of every file, most recently changed first (as the store
        lists them).
    :param open_artifact: The newest version of the file open beside the chat, if any. It is
        named first, so the cut never leaves it out.
    :param editing: Names of the files whose studio has a turn running (marked as being edited).
    :returns: The header, one line per file within :data:`MAX_CHARS`, then how many more there
        are and the tool that lists them.
    """
    items = list(newest)
    if open_artifact is not None:
        items = [open_artifact, *(a for a in items if a.name != open_artifact.name)]
    if not items:
        return None
    open_name = open_artifact.name if open_artifact is not None else None
    lines: list[str] = []
    size = len(HEADER)
    for item in items[:MAX_ARTIFACTS]:
        line = _line(item, is_open=item.name == open_name, editing=item.name in editing)
        if lines and size + 1 + len(line) > MAX_CHARS:
            break
        lines.append(line)
        size += 1 + len(line)
    more = len(items) - len(lines)
    tail = [f"{more} more: {LIST_TOOL}"] if more > 0 else []
    return "\n".join([HEADER, *lines, *tail])


async def fetch_artifact_hint(
    server_client: httpx.AsyncClient | None, conversation_id: str
) -> list[str]:
    """The handle block for this turn from the engine (``GET /v1/artifacts/hint``), or ``[]``.

    Read by the runner's turn prefix for a Conversation turn; a Helper, a Conversation with no
    files or any failure gives no block.
    """
    if server_client is None or not conversation_id:
        return []
    try:
        resp = await server_client.get(
            f"/v1/artifacts/hint?session_id={conversation_id}", timeout=10.0
        )
        hint = resp.json().get("hint") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        _logger.warning("artifact hint fetch failed", extra={"session_id": conversation_id})
        return []
    return [hint] if isinstance(hint, str) and hint else []
