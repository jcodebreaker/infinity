"""Native-subagent routing: the server-side policy.

The runner's loopback relay (:mod:`omnigent.runner.subagent_routing`) forwards each
native-subagent spawn to the server relay route
(``omnigent/server/routes/sessions/routes_hooks.py``), which calls
:func:`resolve_subagent_route` here. The policy runs where ``RuntimeCaps.routing_client``
lives, so it stays in the server; the request, decision and record shapes it shares with the
runner live beside the relay.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from omnigent.runner.subagent_routing import (
    SubagentRouteDecision,
    SubagentRouteRequest,
    _opt_str,
    _unavailable_decision,
    harness_family,
    model_in_family,
)

if TYPE_CHECKING:
    from omnigent.entities.conversation import RoutingDecisionData

_logger = logging.getLogger(__name__)

_SCOPE = "native_subagent"
_PROMPT_CAP = 4000
#: Task label scored for a codex spawn that carries no prompt and no
#: task/agent name. Such a spawn has nothing of its own to score; rather than
#: skip the router and let it inherit the parent's — possibly expensive —
#: model, route it on this placeholder so it lands on the router's floor arm.
#: Mirrors ucode PR 251's ``default_task_label`` ("Codex subagent task").
#: Every unnamed spawn scores the same string, so they share one (floor)
#: arm — a cheap default, not per-spawn intelligence.
_PLACEHOLDER_TASK = "Codex subagent task"

# Cross-harness counterpart used to offer the router a second family, and
# to name the redirect target when it picks from that family.
_COUNTERPART_HARNESS: dict[str, str] = {
    "claude-native": "codex-native",
    "codex-native": "claude-native",
}


def candidate_models(
    harness: str,
    *,
    cross_harness: bool = False,
    catalog: Mapping[str, list[str]] | None = None,
    allow_static_fallback: bool = True,
) -> dict[str, list[str]]:
    """Build the harness → models map offered to the router.

    In-family by default: a Claude Code session must not be told to move a
    subagent to Codex, so only the requesting harness's models are offered
    and the router's scenario stays that one family. Auto-harness sessions
    already let the router pick the family, so they also get the
    cross-harness counterpart — the only case where a ``redirect`` verdict
    can fire.

    :param harness: Requesting harness id, e.g. ``"codex-native"``.
    :param cross_harness: ``True`` to also offer the counterpart family.
    :param catalog: Live per-session model catalog keyed by worker name
        (:func:`omnigent.server.smart_routing.fetch_runner_models`). Preferred
        over the static table so a model generation the workspace serves today
        isn't treated as unservable; the static table fills any harness the
        catalog has no row for.
    :param allow_static_fallback: Whether the static :func:`infer_models` table
        may fill a harness the catalog has no row for. Off the AI Gateway it may
        not: every id in that table is a ``databricks-*`` endpoint the spawn
        could not reach, so the catalog is the only provider-accurate source.
    :returns: Harness → model ids, cheapest first, empty entries dropped.
    """
    from omnigent.server.smart_routing import (
        apply_servable_alias,
        catalog_models_for_harness,
        infer_models,
    )

    offered = (harness, _COUNTERPART_HARNESS.get(harness)) if cross_harness else (harness,)
    result: dict[str, list[str]] = {}
    for candidate in offered:
        if candidate is None or candidate in result:
            continue
        from_catalog = catalog_models_for_harness(
            catalog, candidate, allow_self=candidate == harness
        )
        static = infer_models(candidate) if allow_static_fallback else None
        models = from_catalog or static or []
        # A catalog row can hold models the harness cannot speak (a codex
        # ``"self"`` row lists Claude ids too), which earn a hard
        # ``model_family_mismatch`` at dispatch.
        family = harness_family(candidate)
        models = [m for m in models if model_in_family(family, m)]
        # Offer each model under the spelling the gateway serves, so the
        # spawn's model matches what routing resolves to; dedupe when the
        # catalog carries both spellings.
        models = list(dict.fromkeys(apply_servable_alias(m) for m in models))
        if models:
            result[candidate] = models
    return result


def decision_record(
    req: SubagentRouteRequest,
    decision: SubagentRouteDecision,
) -> RoutingDecisionData:
    """Build the transcript payload for *decision*.

    :param req: The spawn that was routed.
    :param decision: The verdict returned to the hook.
    :returns: Item data ready for :func:`persist_subagent_decision`.
    """
    from omnigent.entities.conversation import RoutingDecisionData

    # No routed model means the verdict left ``tool_input`` alone, so the spawn
    # runs on whatever it asked for and only then on the inherited model.
    model = decision.model or req.requested_model or req.parent_model or "unrouted"
    # An explicit ask the router did NOT land is an override attempt worth
    # recording; an ask it happened to match is just the model, and stays out
    # of the field.
    attempted = None if _requested_match(req, model) else req.requested_model
    return RoutingDecisionData(
        model=model,
        applied=decision.action in ("rewrite", "redirect"),
        rationale=decision.rationale,
        decision_id=decision.decision_id,
        harness=decision.harness or req.harness,
        raw_model=decision.raw_model,
        agent=req.task_name or None,
        task_description=req.task_description,
        scope=_SCOPE,
        attempted_override=attempted,
        router_source=decision.router_source,
    )


def _routing_task(req: SubagentRouteRequest) -> str:
    """Return the task text the router should score.

    Precedence: a real prompt (claude) beats the ``task_name`` /
    ``agent_name`` label (both fold into ``task_name`` in the hook), which
    beats the placeholder. A codex spawn with neither prompt nor name still
    routes — on :data:`_PLACEHOLDER_TASK` — so it lands on the router's floor
    arm instead of inheriting the parent's model.

    :param req: Spawn request.
    :returns: Task text; never empty.
    """
    if req.prompt:
        return req.prompt[:_PROMPT_CAP]
    if req.task_name:
        return req.task_name[:_PROMPT_CAP]
    return _PLACEHOLDER_TASK


def _target_harness(
    req_harness: str, picked_harness: str | None, picked_family: str | None
) -> str:
    """Name the harness a picked model should run on."""
    if picked_family is not None and picked_family == harness_family(req_harness):
        return req_harness
    counterpart = _COUNTERPART_HARNESS.get(req_harness)
    if counterpart is not None and harness_family(counterpart) == picked_family:
        return counterpart
    return picked_harness or counterpart or req_harness


async def resolve_subagent_route(
    session_id: str,
    req: SubagentRouteRequest,
    *,
    caps: Any = None,
    available_models: dict[str, list[str]] | None = None,
    catalog: Mapping[str, list[str]] | None = None,
    cross_harness: bool = False,
    gateway_backed: bool = True,
    allow_static_fallback: bool = True,
    persist: Callable[[RoutingDecisionData], Awaitable[None]] | None = None,
) -> SubagentRouteDecision:
    """Decide what happens to one native-subagent spawn.

    :param session_id: Parent session/conversation identifier.
    :param req: The spawn awaiting a verdict.
    :param caps: ``RuntimeCaps``-shaped object. ``None`` reads the
        process-global caps.
    :param available_models: Candidate harness → models map. ``None``
        derives it from the requesting harness.
    :param catalog: Live per-session model catalog, preferred over the
        static table when deriving candidates. Ignored when
        *available_models* is given.
    :param cross_harness: ``True`` when the session may move a subagent to
        the counterpart harness family (auto-harness sessions only).
        Ignored when *available_models* is given.
    :param gateway_backed: Whether every harness family on offer resolves
        AI-Gateway-backed inference on the parent's host. ``False`` takes the
        external router out of play and the built-in judge answers instead.
    :param allow_static_fallback: Whether the static :func:`infer_models` table
        may supply candidates. Callers pass ``gateway_backed``: off the gateway
        its ``databricks-*`` ids are unreachable from the spawn.
    :param persist: Coroutine that records the decision in the
        transcript. ``None`` skips persistence (unit tests, dry runs).
    :returns: The verdict the hook script enforces.
    """
    if caps is None:
        from omnigent.runtime import get_caps

        caps = get_caps()

    decision = await _decide(
        session_id,
        req,
        caps,
        available_models,
        catalog,
        cross_harness,
        gateway_backed=gateway_backed,
        allow_static_fallback=allow_static_fallback,
    )
    if persist is not None:
        try:
            await persist(decision_record(req, decision))
        except Exception:
            _logger.exception("route-subagent: decision persist failed for session=%s", session_id)
    return decision


async def _decide(
    session_id: str,
    req: SubagentRouteRequest,
    caps: Any,
    available_models: dict[str, list[str]] | None,
    catalog: Mapping[str, list[str]] | None,
    cross_harness: bool,
    *,
    gateway_backed: bool = True,
    allow_static_fallback: bool = True,
) -> SubagentRouteDecision:
    if req.fork:
        return SubagentRouteDecision(
            action="allow",
            rationale="Fork keeps the parent's model; forks are not routed",
            model=req.parent_model,
        )

    # Always a task to score: a codex spawn with no prompt and no name routes
    # on the placeholder (see ``_routing_task``) so it lands on the router's
    # floor arm rather than inheriting the parent's — possibly expensive —
    # model. Mirrors ucode PR 251's ``default_task_label``. This reverses the
    # earlier skip-the-router-and-inherit verdict: scoring the placeholder is
    # a cheap-default win over an unbounded inherit, and the recorded
    # ``raw_model`` / ``applied`` stay truthful to whatever the router picks.
    task = _routing_task(req)

    from omnigent.server.routing_backend import (
        backends_from_caps,
        route_with_fallback,
        select_router,
    )

    backends = backends_from_caps(caps)
    if select_router(backends, gateway_backed=gateway_backed) is None:
        return _unavailable_decision("no routing client configured")

    candidates = (
        available_models
        if available_models is not None
        else candidate_models(
            req.harness,
            cross_harness=cross_harness,
            catalog=catalog,
            allow_static_fallback=allow_static_fallback,
        )
    )
    if not candidates:
        return _unavailable_decision(f"no candidate models for harness {req.harness}")

    # An explicit ``requested_model`` never short-circuits this call: the
    # router's pick is the verdict, and the ask only decides how the rationale
    # words it. Fail-open still preserves the ask — a router outage returns an
    # allow with no model, the hook leaves ``tool_input`` untouched, and the
    # spawn runs on exactly the model it named.
    raised: str | None = None
    client: Any = backends.any()  # type: ignore[explicit-any]
    source: str | None = None
    result = None
    try:
        call = await route_with_fallback(backends, task, candidates, gateway_backed=gateway_backed)
    except Exception as exc:
        _logger.warning(
            "route-subagent: router call failed for session=%s", session_id, exc_info=True
        )
        # A client that raises past its own error reporting leaves
        # ``last_error`` unset; without this the chip said only "no verdict"
        # and the cause lived in the server log alone.
        from omnigent.server.smart_routing import failure_detail

        raised = f"router call failed: {failure_detail(exc)}"
    else:
        if call is not None:
            result, client, source = call.result, call.client, call.source
    if result is None or not getattr(result, "model", None):
        detail = (
            _opt_str(getattr(client, "last_error", None)) or raised or "router returned no verdict"
        )
        return _unavailable_decision(detail)

    return replace(_decision_from_result(req, result, candidates), router_source=source)


def _requested_match(req: SubagentRouteRequest, model: str | None) -> bool:
    """Report whether the spawn's explicit ask names the same arm as *model*.

    Matches on the bare arm so any spelling of the ask (catalog id, codex
    slug, servable alias, ``[1m]`` variant) compares against the servable
    id the verdict carries.

    :param req: The spawn that was routed.
    :param model: The model the spawn will actually run on.
    :returns: ``True`` when the spawn asked for that same arm.
    """
    if not req.requested_model or not model:
        return False
    from omnigent.server.smart_routing import _bare_id

    return _bare_id(req.requested_model) == _bare_id(model)


def _ask_note(req: SubagentRouteRequest, model: str) -> str:
    """Say what became of the spawn's explicit model ask.

    Appended after the router's own rationale, which stays first and intact.
    "honored" is only ever claimed on a match — the router strictly decides,
    so an ask is honored by coincidence, never by carve-out.

    :param req: The spawn that was routed.
    :param model: The model the router picked.
    :returns: A sentence to append, or ``""`` when the spawn asked for nothing.
    """
    if not req.requested_model:
        return ""
    if _requested_match(req, model):
        return f" Spawn requested {req.requested_model}; honored — the router picked the same arm."
    return f" Spawn requested {req.requested_model}; overridden — the router picked {model}."


def _decision_from_result(
    req: SubagentRouteRequest,
    result: Any,
    candidates: Mapping[str, list[str]],
) -> SubagentRouteDecision:
    model = result.model
    rationale = getattr(result, "rationale", "") or ""
    # Only report a raw pick that actually differs from the resolved model, so
    # the raw_model field means "the router asked for something else". A
    # prefix-only spelling difference is the same arm, not a substitution.
    from omnigent.server.smart_routing import _bare_id

    raw = _opt_str(getattr(result, "raw_model", None))
    raw_model = raw if raw and _bare_id(raw) != _bare_id(model) else None
    offered = {m for models in candidates.values() for m in models}
    if offered and model not in offered:
        # Never let an unoffered pick through: "didn't spawn" beats "wrong model".
        return SubagentRouteDecision(
            action="deny",
            rationale=f"Router picked {model}, which this harness cannot run",
            raw_model=raw_model or model,
        )

    picked_harness = _opt_str(getattr(result, "harness", None))
    family = harness_family(picked_harness) if picked_harness else None
    if family is None:
        for harness_id, models in candidates.items():
            if model in models:
                family = harness_family(harness_id)
                break
    target = _target_harness(req.harness, picked_harness, family)
    # The deny above carries no note: nothing spawned, so there is no ask to
    # call honored or overridden.
    note = _ask_note(req, model)

    if target == req.harness:
        if req.parent_model is not None and model == req.parent_model:
            return SubagentRouteDecision(
                action="allow",
                rationale=(rationale or "Router kept the parent model") + note,
                model=model,
                raw_model=raw_model,
            )
        return SubagentRouteDecision(
            action="rewrite",
            rationale=(rationale or f"Router selected {model}") + note,
            model=model,
            raw_model=raw_model,
        )
    return SubagentRouteDecision(
        action="redirect",
        rationale=(rationale or f"Router selected {target}/{model}") + note,
        model=model,
        harness=target,
        raw_model=raw_model,
    )
