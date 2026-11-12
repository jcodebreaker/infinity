"""SQLAlchemy table definitions for the omnigent database."""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from omnigent.db.compression import CompressedText

# 32-byte sha256 digest column. LargeBinary → BYTEA (Postgres) / BLOB (SQLite),
# but MySQL cannot index a BLOB without a key-prefix length, so use fixed-length
# BINARY(32) there — an exact fit for the digest and fully indexable.
_CKSUM32 = LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


# Hex length of a bare uuid4 id, the canonical Python-side form.
_UUID_HEX_LEN = 32

# Prefixes ids carried before they became bare 32-char hex. ``uuid_to_bytes``
# strips exactly these (so old URLs/clients keep resolving) and nothing else —
# an unknown prefix fails loud rather than silently storing a wrong-typed id's
# hex tail (e.g. a ``resp_``/``runner_token_`` value mis-passed to a uuid column).
_LEGACY_ID_PREFIXES = frozenset(
    {
        "ag",
        "conv",
        "host",
        "pol",
        "file",
        "cmt",
        # conversation-item per-type prefixes
        "msg",
        "fc",
        "fco",
        "err",
        "rs",
        "cmp",
        "nt",
        "rse",
        "sc",
        "tc",
        "rd",
        # runner-internal conversation binding
        "agy_conv",
    }
)


class InvalidUuidError(ValueError):
    """An id string could not be normalised to a 32-char hex uuid.

    Subclasses ``ValueError`` so existing ``except ValueError`` sites keep
    working. Surfaced (wrapped in ``sqlalchemy.exc.StatementError``) when a
    malformed id reaches a ``Uuid16`` column bind; the server maps it to a 404
    so a bad id in a URL is not-found rather than a 500.
    """


def uuid_to_bytes(value: str | uuid.UUID) -> bytes:
    """Normalise an id to the 16 raw bytes stored in a ``Uuid16`` column.

    Accepts, reducing them all to the same 16 bytes: a :class:`uuid.UUID`
    object; the bare 32-char hex form (what generators emit); the dashed
    canonical uuid (``str(uuid4())``); and a legacy id carrying one of the
    known :data:`_LEGACY_ID_PREFIXES` (``conv_<hex>``, ``ag_<hex>``, …) — so
    old bookmarked URLs, pasted ids, and pre-migration clients keep resolving.
    Anything else — a truncated id, non-hex text, an unknown prefix — fails
    loud rather than silently storing the wrong bytes.

    :param value: A ``uuid.UUID``, or a 32-char hex uuid optionally dashed or
        legacy-prefixed.
    :returns: The 16-byte big-endian value.
    :raises InvalidUuidError: If *value* is not a 32-char hex uuid.
    """
    if isinstance(value, uuid.UUID):
        return value.bytes
    normalized = value.replace("-", "")
    if "_" in normalized:
        prefix, _, tail = normalized.rpartition("_")
        if prefix in _LEGACY_ID_PREFIXES and len(tail) == _UUID_HEX_LEN:
            normalized = tail
    if len(normalized) != _UUID_HEX_LEN:
        raise InvalidUuidError(f"expected a 32-char hex uuid, got {value!r}")
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise InvalidUuidError(f"invalid hex uuid: {value!r}") from exc


def normalize_uuid(value: str | None) -> str | None:
    """Return the bare 32-char hex form of *value*, or *value* unchanged.

    The forgiving companion to :func:`uuid_to_bytes` for **Python-side** id
    comparisons (e.g. a store's scope check against an ORM attribute, which
    always reads back bare hex). A legacy-prefixed or dashed input normalises
    to bare hex; a malformed input is returned as-is so the comparison simply
    mismatches — preserving the pre-migration "unknown id = not found"
    behaviour instead of raising. ``None`` passes through.

    :param value: Any caller-supplied id string, or ``None``.
    :returns: The bare 32-char hex form, or *value* verbatim if not a uuid.
    """
    if value is None:
        return None
    try:
        return uuid_to_bytes(value).hex()
    except InvalidUuidError:
        return value


class Uuid16(TypeDecorator[str]):
    """A uuid stored as 16 raw bytes, presented to Python as bare 32-char hex.

    Our ids are opaque 128-bit uuid4s stored as raw bytes — ``BYTEA``
    (PostgreSQL), ``BLOB`` (SQLite / D1), fixed-length ``BINARY(16)`` (MySQL,
    where a BLOB is not indexable without a key-prefix length). The rest of
    the system keeps the readable bare 32-char hex form (entities, JSON
    blobs, URLs, the FTS mirror), so this type converts at the column
    boundary and nothing else has to change. Binds accept bare, dashed, or
    legacy-prefixed uuids; results always come back as bare lowercase hex.
    Result values guard the same driver variance ``CompressedText`` does:
    ``bytes``, ``memoryview`` (some drivers), or ``str`` (already hex).
    """

    impl = LargeBinary(16)
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLBinary(16))
        return dialect.type_descriptor(LargeBinary(16))

    def process_bind_param(self, value: str | uuid.UUID | None, dialect: object) -> bytes | None:
        del dialect
        if value is None:
            return None
        return uuid_to_bytes(value)

    def process_result_value(
        self, value: bytes | memoryview | str | None, dialect: object
    ) -> str | None:
        del dialect
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return bytes(value).hex()


class OmnigentBase(DeclarativeBase):
    """Declarative base for the Omnigent operational tables.

    Covers agents, files, users, tokens, session permissions,
    conversation metadata, comments, policies, hosts, and daily costs.
    Grouped under their own ``metadata`` so schema creation and Alembic
    autogenerate can target the Omnigent side independently of the
    conversation tables.
    """


class ConversationBase(DeclarativeBase):
    """Declarative base for the conversation tables.

    Covers ``conversations``, ``conversation_items``, and
    ``conversation_labels`` — the user-facing conversation surface
    (the Agent-Platform-side tables). Kept under their own ``metadata``
    so they can be created and, when ``conversation_storage_location``
    is configured, hosted on a separate physical database from the
    Omnigent tables.
    """


# Default workspace id stamped on every row and used as the leading
# member of every composite primary key. 0 is the single-workspace /
# unassigned sentinel: with no workspace bound to the request, all rows
# live in workspace 0.
DEFAULT_WORKSPACE_ID = 0

# Ambient per-request workspace id. Stores are process-wide singletons, so
# the active workspace can't ride on the store instance — it lives here.
# OSS leaves this at the default (single-workspace 0); a multi-tenant
# deployment (e.g. universe) sets it per request from the authenticated
# context (via ``workspace_scope`` in middleware). Reads and inserts
# resolve it through ``current_workspace_id()`` so the same store code
# scopes to the caller's workspace without threading the id through every
# signature — keeping this file byte-identical across deployments.
_current_workspace_id: ContextVar[int] = ContextVar(
    "omnigent_workspace_id", default=DEFAULT_WORKSPACE_ID
)


def current_workspace_id() -> int:
    """Return the workspace id bound to the active request/context.

    Defaults to :data:`DEFAULT_WORKSPACE_ID` (0) — the single-workspace OSS
    deployment. Multi-tenant deployments set it per request so every
    primary-key lookup, filter, and insert scopes to that workspace.
    """
    return _current_workspace_id.get()


@contextlib.contextmanager
def workspace_scope(workspace_id: int) -> Iterator[None]:
    """Bind *workspace_id* for the duration of the ``with`` block.

    Used by multi-tenant request middleware (and tests) to scope all
    store access to one workspace; resets to the prior value on exit so
    nested / concurrent contexts don't leak.
    """
    token = _current_workspace_id.set(workspace_id)
    try:
        yield
    finally:
        _current_workspace_id.reset(token)


AGENT_KIND_TEMPLATE = "template"
AGENT_KIND_SESSION = "session"

POLICY_SCOPE_DEFAULT = "default"
POLICY_SCOPE_SESSION = "session"


class SqlAgent(OmnigentBase):
    """
    SQLAlchemy model for the ``agents`` table.

    Each row represents a registered agent in the system.

    :param id: Unique agent identifier, e.g. ``"ag_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds when the agent was created.
    :param name: Human-readable agent name. Registered template
        agents require unique names; session-scoped copies may reuse
        the same name across different sessions.
    :param bundle_location: Artifact store key for the current bundle.
        Content-addressed (SHA-256 hex), e.g.
        ``"ag_abc123/a1b2c3d4e5f6..."``.
    :param version: Monotonic version counter. Starts at 1, incremented
        on each update via ``PUT /api/agents/{id}``.
    :param kind: ``"template"`` for server-wide registered agents;
        ``"session"`` for per-conversation copies.
    :param description: Optional free-text description of the agent's
        purpose. ``None`` when not provided.
    :param updated_at: Unix epoch seconds of the last update, or
        ``None`` if the agent has never been updated.
    :param created_by: Identity of the user who created a session-scoped
        agent, used to restrict agent-code mutation to its owner. ``None``
        for template agents, single-user mode, and rows created before this
        column existed (an unowned session-scoped agent is admin-only to
        mutate).
    """

    __tablename__ = "agents"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(256))
    bundle_location: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # AGENT_KIND: template=1, session=2). The store converts to/from the
    # string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Owner of a session-scoped agent (the creating user). Gates agent-code
    # mutation to the owner; NULL for template agents, single-user mode, and
    # pre-migration rows (an unowned session-scoped agent is admin-only).
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_agents_kind"),
        Index("ix_agents_created_at", "workspace_id", "created_at", "id"),
        # Template agents have unique names; session-scoped agents (kind=2)
        # may reuse the same name. That "unique only within the template set"
        # rule can't be a partial unique index (MySQL has none), so it is
        # enforced in the store (SqlAlchemyAgentStore.create). This plain index
        # backs the (workspace_id, name, kind) lookup that check and get_by_name
        # do — kind is included so the seek skips same-named session copies
        # straight to the template row.
        Index("ix_agents_name", "workspace_id", "name", "kind", "id"),
    )


class SqlFile(OmnigentBase):
    """
    SQLAlchemy model for the ``files`` table.

    Each row represents an uploaded file tracked by the system.

    :param id: Unique file identifier, e.g. ``"file_a1b2c3d4..."``.
    :param created_at: Unix epoch seconds when the file record was
        created.
    :param filename: Original filename as provided by the uploader,
        max 512 characters. e.g. ``"report.pdf"``.
    :param bytes: Size of the file in bytes.
    :param content_type: MIME type of the file, e.g.
        ``"application/pdf"``. ``None`` when not provided.
    :param blob_key: Artifact-store key for this row's bytes. Normally
        equals ``id``; a forked row points it at the source row's blob
        so the fork shares the bytes instead of duplicating them.
        NULL on pre-``blob_key`` rows, read as ``COALESCE(blob_key, id)``.
    """

    __tablename__ = "files"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    bytes: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(256), nullable=True)
    session_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    # Artifact-store key for the bytes. NULL means "under id" (pre-blob_key
    # rows); a fork copy sets it to the source's blob so many rows share one
    # blob. Read as COALESCE(blob_key, id); reference-count it before deleting
    # the blob so a fork's shared bytes survive the source's deletion.
    blob_key: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    # Opaque JSON metadata about the original upload; never SQL-filtered.
    source_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Files are only ever listed per session (WHERE session_id = ?),
        # so a session-scoped composite is the only index needed. There is
        # no session-less "all files" listing.
        Index(
            "ix_files_session_id_created_at",
            "workspace_id",
            "session_id",
            "created_at",
            "id",
        ),
    )


class SqlUser(OmnigentBase):
    """
    SQLAlchemy model for the ``users`` table.

    Each row represents a user. In header / OIDC modes, ``id`` is
    the upstream identity (email or ``"local"``); the row is
    upserted on first sight and ``password_hash`` stays ``NULL``.
    In ``accounts`` mode, rows are created explicitly by the admin
    or via invite redemption with a populated ``password_hash``.

    :param id: User identifier — email in header/OIDC modes, chosen
        username in accounts mode, ``"local"`` in single-user.
    :param is_admin: When ``True``, the user bypasses all
        permission checks. ``False`` by default.
    :param password_hash: argon2id hash of the user's password.
        ``NULL`` for users created via header/OIDC modes (their
        password is the upstream IdP's).
    :param created_at: Unix epoch seconds when the row was inserted.
        Populated for accounts-mode users; ``NULL`` for legacy rows
        backfilled by the original permissions migration.
    :param last_login_at: Unix epoch seconds of the most recent
        successful ``/auth/login`` (accounts mode). ``NULL`` until
        the first login.
    """

    __tablename__ = "users"
    account_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    deleted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_login_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlPreference(OmnigentBase):
    """Named user preferences, scoped to a workspace and stored as opaque JSON."""

    __tablename__ = "preferences"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(CompressedText, nullable=False)


class SqlAccountToken(OmnigentBase):
    """
    SQLAlchemy model for the ``account_tokens`` table.

    Backs both invite tokens (admin-issued, allow self-serve
    registration) and magic-login tokens (CLI-minted, hand off a
    signed-in session into the web UI). Both have the same
    short-TTL single-use lifecycle, so they share one table.

    :param id: Opaque random token string (43+ URL-safe base64
        chars). This is the secret — the user presents it as a
        query param. Stored verbatim because we need
        constant-time lookup; rotation = delete + recreate.
    :param kind: ``"invite"`` (anyone can redeem; creates a new
        user) or ``"magic"`` (the bound ``user_id`` is signed in).
    :param user_id: For ``magic``, the user the token signs in as.
        For ``invite``, ``NULL`` (the username is chosen at
        redemption time).
    :param created_by: User id of the admin who issued an invite
        (``NULL`` for magic tokens, which are self-issued).
    :param created_at: Unix epoch seconds when the token was
        minted. ``expires_at = created_at + ttl_seconds``.
    :param expires_at: Unix epoch seconds when the token stops
        being redeemable. Single-use enforcement is via
        ``redeemed_at``, this just bounds the window.
    :param redeemed_at: Unix epoch seconds when the token was
        consumed. ``NULL`` until then. After being set, the token
        is dead — redeem checks this column atomically.
    :param invited_is_admin: For invite tokens, whether the
        resulting user should be created with admin rights. False
        for magic tokens.
    """

    __tablename__ = "account_tokens"
    account_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ACCOUNT_TOKEN_KIND: invite=1, magic=2). The store converts to/from
    # the string name at the row↔entity boundary.
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    redeemed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invited_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_account_tokens_kind"),
        Index("ix_account_tokens_expires_at", "workspace_id", "expires_at", "id"),
    )


class SqlConnection(OmnigentBase):
    """
    SQLAlchemy model for the ``connections`` table.

    One row per ``(workspace_id, user_id, provider, account_id)`` recording a
    user's connected third-party integration (GitHub App today; MCP connectors
    later). Backs the "Connect …" flows and the per-user sandbox credential
    broker that vends the secret to managed sandboxes on demand.

    The secret material is stored as one encrypted JSON blob
    (:attr:`secret_enc`, AWS KMS ciphertext via
    :class:`omnigent.stores.credential_store.secret_cipher.KmsSecretCipher`)
    so any provider's secret shape fits without a schema change — plaintext
    never touches the database.
    Non-secret provider metadata (login, ids, scopes, expiries) lives in
    :attr:`metadata_json`. See ``designs/CREDENTIAL_STORE.md``.

    :param user_id: The omnigent user the connection belongs to —
        email in header/OIDC modes, username in accounts mode.
    :param provider: Integration provider key, e.g. ``"github"``.
    :param account_id: Provider account discriminator; ``""`` for the user's
        single account for that provider (in the PK so multi-account needs no
        migration).
    :param secret_enc: Encrypted JSON secret blob (all secret material).
    :param metadata_json: Non-secret provider metadata as a JSON object.
    :param created_at: Unix epoch seconds the connection was first made.
    :param updated_at: Unix epoch seconds of the last refresh/reconnect.
    """

    __tablename__ = "connections"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), primary_key=True, nullable=False, server_default=""
    )
    # Both plain Text, deliberately (not CompressedText): secret_enc is KMS
    # ciphertext (base64) — high-entropy, so compression buys nothing — and
    # metadata_json is a small fixed set of provider fields (login, ids, scopes,
    # expiries), far below the size where zstd's framing overhead pays off.
    # Neither is filtered or pattern-matched in SQL.
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlDeviceGrant(OmnigentBase):
    """
    SQLAlchemy model for the ``device_grants`` table.

    Backs the generic OAuth 2.0 Device Authorization Grant (RFC 8628) —
    any browserless client (the Slack integration is the first, but the
    mechanism is not Slack-specific) obtains a delegated, per-user access
    token without a user credential passing through the client. One row per
    device-authorization request; it moves ``pending`` → ``approved`` /
    ``denied`` (browser consent) → ``redeemed`` (token issued) and can be
    ``revoked`` at any time.

    Secrets are stored **hashed** (never raw): the client's ``device_code``
    and the current ``refresh_token`` are HMAC-SHA256 digests, so a
    database read cannot recover a usable token.

    :param id: Opaque grant id (also the ``grant_id`` JWT claim on issued
        access tokens, used for revocation).
    :param device_code_hash: HMAC-SHA256 hex digest of the secret
        ``device_code`` the client polls with. Never store the raw code.
    :param user_code: Short human-readable code shown on the verification
        page (also carried in ``verification_uri_complete``).
    :param status: ``pending`` / ``approved`` / ``denied`` / ``redeemed`` /
        ``revoked`` (see :data:`omnigent.db.enum_codecs.DEVICE_GRANT_STATUS`).
    :param client_id: The RFC 8628 client identifier — a public string
        naming the requesting application (e.g. ``"slack"``), the same for
        every grant that application initiates. Shown on the consent page
        and recorded in the issued token's ``act`` claim for audit.
        Display/audit only — not a security-decision key.
    :param user_id: The Omnigent identity that approved the grant, set at
        consent time. ``NULL`` while pending. The delegated token's ``sub``.
    :param refresh_token_hash: HMAC-SHA256 hex digest of the current
        refresh token. Rotated on every refresh; a presented token that no
        longer matches (and isn't the current one) is a reuse signal that
        revokes the grant.
    :param created_at: Unix epoch seconds when the grant was created.
    :param expires_at: Unix epoch seconds after which the ``device_code``
        can no longer be exchanged (the authorization request expires).
    :param approved_at: Unix epoch seconds when the grant was approved,
        starting its absolute lifetime clock. ``NULL`` until approved.
        Refresh is refused once ``approved_at`` is older than the absolute
        max lifetime, forcing periodic re-consent.
    :param last_polled_at: Unix epoch seconds of the last token-poll,
        used to enforce the RFC 8628 ``interval`` / ``slow_down``.
    """

    __tablename__ = "device_grants"
    account_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # DEVICE_GRANT_STATUS). The store converts to/from the name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Digest of the just-superseded refresh token, kept only so a replay
    # of the previous token can be recognised as reuse (token theft) and
    # revoke the grant. Cleared on revoke.
    prev_refresh_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_polled_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("status IN (1, 2, 3, 4, 5)", name="ck_device_grants_status"),
        # Poll path looks up by device_code_hash; a top-level index keeps it
        # a point lookup rather than a partition scan.
        Index("ix_device_grants_device_code_hash", "workspace_id", "device_code_hash"),
        Index("ix_device_grants_user_code", "workspace_id", "user_code"),
        Index("ix_device_grants_expires_at", "workspace_id", "expires_at", "id"),
        # Refresh + revoke look up by the current / previous refresh-token
        # digest; index both so those paths stay point lookups.
        Index("ix_device_grants_refresh_hash", "workspace_id", "refresh_token_hash"),
        Index("ix_device_grants_prev_refresh_hash", "workspace_id", "prev_refresh_token_hash"),
    )


class SqlSessionPermission(OmnigentBase):
    """
    SQLAlchemy model for the ``session_permissions`` table.

    Junction table mapping ``(user_id, conversation_id)`` to a
    numeric permission level. PK is ``(user_id, conversation_id)``
    — optimized for the hot path ("list sessions I can access"
    = prefix scan on ``user_id``).

    The ``"__public__"`` sentinel ``user_id`` represents public
    read access to a session.

    :param user_id: The grantee, e.g. ``"alice@example.com"``
        or ``"__public__"`` for public access.
    :param conversation_id: The session being shared, e.g.
        ``"conv_e4f5a6b7..."``.
    :param level: Numeric permission level: ``1`` = read,
        ``2`` = edit, ``3`` = manage. Each level subsumes the
        ones below it (comparison is ``>=``).
    """

    __tablename__ = "session_permissions"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        primary_key=True,
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("level IN (1, 2, 3, 4)", name="ck_session_permissions_level"),
        # Lookups by conversation (get_session_owner) filter workspace_id +
        # conversation_id; user_id trails to complete the PK.
        Index(
            "ix_session_permissions_conversation_id",
            "workspace_id",
            "conversation_id",
            "user_id",
        ),
    )


class SqlConversationMetadata(OmnigentBase):
    """
    SQLAlchemy model for the ``omnigent_conversation_metadata`` table.

    Omnigent-side operational state for a conversation: runner/host
    bindings, native-session linkage, policy accumulators, and launch
    arguments. Paired 1-to-1 with :class:`SqlConversation` by
    ``(workspace_id, id)``; rows are created and deleted together.
    """

    __tablename__ = "omnigent_conversation_metadata"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    # Enum stored as a stable int code (CONVERSATION_KIND: default=1, sub_agent=2).
    kind: Mapped[int] = mapped_column(SmallInteger, default=1)
    runner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # No FK: host records are managed outside this table.
    host_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    sub_agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_summary: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_state: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    session_usage: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-encoded provider binding and model catalog captured at session creation.
    inference_snapshot: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # JSON-encoded list of strings. NULL for non-native sessions.
    terminal_launch_args: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Required when host_id is set; enforced by check constraint below.
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    git_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Live-state columns, written by the replica holding the runner
    # tunnel so any replica can serve the sidebar's live fields.
    # Writes must never bump conversations.updated_at (it drives
    # sidebar ordering).
    # Epoch seconds the bound runner's tunnel was last seen alive;
    # runner_online is derived from freshness (like host_is_live).
    runner_last_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Last relay-observed turn status (enum_codecs.SESSION_LIVE_STATUS);
    # NULL means no relay has ever reported on this session.
    live_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Outstanding elicitation (approval-prompt) count; NULL = never written.
    pending_elicitation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # First-class project membership. Relates to projects.id; no DB FK
    # (Rule R032). NULL = unfiled. Coexists with the implicit ``omni_project``
    # label via the store's dual-read until labels are consolidated.
    project_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN (1, 2)", name="ck_conversation_metadata_kind"),
        CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversation_metadata_workspace_required_for_host",
        ),
        # Supports list_conversations_by_runner_id and get_runner_ids.
        Index("ix_conversation_metadata_runner_id", "workspace_id", "runner_id", "id"),
        # "list sessions in project X" + per-project counts (GROUP BY project_id).
        Index("ix_conversation_metadata_project_id", "workspace_id", "project_id", "id"),
    )


class SqlProject(OmnigentBase):
    """
    SQLAlchemy model for the ``projects`` table.

    A user-defined, owner-private container that groups sessions (see
    ``designs/PROJECTS_PRD.md``). A project row exists independently of its
    member sessions, so it can be empty, renamed, and carry its own config —
    the things the implicit ``omni_project`` label could not. Session
    membership lives on ``omnigent_conversation_metadata.project_id``, not
    here; there is no DB foreign key (Rule R032).

    Ownership is stamped on the row via ``user_id`` (like ``scheduled_tasks``),
    not derived from a permission table the way session ownership is — projects
    have no ACL of their own and are never shared.

    :param id: Uuid16 primary key (bare 32-char hex in Python).
    :param name: Human-readable project name; unique per owner, enforced in the
        store (``_name_taken``) rather than by a DB constraint — see
        ``__table_args__``.
    :param user_id: Owning user, or ``None`` in single-user mode.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    __tablename__ = "projects"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Owning user identity. String(128) matches session_permissions.user_id and
    # every other user-identity column in this schema.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Default session settings as a compact JSON object (host/workspace/harness/
    # model/reasoning_effort/git base-branch, …), or NULL for "no defaults". The
    # keys are an opaque, client-owned vocabulary: the value is read and written
    # whole with the row and never filtered in SQL, so new keys need no schema
    # change. Stored values are hints the new-chat dialog pre-fills and the user
    # can always override. Opaque and never SQL-filtered — stored compressed
    # (CompressedText).
    config: Mapped[str | None] = mapped_column(CompressedText, nullable=True)

    __table_args__ = (
        # "list my projects" — prefix scan on (workspace_id, user_id) with
        # created_at in the key so the ORDER BY created_at, id is served by the
        # index (no filesort). Personal display order lives in preferences.
        #
        # Also covers the two name lookups via its (workspace_id, user_id)
        # prefix: the store's ``_name_taken`` probe and the ``?project=<name>``
        # member join. Both then filter ``name`` over the owner's handful of
        # rows, so neither needs a name-leading index of its own.
        #
        # There is deliberately NO unique index on (workspace_id, user_id, name).
        # Per-owner name uniqueness is a store-level check (``_name_taken``), not
        # a DB constraint: it never held for single-user mode anyway (``user_id``
        # is NULL there and SQL treats NULLs as distinct), and ``name`` is
        # mutable, so a unique key over it is maintained on every rename. The
        # cost is that two concurrent creates/renames to the same name can both
        # land; the member join already tolerates duplicate names by
        # construction, since it unions first-class members with label-projects
        # matched on the same string.
        Index(
            "ix_projects_user_id",
            "workspace_id",
            "user_id",
            "created_at",
            "id",
        ),
    )


class SqlConversation(ConversationBase):
    """
    SQLAlchemy model for the ``conversations`` table.

    Agent Platform (AP) fields for a conversation: identity, timestamps,
    title, hierarchy, the next_position allocator, and the agent binding
    (``agent_id`` + the ``session_overrides`` JSON blob). Omnigent
    operational state lives in :class:`SqlConversationMetadata`.

    :param id: Unique conversation identifier, e.g.
        ``"conv_e4f5a6b7..."``.
    :param created_at: Unix epoch seconds when the conversation was
        created.
    :param updated_at: Unix epoch seconds when the conversation was
        last updated (item append, title change, etc.).
    :param title: Human-readable title; empty string when untitled.
    :param parent_conversation_id: For Phase 4 named sub-agents,
        points at the parent conversation. ``None`` for top-level
        conversations.
    :param root_conversation_id: Id of the root (top-level)
        conversation in the spawn tree. Equal to ``id`` for
        top-level conversations.
    :param next_position: Monotonic allocator for the next item position.
    :param agent_id: Agent bound to the conversation at creation time.
        ``None`` for conversations created without an agent binding.
    :param session_overrides: Compact JSON blob of per-session config
        overrides (reasoning_effort, model_override,
        cost_control_mode_override, harness_override). ``None`` when the
        session uses all agent/spec defaults.
    """

    __tablename__ = "conversations"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(768), nullable=False, server_default="")
    parent_conversation_id: Mapped[str | None] = mapped_column(
        Uuid16(),
        nullable=True,
    )
    root_conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        nullable=False,
    )
    # Monotonic allocator for the next item position in this conversation.
    next_position: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # Agent bound to this conversation at creation time. NULL for conversations
    # created without an agent binding. Indexed for the agent→conversation
    # reverse lookup and the list filters (agent_id / has_agent_id / agent_name).
    agent_id: Mapped[str | None] = mapped_column(Uuid16(), nullable=True)
    # Per-session config overrides packed as a compact JSON object, e.g.
    # ``{"model_override":"claude-opus-4-8","reasoning_effort":"high"}``. Keys:
    # reasoning_effort, model_override, cost_control_mode_override,
    # harness_override. NULL when the session uses all agent/spec defaults; only
    # set keys are stored. Never filtered in SQL — read and written whole with
    # the row (see the store's _encode/_decode_session_overrides).
    session_overrides: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Whether the session is archived (hidden from the default sidebar). Lives
    # here on the AP table so list_conversations can filter it inline alongside
    # the created_at/updated_at sort keys, instead of pre-fetching ids from the
    # Omnigent metadata DB.
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    __table_args__ = (
        # Keep created_at unindexed here: ACL-selective listings may rationally
        # use a semi-join plus sort, while an ordering index can encourage many
        # permission probes. The default sidebar (archived=false, updated_at
        # DESC) is served by the archived_updated index below.
        Index("ix_conversations_archived_updated", "workspace_id", "archived", "updated_at", "id"),
        Index(
            "ix_conversations_root_conversation_id",
            "workspace_id",
            "root_conversation_id",
            "id",
        ),
        # Agent→conversation reverse lookup and the agent_id / has_agent_id /
        # agent_name list filters. id trails to complete the PK (index-only).
        Index(
            "ix_conversations_agent_id",
            "workspace_id",
            "agent_id",
            "id",
        ),
        # Child-session listing, and the per-parent title lookup that backs the
        # application-level (parent, title) uniqueness check in create_conversation
        # (no DB unique constraint: the check seeks this parent's children here and
        # filters title as a residual).
        Index(
            "idx_conversations_parent",
            "workspace_id",
            "parent_conversation_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # Session title search uses lower(title) LIKE '%query%'; Alembic owns
        # its PostgreSQL trigram index because the migration first enables pg_trgm.
    )


class SqlConversationItem(ConversationBase):
    """
    SQLAlchemy model for the ``conversation_items`` table.

    Each row represents a single item (message, function call,
    function call output, or reasoning block) within a conversation.

    :param id: Unique item identifier with a type-based prefix,
        e.g. ``"msg_a1b2c3..."``, ``"fc_d4e5f6..."``.
    :param conversation_id: Foreign key to
        :class:`SqlConversation.id`. Cascades on delete.
    :param response_id: The task/response ID this item belongs to,
        e.g. ``"resp_d8e9f0a1..."``.
    :param created_at: Unix epoch seconds when the item was created.
    :param status: Item status string. Defaults to ``"completed"``.
    :param position: Zero-based ordering index within the
        conversation. Used for deterministic item ordering.
    :param type: Item type discriminator, one of ``"message"``,
        ``"function_call"``, ``"function_call_output"``,
        ``"reasoning"``.
    :param data: JSON-serialized item payload. Structure varies by
        ``type``.
    :param search_text: Plain-text extraction of ``data`` used for
        full-text search indexing.
    :param created_by: Identity of the human actor who authored the
        item, or ``None`` for agent/tool/system items and single-user
        mode. Mirrors :class:`SqlComment.created_by`.
    """

    __tablename__ = "conversation_items"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    # conversation_id leads id in the PK so a conversation's items stay
    # contiguous for the per-conversation prefix scans that dominate reads.
    conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    response_id: Mapped[str] = mapped_column(String(64))
    # In the PK so deployments can PARTITION BY (created_at) with pure DDL —
    # both PostgreSQL and MySQL require the partition key in the PK and in
    # every unique index. Immutable: items are insert/delete-only.
    created_at: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_STATUS: completed=1). Only "completed" is written today, but the
    # CHECK admits the wider OpenAI-style status vocabulary reserved there.
    status: Mapped[int] = mapped_column(SmallInteger, default=1)
    position: Mapped[int] = mapped_column(Integer)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # ITEM_TYPE). The store converts to/from the string name at the
    # row↔entity boundary.
    type: Mapped[int] = mapped_column(SmallInteger)
    data: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        # Backs the per-conversation position-ordered scan (the dominant read).
        # Non-unique on purpose: the real position allocator is the next_position
        # counter advanced under _lock_conversation, which never reuses a
        # position; the DB is not relied on to enforce it (nothing catches a
        # collision). Being non-unique also means it needs no partition key, so
        # created_at is left out — the PK still carries it for partition-readiness.
        Index(
            "ix_conversation_items_conversation_id_position",
            "workspace_id",
            "conversation_id",
            "position",
        ),
        # Fork-truncation looks up by workspace_id + conversation_id +
        # response_id; id trails to complete the PK.
        Index(
            "ix_conversation_items_response_id",
            "workspace_id",
            "conversation_id",
            "response_id",
            "id",
        ),
        # Latest-message previews scan one type per conversation ordered by
        # position DESC (list_latest_message_items_for_conversations). Ordering
        # type before position lets the scan seek to (workspace_id,
        # conversation_id, type) and walk position DESC directly, avoiding a
        # heap recheck on type — which no other index covers.
        Index(
            "ix_conversation_items_conv_type_position",
            "workspace_id",
            "conversation_id",
            "type",
            text("position DESC"),
        ),
        CheckConstraint(
            "type IN (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)",
            name="ck_conversation_items_type",
        ),
        CheckConstraint("status IN (1, 2, 3, 4)", name="ck_conversation_items_status"),
    )


# Width of the ``conversation_labels.value`` column. Exported so the store
# (and the session-status error-label path) can clamp values to fit instead
# of letting an over-length write raise ``DataError`` on PostgreSQL.
LABEL_VALUE_MAX_LEN = 256


class SqlConversationLabel(ConversationBase):
    """
    SQLAlchemy model for the ``conversation_labels`` table.

    One row per (conversation, label-key) pair. Labels live in
    a dedicated table rather than a JSON column on
    ``conversations`` so per-key UPDATEs are atomic without
    read-modify-write (see POLICIES.md §6). The table is keyed
    only by ``conversation_id`` + ``key``, so it is untouched
    by compaction (which rewrites ``conversation_items``) —
    labels set turn 3 still exist turn 20 even after the
    earlier turns have been folded into a summary.

    :param conversation_id: The conversation this label belongs
        to. Composite PK member. Deleted with the conversation
        via ``ON DELETE CASCADE``.
    :param key: The label key, e.g. ``"integrity"``,
        ``"sensitivity"``. Composite PK member.
    :param value: The label value as a string, e.g. ``"0"``,
        ``"confidential"``. All label values are string-typed
        regardless of what the YAML author wrote — the parser
        coerces scalar / list values during spec load
        (POLICIES.md §14).
    :param updated_at: Unix epoch seconds of the last write.
        Single timestamp for each row; on UPSERT the row's
        timestamp is refreshed even when the value is
        unchanged (matches omnigent parity and keeps
        debugging timelines accurate).
    """

    __tablename__ = "conversation_labels"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(String(LABEL_VALUE_MAX_LEN))
    updated_at: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        # Finds the sessions carrying one label value (e.g. a caller's Super Chat).
        Index("ix_conversation_labels_key_value", "workspace_id", "key", "value"),
    )


class SqlComment(OmnigentBase):
    """SQLAlchemy model for the ``comments`` table.

    Stores per-review comments associated with a conversation.
    Each comment is anchored to a character range in the file expressed
    as absolute document-level offsets. Comments survive server restarts
    and are cleaned up when the owning conversation is deleted.

    :param id: UUID primary key, e.g. ``"a1b2c3d4-..."``.
    :param conversation_id: The conversation this comment belongs to.
    :param path: File path relative to the workspace root,
        e.g. ``"src/App.tsx"``.
    :param start_index: 0-based absolute character offset (inclusive)
        within the file where the anchor range begins.
    :param end_index: 0-based absolute character offset (exclusive)
        within the file where the anchor range ends.
    :param body: The comment text.
    :param status: One of ``"draft"``, ``"addressed"``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch **microseconds** of the last
        body/status mutation; set at creation for never-edited
        comments. Feeds the per-session comments fingerprint surfaced
        on ``GET /v1/sessions`` so clients can detect comment changes;
        microsecond precision keeps back-to-back mutations within one
        second distinguishable while remaining an exact integer in
        JavaScript. ``BigInteger`` because epoch-µs overflows a
        32-bit column on PostgreSQL.
    :param anchor_content: Plain-text snapshot of the selected range at
        comment creation time. Used to re-anchor the comment (e.g. via
        content search) when the file is subsequently edited.
        ``NULL`` for legacy comments created before anchor support.
    :param created_by: Email of the user who created this comment,
        e.g. ``"alice@example.com"``. ``NULL`` for legacy comments or
        comments created in single-user mode.
    """

    __tablename__ = "comments"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    # conversation_id leads id in the PK so a conversation's comments stay
    # contiguous for the per-conversation prefix scans that dominate reads
    # (list_for_conversation, fingerprints, cascade delete). This subsumes the
    # old ix_comments_conversation_id secondary index; list_for_conversation's
    # ORDER BY created_at now filesorts the (small) per-conversation row set.
    conversation_id: Mapped[str] = mapped_column(
        Uuid16(),
        primary_key=True,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    path: Mapped[str] = mapped_column(String(4096))
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(CompressedText)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # COMMENT_STATUS: draft=1, addressed=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    anchor_content: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (CheckConstraint("status IN (1, 2)", name="ck_comments_status"),)


def policy_name_cksum(name: str) -> bytes:
    """Return the sha256 digest of a policy name.

    This 32-byte digest is what the name-uniqueness indexes key on instead
    of the raw ``VARCHAR(256)`` name — a fixed, compact index entry. Two
    names collide iff their digests do, so uniqueness is preserved.
    """
    return hashlib.sha256(name.encode("utf-8")).digest()


def _default_policy_name_cksum(context: Any) -> bytes:
    """Column default: derive ``name_cksum`` from the bound ``name`` on INSERT.

    Mirrors the ``workspace_id`` default pattern so every ORM insert stamps
    the checksum without the caller setting it. Column defaults do not fire
    on UPDATE, so renames recompute it explicitly in the store.
    """
    return policy_name_cksum(context.get_current_parameters()["name"])


class SqlPolicy(OmnigentBase):
    """
    SQLAlchemy model for the ``policies`` table.

    Policies are either session-scoped (``session_id`` set, FK to
    ``conversations.id``) or server-wide defaults
    (``session_id IS NULL``).

    Session-scoped policies are created via
    ``POST /v1/sessions/{session_id}/policies``. Default policies
    are created via ``POST /v1/policies``.

    :param id: Opaque PK, e.g. ``"pol_a1b2c3..."``.
    :param name: Human-readable name. UNIQUE per session for
        session policies; globally unique for default policies
        (``session_id IS NULL``). Uniqueness is enforced in the
        store (application layer), not by a DB constraint, and
        keys on ``name_cksum`` rather than this column.
    :param name_cksum: sha256 digest of ``name`` (32 bytes). The
        store's name-uniqueness checks key on this compact digest
        instead of the wide ``VARCHAR(256)`` name, backed by
        ``ix_policies_name_cksum``. Stamped on INSERT by a column
        default; recomputed by the store on rename.
    :param session_id: FK to ``conversations.id``. ``None`` for
        server-wide default policies. ``ON DELETE CASCADE`` so
        removing a session cleans up its policies.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write,
        ``None`` if the row has never been updated.
    :param type: Handler discriminator: ``"python"``,
        ``"url"``.
    :param handler: Dotted import path (``type="python"``)
        or HTTPS URL (``type="url"``).
    :param factory_params: JSON-encoded dict of kwargs passed to
        the handler when it is a factory function. ``None`` when
        the handler is a direct callable or for ``type="url"``.
    :param enabled: Whether the engine consults this row.
        Defaults to true.
    :param scope: ``"default"`` for server-wide policies;
        ``"session"`` for session-scoped policies. Explicit
        discriminator so queries filter by column value instead
        of checking ``session_id IS NULL``.
    :param created_by: User ID of the admin who created this
        policy. ``None`` in single-user mode or for
        session-scoped policies.
    """

    __tablename__ = "policies"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    # sha256(name) — the value the name-uniqueness indexes key on instead of
    # the wide name column. Stamped from `name` on INSERT via the column
    # default; the store recomputes it on rename (defaults don't fire on UPDATE).
    name_cksum: Mapped[bytes] = mapped_column(_CKSUM32, default=_default_policy_name_cksum)
    # Nullable: NULL for server-wide default policies.
    session_id: Mapped[str | None] = mapped_column(
        Uuid16(),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Handler discriminator stored as a stable int code (see
    # omnigent.db.enum_codecs POLICY_TYPE: python=1, url=2).
    type: Mapped[int] = mapped_column(SmallInteger)
    # Dotted import path (type="python") or HTTPS URL
    # (type="url") for the policy handler. Opaque; never SQL-filtered
    # — stored compressed (CompressedText).
    handler: Mapped[str] = mapped_column(CompressedText)
    # JSON-encoded dict of factory kwargs for type="python" when
    # the handler is a factory function. NULL when the handler is
    # a direct callable or for type="url". See the design doc's
    # FunctionRef.arguments pattern. Opaque; never SQL-filtered
    # — stored compressed (CompressedText).
    factory_params: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    # "default" for server-wide policies; "session" for per-conversation
    # copies. Mirrors the agents.kind pattern so queries filter by column
    # value rather than session_id IS NULL. Enum stored as a stable int
    # code (see omnigent.db.enum_codecs POLICY_SCOPE: default=1, session=2).
    scope: Mapped[int] = mapped_column(SmallInteger)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("type IN (1, 2)", name="ck_policies_type"),
        CheckConstraint("scope IN (1, 2)", name="ck_policies_scope"),
        # One index serves both listing paths. scope leads (the global-vs-
        # session discriminator), then session_id:
        #   - list_defaults:     WHERE workspace_id=? AND scope='default'
        #   - list_for_session:  WHERE workspace_id=? AND scope='session'
        #                              AND session_id=?
        # scope must precede session_id so the defaults query (which does not
        # constrain session_id) can still seek. Both listings sort their small
        # result set by created_at in memory (created_at is deliberately left
        # out — it can't cover both sorts, see migration d4c1b9e6f3a2).
        # Any future session_id lookup must also constrain scope to seek here.
        Index("ix_policies_scope_session", "workspace_id", "scope", "session_id", "id"),
        # Name uniqueness is enforced in the store, not by a DB constraint:
        # default policies must have globally-unique names while session
        # policies must be unique only within their session — a "unique within
        # a subset" rule that can't be a partial unique index (MySQL has none).
        # The store's create/update checks (session) and create_default/
        # update_default (default) key on name_cksum; this plain index backs
        # those lookups.
        Index("ix_policies_name_cksum", "workspace_id", "name_cksum", "id"),
    )


class SqlHost(OmnigentBase):
    """
    SQLAlchemy model for the ``hosts`` table.

    Each row represents a machine that has connected to the server
    via ``omnigent host``. The row is upserted on first connect
    and updated on subsequent reconnects (name, status, timestamps).

    :param host_id: Stable host identifier from the host's local
        ``~/.omnigent/config.yaml``, e.g. ``"host_a1b2c3d4e5f6..."``.
    :param name: Human-readable name from ``config.yaml``, e.g.
        ``"corey-laptop"``. Displayed in the Web UI host picker. Max 64
        characters.
    :param user_id: User ID from the Databricks auth Bearer token
        presented during the host's WebSocket handshake, e.g.
        ``"corey.zumar@databricks.com"``.
    :param status: ``"online"`` when the host has an active WebSocket
        connection, ``"offline"`` when disconnected.
    :param created_at: Unix epoch seconds when the host was first
        registered (first ``omnigent host``).
    :param updated_at: Unix epoch seconds the row was last touched — a
        status change (connect/disconnect) or a tunnel heartbeat. Doubles
        as the host's last-seen for the liveness freshness gate, so a
        host that crashed without a graceful disconnect ages out of the
        "online" set once this stops advancing.
    :param token_hash: Hex SHA-256 digest of the launch token that
        authenticates a SERVER-MANAGED sandbox host's tunnel connection
        (``host_type="managed"`` sessions) — never the raw token.
        ``NULL`` for external (user-connected) hosts. Overwritten when
        the sandbox is relaunched, which atomically revokes the
        previous generation's token.
    :param token_expires_at: Unix epoch seconds after which the launch
        token no longer authenticates. Scoped to the TOKEN, not the
        host — the host row is durable across sandbox generations; the
        expiry is set past the provider's maximum sandbox lifetime so a
        live sandbox can always reconnect while a token leaked from a
        dead one cannot. ``NULL`` for external hosts.
    :param sandbox_provider: Host provider backing a managed host,
        e.g. ``"docker"``. ``NULL`` for external hosts — non-NULL is the
        "this host is server-managed" discriminator.
    :param sandbox_id: Provider-assigned id of the sandbox currently
        backing the host, e.g. ``"sb-a1b2c3"`` — what termination is
        issued against. ``NULL`` for external hosts.
    :param terminating_sandbox_id: Provider-assigned id detached from the
        active host generation and awaiting successful provider termination.
        A fresh generation may be registered in ``sandbox_id`` while this
        cleanup remains pending.
    :param deleted_at: Logical deletion timestamp for a managed host whose
        provider sandbox cleanup is still pending. The row is physically
        removed after every recorded sandbox id terminates successfully.
    :param configured_harnesses: JSON-encoded per-harness readiness map
        reported in the host's last ``host.hello`` frame, e.g.
        ``'{"claude-sdk": true, "codex": false}'``. ``NULL`` when the
        host has never reported it (older host build) — unknown, not
        "nothing configured". Surfaced via ``GET /v1/hosts`` so the web
        agent picker can warn about unconfigured harnesses.
    """

    __tablename__ = "hosts"
    account_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    host_id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    # Session-owner identity from the Databricks auth Bearer token. String(128)
    # matches session_permissions.user_id and every other user-identity column
    # in this schema.
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # HOST_STATUS: online=1, offline=2).
    status: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sandbox_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    terminating_sandbox_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Opaque; never SQL-filtered — stored compressed (CompressedText).
    configured_harnesses: Mapped[str | None] = mapped_column(CompressedText, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2)",
            name="ck_hosts_status",
        ),
        # (workspace_id, user_id, name) was the old PK; keep it unique so the
        # upsert-on-connect logic (look up by user_id+name to detect host_id
        # rotation) stays consistent.
        UniqueConstraint(
            "workspace_id", "user_id", "name", name="uq_hosts_workspace_user_id_name"
        ),
        Index("ix_hosts_sandbox_scan", "sandbox_id", "workspace_id", "host_id"),
        Index(
            "ix_hosts_terminating_sandbox_scan",
            "terminating_sandbox_id",
            "workspace_id",
            "host_id",
        ),
    )


class SqlUserDailyCost(OmnigentBase):
    """
    SQLAlchemy model for the ``user_daily_cost`` table.

    A running per-user, per-UTC-day rollup of LLM spend, used by
    cost-aware policies (e.g. the "downgrade expensive model once a
    user has spent >$X today" sample policy) to read a user's
    accumulated daily cost as a single O(1) point lookup instead of
    aggregating the per-session ``conversations.session_usage`` blobs
    on every policy evaluation.

    One row per ``(user_id, day_utc)``. Incremented (UPSERT
    ``cost_usd = cost_usd + delta``) at each turn boundary from the
    cost write sites — but only when the session runs under at least
    one policy, so the table is never touched in deployments that
    have no policies configured (this keeps the shared server code
    inert against a database that lacks this table).

    :param user_id: The user the cost is attributed to — the session
        creator (``LEVEL_OWNER`` grantee), e.g.
        ``"alice@example.com"``.
    :param day_utc: The UTC calendar day the spend occurred, as an
        ISO date string ``"YYYY-MM-DD"``, e.g. ``"2026-06-05"``.
        Bucketed by the turn's wall-clock time, so a session spanning
        midnight splits its cost across both days correctly.
    :param cost_usd: Cumulative USD spend for this user on this day.
        Starts at the first turn's delta and grows by each subsequent
        turn's delta.
    :param ask_approved_usd: Highest soft warning checkpoint (USD) the
        user has already approved continuing past for this day — read
        and written by the per-user daily cost-budget policy so an
        approved checkpoint prompts at most once per day (across all of
        the user's sessions), not once per session. ``0.0`` (the
        server default) means no checkpoint approved yet.
    :param updated_at: Unix epoch seconds of the last increment.
    """

    __tablename__ = "user_daily_cost"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    ask_approved_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer)


class SqlScheduledTask(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_tasks`` table.

    A scheduled task is a saved, scheduled instruction that fires an agent
    session on a recurring schedule (``rrule``).

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param rrule: The required RFC 5545 recurrence rule for the recurring
        trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``. Evaluated in
        ``timezone``.
    :param user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for — who the run belongs to, e.g. ``"alice@example.com"``.
        ``None`` in single-user / OSS mode; the fire path resolves it to the
        reserved ``"local"`` user.
    :param agent_id: The agent bound to this task (relates to
        ``agents.id``). Cascade cleanup on agent deletion is application-owned
        — there is no DB-level foreign key (schema Rule R032).
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param permission_mode: Per-task permission mode for native coding harnesses
        that support one (Claude Code), e.g. ``"acceptEdits"``. The fire path
        turns it into the runner's ``--permission-mode`` launch arg. ``None``
        means use the agent default.
    :param workspace: Absolute path on disk where a fired session's runner
        should start (the source repo / working dir). ``None`` when unset.
    :param base_branch: Git base ref a firing branches FROM when it creates a
        worktree at fire time (mirrors session-create's ``git.base_branch``
        input). Pairs with ``workspace``:
        ``workspace`` is where, ``base_branch`` is what to branch from. ``None``
        when unset. The per-run *output* branch is not stored on the definition.
    :param execution_target: Where a firing runs —
        ``connected_host``/``managed_sandbox``. ``connected_host`` resolves the
        owner's live host at fire time (see ``host_id``); ``managed_sandbox``
        provisions/adopts a sandbox at fire time. Stored as a stable int code
        (see omnigent.db.enum_codecs SCHEDULED_TASK_EXECUTION_TARGET); the store
        converts to/from the string name at the row↔entity boundary. Defaults to
        ``connected_host``.
    :param host_id: For ``execution_target=connected_host``, the specific host
        to run on (relates to ``hosts.host_id``; no DB foreign key, Rule R032).
        ``None`` means "the owner's freshest online host". Always ``None`` for
        ``managed_sandbox`` (the sandbox is provisioned/adopted under a
        deterministic id at fire time, so there is nothing to pin).
    :param timezone: IANA timezone the trigger is evaluated in, e.g.
        ``"America/Los_Angeles"``.
    :param state: Lifecycle state — ``active``/``paused``/``deleted``.
        The scheduler only dispatches ``active`` tasks.
        Stored as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_STATE); the store converts to/from the string name at the
        row↔entity boundary. Defaults to ``active``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: The conversation created by the most recent
        firing (relates to ``conversations.id``). ``None`` if never fired or the
        referenced conversation was deleted (application-owned SET-NULL cleanup;
        no DB foreign key).
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    __tablename__ = "scheduled_tasks"
    account_generation: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # Opaque free text, never SQL-queried — stored compressed (CompressedText).
    prompt: Mapped[str] = mapped_column(CompressedText, nullable=False)
    # RFC 5545 recurrence rule, e.g. "FREQ=DAILY;BYHOUR=9;BYMINUTE=0".
    rrule: Mapped[str] = mapped_column(String(512), nullable=False)
    # Session-owner identity: the spawned run's LEVEL_OWNER grant is written
    # for this user. Nullable — None in single-user/OSS mode (the fire path
    # resolves null to the reserved "local" user). String(128) to match
    # session_permissions.user_id (the column the LEVEL_OWNER grant is
    # written into) and every other user-identity column in this schema.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Relates to agents.id. No DB foreign key (Rule R032); cascade is app-owned.
    agent_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Per-task overrides — None means fall back to the agent default. Widths
    # mirror the matching conversations.* override columns.
    model_override: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-task permission mode for native coding harnesses that carry one
    # (Claude Code). The fire path converts it to the runner's
    # ``--permission-mode`` launch arg. NULL = use the agent default.
    permission_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Per-firing cost budget in USD. When set, the fire path attaches a
    # cost_budget policy to each spawned session. NULL = no per-firing cap.
    max_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    workspace: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Git base ref a firing branches from when it creates a worktree at fire
    # time (mirrors session-create's git.base_branch input). None when unset.
    base_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Where a firing runs, as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_EXECUTION_TARGET: connected_host=1, managed_sandbox=2).
    # connected_host → resolve the owner's live host at fire time (see host_id);
    # managed_sandbox → provision/adopt a sandbox at fire time. Defaults to
    # connected_host so existing rows keep connected-host behavior. The store
    # converts to/from the string name at the row↔entity boundary.
    execution_target: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    # For execution_target=connected_host: the specific host to run on (relates
    # to hosts.host_id; No DB foreign key, Rule R032). None = "the owner's
    # freshest online host, whichever". Always None for managed_sandbox (the
    # sandbox is provisioned/adopted under a deterministic id at fire time, so
    # there is nothing to pin here).
    host_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_STATE: active=1, paused=2, deleted=3). The
    # store converts to/from the string name at the row↔entity boundary.
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Relates to conversations.id. No DB foreign key (Rule R032); the
    # application nulls this out when the referenced conversation is deleted.
    last_run_conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    # Parent binding: when set, each fire creates a sub-agent child of this
    # session (relates to conversations.id; no DB foreign key) so the result
    # wakes it. ``agent_type`` names the sub-agent Type within the parent's agent.
    parent_session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    agent_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Explicit product marker set at creation (e.g. ``followed_topic``); ``None`` = unmarked.
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Epoch seconds fixing the phase of INTERVAL>1 rules: the person's start date, else when the
    # rule was set. ``None`` on rows that predate the column (read as ``created_at``).
    anchor_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("state IN (1, 2, 3)", name="ck_scheduled_tasks_state"),
        CheckConstraint("execution_target IN (1, 2)", name="ck_scheduled_tasks_execution_target"),
        # One user-scoped listing index. Covers "a user's tasks ordered by
        # created_at" (GET /scheduled-tasks) as a covered seek; the scheduler's
        # state scan reads whole rows regardless of any index.
        Index(
            "ix_scheduled_tasks_user_scope",
            "workspace_id",
            "user_id",
            "created_at",
            "id",
        ),
    )


class SqlOwnerPreferences(OmnigentBase):
    """
    SQLAlchemy model for the ``owner_preferences`` table.

    Per-owner gating preferences for parent-bound scheduled runs.

    :param user_id: Owner identity (``"local"`` in single-user mode).
    :param proactivity: ``off`` / ``low`` / ``normal`` / ``high``. ``off`` skips every
        parent-bound fire for the owner.
    :param quiet_start: Local ``HH:MM`` the quiet window starts, or ``None``.
    :param quiet_end: Local ``HH:MM`` the quiet window ends, or ``None``.
    :param timezone: IANA timezone the quiet window is evaluated in.
    :param updated_at: Unix epoch seconds of the last write.
    """

    __tablename__ = "owner_preferences"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    proactivity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    quiet_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "proactivity IN ('off', 'low', 'normal', 'high')",
            name="ck_owner_preferences_proactivity",
        ),
    )


class SqlSuggestion(OmnigentBase):
    """
    SQLAlchemy model for the ``suggestions`` table.

    A concrete thing an agent could do for the person next (an Idea), recorded
    by a background run and acted on from a UI.

    :param id: UUID primary key (bare 32-char hex string).
    :param user_id: Owner identity; ``None`` in single-user mode.
    :param parent_session_id: The session the suggestion belongs to (relates to
        ``conversations.id``; no DB foreign key, Rule R032).
    :param title: Short headline.
    :param why: One-line reason it is worth doing.
    :param message: The exact message to send to the parent session if accepted.
    :param status: ``open``/``done``/``dismissed``.
    :param source_session_id: The run (Helper session) that recorded it, if any.
    """

    __tablename__ = "suggestions"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_session_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    why: Mapped[str] = mapped_column(CompressedText, nullable=False)
    message: Mapped[str] = mapped_column(CompressedText, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    source_session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'done', 'dismissed')",
            name="ck_suggestions_status",
        ),
        Index("ix_suggestions_parent", "workspace_id", "parent_session_id", "created_at", "id"),
        Index("ix_suggestions_owner", "workspace_id", "user_id", "status", "created_at"),
    )


class SqlArtifact(OmnigentBase):
    """
    SQLAlchemy model for the ``artifacts`` table.

    One row per *version* of a deliverable file (HTML page, document, sheet, deck...) the Muse
    saved from its host. Versions of one deliverable share ``(user_id, parent_session_id,
    name)``; the bytes live in the artifact store under ``blob_key``.

    :param id: UUID primary key of this version (bare 32-char hex string).
    :param user_id: Owner identity; ``None`` in single-user mode.
    :param parent_session_id: The Conversation it belongs to (relates to ``conversations.id``;
        no DB foreign key, Rule R032).
    :param name: File name, e.g. ``"report.html"``; with the session it groups versions.
    :param title: Optional display title.
    :param kind: Lower-case extension kind, e.g. ``"html"``.
    :param mime: Served content type.
    :param size: Byte length of the content.
    :param version: 1-based version number within the group.
    :param blob_key: Artifact-store key of the bytes.
    :param published: Reserved for a later "publish" feature.
    :param origin: ``"ai"`` (saved by the Muse), ``"manual"`` (edited by hand) or ``"restore"``.
    :param parent_version_id: The version this one was derived from, if any.
    :param source_path: host workspace path the bytes were read from / are written back to.
    :param edit_summary: Short human text describing a manual edit.
    :param delivered_at: Epoch seconds when a manual version was written back to the host
        and announced to the Muse; ``None`` while still pending.
    :param turn_item_id: The person's message item that opened the turn that saved it, if known.
    :param chat_id: The chat (Conversation, Helper or update run) that saved it.
    :param item_id: The person's message in the Conversation that led to it: the turn item of
        the Conversation's own save, or for a Helper's save the Conversation message the Helper
        was started from. ``None`` for hand edits, restores and unknown turns.
    :param group_id: The deliverable's stable id, the same on every version: its first version's
        id (ADR 0024).
    :param round_id: The Round of the chat that saved it (ADR 0029), stamped at save; ``None``
        for a save outside a Round and for older versions.
    """

    __tablename__ = "artifacts"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_session_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    blob_key: Mapped[str] = mapped_column(String(128), nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ai")
    parent_version_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    edit_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turn_item_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    item_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    group_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    round_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)

    __table_args__ = (
        Index(
            "ix_artifacts_group",
            "workspace_id",
            "user_id",
            "parent_session_id",
            "name",
            "version",
        ),
        Index(
            "uq_artifacts_session_name_version",
            "workspace_id",
            "parent_session_id",
            "name",
            "version",
            unique=True,
        ),
        Index("ix_artifacts_owner", "workspace_id", "user_id", "created_at"),
        Index("ix_artifacts_group_id", "workspace_id", "group_id", "version"),
        Index("ix_artifacts_round", "workspace_id", "parent_session_id", "round_id"),
    )


class SqlArtifactPublication(OmnigentBase):
    """
    SQLAlchemy model for the ``artifact_publications`` table.

    One row per *published* deliverable (an artifact group: ``user_id`` + ``parent_session_id``
    + ``name``). Presence of the row means "published"; deleting it unpublishes.

    :param slug: URL-safe public address segment (title + short random suffix); unique.
    :param user_id: Owner identity; ``None`` in single-user mode.
    :param parent_session_id: The Conversation of the deliverable.
    :param name: File name of the deliverable.
    :param audience: ``"owner"``, ``"org"`` or ``"link"``.
    :param published_version: The pinned version number that is served.
    :param published_at: Epoch seconds of the first publish.
    :param updated_at: Epoch seconds of the last republish / audience change.
    """

    __tablename__ = "artifact_publications"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    slug: Mapped[str] = mapped_column(String(96), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_session_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    audience: Mapped[str] = mapped_column(String(8), nullable=False)
    published_version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index(
            "uq_artifact_publications_group",
            "workspace_id",
            "parent_session_id",
            "name",
            unique=True,
        ),
        Index("ix_artifact_publications_owner", "workspace_id", "user_id"),
    )


class SqlMessageArtifact(OmnigentBase):
    """
    SQLAlchemy model for the ``message_artifacts`` table: which artifact a person's message is
    about (docs/conversation.md). One row per linked message; no row means no link yet.

    :param session_id: The Conversation the message is in.
    :param item_id: The person's message item.
    :param artifact_id: The linked version's id, or ``None`` when the person said the message is
        about no artifact.
    :param name: The artifact's file name when it was linked.
    :param version: The version the message produced, once there is one.
    :param how: ``"open"`` (open beside the chat when sent), ``"edited"`` (a save during its
        turn) or ``"chosen"`` (picked on the chip).
    :param updated_at: Epoch seconds of the last change.
    """

    __tablename__ = "message_artifacts"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    session_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    item_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    how: Mapped[str] = mapped_column(String(8), nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlArtifactLive(OmnigentBase):
    """
    SQLAlchemy model for the ``artifact_live`` table: one row per live page (an artifact group).

    The schedule is the scheduled task ``task_id``; ``sources`` is a JSON list of web page URLs
    or host file paths; ``state`` is what the updates remember (``superchat/live/store.py``).
    """

    __tablename__ = "artifact_live"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    parent_session_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    sources: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    tell: Mapped[str] = mapped_column(String(8), nullable=False, server_default="notable")
    follow_publish: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")

    __table_args__ = (
        Index("ix_artifact_live_task", "workspace_id", "task_id"),
        Index("ix_artifact_live_owner", "workspace_id", "user_id"),
    )


class SqlArtifactView(OmnigentBase):
    """
    SQLAlchemy model for the ``artifact_views`` table.

    Opens of a published app, one row per (slug, UTC day, viewer key). The viewer key is an
    account id or a daily-rotating salted hash computed by the caller; never a raw IP.

    :param slug: The publication slug.
    :param day: UTC day, ``YYYY-MM-DD``.
    :param viewer_key: Opaque viewer key (<= 64 chars).
    :param opens: Counted opens by that viewer on that day.
    :param last_at: Epoch seconds of the last counted open (rapid repeats are not counted).
    """

    __tablename__ = "artifact_views"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    slug: Mapped[str] = mapped_column(String(96), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    viewer_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    opens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    last_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlDailyNote(OmnigentBase):
    """
    SQLAlchemy model for the ``daily_notes`` table.

    One note per owner per local day: what we talked about, decisions, promises and open
    loops. Written by background passes, editable by the person.

    :param owner: Owner identity (``"local"`` in single-user mode); with the date it is the key.
    :param note_date: Local calendar day, ``YYYY-MM-DD`` in the owner's timezone.
    :param sections: JSON object ``{section_key: text}``.
    :param edited_sections: JSON array of section keys the person has edited (the writer only
        appends to these, never rewrites them).
    :param edited_by_person: ``True`` once the person edited any section.
    :param parent_session_id: The Super Chat the day's passes ran for (no DB foreign key).
    :param quiet_passes: Quiet-moment passes started for this owner on this day.
    :param finalized_at: Epoch seconds the nightly finalizing pass succeeded, or ``None``.
    """

    __tablename__ = "daily_notes"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    note_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    sections: Mapped[str] = mapped_column(CompressedText, nullable=False)
    edited_sections: Mapped[str] = mapped_column(String(512), nullable=False, server_default="[]")
    edited_by_person: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    parent_session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    quiet_passes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    finalized_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Epoch seconds the nightly Dreaming / People passes succeeded for this day.
    dreamed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    people_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Epoch seconds a finalize / Dreaming / People pass took the lease; the done markers above
    # are set only when a pass succeeded.
    claimed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlTopic(OmnigentBase):
    """
    SQLAlchemy model for the ``topics`` table (ADR 0035).

    One row per owner per topic, upserted by ``key`` by the quiet-moment pass. The lists the
    web draws (covered, open, starters, events, sources, watch) live in ``body``; what lists
    filter and sort on are columns.

    :param owner: Owner identity (``"local"`` in single-user mode); with ``key`` the key.
    :param key: Stable slug the pass reuses when the topic comes back, e.g. ``"polaris-launch"``.
    :param title: 2 to 5 words in the person's terms.
    :param status: ``"open"`` or ``"closed"`` (closed ones are hidden, never deleted).
    :param body: JSON object: ``left_off``, ``covered``, ``open``, ``starters``, ``events``,
        ``sources``, ``watch`` and the ``since`` line.
    :param last_touched_at: Epoch seconds of the newest event.
    :param since_at: Epoch seconds the ``since`` ("New") line was set, or ``None``.
    """

    __tablename__ = "topics"
    __table_args__ = (
        Index("ix_topics_owner_touched", "workspace_id", "owner", "last_touched_at"),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    body: Mapped[str] = mapped_column(CompressedText, nullable=False)
    last_touched_at: Mapped[int] = mapped_column(Integer, nullable=False)
    since_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlActivityTitle(OmnigentBase):
    """
    SQLAlchemy model for the ``activity_titles`` table.

    The model-written title of one chat turn in the Activity Feed, keyed by the
    turn's response id. Activities themselves are derived on read; only this
    presentational title is stored.

    :param response_id: The turn's response id; primary key with the workspace.
    :param conversation_id: The chat the title was generated for (no DB foreign
        key, Rule R032).
    :param title: Short action-first title, e.g. ``"Check Tesla's latest price"``.
    :param summary: Short model-written summary of the turn's result, once finished.
    :param created_at: Unix epoch seconds at row creation.
    """

    __tablename__ = "activity_titles"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    response_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlTask(OmnigentBase):
    """
    SQLAlchemy model for the ``tasks`` table: one deliverable in progress in a Conversation
    (ADR 0025). The engine writes it from what happened (a tool call, a card answered, a helper
    started, a save), never from the model's word.

    :param id: The task's id (32 hex).
    :param owner: The Conversation owner's key.
    :param conversation_id: The Conversation it belongs to (no DB foreign key, Rule R032).
    :param kind: ``deck``, ``page``, ``document``, ``sheet`` or ``file``.
    :param title: What the deliverable is called.
    :param fields: JSON object of what the kind needs (a deck: look, slides) and the answers.
    :param status: ``open``, ``building``, ``done`` or ``dropped``.
    :param artifact_group_ids: JSON list of the artifact groups it produced.
    :param helper_ids: JSON list of the Helpers started for it.
    :param created_at: Epoch seconds at creation.
    :param updated_at: Epoch seconds of the last change.
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_conversation", "workspace_id", "conversation_id", "status"),)

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    fields: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_group_ids: Mapped[str] = mapped_column(Text, nullable=False)
    helper_ids: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlCardAnswer(OmnigentBase):
    """
    SQLAlchemy model for the ``card_answers`` table: the person's pick on an ``ask`` card, kept on
    the message that carried it (ADR 0025). One row per answering message.

    :param session_id: The Conversation the message is in.
    :param item_id: The person's message item.
    :param card_id: The card the answer names, as the client sent it.
    :param option_id: The option picked.
    :param label: The option's label, as the person read it.
    :param task_id: The task the card asked for, when it asked for one.
    :param created_at: Epoch seconds when it was stored.
    """

    __tablename__ = "card_answers"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    session_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    item_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    card_id: Mapped[str] = mapped_column(String(200), nullable=False)
    option_id: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    task_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlRoundEvent(OmnigentBase):
    """
    SQLAlchemy model for the ``round_events`` table: what happened in a Round, one fact per row,
    appended as the engine sees it (ADR 0029). A Round has no row of its own; it is the set of
    events that share a ``round_id``.

    :param round_id: The Round: the id of the person's message that opened it, or a hidden id
        for a scheduled or system run.
    :param seq: The event's place in its Round, from 1.
    :param conversation_id: The Conversation the Round is in (no DB foreign key, Rule R032).
    :param kind: What happened, e.g. ``round.opened``, ``tool.started``, ``helper.finished``.
    :param actor: Who did it: ``person``, ``system``, ``assistant`` or ``helper``.
    :param body: JSON object of the kind's fields, e.g. ``{"call_id": "...", "name": "..."}``.
    :param at_ms: Epoch milliseconds when the engine saw it.
    """

    __tablename__ = "round_events"
    __table_args__ = (
        Index(
            "ix_round_events_conversation", "workspace_id", "conversation_id", "round_id", "seq"
        ),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    round_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SqlOutboxItem(OmnigentBase):
    """
    SQLAlchemy model for the ``outbox_items`` table: one row per proactive message (ADR 0036).

    A row is opened when a scheduled run's notice starts a turn of the Conversation, and bound to
    the message that turn writes; a later message of the same turn moves it there (the turn's
    last text is its reply). Channels read bound rows after their own cursor.

    :param id: The notice that started the turn (the row's origin).
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param conversation_id: The Conversation (no DB foreign key, Rule R032).
    :param item_id: The proactive message, ``None`` until the turn writes one.
    :param response_id: The turn that wrote it.
    :param kind: The source's kind, e.g. ``automation``, ``topic_watch``, ``daily_brief``.
    :param topic_key: The topic it is about, when the source named one.
    :param urgency: ``now``, ``today`` or ``brief``.
    :param source: JSON object of what the source matched on, e.g. ``{"sender": "..."}``.
    :param title: Code-cut from the message: first heading or sentence, at most 80 characters.
    :param line: Code-cut from the message: first sentence after the title, at most 160.
    :param created_at_ms: Epoch milliseconds the row was bound (opened, while unbound).
    :param open: The turn may still write to it.
    """

    __tablename__ = "outbox_items"
    __table_args__ = (
        Index("ix_outbox_items_owner", "workspace_id", "owner", "created_at_ms"),
        Index("ix_outbox_items_item", "workspace_id", "item_id"),
        Index("ix_outbox_items_open", "workspace_id", "open"),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    item_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    topic_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    urgency: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    title: Mapped[str] = mapped_column(String(320), nullable=False, server_default="")
    line: Mapped[str] = mapped_column(String(640), nullable=False, server_default="")
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())


class SqlOutboxCursor(OmnigentBase):
    """
    SQLAlchemy model for the ``outbox_cursors`` table: how far one channel got for one owner.

    The ``web`` channel's cursor is the owner's ``last_seen_item_id`` (ADR 0036).

    :param channel: e.g. ``"web"``.
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param cursor: The last proactive message the channel delivered (or the person saw).
    :param cursor_ms: That message's row time, so a cursor survives the row moving on.
    """

    __tablename__ = "outbox_cursors"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    channel: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    cursor: Mapped[str] = mapped_column(Uuid16, nullable=False)
    cursor_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SqlOutboxFeedback(OmnigentBase):
    """
    SQLAlchemy model for the ``outbox_feedback`` table: the person's *Not useful* on a proactive
    message, the stored rule topic watch turns into a mute (ADR 0035, 0036).

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param item_id: The proactive message.
    :param topic_key: Its topic, when it had one.
    :param reason: The person's words, if any.
    :param at: Epoch seconds it was recorded.
    """

    __tablename__ = "outbox_feedback"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    topic_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlRhythmTally(OmnigentBase):
    """
    SQLAlchemy model for the ``rhythm_tallies`` table: what the person's behaviour after
    proactive messages said about one situation (ADR 0038, Rhythm).

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param cell: The situation, ``<day-part>:<weekday|weekend>:<chatting|not>``.
    :param yes: Weighted yes count, decayed to ``updated_at``.
    :param no: Weighted no count, decayed to ``updated_at``.
    :param updated_at: Epoch seconds of the last change.
    """

    __tablename__ = "rhythm_tallies"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    cell: Mapped[str] = mapped_column(String(32), primary_key=True)
    yes: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    no: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlRhythmEvent(OmnigentBase):
    """
    SQLAlchemy model for the ``rhythm_events`` table: one behaviour event on one proactive
    message (ADR 0038, Rhythm); the first of each kind per message is kept.

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param item_id: The proactive message.
    :param event: ``sent``, ``shown``, ``opened``, ``acted_on`` or ``unseen`` (the sweep's).
    :param at: Epoch seconds it happened.
    :param cell: The situation the message was sent in, ``None`` outside the day-parts.
    :param topic_keys: On ``sent`` only: JSON list of every topic the message covers (the
        one-per-topic limit counts each), else ``None``.
    """

    __tablename__ = "rhythm_events"
    __table_args__ = (Index("ix_rhythm_events_owner_at", "workspace_id", "owner", "at"),)

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    item_id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    event: Mapped[str] = mapped_column(String(16), primary_key=True)
    at: Mapped[int] = mapped_column(Integer, nullable=False)
    cell: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic_keys: Mapped[str | None] = mapped_column(Text, nullable=True)


class SqlReachRule(OmnigentBase):
    """
    SQLAlchemy model for the ``reach_rules`` table: how the person said to be reached (ADR 0038,
    "what the person says is a rule"), read by the rhythm before anything it learned.

    :param id: Rule id (32-char hex).
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param kind: ``pause``, ``level``, ``hours``, ``only_for`` or ``mute``.
    :param params: JSON object, the kind's params.
    :param said: The person's own sentence, short.
    :param created_at: Epoch seconds it was said.
    :param expires_at: Epoch seconds it ends, ``None`` until lifted.
    """

    __tablename__ = "reach_rules"
    __table_args__ = (Index("ix_reach_rules_owner", "workspace_id", "owner"),)

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    params: Mapped[str] = mapped_column(Text, nullable=False)
    said: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlObjective(OmnigentBase):
    """
    SQLAlchemy model for the ``objectives`` table.

    An outcome a person hands to an agent to pursue over time, owned by one
    parent session (the Conversation). Its plan lives in ``objective_tasks``.

    :param id: UUID primary key (bare 32-char hex string).
    :param user_id: Owner identity; ``None`` in single-user mode.
    :param parent_session_id: The session the objective belongs to (relates to
        ``conversations.id``; no DB foreign key, Rule R032).
    :param status: ``active``/``paused``/``done``/``archived``.
    :param due: Optional ISO-8601 date or datetime string.
    :param scheduled_task_id: The scheduled task that advances it (relates to
        ``scheduled_tasks.id``; no DB foreign key), or ``None`` without cadence.
    """

    __tablename__ = "objectives"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_session_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(CompressedText, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    due: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scheduled_task_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'done', 'archived')",
            name="ck_objectives_status",
        ),
        Index("ix_objectives_parent", "workspace_id", "parent_session_id", "created_at", "id"),
        Index("ix_objectives_scheduled_task", "workspace_id", "scheduled_task_id"),
    )


class SqlObjectiveTask(OmnigentBase):
    """
    SQLAlchemy model for the ``objective_tasks`` table: one plan item.

    :param objective_id: Owning objective (no DB foreign key, Rule R032).
    :param position: Zero-based order within the plan.
    :param status: ``pending``/``in_progress``/``done``/``blocked``/``skipped``.
    :param note: Short free-text progress note, or ``None``.
    """

    __tablename__ = "objective_tasks"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    objective_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    note: Mapped[str | None] = mapped_column(CompressedText, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'blocked', 'skipped')",
            name="ck_objective_tasks_status",
        ),
        Index("ix_objective_tasks_objective", "workspace_id", "objective_id", "position"),
    )


class SqlObjectiveProposal(OmnigentBase):
    """
    SQLAlchemy model for the ``objective_proposals`` table.

    A suggested full plan for an objective that the owner accepts or dismisses.
    At most one is ``open`` per objective (enforced by the store).

    :param plan: JSON list of ``{"id": <existing task id or null>, "title": ...}``.
    :param status: ``open``/``accepted``/``dismissed`` (by the owner)/``superseded`` (replaced by
        a newer proposal).
    """

    __tablename__ = "objective_proposals"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    objective_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    reason: Mapped[str] = mapped_column(CompressedText, nullable=False)
    plan: Mapped[str] = mapped_column(CompressedText, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'accepted', 'dismissed', 'superseded')",
            name="ck_objective_proposals_status",
        ),
        Index("ix_objective_proposals_objective", "workspace_id", "objective_id", "status"),
    )


class SqlScheduledTaskRun(OmnigentBase):
    """
    SQLAlchemy model for the ``scheduled_task_runs`` table.

    One row per firing of a scheduled task — the run history. Recorded and
    advanced by the scheduler as a firing moves through its lifecycle.

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param scheduled_task_id: The task this run belongs to (relates to
        ``scheduled_tasks.id``; also a :class:`Uuid16`). Indexed for per-task
        history listing. Cascade cleanup on task deletion is application-owned —
        no DB foreign key (Rule R032).
    :param conversation_id: The conversation created by this firing (relates to
        ``conversations.id``). ``None`` before dispatch, or after the referenced
        conversation is deleted (application-owned SET-NULL; no DB foreign key).
    :param status: Lifecycle state —
        ``scheduled``/``running``/``succeeded``/``failed``/``skipped``. Stored
        as a stable int code (see omnigent.db.enum_codecs
        SCHEDULED_TASK_RUN_STATUS); the store converts to/from the string name
        at the row↔entity boundary.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param fired_at: Unix epoch seconds dispatch actually began, or ``None`` if
        it has not fired yet.
    :param finished_at: Unix epoch seconds the run reached a terminal state, or
        ``None`` if still pending/running.
    :param error: Failure detail when ``status = 'failed'``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retryable-vs-terminal retry logic;
        ``None`` unless ``status = 'failed'``.
    """

    __tablename__ = "scheduled_task_runs"

    # Tenant partition key: Databricks workspace id owning this row (0 = default). Part of the PK.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    # Relates to scheduled_tasks.id. No DB foreign key (Rule R032); cascade is
    # app-owned.
    scheduled_task_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    # Relates to conversations.id. No DB foreign key; app nulls on delete.
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # SCHEDULED_TASK_RUN_STATUS: scheduled=1, running=2, succeeded=3, failed=4,
    # skipped=5). The store converts to/from the string name at the
    # row↔entity boundary.
    status: Mapped[int] = mapped_column(SmallInteger)
    scheduled_at: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Opaque free-text error blob, never SQL-queried — stored compressed.
    error: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    # Short, queryable failure classification token (e.g. "timeout",
    # "rate_limited") for future retry logic. Bounded plain string, not a blob;
    # no CHECK constraint (no code taxonomy defined yet).
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 1 for a regular fire, 2 for the single retry after a failed parent-bound fire.
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")

    __table_args__ = (
        CheckConstraint(
            "status IN (1, 2, 3, 4, 5)",
            name="ck_scheduled_task_runs_status",
        ),
        Index(
            "ix_scheduled_task_runs_scheduled_task_id",
            "workspace_id",
            "scheduled_task_id",
            "scheduled_at",
            "id",
        ),
        # Reverse lookup conversation_id -> run for the event-driven completion
        # hook (get_running_run_by_conversation), which fires on every turn's
        # terminal edge; without this the lookup is a full-table scan.
        Index(
            "ix_scheduled_task_runs_conversation_id",
            "workspace_id",
            "conversation_id",
        ),
    )


class SqlMemoryClaim(OmnigentBase):
    """
    SQLAlchemy model for the ``memory_claims`` table.

    A claim is one durable fact, preference, instruction, or decision learned
    about a user, written by the ``memory_remember`` tool (Phase 1) or a
    future upkeep job (Phase 3). The table is the source of truth; the txtai
    search index (``omnigent/memory/index.py``) is rebuilt from it on demand.
    See ``docs/context.md`` for the field-by-field design.

    :param id: UUID primary key stored as 16 raw bytes (see :class:`Uuid16`),
        surfaced as a bare 32-char hex string (no dashes).
    :param user_id: The user this claim is about. Every store query filters
        on this column — claims never cross users.
    :param kind: One of ``preference``, ``instruction``, ``fact``,
        ``decision``, ``person``, ``project``, ``working_style``. Stored as
        plain text (not a smallint-coded enum like other tables) since the
        memory team may extend this set freely in a later phase.
    :param claim_text: One self-contained sentence describing the claim.
    :param quote: The exact words the evidence was drawn from, or ``None``.
    :param speaker: Who said it (usually the user), or ``None``.
    :param evidence: JSON-encoded list of ``{"session_id", "item_id"}``
        source links.
    :param explicitness: ``"stated"`` (the user said it) or ``"inferred"``
        (deduced from behavior or a correction).
    :param confidence: 0-1. Stated claims start at 0.9, inferred at 0.4.
    :param first_seen: Unix epoch seconds the claim was first written.
    :param reinforced_at: Unix epoch seconds of the most recent
        reinforcement, or ``None`` if never reinforced.
    :param reinforcement_count: Number of times a near-duplicate claim was
        reinforced into this one. Starts at 0.
    :param supersedes_claim_id: The claim this one replaced, or ``None``.
        Relates to ``memory_claims.id``; no DB foreign key (Rule R032).
    :param status: ``active``, ``superseded``, ``expired``, or ``forgotten``.
        Plain text for the same reason as ``kind``.
    :param valid_until: Unix epoch seconds after which the claim no longer
        applies (e.g. "on leave until Nov 3"), or ``None``.
    :param run_id: The upkeep run that wrote this claim, or ``None`` for a
        claim written directly by ``memory_remember``.
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        the row has never been updated.
    """

    __tablename__ = "memory_claims"

    # Tenant partition key: matches every other table after r1a2b3c4d5e6.
    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Opaque free text, never SQL-filtered directly (the txtai index is the
    # search path) — stored compressed like other large text columns.
    claim_text: Mapped[str] = mapped_column(CompressedText, nullable=False)
    quote: Mapped[str | None] = mapped_column(CompressedText, nullable=True)
    speaker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON-encoded list of {"session_id", "item_id"} source links.
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    explicitness: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen: Mapped[int] = mapped_column(Integer, nullable=False)
    reinforced_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reinforcement_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Relates to memory_claims.id. No DB foreign key (Rule R032).
    supersedes_claim_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    valid_until: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True once the person edited the text: upkeep and Helpers never overwrite it.
    person_authored: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('preference', 'instruction', 'fact', 'decision', "
            "'person', 'project', 'working_style', 'commitment')",
            name="ck_memory_claims_kind",
        ),
        CheckConstraint(
            "explicitness IN ('stated', 'inferred')",
            name="ck_memory_claims_explicitness",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'forgotten')",
            name="ck_memory_claims_status",
        ),
        # Covers "a user's active claims" (search/list) as a seek, same shape
        # as ix_scheduled_tasks_user_scope.
        Index(
            "ix_memory_claims_user_scope",
            "workspace_id",
            "user_id",
            "status",
            "created_at",
            "id",
        ),
    )


class SqlMemoryUpkeepRun(OmnigentBase):
    """
    SQLAlchemy model for the ``memory_upkeep_runs`` table.

    One row per Phase 3 memory upkeep run (window -> gate -> extract ->
    verify -> apply -> project -> record). The per-user watermark is the
    last ``succeeded`` run's ``window_until`` — there is no separate
    watermark table — and a ``running`` row doubles as that user's
    single-run lease. See ``docs/context.md``.

    :param id: Unique run identifier (bare 32-char hex string).
    :param user_id: The user this run processed.
    :param window_since: Unix epoch seconds the window started at.
    :param window_until: Unix epoch seconds the window ended at.
    :param state: ``running``, ``succeeded``, ``failed``, or ``skipped``.
    :param disposition: Short machine-readable outcome, or ``None``.
    :param counts: JSON-encoded counts dict (seen/candidates/inserted/
        reinforced/superseded/rejected/rejected_reasons).
    :param started_at: Unix epoch seconds the run started.
    :param finished_at: Unix epoch seconds the run finished, or ``None``
        while ``state == "running"``.
    """

    __tablename__ = "memory_upkeep_runs"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    window_since: Mapped[int] = mapped_column(Integer, nullable=False)
    window_until: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="running")
    disposition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    counts: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed', 'skipped')",
            name="ck_memory_upkeep_runs_state",
        ),
        Index(
            "ix_memory_upkeep_runs_user_scope",
            "workspace_id",
            "user_id",
            "state",
            "started_at",
            "id",
        ),
    )


def _ws_column() -> Mapped[int]:
    return mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )


class SqlApprovalRule(OmnigentBase):
    """
    SQLAlchemy model for the ``approval_rules`` table: a standing rule the person set with
    "Always allow" on an approval card.

    :param id: UUID primary key (bare 32-char hex string).
    :param user_id: Owner the rule belongs to.
    :param category: One of the six risk categories (``send``, ``post``, ``delete``,
        ``spend``, ``upload``, ``share``).
    :param target: Case-insensitive pattern (``*`` wildcards) for who/what the rule covers.
    :param label: Human sentence shown in Settings, e.g. ``"Send emails to my team"``.
    :param decision: ``allow`` (the only kind the UI creates) or ``deny``.
    """

    __tablename__ = "approval_rules"

    workspace_id: Mapped[int] = _ws_column()
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    decision: Mapped[str] = mapped_column(String(8), nullable=False, server_default="allow")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("decision IN ('allow', 'deny')", name="ck_approval_rules_decision"),
        Index("ix_approval_rules_user", "workspace_id", "user_id", "created_at", "id"),
    )


class SqlApprovalSettings(OmnigentBase):
    """SQLAlchemy model for ``approval_settings``: the owner's daily spending cap in USD."""

    __tablename__ = "approval_settings"

    workspace_id: Mapped[int] = _ws_column()
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    daily_cap_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlApprovalSpend(OmnigentBase):
    """SQLAlchemy model for ``approval_spend``: USD let through under standing rules today."""

    __tablename__ = "approval_spend"

    workspace_id: Mapped[int] = _ws_column()
    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    usd: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")


class SqlApprovalPending(OmnigentBase):
    """
    SQLAlchemy model for ``approval_pending``: approval prompts, waiting on the person or decided.

    Persisted so a restart does not forget them; never expires to a yes. A decided prompt keeps
    its row (ADR 0036, amended): ``status`` ``approved`` / ``denied`` with who decided and when,
    or ``expired`` when nobody can answer it any more. Only ``pending`` rows wait.
    """

    __tablename__ = "approval_pending"

    workspace_id: Mapped[int] = _ws_column()
    elicitation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_approval_pending_user", "workspace_id", "user_id", "created_at"),
        Index("ix_approval_pending_session", "workspace_id", "session_id"),
        Index("ix_approval_pending_decided", "workspace_id", "user_id", "decided_at"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_pending_status",
        ),
    )


class SqlVaultSecret(OmnigentBase):
    """
    SQLAlchemy model for the ``vault_secrets`` table: one saved login, encrypted at rest.

    :param id: UUID primary key (bare 32-char hex string).
    :param user_id: Owner identity; ``None`` in single-user mode.
    :param name: Reference name the Muse uses (``{{secret:<name>}}``), unique per owner.
    :param site: HTTPS origin the login was saved for; fills are refused anywhere else.
    :param username: Plain account name (metadata shown in Settings), may be empty.
    :param ciphertext: AES-GCM envelope of the password (never plaintext).
    :param last_used_at: Epoch seconds of the last fill, or ``None``.
    """

    __tablename__ = "vault_secrets"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    site: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    last_used_at: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_vault_secrets_owner", "workspace_id", "user_id", "name"),)


class SqlModelConnection(OmnigentBase):
    """
    SQLAlchemy model for the ``model_connection`` table: a sealed model-provider key.

    The primary key ``(workspace_id, scope, owner_id, provider)`` keeps it to one key per provider
    per owner: saving another replaces the row. Providers are validated in code, not by a CHECK.

    :param scope: ``user`` (a person's own key) or ``org`` (shared by the workspace).
    :param owner_id: The user id for scope ``user``; ``''`` for scope ``org``.
    :param provider: Upstream name from :mod:`omnigent.superchat.models.upstreams`.
    :param ciphertext: AES-GCM envelope of the key (never plaintext).
    :param hint: Last 4 characters of the key, for display.
    :param validated_at: Epoch seconds of the last successful provider probe.
    :param status: ``valid`` or ``invalid``.
    :param label: Optional free-text name shown to the owner.
    """

    __tablename__ = "model_connection"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    scope: Mapped[str] = mapped_column(String(8), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    hint: Mapped[str] = mapped_column(String(8), nullable=False)
    validated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="valid")
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("scope IN ('org', 'user')", name="ck_model_connection_scope"),
        CheckConstraint("status IN ('valid', 'invalid')", name="ck_model_connection_status"),
    )


class SqlVaultRequest(OmnigentBase):
    """
    SQLAlchemy model for the ``vault_requests`` table: a one-time secure-entry request.

    :param id: Request id (bare 32-char hex string), the only thing the chat ever carries.
    :param session_id: The session the Muse asked from.
    :param status: ``pending`` until the person saves it, then ``saved``.
    """

    __tablename__ = "vault_requests"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    site: Mapped[str] = mapped_column(String(512), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(8), nullable=False, server_default="pending")
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'saved')", name="ck_vault_requests_status"),
    )


class SqlVaultAudit(OmnigentBase):
    """
    SQLAlchemy model for the ``vault_audit`` table: who touched which secret, never a value.

    :param action: ``create``, ``fill``, ``delete`` or ``refuse`` (a denied fill).
    :param secret_id: The secret (kept after the secret is deleted).
    :param session_id: The session a fill ran in, or ``None``.
    """

    __tablename__ = "vault_audit"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    secret_id: Mapped[str] = mapped_column(Uuid16, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    at: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "action IN ('create', 'fill', 'delete', 'refuse')", name="ck_vault_audit_action"
        ),
        Index("ix_vault_audit_secret", "workspace_id", "secret_id", "at"),
    )


class SqlLookoutSignal(OmnigentBase):
    """
    SQLAlchemy model for the ``lookout_signals`` table: one thing that might be worth telling a
    person, left for their Lookout by a source without any model run (ADR 0038).

    :param id: Signal id (bare 32-char hex).
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param kind: The source's kind, e.g. ``library_file`` or ``promise``.
    :param topic_key: The topic the source already knows it belongs to, else ``None`` (the
        Lookout's code check matches it).
    :param subject: One line naming it, e.g. a file title or a promise's text.
    :param detail: JSON object the code check and the wake run read (excerpt, refs, sender).
    :param conversation_id: The person's Conversation when the source knows it.
    :param due_at: Epoch seconds from which it may be told.
    :param stale_after: Epoch seconds after which it is never told; it only marks its topic.
    :param urgency: ``now`` or ``normal`` (set by code from the kind).
    :param dedupe_key: Folds repeats: one waiting signal per key per owner.
    :param claimed_run_id: The wake that took it, while that wake runs.
    :param sent_at: Epoch seconds the message that carried it was stored.
    :param done_at: Epoch seconds it was settled any other way (``outcome`` says how).
    :param outcome: ``sent``, ``stale``, ``unmatched``, ``muted``, ``nothing``, ``removed``.
    """

    __tablename__ = "lookout_signals"
    __table_args__ = (
        Index("ix_lookout_signals_owner_key", "workspace_id", "owner", "dedupe_key"),
        Index("ix_lookout_signals_owner_due", "workspace_id", "owner", "done_at", "due_at"),
        Index("ix_lookout_signals_claim", "workspace_id", "claimed_run_id"),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    topic_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    due_at: Mapped[int] = mapped_column(Integer, nullable=False)
    stale_after: Mapped[int] = mapped_column(Integer, nullable=False)
    urgency: Mapped[str] = mapped_column(String(8), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    claimed_run_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    sent_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    done_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlLookoutOwner(OmnigentBase):
    """
    SQLAlchemy model for the ``lookout_owners`` table: each person's one next wake (ADR 0038).

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param conversation_id: The person's Conversation, as last seen from a source.
    :param next_wake_at: Epoch seconds of the next wake, ``None`` on an empty tray.
    :param lease_until: While a replica wakes this person, until when it holds them.
    :param updated_at: Epoch seconds of the last change.
    """

    __tablename__ = "lookout_owners"
    __table_args__ = (Index("ix_lookout_owners_next", "next_wake_at"),)

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    next_wake_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_until: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlLookoutWake(OmnigentBase):
    """
    SQLAlchemy model for the ``lookout_wakes`` table: one row per wake that reached a run (the
    Lookout's own, or the morning brief's), the ledger its limits count (ADR 0038).

    :param id: Wake id (bare 32-char hex); the claimed signals carry it.
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param kind: ``wake`` (a Lookout run) or ``brief`` (the morning brief's run).
    :param scheduled_task_id: The hidden task the run fired from.
    :param conversation_id: The person's Conversation the message goes to.
    :param run_conversation_id: The run's own session, once started.
    :param status: ``claimed``, ``running``, ``sent``, ``nothing`` or ``released``.
    :param urgency: The message's outbox urgency (``now``, ``today`` or ``brief``).
    :param state: What the person was doing when the wake decided to speak (``chatting`` or
        ``not``), recorded with the message's ``sent`` event for the rhythm.
    :param topic_keys: JSON list of the topics its signals belong to.
    :param item_id: The message it posted, once stored.
    :param started_at: Epoch seconds it was claimed.
    :param finished_at: Epoch seconds it ended.
    """

    __tablename__ = "lookout_wakes"
    __table_args__ = (
        Index("ix_lookout_wakes_owner_started", "workspace_id", "owner", "started_at"),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scheduled_task_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    run_conversation_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    urgency: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    topic_keys: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str | None] = mapped_column(Uuid16, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SqlLookoutTerm(OmnigentBase):
    """
    SQLAlchemy model for the ``lookout_terms`` table (topic watch's term counts, kept): per
    person, day and watched term, how many signals the Lookout checked that day while the term
    was watched, and how many contained it, so a common term is told from a specific one.

    :param owner: Owner identity (``"local"`` in single-user mode).
    :param day: UTC day number (epoch seconds // 86400).
    :param term: The watched term, normalized (case and spacing folded).
    :param items: Signals checked that day while the term was watched.
    :param hits: Of those, the signals containing the term.
    """

    __tablename__ = "lookout_terms"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    owner: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(128), primary_key=True)
    items: Mapped[int] = mapped_column(Integer, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlWorkJob(OmnigentBase):
    """
    SQLAlchemy model for the ``work_jobs`` table: the work queue every background model run waits
    in (ADR 0038). A row is claimed under a row lock, bound to the run it started, and finished
    when that run ends, so a restart neither loses a waiting job nor runs one twice.

    :param id: Job id (bare 32-char hex).
    :param kind: The handler that runs it, e.g. ``scheduled_fire``, ``memory_upkeep``.
    :param key: Idempotency key within the kind: a submit while a job with the same key is open
        returns that job.
    :param owner: Owner identity (``"local"`` in single-user mode).
    :param conversation_id: The Conversation whose running turn lowers the owner's limit, if any.
    :param payload: JSON object the handler reads.
    :param state: ``queued``, ``claimed`` (launching), ``running`` (bound to ``run_id``),
        ``done`` or ``failed``.
    :param owner_limit: Start only while fewer than this many of the owner's jobs are in flight
        (``None``: the owner's limit alone).
    :param not_before: Epoch seconds before which the job is not claimed.
    :param attempts: How many times it was claimed.
    :param lease_until: Epoch seconds a claim expires if its process died before binding a run.
    :param claimed_by: The claiming process.
    :param run_id: The run the job started (a session id); its end finishes the job.
    :param error: Why it failed, short.
    """

    __tablename__ = "work_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "kind", "key", name="uq_work_jobs_kind_key"),
        Index("ix_work_jobs_due", "state", "not_before"),
        Index("ix_work_jobs_owner", "workspace_id", "owner", "state"),
        Index("ix_work_jobs_run", "run_id"),
    )

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_limit: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    not_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
