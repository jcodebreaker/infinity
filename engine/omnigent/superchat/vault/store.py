"""Secrets vault store: saved logins encrypted at rest, secure-entry requests, and an audit trail.

Passwords are sealed with AES-256-GCM under a server key from ``OMNIGENT_VAULT_KEY`` (base64 of
32 random bytes); the secret's id and owner are bound in as associated data. With no valid key
the vault fails closed: nothing is saved and nothing is revealed. Plaintext leaves the store
only through :meth:`VaultStore.reveal` (the runner's fill path) and
:meth:`VaultStore.secret_values` (in-process, for redacting what clients read); it is never
logged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlVaultAudit,
    SqlVaultRequest,
    SqlVaultSecret,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.superchat.sealing import (  # noqa: F401  (re-exported: the vault's old home)
    VAULT_KEY_ENV,
    VaultUnavailableError,
    require_key,
    seal,
    unseal,
)

MAX_VALUE = 4096
REQUEST_TTL_SECONDS = 15 * 60


class VaultInputError(ValueError):
    """The name, site or value is not acceptable."""


@dataclass
class VaultEntry:
    """Metadata of a saved login; never carries the password."""

    id: str
    name: str
    site: str
    username: str
    created_at: int
    last_used_at: int | None


@dataclass
class VaultRequest:
    """A one-time secure-entry request."""

    id: str
    user_id: str | None
    session_id: str
    name: str
    site: str
    reason: str
    status: str  # pending | saved | expired


def normalize_site(site: str) -> str:
    """:returns: The HTTPS origin of site (scheme, host, non-default port), else raises."""
    parts = urlsplit(site.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise VaultInputError("the site must be an https address")
    port = f":{parts.port}" if parts.port and parts.port != 443 else ""
    return f"https://{parts.hostname.lower()}{port}"


def origin_of(url: str) -> str | None:
    """:returns: The origin of a page URL as :func:`normalize_site` would write it, or ``None``."""
    try:
        return normalize_site(url)
    except (VaultInputError, ValueError):
        return None


def _entry(row: SqlVaultSecret) -> VaultEntry:
    return VaultEntry(
        id=row.id,
        name=row.name,
        site=row.site,
        username=row.username,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _audit(
    session: Session,
    user_id: str | None,
    secret: SqlVaultSecret,
    action: str,
    session_id: str | None,
) -> None:
    session.add(
        SqlVaultAudit(
            id=uuid.uuid4().hex,
            user_id=user_id,
            secret_id=secret.id,
            secret_name=secret.name,
            session_id=session_id,
            action=action,
            at=now_epoch(),
        )
    )


class VaultStore:
    """SQLAlchemy-backed vault (owner-scoped)."""

    def __init__(self, storage_location: str) -> None:
        """:param storage_location: SQLAlchemy database URI."""
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.vault_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.vault_store", immediate=True
        )

    # ── secure-entry requests ──────────────────────────────────────────────

    def create_request(
        self, *, user_id: str | None, session_id: str, name: str, site: str, reason: str
    ) -> VaultRequest:
        """Open a one-time request the person answers on a secure form."""
        require_key()  # fail closed: never show a card that cannot save
        name = _clean_name(name)
        site = normalize_site(site)
        req = SqlVaultRequest(
            id=uuid.uuid4().hex,
            user_id=user_id,
            session_id=session_id,
            name=name,
            site=site,
            reason=reason.strip()[:512],
            status="pending",
            created_at=now_epoch(),
        )

        def write(session: Session) -> VaultRequest:
            session.add(req)
            session.flush()
            return _request(req)

        return run_write_transaction(self._session_immediate, "vault_create_request", write)

    def get_request(self, request_id: str, *, user_id: str | None) -> VaultRequest | None:
        """The owner's request, or ``None``."""
        with self._session("vault_get_request") as session:
            row = session.execute(
                select(SqlVaultRequest).where(
                    SqlVaultRequest.workspace_id == current_workspace_id(),
                    SqlVaultRequest.id == request_id,
                )
            ).scalar_one_or_none()
            if row is None or row.user_id != user_id:
                return None
            req = _request(row)
            if req.status == "pending" and now_epoch() - row.created_at > REQUEST_TTL_SECONDS:
                req.status = "expired"
            return req

    # ── secrets ────────────────────────────────────────────────────────────

    def save(
        self,
        *,
        user_id: str | None,
        name: str,
        site: str,
        username: str,
        password: str,
        request_id: str | None = None,
    ) -> VaultEntry:
        """Encrypt and store a login (same name replaces it); audit ``create``."""
        require_key()
        name = _clean_name(name)
        site = normalize_site(site)
        if not password or len(password) > MAX_VALUE:
            raise VaultInputError("the password is empty or too long")
        username = username.strip()[:256]

        def write(session: Session) -> VaultEntry:
            row = self._find(session, user_id, name)
            if row is None:
                row = SqlVaultSecret(
                    id=uuid.uuid4().hex, user_id=user_id, name=name, created_at=now_epoch()
                )
                session.add(row)
            row.site, row.username = site, username
            row.ciphertext = seal(password, user_id=user_id, secret_id=row.id)
            row.last_used_at = None
            if request_id:
                req = session.execute(
                    select(SqlVaultRequest).where(
                        SqlVaultRequest.workspace_id == current_workspace_id(),
                        SqlVaultRequest.id == request_id,
                    )
                ).scalar_one_or_none()
                if (
                    req is None
                    or req.user_id != user_id
                    or req.status != "pending"
                    or now_epoch() - req.created_at > REQUEST_TTL_SECONDS
                    or req.name != name
                    or req.site != site
                ):
                    raise VaultInputError("this request is expired, used or does not match")
                req.status = "saved"
            session.flush()
            _audit(session, user_id, row, "create", None)
            return _entry(row)

        entry = run_write_transaction(self._session_immediate, "vault_save", write)
        _forget_redaction_values(user_id)
        return entry

    def entries(self, *, user_id: str | None) -> list[VaultEntry]:
        """The owner's logins, metadata only, newest first."""
        with self._session("vault_list") as session:
            rows = session.execute(
                select(SqlVaultSecret).where(
                    SqlVaultSecret.workspace_id == current_workspace_id(),
                    SqlVaultSecret.user_id == user_id
                    if user_id is not None
                    else SqlVaultSecret.user_id.is_(None),
                )
            ).scalars()
            return sorted((_entry(r) for r in rows), key=lambda e: -e.created_at)

    def delete(self, secret_id: str, *, user_id: str | None) -> bool:
        """Delete one of the owner's logins; audit ``delete``."""

        def write(session: Session) -> bool:
            row = session.execute(
                select(SqlVaultSecret).where(
                    SqlVaultSecret.workspace_id == current_workspace_id(),
                    SqlVaultSecret.id == secret_id,
                )
            ).scalar_one_or_none()
            if row is None or row.user_id != user_id:
                return False
            _audit(session, user_id, row, "delete", None)
            session.execute(
                delete(SqlVaultSecret).where(
                    SqlVaultSecret.workspace_id == current_workspace_id(),
                    SqlVaultSecret.id == secret_id,
                )
            )
            return True

        deleted = run_write_transaction(self._session_immediate, "vault_delete", write)
        if deleted:
            _forget_redaction_values(user_id)
        return deleted

    def secret_values(self, *, user_id: str | None) -> list[str]:
        """The owner's saved passwords, decrypted, for in-process redaction only.

        Never audited (nothing is filled) and never returned by a route. Empty when the vault
        is off; a row that cannot be decrypted is skipped.
        """
        try:
            require_key()
        except VaultUnavailableError:
            return []
        with self._session("vault_secret_values") as session:
            rows = session.execute(
                select(SqlVaultSecret.id, SqlVaultSecret.ciphertext).where(
                    SqlVaultSecret.workspace_id == current_workspace_id(),
                    SqlVaultSecret.user_id == user_id
                    if user_id is not None
                    else SqlVaultSecret.user_id.is_(None),
                )
            ).all()
        values: list[str] = []
        for secret_id, ciphertext in rows:
            try:
                values.append(unseal(ciphertext, user_id=user_id, secret_id=secret_id))
            except VaultUnavailableError:
                continue
        return values

    def site_of(self, name: str, *, user_id: str | None) -> str | None:
        """The saved origin of a login by name, or ``None`` (no decryption)."""
        with self._session("vault_site_of") as session:
            row = self._find(session, user_id, name.strip())
            return row.site if row is not None else None

    def reveal(
        self, name: str, *, user_id: str | None, field: str, session_id: str | None
    ) -> tuple[str, str]:
        """Decrypt one field for a fill; audit ``fill``.

        :returns: ``(value, site)``. Only the runner's fill path calls this.
        """
        if field not in ("username", "password"):
            raise VaultInputError("field must be username or password")

        def write(session: Session) -> tuple[str, str]:
            row = self._find(session, user_id, name.strip())
            if row is None:
                raise KeyError(name)
            value = (
                row.username
                if field == "username"
                else unseal(row.ciphertext, user_id=user_id, secret_id=row.id)
            )
            if field == "username":
                require_key()
            row.last_used_at = now_epoch()
            _audit(session, user_id, row, "fill", session_id)
            return value, row.site

        return run_write_transaction(self._session_immediate, "vault_reveal", write)

    def refuse_fill(self, name: str, *, user_id: str | None, session_id: str | None) -> None:
        """Record a refused fill attempt against a login (nothing if the name is unknown)."""

        def write(session: Session) -> None:
            row = self._find(session, user_id, name.strip())
            if row is not None:
                _audit(session, user_id, row, "refuse", session_id)

        run_write_transaction(self._session_immediate, "vault_refuse", write)

    def audit(self, secret_id: str, *, user_id: str | None) -> list[tuple[str, int, str | None]]:
        """``(action, at, session_id)`` rows for one secret, oldest first (owner only)."""
        with self._session("vault_audit") as session:
            rows = session.execute(
                select(SqlVaultAudit)
                .where(
                    SqlVaultAudit.workspace_id == current_workspace_id(),
                    SqlVaultAudit.secret_id == secret_id,
                    SqlVaultAudit.user_id == user_id
                    if user_id is not None
                    else SqlVaultAudit.user_id.is_(None),
                )
                .order_by(SqlVaultAudit.at)
            ).scalars()
            return [(r.action, r.at, r.session_id) for r in rows]

    @staticmethod
    def _find(session: Session, user_id: str | None, name: str) -> SqlVaultSecret | None:
        return session.execute(
            select(SqlVaultSecret).where(
                SqlVaultSecret.workspace_id == current_workspace_id(),
                SqlVaultSecret.name == name,
                SqlVaultSecret.user_id == user_id
                if user_id is not None
                else SqlVaultSecret.user_id.is_(None),
            )
        ).scalar_one_or_none()


def _forget_redaction_values(user_id: str | None) -> None:
    """Drop the redactor's cached copy of this owner's values (they just changed)."""
    from omnigent.server.redaction import invalidate_vault

    invalidate_vault(user_id)


def _request(row: SqlVaultRequest) -> VaultRequest:
    return VaultRequest(
        id=row.id,
        user_id=row.user_id,
        session_id=row.session_id,
        name=row.name,
        site=row.site,
        reason=row.reason,
        status=row.status,
    )


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 128 or not all(c.isalnum() or c in "._-" for c in name):
        raise VaultInputError("the name may use letters, digits, '.', '_' and '-'")
    return name
