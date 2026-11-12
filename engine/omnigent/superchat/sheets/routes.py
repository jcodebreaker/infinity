"""``/v1/artifacts/{id}/table``: a CSV / XLSX artifact as grids, read and hand-edited.

An edit saves a NEW ``manual`` version of the artifact (rows live in the artifacts store); this
router resolves the artifact through the artifacts capability and adds the table routes. The
pending-edits / delivered routes stay with artifacts: a router mounted after it would lose
``GET /artifacts/{artifact_id}``'s precedence for them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.superchat.artifact_kinds import MAX_ARTIFACT_BYTES
from omnigent.superchat.artifacts import (
    Artifact,
    SqlAlchemyArtifactStore,
    VersionConflictError,
    artifact_to_response,
    load_artifact,
    request_owner,
)
from omnigent.superchat.sheets import table as tbl


class TableCellEdit(BaseModel):
    """One cell change; ``row`` / ``col`` are 0-based, row 0 is the first displayed row."""

    sheet: str = Field(max_length=256)
    row: int = Field(ge=0, lt=1_048_576)
    col: int = Field(ge=0, lt=16_384)
    value: str | int | float | bool | None = None


class TableEditBody(BaseModel):
    """``PATCH /artifacts/{id}/table`` body."""

    base_version: int = Field(ge=1)
    edits: list[TableCellEdit] = Field(min_length=1, max_length=500)


def create_sheets_router(
    store: SqlAlchemyArtifactStore, *, auth_provider: AuthProvider | None = None
) -> APIRouter:
    """Build the sheets router, mounted with ``prefix="/v1"`` after the artifacts router."""
    router = APIRouter()

    async def _load(request: Request, artifact_id: str) -> Artifact:
        return await load_artifact(store, request_owner(request, auth_provider), artifact_id)

    async def _table_item(request: Request, artifact_id: str) -> Artifact:
        item = await _load(request, artifact_id)
        if item.kind not in tbl.TABLE_KINDS:
            raise OmnigentError("not a spreadsheet or CSV file", code=ErrorCode.INVALID_INPUT)
        return item

    async def _sheets(item: Artifact) -> list[dict[str, Any]]:
        data = await asyncio.to_thread(store.read, item)
        try:
            return await asyncio.to_thread(tbl.read_table, data, item.kind)
        except tbl.TableError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

    @router.get("/artifacts/{artifact_id}/table")
    async def get_table(request: Request, artifact_id: str) -> dict[str, Any]:
        """The CSV / XLSX as grids: ``sheets[].rows[][] = {v, f, t}`` (see ``truncated``)."""
        item = await _table_item(request, artifact_id)
        return {
            "artifact_id": item.id,
            "version": item.version,
            "kind": item.kind,
            "sheets": await _sheets(item),
        }

    @router.get("/artifacts/{artifact_id}/table/range")
    async def get_table_range(
        request: Request,
        artifact_id: str,
        range: str = Query(max_length=64),
        sheet: str | None = Query(default=None, max_length=256),
    ) -> dict[str, Any]:
        """A fenced, untrusted-data excerpt of a selection for the person's next message."""
        item = await _table_item(request, artifact_id)
        sheets = await _sheets(item)
        try:
            block = tbl.range_block(
                sheets, name=item.name, version=item.version, sheet=sheet, range_text=range
            )
        except tbl.TableError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return {"artifact_id": item.id, "version": item.version, "block": block}

    @router.patch("/artifacts/{artifact_id}/table")
    async def edit_table(
        request: Request, artifact_id: str, body: TableEditBody
    ) -> dict[str, Any]:
        """Apply cell edits as a NEW ``manual`` version; 409 when ``base_version`` is stale."""
        item = await _table_item(request, artifact_id)
        newest = await asyncio.to_thread(store.newest, item)
        head = newest.version if newest else item.version
        if body.base_version != head or item.version != head:
            raise OmnigentError(
                f"Stale edit: the newest version is {head}, not {body.base_version}",
                code=ErrorCode.CONFLICT,
            )
        data = await asyncio.to_thread(store.read, item)
        edits = [tbl.CellEdit(e.sheet, e.row, e.col, e.value) for e in body.edits]
        try:
            new_data, summary = await asyncio.to_thread(tbl.apply_edits, data, item.kind, edits)
        except tbl.TableError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        if len(new_data) > MAX_ARTIFACT_BYTES:
            raise OmnigentError("file too large (25 MB max)", code=ErrorCode.INVALID_INPUT)
        try:
            created = await asyncio.to_thread(
                store.create,
                user_id=item.user_id,
                parent_session_id=item.parent_session_id,
                name=item.name,
                title=item.title,
                kind=item.kind,
                mime=item.mime,
                data=new_data,
                origin="manual",
                parent_version_id=item.id,
                source_path=item.source_path,
                edit_summary=summary,
                base_version=body.base_version,
            )
        except VersionConflictError as exc:
            raise OmnigentError(
                f"Stale edit: the newest version is {exc.newest}, not {body.base_version}",
                code=ErrorCode.CONFLICT,
            ) from exc
        return artifact_to_response(created, versions=created.version)

    return router
