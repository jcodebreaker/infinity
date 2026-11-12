"""The ordered registry of Super Chat features and the framework seams that read it.

Add a primitive by creating ``omnigent/superchat/<name>/`` with a ``FEATURE`` and listing it
here; ``tools/manager.py``, ``runner/tool_dispatch.py`` and ``server/app.py`` need no change.

This module is the product's composition root (ADR 0019): the tool manager and the policy builder
are SHARED and never import Superchat, so importing this module registers the features' tools,
the approvals policy and the owner budgets with them. ``server/app.py`` and
``runner/app.py`` import it.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnigent.runtime.policies.hooks import (
    SessionPolicy,
    register_owner_budgets,
    register_session_policy,
)
from omnigent.superchat.approvals import policy as approvals_policy
from omnigent.superchat.feature import (
    BackgroundJob,
    Feature,
    Handler,
    HandlerCtx,
    InstallDeps,
    ToolManagerCtx,
)
from omnigent.superchat.models.budget import get_budgets
from omnigent.tools.manager import register_tool_source

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.spec import AgentSpec
    from omnigent.tools.base import Tool


def _load() -> list[Feature]:
    """Import each feature folder (order is registration order)."""
    from omnigent.superchat.admin import FEATURE as admin
    from omnigent.superchat.approvals import FEATURE as approvals
    from omnigent.superchat.apps import FEATURE as apps
    from omnigent.superchat.artifacts import ARTIFACTS_FEATURE as artifacts
    from omnigent.superchat.brief import FEATURE as brief
    from omnigent.superchat.cards import FEATURE as cards
    from omnigent.superchat.context_brief import FEATURE as context_brief
    from omnigent.superchat.daily_notes import FEATURE as daily_notes
    from omnigent.superchat.decks import FEATURE as decks
    from omnigent.superchat.family import FEATURE as family
    from omnigent.superchat.helpers import FEATURE as helpers
    from omnigent.superchat.knowledge import FEATURE as knowledge
    from omnigent.superchat.live import FEATURE as live
    from omnigent.superchat.lookout import FEATURE as lookout
    from omnigent.superchat.mail import FEATURE as mail
    from omnigent.superchat.memory import FEATURE as memory
    from omnigent.superchat.models import FEATURE as models
    from omnigent.superchat.outbox import FEATURE as outbox
    from omnigent.superchat.projects import FEATURE as projects
    from omnigent.superchat.recall_hint import FEATURE as recall_hint
    from omnigent.superchat.rhythm import FEATURE as rhythm
    from omnigent.superchat.rounds import FEATURE as rounds
    from omnigent.superchat.sheets import FEATURE as sheets
    from omnigent.superchat.state_file import FEATURE as state_file
    from omnigent.superchat.system_jobs import FEATURE as system_jobs
    from omnigent.superchat.tasks import FEATURE as tasks
    from omnigent.superchat.topics import FEATURE as topics
    from omnigent.superchat.transcript import FEATURE as transcript
    from omnigent.superchat.vault import FEATURE as vault

    return [
        approvals,
        vault,
        models,
        admin,
        daily_notes,
        topics,
        cards,
        memory,
        artifacts,
        live,
        brief,
        knowledge,
        apps,
        sheets,
        decks,
        helpers,
        projects,
        recall_hint,
        context_brief,
        state_file,
        tasks,
        transcript,
        family,
        system_jobs,
        rounds,
        outbox,
        rhythm,
        lookout,
        mail,
    ]


_logger = logging.getLogger(__name__)

FEATURES: list[Feature] = _load()


def _wire_brief_topics() -> None:
    """The morning brief reads the person's open topics (ADR 0037 over 0035); wired here, the one
    place that already imports every capability, so neither imports the other."""
    from omnigent.superchat.brief.sources import set_topics_source
    from omnigent.superchat.topics import list_watched_topics

    set_topics_source(list_watched_topics)


_wire_brief_topics()


def _wire_lookout() -> None:
    """The Lookout's seams (ADR 0038), wired here so no capability imports another: topics feed
    its code check and its promise signals, the rhythm decides when it may speak (and hears each
    message it sends), a live refresh in the Library pushes a signal, and the brief's run claims
    what waits and is posted by code."""
    from omnigent.superchat import lookout, rhythm
    from omnigent.superchat.artifacts import store as artifact_store
    from omnigent.superchat.brief.sources import set_signals_source
    from omnigent.superchat.brief.task import BRIEF_KIND
    from omnigent.superchat.topic_watch import library
    from omnigent.superchat.topics import (
        list_watched_topics,
        mark_topic_new,
        register_topic_listener,
        set_topic_status,
    )

    lookout.set_topic_seams(list_watched_topics, mark_topic_new, set_topic_status)
    lookout.set_rhythm(rhythm)
    rhythm.register_rules_changed(lookout.on_rules_changed)
    register_topic_listener(lookout.on_topic_write)
    library.set_sink(lookout.add_signal)
    artifact_store.ON_VERSION_SAVED["library_signals"] = library.on_version_saved
    set_signals_source(lookout.claim_for_brief)
    lookout.post_results_of(BRIEF_KIND)


_wire_lookout()


def _wire_reach_rules() -> None:
    """A pause that names the morning brief stops it too (ADR 0038, reach rules); every other
    reach rule is read by the rhythm itself."""
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.server.scheduled.helper_fire import register_fire_skip
    from omnigent.superchat import rhythm
    from omnigent.superchat.brief.task import BRIEF_KIND

    register_fire_skip(
        BRIEF_KIND, lambda task: rhythm.brief_paused(task.user_id or RESERVED_USER_LOCAL)
    )


_wire_reach_rules()


def _wire_mail() -> None:
    """Mail is a Lookout source (ADR 0039): it pushes through the Lookout's entry points after
    the Lookout's own code check, and only the Lookout's and the brief's runs read a thread."""
    from omnigent.superchat import lookout, mail
    from omnigent.superchat.brief.task import BRIEF_KIND
    from omnigent.superchat.lookout.run import LOOKOUT_KIND

    mail.set_lookout(lookout.add_signal, lookout.remove_signal, lookout.check_item)
    mail.set_reader_kinds((LOOKOUT_KIND, BRIEF_KIND))


_wire_mail()

#: ``tool_name -> handler`` over every feature, consulted by the runner dispatch.
FEATURE_HANDLERS: dict[str, Handler] = {
    name: handler for feature in FEATURES for name, handler in feature.handlers.items()
}

#: Handlers only the engine's relays call; the runner's granted-tool gate lets them through.
FEATURE_RELAY_OPS: frozenset[str] = frozenset(
    name for feature in FEATURES for name in feature.relay_ops
)


def notify_result(ctx: HandlerCtx, output: str) -> None:
    """Tell every feature that listens (``Feature.on_result``) what a handler just returned."""
    for feature in FEATURES:
        if feature.on_result is not None:
            feature.on_result(ctx, output)


def notify_turn_end(conversation_id: str) -> None:
    """Tell every feature that listens (``Feature.on_turn_end``) that a turn of this session ended.

    A listener's failure is logged and never reaches the runner's stream.
    """
    for feature in FEATURES:
        if feature.on_turn_end is None:
            continue
        try:
            feature.on_turn_end(conversation_id)
        except Exception:  # a listener is bookkeeping, never a reason to fail a turn
            _logger.warning("%s turn end failed", feature.name, exc_info=True)


def feature_tools(labels: Mapping[str, str] | None, ctx: ToolManagerCtx) -> list[Tool]:
    """Every feature's label-gated tool defs, in feature order."""
    return [tool for feature in FEATURES for tool in feature.tools(labels, ctx)]


def _superchat_tools(labels: Mapping[str, str] | None, spec: AgentSpec) -> list[Tool]:
    """The tool manager's view of :func:`feature_tools`."""
    return feature_tools(labels, ToolManagerCtx(spec=spec))


def install_features(app: FastAPI, deps: InstallDeps) -> None:
    """Wire every feature's store and router onto ``app``."""
    for feature in FEATURES:
        if feature.install is not None:
            feature.install(app, deps)


def feature_jobs(app: FastAPI) -> list[BackgroundJob]:
    """Every feature's background jobs (call once the app's lifespan state is ready)."""
    return [job for feature in FEATURES if feature.jobs for job in feature.jobs(app)]


async def start_feature_jobs(app: FastAPI) -> None:
    """Build every feature's jobs and start them (lifespan startup)."""
    app.state.feature_jobs = feature_jobs(app)
    for job in app.state.feature_jobs:
        result = job.start()
        if inspect.isawaitable(result):
            await result


async def stop_feature_jobs(app: FastAPI) -> None:
    """Shut down the jobs :func:`start_feature_jobs` started (lifespan shutdown)."""
    for job in reversed(getattr(app.state, "feature_jobs", [])):
        result = job.shutdown()
        if inspect.isawaitable(result):
            await result


# The engine's product seams, filled once at import (registration is by name, so idempotent).
register_tool_source("superchat", _superchat_tools)
register_session_policy(SessionPolicy(approvals_policy.POLICY_SPEC, approvals_policy.applies))
register_owner_budgets(get_budgets)
