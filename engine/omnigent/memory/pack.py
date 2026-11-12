"""What background work knows about the person, from memory claims (ADR 0040).

The **memory pack** is the person's standing memory (the profile every turn carries: preferences,
standing instructions, working style, facts, commitments, projects, current focus) plus the people
and organisations they deal with and what each is to them (``person`` claims, which the profile
leaves out). The morning brief, the Lookout's Judge and its wake run read the same pack.

The **writing voice** is what memory says about how the person writes: their working style and
the preferences or instructions about mail, replies, tone or sign-offs. ``None`` when nothing is
known; the caller says so plainly instead of guessing.

Blocking (reads the claim store). Both read as ``None`` when memory is off or unreadable.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.memory.service import MemoryService

_logger = logging.getLogger(__name__)

#: Heading over the people the person deals with.
PEOPLE_HEADING = "People and organisations:"
#: The most ``person`` claims a pack carries (newest first).
MAX_PEOPLE = 40
#: The most characters the people section adds to the profile.
MAX_PEOPLE_CHARS = 4_000
#: The most voice lines.
MAX_VOICE = 12
#: Preference or instruction claims about how the person writes.
_VOICE_WORDS = re.compile(
    r"\b(writ\w*|e-?mails?|mails?|sign(?:s|ed|ing)?[- ]?off|signs?|signature|tone|greet\w*"
    r"|formal|informal|repl(?:y|ies)|voice|salutation|regards|cheers|phras\w*|wording"
    r"|concise|emoji\w*|bullets?|first name|address(?:es)? (?:people|them))\b",
    re.IGNORECASE,
)


def _service(service: MemoryService | None) -> MemoryService | None:
    if service is not None:
        return service
    from omnigent.runtime import get_memory_service

    return get_memory_service()


def _line(text: Any) -> str:
    """One line, no square brackets (a claim never forges a fence)."""
    return " ".join(str(text or "").split()).replace("[", "(").replace("]", ")")


def _active(claims: list[dict[str, Any]]) -> list[str]:
    return [_line(c.get("text")) for c in claims if c.get("status") == "active" and c.get("text")]


def memory_pack(owner: str, *, service: MemoryService | None = None) -> str | None:
    """The person's standing memory and the people they deal with, as text; ``None`` when
    nothing is known."""
    found = _service(service)
    if found is None:
        return None
    try:
        profile = found.profile(owner)
        people = _active(found.list_claims(owner, kinds=["person"]))[:MAX_PEOPLE]
    except Exception:
        _logger.exception("memory pack: reading the memory of %s failed", owner)
        return None
    lines: list[str] = []
    used = 0
    for text in people:
        line = f"- {text}"
        if used + len(line) + 1 > MAX_PEOPLE_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    parts = [p for p in (profile, "\n".join([PEOPLE_HEADING, *lines]) if lines else None) if p]
    return "\n".join(parts) or None


def writing_voice(owner: str, *, service: MemoryService | None = None) -> str | None:
    """How the person writes, from memory (working style, and preferences or standing
    instructions about mail, replies, tone or sign-offs); ``None`` when nothing is known."""
    found = _service(service)
    if found is None:
        return None
    try:
        style = _active(found.list_claims(owner, kinds=["working_style"]))
        said = _active(found.list_claims(owner, kinds=["preference", "instruction"]))
    except Exception:
        _logger.exception("memory pack: reading the writing voice of %s failed", owner)
        return None
    voice = [*style, *(t for t in said if _VOICE_WORDS.search(t))]
    lines = list(dict.fromkeys(f"- {t}" for t in voice))[:MAX_VOICE]
    return "\n".join(lines) or None
