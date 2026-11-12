"""The owner's monthly model budget as a policy: stop at the limit, or ask to continue.

A thin sibling of :func:`omnigent.policies.builtins.cost.user_daily_cost_budget`, not a mode of
it: that factory is a daily, admin-configured *downgrade gate* with unpriced-model prompts, while
this one is month-based, takes its limits from the person's and the organization's settings
(:mod:`omnigent.superchat.models.budget`) and must not nag about unpriced models. It shares the
mechanics that matter: the owner rollup seed in ``event["context"]["user_daily_cost"]``, the
request/tool-call gates and an ASK whose approval the engine records per owner through a reserved
``state_updates`` key, so it holds across all the owner's sessions.

``at_limit="stop"`` DENYs once spend reaches the limit. ``"ask"`` ASKs at the limit and then
each time spend grows by :data:`ASK_STEP` of the limit. The person's limit and the
organization's are checked, and their approvals remembered for the rest of the month,
independently; a stop beats an ask.
"""

from __future__ import annotations

import math
from typing import Any

from omnigent.policies.builtins.cost import _ALLOW, _GATED_PHASES
from omnigent.policies.schema import (
    OWNER_BUDGET_ASK_APPROVED_STATE_KEY,
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
)

# Spend growth, as a fraction of the limit, between asks after a person continues past it.
ASK_STEP = 0.25


def ask_level(spent: float, limit: float) -> float | None:
    """The check-in level *spent* has reached: ``1.0`` at the limit, then every :data:`ASK_STEP`.

    :returns: ``None`` below the limit.
    """
    ratio = spent / limit
    if ratio < 1.0:
        return None
    steps = math.floor((ratio - 1.0) / ASK_STEP + 1e-9)
    return round(1.0 + steps * ASK_STEP, 6)


def _number(seed: dict[str, Any], key: str) -> float | None:
    value = seed.get(key)
    return (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    )


def _subject(scope: str, limit: float) -> str:
    return (
        f"You've reached your ${limit:,.2f} monthly budget"
        if scope == "user"
        else (f"Your organization has reached its ${limit:,.2f} monthly budget")
    )


def owner_model_budget() -> PolicyCallable:
    """Factory: gate a session on its owner's and organization's monthly model spend.

    Takes no arguments: the limits arrive in the event context, seeded by the engine from the
    saved budgets, so a person changing their limit applies to their next turn. Abstains when
    nothing is seeded.

    :returns: The policy callable.
    """

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        if event.get("type") not in _GATED_PHASES:
            return _ALLOW
        seed = (event.get("context") or {}).get("user_daily_cost") or {}
        if not seed.get("month_period"):
            return _ALLOW
        asks: list[tuple[float, str, float]] = []
        for scope, prefix in (("user", "month"), ("org", "org")):
            limit = _number(seed, f"{prefix}_limit_usd")
            spent = _number(seed, f"{prefix}_cost_usd")
            if limit is None or spent is None or limit <= 0 or spent < limit:
                continue
            if seed.get(f"{prefix}_at_limit") == "stop":
                hint = (
                    "Raise or remove the limit in Settings to continue."
                    if scope == "user"
                    else "Ask an admin to raise it."
                )
                return {"result": "DENY", "reason": f"{_subject(scope, limit)}. {hint}"}
            level = ask_level(spent, limit)
            if level is not None and level > (_number(seed, f"{scope}_ask_level") or 0.0):
                asks.append((level, scope, limit))
        if not asks:
            return _ALLOW
        level, scope, limit = max(asks, key=lambda ask: ask[0])
        return {
            "result": "ASK",
            "reason": f"{_subject(scope, limit)}. Continue?",
            "state_updates": [
                {
                    "key": OWNER_BUDGET_ASK_APPROVED_STATE_KEY,
                    "action": "set",
                    "value": {"scope": scope, "level": level},
                }
            ],
        }

    return evaluate
