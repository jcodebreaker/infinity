"""``GET /v1/me/system-jobs``: the engine's own recurring background jobs, for the caller.

Read-only. Each job reports what it does in plain words, how often it runs, when it last ran for
this person, what that run did (``last_outcome``, plain words) and when it runs next, when the
engine knows. Unknown values are ``null``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, FastAPI, Request

from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user


def _every(seconds: float) -> str:
    """Plain-words cadence, e.g. ``"Every hour"``."""
    if seconds % 3600 == 0:
        hours = int(seconds // 3600)
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    if seconds % 60 == 0:
        minutes = int(seconds // 60)
        return "Every minute" if minutes == 1 else f"Every {minutes} minutes"
    return f"Every {int(seconds)} seconds"


def collect_jobs(app: FastAPI, owner: str) -> list[dict[str, Any]]:
    """Build the caller's job rows from what the server is running."""
    jobs: list[dict[str, Any]] = []

    from omnigent.server.memory_upkeep import upkeep_status

    coordinator = getattr(app.state, "memory_upkeep_coordinator", None)
    scheduler = getattr(app.state, "memory_upkeep_scheduler", None)
    interval = float(scheduler.sweep_interval_s) if scheduler is not None else 3600.0
    status = upkeep_status(app.state, owner)
    last = status["runs"][0] if status and status["runs"] else None
    jobs.append(
        {
            "id": "memory_upkeep",
            "name": "Memory upkeep",
            "description": (
                "Reads your new messages and keeps lasting facts about you: who you are, what "
                "you work on, and how you like things done, each with your words as evidence."
            ),
            "schedule": _every(interval) + ", and after a conversation is condensed",
            "last_run_at": last["at"] if last else None,
            "last_outcome": last["outcome"] if last else None,
            "last_failed": bool(last and last["state"] == "failed"),
            "next_run_at": status["next_run_at"] if status else None,
            "enabled": coordinator is not None and scheduler is not None,
        }
    )

    jobs.extend(_note_jobs(app, owner))
    return jobs


def _note_jobs(app: FastAPI, owner: str) -> list[dict[str, Any]]:
    """The quiet-moment worker's three passes: Daily note, Dreaming and People."""
    from omnigent.superchat.daily_notes import quiet_moment as qm

    worker = getattr(app.state, "quiet_moment", None)
    store = getattr(app.state, "daily_note_store", None)
    notes = []
    if store is not None:
        user_id = None if owner == RESERVED_USER_LOCAL else owner
        notes = store.list_range(store.owner_key(user_id), date_from=None, date_to=None, limit=14)
    enabled = worker is not None

    def latest(column: str) -> int | None:
        stamps = [getattr(n, column) for n in notes if getattr(n, column)]
        return max(stamps) if stamps else None

    passes = [n.updated_at for n in notes if n.quiet_passes > 0]
    last_note = max([*passes, *(n.finalized_at for n in notes if n.finalized_at)], default=None)
    idle_minutes = int(qm.idle_seconds() // 60)
    evening = f"{qm.FINALIZE_HOUR}:00"
    nightly = f"about {qm.NIGHTLY_DELAY_SECONDS // 60} minutes after the day's final note"
    return [
        {
            "id": "daily_note",
            "name": "Daily note",
            "description": (
                "Keeps a short note of each day: what you talked about, what was decided and "
                "promised, and what is still open. It also saves lasting facts about you to "
                "memory."
            ),
            "schedule": (
                f"After a chat has been quiet {idle_minutes} minutes (up to "
                f"{qm.MAX_PASSES_PER_DAY} times a day), and a final pass at {evening}"
            ),
            "last_run_at": last_note,
            "last_outcome": None,
            "last_failed": False,
            "next_run_at": None,
            "enabled": enabled,
        },
        {
            "id": "dreaming",
            "name": "Dreaming",
            "description": (
                "Reflects on how the day went (what worked, what did not) and adjusts how it "
                "works with you from what you said."
            ),
            "schedule": f"Nightly, {nightly}",
            "last_run_at": latest("dreamed_at"),
            "last_outcome": None,
            "last_failed": False,
            "next_run_at": None,
            "enabled": enabled,
        },
        {
            "id": "people",
            "name": "People",
            "description": (
                "Keeps one short page in memory for each person or group you mention, with the "
                "key facts and when you said them."
            ),
            "schedule": f"Nightly, {nightly}",
            "last_run_at": latest("people_at"),
            "last_outcome": None,
            "last_failed": False,
            "next_run_at": None,
            "enabled": enabled,
        },
    ]


def create_system_jobs_router(
    app: FastAPI, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the router (mount with ``prefix="/v1"``)."""
    router = APIRouter()

    @router.get("/me/system-jobs")
    async def list_system_jobs(request: Request) -> dict[str, Any]:
        """The engine's recurring background jobs, with this person's last and next run."""
        user_id = require_user(request, auth_provider)
        owner = user_id or RESERVED_USER_LOCAL
        jobs = await asyncio.to_thread(collect_jobs, app, owner)
        return {"jobs": jobs}

    return router
