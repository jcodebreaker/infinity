"""The parts of a file a live update reads and writes, by artifact kind.

A part is the smallest piece that shows one fact: an element of a page (its
``data-superchat-id``), a cell of a sheet (``B3``, ``Sheet2!B3``). A kind that can be kept
updated registers a :class:`PartEditor` here; pages do below, sheets from their own capability.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from omnigent.superchat.artifact_kinds import is_deck_name
from omnigent.superchat.artifacts.patches import PatchError, SetText, apply_patches, read_text


class PartError(ValueError):
    """The part cannot be read or written (no such part, or one that must not change)."""


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


@dataclass(frozen=True)
class PartEditor:
    """How one kind's parts are read and written."""

    #: ``(data, part id) -> text``, ``None`` when the part is gone; :class:`PartError` otherwise.
    read: Callable[[bytes, str], str | None]
    #: ``(data, part id, text) -> new data``; :class:`PartError` when it cannot be written.
    write: Callable[[bytes, str, str], bytes]
    #: What an update run reads to find the parts and their ids.
    describe: Callable[[bytes], str]
    #: One line for the run: what a part id is for this kind.
    ids: str
    #: Whether two texts say the same thing (a sheet's ``12.5`` and ``12.50`` do).
    same: Callable[[str, str], bool] = _same_text


#: ``artifact kind -> its editor``.
PART_EDITORS: dict[str, PartEditor] = {}


def part_editor(kind: str, name: str) -> PartEditor | None:
    """The editor of a file that can be kept updated, or ``None`` (a deck never is)."""
    return None if is_deck_name(name) else PART_EDITORS.get(kind)


def _read_html(data: bytes, ident: str) -> str | None:
    try:
        return read_text(data, ident)
    except PatchError as exc:
        raise PartError(str(exc)) from exc


def _write_html(data: bytes, ident: str, text: str) -> bytes:
    try:
        return apply_patches(data, [SetText(kind="set-text", id=ident, text=text)])[0]
    except PatchError as exc:
        raise PartError(str(exc)) from exc


PART_EDITORS["html"] = PartEditor(
    read=_read_html,
    write=_write_html,
    describe=lambda data: data.decode("utf-8", "replace"),
    ids="the data-superchat-id of the element holding the fact",
)
