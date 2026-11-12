"""``/v1/artifacts``: deliverable files the Muse saved from its host.

``POST`` takes the raw file bytes (the runner-side ``artifact_save`` tool uploads them) and
adds a version when the name already exists in the Conversation. ``GET`` lists the newest
version of each deliverable, returns one with its versions, or streams the content. Owner-scoped:
another owner's rows 404.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel

from omnigent.context.labels import ARTIFACT_CHAT_LABEL_KEY
from omnigent.context.rollover import _MID_TURN_LIVE_STATUSES
from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.entities import Conversation, MessageData
from omnigent.entities.conversation import is_system_notice
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.redaction import session_redactor
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.superchat.artifact_kinds import (
    KIND_MIME,
    MAX_ARTIFACT_BYTES,
    is_deck_name,
    kind_for_name,
)
from omnigent.superchat.artifacts.hint import MAX_ARTIFACTS, artifact_hint
from omnigent.superchat.artifacts.links import (
    conversation_item,
    is_person_message,
    link_saved_version,
    open_artifact,
    steer_text,
)
from omnigent.superchat.artifacts.patches import name_elements
from omnigent.superchat.artifacts.prelude import brief_line, prelude
from omnigent.superchat.artifacts.store import (
    LINK_CHOSEN,
    WRITEBACK_ORIGINS,
    Artifact,
    MessageLink,
    Publication,
    SqlAlchemyArtifactStore,
    VersionConflictError,
    ViewStats,
)
from omnigent.superchat.artifacts.studio import (
    file_line,
    find_or_create_studio,
    group_of,
    is_studio_of,
    studio_of,
)
from omnigent.superchat.artifacts.studio_turn import MAX_SELECTED, selected_block
from omnigent.superchat.decks.outline import deck_outline, element_source, slide_sources
from omnigent.superchat.family.signals import notify_item_updated
from omnigent.superchat.rounds.record import file_saved, round_of
from omnigent.superchat.transcript.blocks import project_items

#: Steers the running turn of a session with one line; ``True`` when the turn took it.
Steer = Callable[[Request, str, str], Awaitable[bool]]
#: ``(request, owner, Conversation) -> model`` a new studio session runs on (``None``: inherit).
StudioModel = Callable[[Request, str | None, Conversation], Awaitable[str | None]]
#: ``(request, studio session id) -> warm``: tells the studio's runner, when one is up, to keep
#: its harness warm; never wakes a host.
KeepWarm = Callable[[Request, str], Awaitable[bool]]

#: The preview is shown in a sandboxed, origin-less iframe; this keeps a page that is opened
#: directly (or downloaded and served) from reaching anything but public https assets.
_HTML_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' https:; "
    "style-src 'unsafe-inline' https:; img-src data: blob: https:; font-src data: https:; "
    "media-src data: blob: https:; connect-src 'none'; form-action 'none'; base-uri 'none'"
)


def publication_to_response(pub: Publication, stats: ViewStats | None = None) -> dict[str, Any]:
    """Serialize a publication with its view stats."""
    return {
        "slug": pub.slug,
        "url_path": pub.url_path,
        "audience": pub.audience,
        "version": pub.published_version,
        "published_at": pub.published_at,
        "updated_at": pub.updated_at,
        "stats": (stats or ViewStats()).as_dict(),
    }


def artifact_to_response(
    item: Artifact,
    *,
    versions: int = 1,
    publication: Publication | None = None,
    stats: ViewStats | None = None,
) -> dict[str, Any]:
    """Serialize one artifact version for the REST API."""
    return {
        "publish": publication_to_response(publication, stats) if publication else None,
        "id": item.id,
        "artifact_id": item.id,
        "parent_session_id": item.parent_session_id,
        "name": item.name,
        "title": item.title,
        "kind": item.kind,
        "mime": item.mime,
        "size": item.size,
        "version": item.version,
        "versions": versions,
        "published": publication is not None,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "origin": item.origin,
        "chat_id": item.chat_id,
        "parent_version_id": item.parent_version_id,
        "edit_summary": item.edit_summary,
        "group_id": item.group_id or item.id,
    }


#: How many of a chat's newest messages are searched for the turn's opening one.
_TURN_LOOKBACK = 50
_logger = logging.getLogger(__name__)


def producing_turn(
    conversations: ConversationStore, parent_session_id: str, chat_id: str | None
) -> tuple[str | None, str | None]:
    """The chat saving a version into *parent_session_id*, and its current turn's person message.

    *chat_id* (``None``: the Conversation itself) counts only when it is that Conversation or a
    chat under it (a Helper, an update run); any other chat records nothing. The turn is named by
    the newest message of the person in that chat (a Helper's Brief, a run's prompt); runtime
    notices are skipped. A failed lookup records nothing: it never fails the save.

    :returns: ``(chat_id, turn_item_id)``, each ``None`` when unknown.
    """
    try:
        return _producing_turn(conversations, parent_session_id, chat_id)
    except Exception:  # noqa: BLE001 - provenance is a label, never a reason to lose a version
        _logger.warning("artifacts: could not find the turn saving into %s", parent_session_id)
        return None, None


def saving_chat(
    conversations: ConversationStore, parent_session_id: str, chat_id: str | None
) -> Conversation | None:
    """The chat saving into *parent_session_id*: that Conversation, or a chat under it (a Helper,
    an update run) named by *chat_id*. ``None`` for any other chat, one that does not exist, or
    a failed lookup: it never fails the save."""
    chat = chat_id or parent_session_id
    try:
        found = conversations.get_conversation(chat)
    except Exception:  # noqa: BLE001 - provenance is a label, never a reason to lose a version
        _logger.warning("artifacts: could not find the chat saving into %s", parent_session_id)
        return None
    if found is None or chat == parent_session_id:
        return found
    family = {found.parent_conversation_id, found.root_conversation_id}
    return found if parent_session_id in family else None


def _producing_turn(
    conversations: ConversationStore, parent_session_id: str, chat_id: str | None
) -> tuple[str | None, str | None]:
    chat = chat_id or parent_session_id
    if chat != parent_session_id and saving_chat(conversations, parent_session_id, chat) is None:
        return None, None
    page = conversations.list_items(chat, limit=_TURN_LOOKBACK, order="desc", type="message")
    for item in page.data:
        data = item.data
        if isinstance(data, MessageData) and data.role == "user" and not is_system_notice(data):
            return chat, item.id
    return chat, None


async def steer_running_turn(
    request: Request, conversation_store: ConversationStore, session_id: str, text: str
) -> bool:
    """Steer the session's running turn with *text* (:func:`steer_session`)."""
    return await steer_session(
        getattr(request.app.state, "runner_router", None), conversation_store, session_id, text
    )


async def steer_session(
    runner_router: Any, conversation_store: ConversationStore, session_id: str, text: str
) -> bool:
    """Steer the session's running turn with *text*, through the runner's own steer path.

    The line goes as a runtime notice with ``if_running``: the runner takes it into the turn
    running now, or refuses it (nothing is kept) when none runs.
    """
    from omnigent.server.routes._sessions.helpers import _get_runner_client
    from omnigent.server.routes._sessions.orchestration import (
        _dispatch_session_event_to_runner,
    )
    from omnigent.server.schemas import SessionEventInput

    conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
    if conversation is None:
        return False
    runner = await _get_runner_client(session_id, runner_router)
    if runner is None:
        return False
    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "is_system_notice": True,
            "content": [{"type": "input_text", "text": text}],
        },
        if_running=True,
    )
    try:
        await _dispatch_session_event_to_runner(
            session_id,
            conversation,
            body,
            conversation_store,
            runner,
            agent_name=None,
            file_store=None,
            artifact_store=None,
        )
    except (httpx.HTTPError, OmnigentError):
        return False
    return True


async def keep_studio_warm(request: Request, session_id: str) -> bool:
    """Ask the studio's runner, if it is up, to keep the studio's harness warm (ADR 0024).

    Only a runner already bound and online is told; with none, nothing happens: opening a
    studio never wakes a sleeping host.
    """
    from omnigent.server.routes._sessions.helpers import _get_runner_client

    runner = await _get_runner_client(
        session_id, getattr(request.app.state, "runner_router", None)
    )
    if runner is None:
        return False
    try:
        resp = await runner.post(f"/v1/sessions/{session_id}/studio/keep-warm", timeout=5.0)
        return resp.status_code == 200 and resp.json().get("warm") is True
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


class MessageArtifactBody(BaseModel):
    """``POST /sessions/{id}/items/{item_id}/artifact``: the artifact picked, or ``None``."""

    artifact_id: str | None


def check_id(value: str, what: str) -> None:
    try:
        uuid_to_bytes(value)
    except InvalidUuidError as exc:
        raise OmnigentError(f"invalid {what}", code=ErrorCode.INVALID_INPUT) from exc


def _disposition(kind: str, name: str) -> str:
    safe = posixpath.basename(name).replace('"', "'") or f"artifact.{kind}"
    return (
        f"filename=\"{safe.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{quote(name)}"
    )


def request_owner(request: Request, auth_provider: AuthProvider | None) -> str | None:
    """The calling owner's id, or ``None`` for the single local user (rows are unowned)."""
    user_id = require_user(request, auth_provider)
    owner = user_id if user_id is not None else RESERVED_USER_LOCAL
    return None if owner == RESERVED_USER_LOCAL else owner


async def load_artifact(
    store: SqlAlchemyArtifactStore, owner: str | None, artifact_id: str
) -> Artifact:
    """The owner's artifact version, or a 404 (another owner's rows are indistinguishable)."""
    check_id(artifact_id, "artifact id")
    item = await asyncio.to_thread(store.get, artifact_id, user_id=owner)
    if item is None:
        raise OmnigentError("Artifact not found", code=ErrorCode.NOT_FOUND)
    return item


def _refuse_unseen_manual_edit(
    store: SqlAlchemyArtifactStore, owner: str | None, session_id: str, name: str
) -> None:
    """409 when the newest version is a hand edit the Muse has not been told about yet.

    A save built from an older copy of the file would silently overwrite the person's edit; the
    write-back at the start of the Muse's turn marks the edit delivered, so a Muse that re-read
    the file passes.
    """
    group = store.versions_of(user_id=owner, parent_session_id=session_id, name=name)
    head = group[0] if group else None
    if head is not None and head.origin in WRITEBACK_ORIGINS and head.delivered_at is None:
        who = (
            f"{name} was refreshed from its sources"
            if head.origin == "update"
            else f"The person edited {name} by hand"
        )
        raise OmnigentError(
            f"{who} (v{head.version}); read the workspace file "
            "again and apply your change on top of their edits before saving.",
            code=ErrorCode.CONFLICT,
        )


def _unchanged_head(
    store: SqlAlchemyArtifactStore,
    owner: str | None,
    parent_session_id: str,
    name: str,
    data: bytes,
    base_version: int | None,
) -> Artifact | None:
    """The newest version when *data* is byte-identical to it, else ``None``.

    A save of the same bytes is no edit: it must not mint a version that claims a change (a
    save read before the edit landed looked exactly like this). A stale ``base_version`` is
    left to the store's conflict check.
    """
    group = store.versions_of(user_id=owner, parent_session_id=parent_session_id, name=name)
    head = group[0] if group else None
    if head is None or head.size != len(data):
        return None
    if base_version is not None and head.version != base_version:
        return None
    return head if store.read(head) == data else None


def _studio_saving_its_file(
    store: SqlAlchemyArtifactStore,
    owner: str | None,
    parent_session_id: str,
    name: str,
    chat_id: str | None,
    conversations: ConversationStore | None = None,
) -> bool:
    """Whether *chat_id* is the studio of the file *name* it is saving (ADR 0024)."""
    if chat_id is None or conversations is None:
        return False
    group = store.versions_of(user_id=owner, parent_session_id=parent_session_id, name=name)
    if not group:
        return False
    chat = conversations.get_conversation(chat_id)
    return (
        chat is not None
        and chat.parent_conversation_id == parent_session_id
        and is_studio_of(chat, group_of(group[0]))
    )


def _being_edited(conversations: ConversationStore, items: list[Artifact]) -> set[str]:
    """Names of the files among *items* whose studio has a turn running (ADR 0024)."""
    names: set[str] = set()
    for item in items:
        studio = conversations.get_conversation(studio_of(item))
        if (
            is_studio_of(studio, group_of(item))
            and studio is not None
            and not studio.archived
            and studio.live_status in _MID_TURN_LIVE_STATUSES
        ):
            names.add(item.name)
    return names


def create_artifacts_router(
    store: SqlAlchemyArtifactStore,
    *,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
    steer: Steer | None = None,
    studio_model: StudioModel | None = None,
    keep_warm: KeepWarm | None = None,
) -> APIRouter:
    """Build the artifacts router, mounted with ``prefix="/v1"``.

    :param steer: Steers a running turn (tests pass a fake); the runner's steer path otherwise.
    :param studio_model: The model a new studio session runs on (``None``: the Conversation's).
    :param keep_warm: Tells a studio's runner it is open (tests pass a fake); the runner's
        keep-warm route otherwise (:func:`keep_studio_warm`).
    """
    router = APIRouter()

    async def _steer(request: Request, session_id: str, text: str) -> bool:
        if steer is not None:
            return await steer(request, session_id, text)
        return await steer_running_turn(request, conversation_store, session_id, text)

    def _owner(request: Request) -> str | None:
        return request_owner(request, auth_provider)

    def _caller(request: Request) -> str | None:
        # Access is checked as the real caller: the single local user owns its sessions, even
        # though its artifact rows are stored unowned (``_owner`` is ``None`` for it).
        return require_user(request, auth_provider)

    async def _load(request: Request, artifact_id: str) -> Artifact:
        return await load_artifact(store, _owner(request), artifact_id)

    @router.post("/artifacts", status_code=201)
    async def create_artifact(
        request: Request,
        response: Response,
        parent_session_id: str = Query(),
        name: str = Query(min_length=1, max_length=512),
        title: str | None = Query(default=None, max_length=256),
        source_path: str | None = Query(default=None, max_length=1024),
        chat_id: str | None = Query(default=None),
        base_version: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        """Save the request body as the next version of ``name`` in a session the caller owns.

        ``chat_id`` is the chat saving it when that is not the Conversation itself (a Helper);
        the version records it and its turn (:func:`producing_turn`). A file's studio saving
        its own file must name the ``base_version`` it edited (ADR 0024): a stale one is an
        ``artifact_stale`` 409, and the version links to the studio's own message, not to one
        of the Conversation's. Bytes identical to the newest version make no version: the
        answer is that version with ``unchanged: true`` (200).
        """
        owner = _owner(request)
        check_id(parent_session_id, "parent_session_id")
        await require_access(
            _caller(request), parent_session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        kind = kind_for_name(name)
        if kind is None:
            raise OmnigentError(
                f"file type not allowed; use one of: {', '.join(KIND_MIME)}",
                code=ErrorCode.INVALID_INPUT,
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_ARTIFACT_BYTES:
            raise OmnigentError("file too large (25 MB max)", code=ErrorCode.INVALID_INPUT)
        data = await request.body()
        if not data:
            raise OmnigentError("file is empty", code=ErrorCode.INVALID_INPUT)
        if len(data) > MAX_ARTIFACT_BYTES:
            raise OmnigentError("file too large (25 MB max)", code=ErrorCode.INVALID_INPUT)
        clean_name = posixpath.basename(name)
        if chat_id is not None:
            check_id(chat_id, "chat_id")
        studio = await asyncio.to_thread(
            _studio_saving_its_file,
            store,
            owner,
            parent_session_id,
            clean_name,
            chat_id,
            conversation_store,
        )
        if studio and base_version is None:
            raise OmnigentError(
                "Saving this chat's file needs base_version: the version you edited.",
                code=ErrorCode.INVALID_INPUT,
            )
        if not studio:
            # The studio's base_version is the stronger check; everyone else may not save over
            # a hand edit they were not told about.
            await asyncio.to_thread(
                _refuse_unseen_manual_edit, store, owner, parent_session_id, clean_name
            )
        if kind == "html" and not is_deck_name(clean_name):
            # A page is editable by hand only where it has ids; don't leave that to the author.
            data = await asyncio.to_thread(name_elements, data)
        same = await asyncio.to_thread(
            _unchanged_head, store, owner, parent_session_id, clean_name, data, base_version
        )
        if same is not None:
            response.status_code = 200  # nothing was created
            return {**artifact_to_response(same, versions=same.version), "unchanged": True}
        saving = await asyncio.to_thread(
            saving_chat, conversation_store, parent_session_id, chat_id
        )
        round_id = round_of(saving.labels) if saving is not None and not studio else None
        if saving is not None and round_id is not None:
            # The version came from its chat's Round: the person's message that opened it.
            saver, turn_item_id, item_id = saving.id, round_id, round_id
        else:
            saver, turn_item_id = await asyncio.to_thread(
                producing_turn, conversation_store, parent_session_id, chat_id
            )
            if studio:
                # The studio turn's own message; no Conversation message led to this version.
                item_id = turn_item_id
            else:
                item_id = await asyncio.to_thread(
                    conversation_item, conversation_store, parent_session_id, saver, turn_item_id
                )
        try:
            item = await asyncio.to_thread(
                store.create,
                user_id=owner,
                parent_session_id=parent_session_id,
                name=clean_name,
                title=title,
                kind=kind,
                mime=KIND_MIME[kind],
                data=data,
                source_path=source_path,
                turn_item_id=turn_item_id,
                chat_id=saver,
                item_id=item_id,
                base_version=base_version,
                round_id=round_id,
            )
        except VersionConflictError as exc:
            raise OmnigentError(
                f"{clean_name} is at v{exc.newest} now, newer than v{base_version}; read the "
                "file again and redo your change on top of it before saving.",
                code=ErrorCode.ARTIFACT_STALE,
            ) from exc
        if not studio:  # the Conversation's chip; a studio save stays out of Super Chat
            try:
                await asyncio.to_thread(link_saved_version, store, item)
            except Exception:  # the chip is a label, never a reason to fail a save
                _logger.warning(
                    "artifacts: could not link %s to its message", item.id, exc_info=True
                )
            if saving is not None:
                file_saved(saving, item)
        return artifact_to_response(item, versions=item.version)

    @router.get("/artifacts")
    async def list_artifacts(
        request: Request,
        parent_session_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        """The newest version of each deliverable, newest first."""
        owner = _owner(request)
        if parent_session_id is not None:
            check_id(parent_session_id, "parent_session_id")
        rows = await asyncio.to_thread(
            store.list_latest, user_id=owner, parent_session_id=parent_session_id, limit=limit
        )
        pubs = await asyncio.to_thread(store.publications_for_owner, owner)
        stats = await asyncio.to_thread(store.stats, [p.slug for p in pubs.values()])
        out = []
        for a, n in rows:
            pub = pubs.get((a.parent_session_id, a.name))
            out.append(
                artifact_to_response(
                    a, versions=n, publication=pub, stats=stats.get(pub.slug) if pub else None
                )
            )
        return {"artifacts": out}

    @router.get("/artifacts/pending-edits")
    async def pending_edits(
        request: Request,
        parent_session_id: str = Query(),
        group_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Hand-edited versions in a session not yet written back to its host.

        The runner calls this at the start of the Muse's turn: it writes each ``latest``
        edit's bytes to ``source_path``, tells the Muse (``note``) and then acks the ids via
        ``POST /artifacts/{id}/delivered``. A file's studio asks for its own file only
        (``group_id``).
        """
        owner = _owner(request)
        check_id(parent_session_id, "parent_session_id")
        if group_id is not None:
            check_id(group_id, "group_id")
        await require_access(
            _caller(request), parent_session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        rows = await asyncio.to_thread(
            store.pending_manual,
            user_id=owner,
            parent_session_id=parent_session_id,
            group_id=group_id,
        )
        newest: dict[str, int] = {}
        for row in rows:
            newest[row.name] = max(newest.get(row.name, 0), row.version)
        edits = []
        for row in rows:
            head = await asyncio.to_thread(store.newest, row)
            edits.append(
                {
                    **artifact_to_response(row),
                    "source_path": row.source_path,
                    # Only the group's newest version is written back, and only when it is
                    # this hand edit (a later save by the Muse supersedes it).
                    "write_back": head is not None and head.id == row.id,
                }
            )
        return {"edits": edits}

    @router.get("/artifacts/hint")
    async def artifacts_hint(request: Request, session_id: str = Query()) -> dict[str, Any]:
        """The file handles for the start of a turn in the Conversation (``None``: none).

        The runner calls this when a turn starts: this Conversation's files, most recently
        changed first, with the one the person's latest message was sent with open marked, cut
        to the hint's budget with a count of the rest. A Helper has none.
        """
        owner = _owner(request)
        check_id(session_id, "session_id")
        await require_access(
            _caller(request), session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None or conversation.parent_conversation_id is not None:
            return {"hint": None}
        rows = await asyncio.to_thread(
            store.list_latest, user_id=owner, parent_session_id=session_id
        )
        _, asked = await asyncio.to_thread(producing_turn, conversation_store, session_id, None)
        opened = await asyncio.to_thread(open_artifact, store, session_id, asked)
        newest = [a for a, _n in rows]
        shown = [*newest[:MAX_ARTIFACTS], *([opened] if opened else [])]
        editing = await asyncio.to_thread(_being_edited, conversation_store, shown)
        return {"hint": artifact_hint(newest, opened, editing)}

    @router.get("/artifacts/studio-turn")
    async def studio_turn(
        request: Request,
        session_id: str = Query(),
        element: list[str] = Query(default=[]),
        slide: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        """What a file's studio turn starts with (``{"studio": null}`` for any other session).

        The runner calls this at the start of every studio turn: the file line, a deck's slide
        outline, the source of the elements (by ``data-superchat-id``) or the slide the message
        selected (ADR 0031), and the Conversation and group its write-back is scoped to
        (resolved the way saves are: the studio's parent, ADR 0024).
        """
        owner = _owner(request)
        check_id(session_id, "session_id")
        await require_access(
            _caller(request), session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        session = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        group = (session.labels if session is not None else {}).get(ARTIFACT_CHAT_LABEL_KEY)
        if session is None or not group or session.parent_conversation_id is None:
            return {"studio": None}
        newest = await asyncio.to_thread(store.newest_in_group, group, user_id=owner)
        if newest is None or newest.parent_session_id != session.parent_conversation_id:
            return {"studio": None}
        outline: str | None = None
        selected: str | None = None
        wanted = element[:MAX_SELECTED]
        if is_deck_name(newest.name) or ((wanted or slide) and newest.kind == "html"):
            try:
                source = await asyncio.to_thread(store.read, newest)
                if is_deck_name(newest.name):  # the slides by code: most turns need no read
                    outline = await asyncio.to_thread(deck_outline, source)
                selected = await asyncio.to_thread(
                    _selected, source, newest.version, wanted, slide
                )
            except Exception:
                _logger.warning("artifacts: no outline for %s", newest.id, exc_info=True)
        rows = await asyncio.to_thread(_prelude, newest, session_id)
        return {
            "studio": {
                "conversation_id": newest.parent_session_id,
                "group_id": group,
                "file_line": file_line(newest, session_id),
                "brief": brief_line(rows),
                "outline": outline,
                "selected": selected,
            }
        }

    def _selected(
        source: bytes, version: int, ids: list[str], slide: int | None
    ) -> str | None:
        """The source of the selected elements, else of the one slide the ask is about."""
        parts = [(f"element {i}", found) for i in ids if (found := element_source(source, i))]
        if not ids and slide is not None:
            slides = slide_sources(source)
            if slide <= len(slides):
                parts = [(f"slide {slide}", slides[slide - 1])]
        return selected_block(version, parts)

    def _prelude(item: Artifact, studio_id: str) -> list[dict[str, Any]]:
        """The file's prelude (ADR 0030)."""
        return prelude(conversation_store, store.versions(item), studio_id)

    @router.post("/artifacts/{artifact_id}/chat")
    async def artifact_chat(request: Request, artifact_id: str) -> dict[str, Any]:
        """The file's studio session, made on first open (ADR 0024), with its prelude (ADR 0030):
        ``{session_id, created, prelude: [{version, round_id, ask, handback, item_id, at}]}``.

        Any version id of the file; the caller must own its Conversation (another owner's file
        is a 404, and so is a deleted one). Never wakes the host: the session is only written
        to the store; its first message brings the runner up like any other. The prelude is
        read from the Conversation each time (``artifacts/prelude.py``), never stored.
        """
        item = await _load(request, artifact_id)
        caller = _caller(request)
        await require_access(
            caller, item.parent_session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        session_id = studio_of(item)
        model: str | None = None
        existing = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if existing is None and studio_model is not None:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, item.parent_session_id
            )
            if parent is not None:
                model = await studio_model(request, item.user_id, parent)
        try:
            session_id, created = await asyncio.to_thread(
                find_or_create_studio, conversation_store, item, model_override=model
            )
        except LookupError as exc:
            raise OmnigentError("Artifact not found", code=ErrorCode.NOT_FOUND) from exc
        if created and permission_store is not None and hasattr(permission_store, "grant"):
            owner = caller if caller is not None else RESERVED_USER_LOCAL
            if owner == RESERVED_USER_LOCAL:
                await asyncio.to_thread(permission_store.ensure_user, RESERVED_USER_LOCAL)
            await asyncio.to_thread(permission_store.grant, owner, session_id, LEVEL_OWNER)
        rows = await asyncio.to_thread(_prelude, item, session_id)
        return {"session_id": session_id, "created": created, "prelude": rows}

    @router.post("/artifacts/{artifact_id}/chat/open")
    async def artifact_chat_open(request: Request, artifact_id: str) -> dict[str, Any]:
        """The file's studio is open (ADR 0024): ``{session_id, warm}``.

        Sent when the studio opens and every few minutes while it stays open; its runner keeps
        the studio's harness warm for ten minutes from each. Never makes the session (``POST
        .../chat`` does) and never wakes the host: ``warm`` is ``False`` when no runner is up.
        """
        item = await _load(request, artifact_id)
        await require_access(
            _caller(request),
            item.parent_session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
        )
        session_id = studio_of(item)
        studio = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if not is_studio_of(studio, group_of(item)) or studio is None or studio.archived:
            raise OmnigentError("Studio not found", code=ErrorCode.NOT_FOUND)
        warm = await (keep_warm or keep_studio_warm)(request, session_id)
        return {"session_id": session_id, "warm": warm}

    @router.post("/sessions/{session_id}/items/{item_id}/artifact")
    async def set_message_artifact(
        request: Request,
        session_id: str,
        item_id: str,
        body: MessageArtifactBody,
    ) -> dict[str, Any]:
        """The person picks which artifact their message is about (``None``: none).

        Records it as ``chosen``, tells the family stream, steers the turn the message started
        when it is still running, and answers with the message as the transcript shows it.
        """
        user_id = _caller(request)
        check_id(session_id, "session_id")
        await require_access(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        try:
            uuid_to_bytes(item_id)
            item = await asyncio.to_thread(conversation_store.get_item, session_id, item_id)
        except InvalidUuidError:
            item = None
        if item is None or not is_person_message(item):
            raise OmnigentError("Message not found", code=ErrorCode.NOT_FOUND)
        current = await asyncio.to_thread(store.message_link, session_id, item_id)
        artifact_id = body.artifact_id
        if artifact_id is None:
            link = MessageLink(None, None, None, LINK_CHOSEN)
        else:
            check_id(artifact_id, "artifact_id")
            picked = await asyncio.to_thread(store.get_in_session, artifact_id, session_id)
            if picked is None:
                raise OmnigentError("Artifact not found", code=ErrorCode.NOT_FOUND)
            same = current is not None and current.name == picked.name
            version = current.version if same and current is not None else None
            link = MessageLink(picked.id, picked.name, version, LINK_CHOSEN)
        await asyncio.to_thread(store.set_message_link, session_id, item_id, link)
        notify_item_updated(session_id, item_id)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is not None and conversation.live_status in _MID_TURN_LIVE_STATUSES:
            _, latest = await asyncio.to_thread(
                producing_turn, conversation_store, session_id, None
            )
            if latest == item_id:
                await _steer(request, session_id, steer_text(link))
        projected = project_items([item.to_api_dict()])
        message = projected[0] if projected else {"id": item.id, "role": "user", "blocks": []}
        message["artifact"] = link.to_wire()
        redactor = await session_redactor(request, conversation_store, session_id, user_id)
        return redactor.deep(message)

    @router.post("/artifacts/{artifact_id}/delivered")
    async def mark_delivered(request: Request, artifact_id: str) -> dict[str, Any]:
        """Ack that a hand-edited version was written back and announced (idempotent)."""
        item = await _load(request, artifact_id)
        ok = await asyncio.to_thread(store.mark_delivered, item.id, user_id=item.user_id)
        return {"delivered": ok}

    @router.post("/artifacts/{artifact_id}/restore")
    async def restore_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """Make this earlier version the newest again: a NEW ``restore`` version with its bytes.

        Nothing is rewritten in place, so the versions in between stay in the history (and a
        restore can itself be restored over). Like a hand edit it is written back to the
        host and announced to the Muse at the start of its next turn.
        """
        item = await _load(request, artifact_id)
        head = await asyncio.to_thread(store.newest, item)
        if head is None or head.id == item.id:
            return artifact_to_response(item, versions=item.version)
        data = await asyncio.to_thread(store.read, item)
        try:
            created = await asyncio.to_thread(
                store.create,
                user_id=item.user_id,
                parent_session_id=item.parent_session_id,
                name=item.name,
                title=head.title,
                kind=item.kind,
                mime=item.mime,
                data=data,
                origin="restore",
                parent_version_id=head.id,
                source_path=head.source_path,
                edit_summary=f"restored version {item.version}",
                base_version=head.version,
            )
        except VersionConflictError as exc:
            raise OmnigentError(
                "A newer version arrived meanwhile; pick the version again to restore it.",
                code=ErrorCode.CONFLICT,
            ) from exc
        return artifact_to_response(created, versions=created.version)

    @router.get("/artifacts/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """One version's metadata plus every version of its deliverable."""
        item = await _load(request, artifact_id)
        versions = await asyncio.to_thread(store.versions, item)
        pub = await asyncio.to_thread(store.publication_for, item)
        stats = (await asyncio.to_thread(store.stats, [pub.slug])).get(pub.slug) if pub else None
        studio = studio_of(item)
        return {
            **artifact_to_response(item, versions=len(versions), publication=pub, stats=stats),
            "all_versions": [
                {
                    "id": v.id,
                    "version": v.version,
                    "size": v.size,
                    "created_at": v.created_at,
                    "origin": v.origin,
                    "parent_version_id": v.parent_version_id,
                    "edit_summary": v.edit_summary,
                    "turn_item_id": v.turn_item_id,
                    "chat_id": v.chat_id,
                    "item_id": v.item_id,
                    # Saved from the file's own studio chat; item_id is then its message there.
                    "from_studio": v.chat_id is not None and v.chat_id == studio,
                }
                for v in versions
            ],
        }

    @router.get("/artifacts/{artifact_id}/content")
    async def get_artifact_content(
        request: Request,
        artifact_id: str,
        version: int | None = Query(default=None, ge=1),
        download: bool = Query(default=False),
    ) -> Response:
        """The file's bytes with its real content type (``?version=`` picks another version)."""
        item = await _load(request, artifact_id)
        if version is not None and version != item.version:
            match = next(
                (v for v in await asyncio.to_thread(store.versions, item) if v.version == version),
                None,
            )
            if match is None:
                raise OmnigentError("Version not found", code=ErrorCode.NOT_FOUND)
            item = match
        data = await asyncio.to_thread(store.read, item)
        headers = {
            "Content-Disposition": f"{'attachment' if download else 'inline'}; "
            f"{_disposition(item.kind, item.name)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0, must-revalidate",
        }
        if item.kind == "html":
            headers["Content-Security-Policy"] = _HTML_CSP
        return Response(content=data, media_type=item.mime, headers=headers)

    @router.delete("/artifacts/{artifact_id}")
    async def delete_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """Delete a deliverable with all its versions."""
        item = await _load(request, artifact_id)
        removed = await asyncio.to_thread(store.delete_group, item)
        return {"deleted": True, "name": item.name, "versions_removed": removed}

    return router
