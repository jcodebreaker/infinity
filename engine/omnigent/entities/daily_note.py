"""Daily note entity and its merge rules: one note per owner per local day.

The person's edits win. A section the person edited is never rewritten by a background writer:
the writer's new lines are appended only when the section does not already say them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WRITER_KEYS = ("talked_about", "decisions", "promised", "open_loops")
SECTION_KEYS = (*WRITER_KEYS, "reflection")
SECTION_TITLES = {
    "talked_about": "What we talked about",
    "decisions": "Decisions",
    "promised": "Promised",
    "open_loops": "Open loops",
    "reflection": "Reflection",
}
MAX_SECTION_CHARS = 6000


@dataclass
class DailyNote:
    """
    One day's note for an owner.

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param note_date: Local calendar day, ``YYYY-MM-DD``.
    :param sections: ``{section_key: text}``; every key in :data:`SECTION_KEYS` is present.
    :param edited_sections: Section keys the person edited (writers only append to these).
    :param edited_by_person: ``True`` once the person edited any section.
    :param parent_session_id: The Super Chat the day's passes ran for, or ``None``.
    :param quiet_passes: Quiet-moment passes started on this day.
    :param finalized_at: Epoch seconds the nightly finalizing pass succeeded, or ``None``.
    :param dreamed_at: Epoch seconds the Dreaming pass succeeded, or ``None``.
    :param people_at: Epoch seconds the People pass succeeded, or ``None``.
    :param claimed_at: Epoch seconds a pass took the lease (single-flight), or ``None``.
    :param created_at: Epoch seconds.
    :param updated_at: Epoch seconds of the last write.
    """

    owner: str
    note_date: str
    sections: dict[str, str] = field(default_factory=dict)
    edited_sections: list[str] = field(default_factory=list)
    edited_by_person: bool = False
    parent_session_id: str | None = None
    quiet_passes: int = 0
    finalized_at: int | None = None
    dreamed_at: int | None = None
    people_at: int | None = None
    claimed_at: int | None = None
    created_at: int = 0
    updated_at: int = 0


def empty_sections() -> dict[str, str]:
    """A section map with every key empty."""
    return dict.fromkeys(SECTION_KEYS, "")


def _clean(text: object) -> str:
    return text.strip()[:MAX_SECTION_CHARS] if isinstance(text, str) else ""


def _norm(line: str) -> str:
    return " ".join(line.lstrip("-*• ").lower().split())


def merge_writer(
    current: dict[str, str], edited: list[str], incoming: dict[str, object]
) -> dict[str, str]:
    """Apply a background writer's sections: replace untouched sections, append to edited ones.

    :param current: The stored sections.
    :param edited: Section keys the person edited.
    :param incoming: ``{section_key: text}`` from the writer; unknown keys are ignored.
    :returns: The new section map.
    """
    merged = {**empty_sections(), **current}
    for key in SECTION_KEYS:
        if key not in incoming:
            continue
        text = _clean(incoming[key])
        if key not in edited:
            merged[key] = text
            continue
        have = {_norm(line) for line in merged[key].splitlines() if line.strip()}
        fresh = [
            line.rstrip() for line in text.splitlines() if line.strip() and _norm(line) not in have
        ]
        if fresh:
            merged[key] = (merged[key].rstrip() + "\n" + "\n".join(fresh)).strip()[
                :MAX_SECTION_CHARS
            ]
    return merged


def apply_person_edit(
    current: dict[str, str], edited: list[str], incoming: dict[str, object]
) -> tuple[dict[str, str], list[str]]:
    """Apply the person's edit: given sections replace stored text and become protected.

    :returns: The new section map and the new list of edited section keys.
    """
    merged = {**empty_sections(), **current}
    protected = [k for k in SECTION_KEYS if k in edited]
    for key in SECTION_KEYS:
        if key not in incoming:
            continue
        text = _clean(incoming[key])
        if text != merged[key].strip():
            merged[key] = text
            if key not in protected:
                protected.append(key)
    return merged, protected
