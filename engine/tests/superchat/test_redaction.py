"""Redaction of the engine's own secrets (omnigent.server.redaction) on every read route."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from omnigent.entities import (
    CompactionData,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.server import redaction
from omnigent.server.redaction import (
    REDACTED,
    Redactor,
    env_secret_values,
    is_secret_env_name,
    redactor_for,
)
from omnigent.server.routes.sessions.routes_items import register_items_routes
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.superchat.vault.store import VAULT_KEY_ENV, VaultStore

_MODE = {"omnigent.context.mode": "superside-chat"}
SECRET = "sk-live-9f8e7d6c5b4a3210"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    redaction.invalidate_vault()
    monkeypatch.setenv("ACME_API_KEY", SECRET)


# ── the redactor ─────────────────────────────────────────────────────────


def test_redactor_replaces_values_keys_and_json_escaped_forms() -> None:
    quoted = 'pa"ss\\word-123'
    redactor = Redactor([SECRET, quoted, "short"])
    value = {
        "text": f"use {SECRET} now",
        SECRET: 1,
        "args": json.dumps({"password": quoted}),
        "list": [quoted, 3, None, True],
        "short": "short stays",
    }
    out = redactor.deep(value)
    flat = json.dumps(out)
    assert SECRET not in flat and 'pa\\\\\\"ss' not in flat
    assert out["text"] == f"use {REDACTED} now"
    assert out["list"] == [REDACTED, 3, None, True]
    assert json.loads(out["args"]) == {"password": REDACTED}
    assert out["short"] == "short stays"
    assert value["text"] == f"use {SECRET} now"  # input untouched


def test_longest_value_wins_when_one_contains_another() -> None:
    redactor = Redactor(["abcdefgh", "abcdefgh-ijkl"])
    assert redactor.text("x abcdefgh-ijkl y") == f"x {REDACTED} y"


def test_secret_env_names_and_extra_names() -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "OMNIGENT_VAULT_KEY",
        "GIT_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DB_PASSWORD",
        "CLIENT_SECRET_VALUE",
        "AWS_CREDENTIALS",
    ):
        assert is_secret_env_name(name), name
    for name in ("ANTHROPIC_BASE_URL", "HOME", "SUPERCHAT_CLAUDE_MODEL", "MAX_TOKENS"):
        assert not is_secret_env_name(name), name
    env = {
        "A_API_KEY": "aaaaaaaaaa",
        "B_TOKEN": "tiny",
        "LITELLM_MASTER": "mmmmmmmmmm",
        "PLAIN": "pppppppppp",
        redaction.EXTRA_NAMES_ENV: "LITELLM_MASTER",
    }
    assert env_secret_values(env) == {"aaaaaaaaaa", "mmmmmmmmmm"}


def test_no_secrets_means_values_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redaction, "env_secret_values", lambda: frozenset())
    redactor = redactor_for(None)
    assert not redactor.active
    value = {"a": ["b"]}
    assert redactor.deep(value) is value


# ── vault values ─────────────────────────────────────────────────────────


@pytest.fixture()
def vault(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> VaultStore:
    monkeypatch.setenv(VAULT_KEY_ENV, base64.b64encode(os.urandom(32)).decode())
    return VaultStore(db_uri)


def _save(vault: VaultStore, user: str | None, password: str, name: str = "acme") -> None:
    vault.save(
        user_id=user, name=name, site="https://acme.test", username="ann", password=password
    )


def test_vault_passwords_are_redacted_for_their_owner_and_refresh_on_change(
    vault: VaultStore,
) -> None:
    _save(vault, "u1", "hunter2-Zq9!-long")
    _save(vault, "u2", "other-users-pass")
    redactor = redactor_for(vault, ["u1"])
    assert redactor.text("pw hunter2-Zq9!-long") == f"pw {REDACTED}"
    assert redactor.text("other-users-pass") == "other-users-pass"
    _save(vault, "u1", "rotated-password-1")  # same name replaces it, and the cache drops
    redactor = redactor_for(vault, ["u1"])
    assert redactor.text("rotated-password-1") == REDACTED
    assert redactor.text("hunter2-Zq9!-long") == "hunter2-Zq9!-long"
    entry = vault.entries(user_id="u1")[0]
    vault.delete(entry.id, user_id="u1")
    assert redactor_for(vault, ["u1"]).text("rotated-password-1") == "rotated-password-1"


def test_vault_off_adds_nothing(vault: VaultStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _save(vault, None, "local-password-1")
    assert redactor_for(vault, [None]).text("local-password-1") == REDACTED
    redaction.invalidate_vault()
    monkeypatch.delenv(VAULT_KEY_ENV)
    assert vault.secret_values(user_id=None) == []


# ── every items-route serializer ─────────────────────────────────────────


def _new(kind: str, data: Any, response_id: str = "r1") -> NewConversationItem:
    return NewConversationItem(type=kind, response_id=response_id, data=data)


def _say(role: str, text: str) -> NewConversationItem:
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    return _new(
        "message",
        MessageData(role=role, content=content, agent="brain" if role == "assistant" else None),
    )


@pytest.fixture()
async def client(
    conversation_store: SqlAlchemyConversationStore, vault: VaultStore
) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.vault_store = vault
    router = APIRouter()
    register_items_routes(
        router,
        conversation_store=conversation_store,
        agent_store=None,  # type: ignore[arg-type]
    )
    app.include_router(router, prefix="/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        yield http


async def test_no_items_route_leaks_a_secret(
    client: httpx.AsyncClient, conversation_store: SqlAlchemyConversationStore, vault: VaultStore
) -> None:
    vault_pw = "vault-pw-7777-long"
    _save(vault, None, vault_pw)
    leak = f"{SECRET} and {vault_pw}"
    root = conversation_store.create_conversation(kind="default", title="S", labels=_MODE)
    conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id, title=f"job:{SECRET}", labels=_MODE
    )
    conversation_store.append(
        root.id,
        [
            _say("user", f"check {leak}"),
            _new(
                "function_call",
                FunctionCallData(
                    agent="brain",
                    name="sys_os_shell",
                    arguments=json.dumps({"cmd": f"curl -H {leak}"}),
                    call_id="k1",
                ),
            ),
            _new("function_call_output", FunctionCallOutputData(call_id="k1", output=leak)),
            _say("assistant", f"done with {leak}"),
            _new(
                "compaction",
                CompactionData(summary=f"summary {leak}", last_item_id="x", token_count=1),
            ),
        ],
    )

    activities = await client.get(f"/v1/sessions/{root.id}/activities")
    assert activities.status_code == 200 and activities.json()["data"]
    activity_id = activities.json()["data"][0]["id"]
    paths = [
        f"/v1/sessions/{root.id}/items",
        f"/v1/sessions/{root.id}/items/search?query=check",
        f"/v1/sessions/{root.id}/items/search?query=done&scope=family",
        f"/v1/sessions/{root.id}/context_summary",
        f"/v1/sessions/{root.id}/child_sessions",
        f"/v1/sessions/{root.id}/activities",
        f"/v1/sessions/{root.id}/activities/{activity_id}",
    ]
    for path in paths:
        resp = await client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert SECRET not in resp.text and vault_pw not in resp.text, path
        assert REDACTED in resp.text, path
