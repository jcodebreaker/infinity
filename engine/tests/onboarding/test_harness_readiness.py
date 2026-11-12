"""Tests for harness readiness checks (``harness_readiness.py``)."""

from __future__ import annotations

import subprocess
import threading
from collections import Counter
from pathlib import Path

import pytest
import yaml

import omnigent.onboarding.harness_install as hi
from omnigent.onboarding.harness_readiness import (
    _READINESS_PROBE_MAX_WORKERS,
    configured_harness_map,
    harness_is_configured,
)


@pytest.fixture(autouse=True)
def _isolate_cli_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate CLI credential sources so their readiness is deterministic.

    Point the config home at an empty tmp dir so a developer's real config
    can't flip a verdict under these tests.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    # SDK-harness readiness checks ambient credential sources (env API keys,
    # Claude Code's login / managed settings, GCP ADC).
    # Isolate every one of them — a developer's or CI runner's real key or
    # gcloud ADC would otherwise flip these verdicts.
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(f"OMNIGENT_{var}", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # gcloud ADC, …
    from omnigent.onboarding import ambient as _ambient

    monkeypatch.setattr(_ambient, "_claude_login_detected", lambda: False)
    monkeypatch.setattr(
        _ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (tmp_path / "managed-absent.json",)
    )
    # The provider fallback check merges ambient detections the way runtime
    # resolution does (``effective_config_with_detected``); stub the live
    # detector so a developer's real env keys / CLI logins / local Ollama
    # can't flip these verdicts (tests inject detections explicitly).
    import omnigent.onboarding.detected as _detected

    monkeypatch.setattr(_detected, "detect_providers", list)
    # Codex readiness resolves the binary via resolve_cli_binary, which honors
    # an OMNIGENT_CODEX_PATH override and probes on-disk global install dirs.
    # Clear the override and stub the fallback dirs so a developer's real codex
    # install can't flip the binary-missing verdict these tests assert.
    import omnigent._platform as platform

    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setattr(platform, "_cli_fallback_dirs", lambda: ())


def _all_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear installed.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    # Follow test_harness_install.py's convention: patch the module's
    # shutil.which (reverted by monkeypatch after the test).
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    # Versioned harnesses validate the CLI's ``--version``. Stub a satisfying
    # version so tests that simply need "binary present" are not tripped up by
    # an unexpected subprocess probe.
    def _stub_run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="9.9.9\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess during readiness tests: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _stub_run)
    # Auth-aware native harnesses check login state
    # in the picker map. Treat them as logged in when the test just needs
    # "binary present".
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)


def _no_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear missing.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)


# SDK and unknown harnesses are never gated — their credentials resolve at
# runtime from ambient/spec sources the daemon can't enumerate.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-sdk",
        "claude_sdk",
        "claude",  # alias → claude-sdk
        "some-future-harness",  # unknown → fail open
    ],
)
def test_sdk_and_unknown_harnesses_are_never_gated(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """SDK / unknown harnesses are configured even with no CLI installed.

    They run in-process (or are unknown to the daemon) and resolve any
    credential at runtime, so the daemon must not block them. A ``False``
    here is a false negative that would break a launch authenticating via
    an env key, a provider profile, or the spec's ``executor.auth`` —
    none of which the daemon can see.
    """
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is True


# CLI-wrapping harnesses are gated on their binary being on PATH.
@pytest.mark.parametrize(
    "harness",
    [
        "pi",
    ],
)
def test_cli_harness_configured_only_when_binary_installed(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """A CLI-wrapping harness is configured iff its binary is on PATH.

    These harnesses cannot run without their CLI; the missing binary is
    the one thing the daemon can reliably detect. Installed → True,
    absent → False. A wrong verdict here either blocks the headline
    "I never installed Claude Code/Codex" case (if it stayed True) or
    breaks every native launch (if it stayed False).
    """
    _all_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is True
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is False


def test_configured_harness_map_covers_all_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hello-frame map carries every spelling a consumer may hold.

    The server/web UI does a plain dict lookup with whatever harness
    string it has (spec executor types, canonical ids, aliases) — a
    missing key reads as "unknown" and silently disables the warning
    for that agent.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    expected_keys = {
        "claude-sdk",
        "claude_sdk",
        "claude",
        "pi",
    }
    assert set(result) == expected_keys


def test_configured_harness_map_gates_only_cli_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no CLI installed, spellings classify onto the right readiness axis.

    SDK spellings (incl. the ``claude`` alias) have no binary to miss — with no credential visible
    either they read the credential axis (``needs-auth``), never a
    binary-shaped ``False``. The native + pi spellings flip on the binary. A
    misclassified spelling would warn the wrong agents in the picker.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    # SDK / alias spellings — no binary axis; with nothing configured they
    # read the credential axis. (An SDK agent authenticating via a visible
    # source — an env key — reads True; covered below.)
    for sdk in (
        "claude-sdk",
        "claude_sdk",
        "claude",
    ):
        assert result[sdk] == "needs-auth", f"{sdk} should read the credential axis"
    # CLI-wrapping harnesses (codex, claude, pi) are auth-aware: they carry a
    # two-step signal in the picker map, so a missing binary is the structured
    # ``"binary-missing"`` (step 1 to-do), not a bare ``False``. Pi reports the
    # credential axis once installed (no CLI login; its credential is a provider).
    for missing in ("pi",):
        assert result[missing] == "binary-missing", f"{missing} should name the missing CLI binary"


def test_configured_harness_map_all_true_with_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every spelling reads True once the CLIs are installed and the key/token-
    gated harnesses are satisfied.

    The CLI harnesses pass their binary check and the provider check is
    satisfied, so nothing is reported unconfigured.
    """
    _all_clis_installed(monkeypatch)
    # claude / pi are auth-aware on the credential axis now: satisfy the provider
    # check deterministically (don't depend on the dev machine's real config).
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )
    result = configured_harness_map()
    not_ready = {k: v for k, v in result.items() if v is not True}
    assert not not_ready, f"expected every spelling ready, got {not_ready}"


def test_configured_harness_map_runs_independent_probes_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent readiness probes overlap without duplicating aliases."""
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0
    calls: Counter[str] = Counter()

    def _availability(canonical: str) -> bool | str:
        nonlocal active, max_active
        with lock:
            calls[canonical] += 1
            active += 1
            max_active = max(max_active, active)
            # Two distinct probes (claude-sdk, pi) once aliases fold together.
            if active >= min(_READINESS_PROBE_MAX_WORKERS, 2):
                release.set()
        if not release.wait(timeout=1.0):
            raise AssertionError("readiness probes did not overlap")
        with lock:
            active -= 1
        if "codex" in canonical:
            return "needs-auth"
        return canonical != "pi"

    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._harness_availability",
        _availability,
    )

    result = configured_harness_map()

    assert result
    assert max_active == min(_READINESS_PROBE_MAX_WORKERS, 2)
    assert all(count == 1 for count in calls.values())
    assert result["pi"] is False


def test_sdk_harness_needs_auth_with_no_visible_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-less host reports the SDK harnesses as ``needs-auth``.

    The headline picker bug: the in-process SDK harnesses were hardcoded
    ready, so a host with no resolvable credential offered them with no
    warning and the launch died at the first turn. The map now reads the
    credential axis — while the launch gate stays ungated (the warning
    informs, it never blocks).
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    for sdk in (
        "claude-sdk",
        "claude_sdk",
        "claude",
    ):
        assert result[sdk] == "needs-auth", f"{sdk} should read needs-auth"
        assert harness_is_configured(sdk) is True, f"{sdk} launch gate must stay ungated"


def test_sdk_harness_ready_via_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured family provider entry alone makes the SDK harnesses ready."""
    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True


def test_sdk_harness_ready_via_family_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient family API key readies only that family's SDK harness."""
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    result = configured_harness_map()
    assert result["claude-sdk"] is True

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"


def test_claude_sdk_ready_via_claude_code_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code's own login serves claude-sdk (the SDK drives that CLI)."""
    from omnigent.onboarding import ambient

    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(ambient, "_claude_login_detected", lambda: True)
    result = configured_harness_map()
    assert result["claude-sdk"] is True


def test_claude_sdk_ready_via_managed_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's managed-settings gateway alone makes claude-sdk ready."""
    import json

    from omnigent.onboarding import ambient

    _no_clis_installed(monkeypatch)
    settings = tmp_path / "managed-settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://dbc.cloud.databricks.com/ai-gateway/anthropic"
                },
                "apiKeyHelper": "print-token",
            }
        )
    )
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (settings,))
    assert configured_harness_map()["claude-sdk"] is True


def test_sdk_harness_ready_via_global_auth_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-level global ``auth:`` block counts as an SDK credential source.

    Both SDK builders inherit the global ``config.yaml`` ``auth:`` mapping when
    the agent spec declares no ``executor.auth`` (workflow's
    ``_load_global_auth``), so a host configured only through global API-key
    authentication must not read ``needs-auth`` (which the picker renders as
    an unselectable row).
    """
    _no_clis_installed(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "api_key", "api_key": "sk-global-test"}})
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True


@pytest.mark.parametrize("token_var", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"])
def test_claude_sdk_ready_via_ambient_claude_token_env(
    monkeypatch: pytest.MonkeyPatch,
    token_var: str,
) -> None:
    """Ambient Claude token env credentials count for claude-sdk readiness.

    The host forwards ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``
    subscription auth) and ``ANTHROPIC_AUTH_TOKEN`` (gateway bearer, paired
    with ``ANTHROPIC_BASE_URL``) to its runners, and Claude Code resolves them
    directly, so a token-only host must not read ``needs-auth``.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv(token_var, "token-test-value")
    if token_var == "ANTHROPIC_AUTH_TOKEN":
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")
    result = configured_harness_map()
    assert result["claude-sdk"] is True


def test_sdk_harness_ready_via_non_default_family_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured non-default family provider counts as an SDK credential.

    Launch resolution falls back to the first provider entry serving the
    family when no default is configured (``first_available_provider``,
    consumed with ``for_launch=True`` in the runtime), so readiness must not
    report ``needs-auth`` for a host whose only credential is a non-default
    provider entry serving the harness's family — provided its credential
    reference actually resolves locally.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("WORK_ANTHROPIC_KEY", "sk-work-test-key")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "work-anthropic": {
                        "kind": "key",
                        "anthropic": {
                            "base_url": "https://api.example.com/v1",
                            "api_key_ref": "env:WORK_ANTHROPIC_KEY",
                        },
                    }
                }
            }
        )
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True


@pytest.mark.parametrize("default", [False, True])
def test_sdk_provider_with_unresolved_credential_reference_is_not_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default: bool,
) -> None:
    """An unresolved ``api_key_ref`` must not report the SDK harness ready.

    Launch resolves the selected entry's secret through
    ``ProviderEntry.family()`` / ``resolve_secret`` and fails rather than
    skipping the provider, so an entry pointing at an unset ``env:`` variable
    is a first-turn auth failure, not a credential — readiness must warn
    instead of reporting ready. Covers both the default-provider and
    first-available fallback selection paths.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.delenv("MISSING_READINESS_TEST_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_MISSING_READINESS_TEST_KEY", raising=False)
    entry: dict[str, object] = {
        "kind": "key",
        "anthropic": {
            "base_url": "https://api.example.com/v1",
            "api_key_ref": "env:MISSING_READINESS_TEST_KEY",
        },
    }
    if default:
        entry["default"] = True
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"work": entry}}))
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
