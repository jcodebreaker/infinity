"""What a brief reads that other capabilities own, injected by the composition root.

The brief never imports the capability that owns a source: the composition root
(``superchat/features.py``) hands each one in. A source that is not set reads as empty, so the
brief runs on what exists ("(no topics yet)", no calendar or mail).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

#: ``owner -> open topics`` (ADR 0035 ``list_watched_topics``), or ``None`` until topics exist.
_topics: Callable[[str], Sequence[Any]] | None = None
#: ``owner -> names of connected mail or calendar sources`` (none exist yet).
_connectors: Callable[[str], Sequence[str]] | None = None
#: ``task -> lines``: the signals the person's Lookout claims into this brief run (ADR 0038).
_signals: Callable[[Any], str | None] | None = None


def set_topics_source(source: Callable[[str], Sequence[Any]] | None) -> None:
    """Read a person's open topics with ``source(owner)`` (``None`` unsets it)."""
    global _topics
    _topics = source


def set_connectors_source(source: Callable[[str], Sequence[str]] | None) -> None:
    """Name a person's connected mail and calendar sources with ``source(owner)``."""
    global _connectors
    _connectors = source


def open_topics(owner: str) -> Sequence[Any] | None:
    """The owner's open topics, or ``None`` when no topics source is set."""
    return None if _topics is None else list(_topics(owner))


def connected_sources(owner: str) -> list[str]:
    """Names of the owner's connected mail and calendar sources (empty without any)."""
    return [] if _connectors is None else [str(name) for name in _connectors(owner) if name]


def set_signals_source(source: Callable[[Any], str | None] | None) -> None:
    """Claim the Lookout's waiting signals into a brief run with ``source(task)``."""
    global _signals
    _signals = source


def waiting_signals(task: Any) -> str | None:
    """The waiting signals claimed into this run, one line each, or ``None``."""
    return None if _signals is None else _signals(task)
