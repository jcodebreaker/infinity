"""The rhythm (ADR 0038): when a person likes to hear from the assistant unasked, learned from
how they behave after its proactive messages, never from ratings.

Layout: ``score`` (situations, scoring, smoothing, the decision and the summary line, pure),
``reach`` (the person's reach rules, pure), ``store`` (``rhythm_tallies``, ``rhythm_events``,
``reach_rules``), ``service`` (reads once, decides in code), ``routes`` (``/v1/me/rhythm``,
``/v1/me/reach-rules``), ``tools`` / ``handlers`` (the Conversation's ``reach_rules``). Other
capabilities call the module functions below; the topics capability reaches it through
:data:`omnigent.superchat.feature.ON_TOPIC_TOUCHED`, the outbox's messages come through
:func:`omnigent.superchat.feature.proactive_items`.

API (blocking; call off the event loop):

* :func:`good_moment` ``(owner, at, state, *, topic_key=None, urgent=False, senders=None)``
* :func:`next_good_slot` ``(owner, after, state, *, topic_key=None, urgent=False, senders=None)``
* :func:`record` ``(owner, item_id, event, at, *, state=None, topic_keys=None)``
* :func:`brief_paused` ``(owner) -> bool``: the person paused the morning brief too.
* :func:`register_rules_changed` ``(fn(owner))``: told after a reach rule is added or lifted.

What the person said beats all of it (ADR 0038, amended): their reach rules (pause, level,
hours, only for, mute) are hard ceilings over the learned rhythm, cold start, the exploration
floor and ``urgent``. ``senders`` (one per signal, ``None`` for one without a sender) lets
``only_for`` and ``mute`` name people. *Start over* clears what was learned, never the rules.

The rhythm owns every ceiling on unasked messages (ADR 0038, merged): the level (``off`` never),
the hard window (quiet hours, else weekdays 8-18), the level's daily cap (the brief not counted)
and one message per topic per 3 hours. ``urgent`` (a ``now`` signal) checks those only; the
Lookout keeps no limit of its own.

``state`` is ``"chatting"`` (a turn of the person's Conversation runs) or ``"not"``. Before the
feature is installed they follow today's rules with default preferences and nothing learned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from omnigent.superchat.feature import (
    ON_TOPIC_TOUCHED,
    Feature,
    InstallDeps,
    ToolManagerCtx,
    is_super_chat,
)
from omnigent.superchat.rhythm.handlers import handle_reach_rules_tool
from omnigent.superchat.rhythm.score import CHATTING, NOT, Model, Prefs
from omnigent.superchat.rhythm.tools import REACH_RULES_TOOL_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import FastAPI

    from omnigent.superchat.rhythm.service import Rhythm
    from omnigent.tools.base import Tool

__all__ = [
    "CHATTING",
    "FEATURE",
    "NOT",
    "brief_paused",
    "good_moment",
    "next_good_slot",
    "record",
    "register_rules_changed",
]

_rhythm: Rhythm | None = None


def good_moment(
    owner: str,
    at: datetime,
    state: str,
    *,
    topic_key: str | None = None,
    urgent: bool = False,
    senders: Sequence[str | None] | None = None,
) -> bool:
    """Whether *at* is a good moment to message *owner* unasked (the person's rules, the
    ceilings, then the rhythm; *urgent*: rules and ceilings only)."""
    if _rhythm is None:
        return Model(prefs=Prefs()).good(at, state, topic_key, urgent=urgent)
    return _rhythm.good_moment(
        owner, at, state, topic_key=topic_key, urgent=urgent, senders=senders
    )


def next_good_slot(
    owner: str,
    after: datetime,
    state: str,
    *,
    topic_key: str | None = None,
    urgent: bool = False,
    senders: Sequence[str | None] | None = None,
) -> datetime:
    """The first good moment at or after *after* (eight days on when there is none)."""
    if _rhythm is None:
        found = Model(prefs=Prefs()).next_slot(after, state, topic_key, urgent=urgent)
        return found if found is not None else after + timedelta(days=8)
    return _rhythm.next_good_slot(
        owner, after, state, topic_key=topic_key, urgent=urgent, senders=senders
    )


def record(
    owner: str,
    item_id: str,
    event: str,
    at: datetime,
    *,
    state: str | None = None,
    topic_keys: list[str] | None = None,
) -> bool:
    """Keep one behaviour event (``sent`` with *state* and every topic it covers, ``shown``,
    ``opened``, ``acted_on``) on the owner's proactive message; ``False`` when not recorded."""
    if _rhythm is None:
        return False
    return _rhythm.record(owner, item_id, event, at, state=state, topic_keys=topic_keys)


def brief_paused(owner: str) -> bool:
    """Whether the person paused the morning brief too (a pause that named it). Blocking."""
    return _rhythm.brief_paused(owner) if _rhythm is not None else False


def register_rules_changed(listener: Callable[[str], None]) -> None:
    """Call ``listener(owner)`` after the person adds or lifts a reach rule (blocking, never
    raising into the write), so held messages are looked at again under the new rules."""
    from omnigent.superchat.rhythm.service import RULES_CHANGED

    if listener not in RULES_CHANGED:
        RULES_CHANGED.append(listener)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """The person's own Conversation only: never a Helper, so never a hidden scheduled run."""
    from omnigent.superchat.rhythm.tools import ReachRulesTool

    return [ReachRulesTool()] if is_super_chat(labels) else []


def _install(app: FastAPI, deps: InstallDeps) -> None:
    global _rhythm
    from omnigent.superchat.rhythm.routes import create_rhythm_router
    from omnigent.superchat.rhythm.service import Rhythm
    from omnigent.superchat.rhythm.store import SqlAlchemyRhythmStore

    if deps.storage_location is None:
        return
    _rhythm = Rhythm(SqlAlchemyRhythmStore(deps.storage_location), deps.scheduled_task_store)
    app.state.rhythm = _rhythm
    ON_TOPIC_TOUCHED["rhythm"] = _rhythm.topic_touched
    app.include_router(
        create_rhythm_router(
            _rhythm,
            auth_provider=deps.auth_provider,
            conversation_store=deps.conversation_store,
            permission_store=deps.permission_store,
        ),
        prefix="/v1",
        tags=["rhythm"],
    )


FEATURE = Feature(
    name="rhythm",
    tools=_tools,
    handlers=dict.fromkeys(REACH_RULES_TOOL_NAMES, handle_reach_rules_tool),
    install=_install,
)
