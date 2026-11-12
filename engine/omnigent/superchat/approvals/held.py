"""Which stored approvals a runner still holds.

An approval waits inside the runner that raised it; the stored copy only mirrors it for the
Inbox. :func:`drop_unparked` forgets the copies no runner can answer any more.
"""

from __future__ import annotations

import asyncio
from typing import Any

from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import pending_elicitations
from omnigent.server.routes._sessions.helpers import _get_runner_client


async def drop_unparked(
    store: Any, rows: list[Any], runner_router: RunnerRouter | None
) -> list[Any]:
    """
    The waiting approvals a runner is still holding; the others read ``expired``.

    An approval waits inside the runner that raised it. When that runner restarts, or the turn
    holding it ends or is interrupted, nobody can answer it any more, but the stored copy
    stays. Each session's runner is asked which approvals it still holds; a session whose
    runner cannot be reached keeps its approvals, since they may well be alive.

    :param store: The approval store.
    :param rows: Pending approvals, as ``list_pending`` returns them.
    :param runner_router: Resolves a session's runner, or ``None`` when there is none.
    :returns: The rows still held by a runner (or not checkable).
    """
    held: dict[str, set[str] | None] = {}
    for session_id in {row.session_id for row in rows}:
        held[session_id] = None
        try:
            client = await _get_runner_client(session_id, runner_router)
            if client is None:
                continue
            response = await client.get(
                f"/v1/sessions/{session_id}/pending-approvals", timeout=5.0
            )
        except Exception:  # noqa: BLE001 - unreachable runner: keep its approvals
            continue
        if response.status_code == 200:
            ids = response.json().get("elicitation_ids")
            if isinstance(ids, list):
                held[session_id] = {str(i) for i in ids}
    kept = []
    for row in rows:
        ids = held.get(row.session_id)
        if ids is None or row.elicitation_id in ids:
            kept.append(row)
            continue
        pending_elicitations.resolve(row.session_id, row.elicitation_id)
        await asyncio.to_thread(store.decide, row.elicitation_id, "expired")
    return kept
