"""Library files as signals (ADR 0038): a live refresh is news, pushed on write, never polled."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from typing import Any

_logger = logging.getLogger(__name__)

KIND = "library_file"
#: Version origins that are news to the person: a live file refreshed from its sources.
NEWS_ORIGINS = frozenset({"update"})
#: Bytes of a text file read for its excerpt, and the excerpt's length.
MAX_FILE_BYTES = 64 * 1024
MAX_EXCERPT_CHARS = 1500
_TEXT_MIMES = ("text/", "application/json", "application/xml", "image/svg+xml")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)

#: The Lookout's entry point (``add_signal``, taking the signal's fields as a mapping), set by
#: the composition root so this source imports no other capability.
_sink: Callable[[dict[str, Any]], Any] | None = None
#: ``blob_key -> bytes``; defaults to the engine's artifact blob store.
_read_blob: Callable[[str], bytes] | None = None


def set_sink(
    sink: Callable[[dict[str, Any]], Any] | None, read_blob: Callable[[str], bytes] | None = None
) -> None:
    """Push signals with *sink*; read a version's bytes with *read_blob*."""
    global _sink, _read_blob
    _sink, _read_blob = sink, read_blob


def excerpt(text: str, *, markup: bool = False) -> str:
    """*text* as one excerpt: tags dropped for *markup*, whitespace collapsed, cut."""
    if markup:
        text = html.unescape(_TAGS.sub(" ", text))
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_EXCERPT_CHARS else flat[: MAX_EXCERPT_CHARS - 1] + "…"


def _text(saved: Any) -> str:
    mime = str(getattr(saved, "mime", "") or "")
    if not mime.startswith(_TEXT_MIMES):
        return ""
    try:
        if _read_blob is not None:
            data = _read_blob(saved.blob_key)
        else:
            from omnigent.runtime import get_artifact_store

            blobs = get_artifact_store()
            if blobs is None:
                return ""
            data = blobs.get(saved.blob_key)
    except Exception:
        _logger.warning("library signal: could not read file %s", saved.id, exc_info=True)
        return ""
    return excerpt(
        data[:MAX_FILE_BYTES].decode("utf-8", "replace"), markup="html" in mime or "xml" in mime
    )


def on_version_saved(saved: Any) -> None:
    """``ON_VERSION_SAVED`` hook: a live refresh becomes one signal; never fails the save."""
    if _sink is None or getattr(saved, "origin", None) not in NEWS_ORIGINS:
        return
    try:
        group = saved.group_id or saved.id
        _sink(
            {
                "owner": saved.user_id or "local",
                "kind": KIND,
                "subject": saved.title or saved.name,
                "dedupe_key": f"file:{group}",
                "due_at": int(saved.created_at),
                "stale_after": 0,
                "conversation_id": saved.parent_session_id,
                "detail": {
                    "source": "files",
                    "item_id": saved.id,
                    "thread": group,
                    "refs": [r for r in (saved.name, saved.source_path) if r],
                    "excerpt": _text(saved),
                    "version": saved.version,
                    "at": int(saved.created_at),
                },
            }
        )
    except Exception:
        _logger.exception("library signal for %s failed", getattr(saved, "id", "?"))
