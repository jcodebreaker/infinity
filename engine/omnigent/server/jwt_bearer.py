"""Bearer JWTs from an external identity provider, for third-party clients.

A site holding a user token from its own IdP calls the engine with
``Authorization: Bearer <jwt>``. The token is verified against the IdP's JWKS, issuer, audience
and expiry; the user comes from a configured claim and an optional tenant from another. Opt-in
(``OMNIGENT_JWT_JWKS_URL``) and additive: it runs only when the deployment's own identity source
found no user, so header-mode proxies and cookie logins keep working unchanged.

Only asymmetric algorithms are accepted, so a token can never be checked with a shared secret
(no ``alg`` confusion with the server's own HS256 session and runner tokens).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

logger = logging.getLogger(__name__)

JWKS_URL_ENV = "OMNIGENT_JWT_JWKS_URL"
ISSUER_ENV = "OMNIGENT_JWT_ISSUER"
AUDIENCE_ENV = "OMNIGENT_JWT_AUDIENCE"
USER_CLAIM_ENV = "OMNIGENT_JWT_USER_CLAIM"
TENANT_CLAIM_ENV = "OMNIGENT_JWT_TENANT_CLAIM"
ALGORITHMS_ENV = "OMNIGENT_JWT_ALGORITHMS"

_DEFAULT_USER_CLAIM = "email"
_FALLBACK_USER_CLAIM = "sub"
_ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
_DEFAULT_ALGORITHMS = ("RS256", "ES256")
_RESERVED_USERS = frozenset({"local", "__public__", "__workspace__"})
_LEEWAY_SECONDS = 30
_JWKS_TIMEOUT_SECONDS = 5
_JWKS_LIFESPAN_SECONDS = 300
_CACHE_MAX = 1024
_MAX_CLAIM_CHARS = 256


@dataclass(frozen=True)
class BearerIdentity:
    """Who a verified bearer token names.

    :param user_id: The user, from the configured claim, e.g. ``"alice@example.com"``.
    :param tenant: The tenant claim's value, or ``None`` when unconfigured or absent.
    """

    user_id: str
    tenant: str | None = None


@dataclass(frozen=True)
class JwtBearerConfig:
    """Settings for :class:`JwtBearerVerifier` (all non-secret).

    :param jwks_url: The IdP's JWKS URL.
    :param issuer: Required ``iss``.
    :param audience: Required ``aud`` (this engine's API identifier at the IdP).
    :param user_claim: Claim naming the user; ``sub`` is the fallback when it is absent.
    :param tenant_claim: Claim naming the tenant, or ``None`` for no tenant.
    :param algorithms: Accepted signing algorithms (asymmetric only).
    """

    jwks_url: str
    issuer: str
    audience: str
    user_claim: str = _DEFAULT_USER_CLAIM
    tenant_claim: str | None = None
    algorithms: tuple[str, ...] = _DEFAULT_ALGORITHMS

    @classmethod
    def from_env(cls) -> JwtBearerConfig | None:
        """Read the ``OMNIGENT_JWT_*`` settings; ``None`` when no JWKS URL is set.

        :raises RuntimeError: When the JWKS URL is set without an issuer and audience, is not
            HTTPS (loopback excepted), or names a symmetric or unknown algorithm.
        """
        jwks_url = os.environ.get(JWKS_URL_ENV, "").strip()
        if not jwks_url:
            return None
        issuer = os.environ.get(ISSUER_ENV, "").strip()
        audience = os.environ.get(AUDIENCE_ENV, "").strip()
        if not issuer or not audience:
            raise RuntimeError(f"{JWKS_URL_ENV} requires {ISSUER_ENV} and {AUDIENCE_ENV}")
        if not _is_https_or_loopback(jwks_url):
            raise RuntimeError(f"{JWKS_URL_ENV} must be an https:// URL")
        raw_algorithms = os.environ.get(ALGORITHMS_ENV, "").strip()
        algorithms = (
            tuple(a.strip() for a in raw_algorithms.split(",") if a.strip())
            if raw_algorithms
            else _DEFAULT_ALGORITHMS
        )
        bad = [a for a in algorithms if a not in _ASYMMETRIC_ALGORITHMS]
        if bad or not algorithms:
            got = ", ".join(bad) or "none"
            raise RuntimeError(f"{ALGORITHMS_ENV} accepts only asymmetric algorithms, got {got}")
        return cls(
            jwks_url=jwks_url,
            issuer=issuer,
            audience=audience,
            user_claim=os.environ.get(USER_CLAIM_ENV, "").strip() or _DEFAULT_USER_CLAIM,
            tenant_claim=os.environ.get(TENANT_CLAIM_ENV, "").strip() or None,
            algorithms=algorithms,
        )


def _is_https_or_loopback(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _claim_string(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= _MAX_CLAIM_CHARS else None


class JwtBearerVerifier:
    """Verify external bearer JWTs and map them to a :class:`BearerIdentity`.

    :param config: The verified settings.
    :param signing_key: Resolves a token to its verification key; defaults to a cached JWKS
        fetch from ``config.jwks_url``. Tests pass an offline resolver.
    """

    def __init__(
        self,
        config: JwtBearerConfig,
        signing_key: Callable[[str], Any] | None = None,
    ) -> None:
        self._config = config
        if signing_key is None:
            client = jwt.PyJWKClient(
                config.jwks_url,
                cache_keys=True,
                lifespan=_JWKS_LIFESPAN_SECONDS,
                timeout=_JWKS_TIMEOUT_SECONDS,
            )
            signing_key = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._signing_key = signing_key
        self._cache: OrderedDict[str, tuple[BearerIdentity, float]] = OrderedDict()

    def verify(self, token: str) -> BearerIdentity | None:
        """The identity *token* proves, or ``None`` when it fails any check.

        Valid results are cached by token digest until the token expires.
        """
        digest = hashlib.sha256(token.encode()).hexdigest()
        cached = self._cache.get(digest)
        if cached is not None:
            identity, expires_at = cached
            if expires_at > time.time():
                return identity
            del self._cache[digest]
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self._config.algorithms:
                return None
            payload = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except (jwt.PyJWTError, ValueError) as exc:
            logger.debug("Bearer JWT rejected: %s", type(exc).__name__)
            return None
        user_id = _claim_string(payload, self._config.user_claim)
        # An email the IdP says it has not verified is not an identity.
        if user_id is not None and self._config.user_claim == "email":
            if payload.get("email_verified") is False or payload.get("email_verified") == "false":
                return None
        user_id = user_id or _claim_string(payload, _FALLBACK_USER_CLAIM)
        if user_id is None or user_id in _RESERVED_USERS:
            return None
        tenant = (
            _claim_string(payload, self._config.tenant_claim)
            if self._config.tenant_claim
            else None
        )
        identity = BearerIdentity(user_id=user_id, tenant=tenant)
        self._cache[digest] = (identity, float(payload["exp"]))
        if len(self._cache) > _CACHE_MAX:
            self._cache.popitem(last=False)
        return identity


def create_jwt_bearer_verifier() -> JwtBearerVerifier | None:
    """Build the verifier from the environment, or ``None`` when it is not configured."""
    config = JwtBearerConfig.from_env()
    return JwtBearerVerifier(config) if config is not None else None
