"""``GET /v1/me/system-jobs``: the engine's own background jobs for the caller."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from omnigent.entities import MemoryUpkeepRun
from omnigent.superchat.system_jobs.routes import create_system_jobs_router


class _Store:
    def list_runs(self, user_id: str, *, limit: int = 20) -> list[MemoryUpkeepRun]:
        run = MemoryUpkeepRun(
            run_id="r",
            user_id=user_id,
            window_since=0,
            window_until=10,
            state="succeeded",
            disposition="ok",
            counts={},
            started_at=100,
            finished_at=160,
        )
        return [run]


def _client(app: FastAPI) -> httpx.AsyncClient:
    app.include_router(create_system_jobs_router(app), prefix="/v1")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_jobs_report_last_and_next_run() -> None:
    app = FastAPI()
    app.state.memory_upkeep_coordinator = SimpleNamespace(upkeep_store=_Store())
    app.state.memory_upkeep_scheduler = SimpleNamespace(sweep_interval_s=3600, last_sweep_at=1000)
    async with _client(app) as http:
        jobs = (await http.get("/v1/me/system-jobs")).json()["jobs"]
    by_id = {j["id"]: j for j in jobs}
    upkeep = by_id["memory_upkeep"]
    assert upkeep["name"] == "Memory upkeep"
    assert upkeep["last_run_at"] == 160
    assert upkeep["next_run_at"] == 4600
    assert upkeep["enabled"] is True
    assert upkeep["schedule"].startswith("Every hour")


async def test_jobs_listed_disabled_with_unknown_times_when_nothing_runs() -> None:
    app = FastAPI()
    async with _client(app) as http:
        jobs = (await http.get("/v1/me/system-jobs")).json()["jobs"]
    assert [j["id"] for j in jobs] == [
        "memory_upkeep",
        "daily_note",
        "dreaming",
        "people",
    ]
    for job in jobs:
        assert job["enabled"] is False
        assert job["last_run_at"] is None
        assert job["next_run_at"] is None


async def test_note_jobs_report_last_runs_from_the_notes() -> None:
    from omnigent.entities.daily_note import DailyNote

    note = DailyNote(
        owner="local",
        note_date="2026-10-09",
        quiet_passes=2,
        finalized_at=900,
        dreamed_at=950,
        people_at=960,
        updated_at=800,
    )

    class _Notes:
        @staticmethod
        def owner_key(_user_id: str | None) -> str:
            return "local"

        def list_range(self, owner: str, **_kw: object) -> list[DailyNote]:
            return [note] if owner == "local" else []

    app = FastAPI()
    app.state.daily_note_store = _Notes()
    app.state.quiet_moment = object()
    async with _client(app) as http:
        jobs = (await http.get("/v1/me/system-jobs")).json()["jobs"]
    by_id = {j["id"]: j for j in jobs}
    assert by_id["daily_note"]["last_run_at"] == 900
    assert "20 minutes" in by_id["daily_note"]["schedule"]
    assert by_id["dreaming"]["last_run_at"] == 950
    assert by_id["people"]["last_run_at"] == 960
    assert all(by_id[k]["enabled"] for k in ("daily_note", "dreaming", "people"))
