"""Decks: a saved HTML deck (``*.deck.html``) exported to editable PowerPoint or vector PDF.

Layout: ``tools`` (the ``deck_export`` tool def), ``handlers`` (runner side: runs the host's
``superchat-deck-export`` helper and saves the result), ``routes`` (``/v1/decks/{id}/export`` for
the panel's Export buttons), ``feature`` (registration). Sits on top of artifacts.
"""

from __future__ import annotations

from omnigent.superchat.artifacts.patches import set_theme_restyler
from omnigent.superchat.decks.feature import DECKS_FEATURE
from omnigent.superchat.decks.kit import restyle_for_patch
from omnigent.superchat.decks.names import DECK_SUFFIX, deck_stem, is_deck_name

# A deck's theme is the decks capability's; artifacts' hand-edit patches call into it.
set_theme_restyler(restyle_for_patch)

FEATURE = DECKS_FEATURE

__all__ = ["DECKS_FEATURE", "DECK_SUFFIX", "FEATURE", "deck_stem", "is_deck_name"]
