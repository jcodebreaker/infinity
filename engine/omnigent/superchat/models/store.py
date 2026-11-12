"""Model connections: sealed provider keys, per user or per organization, never shown back.

A connection is one key for one provider held by an owner: ``scope='user'`` with the user id as
``owner_id``, or ``scope='org'`` (the workspace) with ``owner_id=''``. The primary key
``(workspace_id, scope, owner_id, provider)`` keeps it to one key per provider per owner; saving
again replaces it. The key is sealed with the vault's AES-256-GCM (``OMNIGENT_VAULT_KEY``) with
associated data ``"<scope>:<owner_id>|<provider>"`` (so a ciphertext cannot be replayed under
another scope, owner or provider) and fails closed when the vault key is missing. Plaintext leaves
only through :func:`resolve_model_connection` / :meth:`ModelConnectionStore.get_plaintext` (server
side, for the run path); it is never logged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlModelConnection, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.models.upstreams import PROVIDERS, UPSTREAMS
from omnigent.superchat.sealing import seal, unseal

STATUS_VALID = "valid"
SCOPE_USER = "user"
SCOPE_ORG = "org"
#: The operator's own key for an upstream it runs (``Upstream.operator_key_env``): never saved,
#: read from the engine's environment when no person or organization key exists.
SCOPE_OPERATOR = "operator"
ORG_OWNER = ""
MAX_LABEL = 64


class ConnectionInputError(ValueError):
    """The provider, scope or key is not acceptable."""


@dataclass(frozen=True)
class ConnectionMeta:
    """What a client may see about a saved key: never the key."""

    provider: str
    hint: str
    validated_at: int
    status: str
    label: str | None
    scope: str


@dataclass(frozen=True)
class ResolvedConnection:
    """The connection a run uses: server side only."""

    provider: str
    plaintext: str = field(repr=False)
    scope: str


def _meta(row: SqlModelConnection) -> ConnectionMeta:
    return ConnectionMeta(
        row.provider, row.hint, row.validated_at, row.status, row.label, row.scope
    )


def _aad(scope: str, owner_id: str) -> str:
    # The vault binds "{user_id}|{secret_id}": this makes the AAD "{scope}:{owner_id}|{provider}".
    return f"{scope}:{owner_id}"


def seal_key(scope: str, owner_id: str, provider: str, api_key: str) -> str:
    """:returns: *api_key* sealed for exactly this scope, owner and provider."""
    return seal(api_key, user_id=_aad(scope, owner_id), secret_id=provider)


def unseal_key(scope: str, owner_id: str, provider: str, token: str) -> str:
    """:returns: The plaintext key; raises ``VaultUnavailableError`` on a wrong binding or key."""
    return unseal(token, user_id=_aad(scope, owner_id), secret_id=provider)


def _check_owner(scope: str, owner_id: str) -> None:
    if scope == SCOPE_USER and owner_id:
        return
    if scope == SCOPE_ORG and owner_id == ORG_OWNER:
        return
    raise ConnectionInputError("scope must be 'user' with an owner, or 'org' without one")


class ModelConnectionStore:
    """SQLAlchemy-backed store of model connections."""

    def __init__(self, storage_location: str) -> None:
        """:param storage_location: SQLAlchemy database URI."""
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.model_connection_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.model_connection_store", immediate=True
        )

    def put(
        self,
        scope: str,
        owner_id: str,
        provider: str,
        api_key: str,
        *,
        label: str | None = None,
        validated_at: int | None = None,
    ) -> ConnectionMeta:
        """Seal and store the key, replacing the owner's previous key for that provider.

        :raises VaultUnavailableError: no usable ``OMNIGENT_VAULT_KEY`` (nothing is stored).
        :raises ConnectionInputError: unknown provider or scope, or empty key.
        """
        _check_owner(scope, owner_id)
        if provider not in PROVIDERS:
            raise ConnectionInputError(f"provider must be one of: {', '.join(PROVIDERS)}")
        api_key = api_key.strip()
        if not api_key:
            raise ConnectionInputError("api_key is required")
        label = (label or "").strip()[:MAX_LABEL] or None
        ciphertext = seal_key(scope, owner_id, provider, api_key)
        now = now_epoch()
        stamp = validated_at if validated_at is not None else now

        def write(session: Session) -> ConnectionMeta:
            key = (current_workspace_id(), scope, owner_id, provider)
            row = session.get(SqlModelConnection, key)
            if row is None:
                row = SqlModelConnection(
                    scope=scope, owner_id=owner_id, provider=provider, created_at=now
                )
                session.add(row)
            row.ciphertext = ciphertext
            row.hint = api_key[-4:]
            row.validated_at = stamp
            row.status = STATUS_VALID
            row.label = label
            row.updated_at = now
            session.flush()
            return _meta(row)

        return run_write_transaction(self._session_immediate, "model_connection_put", write)

    def list(self, scope: str, owner_id: str) -> list[ConnectionMeta]:
        """:returns: Masked metadata of the owner's connections, ordered by provider."""
        _check_owner(scope, owner_id)
        with self._session("model_connection_list") as session:
            rows = session.scalars(
                select(SqlModelConnection)
                .where(
                    SqlModelConnection.workspace_id == current_workspace_id(),
                    SqlModelConnection.scope == scope,
                    SqlModelConnection.owner_id == owner_id,
                )
                .order_by(SqlModelConnection.provider)
            ).all()
            return [_meta(row) for row in rows]

    def list_by_user(self) -> dict[str, list[ConnectionMeta]]:
        """:returns: Masked metadata of every person's own connections, by user id."""
        with self._session("model_connection_list_by_user") as session:
            rows = session.scalars(
                select(SqlModelConnection)
                .where(
                    SqlModelConnection.workspace_id == current_workspace_id(),
                    SqlModelConnection.scope == SCOPE_USER,
                )
                .order_by(SqlModelConnection.owner_id, SqlModelConnection.provider)
            ).all()
            found: dict[str, list[ConnectionMeta]] = {}
            for row in rows:
                found.setdefault(row.owner_id, []).append(_meta(row))
            return found

    def get_plaintext(
        self, scope: str, owner_id: str, preferred: Sequence[str]
    ) -> tuple[str, str] | None:
        """:returns: ``(provider, api_key)`` for the first of *preferred* the owner holds.

        Server side only.

        :raises VaultUnavailableError: the vault key is missing or cannot open the value.
        """
        _check_owner(scope, owner_id)
        with self._session("model_connection_get") as session:
            rows = {
                row.provider: row.ciphertext
                for row in session.scalars(
                    select(SqlModelConnection).where(
                        SqlModelConnection.workspace_id == current_workspace_id(),
                        SqlModelConnection.scope == scope,
                        SqlModelConnection.owner_id == owner_id,
                        SqlModelConnection.provider.in_(list(preferred)),
                    )
                )
            }
        for provider in preferred:
            if provider in rows:
                return provider, unseal_key(scope, owner_id, provider, rows[provider])
        return None

    def delete(self, scope: str, owner_id: str, provider: str) -> bool:
        """:returns: ``True`` when a key was removed."""
        _check_owner(scope, owner_id)

        def write(session: Session) -> bool:
            result = session.execute(
                delete(SqlModelConnection).where(
                    SqlModelConnection.workspace_id == current_workspace_id(),
                    SqlModelConnection.scope == scope,
                    SqlModelConnection.owner_id == owner_id,
                    SqlModelConnection.provider == provider,
                )
            )
            return bool(result.rowcount)

        return run_write_transaction(self._session_immediate, "model_connection_delete", write)


def resolve_model_connection(
    store: ModelConnectionStore | None, *, owner_id: str, preferred: Sequence[str]
) -> ResolvedConnection | None:
    """The connection a run uses, for the run path; server side.

    Order: the user's own connection, then the organization's, then the operator's key for an
    upstream the operator runs (``Upstream.operator_key_env``, read from the engine's
    environment, configured upstreams only), else ``None``. Within a scope the first of
    *preferred* (providers, best first) wins.

    :raises VaultUnavailableError: the vault key is missing or cannot open the value.
    """
    if store is not None:
        for scope, scope_owner in ((SCOPE_USER, owner_id), (SCOPE_ORG, ORG_OWNER)):
            if scope == SCOPE_USER and not scope_owner:
                continue
            found = store.get_plaintext(scope, scope_owner, preferred)
            if found is not None:
                return ResolvedConnection(provider=found[0], plaintext=found[1], scope=scope)
    for provider in preferred:
        upstream = UPSTREAMS.get(provider)
        key = upstream.operator_key if upstream is not None and upstream.configured else None
        if key is not None:
            return ResolvedConnection(provider=provider, plaintext=key, scope=SCOPE_OPERATOR)
    return None
