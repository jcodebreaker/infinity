"""Detect AI credentials already present on the machine.

This module discovers credentials a host already has — vendor API keys in
the environment, a logged-in ``claude`` CLI, or a local Ollama server — so
the runtime can fold them into the provider config without asking the user
to paste keys they already have.

Detection is almost entirely pure standard library (``os``, ``socket``,
``pathlib``) and performs no network I/O beyond a single non-blocking
localhost TCP probe for Ollama. The one exception is macOS Claude detection:
Claude Code stores its subscription OAuth in the macOS Keychain (not a file),
so on macOS — and only when the file check comes up empty — Claude detection
falls back to a ``claude auth status`` subprocess (see
:func:`_claude_login_detected`). Linux detection stays purely file-based and
subprocess-free.

The output is a list of :class:`DetectedProvider`, one per credential
found, in a stable priority order (environment keys first, then CLI
logins, then a local server). The caller maps each detection's
:attr:`DetectedProvider.family` to the harness surface it serves.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY
from omnigent.onboarding.providers import PROVIDER_ENV_VARS
from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

DetectedKind = Literal["key", "subscription", "local"]

# The detection kinds. ``key`` is a vendor API key from the environment;
# ``subscription`` is a logged-in CLI; ``local`` is a self-hosted endpoint.
KEY_KIND: DetectedKind = "key"
SUBSCRIPTION_KIND: DetectedKind = "subscription"
LOCAL_KIND: DetectedKind = "local"

# Ollama's default OpenAI-compatible endpoint.
_OLLAMA_HOST = "localhost"
_OLLAMA_PORT = 11434
_OLLAMA_URL = f"http://{_OLLAMA_HOST}:{_OLLAMA_PORT}"

# Timeout (seconds) for the Ollama TCP probe — short so setup stays snappy
# when nothing is listening.
_OLLAMA_PROBE_TIMEOUT = 0.25

# Claude Code's enterprise "managed settings" chain, highest precedence first.
# An enterprise install (the shape ``isaac configure claude`` writes) configures
# Claude Code here and nowhere else: an ``env`` block pinning
# ``ANTHROPIC_BASE_URL`` at a gateway plus a top-level ``apiKeyHelper`` that
# prints the bearer token. Managed settings win at Claude Code's own launch, so
# this file alone is a complete, working credential — but it belongs to Claude
# Code, not omnigent: we reflect it (readiness + label), we never adopt it into
# a ``providers:`` shape (see ``claude_managed_gateway``).
CLAUDE_CODE_MANAGED_SETTINGS_PATHS: tuple[Path, ...] = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
    Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ClaudeCode" / "managed-settings.json",
)

# Maps each provider whose env key we surface to the served model family.
# Providers absent here (or mapped to ``None``) are reported with
# ``family=None`` — their key is detected but no harness surface is
# implied. Anthropic serves the ``anthropic`` surface; OpenAI and
# OpenAI-compatible gateways (OpenRouter) serve the ``openai`` surface.
_ENV_KEY_FAMILY: dict[str, str | None] = {
    "anthropic": ANTHROPIC_FAMILY,
    "openai": OPENAI_FAMILY,
    "openrouter": OPENAI_FAMILY,
}


@dataclass(frozen=True)
class DetectedProvider:
    """A credential found on the machine during ambient detection.

    :param name: The provider/source name, e.g. ``"anthropic"``,
        ``"openai"``, ``"openrouter"``, ``"claude"``, or ``"ollama"``.
    :param kind: How the credential authenticates — ``"key"`` (an API key
        in the environment), ``"subscription"`` (a logged-in CLI), or
        ``"local"`` (a self-hosted endpoint).
    :param family: The model family this credential serves
        (``"anthropic"`` / ``"openai"``), or ``None`` when the
        credential is detected but maps to no omnigent harness surface.
    :param source: A human-readable descriptor of where the credential
        comes from, e.g. ``"$ANTHROPIC_API_KEY"``, ``"claude CLI login"``,
        or ``"http://localhost:11434"``.
    """

    name: str
    kind: DetectedKind
    family: str | None
    source: str


def _claude_credentials_path() -> Path:
    """Return the path to the Claude CLI's stored login credentials.

    Honors ``$HOME`` (and thus ``monkeypatch.setenv("HOME", ...)``) so the
    check can be redirected in tests.

    :returns: Path to ``~/.claude/.credentials.json``.
    """
    return Path(os.path.expanduser("~")) / ".claude" / ".credentials.json"


def claude_auth_has_credential(creds_path: Path) -> bool:
    """Return whether a Claude ``.credentials.json`` carries a usable login.

    Claude Code stores its subscription OAuth credentials under a ``claudeAiOauth`` object
    (verified against a live file: ``accessToken``, ``refreshToken``,
    ``expiresAt`` as epoch **milliseconds**, ``scopes``, ``subscriptionType``).

    The access token is short-lived (~hours) and the CLI silently refreshes it
    from ``refreshToken``, so "expired" is the normal steady state — gating on
    ``expiresAt`` alone would flag a perfectly good login every time the token
    rolls over. A login therefore counts as usable when it parses and has a
    non-empty ``accessToken`` that is **either renewable** (a non-empty
    ``refreshToken``) **or** not yet expired (``expiresAt`` in the future). This
    rejects the empty / logged-out / malformed cases without false-flagging a
    stale-but-refreshable token.

    This is purely local (no network): it cannot detect a
    server-side *revocation* — only the harness's own login attempt can. Its job
    is to reject "no usable login," not to prove the token will authenticate.

    :param creds_path: Path to ``.credentials.json``, e.g.
        ``Path("~/.claude/.credentials.json").expanduser()``.
    :returns: ``True`` when the file carries a usable (present + renewable or
        unexpired) subscription login; ``False`` when missing, unreadable, not
        valid JSON, not an object, or carrying no usable credential.
    """
    try:
        raw = creds_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    access_token = oauth.get("accessToken")
    if not (isinstance(access_token, str) and access_token.strip()):
        return False
    # Renewable: a refresh token means the CLI can mint a fresh access token
    # even if the current one has expired.
    refresh_token = oauth.get("refreshToken")
    if isinstance(refresh_token, str) and refresh_token.strip():
        return True
    # No refresh token — the access token must still be unexpired to be usable.
    expires_at = oauth.get("expiresAt")  # epoch milliseconds
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return expires_at > time.time() * 1000
    return False


def claude_managed_gateway(
    paths: tuple[Path, ...] | None = None,
) -> tuple[str | None, bool]:
    """Read the credential Claude Code applies from its managed settings chain.

    The single canonical parser for Claude Code's managed-settings credential,
    shared by ambient detection and the readiness gate. A credential counts as
    delivered when the file carries a top-level ``apiKeyHelper`` (a
    token-printing command) or a truthy ``env.CLAUDE_CODE_USE_GATEWAY``.

    The **first readable, object-shaped** file decides, matching Claude Code's
    own precedence: a settings file that exists but pins no credential reports
    "none" rather than falling through to a lower-precedence file it would
    itself override.

    :param paths: Settings files to read, highest precedence first; defaults to
        :data:`CLAUDE_CODE_MANAGED_SETTINGS_PATHS`.
    :returns: ``(base_url, has_credential)`` from the first readable file, or
        ``(None, False)`` when none is present or parseable. ``base_url`` is the
        gateway ``env.ANTHROPIC_BASE_URL`` (``None`` when unset — a helper alone
        credentials api.anthropic.com); ``has_credential`` is whether Claude
        Code has a usable credential to apply at launch.
    """
    for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS if paths is None else paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_env = payload.get("env")
        env = raw_env if isinstance(raw_env, dict) else {}
        raw_base_url = env.get("ANTHROPIC_BASE_URL")
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) else None
        has_helper = bool(payload.get("apiKeyHelper"))
        use_gateway = str(env.get("CLAUDE_CODE_USE_GATEWAY", "")).strip().lower() not in (
            "",
            "0",
            "false",
        )
        return base_url or None, has_helper or use_gateway
    return None, False


def claude_managed_model_picker(
    paths: tuple[Path, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Read a replacement model picker from Claude Code's managed settings."""
    for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS if paths is None else paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        picker = payload.get("modelPicker")
        if not isinstance(picker, dict) or picker.get("replaceBuiltInOptions") is not True:
            return ()
        options = picker.get("options")
        if not isinstance(options, list):
            return ()
        return tuple(
            (model.strip(), label.strip())
            for option in options
            if isinstance(option, dict)
            and isinstance((model := option.get("model")), str)
            and model.strip()
            and isinstance((label := option.get("label", model)), str)
            and label.strip()
        )
    return ()


def claude_managed_gateway_display_name(paths: tuple[Path, ...] | None = None) -> str | None:
    """A human label for the managed-settings credential, when one is delivered.

    Used by the setup / ``/model`` display layer to show the Claude credential
    as its actual backing (e.g. ``"gateway.example.com"``) rather than the
    generic ``"Subscription"``. Purely a display derivation from live managed
    settings — nothing is persisted.

    :param paths: Settings files to read; defaults to
        :data:`CLAUDE_CODE_MANAGED_SETTINGS_PATHS`.
    :returns: The gateway host, ``"Claude Code gateway"`` for a
        credential with no pinned base URL, or ``None`` when no credential is
        delivered.
    """
    base_url, has_credential = claude_managed_gateway(paths)
    if not has_credential:
        return None
    if base_url is None:
        return "Claude Code gateway"
    return urlsplit(base_url).hostname or "Claude Code gateway"


def _claude_login_detected() -> bool:
    """Return whether a usable Claude Code subscription login is present.

    File-first, with a macOS Keychain fallback. Claude Code stores its OAuth
    credential in ``~/.claude/.credentials.json`` on Linux but in the **macOS
    Keychain** on macOS — so the fast, no-subprocess file check
    (:func:`claude_auth_has_credential`) is accurate on Linux yet silently
    misses a real, working subscription on a Mac, where that file does not
    exist. That gap is why ``configure harnesses`` failed to auto-detect a
    Claude subscription that the same machine could sign in to without a web
    login (the CLI had already cached the credential in the Keychain).

    To close the gap without slowing the common path, this checks the file
    first and, only on macOS and only when the file check comes up empty, falls
    back to the authoritative CLI status check
    (:func:`omnigent.onboarding.harness_install.harness_cli_logged_in`, which
    runs ``claude auth status`` — the command that reads wherever the CLI
    actually stored the credential, Keychain included). The fallback costs one
    subprocess and runs only on macOS when the file is absent (the normal macOS
    case), so Linux detection stays purely file-based and subprocess-free.

    The fallback is a no-op when the ``claude`` binary is not on ``PATH``
    (``harness_cli_logged_in`` returns ``False`` there), so a Keychain
    credential is detected only when the CLI that wrote it is still installed.

    :returns: ``True`` when a usable Claude subscription login is present — via
        the credentials file on any platform, or via the macOS Keychain through
        the CLI status fallback; ``False`` otherwise.
    """
    if claude_auth_has_credential(_claude_credentials_path()):
        return True
    if sys.platform == "darwin":
        # macOS stores the Claude OAuth token in the Keychain, not the file
        # checked above. Ask the CLI itself (``claude auth status`` reads the
        # Keychain) rather than reimplement Keychain access here. Lazy import
        # keeps this module's import graph light and pays nothing off-macOS.
        from omnigent.onboarding.harness_install import harness_cli_logged_in

        return harness_cli_logged_in(ANTHROPIC_FAMILY)
    return False


def _ollama_reachable() -> bool:
    """Return whether a local Ollama server accepts TCP connections.

    Performs a single short-timeout connect to ``localhost:11434``. Isolated
    in its own helper so tests can monkeypatch it without real network I/O.

    :returns: ``True`` when ``localhost:11434`` accepts a TCP connection,
        ``False`` on refusal, timeout, or any socket error.
    """
    try:
        with socket.create_connection((_OLLAMA_HOST, _OLLAMA_PORT), timeout=_OLLAMA_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


# One-shot prewarmed detection result, produced by
# :func:`prewarm_detect_providers` and consumed by the next
# :func:`detect_providers` call. Guarded by ``_prewarm_lock``.
_prewarm_lock = threading.Lock()
_prewarmed_detection: concurrent.futures.Future[list[DetectedProvider]] | None = None


def prewarm_detect_providers() -> None:
    """Start :func:`detect_providers` on a background thread.

    The next ``detect_providers()`` call consumes the result instead of
    re-running the sweep — on macOS the Claude Keychain fallback shells out
    to ``claude auth status`` (~0.6-0.9s), which a claude-native runner
    would otherwise pay inside terminal creation while the user watches the
    "Starting up…" spinner.

    One-shot by design: the result is a point-in-time snapshot, so only the
    first detection after the prewarm uses it and later calls run fresh.
    Call it only from short-lived processes (the runner) where the prewarm
    and its consumer are seconds apart. No-op when a prewarm is already
    pending.
    """
    global _prewarmed_detection
    with _prewarm_lock:
        if _prewarmed_detection is not None:
            return
        future: concurrent.futures.Future[list[DetectedProvider]] = concurrent.futures.Future()
        _prewarmed_detection = future

    def _run() -> None:
        try:
            future.set_result(_detect_providers_now())
        except BaseException as exc:  # the future must always complete
            future.set_exception(exc)

    threading.Thread(target=_run, name="ambient-detect-prewarm", daemon=True).start()


def detect_providers() -> list[DetectedProvider]:
    """Detect credentials already present on the machine.

    Consumes (one-shot) a pending :func:`prewarm_detect_providers` result
    when one exists, waiting for it to finish if needed; otherwise runs the
    sweep inline.

    Checks, in a stable priority order:

    1. Vendor API keys in the environment, via
       :data:`omnigent.onboarding.providers.PROVIDER_ENV_VARS`. Only
       variables that are set and non-empty are reported, in the order they
       appear in ``PROVIDER_ENV_VARS``.
    2. A logged-in Claude CLI — ``~/.claude/.credentials.json`` carries a
       usable login (see :func:`claude_auth_has_credential`), or, on macOS,
       the credential lives in the Keychain and ``claude auth status`` reports
       a login (see :func:`_claude_login_detected`).
    3. A reachable local Ollama (``localhost:11434`` TCP-connectable).

    No network I/O is performed except the single Ollama probe (see
    :func:`_ollama_reachable`). On macOS, a ``claude auth status`` subprocess
    may run as the Claude Keychain fallback (see :func:`_claude_login_detected`).

    :returns: One :class:`DetectedProvider` per credential found, in the
        priority order above. Empty when nothing is detected.
    """
    global _prewarmed_detection
    with _prewarm_lock:
        prewarmed = _prewarmed_detection
        _prewarmed_detection = None
    if prewarmed is not None:
        try:
            return prewarmed.result()
        except Exception:
            # A failed speculative sweep must never make detection worse —
            # fall through to a fresh one.
            pass
    return _detect_providers_now()


def _detect_providers_now() -> list[DetectedProvider]:
    """Run the ambient-credential sweep (see :func:`detect_providers`)."""
    detected: list[DetectedProvider] = []

    # 1. Environment API keys.
    for provider, env_var in PROVIDER_ENV_VARS.items():
        # Only surface providers we can map to a family decision; other
        # PROVIDER_ENV_VARS entries (mistral, groq, ...) are not part of
        # the model-selection surface yet.
        if provider not in _ENV_KEY_FAMILY:
            continue
        resolved = getenv_nonempty_with_omnigent_prefix(env_var)
        if resolved is None:
            continue
        actual_env_var, _value = resolved
        detected.append(
            DetectedProvider(
                name=provider,
                kind=KEY_KIND,
                family=_ENV_KEY_FAMILY[provider],
                source=f"${actual_env_var}",
            )
        )

    # 1b. Claude Code on Vertex AI — the CLI reads three env vars for GCP auth.
    # Detected separately from plain API keys because Vertex uses GCP ADC
    # (no Anthropic key), so none of the PROVIDER_ENV_VARS entries cover it.
    _vertex_truthy = ("1", "true", "yes")
    if (
        os.environ.get("CLAUDE_CODE_USE_VERTEX", "").strip().lower() in _vertex_truthy
        and os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
        and os.environ.get("CLOUD_ML_REGION", "").strip()
    ):
        detected.append(
            DetectedProvider(
                name="vertex-claude",
                kind=KEY_KIND,
                family=ANTHROPIC_FAMILY,
                source="$CLAUDE_CODE_USE_VERTEX",
            )
        )

    # 2. Claude Code credential. Prefer the managed-settings gateway — read
    #    straight from the enterprise settings file (no subprocess, works on
    #    Linux where there is no Keychain, and it is what Claude Code actually
    #    applies at launch). Fall back to a CLI login otherwise (which on macOS
    #    reads the Keychain via ``claude auth status``). Either way it is
    #    surfaced as a ``subscription``: the credential lives in Claude Code's
    #    own files and Claude Code applies it itself, so omnigent must NOT adopt
    #    a gateway provider shape for it — the in-process SDK falls back to its
    #    own auth.
    if claude_managed_gateway()[1]:
        detected.append(
            DetectedProvider(
                name="claude",
                kind=SUBSCRIPTION_KIND,
                family=ANTHROPIC_FAMILY,
                source="Claude Code managed settings",
            )
        )
    elif _claude_login_detected():
        detected.append(
            DetectedProvider(
                name="claude",
                kind=SUBSCRIPTION_KIND,
                family=ANTHROPIC_FAMILY,
                source="claude CLI login",
            )
        )

    # 3. Local Ollama.
    if _ollama_reachable():
        detected.append(
            DetectedProvider(
                name="ollama",
                kind=LOCAL_KIND,
                family=OPENAI_FAMILY,
                source=_OLLAMA_URL,
            )
        )

    return detected
