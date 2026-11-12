"""The policies a product installs through the policy builder.

The builder and the engine are SHARED and never import the product (ADR 0019). The product's
composition root (``omnigent.superchat.features``) fills this registry when it is imported:

- session policies: a function-policy spec and the predicate that says which sessions get it;
- owner budgets: what the auto-injected ``owner_model_budget`` policy is seeded from, and where
  the engine records the check-in level an owner approved.

Registration is by name, so importing the composition root twice changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from omnigent.entities import Conversation
    from omnigent.spec.types import FunctionPolicySpec


#: ``applies(conversation, tree)``: whether a session gets the policy. ``tree`` is the session's
#: verified spawn tree when the engine is built, and ``None`` for the cheap pre-check that decides
#: whether an engine is needed at all. ``conversation`` is ``None`` when the caller holds no row.
SessionPolicyApplies = Callable[["Conversation | None", "list[Conversation] | None"], bool]


@dataclass(frozen=True)
class SessionPolicy:
    """One product policy and the sessions it covers."""

    spec: FunctionPolicySpec
    applies: SessionPolicyApplies


class OwnerBudgets(Protocol):
    """The owner's monthly model budgets, as the builder and the engine read them."""

    def any_limit(self) -> bool:
        """Whether anyone in the workspace has a limit (the cheap gate before per-owner work)."""
        ...

    def seed_for_policy(self, owner: str) -> dict[str, float | str] | None:
        """The ``owner_model_budget`` policy's context, or ``None`` when no limit applies."""
        ...

    def record_approval(self, owner: str, scope: str, level: float) -> None:
        """Remember that *owner* approved continuing past *scope*'s limit up to *level*."""
        ...


_session_policies: dict[str, SessionPolicy] = {}
_owner_budgets: Callable[[], OwnerBudgets | None] | None = None


def register_session_policy(policy: SessionPolicy) -> None:
    """Install *policy* on every session its predicate covers (keyed by the spec's name)."""
    _session_policies[policy.spec.name] = policy


def session_policies() -> tuple[SessionPolicy, ...]:
    """The registered session policies, in registration order."""
    return tuple(_session_policies.values())


def register_owner_budgets(provider: Callable[[], OwnerBudgets | None]) -> None:
    """Name where the owner budgets come from; *provider* returns ``None`` while none are bound."""
    global _owner_budgets
    _owner_budgets = provider


def owner_budgets() -> OwnerBudgets | None:
    """The bound owner budgets, or ``None`` (no product registered, or none bound yet)."""
    return _owner_budgets() if _owner_budgets is not None else None
