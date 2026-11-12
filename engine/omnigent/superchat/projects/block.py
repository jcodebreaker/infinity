"""The per-turn Project list the runner adds to a Super Chat turn."""

from __future__ import annotations

from pathlib import Path

from omnigent.superchat.projects.card import (
    CARD_FILE,
    ProjectCard,
    is_slug,
    list_cards,
    projects_root,
    read_card,
)

#: Most Projects listed in a turn (the most recently modified); the open one is always listed.
MAX_LISTED = 15
_MAX_ALIASES = 6
_MAX_SUMMARY_CHARS = 160
#: Most top-level file names listed per Project (one directory listing, never recursive).
_MAX_FILES = 8
#: How the block starts; it ends with the ``]`` after its last Project line.
PROJECTS_OPEN = "[Projects (folders under ~/workspace/projects/)."


def open_project_slug(workspace: Path | None) -> str | None:
    """The slug of the Project ``workspace`` is in (or under), or ``None`` at the root."""
    if workspace is None:
        return None
    try:
        relative = workspace.resolve().relative_to(projects_root().resolve())
    except ValueError:
        return None
    slug = relative.parts[0] if relative.parts else None
    return slug if is_slug(slug) else None


def _top_level_files(card: ProjectCard) -> str:
    """Up to ``_MAX_FILES`` top-level names in the Project's folder (not the card), or ``""``.

    One directory listing, no recursion and no reads; hidden entries are skipped.
    """
    try:
        names = sorted(
            entry.name
            for entry in card.path.iterdir()
            if entry.name != CARD_FILE and not entry.name.startswith(".")
        )
    except OSError:
        return ""
    if not names:
        return ""
    shown = ", ".join(names[:_MAX_FILES])
    return f" Files: {shown}" + (
        f" (+{len(names) - _MAX_FILES} more)" if len(names) > _MAX_FILES else ""
    )


def _line(card: ProjectCard, *, is_open: bool) -> str:
    text = f"- {card.slug}: {card.name}"
    if card.aliases:
        text += f" (also: {', '.join(card.aliases[:_MAX_ALIASES])})"
    if card.summary:
        summary = card.summary
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[: _MAX_SUMMARY_CHARS - 1].rstrip() + "…"
        text += f". {summary}"
    text += _top_level_files(card)
    return text + (" [open now]" if is_open else "")


def projects_block(workspace: Path | None) -> str | None:
    """The Project list for one turn, or ``None`` when there are no Projects.

    :param workspace: The session's current working directory (marks the open Project).
    """
    open_slug = open_project_slug(workspace)
    cards = list_cards(MAX_LISTED)
    if open_slug is not None and all(c.slug != open_slug for c in cards):
        open_card = read_card(open_slug)
        if open_card is not None:
            cards.append(open_card)
    if not cards:
        return None
    lines = [_line(card, is_open=card.slug == open_slug) for card in cards]
    return (
        f"{PROJECTS_OPEN} Before touching any file for this "
        "message, decide which Project it is about: compare it with EVERY Project below (name, "
        "aliases, summary, files), not just the open one, which is only where you happen to be. "
        "If it belongs to another, call open_project with that slug first. If it could fit two "
        'or more (e.g. "the brief" when several Projects have one), ask which, naming the '
        "options, and edit nothing until the person answers.\n" + "\n".join(lines) + "]"
    )
