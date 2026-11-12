"""Deliverable file types an artifact may hold, and their served content types."""

from __future__ import annotations

import posixpath

#: Largest file ``artifact_save`` accepts (bytes).
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024

#: ``kind`` (lower-case extension) -> served MIME type. This is the allow-list.
KIND_MIME: dict[str, str] = {
    "html": "text/html; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "txt": "text/plain; charset=utf-8",
    "json": "application/json",
}

_ALIASES = {"htm": "html", "markdown": "md", "jpeg": "jpg"}


def kind_for_name(name: str) -> str | None:
    """The artifact kind for a file name, or ``None`` when the type is not allowed."""
    ext = posixpath.splitext(name)[1].lstrip(".").lower()
    ext = _ALIASES.get(ext, ext)
    return ext if ext in KIND_MIME else None


#: The file-name suffix that marks an HTML artifact as a deck.
DECK_SUFFIX = ".deck.html"


def is_deck_name(name: str) -> bool:
    """True when an artifact file name marks a deck (``*.deck.html``, any case)."""
    return name.lower().endswith(DECK_SUFFIX) and len(name) > len(DECK_SUFFIX)
