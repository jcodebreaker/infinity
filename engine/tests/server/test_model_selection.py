"""Selection: the caller's catalog per harness, their three picks, and how a session picks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.labels import (
    ARTIFACT_CHAT_LABEL_KEY,
    CONTEXT_MODE_LABEL,
    SUBAGENT_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.inference_config import HarnessInferenceBinding
from omnigent.onboarding.provider_config import ProviderEntry
from omnigent.server.inference_catalog import SandboxInferenceService
from omnigent.server.routes.sandbox_inference import (
    prepare_create_inference,
    session_model,
    snapshot_serves_model,
)
from omnigent.superchat.models.selection import (
    ModelPreferenceStore,
    create_model_selection_router,
    foundation_resolver,
)

from .test_byok_pi import _hosted_state
from .test_inference_catalog import _state  # noqa: F401

PRICES = {"claude-sonnet-5-5": (3e-6, 15e-6), "claude-haiku-4-5": (1e-6, 5e-6)}


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from omnigent.superchat.models import tiers

    monkeypatch.setattr(tiers, "catalog_price", PRICES.get)
    uri = f"sqlite:///{tmp_path / 's.db'}"
    OmnigentBase.metadata.create_all(get_or_create_engine(uri))
    state = _hosted_state({})

    class _Store:
        def get_plaintext(self, scope, owner_id, preferred):
            return ("anthropic", "sk-ant-x") if scope == "user" else None

    state.model_connection_store = _Store()
    state.model_preference_store = ModelPreferenceStore(uri)
    state.foundation_model = foundation_resolver(state.model_preference_store)
    application = FastAPI()
    for name, value in vars(state).items():
        setattr(application.state, name, value)
    application.include_router(
        create_model_selection_router(application.state.model_preference_store), prefix="/v1"
    )

    @application.exception_handler(OmnigentError)
    async def _err(_: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"code": str(exc.code)}, status_code=exc.http_status)

    return application


def _models(client: TestClient, harness: str) -> dict:
    return client.get("/v1/me/models", params={"harness": harness}).json()


def test_models_are_filtered_per_harness_and_flagged(app: FastAPI) -> None:
    client = TestClient(app)
    sdk = _models(client, "claude-sdk")
    assert sdk["status"] == "ready"
    assert [m["id"] for m in sdk["models"]] == ["claude-sonnet-5-5", "claude-haiku-4-5"]
    sonnet = sdk["models"][0]
    assert sonnet["is_default"] is True
    assert sonnet["categories"] == ["foundation", "builder"]
    assert sonnet["family"] == "claude" and sonnet["wire_api"] == "anthropic-messages"
    # pi on an Anthropic key: the key's own list, nothing named in configuration.
    pi = {m["id"]: m for m in _models(client, "pi")["models"]}
    assert list(pi) == ["claude-sonnet-5-5", "claude-haiku-4-5"]


EMPTY = {"foundation": {}, "builder": {}, "light": {}, "defaults": {}}


def test_preferences_round_trip_and_flag(app: FastAPI) -> None:
    client = TestClient(app)
    assert client.get("/v1/me/model-preferences").json() == EMPTY
    put = client.put(
        "/v1/me/model-preferences", json={"foundation": {"claude-sdk": "claude-haiku-4-5"}}
    )
    haiku = {"claude-sdk": "claude-haiku-4-5"}
    assert put.json() == {**EMPTY, "foundation": haiku, "defaults": haiku}
    assert client.get("/v1/me/model-preferences").json() == put.json()
    sdk = _models(client, "claude-sdk")
    assert sdk["foundation_model"] == "claude-haiku-4-5"
    assert sdk["picks"] == {"foundation": "claude-haiku-4-5", "builder": None, "light": None}
    flagged = {m["id"]: m["categories"] for m in sdk["models"]}
    # Builder stays automatic: the connection's default.
    assert flagged == {
        "claude-sonnet-5-5": ["builder"],
        "claude-haiku-4-5": ["foundation", "light"],
    }
    client.put("/v1/me/model-preferences", json={"foundation": {}})
    assert client.get("/v1/me/model-preferences").json() == EMPTY


def test_defaults_is_accepted_as_foundation(app: FastAPI) -> None:
    client = TestClient(app)
    put = client.put(
        "/v1/me/model-preferences", json={"defaults": {"claude-sdk": "claude-haiku-4-5"}}
    )
    assert put.json()["foundation"] == {"claude-sdk": "claude-haiku-4-5"}


def test_each_category_is_kept_apart(app: FastAPI) -> None:
    client = TestClient(app)
    sdk = _models(client, "claude-sdk")
    assert sdk["picks"] == {"foundation": None, "builder": None, "light": None}
    assert sdk["automatic"] == {
        "foundation": "claude-sonnet-5-5",
        "builder": "claude-sonnet-5-5",
        "light": "claude-haiku-4-5",
    }
    client.put("/v1/me/model-preferences", json={"foundation": {"claude-sdk": "claude-haiku-4-5"}})
    client.put("/v1/me/model-preferences", json={"builder": {"claude-sdk": "claude-haiku-4-5"}})
    put = client.put(
        "/v1/me/model-preferences", json={"light": {"claude-sdk": "claude-sonnet-5-5"}}
    )
    assert put.json()["foundation"] == put.json()["builder"] == {"claude-sdk": "claude-haiku-4-5"}
    assert put.json()["light"] == {"claude-sdk": "claude-sonnet-5-5"}
    sdk = _models(client, "claude-sdk")
    assert (sdk["foundation_model"], sdk["builder_model"], sdk["light_model"]) == (
        "claude-haiku-4-5",
        "claude-haiku-4-5",
        "claude-sonnet-5-5",
    )
    # Back to automatic, one category at a time.
    client.put("/v1/me/model-preferences", json={"light": {}})
    assert _models(client, "claude-sdk")["picks"]["light"] is None
    assert client.get("/v1/me/model-preferences").json()["builder"] == {
        "claude-sdk": "claude-haiku-4-5"
    }
    bad = client.put("/v1/me/model-preferences", json={"builder": {"claude-sdk": "gpt-5-4"}})
    assert bad.status_code == 400 and bad.json() == {"code": "model_not_supported"}


def test_rows_written_earlier_read_as_foundation(app: FastAPI) -> None:
    import json

    from omnigent.db.db_models import SqlPreference

    store = app.state.model_preference_store
    store.set("local", foundation={"claude-sdk": "claude-haiku-4-5"})
    for legacy in (
        {"claude-sdk": "claude-haiku-4-5"},
        {"defaults": {"claude-sdk": "claude-haiku-4-5"}, "light": {}},
    ):
        with store._session_immediate("legacy") as session:
            row = session.query(SqlPreference).one()
            row.value = json.dumps(legacy)
        picks = store.read("local")
        assert picks.foundation == {"claude-sdk": "claude-haiku-4-5"}
        assert picks.builder == picks.light == {}


def test_preferences_reject_models_the_connection_does_not_serve(app: FastAPI) -> None:
    client = TestClient(app)
    bad = client.put("/v1/me/model-preferences", json={"defaults": {"claude-sdk": "gpt-5-4"}})
    assert bad.status_code == 400 and bad.json() == {"code": "model_not_supported"}
    nope = client.put("/v1/me/model-preferences", json={"defaults": {"nope": "x"}})
    assert nope.status_code == 400 and nope.json() == {"code": "invalid_input"}
    assert client.get("/v1/me/model-preferences").json() == EMPTY


SUPER_CHAT = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}


def _create(app: FastAPI, **kwargs):
    store = kwargs.pop("conversation_store", None)
    body = SimpleNamespace(
        labels=kwargs.pop("labels", SUPER_CHAT),
        host_type="managed",
        sandbox_provider=None,
        parent_session_id=kwargs.pop("parent", None),
        inference_configuration_revision=None,
        cost_control_mode_override=None,
    )
    spec = SimpleNamespace(
        executor=SimpleNamespace(type="claude-sdk", config={}, auth=None, profile=None)
    )
    request = SimpleNamespace(app=app)
    return prepare_create_inference(request, body, spec, None, store, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_picks_override_then_foundation(app: FastAPI) -> None:
    assert (await _create(app))[1] == "claude-sonnet-5-5"
    app.state.model_preference_store.set("local", foundation={"claude-sdk": "claude-haiku-4-5"})
    assert (await _create(app))[1] == "claude-haiku-4-5"
    assert (await _create(app, model_override="claude-sonnet-5-5"))[1] == "claude-sonnet-5-5"
    # A pick the catalog no longer serves is ignored, not an error.
    app.state.model_preference_store.set("local", foundation={"claude-sdk": "claude-gone"})
    assert (await _create(app))[1] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_only_a_super_chat_starts_on_the_persons_foundation(app: FastAPI) -> None:
    app.state.model_preference_store.set("local", foundation={"claude-sdk": "claude-haiku-4-5"})
    assert (await _create(app))[1] == "claude-haiku-4-5"
    # Any other session keeps the connection's default.
    assert (await _create(app, labels={}))[1] == "claude-sonnet-5-5"
    studio = {**SUPER_CHAT, ARTIFACT_CHAT_LABEL_KEY: "file-1"}
    assert (await _create(app, labels=studio))[1] == "claude-sonnet-5-5"
    helper = {**SUPER_CHAT, SUBAGENT_LABEL_KEY: "worker"}
    assert (await _create(app, labels=helper))[1] == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_sub_agent_pin_outside_the_catalog_inherits_the_parent(app: FastAPI) -> None:
    snapshot, _ = await _create(app)
    parent = SimpleNamespace(inference_snapshot=snapshot, model_override="claude-haiku-4-5")
    store = SimpleNamespace(get_conversation=lambda _id: parent)
    app.state.model_preference_store.set("local", foundation={"claude-sdk": "claude-sonnet-5-5"})
    kwargs = {"parent": "p1", "conversation_store": store}
    assert (await _create(app, model_override="gpt-5-4", **kwargs))[1] == "claude-haiku-4-5"
    assert (await _create(app, model_override="claude-sonnet-5-5", **kwargs))[1] == (
        "claude-sonnet-5-5"
    )
    # No pin: the parent's model, not the person's default.
    assert (await _create(app, **kwargs))[1] == "claude-sonnet-5-5"  # binding default of snapshot
    assert snapshot_serves_model(snapshot, "claude-haiku-4-5")
    assert not snapshot_serves_model(snapshot, "gpt-5-4")
    assert snapshot_serves_model(None, "anything")


@pytest.mark.asyncio
async def test_session_model_is_the_pin_else_the_owners_foundation(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot, _ = await _create(app)
    owners = SimpleNamespace(get_session_owner=lambda _id: "local")

    def chat(labels: dict = SUPER_CHAT, pin: str | None = None, snap: dict | None = snapshot):
        return SimpleNamespace(id="s1", labels=labels, model_override=pin, inference_snapshot=snap)

    async def model(conv: SimpleNamespace) -> str | None:
        return await session_model(app.state, conv, owners)  # type: ignore[arg-type]

    app.state.model_preference_store.set("local", foundation={"claude-sdk": "claude-haiku-4-5"})
    assert await model(chat()) == "claude-haiku-4-5"
    assert await model(chat(pin="claude-sonnet-5-5")) == "claude-sonnet-5-5"
    # A Helper keeps the connection's default (``None``): its category is the runner's to ask.
    assert await model(chat(labels={**SUPER_CHAT, SUBAGENT_LABEL_KEY: "worker"})) is None
    # No saved catalog: the catalog a new session would get, on the session's harness.
    from omnigent.server.routes._sessions import helpers

    monkeypatch.setattr(helpers, "_resolve_harness", lambda _conv: "claude-sdk")
    assert await model(chat(snap=None)) == "claude-haiku-4-5"


def test_a_model_connection_is_never_filtered_by_name() -> None:
    """A connection's ids are its upstream's names: claude-sdk is offered all of them; an
    operator gateway's curated list keeps the rule for ids recognizably of another vendor."""
    service = SandboxInferenceService(SimpleNamespace())
    allow = ("claude-sonnet-5-5", "gateway/fast", "gpt-5-4", "team-model")
    for connection, expected in (
        (True, list(allow)),
        (False, ["claude-sonnet-5-5", "gateway/fast", "team-model"]),
    ):
        result = service._static_catalog(
            {"provider_label": "x"},
            HarnessInferenceBinding("gw", None, allow),
            ProviderEntry(name="gw", kind="gateway"),
            "claude-sdk",
            connection=connection,
        )
        assert [row["id"] for row in result["models"]] == expected


def test_providers_are_the_engines_registry(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(app)
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    data = client.get("/v1/me/model-providers").json()["data"]
    assert data == [
        {"provider": "anthropic", "label": "Anthropic", "operator_key": False},
        {"provider": "openrouter", "label": "OpenRouter", "operator_key": False},
    ]
    # An operator-run upstream is offered once it is configured.
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.internal:4000")
    names = [p["provider"] for p in client.get("/v1/me/model-providers").json()["data"]]
    assert names == ["anthropic", "openrouter", "litellm"]


def test_helper_models_answer_the_owners_picks(app: FastAPI) -> None:
    client = TestClient(app)

    def ask(harness: str) -> dict:
        return client.get("/v1/sessions/s1/helper-models", params={"harness": harness}).json()

    sonnet, haiku = "claude-sonnet-5-5", "claude-haiku-4-5"
    # Nothing picked: Foundation the dearest, Builder too (two priced rows), Light the cheapest.
    assert ask("claude-sdk") == {
        "foundation": sonnet,
        "builder": sonnet,
        "light": haiku,
    }
    app.state.model_preference_store.set(
        "local", foundation={"claude-sdk": haiku}, builder={"claude-sdk": haiku}
    )
    picked = ask("claude-sdk")
    assert (picked["foundation"], picked["builder"], picked["light"]) == (haiku, haiku, haiku)
    assert set(ask("nope").values()) == {None}


class _SuperChats:
    """The one Super Chat the caller owns, and what was written to it."""

    def __init__(self, conv: SimpleNamespace) -> None:
        self.conv = conv
        self.updates: list[dict] = []

    def find_conversation_ids_by_label(self, _key: str, _value: str, limit: int) -> list[str]:
        return [self.conv.id]

    def get_conversations(self, ids: list[str]) -> dict:
        return {self.conv.id: self.conv}

    def get_session_owner(self, _session_id: str, owner_only: bool = False) -> str | None:
        return None

    def get_conversation(self, _session_id: str) -> SimpleNamespace:
        return self.conv

    def update_conversation(self, session_id: str, **fields) -> None:
        self.updates.append(fields)
        if fields.get("_unset_model_override"):
            self.conv.model_override = None
        elif fields.get("model_override"):
            self.conv.model_override = fields["model_override"]


def _follow_app(app: FastAPI, conv: SimpleNamespace) -> tuple[TestClient, _SuperChats]:
    from omnigent.superchat.muse import MUSE_LABEL_KEY

    conv.labels = {**SUPER_CHAT, MUSE_LABEL_KEY: "true"}
    store = _SuperChats(conv)
    app.router.routes.clear()
    app.include_router(
        create_model_selection_router(app.state.model_preference_store, conversation_store=store),
        prefix="/v1",
    )
    return TestClient(app), store


@pytest.mark.asyncio
async def test_a_foundation_change_re_resolves_the_super_chat(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    snapshot, model = await _create(app)
    conv = SimpleNamespace(id="sc1", inference_snapshot=snapshot, model_override=model)
    client, store = _follow_app(app, conv)
    with caplog.at_level("INFO", logger="omnigent.superchat.models.selection"):
        client.put(
            "/v1/me/model-preferences", json={"foundation": {"claude-sdk": "claude-haiku-4-5"}}
        )
    assert conv.model_override == "claude-haiku-4-5"
    assert any("follows the Foundation pick" in r.message for r in caplog.records)
    # A Light change leaves the chat alone, even when it has drifted from Foundation.
    conv.model_override = "claude-sonnet-5-5"
    client.put("/v1/me/model-preferences", json={"light": {"claude-sdk": "claude-sonnet-5-5"}})
    assert conv.model_override == "claude-sonnet-5-5"
    # Back to automatic Foundation: the connection's default.
    conv.model_override = "claude-haiku-4-5"
    client.put("/v1/me/model-preferences", json={"foundation": {}})
    assert conv.model_override == "claude-sonnet-5-5"
    writes = len(store.updates)
    # Saving the same Foundation again changes nothing.
    client.put("/v1/me/model-preferences", json={"foundation": {}})
    assert len(store.updates) == writes


def test_a_foundation_change_drops_the_pin_of_an_unconfigured_super_chat(app: FastAPI) -> None:
    conv = SimpleNamespace(id="sc1", inference_snapshot=None, model_override="pinned-model")
    client, _ = _follow_app(app, conv)
    client.put("/v1/me/model-preferences", json={"foundation": {"claude-sdk": "claude-haiku-4-5"}})
    assert conv.model_override is None
