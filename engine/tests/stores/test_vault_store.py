"""Vault store: encryption at rest, fail-closed key handling, audit trail."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.superchat.vault.store import (
    VAULT_KEY_ENV,
    VaultInputError,
    VaultStore,
    VaultUnavailableError,
    normalize_site,
)

PASSWORD = "hunter2-Zq9!"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VaultStore:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    uri = f"sqlite:///{tmp_path / 'v.db'}"
    from omnigent.db.db_models import OmnigentBase

    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    return VaultStore(uri)


def _save(store: VaultStore, user: str | None = "u1", name: str = "acme") -> None:
    store.save(
        user_id=user, name=name, site="https://Acme.test/login", username="ann", password=PASSWORD
    )


def test_password_is_encrypted_at_rest_and_metadata_has_no_value(store: VaultStore) -> None:
    _save(store)
    with get_or_create_engine(store.storage_location).connect() as conn:
        stored = conn.execute(text("select ciphertext from vault_secrets")).scalar_one()
    assert PASSWORD not in stored and stored.startswith("v1:")
    entry = store.entries(user_id="u1")[0]
    assert entry.site == "https://acme.test" and entry.username == "ann"
    assert PASSWORD not in repr(entry)


def test_reveal_round_trip_audits_and_marks_used(store: VaultStore) -> None:
    _save(store)
    value, site = store.reveal("acme", user_id="u1", field="password", session_id="a" * 32)
    assert (value, site) == (PASSWORD, "https://acme.test")
    entry = store.entries(user_id="u1")[0]
    assert entry.last_used_at is not None
    actions = [a for a, _, _ in store.audit(entry.id, user_id="u1")]
    assert actions == ["create", "fill"]
    assert store.delete(entry.id, user_id="u1")
    assert store.entries(user_id="u1") == []


def test_other_owner_cannot_see_reveal_or_delete(store: VaultStore) -> None:
    _save(store)
    entry = store.entries(user_id="u1")[0]
    assert store.entries(user_id="u2") == []
    assert not store.delete(entry.id, user_id="u2")
    with pytest.raises(KeyError):
        store.reveal("acme", user_id="u2", field="password", session_id=None)


def test_no_key_refuses_everything(store: VaultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _save(store)
    monkeypatch.delenv(VAULT_KEY_ENV)
    with pytest.raises(VaultUnavailableError):
        _save(store, name="other")
    with pytest.raises(VaultUnavailableError):
        store.reveal("acme", user_id="u1", field="password", session_id=None)
    with pytest.raises(VaultUnavailableError):
        store.create_request(
            user_id="u1", session_id="a" * 32, name="x", site="https://x.test", reason=""
        )


def test_wrong_key_cannot_decrypt(store: VaultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _save(store)
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(VaultUnavailableError) as err:
        store.reveal("acme", user_id="u1", field="password", session_id=None)
    assert PASSWORD not in str(err.value)


def test_malformed_key_refused(store: VaultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VAULT_KEY_ENV, "short")
    with pytest.raises(VaultUnavailableError):
        _save(store)


def test_site_must_be_https_origin() -> None:
    assert normalize_site("https://a.test:8443/x?y=1") == "https://a.test:8443"
    for bad in ("http://a.test", "https://u:p@a.test", "javascript:1", "a.test"):
        with pytest.raises(VaultInputError):
            normalize_site(bad)


def test_request_marked_saved(store: VaultStore) -> None:
    req = store.create_request(
        user_id="u1",
        session_id="a" * 32,
        name="acme",
        site="https://acme.test",
        reason="to sign in",
    )
    assert store.get_request(req.id, user_id="u2") is None
    store.save(
        user_id="u1", name="acme", site=req.site, username="", password=PASSWORD, request_id=req.id
    )
    got = store.get_request(req.id, user_id="u1")
    assert got is not None and got.status == "saved"
