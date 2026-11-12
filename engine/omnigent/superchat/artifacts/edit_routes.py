"""``PATCH /v1/artifacts/{id}/edit``: hand edits of a file, saved as a ``manual`` version.

A deck or any HTML page or mini app takes the same patches (``artifacts/patches.py``; a page has no
slides and no theme, so ``set-theme`` is refused for it). A text document (markdown, plain text)
takes one ``set-full-source`` patch: the document editor sends the whole new text and says what it
changed in the patch's ``summary``. The engine applies them to the version
it names and stores the result as a new version tagged ``manual``. A stale ``base_version`` is an
``artifact_stale`` 409 the editor reloads from; a patch that cannot be applied exactly is a
``conflict`` 409 whose message says to ask Superchat instead. The write-back to the host and
the "you changed X" note are the artifacts capability's turn prefix. The answer for a manual
version carries ``element_id`` and ``what`` (``patches.edit_note``) beside ``edit_summary``: the
edited element and the sentence the studio thread shows for it ("You made the title larger").

``POST /v1/artifacts/{id}/patch`` is the same patches from a file's studio (``artifact_patch``,
ADR 0024): applied to the newest version when ``base_version`` still names it, saved as an
``ai`` version of the studio's turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.superchat.artifact_kinds import MAX_ARTIFACT_BYTES, is_deck_name
from omnigent.superchat.artifacts.patches import (
    MAX_PATCHES,
    AmbiguousEdit,
    InvalidPatch,
    Patch,
    SetFullSource,
    apply_patches,
    changed_ids,
    edit_note,
)
from omnigent.superchat.artifacts.routes import (
    artifact_to_response,
    check_id,
    load_artifact,
    producing_turn,
    request_owner,
)
from omnigent.superchat.artifacts.store import (
    Artifact,
    SqlAlchemyArtifactStore,
    VersionConflictError,
)
from omnigent.superchat.artifacts.studio import group_of, is_studio_of

#: Sent with ``ErrorCode.ARTIFACT_STALE``; the web keys off the code, never this text.
STALE_MESSAGE = "The file changed since you opened it, so the latest version is showing now."

#: Text documents the editor saves whole (no element ids, none of the HTML checks).
TEXT_KINDS = frozenset({"md", "txt"})


def _apply_text(patches: list[Patch]) -> tuple[bytes, str]:
    """A text document's edit: exactly one ``set-full-source`` with the new text."""
    patch = patches[0] if len(patches) == 1 else None
    if not isinstance(patch, SetFullSource):
        raise InvalidPatch("A document is saved whole: send one set-full-source patch")
    if not patch.source.strip():
        raise InvalidPatch("The file can't be empty")
    return patch.source.encode("utf-8"), patch.summary or "restored an earlier state of the file"


class HtmlEditBody(BaseModel):
    """``PATCH /artifacts/{id}/edit`` body."""

    base_version: int = Field(ge=1)
    patches: list[Patch] = Field(min_length=1, max_length=MAX_PATCHES)


class StudioPatchBody(HtmlEditBody):
    """``POST /artifacts/{id}/patch`` body: the patches and the studio session sending them."""

    chat_id: str


def _check_kind(item: Artifact, patches: list[Patch]) -> None:
    """400 when the file takes no patches, or a patch its kind has no use for."""
    deck = is_deck_name(item.name)
    if not deck and item.kind not in TEXT_KINDS and item.kind != "html":
        raise OmnigentError("That file can't be edited by hand", code=ErrorCode.INVALID_INPUT)
    if not deck and any(p.kind == "set-theme" for p in patches):
        raise OmnigentError("Only a deck has a theme", code=ErrorCode.INVALID_INPUT)


def _apply(item: Artifact, data: bytes, patches: list[Patch]) -> tuple[bytes, str]:
    """The patched bytes and their summary (a text document is saved whole)."""
    if item.kind in TEXT_KINDS:
        return _apply_text(patches)
    return apply_patches(data, patches)


#: A patch the studio cannot apply exactly: the editor edits the file itself instead.
_STUDIO_AMBIGUOUS = "Edit the file at its workspace path and save it with artifact_save instead."


def _manual_response(item: Artifact) -> dict[str, Any]:
    """A manual version's answer: the artifact, and its edit in the studio's words."""
    note = edit_note(item.edit_summary)
    return {
        **artifact_to_response(item, versions=item.version),
        "element_id": note.element_id,
        "what": note.what,
    }


def create_html_edit_router(
    store: SqlAlchemyArtifactStore,
    *,
    auth_provider: AuthProvider | None = None,
    conversation_store: ConversationStore | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the edit router (decks, pages, text documents), mounted at ``/v1``."""
    router = APIRouter()

    @router.patch("/artifacts/{artifact_id}/edit")
    async def edit_html(request: Request, artifact_id: str, body: HtmlEditBody) -> dict[str, Any]:
        """Apply patches as a NEW ``manual`` version; 409 when ``base_version`` is stale."""
        item = await load_artifact(store, request_owner(request, auth_provider), artifact_id)
        _check_kind(item, body.patches)
        newest = await asyncio.to_thread(store.newest, item)
        head = newest.version if newest else item.version
        if body.base_version != head or item.version != head:
            raise OmnigentError(STALE_MESSAGE, code=ErrorCode.ARTIFACT_STALE)
        data = await asyncio.to_thread(store.read, item)
        try:
            new_data, summary = await asyncio.to_thread(_apply, item, data, body.patches)
        except InvalidPatch as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except AmbiguousEdit as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        if new_data == data:  # nothing changed: no identical "edited by hand" version
            return _manual_response(item)
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
                edit_summary=summary or "edited by hand",
                base_version=body.base_version,
                changed_ids=changed_ids(body.patches),
            )
        except VersionConflictError as exc:
            raise OmnigentError(STALE_MESSAGE, code=ErrorCode.ARTIFACT_STALE) from exc
        return _manual_response(created)

    @router.post("/artifacts/{artifact_id}/patch")
    async def patch_from_studio(
        request: Request, artifact_id: str, body: StudioPatchBody
    ) -> dict[str, Any]:
        """Apply a studio's patches to the file's newest version as a NEW ``ai`` version.

        Only the file's own studio may (``chat_id``); the version records the studio's turn.
        A ``base_version`` that is no longer the newest is an ``artifact_stale`` 409 naming the
        newest; an edit that cannot be done exactly is a ``conflict`` 409.
        """
        item = await load_artifact(store, request_owner(request, auth_provider), artifact_id)
        check_id(body.chat_id, "chat_id")
        if conversation_store is None:
            raise OmnigentError("Studio not available", code=ErrorCode.NOT_FOUND)
        await require_access(
            require_user(request, auth_provider),
            item.parent_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        studio = await asyncio.to_thread(conversation_store.get_conversation, body.chat_id)
        if (
            studio is None
            or studio.parent_conversation_id != item.parent_session_id
            or not is_studio_of(studio, group_of(item))
        ):
            raise OmnigentError(
                "artifact_patch edits only this chat's own file", code=ErrorCode.INVALID_INPUT
            )
        _check_kind(item, body.patches)
        newest = await asyncio.to_thread(store.newest, item) or item
        if body.base_version != newest.version:
            raise OmnigentError(
                f"{item.name} is at v{newest.version} now, newer than v{body.base_version}; "
                "read it again and redo your change on top of it.",
                code=ErrorCode.ARTIFACT_STALE,
            )
        data = await asyncio.to_thread(store.read, newest)
        try:
            new_data, summary = await asyncio.to_thread(_apply, newest, data, body.patches)
        except InvalidPatch as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        except AmbiguousEdit as exc:
            message = str(exc).replace("ask Superchat instead.", _STUDIO_AMBIGUOUS)
            raise OmnigentError(message, code=ErrorCode.CONFLICT) from exc
        if new_data == data:  # nothing changed: no identical version
            return {
                **artifact_to_response(newest, versions=newest.version),
                "source_path": newest.source_path,
                "summary": "",
                "unchanged": True,
            }
        if len(new_data) > MAX_ARTIFACT_BYTES:
            raise OmnigentError("file too large (25 MB max)", code=ErrorCode.INVALID_INPUT)
        _saver, turn_item_id = await asyncio.to_thread(
            producing_turn, conversation_store, item.parent_session_id, body.chat_id
        )
        try:
            created = await asyncio.to_thread(
                store.create,
                user_id=newest.user_id,
                parent_session_id=newest.parent_session_id,
                name=newest.name,
                title=newest.title,
                kind=newest.kind,
                mime=newest.mime,
                data=new_data,
                parent_version_id=newest.id,
                source_path=newest.source_path,
                edit_summary=summary or None,
                base_version=body.base_version,
                turn_item_id=turn_item_id,
                chat_id=body.chat_id,
                # The studio turn's own message, as for a studio's artifact_save.
                item_id=turn_item_id,
                changed_ids=changed_ids(body.patches),
            )
        except VersionConflictError as exc:
            raise OmnigentError(
                f"{item.name} is at v{exc.newest} now, newer than v{body.base_version}; "
                "read it again and redo your change on top of it.",
                code=ErrorCode.ARTIFACT_STALE,
            ) from exc
        return {
            **artifact_to_response(created, versions=created.version),
            "source_path": created.source_path,
            "summary": summary,
        }

    return router
