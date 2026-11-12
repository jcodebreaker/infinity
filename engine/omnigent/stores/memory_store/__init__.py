"""Memory-claim store — persists long-term memory claims.

See ``docs/context.md`` for the design: the
``memory_claims`` table is the source of truth; the txtai search index
(``omnigent/memory/index.py``) is rebuilt from it on demand.
"""

from abc import ABC, abstractmethod

from omnigent.entities import MemoryClaim, MemoryEvidenceLink

# Sentinel meaning "caller did not supply this argument; leave the column
# unchanged." Distinct from None, which means "set the column to NULL."
UNSET: object = object()


class MemoryStore(ABC):
    """Abstract base for memory-claim persistence. Every query is user-scoped."""

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the memory store.

        :param storage_location: Backend-specific storage URI, e.g.
            ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        claim_id: str,
        user_id: str,
        kind: str,
        claim_text: str,
        *,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        supersedes_claim_id: str | None = None,
        valid_until: int | None = None,
        run_id: str | None = None,
    ) -> MemoryClaim:
        """Insert a new active claim.

        :param claim_id: Pre-generated unique claim identifier.
        :param user_id: The user this claim is about.
        :param kind: ``preference`` / ``instruction`` / ``fact`` /
            ``decision`` / ``person`` / ``project`` / ``working_style``.
        :param claim_text: One self-contained sentence.
        :param quote: Exact words the evidence was drawn from.
        :param speaker: Who said it.
        :param evidence: Source links.
        :param explicitness: ``"stated"`` or ``"inferred"``.
        :param confidence: 0-1.
        :param supersedes_claim_id: A prior claim this one replaces. When
            set, the store marks that claim ``superseded`` in the same
            transaction.
        :param valid_until: Unix epoch seconds after which the claim expires.
        :param run_id: The upkeep run that wrote this claim, or ``None``.
        :returns: The newly created :class:`MemoryClaim`.
        """
        ...

    @abstractmethod
    def get(self, claim_id: str, user_id: str) -> MemoryClaim | None:
        """Return a claim if it belongs to *user_id*, else ``None``."""
        ...

    @abstractmethod
    def list_active(
        self,
        user_id: str,
        *,
        kind: str | None = None,
        min_confidence: float | None = None,
        include_expired: bool = False,
        limit: int = 1000,
    ) -> list[MemoryClaim]:
        """List a user's active claims, newest first.

        Claims past ``valid_until`` are left out unless *include_expired*; their
        ``status`` column stays ``active`` (expiry is derived, see
        :meth:`MemoryClaim.effective_status`).

        :param kind: Restrict to one kind, or ``None`` for all kinds.
        :param min_confidence: Only claims with ``confidence >=`` this value.
        :param include_expired: Also return claims past ``valid_until``.
        :param limit: Maximum rows returned.
        """
        ...

    @abstractmethod
    def list_all_active(self, limit: int = 100_000) -> list[MemoryClaim]:
        """List every live (active, unexpired) claim across every user, for index rebuilds."""
        ...

    @abstractmethod
    def reinforce(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> MemoryClaim | None:
        """Bump ``reinforcement_count`` and ``confidence``, stamp ``reinforced_at``.

        Returns ``None`` if the claim isn't found, isn't owned by *user_id*,
        or isn't ``active``.
        """
        ...

    @abstractmethod
    def supersede(
        self,
        old_claim_id: str,
        user_id: str,
        *,
        new_claim_id: str,
        kind: str,
        claim_text: str,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = 0.9,
        run_id: str | None = None,
    ) -> MemoryClaim:
        """Mark *old_claim_id* ``superseded`` and insert the replacement, atomically."""
        ...

    @abstractmethod
    def set_status(self, claim_id: str, user_id: str, status: str) -> MemoryClaim | None:
        """Transition a claim's status (e.g. to ``forgotten`` or ``expired``)."""
        ...

    @abstractmethod
    def find_successor(self, claim_id: str, user_id: str) -> MemoryClaim | None:
        """Return the claim that supersedes *claim_id*, if any.

        Powers the forward half of ``memory_explain``'s supersession chain
        (the backward half just follows ``supersedes_claim_id`` via
        repeated :meth:`get` calls).
        """
        ...

    @abstractmethod
    def person_edit(self, claim_id: str, user_id: str, claim_text: str) -> MemoryClaim | None:
        """Replace an active claim's text in place and mark it person-authored."""
        ...
