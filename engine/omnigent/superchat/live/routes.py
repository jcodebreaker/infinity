"""``/v1/artifacts/{id}/live``: settings, undo, accept; ``/context`` + ``/save`` for the Helper.

The artifact id is any version of the page. Owner-scoped like the artifact routes (another
owner's rows 404); in single-user mode the rows are unowned and so is the lookup.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.scheduled.rrule import RRuleValidationError, validate_rrule
from omnigent.superchat.artifacts import (
    Artifact,
    artifact_to_response,
    check_id,
    load_artifact,
    request_owner,
)
from omnigent.superchat.artifacts.parts import part_editor
from omnigent.superchat.live.service import LiveService

#: A web page URL or a host file path a page's facts come from.
Sources = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=2048)]], Field(max_length=50)
]


class LiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rrule: str
    sources: Sources | None = None
    tell: Literal["notable", "every", "never"] | None = None
    follow_publish: bool | None = None
    on_change: bool | None = None
    note: str | None = Field(default=None, max_length=1000)


class ChangedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: Sources


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    after: str
    source: str | None = None
    notable: bool = False


class SaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)
    changes: list[Change] = Field(default_factory=list, max_length=200)
    facts: dict[str, Any] = Field(default_factory=dict)
    sources: Sources | None = None


def create_live_router(
    service: LiveService,
    *,
    auth_provider: AuthProvider | None = None,
    host_store: Callable[[], Any] | None = None,
) -> APIRouter:
    """Build the live artifacts router, mounted with ``prefix="/v1"``.

    Settings (``GET``/``PUT /artifacts/{id}/live``): ``rrule`` (schedule), ``sources``, ``tell``,
    ``follow_publish`` (default true; an unusual update is ``held`` until
    ``POST .../live/publish``), ``note`` (what to update) and ``on_change`` (default true: also
    update ~2 minutes after a source file changes in the host). The host's file watcher
    uses ``GET /live/watch`` and ``POST /live/changed`` with its launch token
    (``Authorization: Bearer <host_id>:<token>``).
    """
    router = APIRouter()

    async def _host_owner(request: Request) -> str | None:
        """The owner of the host whose launch token the request carries (else 401)."""
        auth = request.headers.get("authorization", "")
        host_id, _, token = (auth[7:] if auth.lower().startswith("bearer ") else "").partition(":")
        store = host_store() if host_store else None
        host = (
            await asyncio.to_thread(store.resolve_launch_token, host_id, token.strip())
            if host_id and token and store
            else None
        )
        if host is None:
            raise OmnigentError("unauthenticated", code=ErrorCode.UNAUTHORIZED)
        service.loop = asyncio.get_running_loop()
        # Like request_owner: the single local person's rows are stored unowned.
        return None if host.user_id in (None, RESERVED_USER_LOCAL) else host.user_id

    def _held(item: Artifact, held: dict[str, Any] | None) -> dict[str, Any] | None:
        """The held update, unless the person has since published it (or a newer version)."""
        pub = service.artifacts.publication_for(item) if held else None
        return held if held and pub and pub.published_version < held["version"] else None

    def _describe(item: Artifact) -> dict[str, Any]:
        live = service.live.get(item.parent_session_id, item.name)
        if live is None:
            return {"live": None}
        task = service.tasks.get(live.task_id)
        return {
            "live": {
                "task_id": live.task_id,
                "rrule": task.rrule if task else None,
                "timezone": task.timezone if task else None,
                "schedule": task.state if task else "missing",
                "last_run_at": task.last_run_at if task else None,
                "sources": live.sources,
                "tell": live.tell,
                "follow_publish": live.follow_publish,
                "on_change": live.state["on_change"],
                "note": live.state["note"],
                "held": _held(item, live.state["held"]),
                **{
                    k: live.state[k]
                    for k in (
                        "checked_at",
                        "open_issue",
                        "last_update",
                        "failures",
                        "last_failure",
                    )
                },
            }
        }

    async def _page(request: Request, artifact_id: str) -> Artifact:
        service.loop = asyncio.get_running_loop()
        return await load_artifact(
            service.artifacts, request_owner(request, auth_provider), artifact_id
        )

    @router.get("/live/watch")
    async def live_watch(
        request: Request, mem_free: float | None = None, load: float | None = None
    ) -> dict[str, Any]:
        """File paths to watch: sources of this owner's pages that update on change.

        The host also says how busy it is (``mem_free``: share of memory free, ``load``:
        load per CPU), which sizes how many of the owner's runs go at once.
        """
        owner = await _host_owner(request)
        if mem_free is not None or load is not None:
            service.note_load(owner, mem_free, load)
        return {"paths": await asyncio.to_thread(service.watched, owner)}

    @router.post("/live/changed", status_code=202)
    async def live_changed(request: Request, body: ChangedBody) -> dict[str, Any]:
        """Source files changed: start each affected page's update after the debounce."""
        owner = await _host_owner(request)
        keys = await asyncio.to_thread(service.changed, owner, body.paths)
        for key in keys:
            service.arm(key)
        return {"pages": len(keys)}

    def _pages(owner: str | None) -> list[dict[str, Any]]:
        newest = {
            (item.parent_session_id, item.name): item
            for item, _ in service.artifacts.list_latest(user_id=owner, limit=1000)
        }
        pages = []
        for live in service.live.of_user(owner):
            item = newest.get((live.parent_session_id, live.name))
            task = service.tasks.get(live.task_id)
            if item is None:
                continue
            state = live.state
            pages.append(
                {
                    "artifact_id": item.id,
                    "name": item.name,
                    "title": item.title,
                    "parent_session_id": live.parent_session_id,
                    "task_id": live.task_id,
                    "rrule": task.rrule if task else None,
                    "paused": task is None or task.state != "active",
                    "on_change": state["on_change"],
                    "sources": live.sources,
                    "checked_at": state["checked_at"],
                    "failures": state["failures"],
                    "last_failure": state["last_failure"],
                    "open_issue": state["open_issue"],
                    "last_update": state["last_update"]
                    and {
                        "version": state["last_update"].get("version"),
                        "at": state["last_update"].get("at"),
                        "changes": len(state["last_update"].get("applied") or []),
                    },
                }
            )
        return pages

    @router.get("/me/live-pages")
    async def my_live_pages(request: Request) -> dict[str, Any]:
        """Every live page of the caller in one call (Automations list, Library badges)."""
        owner = request_owner(request, auth_provider)
        return {"pages": await asyncio.to_thread(_pages, owner)}

    @router.get("/artifacts/{artifact_id}/live")
    async def get_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """The page's live settings and what the last update did, or ``{"live": null}``."""
        return await asyncio.to_thread(_describe, await _page(request, artifact_id))

    @router.put("/artifacts/{artifact_id}/live")
    async def put_live(request: Request, artifact_id: str, body: LiveBody) -> dict[str, Any]:
        """Keep the page updated on a schedule (create or change it)."""
        item = await _page(request, artifact_id)
        if part_editor(item.kind, item.name) is None:
            raise OmnigentError(
                "Only a page or a sheet can be kept updated", code=ErrorCode.INVALID_INPUT
            )
        try:
            validate_rrule(body.rrule)
        except RRuleValidationError as exc:
            raise OmnigentError(f"invalid rrule: {exc}", code=ErrorCode.INVALID_INPUT) from exc
        fields = body.model_dump(exclude={"rrule"})
        await asyncio.to_thread(lambda: service.upsert(item, rrule=body.rrule, **fields))
        return await asyncio.to_thread(_describe, item)

    @router.delete("/artifacts/{artifact_id}/live")
    async def delete_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """Stop updating: the schedule and the row go, the versions stay."""
        item = await _page(request, artifact_id)
        return {"deleted": await asyncio.to_thread(service.remove, item)}

    @router.post("/artifacts/{artifact_id}/live/reset")
    async def reset_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """Start fresh: forget what the updates wrote and declined; keep the settings."""
        item = await _page(request, artifact_id)
        await asyncio.to_thread(service.reset, item)
        return await asyncio.to_thread(_describe, item)

    @router.post("/artifacts/{artifact_id}/live/run", status_code=202)
    async def run_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """Update now: fire the page's scheduled task."""
        live, _ = await asyncio.to_thread(service.need, await _page(request, artifact_id))
        task = await asyncio.to_thread(service.tasks.get, live.task_id)
        run_now = getattr(request.app.state, "scheduled_task_run_now", None)
        if run_now is None or task is None:
            raise OmnigentError(
                "scheduled task scheduler is not running", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        if not await run_now(task.workspace_id, task.id):
            raise OmnigentError("an update is already running", code=ErrorCode.CONFLICT)
        return {"triggered": True}

    @router.post("/artifacts/{artifact_id}/live/undo")
    async def undo_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """Undo the newest update (only while it is still the newest version)."""
        created = await asyncio.to_thread(service.undo, await _page(request, artifact_id))
        return artifact_to_response(created, versions=created.version)

    @router.post("/artifacts/{artifact_id}/live/accept")
    async def accept_live(
        request: Request, artifact_id: str, element_id: str = Body(embed=True)
    ) -> dict[str, Any]:
        """Take the source's value for a part the person had changed."""
        item = await _page(request, artifact_id)
        created = await asyncio.to_thread(service.accept, item, element_id)
        return artifact_to_response(created, versions=created.version)

    @router.post("/artifacts/{artifact_id}/live/publish")
    async def publish_live(request: Request, artifact_id: str) -> dict[str, Any]:
        """Publish the update the published copy held back (same audience)."""
        item = await _page(request, artifact_id)
        await asyncio.to_thread(service.publish_held, item)
        return await asyncio.to_thread(_describe, item)

    @router.get("/artifacts/{artifact_id}/live/context")
    async def live_context(request: Request, artifact_id: str) -> dict[str, Any]:
        """What an update run (the Helper) needs before it reads the sources."""
        return await asyncio.to_thread(service.context, await _page(request, artifact_id))

    @router.post("/artifacts/{artifact_id}/live/save")
    async def live_save(
        request: Request,
        artifact_id: str,
        body: SaveBody,
        chat_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """An update run's outcome: at most one ``update`` version, made by the run ``chat_id``."""
        item = await _page(request, artifact_id)
        if chat_id is not None:
            check_id(chat_id, "chat_id")
        changes = [c.model_dump() for c in body.changes]
        return await asyncio.to_thread(
            lambda: service.save(
                item,
                base_version=body.base_version,
                changes=changes,
                facts=body.facts,
                sources=body.sources,
                chat_id=chat_id,
            )
        )

    return router
