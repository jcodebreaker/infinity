"""
Tests for the generic-provider routing branch of the per-harness spawn-env
builders in ``omnigent/runtime/workflow.py``.

Chunk 1b wires the kind-typed provider config
(``omnigent/onboarding/provider_config.py``) into the four
``_build_*_spawn_env`` builders so that a configured ``providers:`` entry —
either named explicitly via ``executor.auth: {type: provider, name: X}`` or
selected as the per-family global default — emits the per-harness
vendor-neutral gateway env vars (``HARNESS_*_GATEWAY_BASE_URL`` / ``_HOST`` /
``_AUTH_COMMAND`` / ``HARNESS_*_MODEL`` / the ``HARNESS_*_GATEWAY=true``
enable flag) the executors also consume from the Databricks producer.

Each test asserts the EXACT emitted values, so deleting the provider branch
(or mis-emitting a var) turns the test red. The "backwards-compat" tests
assert that with NO provider configured, the existing api_key / profile
paths are untouched and no provider vars leak in. These are unit tests — no
subprocess spawn, no real CLI.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml as _yaml

from omnigent.errors import OmnigentError
from omnigent.onboarding import ambient, harness_install
from omnigent.runtime.workflow import (
    _build_claude_sdk_spawn_env,
    _build_pi_spawn_env,
    _resolve_catalog_default_model,
    _resolve_provider_for_build,
)
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    LLMConfig,
    ProviderAuth,
)

_CATALOG_DEFAULTS = {
    ("anthropic", "claude"): "catalog-anthropic-default",
    ("openai", "openai"): "catalog-openai-default",
}


@pytest.fixture(autouse=True)
def _clear_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Clear ambient vendor keys and gateway routing so they cannot leak into
    the spawn env.

    The coding-agent process may have ``ANTHROPIC_API_KEY`` /
    ``OPENAI_API_KEY`` / ``DATABRICKS_TOKEN`` set; a gateway-driven shell also
    exports ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_MODEL`` /
    ``ANTHROPIC_CUSTOM_HEADERS``. Detection folds those routing vars into the
    detected anthropic entry, which would redirect the "routes to
    api.anthropic.com" assertions at the developer's own gateway (issue #4279).
    Clearing them keeps the tests deterministic (the provider path resolves
    from the config file, not the ambient environment).

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DATABRICKS_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_catalog_default_model",
        lambda provider_name, family, *, context: _CATALOG_DEFAULTS[(provider_name, family)],
    )
    monkeypatch.setattr(
        "omnigent.models.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=_CATALOG_DEFAULTS[(provider_name, family)]
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_ambient_provider_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate host state ambient provider detection reads (issue #4279).

    Two ambient sources leak past ``$OMNIGENT_CONFIG_HOME``:

    - ``~/.codex/config.toml`` and ``~/.databrickscfg`` live under ``$HOME``
      (``$USERPROFILE`` on Windows), so redirect it to an empty temp dir.
    - On macOS ``_claude_login_detected()`` falls back to ``claude auth status``,
      which reads the **Keychain** — no ``$HOME`` override can hide it. A
      signed-in Mac would inject a ``subscription`` provider that outranks the
      test's own configured entry, so stub it to "not logged in". Tests that
      exercise a detected login can still override this in call order.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr("omnigent.onboarding.ambient._claude_login_detected", lambda: False)


@pytest.fixture
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Point ``$OMNIGENT_CONFIG_HOME`` at an isolated temp dir.

    Both the readout (provider_config) and the spawn-env builders read the
    global config through this env var, so writing a ``config.yaml`` under
    *tmp_path* exercises the real file-loading path the runtime uses.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: The temp directory used as the config home.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_config(config_home: Path, config: dict[str, object]) -> None:
    """
    Write *config* as ``config.yaml`` under *config_home*.

    :param config_home: The ``$OMNIGENT_CONFIG_HOME`` directory.
    :param config: The config mapping to serialize, e.g.
        ``{"providers": {"openrouter": {...}}}``.
    """
    (config_home / "config.yaml").write_text(_yaml.safe_dump(config))


def _make_spec(
    *,
    harness: str,
    model: str | None = None,
    use_responses: object | None = None,
    auth: ApiKeyAuth | ProviderAuth | None = None,
    os_env: object | None = None,
) -> AgentSpec:
    """
    Build a minimal :class:`AgentSpec` for a given harness.

    :param harness: Harness name placed in ``executor.config["harness"]``,
        e.g. ``"claude-sdk"`` / ``"codex"`` / ``"pi"``.
    :param model: Spec-level model, e.g. ``"my-model"``. ``None`` omits it
        so the provider family's ``models.default`` supplies the model.
    :param use_responses: ``executor.config["use_responses"]``. ``None`` omits
        it; strings model values produced by standard bundle parsing.
    :param auth: Typed auth on ``spec.executor.auth``. ``None`` omits it, so
        the no-auth global-default provider path applies.
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": harness}
    if model is not None:
        config["model"] = model
    if use_responses is not None:
        config["use_responses"] = use_responses
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
        os_env=os_env,  # type: ignore[arg-type]
    )


def _key_family(
    base_url: str,
    api_key: str,
    default_model: str,
    *,
    wire_api: str | None = None,
) -> dict[str, object]:
    """
    Build a single provider-family config block (inline static key).

    :param base_url: Family endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key: Inline static key value, e.g. ``"sk-test-123"``.
    :param default_model: The family's ``models.default``, e.g. ``"gpt-4o"``.
    :returns: A family mapping ready to nest under a provider entry.
    """
    family: dict[str, object] = {
        "base_url": base_url,
        "api_key": api_key,
        "models": {"default": default_model},
    }
    if wire_api is not None:
        family["wire_api"] = wire_api
    return family


def _anthropic_default_config() -> dict[str, object]:
    """
    Return a config with a single ``default: true`` anthropic ``key`` provider.

    :returns: A config mapping for an anthropic-family default provider.
    """
    return {
        "providers": {
            "vendor-anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1",
                    "sk-ant-secret",
                    "claude-default-model",
                ),
            }
        }
    }


def _openai_default_config() -> dict[str, object]:
    """
    Return a config with a single ``default: true`` openai ``key`` provider.

    :returns: A config mapping for an openai-family default provider.
    """
    return {
        "providers": {
            "vendor-openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family(
                    "https://openai.example.com/v1",
                    "sk-oai-secret",
                    "gpt-default-model",
                ),
            }
        }
    }


# ── Global-default selection, per harness ──────────────────────────────────


def test_claude_sdk_uses_anthropic_global_default(config_home: Path) -> None:
    """
    A ``default: true`` anthropic provider routes the claude-sdk harness.

    Asserts the exact gateway env vars: base_url, host (origin of base_url),
    the printf auth command carrying the resolved key, the family default
    model, and the ``DATABRICKS=true`` enable flag. Failure means the
    no-auth global-default branch is not selecting the provider, or the
    gateway vars are mis-emitted (the harness would then hit
    api.anthropic.com with no key).
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk")  # no auth, no model → use default

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    # Host is the origin (scheme://netloc) of the base URL, not the full URL.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_HOST"] == "https://anthropic.example.com"
    # The static key becomes a printf command carrying the resolved secret.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    # No spec model → the family's models.default supplies the model.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-default-model"


def test_detected_ambient_key_routes_with_no_config(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh machine with only an ambient key routes via the detected provider.

    No ``config.yaml`` is written — the only credential is an ambient
    ``ANTHROPIC_API_KEY``. The spawn-env builder must merge that detection
    (``effective_config_with_detected``) and route the claude-sdk harness
    through it, so "first run without configure" works. Failure means a
    fresh machine would emit no gateway vars and the harness would hit
    api.anthropic.com with no key. HOME is isolated so a real CLI login on
    the test box can't shadow the env-key detection.
    """
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-detected")
    spec = _make_spec(harness="claude-sdk")  # no auth, no config file

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The detected anthropic key became the routed provider.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    # The ambient key is carried as the printf auth command (resolved, not leaked as a ref).
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-detected"
    # No pinned model on the detected entry → the catalog default fills in
    # (non-empty), rather than leaving the model unset.
    assert env["HARNESS_CLAUDE_SDK_MODEL"]


def test_claude_sdk_falls_back_to_first_available_anthropic_credential(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A configured-but-not-default anthropic credential routes the BRAIN head at spawn.

    The brain-head counterpart to the codex fallback — Debby's Claude head /
    Polly's claude-sdk brain, the most-used surface. With an anthropic
    credential configured but never marked default, the spawn-env builder falls
    back to it via the same `first_available_provider`, so the brain launches
    instead of hitting api.anthropic.com with no key. Resolved per spawn; the
    config is not mutated; the readout resolver (`for_launch=False`) still
    returns `None`. HOME is isolated so a real CLI login can't shadow the test.
    """
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = {
        "providers": {
            "vendor-anthropic": {  # configured, but NOT marked default
                "kind": "key",
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1",
                    "sk-ant-secret",
                    "claude-default-model",
                ),
            }
        }
    }
    _write_config(config_home, config)
    before = (config_home / "config.yaml").read_text()
    spec = _make_spec(harness="claude-sdk")  # unpinned, no auth — like Debby's Claude head

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    assert (config_home / "config.yaml").read_text() == before
    assert _resolve_provider_for_build(spec, harness_type="claude-sdk") is None


def test_pi_uses_anthropic_global_default(config_home: Path) -> None:
    """
    A ``default: true`` anthropic provider routes the pi harness.

    pi consumes both families: it emits a JSON ``BASE_URLS`` object keyed by
    pi's own family names. With only the anthropic family configured, the
    JSON carries just the ``claude`` key. Failure means pi's both-families
    handling regressed or the JSON keying is wrong.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="pi")

    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_GATEWAY"] == "true"
    # pi keys the base-URL JSON by its own family names ("claude" / "openai").
    assert env["HARNESS_PI_GATEWAY_BASE_URLS"] == (
        '{"claude": "https://anthropic.example.com/v1"}'
    )
    assert env["HARNESS_PI_GATEWAY_HOST"] == "https://anthropic.example.com"
    assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    assert env["HARNESS_PI_MODEL"] == "claude-default-model"


def test_pi_gateway_routing_log_reports_the_resolved_base_url(
    config_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The runner's routing log reports pi's plural ``_BASE_URLS`` key.

    pi's generic-provider path emits only ``HARNESS_PI_GATEWAY_BASE_URLS``
    (per-family JSON), so a log that reads just the singular
    ``HARNESS_PI_GATEWAY_BASE_URL`` says ``base_url=None`` on a run that
    routed fine. Failure means that fallback is gone and the line is
    misleading again.
    """
    from omnigent.runner.app import _build_spawn_env_from_spec

    _write_config(config_home, _anthropic_default_config())

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        env = _build_spawn_env_from_spec(_make_spec(harness="pi"), "pi")

    assert env is not None
    assert "HARNESS_PI_GATEWAY_BASE_URL" not in env  # only the plural key is set
    line = next(
        rec.getMessage() for rec in caplog.records if "gateway routing" in rec.getMessage()
    )
    # Compare against the env value itself: the line must carry the plural
    # key's URLs, not just some host substring.
    assert f"base_url={env['HARNESS_PI_GATEWAY_BASE_URLS']}" in line


def test_pi_threads_generic_openai_wire_api(config_home: Path) -> None:
    """Pi routes a generic OpenAI provider using configured wire metadata."""
    config = _openai_default_config()
    provider = config["providers"]["vendor-openai"]
    provider["openai"] = _key_family(
        "https://openai.example.com/v1",
        "sk-oai-secret",
        "gpt-default-model",
        wire_api="responses",
    )
    _write_config(config_home, config)

    env = _build_pi_spawn_env(_make_spec(harness="pi"), workdir=None)

    assert env["HARNESS_PI_GATEWAY_OPENAI_WIRE_API"] == "responses"


# ── Named ProviderAuth selection ───────────────────────────────────────────


def test_named_provider_auth_selects_provider_over_global_default(config_home: Path) -> None:
    """
    ``executor.auth: {type: provider, name: X}`` selects X over the default.

    The config has TWO anthropic ``key`` providers: a ``default: true`` one
    and a non-default ``named`` one. A spec naming the non-default provider
    must route through it, proving the named branch beats the global-default
    branch. Failure means the named ProviderAuth lookup is ignored and the
    global default wins (wrong endpoint + key).
    """
    config: dict[str, object] = {
        "providers": {
            "vendor-default": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://default.example.com/v1", "sk-default", "default-model"
                ),
            },
            "vendor-named": {
                "kind": "key",
                "anthropic": _key_family(
                    "https://named.example.com/v1", "sk-named", "named-model"
                ),
            },
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk", auth=ProviderAuth(name="vendor-named"))

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The NAMED provider, not the default, supplies the endpoint + key.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://named.example.com/v1"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-named"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "named-model"


def test_named_provider_auth_missing_provider_fails_loud(config_home: Path) -> None:
    """
    A ProviderAuth naming an undeclared provider raises a clear error.

    Failure (no raise) means a typo'd / unconfigured provider name would
    silently fall through to ambient credentials instead of failing loud.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk", auth=ProviderAuth(name="does-not-exist"))

    with pytest.raises(Exception, match="does-not-exist"):
        _build_claude_sdk_spawn_env(spec, workdir=None)


# ── Per-family selection through the spawn path ─────────────────────────────


def test_per_family_defaults_route_independently(config_home: Path) -> None:
    """
    An anthropic default and an openai default coexist and route per-family.

    With BOTH a ``default: true`` anthropic provider and a ``default: true``
    openai provider configured, claude-sdk must get the anthropic base_url
    proving per-family selection flows all the way through the spawn path.
    Failure means the harness→family resolution is wrong (e.g. claude-sdk
    picking the openai default).
    """
    config: dict[str, object] = {
        "providers": {
            "vendor-anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1", "sk-ant", "claude-model"
                ),
            },
            "vendor-openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family("https://openai.example.com/v1", "sk-oai", "gpt-model"),
            },
        }
    }
    _write_config(config_home, config)

    claude_env = _build_claude_sdk_spawn_env(_make_spec(harness="claude-sdk"), workdir=None)

    # claude-sdk resolves the anthropic family's default, not the openai one.
    assert claude_env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    assert claude_env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-model"


# ── Spec model overrides the family default ────────────────────────────────


def test_spec_model_beats_family_default(config_home: Path) -> None:
    """
    A spec-level model wins over the provider family's ``models.default``.

    Failure means the provider branch clobbers an explicit ``executor.model``
    with the family default — the spec must always win for the model.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk", model="spec-chosen-model")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The provider branch fired (gateway base_url is set) — so this is not a
    # vacuous pass where the model is simply the spec's because no provider
    # ran. The base_url proves the provider was selected despite a spec model.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    # Spec model wins; the family default ("claude-default-model") is ignored.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "spec-chosen-model"


# ── Catalog default model fallback (no spec model, no provider default) ─────


def _key_family_no_model(base_url: str, api_key: str) -> dict[str, object]:
    """
    Build a provider-family block with NO ``models.default``.

    Mirrors the reported bug: a ``key`` provider with only ``base_url`` +
    credential and no ``models`` block.

    :param base_url: Family endpoint base URL, e.g.
        ``"https://api.anthropic.com"``.
    :param api_key: Inline static key value, e.g. ``"sk-ant-secret"``.
    :returns: A family mapping with no ``models`` key.
    """
    return {"base_url": base_url, "api_key": api_key}


@pytest.mark.parametrize(
    ("provider_name", "catalog_family"),
    [("anthropic", "claude"), ("openai", "openai")],
)
def test_catalog_default_fails_clearly_when_discovery_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    catalog_family: str,
) -> None:
    """Known-family runtime defaults fail clearly without catalog data."""

    monkeypatch.setattr("omnigent.onboarding.providers.get_chat_models", lambda _provider: [])

    with pytest.raises(
        OmnigentError,
        match=r"Set 'executor.model'.*provider 'models.default'.*retry",
    ):
        _resolve_catalog_default_model(
            provider_name,
            catalog_family,
            context=f"provider family {provider_name!r}",
        )


def test_claude_sdk_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An anthropic ``key`` provider with no ``models.default`` resolves a
    catalog default model instead of failing loud.

    This is the reported bug: the spec sets no model and the provider's
    anthropic family declares no ``models.default``. Rather than raising,
    the builder must emit ``HARNESS_CLAUDE_SDK_MODEL`` equal to the bundled
    catalog's default anthropic model — proving the gateway path still gets
    a real model. The base_url assertion proves the provider branch fired
    (not a vacuous pass).
    """
    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk")  # no auth, no model, no provider default

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    catalog_default = _CATALOG_DEFAULTS[("anthropic", "claude")]
    # The provider branch fired (gateway base_url set) AND the model came
    # from the catalog, not from a provider/spec default and not a failure.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == catalog_default
    # The generic provider routes through the vendor-neutral GATEWAY
    # transport: enable flag + a real bearer-token command are emitted,
    # and crucially NO Databricks-branded transport var leaks (the
    # "inherited wart" this rename removed). The only Databricks-named
    # var that may ever appear is the profile, which is absent here
    # because this is a key provider, not a databricks-kind one.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    assert not any(k.startswith("HARNESS_CLAUDE_SDK_DATABRICKS") for k in env)


def test_pi_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An anthropic ``key`` provider with no ``models.default`` resolves the
    catalog default for the pi harness (anthropic auth-source family).

    pi prefers the anthropic family for auth, so the model fallback must
    come from the anthropic catalog default.
    """
    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="pi")

    env = _build_pi_spawn_env(spec, workdir=None)

    catalog_default = _CATALOG_DEFAULTS[("anthropic", "claude")]
    assert env["HARNESS_PI_MODEL"] == catalog_default


def test_provider_default_beats_catalog_default(config_home: Path) -> None:
    """
    A provider's ``models.default`` still wins over the catalog default.

    The catalog fallback is the LAST resort: when the provider declares a
    ``models.default`` it must be used unchanged, never overridden by the
    catalog. Failure means the precedence (provider default > catalog
    default) regressed.
    """
    _write_config(config_home, _anthropic_default_config())  # declares "claude-default-model"
    spec = _make_spec(harness="claude-sdk")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The provider's explicit default is used, not the catalog's.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-default-model"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] != _CATALOG_DEFAULTS[("anthropic", "claude")]


def test_spec_model_beats_catalog_default(config_home: Path) -> None:
    """
    A spec-level model still wins when the provider has no ``models.default``.

    The spec model is the highest-precedence source; the catalog fallback
    must not fire when the spec already named a model.
    """
    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk", model="spec-chosen-model")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Provider branch fired (base_url set) but the spec model wins.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "spec-chosen-model"


# ── databricks-kind provider routes through the profile/ucode path ──────────


# ── Backwards-compat: no provider configured ───────────────────────────────


def test_no_provider_api_key_path_unchanged(config_home: Path) -> None:
    """
    With NO provider configured, the existing api_key path is untouched.

    A spec with ``ApiKeyAuth`` and no ``providers:`` block must still emit
    ``HARNESS_CLAUDE_SDK_API_KEY_HELPER`` and NO provider gateway vars.
    Failure means the provider branch is firing when it shouldn't (and
    swallowing the api_key path), or provider vars leak in.
    """
    _write_config(config_home, {})  # empty config — no providers
    spec = _make_spec(harness="claude-sdk", model=None, auth=ApiKeyAuth(api_key="sk-direct"))

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Existing api_key path emits exactly what it did before.
    assert env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] == "printf %s sk-direct"
    # No provider gateway vars leak in (the provider branch did not fire).
    assert "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL" not in env
    assert "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND" not in env
    # api_key auth does not trigger Databricks routing.
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env


# ── cli-config kind: model_provider pinning ─────────────────────────────────


def test_pi_gateway_default_pi_scope_unresolved_credential_names_var(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``kind: gateway`` provider with ``default: pi`` names the missing env var on failure.

    When a ``kind: gateway`` provider is the pi default via an explicit
    ``default: pi`` scope and its ``api_key_ref: env:VAR`` cannot resolve
    (the env var is unset in the runner), the error must name the missing
    variable. The original credential-resolution error from ``resolve_secret``
    is surfaced rather than a generic message that omits the variable name.
    """

    monkeypatch.delenv("MY_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OMNIGENT_MY_GATEWAY_TOKEN", raising=False)
    config: dict[str, object] = {
        "providers": {
            "my-gateway": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "api_key_ref": "env:MY_GATEWAY_TOKEN",
                    "base_url": "https://example.com/v1",
                    "models": {"default": "some-model-id"},
                },
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="pi")

    with pytest.raises(OmnigentError, match="MY_GATEWAY_TOKEN"):
        _build_pi_spawn_env(spec, workdir=None)


def test_pi_gateway_default_pi_scope_resolved_credential_succeeds(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``kind: gateway`` provider with ``default: pi`` routes pi when the credential resolves.

    When ``MY_GATEWAY_TOKEN`` is exported, ``_build_pi_spawn_env`` must
    populate the openai family's gateway transport vars correctly for a
    provider that only claims the ``pi`` default scope (not ``openai``).
    """
    monkeypatch.setenv("MY_GATEWAY_TOKEN", "sk-gw-secret")
    config: dict[str, object] = {
        "providers": {
            "my-gateway": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "api_key_ref": "env:MY_GATEWAY_TOKEN",
                    "base_url": "https://example.com/v1",
                    "models": {"default": "some-model-id"},
                },
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="pi")

    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_GATEWAY"] == "true"
    assert env["HARNESS_PI_GATEWAY_BASE_URLS"] == '{"openai": "https://example.com/v1"}'
    assert env["HARNESS_PI_GATEWAY_HOST"] == "https://example.com"
    assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == "printf %s sk-gw-secret"
    assert env["HARNESS_PI_MODEL"] == "some-model-id"


_DISMISSIBLE_CODEX_CONFIG_TOML = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"

[model_providers.Databricks.auth]
command = "jq"
"""


# ---------------------------------------------------------------------------
# harness.<canonical>.command → OMNIGENT_<NAME>_PATH (spawn-env builders)
# ---------------------------------------------------------------------------


def _call_builder(builder: object, spec: AgentSpec) -> dict[str, str]:  # type: ignore[explicit-any]
    """Invoke a spawn-env builder without bundle-dir or cwd plumbing."""
    return builder(spec, cwd=None, workdir=None)  # type: ignore[operator, return-value]


@pytest.mark.parametrize(
    ("harness", "builder", "env_var"),
    [
        ("pi", _build_pi_spawn_env, "OMNIGENT_PI_PATH"),
    ],
)
def test_spawn_env_threads_config_command_to_path(
    config_home: Path,
    harness: str,
    builder: object,
    env_var: str,
) -> None:
    """A config ``harness.<canonical>.command`` lands as ``OMNIGENT_<NAME>_PATH``."""
    cfg = _openai_default_config()
    cfg["harness"] = {harness: {"command": "/custom/bin"}}
    _write_config(config_home, cfg)
    spec = _make_spec(harness=harness)

    env = _call_builder(builder, spec)

    assert env[env_var] == "/custom/bin"


@pytest.mark.parametrize(
    ("harness", "builder", "env_var"),
    [
        ("pi", _build_pi_spawn_env, "OMNIGENT_PI_PATH"),
    ],
)
def test_spawn_env_ambient_env_wins_over_config_command(
    monkeypatch: pytest.MonkeyPatch,
    config_home: Path,
    harness: str,
    builder: object,
    env_var: str,
) -> None:
    """The ambient ``OMNIGENT_<NAME>_PATH`` env var wins over config ``command``."""
    cfg = _openai_default_config()
    cfg["harness"] = {harness: {"command": "/config/bin"}}
    _write_config(config_home, cfg)
    monkeypatch.setenv(env_var, "/ambient/bin")
    spec = _make_spec(harness=harness)

    env = _call_builder(builder, spec)

    # The builder does not override the ambient env var; config is skipped.
    assert env_var not in env or env[env_var] != "/config/bin"


@pytest.mark.parametrize(
    ("harness", "builder"),
    [
        ("pi", _build_pi_spawn_env),
    ],
)
def test_spawn_env_no_command_emits_no_path(
    config_home: Path,
    harness: str,
    builder: object,
) -> None:
    """With no config ``command`` and no ambient env var, no ``*_PATH`` is emitted."""
    _write_config(config_home, _openai_default_config())
    spec = _make_spec(harness=harness)

    env = _call_builder(builder, spec)

    suffix = harness.upper()
    # Neither the canonical OMNIGENT_* nor the legacy HARNESS_* is emitted.
    assert f"OMNIGENT_{suffix}_PATH" not in env
    assert f"HARNESS_{suffix}_PATH" not in env


@pytest.mark.parametrize(
    ("harness", "builder"),
    [
        ("pi", _build_pi_spawn_env),
    ],
)
def test_spawn_env_legacy_env_wins_over_config_command(
    monkeypatch: pytest.MonkeyPatch,
    config_home: Path,
    harness: str,
    builder: object,
) -> None:
    """A deprecated ``HARNESS_<NAME>_PATH`` env var wins over config ``command``.

    Per ``env > config``, the legacy env var must not be shadowed by config.
    """
    from omnigent.harness_startup_config import _LEGACY_PATH_WARNED

    legacy_var = f"HARNESS_{harness.upper()}_PATH"
    _LEGACY_PATH_WARNED.discard(legacy_var)
    monkeypatch.delenv(f"OMNIGENT_{harness.upper()}_PATH", raising=False)
    monkeypatch.setenv(legacy_var, "/legacy/bin")
    cfg = _openai_default_config()
    cfg["harness"] = {harness: {"command": "/config/bin"}}
    _write_config(config_home, cfg)
    spec = _make_spec(harness=harness)

    env = _call_builder(builder, spec)

    # The builder must not set OMNIGENT_* from config when the legacy env wins.
    assert f"OMNIGENT_{harness.upper()}_PATH" not in env


# ── Ambient-isolation controls ─────────────────────────────────────────────


def test_claude_cli_login_cannot_shadow_claude_sdk_fallback(
    config_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_ambient_probes: SimpleNamespace,
) -> None:
    """A Keychain-backed Claude CLI login must not shadow the anthropic fallback.

    On macOS the login detector falls back to ``claude auth status``, which
    reads the Keychain and so reports a login regardless of ``$HOME``; the
    resulting subscription provider would outrank the configured-but-not-default
    anthropic credential the brain head falls back to. ``sys.platform`` is
    pinned to darwin so the macOS-only branch is exercised on every CI OS, and
    the calibration assertion proves the unpatched detector would report the
    simulated login.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    # ``_claude_login_detected`` imports this name from the module at call
    # time, so the module attribute is the seam.
    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda *args, **kwargs: True)
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Calibration: file check misses under the isolated HOME, so the darwin
    # CLI fallback fires and the unpatched detector reports a login.
    assert real_ambient_probes.claude_login_detected() is True
    # Isolation: the patched detector never consults the CLI.
    assert ambient._claude_login_detected() is False

    config = {
        "providers": {
            "vendor-anthropic": {  # configured, but NOT marked default
                "kind": "key",
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1",
                    "sk-ant-secret",
                    "claude-default-model",
                ),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"


def test_stale_login_cache_cannot_shadow_claude_sdk_fallback(
    config_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_ambient_probes: SimpleNamespace,
) -> None:
    """A TTL-cached login verdict from an earlier test must not shadow the fallback.

    An earlier test's ambient sweep can run ``claude auth status`` while a
    vendor key sits in the environment; the CLI reports a login for that key
    and the positive verdict is TTL-cached per ``(key, binary)``, outliving the
    test that produced it. A later test with an isolated ``$HOME`` would then
    consume the stale positive and route through a subscription provider
    instead of its configured anthropic credential. The fixture boundary must
    make that cache unreachable.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    # Prime the cache under the key production writes: (family, resolved
    # binary path). Binary resolution is stubbed so the cache is hit — and the
    # status subprocess never spawns — on machines with no claude install.
    fake_binary = "/fake/bin/claude"
    monkeypatch.setattr(
        harness_install, "shutil", SimpleNamespace(which=lambda _name: fake_binary)
    )
    harness_install._LOGIN_PROBE_CACHE[("anthropic", fake_binary)] = time.monotonic() + 3600.0
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    # Calibration: the unpatched detector consumes the stale positive.
    assert real_ambient_probes.claude_login_detected() is True
    # Isolation: the patched detector never reaches the cache.
    assert ambient._claude_login_detected() is False

    config = {
        "providers": {
            "vendor-anthropic": {  # configured, but NOT marked default
                "kind": "key",
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1",
                    "sk-ant-secret",
                    "claude-default-model",
                ),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
