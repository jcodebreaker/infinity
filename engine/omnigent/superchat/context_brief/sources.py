"""Where the runner's context brief reads: the engine over HTTP, the host's index in process.

Artifacts come from ``GET /v1/artifacts`` (the owner's, newest version each); the Rounds still
working from ``GET /v1/sessions/{id}/tasks`` (its ``rounds``); the queries from the message in code
(none with recall off); earlier work and files from ``recall_search`` over the host's index.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omnigent.superchat.context_brief.brief import MAX_EARLIER, MAX_SOURCES
from omnigent.superchat.recall_hint.hint import SCORE_FLOOR, TurnPlan, search, turn_plan

_TIMEOUT = 1.5
#: Files looked at to find :data:`MAX_SOURCES` distinct ones (a file has several pages).
_FILE_HITS = 12
_ARTIFACTS_LISTED = 200


def _field(hit: Any, name: str) -> Any:
    return hit.get(name) if isinstance(hit, dict) else getattr(hit, name, None)


class RunnerSources:
    """The brief's reads from a runner (see :class:`~.brief.BriefSources`)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _get(self, path: str, key: str) -> list[dict[str, Any]]:
        resp = await self._client.get(path, timeout=_TIMEOUT)
        resp.raise_for_status()
        rows = resp.json().get(key)
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    async def artifacts(self) -> list[dict[str, Any]]:
        return await self._get(f"/v1/artifacts?limit={_ARTIFACTS_LISTED}", "artifacts")

    async def rounds(self, session_id: str) -> list[dict[str, Any]]:
        return await self._get(f"/v1/sessions/{session_id}/tasks", "rounds")

    async def plan(self, session_id: str, message: str) -> TurnPlan:
        return await turn_plan(self._client, session_id, message)

    async def recall(self, session_id: str, plan: TurnPlan) -> list[Any]:
        if not plan.queries:
            return []
        from omnigent.superchat.knowledge.handlers import current_epoch

        # The current epoch is in context (from the view start on), so it is left out.
        epoch = await current_epoch(self._client, session_id, plan.checkpoint or {})
        hits = await search(plan.queries, epoch)
        if not hits or (_field(hits[0], "score") or 0) < SCORE_FLOOR:
            return []
        return hits[: MAX_EARLIER + MAX_SOURCES]

    async def files(self, query: str) -> list[dict[str, Any]]:
        from omnigent.superchat.knowledge.recall import recall_search
        from omnigent.superchat.knowledge.runtime import get_runtime

        hits = await recall_search([query], sources={"files"}, limit=_FILE_HITS)
        db = get_runtime().indexer.db
        found: list[dict[str, Any]] = []
        for hit in hits:
            file_id = _field(hit, "file_id")
            if not file_id or any(f["file_id"] == file_id for f in found):
                continue
            row = await asyncio.to_thread(db.get_file, file_id)
            name = row.name if row is not None else _field(hit, "title")
            pages = row.page_count if row is not None else None
            found.append({"name": name, "pages": pages, "file_id": file_id})
            if len(found) >= MAX_SOURCES:
                break
        return found
