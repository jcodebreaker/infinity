"""Memory Profile delivery for ``superside-chat`` (docs/context.md).

:func:`memory_profile_for` is the one function other slices call for a
user's Memory Profile, already wrapped in the per-turn delimiter block
(``omnigent.memory.service.render_profile_block``): S3 calls it for a new
Sub-agent's first message. The per-turn delivery seam for ordinary turns
lives in ``omnigent/runner/app.py`` (``_apply_memory_profile_block``), which
reaches this same logic over ``GET /v1/sessions/{id}/memory/profile``
(``omnigent/server/routes/session_memory.py``) since the runner process may
be out-of-process and has no direct runtime access.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.memory.service import MemoryService

_logger = logging.getLogger(__name__)


def memory_profile_for(user_id: str, *, service: MemoryService | None = None) -> str | None:
    """Render *user_id*'s Memory Profile, wrapped in the per-turn delimiter block.

    :param service: The :class:`MemoryService` to render from. Omit to use
        the server's runtime-global instance (``omnigent.runtime.get_memory_service``)
        — the right default for an in-process caller (e.g. S3 starting a new
        Sub-agent). A caller that already holds its own instance (e.g. a
        route closure built with one explicitly, as in tests) should pass
        it, so this never reads a stale or differently-configured global.
    :returns: ``None`` when long-term memory isn't configured, the profile
        is empty, or rendering it failed (logged here, never raised — a
        turn must proceed with no Memory Profile rather than fail outright
        over one) — callers must then add no block at all, never an empty
        one.
    """
    from omnigent.memory.service import render_profile_block

    if service is None:
        from omnigent.runtime import get_memory_service

        service = get_memory_service()
    if service is None:
        return None
    try:
        profile = service.profile(user_id)
        if not profile:
            return None
        return render_profile_block(profile)
    except Exception as exc:  # noqa: BLE001 — a profile failure must never fail the turn
        _logger.warning(
            "Memory Profile render failed; proceeding without it: %s",
            type(exc).__name__,
        )
        return None
