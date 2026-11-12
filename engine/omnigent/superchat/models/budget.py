"""Monthly model budgets: a person's own limit and the organization's, one shared ledger.

A budget is ``{monthly_limit_usd: number|null, at_limit: "stop"|"ask"}`` kept as one JSON row in
the generic ``preferences`` table: the person's under their user id, the organization's under the
reserved :data:`WORKSPACE_OWNER` (no person can hold that id). Spend is not stored here: it is the
``user_daily_cost`` rollup every turn already feeds, summed from the first of the UTC month
(:func:`month_start`); the organization's spend is that same rollup summed over all users.

Enforcement lives in two places that read this module: the auto-injected ``owner_model_budget``
policy (:func:`seed_for_policy`, asks or denies a turn) and the model proxy (:meth:`stop_breach`,
the backstop that also covers background runs). ``at_limit="stop"`` refuses at the limit;
``"ask"`` (the default) asks the person to continue, see
:func:`omnigent.policies.builtins.model_budget.owner_model_budget`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cachetools

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
    utc_day,
)
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_WORKSPACE

PREFERENCE_KEY = "model_budget"
APPROVAL_KEY = "model_budget_approval"
# Owner id of the organization's row: the reserved identity ``RESERVED_USER_WORKSPACE``, which
# authentication never accepts as a person, so no user can share the organization's row.
WORKSPACE_OWNER = RESERVED_USER_WORKSPACE
AT_LIMIT_VALUES = ("stop", "ask")
DEFAULT_AT_LIMIT = "ask"
_CACHE_TTL_SECONDS = 5


@dataclass(frozen=True)
class Budget:
    """One budget: a monthly USD limit (``None`` = none) and what happens at it."""

    monthly_limit_usd: float | None = None
    at_limit: str = DEFAULT_AT_LIMIT

    @property
    def active(self) -> bool:
        return self.monthly_limit_usd is not None


def parse_budget(monthly_limit_usd: Any, at_limit: Any) -> Budget:
    """Validate request input into a :class:`Budget`.

    :raises OmnigentError: ``INVALID_INPUT`` for a limit that is not a positive finite number
        (or ``null``), or an ``at_limit`` other than ``stop``/``ask``.
    """
    limit: float | None = None
    if monthly_limit_usd is not None:
        if (
            isinstance(monthly_limit_usd, bool)
            or not isinstance(monthly_limit_usd, (int, float))
            or not math.isfinite(monthly_limit_usd)
            or monthly_limit_usd <= 0
        ):
            raise OmnigentError(
                "monthly_limit_usd must be a positive number or null", code=ErrorCode.INVALID_INPUT
            )
        limit = round(float(monthly_limit_usd), 2)
        if limit <= 0:
            raise OmnigentError(
                "monthly_limit_usd must be at least 0.01", code=ErrorCode.INVALID_INPUT
            )
    if at_limit not in AT_LIMIT_VALUES:
        raise OmnigentError(
            f"at_limit must be one of: {', '.join(AT_LIMIT_VALUES)}", code=ErrorCode.INVALID_INPUT
        )
    return Budget(limit, at_limit)


def _decode(raw: str | None) -> Budget:
    try:
        value = json.loads(raw) if raw else {}
        return parse_budget(
            value.get("monthly_limit_usd"), value.get("at_limit", DEFAULT_AT_LIMIT)
        )
    except (ValueError, AttributeError, OmnigentError):
        return Budget()


def month_start(epoch: int | None = None) -> str:
    """First UTC day of the month containing *epoch* (now), as ``"YYYY-MM-01"``."""
    return utc_day(now_epoch() if epoch is None else epoch)[:7] + "-01"


class ModelBudgetStore:
    """Budgets by owner (a user id or :data:`WORKSPACE_OWNER`) in the ``preferences`` table."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.model_budget"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )
        # A turn builds a policy engine per tool call and the proxy checks every model call:
        # a short TTL keeps both to a dict lookup. ``set`` evicts on this replica at once.
        self._cache: WorkspaceScopedCache[str, Budget] = WorkspaceScopedCache(
            lambda: cachetools.TTLCache(maxsize=2048, ttl=_CACHE_TTL_SECONDS)
        )
        self._any: WorkspaceScopedCache[str, bool] = WorkspaceScopedCache(
            lambda: cachetools.TTLCache(maxsize=8, ttl=_CACHE_TTL_SECONDS)
        )

    def get(self, owner: str) -> Budget:
        """The owner's budget; no limit when never set or unreadable."""
        cached = self._cache.get(owner)
        if cached is not None:
            return cached
        with self._session("select_model_budget") as session:
            row = session.get(SqlPreference, (current_workspace_id(), owner, PREFERENCE_KEY))
            raw = row.value if row is not None else None
        budget = _decode(raw)
        self._cache[owner] = budget
        return budget

    def set(self, owner: str, budget: Budget) -> None:
        """Replace the owner's budget; one with no limit and the default action removes the row."""

        def write(session: Any) -> None:
            pk = (current_workspace_id(), owner, PREFERENCE_KEY)
            row = session.get(SqlPreference, pk)
            value = json.dumps(
                {"monthly_limit_usd": budget.monthly_limit_usd, "at_limit": budget.at_limit}
            )
            if not budget.active and budget.at_limit == DEFAULT_AT_LIMIT:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(SqlPreference(user_id=owner, key=PREFERENCE_KEY, value=value))
            else:
                row.value = value

        run_write_transaction(self._session_immediate, "upsert_model_budget", write)
        self._cache.pop(owner, None)
        self._any.clear()

    def get_approval(self, owner: str, month: str) -> dict[str, float]:
        """The levels *owner* approved continuing past this *month* (``"YYYY-MM"``), by scope.

        :returns: ``{"user": level, "org": level}`` with 0.0 for none; an approval stored for
            another month is ignored, so approvals lapse when the month turns.
        """
        with self._session("select_model_budget_approval") as session:
            row = session.get(SqlPreference, (current_workspace_id(), owner, APPROVAL_KEY))
            raw = row.value if row is not None else None
        try:
            value = json.loads(raw) if raw else {}
        except ValueError:
            value = {}
        if not isinstance(value, dict) or value.get("month") != month:
            return {"user": 0.0, "org": 0.0}
        return {scope: float(value.get(f"{scope}_level") or 0.0) for scope in ("user", "org")}

    def set_approval(self, owner: str, month: str, scope: str, level: float) -> None:
        """Remember that *owner* approved continuing past *scope*'s limit up to *level*."""

        def write(session: Any) -> None:
            pk = (current_workspace_id(), owner, APPROVAL_KEY)
            row = session.get(SqlPreference, pk)
            value: dict[str, Any] = {"month": month, "user_level": None, "org_level": None}
            if row is not None:
                try:
                    stored = json.loads(row.value)
                except ValueError:
                    stored = {}
                if isinstance(stored, dict) and stored.get("month") == month:
                    value.update(
                        user_level=stored.get("user_level"), org_level=stored.get("org_level")
                    )
            value[f"{scope}_level"] = level
            if row is None:
                session.add(
                    SqlPreference(user_id=owner, key=APPROVAL_KEY, value=json.dumps(value))
                )
            else:
                row.value = json.dumps(value)

        run_write_transaction(self._session_immediate, "upsert_model_budget_approval", write)

    def any_limit(self) -> bool:
        """Whether anyone in the workspace has a limit (the cheap gate before per-owner work)."""
        cached = self._any.get("any")
        if cached is not None:
            return cached
        from sqlalchemy import select

        with self._session("select_any_model_budget") as session:
            values = session.execute(
                select(SqlPreference.value)
                .where(SqlPreference.workspace_id == current_workspace_id())
                .where(SqlPreference.key == PREFERENCE_KEY)
            ).scalars()
            found = any(_decode(v).active for v in values)
        self._any["any"] = found
        return found


@dataclass(frozen=True)
class Breach:
    """A limit that is used up: whose (``user``/``org``), how much, and what it does."""

    scope: str
    limit_usd: float
    spent_usd: float
    at_limit: str

    @property
    def message(self) -> str:
        if self.scope == "org":
            return (
                f"Your organization has used its ${self.limit_usd:,.2f} monthly model budget. "
                "Ask an admin to raise it."
            )
        return (
            f"You've used your ${self.limit_usd:,.2f} monthly model budget. "
            "Raise or remove the limit in Settings to continue."
        )


class ModelBudgets:
    """Budgets plus the spend ledger: what the routes, the policy seed and the proxy all read."""

    def __init__(self, store: ModelBudgetStore, ledger: Any) -> None:
        """:param ledger: The conversation store (``sum_daily_cost``/``sum_workspace_cost``)."""
        self.store = store
        self._ledger = ledger

    def spent(self, owner: str) -> float:
        """The owner's spend since the first of this UTC month."""
        return self._ledger.sum_daily_cost(owner, month_start())

    def org_spent(self) -> float:
        """Everyone's spend since the first of this UTC month."""
        return self._ledger.sum_workspace_cost(month_start())

    def record_spend(self, owner: str, usd: float) -> None:
        """Add *usd* to the owner's rollup for today (the same row a priced turn feeds)."""
        self._ledger.add_daily_cost(owner, utc_day(now_epoch()), usd)

    def breaches(self, owner: str, *, only_stop: bool = False) -> list[Breach]:
        """Limits that are used up for *owner*, the person's first then the organization's."""
        found: list[Breach] = []
        for scope, budget_owner, spend in (
            ("user", owner, self.spent),
            ("org", WORKSPACE_OWNER, lambda _o: self.org_spent()),
        ):
            budget = self.store.get(budget_owner)
            if not budget.active or (only_stop and budget.at_limit != "stop"):
                continue
            assert budget.monthly_limit_usd is not None
            spent = spend(owner)
            if spent >= budget.monthly_limit_usd:
                found.append(Breach(scope, budget.monthly_limit_usd, spent, budget.at_limit))
        return found

    def stop_breach(self, owner: str) -> Breach | None:
        """The first used-up limit set to stop, or ``None``; the proxy's backstop check."""
        if not self.store.any_limit():
            return None
        found = self.breaches(owner, only_stop=True)
        return found[0] if found else None

    def any_limit(self) -> bool:
        """Whether anyone in the workspace has a limit (the cheap gate before per-owner work)."""
        return self.store.any_limit()

    def record_approval(self, owner: str, scope: str, level: float) -> None:
        """Remember that *owner* approved going past *scope*'s limit up to *level* this month."""
        self.store.set_approval(owner, month_start()[:7], scope, level)

    def seed_for_policy(self, owner: str) -> dict[str, float | str] | None:
        """The ``owner_model_budget`` policy's context, or ``None`` when no limit applies."""
        if not self.store.any_limit():
            return None
        mine, org = self.store.get(owner), self.store.get(WORKSPACE_OWNER)
        if not (mine.active or org.active):
            return None
        approved = self.store.get_approval(owner, month_start()[:7])
        seed: dict[str, float | str] = {
            "month_period": month_start(),
            "user_ask_level": approved["user"],
            "org_ask_level": approved["org"],
        }
        if mine.active:
            assert mine.monthly_limit_usd is not None
            seed.update(
                month_limit_usd=mine.monthly_limit_usd,
                month_at_limit=mine.at_limit,
                month_cost_usd=self.spent(owner),
            )
        if org.active:
            assert org.monthly_limit_usd is not None
            seed.update(
                org_limit_usd=org.monthly_limit_usd,
                org_at_limit=org.at_limit,
                org_cost_usd=self.org_spent(),
            )
        return seed


_budgets: ModelBudgets | None = None


def bind_budgets(budgets: ModelBudgets | None) -> None:
    """Bind the server's budgets so the policy builder can find them (``None`` unbinds)."""
    global _budgets
    _budgets = budgets


def get_budgets() -> ModelBudgets | None:
    """The bound budgets, or ``None`` when the server has none (no limit can then apply)."""
    return _budgets


def current_month_label() -> str:
    """The current UTC month as ``"YYYY-MM"`` (spoken in messages and the usage report)."""
    return datetime.fromtimestamp(now_epoch(), tz=timezone.utc).strftime("%Y-%m")
