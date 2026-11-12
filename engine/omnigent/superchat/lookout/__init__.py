"""The Lookout (ADR 0038): one per person; owns everything that can message the person unasked.

Layout: ``signals`` (the signal and its time rules, pure), ``rules`` (the code check: the hard
filter and the matching that is only a hint, pure), ``judge`` (ADR 0040: one direct model call
per wake scores each signal), ``store`` (``lookout_*`` tables), ``prompt`` (the wake run's
contract and how its Result is read, pure), ``run`` (the hidden ``lookout_wake`` task and
:func:`~omnigent.superchat.lookout.run.launch_wake`, a work-queue job), ``post`` (posting a
Result by code, with the file it made as a card), ``service`` (the wake), ``sources`` (the
interface a source plugs into).

Entry points other capabilities reach through the composition root (``superchat/features.py``),
never by import: :func:`add_signal`, :func:`remove_signal`, :func:`check_item` (the code check a
source runs before it pushes, mail's triage), :func:`on_topic_write` (promise
signals, the topics listener), :func:`claim_for_brief` and :func:`post_results_of` (the brief),
and :func:`set_rhythm` (the rhythm, which owns every ceiling on when the Lookout may speak).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from omnigent.evals.identity import is_eval_identity
from omnigent.superchat.feature import BackgroundJob, Feature, InstallDeps
from omnigent.superchat.lookout.signals import Signal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fastapi import FastAPI

    from omnigent.superchat.lookout.service import Lookout

__all__ = [
    "FEATURE",
    "Signal",
    "add_signal",
    "check_item",
    "claim_for_brief",
    "on_rules_changed",
    "on_topic_write",
    "post_results_of",
    "remove_signal",
    "set_rhythm",
    "set_topic_seams",
]

_logger = logging.getLogger(__name__)

#: The service :func:`_install` made; ``None`` before (or in the runner, which never installs).
_service: Lookout | None = None
#: Topic readers and writers handed in by the composition root before install.
_topic_seams: tuple[Any, Any, Any] = (None, None, None)
#: Other kinds whose runs' Results the Lookout posts by code (the brief's).
_result_kinds: list[str] = []
#: The rhythm capability, handed in by the composition root before install.
_rhythm: Any | None = None


def add_signal(signal: Signal | Mapping[str, Any]) -> str | None:
    """Leave *signal* for its owner's Lookout (blocking, no model run): the one entry point every
    source pushes through, a :class:`Signal` or a mapping of its fields (so a source needs no
    import of this folder). ``None`` when the Lookout is not installed."""
    if _service is None:
        _logger.debug("lookout not installed; a signal was dropped")
        return None
    owner = signal.owner if isinstance(signal, Signal) else signal.get("owner")
    if is_eval_identity(owner):
        return None  # an eval person has no Lookout (ADR 0042)
    return _service.add_signal(signal)


def remove_signal(owner: str, dedupe_key: str) -> int:
    """Drop the owner's waiting signal with *dedupe_key* (blocking)."""
    return _service.remove_signal(owner, dedupe_key) if _service is not None else 0


def check_item(owner: str, fields: Mapping[str, Any]) -> dict[str, str]:
    """The Lookout's code check for one item a source read (blocking, no model run):
    ``{"noise": why}``, the matched topic ``{"topic_key", "strength", "reason"}``, or ``{}``.
    ``{}`` too when the Lookout is not installed."""
    return _service.check_item(owner, fields) if _service is not None else {}


def on_topic_write(owner: str, before: Any, after: Any) -> None:
    """The topics listener (``register_topic_listener``: ``fn(owner, before, after)``, each a
    ``Topic`` or ``None``): keep promise signals in step with dated open lines."""
    if _service is not None:
        _service.on_topic_write(owner, before, after)


def on_rules_changed(owner: str) -> None:
    """The rhythm's listener (``register_rules_changed``): a reach rule was added or lifted, so
    held signals are looked at again now."""
    if _service is not None:
        _service.on_rules_changed(owner)


def claim_for_brief(task: Any) -> str | None:
    """The brief's hook: claim the waiting signals into its run (lines, or ``None``)."""
    return _service.claim_for_brief(task) if _service is not None else None


def set_topic_seams(
    list_topics: Callable[[str], Sequence[Any]] | None,
    mark_new: Callable[[str, str, str, int], Any] | None,
    set_status: Callable[..., Any] | None = None,
) -> None:
    """Hand in the topics capability's readers and its status writer (composition root)."""
    global _topic_seams
    _topic_seams = (list_topics, mark_new, set_status)
    if _service is not None:
        _service.set_topic_seams(list_topics, mark_new, set_status)


def set_rhythm(rhythm: Any | None) -> None:
    """Hand in the rhythm capability (``good_moment``, ``next_good_slot``, ``record``)."""
    global _rhythm
    _rhythm = rhythm
    if _service is not None:
        _service.set_rhythm(rhythm)


def post_results_of(kind: str) -> None:
    """Post the Results of *kind* runs by code too, folding in claimed signals (the brief)."""
    if kind not in _result_kinds:
        _result_kinds.append(kind)


def _install(app: FastAPI, deps: InstallDeps) -> None:
    global _service
    import functools

    from omnigent.server import work_queue
    from omnigent.server.routes.sandbox_inference import session_model
    from omnigent.server.scheduled.helper_fire import register_run_brief, register_run_result
    from omnigent.superchat.lookout import judge as judging
    from omnigent.superchat.lookout.run import LOOKOUT_KIND
    from omnigent.superchat.lookout.service import Lookout
    from omnigent.superchat.lookout.store import SqlAlchemyLookoutStore

    if deps.scheduled_task_store is None or deps.storage_location is None:
        return
    tasks = deps.scheduled_task_store

    async def conversation_model(conversation: Any) -> str | None:
        return await session_model(app.state, conversation, deps.conversation_store)

    service = Lookout(
        store=SqlAlchemyLookoutStore(deps.storage_location),
        tasks=tasks,
        conversations=deps.conversation_store,
        judge_caller=functools.partial(
            judging.resolve_caller, conversation_model=conversation_model
        ),
    )
    service.set_topic_seams(*_topic_seams)
    service.set_rhythm(_rhythm)
    _service = service
    app.state.lookout = service
    register_run_brief(LOOKOUT_KIND, service.run_brief)
    for kind in (LOOKOUT_KIND, *_result_kinds):
        register_run_result(kind, service.on_run_result)
    # A wake's run waits in the work queue as a ``lookout_wake`` job keyed by the wake id.
    work_queue.register_handler(LOOKOUT_KIND, service.run_job)


def _jobs(app: FastAPI) -> list[BackgroundJob]:
    """The ticker that wakes due Lookouts, when the app can launch runs."""
    service = getattr(app.state, "lookout", None)
    if service is None or getattr(app.state, "scheduled_task_scheduler", None) is None:
        return []
    return [service]


FEATURE = Feature(name="lookout", tools=lambda _labels, _ctx: [], install=_install, jobs=_jobs)
