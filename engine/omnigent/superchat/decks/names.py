"""The deck marker: a deck is an HTML artifact whose file name ends with ``.deck.html``.

The marker itself lives in ``artifact_kinds`` (artifacts edit decks too); re-exported here.
"""

from __future__ import annotations

from omnigent.superchat.artifact_kinds import DECK_SUFFIX, is_deck_name

__all__ = ["DECK_SUFFIX", "deck_stem", "is_deck_name"]


def deck_stem(name: str) -> str:
    """The deck's name without the ``.deck.html`` suffix (``q3.deck.html`` -> ``q3``)."""
    return name[: -len(DECK_SUFFIX)] if is_deck_name(name) else name
