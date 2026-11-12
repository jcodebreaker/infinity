"""The vocabulary every capability shares to flag an action the person is asked about first.

A capability that owns a risky tool (delete a file, publish an app) registers a classifier here;
``approvals.policy`` reads them. Kept out of ``approvals`` so a capability never imports another.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Risk:
    """One recognised risky action.

    :param category: One of :data:`CATEGORIES`.
    :param targets: Who or what it touches (recipients, host, path, tool); a standing rule must
        cover every one.
    :param summary: One plain sentence for the person.
    :param amount_usd: What it would cost, when the call says so.
    """

    category: str
    targets: tuple[str, ...]
    summary: str
    amount_usd: float | None = None


Classifier = Callable[[str, Mapping[str, Any]], Risk | None]

_extra_classifiers: list[Classifier] = []


def register_classifier(classifier: Classifier) -> None:
    """Add a recogniser for a tool family (an email or payments connector, say).

    :param classifier: ``(tool_name, arguments) -> Risk | None``; ``None`` means "not mine".
    """
    if classifier not in _extra_classifiers:
        _extra_classifiers.append(classifier)


TargetNamer = Callable[[str, "str | None"], "str | None"]

_target_namers: dict[str, TargetNamer] = {}


def register_target_namer(kind: str, namer: TargetNamer) -> None:
    """Name the targets a capability keys by id (``"artifact:<id>"``) for the person.

    :param kind: The prefix before ``:``, e.g. ``"artifact"``.
    :param namer: ``(id, owner) -> name``; ``None`` when it is gone or not the owner's.
    """
    _target_namers[kind] = namer


def target_name(target: str, owner: str | None) -> str:
    """What the person reads for *target*: its name when a capability knows it, else as is."""
    kind, _, ident = target.partition(":")
    namer = _target_namers.get(kind)
    if namer is None or not ident:
        return target
    name = namer(ident, owner)
    return f"“{name}”" if name else "something that no longer exists"
