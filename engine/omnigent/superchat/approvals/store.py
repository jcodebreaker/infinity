"""Approval store: standing rules, the daily spending cap and spend, and approval prompts.

All rows are per owner. A pending approval is persisted so a restart does not forget it; nothing
here ever turns an unanswered prompt into a yes. A decided prompt keeps its row with its
``status`` (``approved``, ``denied`` or ``expired``), who decided and when, for the Inbox's
History (ADR 0036, amended); everything that asks what waits reads ``pending`` rows only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlApprovalPending,
    SqlApprovalRule,
    SqlApprovalSettings,
    SqlApprovalSpend,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)

LOCAL_OWNER = "local"


@dataclass(frozen=True)
class ApprovalRule:
    """A standing rule: ``category`` actions on a ``target`` pattern get ``decision``."""

    id: str
    user_id: str
    category: str
    target: str
    label: str
    decision: str
    created_at: int


@dataclass(frozen=True)
class PendingApproval:
    """An approval prompt still waiting on the person."""

    elicitation_id: str
    session_id: str
    user_id: str | None
    category: str
    target: str
    summary: str
    amount_usd: float | None
    event: str
    created_at: int
    status: str = "pending"
    decided_by: str | None = None
    decided_at: int | None = None


#: The statuses a decided prompt can end in.
DECIDED = ("approved", "denied", "expired")


def _rule(row: SqlApprovalRule) -> ApprovalRule:
    return ApprovalRule(
        id=row.id,
        user_id=row.user_id,
        category=row.category,
        target=row.target,
        label=row.label,
        decision=row.decision,
        created_at=row.created_at,
    )


def _pending(row: SqlApprovalPending) -> PendingApproval:
    return PendingApproval(
        elicitation_id=row.elicitation_id,
        session_id=row.session_id,
        user_id=row.user_id,
        category=row.category,
        target=row.target,
        summary=row.summary,
        amount_usd=row.amount_usd,
        event=row.event,
        created_at=row.created_at,
        status=row.status,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
    )


class SqlAlchemyApprovalStore:
    """SQLAlchemy-backed approval store (owner-scoped)."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.approval_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.approval_store", immediate=True
        )

    # ── standing rules ─────────────────────────────────────────────────────

    def list_rules(self, user_id: str) -> list[ApprovalRule]:
        """The owner's standing rules, newest first."""
        with self._session("list_approval_rules") as session:
            rows = (
                session.execute(
                    select(SqlApprovalRule)
                    .where(
                        SqlApprovalRule.workspace_id == current_workspace_id(),
                        SqlApprovalRule.user_id == user_id,
                    )
                    .order_by(SqlApprovalRule.created_at.desc(), SqlApprovalRule.id)
                )
                .scalars()
                .all()
            )
            return [_rule(row) for row in rows]

    def create_rule(
        self, user_id: str, *, category: str, target: str, label: str, decision: str = "allow"
    ) -> ApprovalRule:
        """Add a rule; an identical (category, target, decision) one is returned as is."""
        existing = next(
            (
                r
                for r in self.list_rules(user_id)
                if r.category == category
                and r.target.lower() == target.lower()
                and r.decision == decision
            ),
            None,
        )
        if existing is not None:
            return existing

        def write(session: Session) -> ApprovalRule:
            row = SqlApprovalRule(
                id=uuid.uuid4().hex,
                user_id=user_id,
                category=category,
                target=target[:256],
                label=label[:256],
                decision=decision,
                created_at=now_epoch(),
            )
            session.add(row)
            session.flush()
            return _rule(row)

        return run_write_transaction(self._session_immediate, "insert_approval_rule", write)

    def delete_rule(self, user_id: str, rule_id: str) -> bool:
        """Revoke a rule; ``False`` when it does not exist (or is not this owner's)."""

        def write(session: Session) -> bool:
            result = session.execute(
                delete(SqlApprovalRule).where(
                    SqlApprovalRule.workspace_id == current_workspace_id(),
                    SqlApprovalRule.user_id == user_id,
                    SqlApprovalRule.id == rule_id,
                )
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

        return run_write_transaction(self._session_immediate, "delete_approval_rule", write)

    # ── daily spending cap + spend ─────────────────────────────────────────

    def get_cap(self, user_id: str) -> float:
        """The owner's daily spending cap in USD (``0`` = always ask)."""
        with self._session("get_approval_cap") as session:
            row = session.get(SqlApprovalSettings, (current_workspace_id(), user_id))
            return float(row.daily_cap_usd) if row is not None else 0.0

    def set_cap(self, user_id: str, cap_usd: float) -> float:
        """Set the owner's daily spending cap."""

        def write(session: Session) -> float:
            row = session.get(SqlApprovalSettings, (current_workspace_id(), user_id))
            if row is None:
                session.add(
                    SqlApprovalSettings(
                        user_id=user_id, daily_cap_usd=cap_usd, updated_at=now_epoch()
                    )
                )
            else:
                row.daily_cap_usd = cap_usd
                row.updated_at = now_epoch()
            return cap_usd

        return run_write_transaction(self._session_immediate, "set_approval_cap", write)

    def get_spend(self, user_id: str, day: str) -> float:
        """USD recorded as spent by the owner on ``day`` (UTC, ``YYYY-MM-DD``)."""
        with self._session("get_approval_spend") as session:
            row = session.get(SqlApprovalSpend, (current_workspace_id(), user_id, day))
            return float(row.usd) if row is not None else 0.0

    def add_spend(self, user_id: str, day: str, usd: float) -> float:
        """Record ``usd`` as spent today; returns the new total."""

        def write(session: Session) -> float:
            row = session.get(SqlApprovalSpend, (current_workspace_id(), user_id, day))
            if row is None:
                row = SqlApprovalSpend(user_id=user_id, day=day, usd=usd)
                session.add(row)
            else:
                row.usd = float(row.usd) + usd
            return float(row.usd)

        return run_write_transaction(self._session_immediate, "add_approval_spend", write)

    # ── pending prompts ────────────────────────────────────────────────────

    def put_pending(
        self,
        elicitation_id: str,
        session_id: str,
        *,
        user_id: str | None,
        category: str,
        target: str,
        summary: str,
        amount_usd: float | None,
        event: str,
    ) -> None:
        """Persist (or refresh) a pending approval."""

        def write(session: Session) -> None:
            row = session.get(SqlApprovalPending, (current_workspace_id(), elicitation_id))
            if row is None:
                session.add(
                    SqlApprovalPending(
                        elicitation_id=elicitation_id,
                        session_id=session_id,
                        user_id=user_id,
                        category=category,
                        target=target[:256],
                        summary=summary,
                        amount_usd=amount_usd,
                        event=event,
                        created_at=now_epoch(),
                    )
                )
            elif row.status == "pending":
                row.event = event

        run_write_transaction(self._session_immediate, "put_approval_pending", write)

    def get_pending(self, elicitation_id: str) -> PendingApproval | None:
        """One approval still waiting, or ``None`` (none, or already decided)."""
        with self._session("get_approval_pending") as session:
            row = session.get(SqlApprovalPending, (current_workspace_id(), elicitation_id))
            return _pending(row) if row is not None and row.status == "pending" else None

    def list_pending(
        self, *, user_id: str | None = None, session_id: str | None = None
    ) -> list[PendingApproval]:
        """Pending approvals, oldest first; optionally for one owner or one session."""
        stmt = select(SqlApprovalPending).where(
            SqlApprovalPending.workspace_id == current_workspace_id(),
            SqlApprovalPending.status == "pending",
        )
        if user_id is not None:
            stmt = stmt.where(SqlApprovalPending.user_id == user_id)
        if session_id is not None:
            stmt = stmt.where(SqlApprovalPending.session_id == session_id)
        with self._session("list_approval_pending") as session:
            rows = session.execute(stmt.order_by(SqlApprovalPending.created_at)).scalars().all()
            return [_pending(row) for row in rows]

    def decide(
        self,
        elicitation_id: str,
        status: str,
        decided_by: str | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        """End a waiting approval: ``approved`` or ``denied`` by *decided_by*, or ``expired``
        (nobody can answer it any more). A decision is never overwritten, except that the
        person's answer replaces an ``expired`` its own resolution recorded a moment before.
        With *session_id*, only an approval asked in that session is decided."""
        if status not in DECIDED:
            raise ValueError(status)
        replaceable = ("pending",) if status == "expired" else ("pending", "expired")

        def write(session: Session) -> None:
            session.execute(
                update(SqlApprovalPending)
                .where(
                    SqlApprovalPending.workspace_id == current_workspace_id(),
                    SqlApprovalPending.elicitation_id == elicitation_id,
                    SqlApprovalPending.status.in_(replaceable),
                    *(
                        (SqlApprovalPending.session_id == session_id,)
                        if session_id is not None
                        else ()
                    ),
                )
                .values(status=status, decided_by=decided_by, decided_at=now_epoch())
            )

        run_write_transaction(self._session_immediate, "decide_approval_pending", write)

    def list_decided(self, *, user_id: str, since: int) -> list[PendingApproval]:
        """The owner's approvals someone approved or denied since *since*, newest first."""
        stmt = (
            select(SqlApprovalPending)
            .where(
                SqlApprovalPending.workspace_id == current_workspace_id(),
                SqlApprovalPending.user_id == user_id,
                SqlApprovalPending.status.in_(("approved", "denied")),
                SqlApprovalPending.decided_at >= since,
            )
            .order_by(SqlApprovalPending.decided_at.desc())
        )
        with self._session("list_approval_decided") as session:
            return [_pending(row) for row in session.execute(stmt).scalars().all()]


def pending_to_event(pending: PendingApproval) -> dict[str, Any]:
    """The stored ``response.elicitation_request`` event of a pending approval."""
    import json

    event = json.loads(pending.event)
    return event if isinstance(event, dict) else {}
