"""Daily notes: the owner's local day, and rendering a note for a Helper's brief."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.entities.daily_note import SECTION_KEYS, SECTION_TITLES, DailyNote


def local_zone(timezone: str | None) -> ZoneInfo:
    """The IANA zone, falling back to UTC when empty or unknown."""
    try:
        return ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_date(timezone: str | None, now: datetime | None = None) -> str:
    """The owner's current local calendar day, ``YYYY-MM-DD``."""
    return (now or datetime.now(UTC)).astimezone(local_zone(timezone)).date().isoformat()


def render_note(note: DailyNote | None) -> str:
    """The note as plain text for a brief (every section, empty ones marked)."""
    parts = []
    for key in SECTION_KEYS:
        text = (note.sections.get(key, "") if note else "").strip()
        parts.append(f"## {SECTION_TITLES[key]}\n{text or '(nothing yet)'}")
    return "\n\n".join(parts)
