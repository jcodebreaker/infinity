"""Read Project cards: the YAML front matter of ``projects/<slug>/PROJECT.md``.

Only the head of each card is read (name, aliases, summary, ...); the notes below it are the
Muse's own and never parsed. A card that cannot be read or parsed is skipped, never an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Re-exported: the slug rule lives with the session's open Project in ``entities``.
from omnigent.entities.session_project import (
    project_slug_from_workspace as project_slug_from_workspace,
)

CARD_FILE = "PROJECT.md"
#: How much of a card is read to find its front matter (it is a handful of lines).
_HEAD_BYTES = 4096
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: The Muse's workspace in a working directory: ``.../workspace`` and what follows it.
_WORKSPACE_DIR_RE = re.compile(r"/workspace(?:/(.*?))?/*$")
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def workspace_root() -> Path:
    """The Muse's workspace on the host (``~/workspace``)."""
    return Path.home() / "workspace"


def projects_root() -> Path:
    """Where Projects live (``~/workspace/projects``)."""
    return workspace_root() / "projects"


def is_slug(value: object) -> bool:
    """True for a folder name a Project may have (lowercase, digits, ``-`` and ``_``)."""
    return isinstance(value, str) and _SLUG_RE.fullmatch(value) is not None


def workspace_folder(workspace: str | None) -> str | None:
    """A working directory relative to the workspace (``"reports/q3"``); ``None`` at its root."""
    match = _WORKSPACE_DIR_RE.search(workspace or "")
    return (match.group(1) or None) if match else None


@dataclass(frozen=True)
class ProjectCard:
    """One Project's card.

    :param slug: Folder name under ``projects/``.
    :param name: Display name, e.g. ``"Q3 board deck"``.
    :param aliases: Other names the person uses for it, e.g. ``("the deck",)``.
    :param summary: One line on what the work is.
    :param people: People involved.
    :param goal: Slug of the linked Goal, if any.
    :param updated: ``YYYY-MM-DD`` the card says it was last updated, if any.
    """

    slug: str
    name: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    people: tuple[str, ...] = ()
    goal: str | None = None
    updated: str | None = None

    @property
    def path(self) -> Path:
        """The Project's folder."""
        return projects_root() / self.slug


def _strings(value: Any) -> tuple[str, ...]:
    """A front-matter list of strings (a lone string counts as one); anything else is dropped."""
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return tuple(s.strip() for s in items if isinstance(s, str) and s.strip())


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def parse_card(slug: str, text: str) -> ProjectCard | None:
    """The card in ``text`` (a PROJECT.md head), or ``None`` when its front matter is unusable.

    A card needs a ``name``. Every other field is optional and a malformed one is ignored.
    """
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    name = _one_line(data.get("name"))
    if not name:
        return None
    goal = data.get("goal")
    updated = data.get("updated")
    return ProjectCard(
        slug=slug,
        name=name,
        aliases=_strings(data.get("aliases")),
        summary=_one_line(data.get("summary")),
        people=_strings(data.get("people")),
        goal=goal.strip() if isinstance(goal, str) and goal.strip() else None,
        updated=updated.isoformat()
        if hasattr(updated, "isoformat")
        else (updated.strip() if isinstance(updated, str) and updated.strip() else None),
    )


def read_card(slug: str, root: Path | None = None) -> ProjectCard | None:
    """Read one Project's card from disk, or ``None`` (missing, unreadable or bad front matter)."""
    if not is_slug(slug):
        return None
    path = (root or projects_root()) / slug / CARD_FILE
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    return parse_card(slug, head)


def list_cards(limit: int, root: Path | None = None) -> list[ProjectCard]:
    """The ``limit`` most recently modified Projects' cards, newest first.

    One directory listing and one ``stat`` per card; only the newest ``limit`` cards are
    opened. Folders without a readable card are skipped.
    """
    base = root or projects_root()
    found: list[tuple[float, str]] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not is_slug(entry.name):
            continue
        try:
            found.append(((entry / CARD_FILE).stat().st_mtime, entry.name))
        except OSError:
            continue
    found.sort(reverse=True)
    cards: list[ProjectCard] = []
    for _, slug in found:
        card = read_card(slug, base)
        if card is not None:
            cards.append(card)
            if len(cards) == limit:
                break
    return cards
