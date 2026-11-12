"""The morning brief (ADR 0037): one scheduled page per person, one line in the Conversation.

Layout: ``page`` (what the content may hold and its fixed layout, pure), ``prompt`` (the run's
opening message, pure), ``sources`` (topics and connectors, injected by the composition root),
``service`` (the task, the run's brief, the latest page), ``routes`` (``/v1/me/brief/latest``),
``tools`` / ``handlers`` (``brief_save``), ``feature`` (registration).
"""

from __future__ import annotations

from omnigent.superchat.brief.feature import BRIEF_FEATURE

FEATURE = BRIEF_FEATURE

__all__ = ["BRIEF_FEATURE", "FEATURE"]
