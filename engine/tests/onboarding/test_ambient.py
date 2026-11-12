"""Tests for omnigent.onboarding.ambient — machine credential detection.

Detection reads the environment, two CLI-login files under ``$HOME``, a single
localhost TCP probe for Ollama, and — on macOS only — a ``claude auth status``
fallback for the Keychain-stored Claude credential. These tests redirect
``$HOME`` to a tmp dir, control the environment explicitly, and monkeypatch
both :func:`omnigent.onboarding.ambient._ollama_reachable` and
:func:`omnigent.onboarding.harness_install.harness_cli_logged_in` so no real
network or subprocess I/O occurs. Each test asserts the exact
:class:`DetectedProvider` fields (name / kind / family / source), not just the
count, so a wrong field turns the test red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.onboarding import ambient
from omnigent.onboarding.ambient import (
    DetectedProvider,
    detect_providers,
)

# The suite-wide ``_isolate_ambient_provider_discovery`` fixture swaps the
# login check for a file-only stub; this module tests the real one.
_REAL_CLAUDE_LOGIN_DETECTED = ambient._claude_login_detected

# Every provider env var ambient may read — cleared in the base fixture so
# the host's own keys don't leak into the deterministic detection tests.
_PROVIDER_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "OMNIGENT_ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OMNIGENT_OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OMNIGENT_OPENROUTER_API_KEY",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
]


@pytest.fixture
def clean_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolate detection: empty HOME, no provider keys, Ollama unreachable.

    Redirects ``$HOME`` to a tmp dir (so the CLI-login path checks see no
    credential files), clears every provider env var, stubs
    ``_ollama_reachable`` to ``False``, and stubs the macOS Keychain fallback
    (``harness_cli_logged_in``) to ``False`` so the suite never shells out to a
    real ``claude auth status`` — keeping detection deterministic and free of
    real I/O even when the suite runs on a macOS host with a logged-in CLI.
    Individual tests then opt back in to exactly the signal they exercise.

    :param tmp_path: pytest's per-test temporary directory fixture.
    :param monkeypatch: pytest's env/attr patching fixture.
    :returns: The tmp HOME path, e.g. ``"/tmp/pytest-.../test_x0"``.
    """
    from omnigent.onboarding import harness_install

    monkeypatch.setenv("HOME", str(tmp_path))
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: False)
    # Neutralize the macOS Keychain fallback by default (the file check already
    # sees no creds under the tmp HOME). The detected/absent tests override this.
    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: False)
    monkeypatch.setattr(ambient, "_claude_login_detected", _REAL_CLAUDE_LOGIN_DETECTED)
    return tmp_path


def test_no_credentials_detected(clean_env) -> None:
    """With nothing present, detection returns an empty list.

    Failure means a stray host credential leaked into the test, or
    detection fabricated a provider with no backing signal.
    """
    assert detect_providers() == []


@pytest.mark.parametrize(
    "env_var,expected",
    [
        (
            "ANTHROPIC_API_KEY",
            DetectedProvider(
                name="anthropic", kind="key", family="anthropic", source="$ANTHROPIC_API_KEY"
            ),
        ),
        (
            "OPENAI_API_KEY",
            DetectedProvider(name="openai", kind="key", family="openai", source="$OPENAI_API_KEY"),
        ),
        (
            "OPENROUTER_API_KEY",
            DetectedProvider(
                name="openrouter", kind="key", family="openai", source="$OPENROUTER_API_KEY"
            ),
        ),
    ],
)
def test_env_key_detection(
    clean_env, monkeypatch: pytest.MonkeyPatch, env_var: str, expected: DetectedProvider
) -> None:
    """Each provider env key is detected with the right family mapping.

    Failure means a key would be missed, or mapped to the wrong family —
    routing a Claude key through the OpenAI surface, say.
    """
    monkeypatch.setenv(env_var, "some-secret-value")
    detected = detect_providers()
    # Exactly the one key we set is detected, with all fields exact.
    assert detected == [expected]


def test_env_key_detection_accepts_omnigent_prefixed_alias(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``OMNIGENT_``-prefixed key is detected without setting the raw name."""
    monkeypatch.setenv("OMNIGENT_ANTHROPIC_API_KEY", "some-secret-value")

    detected = detect_providers()

    assert detected == [
        DetectedProvider(
            name="anthropic",
            kind="key",
            family="anthropic",
            source="$OMNIGENT_ANTHROPIC_API_KEY",
        )
    ]


def test_empty_env_key_not_detected(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (set-but-blank) env var is not treated as a credential.

    Failure means a ``ANTHROPIC_API_KEY=""`` would surface a bogus
    detection that resolves to an empty bearer token downstream.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert detect_providers() == []


@pytest.mark.parametrize(
    "creds_json",
    [
        # Renewable login: refresh token present (access token may be stale).
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real", "expiresAt": 1}}',
        # No refresh token, but the access token is far from expiry.
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "", '
        '"expiresAt": 99999999999999}}',
    ],
)
def test_claude_cli_login_detected(clean_env, creds_json: str) -> None:
    """A ``~/.claude/.credentials.json`` carrying a usable login is detected.

    Failure means a genuinely logged-in Claude CLI would not be offered during
    setup. The two shapes cover the "renewable (refresh token)" and "unexpired
    access token" branches of usability.
    """
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(creds_json, encoding="utf-8")
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


@pytest.mark.parametrize(
    "creds_json",
    [
        "{}",  # empty — logged out / never logged in
        '{"claudeAiOauth": {}}',  # present object, no tokens
        '{"claudeAiOauth": {"accessToken": "", "refreshToken": "rt"}}',  # blank access token
        # Expired access token with NO refresh token → dead (re-login needed).
        '{"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 1}}',
        '{"claudeAiOauth": null}',
        "not json",  # malformed
    ],
)
def test_claude_auth_without_credential_not_detected(clean_env, creds_json: str) -> None:
    """A ``.credentials.json`` with no usable login is NOT detected (claude Fix 1).

    Mirrors the codex check: existence alone used to count as a subscription, so
    an empty / logged-out / expired-without-refresh / malformed file planted a
    phantom claude subscription. The expired-without-refresh case is the
    important one — an expired access token with a refresh token is still usable
    (covered above), but with neither it's a dead login. Failure means the
    over-detection regressed.
    """
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(creds_json, encoding="utf-8")
    assert detect_providers() == []


def test_claude_macos_keychain_login_detected(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS with no creds file, a Keychain login (CLI status) is detected.

    The bug this fixes: Claude Code stores its OAuth in the macOS Keychain, not
    ``~/.claude/.credentials.json``, so the file check misses a real, working
    subscription — and ``configure harnesses`` failed to auto-detect it even
    though the same machine could sign in without a web login. With the file
    absent, detection must fall back to the CLI status check on macOS. Failure
    means the Keychain subscription is silently dropped again.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    # No ~/.claude/.credentials.json under the tmp HOME → file check is False,
    # forcing the macOS Keychain fallback. The fallback asks the CLI (which
    # reads the Keychain); stub it so no real ``claude auth status`` runs.
    from omnigent.onboarding import harness_install

    seen_keys: list[str] = []

    def _fake_logged_in(key: str) -> bool:
        seen_keys.append(key)
        return True

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _fake_logged_in)
    detected = detect_providers()
    # The fallback queried the CLI for the anthropic family specifically.
    assert seen_keys == ["anthropic"]
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


def test_claude_macos_keychain_absent_not_detected(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS with no creds file and a logged-out CLI, claude is NOT detected.

    The Keychain fallback runs (file is absent) but the CLI reports no login, so
    detection must stay empty — no phantom subscription. Failure means the macOS
    fallback fabricates a subscription whenever the file happens to be missing.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    from omnigent.onboarding import harness_install

    seen_keys: list[str] = []

    def _fake_logged_in(key: str) -> bool:
        seen_keys.append(key)
        return False

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _fake_logged_in)
    # The fallback was consulted (proving it ran) but reported no login.
    assert detect_providers() == []
    assert seen_keys == ["anthropic"]


def test_claude_linux_no_keychain_cli_fallback(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux the CLI-status fallback never runs — detection stays file-only.

    The macOS Keychain fallback is gated on ``sys.platform == "darwin"``: Linux
    stores the credential in the file, so a subprocess fallback would be both
    wrong (Linux is file-accurate) and a needless cost. The stub raises if
    called, so any Linux invocation of the CLI fallback fails the test.
    """
    monkeypatch.setattr(ambient.sys, "platform", "linux")
    from omnigent.onboarding import harness_install

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"CLI fallback must not run on Linux (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    # No creds file under tmp HOME, and the fallback is gated off → empty.
    assert detect_providers() == []


def test_claude_macos_file_present_skips_cli_fallback(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS a usable creds file is detected without invoking the CLI.

    Detection is file-first: when ``~/.claude/.credentials.json`` already
    carries a usable login, the macOS path must short-circuit and never pay the
    ``claude auth status`` subprocess. The stub raises if called, so a redundant
    fallback fails the test.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    from omnigent.onboarding import harness_install

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"file-present path must not invoke the CLI (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real"}}',
        encoding="utf-8",
    )
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


def test_ollama_detected_when_reachable(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable local Ollama is detected as a local openai provider.

    Uses the monkeypatched ``_ollama_reachable`` so no real socket is
    opened. Failure means a running local model server would not be offered.
    """
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: True)
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="ollama",
            kind="local",
            family="openai",
            source="http://localhost:11434",
        )
    ]


def test_detection_priority_order(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """All signals together are returned in env → claude → ollama order.

    Failure means the stable ordering broke, so the setup UI would present
    detected providers in a non-deterministic / surprising order.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "k-oai")
    claude_dir = clean_env / ".claude"
    claude_dir.mkdir()
    # A real credential — existence alone is no longer enough to be detected.
    (claude_dir / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: True)

    detected = detect_providers()
    # Env keys first, in PROVIDER_ENV_VARS iteration order (openai precedes
    # anthropic in that dict), then claude login, ollama.
    assert [d.name for d in detected] == ["openai", "anthropic", "claude", "ollama"]


# ── Claude on Vertex AI (GCP ADC) detection ──────────────────────────────────


def test_vertex_claude_detected_with_all_env_vars(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three Vertex env vars present → vertex-claude is detected.

    Claude Code natively supports Vertex AI via CLAUDE_CODE_USE_VERTEX,
    ANTHROPIC_VERTEX_PROJECT_ID, and CLOUD_ML_REGION. When all three are
    set, ambient detection must surface a vertex-claude provider so
    Omnigent recognises the credential and routes through the CLI's own
    Vertex auth (GCP ADC).
    """
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-gcp-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="vertex-claude",
            kind="key",
            family="anthropic",
            source="$CLAUDE_CODE_USE_VERTEX",
        )
    ]


@pytest.mark.parametrize(
    "label,env",
    [
        ("missing-use-vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "r"}),
        ("missing-project-id", {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "r"}),
        ("missing-region", {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "p"}),
        (
            "use-vertex-false",
            {
                "CLAUDE_CODE_USE_VERTEX": "0",
                "ANTHROPIC_VERTEX_PROJECT_ID": "p",
                "CLOUD_ML_REGION": "r",
            },
        ),
        (
            "use-vertex-blank",
            {
                "CLAUDE_CODE_USE_VERTEX": "",
                "ANTHROPIC_VERTEX_PROJECT_ID": "p",
                "CLOUD_ML_REGION": "r",
            },
        ),
    ],
)
def test_vertex_claude_not_detected_when_incomplete(
    clean_env, monkeypatch: pytest.MonkeyPatch, label: str, env: dict[str, str]
) -> None:
    """Missing or disabled Vertex env vars → no vertex-claude detection.

    All three vars must be present and CLAUDE_CODE_USE_VERTEX must be
    truthy. Failure means a partial configuration would surface a bogus
    provider that fails at run time.
    """
    for var, val in env.items():
        monkeypatch.setenv(var, val)
    assert detect_providers() == []


def test_prewarm_detect_providers_is_consumed_once(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prewarmed sweep serves exactly the next detection, then expires.

    One-shot semantics keep staleness bounded: only the runner-boot consumer
    (seconds after the prewarm) sees the snapshot; every later call runs a
    fresh sweep, exactly as before the prewarm existed.
    """
    sweeps: list[str] = []
    sentinel = [
        DetectedProvider(name="claude", kind="subscription", family="anthropic", source="test")
    ]

    def _fake_sweep() -> list[DetectedProvider]:
        sweeps.append("sweep")
        return sentinel

    monkeypatch.setattr(ambient, "_detect_providers_now", _fake_sweep)
    monkeypatch.setattr(ambient, "_prewarmed_detection", None)

    ambient.prewarm_detect_providers()
    # Consumes the prewarmed future — no second sweep.
    assert detect_providers() == sentinel
    assert sweeps == ["sweep"]
    # The next call is fresh, not the stale snapshot.
    assert detect_providers() == sentinel
    assert sweeps == ["sweep", "sweep"]


def test_prewarm_detect_providers_failure_falls_back_to_fresh_sweep(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed speculative sweep must not poison the real detection."""
    sweeps: list[str] = []

    def _flaky_sweep() -> list[DetectedProvider]:
        sweeps.append("sweep")
        if len(sweeps) == 1:
            raise RuntimeError("scripted prewarm failure")
        return []

    monkeypatch.setattr(ambient, "_detect_providers_now", _flaky_sweep)
    monkeypatch.setattr(ambient, "_prewarmed_detection", None)

    ambient.prewarm_detect_providers()
    assert detect_providers() == []
    assert sweeps == ["sweep", "sweep"]


# --- Claude Code managed-settings gateway (the `isaac configure claude` shape) ---
#
# The credential lives in Claude Code's own enterprise settings, so omnigent
# REFLECTS it (readiness + label) but surfaces it as the existing `subscription`
# credential — never a new `cli-config` provider shape, which released/stale
# runners reject at parse (the version-skew break that got the first attempt
# reverted). These tests pin that: detection yields a subscription, and the
# managed paths are read from an explicit tuple (absolute paths escape $HOME).

_ISAAC_CLAUDE_SETTINGS: dict[str, object] = {
    "env": {
        "ANTHROPIC_BASE_URL": "https://dbc-test.cloud.databricks.com/ai-gateway/anthropic",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8",
    },
    "apiKeyHelper": "jq -r '.access_token' ~/.databricks/model-serving-token.json",
}


def _write_managed_settings(home, monkeypatch, payload: object):
    """Write a Claude Code managed-settings file and point detection at it."""
    import json

    path = home / "managed-settings.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (path,))
    return path


def test_claude_managed_gateway_reads_base_url_and_credential(clean_env, monkeypatch) -> None:
    """An isaac-style settings file yields (base_url, has_credential=True)."""
    _write_managed_settings(clean_env, monkeypatch, _ISAAC_CLAUDE_SETTINGS)
    base_url, has_credential = ambient.claude_managed_gateway()
    assert base_url == "https://dbc-test.cloud.databricks.com/ai-gateway/anthropic"
    assert has_credential is True


def test_claude_managed_gateway_detected_as_subscription_not_cli_config(
    clean_env, monkeypatch
) -> None:
    """The managed gateway surfaces as a `claude` SUBSCRIPTION, never `cli-config`.

    This is the load-bearing safety property: a `cli-config cli:claude` entry is
    a poison pill an older runner's parser rejects, breaking every turn. Failure
    here means the revert's root cause has been reintroduced.
    """
    _write_managed_settings(clean_env, monkeypatch, _ISAAC_CLAUDE_SETTINGS)
    detected = detect_providers()
    assert [(d.name, d.kind, d.family) for d in detected] == [
        ("claude", "subscription", "anthropic")
    ]
    assert detected[0].source == "Claude Code managed settings"
    assert all(d.kind != "cli-config" or d.name != "claude" for d in detected)


def test_claude_managed_gateway_wins_over_cli_login_no_double_count(
    clean_env, monkeypatch
) -> None:
    """Gateway present AND `claude auth status` logged-in → ONE claude subscription.

    `claude auth status` reports a managed apiKeyHelper as logged in, so the two
    signals describe the same credential; detection must not emit it twice.
    """
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: True)
    _write_managed_settings(clean_env, monkeypatch, _ISAAC_CLAUDE_SETTINGS)
    claude_rows = [d for d in detect_providers() if d.name == "claude"]
    assert len(claude_rows) == 1
    assert claude_rows[0].source == "Claude Code managed settings"


def test_claude_cli_login_still_detected_without_managed_gateway(clean_env, monkeypatch) -> None:
    """With no managed gateway, an ordinary CLI login is still detected."""
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: True)
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", ())
    claude_rows = [d for d in detect_providers() if d.name == "claude"]
    assert [(d.kind, d.source) for d in claude_rows] == [("subscription", "claude CLI login")]


def test_claude_managed_gateway_synthesizes_a_subscription_entry(clean_env, monkeypatch) -> None:
    """The adopted/synthesized entry is a plain subscription — old-parser-safe.

    Proves the shape written to (and merged into) config.yaml is
    `{kind: subscription, cli: claude}` — a value every released parser accepts —
    with NO `cli-config` / `model_provider` / new field.
    """
    from omnigent.onboarding.detected import synthesize_detected_entries

    _write_managed_settings(clean_env, monkeypatch, _ISAAC_CLAUDE_SETTINGS)
    entries = synthesize_detected_entries(detect_providers())
    assert entries["claude"] == {"kind": "subscription", "cli": "claude"}


@pytest.mark.parametrize(
    "payload,expected",
    [
        (_ISAAC_CLAUDE_SETTINGS, "dbc-test.cloud.databricks.com"),
        (
            {
                "env": {"ANTHROPIC_BASE_URL": "https://llm.corp.example.com/v1"},
                "apiKeyHelper": "print-token",
            },
            "llm.corp.example.com",
        ),
        ({"apiKeyHelper": "print-token"}, "Claude Code gateway"),  # helper, no base URL
        ({"env": {"ANTHROPIC_BASE_URL": "https://x.cloud.databricks.com/ai-gateway/a"}}, None),
        ({"env": {"MCP_TOOL_TIMEOUT": "1"}}, None),
    ],
)
def test_claude_managed_gateway_display_name(
    clean_env, monkeypatch, payload: object, expected: object
) -> None:
    """The display label reflects the backing; None when no credential is delivered."""
    _write_managed_settings(clean_env, monkeypatch, payload)
    assert ambient.claude_managed_gateway_display_name() == expected


def test_claude_managed_gateway_missing_file_is_graceful(clean_env, monkeypatch) -> None:
    """An absent settings file yields (None, False) rather than raising."""
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (clean_env / "nope.json",))
    assert ambient.claude_managed_gateway() == (None, False)
    assert [d for d in detect_providers() if d.name == "claude"] == []


def test_claude_managed_gateway_first_readable_file_decides(clean_env, monkeypatch) -> None:
    """A higher-precedence file without a gateway is not overridden by a lower one."""
    import json

    high = clean_env / "high.json"
    high.write_text(json.dumps({"env": {"MCP_TOOL_TIMEOUT": "1"}}))
    low = clean_env / "low.json"
    low.write_text(json.dumps(_ISAAC_CLAUDE_SETTINGS))
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (high, low))
    assert ambient.claude_managed_gateway() == (None, False)


def test_claude_managed_model_picker_reads_replacement_options(tmp_path: Path) -> None:
    path = tmp_path / "managed-settings.json"
    path.write_text(
        json.dumps(
            {
                "modelPicker": {
                    "options": [
                        {"model": "gateway-opus", "label": "Opus"},
                        {"model": "gateway-sonnet", "label": "Sonnet"},
                    ],
                    "replaceBuiltInOptions": True,
                }
            }
        )
    )

    assert ambient.claude_managed_model_picker((path,)) == (
        ("gateway-opus", "Opus"),
        ("gateway-sonnet", "Sonnet"),
    )
