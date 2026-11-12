"""``/v1/me/muse``: the caller's own Super Chat, found, adopted or made by the engine.

A client holding only a user token asks for "my Super Chat" here instead of keeping its own
user-to-session map. There is one per ``(user, tenant)``, found in this order:

1. a session carrying that pair's Muse key label and owned by the caller (newest first): an
   adopted Super Chat (``POST /v1/me/muse/adopt``) or one this route made;
2. the session whose id is derived from the pair (one this route made, read even before its
   owner grant lands);
3. otherwise a new one, created under that derived id, so the database primary key keeps two
   concurrent first calls to one row.

The agent a new one starts on is server config (``OMNIGENT_SUPERCHAT_DEFAULT_AGENT``), and
``OMNIGENT_HOST_PROVIDER`` optionally launches it on a host (``docker``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field

from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.evals.identity import run_of
from omnigent.hosts.docker import (
    LABEL_HOST_OWNER,
    LABEL_TENANT,
    tenant_from_labels,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user as _require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes.sessions.routes_core import CoreSessionOps
from omnigent.server.schemas import SessionCreateRequest
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import (
    ConversationAlreadyExistsError,
)
from omnigent.superchat.feature import is_super_chat

DEFAULT_AGENT_ENV = "OMNIGENT_SUPERCHAT_DEFAULT_AGENT"
HOST_PROVIDER_ENV = "OMNIGENT_HOST_PROVIDER"

#: Marks a caller's Super Chat (made or adopted here).
MUSE_LABEL_KEY = "omnigent.superchat.muse"
#: Its ``(user, tenant)`` key (:func:`muse_key`), the indexed value ``GET`` looks it up by.
MUSE_KEY_LABEL_KEY = "omnigent.superchat.muse.key"

#: Candidates read per lookup; normally one, more only if labels were copied by hand.
_LOOKUP_LIMIT = 10


class MuseResponse(BaseModel):
    """The caller's Super Chat.

    :param session_id: Its session id.
    :param agent: The agent it runs on, e.g. ``"superchat"``.
    :param created: ``True`` when this call created it.
    """

    session_id: str
    agent: str
    created: bool


class SetMuseAgentRequest(BaseModel):
    """Body of ``PUT /v1/me/muse``: the built-in agent the Super Chat should run on."""

    agent: str = Field(min_length=1, max_length=200)


class AdoptMuseRequest(BaseModel):
    """Body of ``POST /v1/me/muse/adopt``: an existing Super Chat the caller owns."""

    session_id: str = Field(min_length=1, max_length=64)


def muse_key(user_id: str, tenant: str | None) -> str:
    """The ``(user_id, tenant)`` key: 32 hex chars, also the id a new Super Chat gets."""
    key = f"omnigent-muse-v1\0{tenant or ''}\0{user_id}".encode()
    return hashlib.sha256(key).hexdigest()[:32]


#: A new Super Chat's session id is its key.
muse_session_id = muse_key


def find_super_chat(
    conversation_store: ConversationStore, user_id: str | None, key: str
) -> Conversation | None:
    """The caller's Super Chat for its Muse *key* (:func:`muse_key`), or ``None``. Blocking.

    A session carrying the key label and owned by the caller, else the one made under the
    derived id before its owner grant landed (its id proves whose it is).
    """
    ids = conversation_store.find_conversation_ids_by_label(
        MUSE_KEY_LABEL_KEY, key, limit=_LOOKUP_LIMIT
    )
    rows = conversation_store.get_conversations(ids)
    for session_id in ids:
        conv = rows.get(session_id)
        if conv is None or conv.labels.get(MUSE_LABEL_KEY) != "true":
            continue
        # Auth disabled (no user): the single local user owns everything.
        if user_id is None or (
            conversation_store.get_session_owner(session_id, owner_only=True) == user_id
        ):
            return conv
    made = conversation_store.get_conversation(key)
    if made is not None and made.labels.get(MUSE_KEY_LABEL_KEY) == key:
        return made
    return None


def register_muse_routes(
    router: APIRouter,
    *,
    ops: CoreSessionOps,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
) -> None:
    """Register ``GET`` / ``PUT /me/muse`` and ``POST /me/muse/adopt`` on *router*."""

    def _caller(request: Request) -> tuple[str | None, str | None, str]:
        """``(user_id, tenant, key)``; 401 without a user when auth is on."""
        user_id = _require_user(request, auth_provider)
        tenant = auth_provider.get_tenant(request) if auth_provider is not None else None
        return user_id, tenant, muse_key(user_id or RESERVED_USER_LOCAL, tenant)

    def _owns(user_id: str | None, session_id: str) -> bool:
        if user_id is None:  # auth disabled: the single local user owns everything
            return True
        return conversation_store.get_session_owner(session_id, owner_only=True) == user_id

    async def _find(user_id: str | None, key: str) -> Conversation | None:
        return await asyncio.to_thread(find_super_chat, conversation_store, user_id, key)

    async def _builtin_agent_id(name: str) -> str:
        agent = await asyncio.to_thread(agent_store.get_by_name, name)
        if agent is None or agent.session_id is not None:
            raise OmnigentError(f"Agent not found: {name!r}", code=ErrorCode.NOT_FOUND)
        return agent.id

    async def _current_agent(conv: Conversation) -> str:
        agent = await asyncio.to_thread(agent_store.get, conv.agent_id) if conv.agent_id else None
        return agent.name if agent is not None else ""

    async def _found(conv: Conversation) -> MuseResponse:
        return MuseResponse(session_id=conv.id, agent=await _current_agent(conv), created=False)

    async def _create(
        request: Request, user_id: str | None, tenant: str | None, key: str, agent: str
    ) -> MuseResponse:
        agent_id = await _builtin_agent_id(agent)
        labels = {
            CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE,
            MUSE_LABEL_KEY: "true",
            MUSE_KEY_LABEL_KEY: key,
            # The host is keyed by this opaque key, not the person's email.
            LABEL_HOST_OWNER: key,
        }
        if tenant:
            labels[LABEL_TENANT] = tenant
        provider = os.environ.get(HOST_PROVIDER_ENV, "").strip() or None
        body = SessionCreateRequest(
            agent_id=agent_id,
            labels=labels,
            host_type="managed" if provider else "external",
            sandbox_provider=provider,
        )
        try:
            await ops.create_json_session(request, user_id, body, conversation_id=key)
        except ConversationAlreadyExistsError:
            # A concurrent call for the same caller won: read the row it wrote.
            conv = await asyncio.to_thread(conversation_store.get_conversation, key)
            if conv is None or conv.labels.get(MUSE_KEY_LABEL_KEY) != key:
                raise OmnigentError("Super Chat id is taken", code=ErrorCode.CONFLICT) from None
            return await _found(conv)
        return MuseResponse(session_id=key, agent=agent, created=True)

    @router.get("/me/muse", response_model=MuseResponse)
    async def get_my_muse(request: Request) -> MuseResponse:
        """
        The caller's Super Chat for the current tenant: adopted, else made here, else new.

        :returns: ``{"session_id", "agent", "created"}``.
        :raises OmnigentError: 401 without a user; 503 ``superchat_not_configured`` when
            none exists and the server has no default agent; 404 if that agent is missing.
        """
        user_id, tenant, key = _caller(request)
        # An eval person's first ask for their Conversation registers them (ADR 0043).
        eval_persons = getattr(request.app.state, "eval_persons", None)
        if eval_persons is not None and run_of(user_id) is not None:
            await asyncio.to_thread(eval_persons.register, user_id)
        conv = await _find(user_id, key)
        if conv is not None:
            return await _found(conv)
        agent = os.environ.get(DEFAULT_AGENT_ENV, "").strip()
        if not agent:
            raise OmnigentError(
                f"No Super Chat agent is configured ({DEFAULT_AGENT_ENV})",
                code=ErrorCode.SUPERCHAT_NOT_CONFIGURED,
            )
        return await _create(request, user_id, tenant, key, agent)

    @router.put(
        "/me/muse",
        response_model=MuseResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def set_my_muse_agent(
        request: Request, body: SetMuseAgentRequest, background_tasks: BackgroundTasks
    ) -> MuseResponse:
        """
        Run the caller's Super Chat on the built-in agent ``body.agent`` (creating it if
        missing). Same trust checks as ``POST /v1/sessions/{id}/switch-agent``; a no-op when
        it already runs that agent.

        :raises OmnigentError: 404 for an unknown or non-built-in agent; 409 while a turn runs.
        """
        user_id, tenant, key = _caller(request)
        conv = await _find(user_id, key)
        if conv is None:
            return await _create(request, user_id, tenant, key, body.agent)
        if await _current_agent(conv) == body.agent:
            return MuseResponse(session_id=conv.id, agent=body.agent, created=False)
        agent_id = await _builtin_agent_id(body.agent)
        await ops.switch_agent(request, conv.id, agent_id, background_tasks)
        return MuseResponse(session_id=conv.id, agent=body.agent, created=False)

    @router.post(
        "/me/muse/adopt",
        response_model=MuseResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def adopt_my_muse(request: Request, body: AdoptMuseRequest) -> MuseResponse:
        """
        Claim an existing Super Chat the caller owns as their Super Chat for this tenant.

        Labels it with the Muse marker, its key and (when absent) the tenant; every other
        label, including the ones placing its host, is left as is. Adopting the same
        session again is a no-op.

        :raises OmnigentError: 404 unless the caller owns the session; 422 ``not_a_super_chat``
            for a Helper or plain session; 409 ``muse_already_set`` when another
            Super Chat is already theirs here; 409 ``muse_tenant_mismatch`` when the session
            sits in another tenant.
        """
        user_id, tenant, key = _caller(request)
        conv = await asyncio.to_thread(conversation_store.get_conversation, body.session_id)
        if conv is None or not await asyncio.to_thread(_owns, user_id, body.session_id):
            raise OmnigentError(
                f"Session not found: {body.session_id!r}", code=ErrorCode.NOT_FOUND
            )
        if (
            conv.kind == "sub_agent"
            or conv.parent_conversation_id is not None
            or not is_super_chat(conv.labels)
        ):
            raise OmnigentError(
                "Only a Super Chat itself can be adopted", code=ErrorCode.NOT_A_SUPER_CHAT
            )
        current = await _find(user_id, key)
        if current is not None and current.id != conv.id:
            raise OmnigentError(
                "Another Super Chat is already yours here", code=ErrorCode.MUSE_ALREADY_SET
            )
        session_tenant = tenant_from_labels(conv.labels)
        if tenant and session_tenant and session_tenant != tenant:
            raise OmnigentError(
                "The session belongs to another tenant", code=ErrorCode.MUSE_TENANT_MISMATCH
            )
        labels = {MUSE_LABEL_KEY: "true", MUSE_KEY_LABEL_KEY: key}
        if tenant and conv.labels.get(LABEL_TENANT) != tenant:
            labels[LABEL_TENANT] = tenant
        if any(conv.labels.get(k) != v for k, v in labels.items()):
            await asyncio.to_thread(conversation_store.set_labels, conv.id, labels)
        return await _found(conv)
