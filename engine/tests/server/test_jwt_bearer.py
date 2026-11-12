"""External IdP bearer JWTs (``omnigent.server.jwt_bearer``) and their place in the auth provider.

Keys are generated per test and the JWKS is served offline, so nothing leaves the process.
"""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.cors import cors_allowed_origins
from omnigent.server.jwt_bearer import JwtBearerConfig, JwtBearerVerifier

ISSUER = "https://idp.example.test/"
AUDIENCE = "omnigent-api"


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _offline_jwks(private_key: rsa.RSAPrivateKey) -> Any:
    """A signing-key resolver over an in-memory JWKS holding *private_key*'s public half."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid="k1", use="sig", alg="RS256")
    jwks = jwt.PyJWKSet.from_dict({"keys": [jwk]})

    def resolve(token: str) -> Any:
        kid = jwt.get_unverified_header(token).get("kid")
        return next(k.key for k in jwks.keys if k.key_id == kid)

    return resolve


def _verifier(private_key: rsa.RSAPrivateKey, **config: Any) -> JwtBearerVerifier:
    settings = {"jwks_url": "https://idp.example.test/jwks", "issuer": ISSUER}
    settings["audience"] = AUDIENCE
    return JwtBearerVerifier(
        JwtBearerConfig(**settings, **config), signing_key=_offline_jwks(private_key)
    )


def _token(private_key: Any, *, alg: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "email": "alice@example.com",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_key, algorithm=alg, headers={"kid": "k1"})


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw, "path": "/v1/me/muse", "query_string": b""})


# ── the verifier ─────────────────────────────────────────────────────────


def test_a_valid_token_names_the_user_from_the_email_claim(key: rsa.RSAPrivateKey) -> None:
    identity = _verifier(key).verify(_token(key))
    assert identity is not None
    assert identity.user_id == "alice@example.com"
    assert identity.tenant is None


def test_the_user_falls_back_to_sub_without_an_email(key: rsa.RSAPrivateKey) -> None:
    identity = _verifier(key).verify(_token(key, email=None))
    assert identity is not None and identity.user_id == "user-123"


def test_the_tenant_claim_is_read_when_configured(key: rsa.RSAPrivateKey) -> None:
    identity = _verifier(key, tenant_claim="org_id").verify(_token(key, org_id="acme"))
    assert identity is not None and identity.tenant == "acme"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
        pytest.param({"aud": "someone-else"}, id="wrong-audience"),
        pytest.param({"iss": "https://evil.example.test/"}, id="wrong-issuer"),
        pytest.param({"exp": None}, id="no-expiry"),
        pytest.param({"email_verified": False}, id="unverified-email"),
        pytest.param({"email": "local", "sub": None}, id="reserved-user"),
    ],
)
def test_a_bad_token_is_rejected(key: rsa.RSAPrivateKey, overrides: dict[str, Any]) -> None:
    assert _verifier(key).verify(_token(key, **overrides)) is None


def test_a_token_signed_by_another_key_is_rejected(
    key: rsa.RSAPrivateKey, other_key: rsa.RSAPrivateKey
) -> None:
    assert _verifier(key).verify(_token(other_key)) is None


def test_a_symmetric_token_is_rejected(key: rsa.RSAPrivateKey) -> None:
    """No ``alg`` confusion: an HS256 token never verifies, whatever secret signed it."""
    assert (
        _verifier(key).verify(_token("a-shared-secret-of-enough-length-1234", alg="HS256")) is None
    )


def test_config_refuses_symmetric_algorithms_and_plain_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_JWT_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.setenv("OMNIGENT_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("OMNIGENT_JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("OMNIGENT_JWT_TENANT_CLAIM", "org_id")
    config = JwtBearerConfig.from_env()
    assert config is not None and config.tenant_claim == "org_id"
    assert config.user_claim == "email"
    monkeypatch.setenv("OMNIGENT_JWT_ALGORITHMS", "HS256")
    with pytest.raises(RuntimeError, match="asymmetric"):
        JwtBearerConfig.from_env()
    monkeypatch.delenv("OMNIGENT_JWT_ALGORITHMS")
    monkeypatch.setenv("OMNIGENT_JWT_JWKS_URL", "http://idp.example.test/jwks")
    with pytest.raises(RuntimeError, match="https"):
        JwtBearerConfig.from_env()
    monkeypatch.setenv("OMNIGENT_JWT_JWKS_URL", "https://idp.example.test/jwks")
    monkeypatch.delenv("OMNIGENT_JWT_AUDIENCE")
    with pytest.raises(RuntimeError, match="AUDIENCE"):
        JwtBearerConfig.from_env()


def test_config_is_off_without_a_jwks_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_JWT_JWKS_URL", raising=False)
    assert JwtBearerConfig.from_env() is None


# ── in the auth provider ─────────────────────────────────────────────────


def _header_provider(key: rsa.RSAPrivateKey, **kwargs: Any) -> UnifiedAuthProvider:
    return UnifiedAuthProvider(
        source="header",
        local_single_user=False,
        header_name="X-Forwarded-Email",
        header_strip_prefix="",
        header_secret="proxy-secret",
        jwt_bearer=_verifier(key, tenant_claim="org_id"),
        tenant_header="X-Omnigent-Tenant",
        **kwargs,
    )


def test_header_mode_is_unchanged_next_to_bearer_jwts(key: rsa.RSAPrivateKey) -> None:
    provider = _header_provider(key)
    proxied = _request(
        {
            "X-Forwarded-Email": "superchat-user@example.com",
            "X-Omnigent-Proxy-Secret": "proxy-secret",
            "X-Omnigent-Tenant": "space-1",
        }
    )
    assert provider.get_user_id(proxied) == "superchat-user@example.com"
    assert provider.get_tenant(proxied) == "space-1"
    # Without the proxy secret the identity header (and its tenant) is ignored.
    forged = _request(
        {"X-Forwarded-Email": "superchat-user@example.com", "X-Omnigent-Tenant": "x"}
    )
    assert provider.get_user_id(forged) is None
    assert provider.get_tenant(forged) is None


def test_a_bearer_jwt_authenticates_in_header_mode_with_its_tenant(
    key: rsa.RSAPrivateKey,
) -> None:
    provider = _header_provider(key)
    request = _request({"Authorization": f"Bearer {_token(key, org_id='acme')}"})
    assert provider.get_user_id(request) == "alice@example.com"
    assert provider.get_tenant(request) == "acme"


def test_a_tenant_header_never_rides_on_a_bearer_jwt(key: rsa.RSAPrivateKey) -> None:
    """The tenant header is trusted only from the proxy, never next to a client's own token."""
    provider = _header_provider(key)
    request = _request(
        {"Authorization": f"Bearer {_token(key)}", "X-Omnigent-Tenant": "someone-elses"}
    )
    assert provider.get_user_id(request) == "alice@example.com"
    assert provider.get_tenant(request) is None


def test_a_bad_bearer_jwt_is_unauthenticated(key: rsa.RSAPrivateKey) -> None:
    provider = _header_provider(key)
    expired = _token(key, exp=int(time.time()) - 3600)
    assert provider.get_user_id(_request({"Authorization": f"Bearer {expired}"})) is None


def test_bearer_jwts_are_refused_in_accounts_mode(key: rsa.RSAPrivateKey) -> None:
    with pytest.raises(RuntimeError, match="accounts"):
        UnifiedAuthProvider(source="accounts", jwt_bearer=_verifier(key))


# ── CORS allow-list ──────────────────────────────────────────────────────


def test_cors_is_off_by_default_and_takes_exact_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_CORS_ALLOWED_ORIGINS", raising=False)
    assert cors_allowed_origins() == []
    monkeypatch.setenv(
        "OMNIGENT_CORS_ALLOWED_ORIGINS", "https://app.example.com, http://localhost:3000/"
    )
    assert cors_allowed_origins() == ["https://app.example.com", "http://localhost:3000"]


@pytest.mark.parametrize(
    "entry", ["*", "https://*.example.com", "https://app.example.com/path", "app.example.com"]
)
def test_cors_refuses_wildcards_and_non_origins(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    monkeypatch.setenv("OMNIGENT_CORS_ALLOWED_ORIGINS", entry)
    with pytest.raises(RuntimeError):
        cors_allowed_origins()
