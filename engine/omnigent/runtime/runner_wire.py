"""Wire shapes the runner and the engine server share.

The runner (HOST) never imports ``omnigent.server`` (ADR 0019), so the request, response and
event models both sides speak live here. ``omnigent.server.schemas`` re-exports them, and its
SSE event models all extend :class:`SSEEventBase`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BackgroundSessionTitleRequest(BaseModel):
    """Private runner request for isolated background title inference."""

    prompt: str = Field(min_length=1, max_length=20_000)
    additional_instructions: str | None = Field(default=None, max_length=4_000)
    agent_id: str | None = None
    model_override: str | None = None
    harness_override: str | None = None
    sub_agent_name: str | None = None

    model_config = ConfigDict(extra="forbid")


class BackgroundSessionTitleResponse(BaseModel):
    """Private runner result for background title inference."""

    status: Literal["generated", "unsupported"]
    title: str | None = None


class SSEEventBase(BaseModel):
    """
    Common base for every SSE event payload model.

    All events share two ambient fields:

    - ``type``: the event-type discriminator literal (defined per
      subclass so :data:`ServerStreamEvent` can dispatch).
    - ``sequence_number``: monotonic per-stream sequence number
      assigned by the SSE serializer at emit time
      (``_format_sse`` in ``omnigent/server/routes/sessions.py``).
      Producers leave it ``None``; the route serializer populates
      it on the wire. ``None`` on session-scoped events emitted
      directly by the runtime (the session stream does not number
      events).

    Subclasses MUST declare ``type`` as ``Literal[...]`` so the
    discriminated-union machinery can route incoming dicts. The
    ``model_config`` is forward-compatible (``extra="ignore"``) so a
    producer can add a field without breaking older parsers.

    :param sequence_number: Per-stream monotonic counter assigned
        by the SSE serializer, e.g. ``42``. ``None`` on the
        producer side (before serialization) and on session-scoped
        events that the runtime publishes directly without
        sequencing.
    """

    sequence_number: int | None = None

    model_config = ConfigDict(extra="ignore")


class SessionCreatedEvent(SSEEventBase):
    """
    A child (sub-agent) session was spawned from this session.

    Emitted by ``omnigent/tools/builtins/spawn.py:_spawn_one``
    onto the **parent** session's conversation stream after the
    child conversation row is created and the child task has been
    started. Per the session-rearchitecture spec §3 ("Event types
    and direction") and §7 ("Flow: client interacts with
    sub-agent"), this lets clients watching the parent session's
    SSE subscribe directly to the child's stream without polling
    history for the tunneled ``function_call`` item.

    The wire shape is FLAT (not enveloped):
    ``{"type": "session.created", "conversation_id": <parent>,
    "child_session_id": <child>, "agent_id": <agent or None>,
    "parent_session_id": <parent>, "sequence_number": null}``.

    The existing tunneled ``function_call`` ConversationItem
    (carried inside :class:`OutputItemDoneEvent`) is retained
    for compatibility — clients that don't yet implement the
    "subscribe to child stream" pattern can keep rendering sub-
    agent calls from the parent's persistent history.

    :param type: Always ``"session.created"``.
    :param conversation_id: The PARENT session/conversation id —
        this event rides the parent's stream, e.g.
        ``"conv_parent123"``.
    :param child_session_id: The newly-created child session id,
        e.g. ``"conv_child456"``. Same as ``conversation_id`` on
        the child's own stream when consumers pivot to it.
    :param agent_id: Registered agent id the child runs as,
        e.g. ``"agent_xyz"``. ``None`` is permitted only for
        legacy spawn paths that did not record an agent id;
        new code MUST set it.
    :param parent_session_id: Echo of ``conversation_id`` for
        consumers that key on a dedicated "parent" field rather
        than the carrier ``conversation_id``. Always equal to
        ``conversation_id``; included for forward-compat with
        clients that may relay these events across stream
        boundaries.

    Category: **transient** (SSE-only). The corresponding durable
    record of "a child session exists" lives in the conversation
    store as the child conversation row itself
    (``parent_conversation_id`` foreign key) and the parent's
    tunneled ``function_call`` item — reconnecting clients
    discover children by walking the parent's persisted history,
    not by replaying this event.
    """

    type: Literal["session.created"]
    conversation_id: str
    child_session_id: str
    agent_id: str | None = None
    parent_session_id: str | None = None
