"""How a source plugs into the Lookout (ADR 0038, "Signals, pushed by sources").

A source is its own capability folder. It never calls a model and never imports the Lookout: the
composition root (``superchat/features.py``) hands it the entry point, a function of the
:class:`SignalSink` shape (:func:`omnigent.superchat.lookout.add_signal`), and, when it can
say that something went away, :class:`SignalRemover`
(:func:`omnigent.superchat.lookout.remove_signal`).

What a source pushes is a :class:`~omnigent.superchat.lookout.signals.Signal`, or a plain mapping
of its fields so the source imports nothing of the Lookout:

* ``kind``: the source's own word (``library_file``, ``promise``; later ``mail``, ``calendar``,
  ``web``). Code sets the urgency from it (``signals.NOW_KINDS``), never a model.
* ``dedupe_key``: folds repeats (``file:<group>``, ``promise:<topic>:<id>``, a mail thread).
* ``due_at``: now for news, the moment it matters for something time-based (a reminder).
* ``stale_after``: ``0`` takes the kind's default in the person's zone (the end of the next
  working day; two hours for a ``now`` kind).
* ``topic_key`` when the source knows it; else the Lookout's code check matches the signal to a
  topic from ``subject`` and ``detail``: ``excerpt``, ``refs`` (file names, thread ids),
  ``thread``, ``sender``, ``people``, ``headers`` (mail headers that mark bulk or auto mail),
  ``from_self``, ``at``.
* ``conversation_id`` when the source knows the person's Conversation.

A source that cannot push (a mailbox with no change feed) runs its own small model-free check at
its own pace through the work queue and pushes what it finds; before it pushes it may run the
Lookout's code check (:func:`omnigent.superchat.lookout.check_item`, handed in the same way).
Mail (``superchat/mail/``, ADR 0039) is such a source; calendar and web news come later.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnigent.superchat.lookout.signals import Signal


class SignalSink(Protocol):
    """The one entry point a source pushes through (blocking, no model run)."""

    def __call__(self, signal: Signal | Mapping[str, Any]) -> str | None: ...


class SignalRemover(Protocol):
    """Drop an owner's waiting signal by its dedupe key (something went away)."""

    def __call__(self, owner: str, dedupe_key: str) -> int: ...
