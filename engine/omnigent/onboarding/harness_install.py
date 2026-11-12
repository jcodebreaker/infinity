"""Harness CLI install + auth operations for the host.

A coding harness is "ready" along two independent axes:

- **configured** — a usable model credential serves its family (resolved via
  :func:`omnigent.onboarding.provider_config.default_provider_for_harness`
  over the ambient-merged config). That lives in the provider layer.
- **installed** — the harness's CLI binary is on ``PATH``. This module owns
  that axis (``shutil.which(binary)``) and the packages it installs.

This module also owns the per-harness **CLI binary name**, so it is the natural
home for driving each harness's own *subscription login/logout* commands
(:func:`harness_login` / :func:`harness_logout`).

The "is the CLI logged in?" verdict (:func:`harness_cli_logged_in`) asks the
CLI itself (``claude auth status``) rather than
reading a credential file, because the file location is **platform-specific**
— Claude Code stores its OAuth tokens in the macOS Keychain (not
``~/.claude/.credentials.json``) on macOS, so a file check would falsely report
"not logged in" right after a successful ``claude auth login``. The CLI's own
status command reads wherever it actually stored the credential, so login
verification is correct on every platform. (Ambient detection in
:mod:`omnigent.onboarding.ambient` is file-based and subprocess-free on
Linux; on macOS it reuses :func:`harness_cli_logged_in` as a Keychain fallback
when the credentials file is absent — see ``ambient._claude_login_detected``.)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from packaging.version import InvalidVersion, Version

from omnigent._platform import resolve_cli_binary
from omnigent.harness_install_spec import HarnessInstallSpec, SetupStep
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY

# Pi is keyed by its own name rather than a model family (it consumes both).
PI_KEY = "pi"

# Minimum CLI versions for native harnesses where the runtime has a known
# feature floor. These are intentionally conservative: the runtime may
# gracefully degrade on older CLIs, but setup enforces the floor so a user
# isn't surprised by missing behaviour (e.g. policy hooks, non-interactive
# approval) after launching.
# Sources:
# - pi: non-interactive ``--approve`` override requires >= 0.79.0. Adaptive
#   thinking (thinking.type.adaptive for claude-4+/claude-5) requires >= 0.84.2
#   (``omnigent/harnesses/pi_native/credentials.py``).
# - claude: `--mcp-config` (required by the native bridge) introduced long
#   before 2026-06-01. The first Claude Code release after the cutoff is
#   2.1.161, so use that as the supported floor.
_PI_MIN_VERSION = "0.84.2"
_CLAUDE_MIN_VERSION = "2.1.161"

# Anthropic recommends its native installer over ``npm install -g``: it writes
# to a user-writable ``~/.local/bin`` and self-updates, so it sidesteps the
# EACCES failure on a root-owned npm global prefix.
# See https://code.claude.com/docs/en/setup#native-install-recommended
_CLAUDE_INSTALL_HINT = "curl -fsSL https://claude.ai/install.sh | bash"


# Keyed by harness family (Claude=anthropic) plus pi. Login/logout argv use the
# CLI's first-class auth subcommands (``claude auth login --claudeai``), so the
# user can sign in to a subscription from the host directly.
_HARNESS_INSTALL: dict[str, HarnessInstallSpec] = {
    # Claude ships a vendor installer, so it carries an ``install_hint`` +
    # ``install_command`` and no ``package``. ``package is None`` is what
    # routes :func:`harness_setup_hint` and the runner's missing-CLI error to
    # the installer instead of the npm command.
    ANTHROPIC_FAMILY: HarnessInstallSpec(
        "Claude",
        "claude",
        package=None,
        login_args=("auth", "login", "--claudeai"),
        logout_args=("auth", "logout"),
        status_args=("auth", "status"),
        login_status_key="loggedIn",
        install_hint=_CLAUDE_INSTALL_HINT,
        install_command=("bash", "-c", _CLAUDE_INSTALL_HINT),
        min_version=_CLAUDE_MIN_VERSION,
    ),
    PI_KEY: HarnessInstallSpec(
        "Pi",
        "pi",
        "@earendil-works/pi-coding-agent",
        # The ``--approve`` / non-interactive trust override requires
        # ``pi >= 0.79.0``; older CLIs would prompt mid-session.
        min_version=_PI_MIN_VERSION,
    ),
}


# Maps an executor *harness identifier* (the value the runtime resolves from a
# spec's ``executor.config["harness"]`` / ``executor.type``) to its
# :data:`_HARNESS_INSTALL` family key. Only the CLI-backed harnesses appear
# here — the ones that cannot launch without a binary on ``PATH``:
# ``claude-native`` wraps the ``claude`` CLI, and ``pi`` / ``pi-native`` the
# ``pi`` CLI. The in-process
# ``claude-sdk`` harness is deliberately absent, so it resolves to "no CLI
# required".
_HARNESS_NAME_TO_KEY: dict[str, str] = {
    "claude-native": ANTHROPIC_FAMILY,
    PI_KEY: PI_KEY,
    "pi-native": PI_KEY,
}


# UI-installable harnesses: the identifiers the web UI's New Chat dialog may
# request an install for, mapped to their :data:`_HARNESS_INSTALL` key. Single
# source of truth for both the host install handler (which runs the installer)
# and the server route (which allowlists the request).
_UI_INSTALLABLE_HARNESS_TO_KEY: dict[str, str] = {
    "claude": ANTHROPIC_FAMILY,
    PI_KEY: PI_KEY,
}


# Family keys the UI may install, derived once from the allowlist so the
# executor-spelling fallback in ``ui_install_key`` can't admit a family a
# plugin maps into the name map without making it UI-installable.
_UI_INSTALLABLE_KEYS: frozenset[str] = frozenset(_UI_INSTALLABLE_HARNESS_TO_KEY.values())


def ui_install_key(harness: str) -> str | None:
    """Resolve a harness identifier to its UI-installable install-spec key.

    Accepts both the bare install ids (``"claude"``, ``"pi"``) and the
    executor spellings a session actually carries — the native TUI wrappers
    (``"claude-native"``, ``"pi-native"``) resolve
    through the shared :data:`_HARNESS_NAME_TO_KEY` map to the same family key.
    Any harness that doesn't map onto the UI-installable family set (SDK
    harnesses like ``"claude-sdk"``) returns ``None`` so the caller rejects it.

    :param harness: A harness identifier from the web UI, e.g. ``"claude"`` or
        ``"pi-native"``.
    :returns: The :data:`_HARNESS_INSTALL` key (e.g. ``"anthropic"``) when the
        harness is UI-installable; ``None`` otherwise (caller rejects it).
    """
    direct = _UI_INSTALLABLE_HARNESS_TO_KEY.get(harness)
    if direct is not None:
        return direct
    # Fall back to the executor-spelling map, but only accept keys that are
    # themselves UI-installable.
    key = _all_harness_name_to_key().get(harness)
    if key is not None and key in _UI_INSTALLABLE_KEYS:
        return key
    return None


def ui_installable_harnesses() -> frozenset[str]:
    """Return every harness identifier the web UI may install.

    Includes the bare install ids and all executor spellings that resolve to a
    UI-installable family (e.g. ``"pi-native"``), so the
    New Chat dialog can offer setup for the harness a session actually declares
    — not just the bare ids.

    :returns: The full set of accepted harness identifiers, e.g.
        ``{"claude", "claude-native", "pi", ...}``.
    """
    resolvable = set(_UI_INSTALLABLE_HARNESS_TO_KEY)
    for name, mapped in _all_harness_name_to_key().items():
        if mapped in _UI_INSTALLABLE_KEYS:
            resolvable.add(name)
    return frozenset(resolvable)


def ui_credential_configurable_harnesses() -> frozenset[str]:
    """Return every harness identifier the web UI may write a credential for.

    Every UI-installable family (Claude / Pi) consumes a provider
    credential omnigent owns, so the host store-secret handler can configure
    each of them; the credential route accepts the same set as the install
    route.

    :returns: The set of harness identifiers accepted by the credential route,
        e.g. ``{"claude", "claude-native", "pi", ...}``.
    """
    return ui_installable_harnesses()


# The auth step per UI-installable family, for the setup checklist. These are
# display-only checklist rows (the command is shown for the user to run on the
# host, never executed server-side), so the commands are literal here rather
# than derived from ``HarnessInstallSpec.login_args`` — keep them in sync with
# that spec by hand if a harness's login command changes. Every row is an
# ``auth`` step: it opens the UI credential form and is status-tracked.
#   claude: subscription login via the CLI's own login command, an API key,
#     or a gateway.
#   pi: a provider credential (API key / gateway / adopt) written from the UI.
_UI_AUTH_STEP_BY_KEY: dict[str, SetupStep] = {
    ANTHROPIC_FAMILY: SetupStep(
        kind="auth",
        # UI-authable: the dialog opens a form listing every way to authenticate
        # (subscription login, API key, gateway, or adopting a detected key), so
        # the row is a neutral "set up auth" rather than naming just one path —
        # the subscription ``command`` below is one option inside that form, not
        # the whole step.
        title="Set up authentication",
        detail="Sign in with your Claude subscription, an API key, or a gateway.",
        action="auth",
        command="claude auth login --claudeai",
        status_key="authed",
    ),
    PI_KEY: SetupStep(
        kind="auth",
        # Same neutral "set up auth" framing as claude — the dialog opens a
        # form listing the ways to authenticate. Pi has NO subscription CLI login
        # (``command=None``, so the form omits that option), so the detail names
        # only the applicable paths. ``status_key="authed"`` keeps the step
        # trackable so it isn't dropped as "unknown" (which would wrongly read
        # "ready").
        title="Set up authentication",
        detail="Add an API key or a gateway so Pi can run.",
        action="auth",
        command=None,
        status_key="authed",
    ),
}


def ui_setup_steps(harness: str) -> list[SetupStep]:
    """Return the ordered setup checklist for a UI harness identifier.

    An install step, then (for the UI-installable families) an auth step. The
    install step's label uses the harness's :class:`HarnessInstallSpec` display
    name; the auth step's command is a display-only literal from
    :data:`_UI_AUTH_STEP_BY_KEY` (shown for the user to run, not executed).
    Harnesses outside the UI-installable set get a single generic
    "set up on the host" step.

    :param harness: A harness identifier the UI holds, e.g. ``"pi"`` or the
        native spelling ``"pi-native"`` (both resolve to the same steps).
    :returns: Ordered :class:`SetupStep` list; never empty.
    """
    key = ui_install_key(harness)
    if key is None:
        # Not UI-installable (curl/OAuth/SDK harness): one generic step.
        return [
            SetupStep(
                kind="install",
                title="Set up on the host",
                detail="Install and configure this agent's harness on the host.",
                action="setup",
                command=None,
                status_key=None,
            )
        ]

    spec = _all_harness_install().get(key)
    display = spec.display if spec is not None else harness
    steps = [
        SetupStep(
            kind="install",
            title=f"Install {display}",
            detail=f"We'll install {display} on the host for you.",
            action="install",
            command=None,
            status_key="installed",
        )
    ]
    auth = _UI_AUTH_STEP_BY_KEY.get(key)
    if auth is not None:
        steps.append(auth)
    return steps


def _all_harness_install() -> dict[str, HarnessInstallSpec]:
    from omnigent.harness_plugins import install_specs

    merged = dict(_HARNESS_INSTALL)
    merged.update(install_specs())
    return merged


def _all_harness_name_to_key() -> dict[str, str]:
    from omnigent.harness_plugins import harness_install_keys

    merged = dict(_HARNESS_NAME_TO_KEY)
    merged.update(harness_install_keys())
    return merged


def required_cli_for_harness(harness: str) -> HarnessInstallSpec | None:
    """Return the CLI a harness needs on ``PATH`` to launch, or ``None``.

    :param harness: An executor harness identifier, e.g. ``"pi"``,
        ``"claude-native"``, or an SDK harness like
        ``"claude-sdk"``.
    :returns: The :class:`HarnessInstallSpec` whose ``binary`` must be on
        ``PATH`` for *harness* to start; ``None`` for SDK-based / unknown
        harnesses that need no CLI binary.
    """
    key = _all_harness_name_to_key().get(harness)
    return _all_harness_install().get(key) if key is not None else None


def missing_harness_cli(harness: str) -> HarnessInstallSpec | None:
    """Return a harness's required CLI spec when that CLI can't be used.

    Combines :func:`required_cli_for_harness` with the same probe
    :func:`harness_cli_installed` uses, so the verdict matches what the
    harness's own launch will see (both check ``PATH`` plus the common global
    install dirs the host daemon's frozen ``PATH`` may omit, and now also the
    declared version range). Used by sub-agent dispatch to fail loud *before*
    spawning a worker whose harness can never boot here, instead of letting
    the missing or incompatible binary surface as a lazy, generic turn failure.

    :param harness: An executor harness identifier, e.g. ``"pi"`` or
        ``"claude-native"``.
    :returns: The :class:`HarnessInstallSpec` for a CLI-backed harness whose
        ``binary`` is not on ``PATH`` or is outside its supported version range;
        ``None`` when the harness needs no CLI (SDK-based / unknown) or the
        required binary is present and version-compatible.
    """
    spec = required_cli_for_harness(harness)
    if spec is None:
        return None
    install_key = _all_harness_name_to_key().get(harness)
    if install_key is not None and harness_cli_installed(install_key):
        return None
    return spec


def harness_setup_hint(harness: str | None) -> str:
    """Return actionable remediation when *harness* can't launch on a machine.

    npm-installed CLI harnesses (``pi``) get a generic install hint. A harness
    whose CLI
    ships via a vendor installer (``claude``, via Anthropic's curl installer —
    it carries an ``install_hint`` and no ``package``) names that installer and
    the CLI's own login instead.

    :param harness: An executor harness identifier, e.g. ``"claude-native"``
        or ``"pi"``; ``None`` falls back to the generic hint.
    :returns: A remediation clause for the "harness not configured" message,
        e.g. ``"install the claude CLI on that machine with `curl -fsSL
        https://claude.ai/install.sh | bash`, then run `claude auth login
        --claudeai`"`` for Claude, or a generic install hint otherwise.
    """
    spec = required_cli_for_harness(harness or "")
    if spec is not None and spec.package is None and spec.install_hint:
        login = ""
        if spec.login_args:
            login = f", then run `{spec.binary} {' '.join(spec.login_args)}`"
        return f"install the {spec.binary} CLI on that machine with `{spec.install_hint}`{login}"
    return "install the harness on that machine and set a default credential"


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*)")


def _parse_harness_cli_version(text: str) -> str | None:
    """Extract a semver-ish string from ``<binary> --version`` output.

    Generic so any harness can declare a version range in its install spec.
    """
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    return match.group(1)


# Wall-clock cap on a harness CLI probe subprocess (``--version`` /
# ``auth status``). The default stays lenient for setup and launch gating; the
# throttled readiness refresh passes ``READINESS_CLI_PROBE_TIMEOUT_S`` so a hung
# CLI can't stall the refresh — and, through it, the host tunnel's keepalive.
# The readiness cap is enough for a healthy ``auth status`` keychain read / token refresh, short
# enough that a wedged CLI fails fast.
_DEFAULT_CLI_PROBE_TIMEOUT_S = 30.0
READINESS_CLI_PROBE_TIMEOUT_S = 10.0

# Probe caches damping the ambient readiness storm: every readiness refresh
# on every host daemon execs vendor CLIs (``--version`` / ``auth status``)
# whose answers change only when the binary is swapped or a login flips —
# with a few dozen idle hosts that compounds into a constant machine-wide
# subprocess churn. ``--version`` output is a pure function of the binary
# bytes, so successful parses are cached against the binary's
# ``(path, mtime_ns, size)`` signature; login state can flip without the
# binary changing, so only positive verdicts are cached and they expire
# after a short TTL — a cached "not logged in" would blind the setup
# wizard's confirmation right after a successful login.
_VERSION_PROBE_CACHE: dict[tuple[str, int, int], str] = {}
_LOGIN_PROBE_CACHE: dict[tuple[str, str], float] = {}
_PROBE_CACHE_LOCK = threading.Lock()
_LOGIN_PROBE_CACHE_TTL_S = 120.0
_PROBE_CACHE_MAX_ENTRIES = 64


def _binary_signature(binary: str) -> tuple[str, int, int] | None:
    """Return *binary*'s identity signature for the version cache.

    :param binary: Absolute path of the CLI binary, e.g. ``"/usr/bin/claude"``.
    :returns: ``(path, mtime_ns, size)``, or ``None`` when it cannot be
        stat'ed (caller probes uncached).
    """
    try:
        stat = os.stat(binary)
    except OSError:
        return None
    return (binary, stat.st_mtime_ns, stat.st_size)


def _harness_cli_version_satisfies(
    spec: HarnessInstallSpec,
    binary: str,
    timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S,
) -> bool:
    """Check *binary*'s ``--version`` against *spec*'s declared range.

    A missing/unparseable version or a subprocess error is treated as not
    satisfying the range, so an installed but incompatible CLI is reported
    as not ready and the setup flow prompts for an upgrade before the
    runtime gate rejects it.

    :param timeout: Seconds to wait for the ``--version`` subprocess before
        giving up, e.g. ``10.0`` on the readiness path.
    """
    if spec.min_version is None and spec.max_version_exclusive is None:
        return True
    version = _harness_cli_version_string(spec, binary, timeout)
    if version is None:
        return False
    try:
        parsed = Version(version)
    except InvalidVersion:
        # Fall back to the leading X.Y.Z segment for pre-release version
        # strings that packaging rejects (e.g. ``0.146.0-alpha.9.2``).
        # This avoids mis-classifying a newer pre-release as too old.
        m = re.match(r"^(\d+\.\d+\.\d+)", version)
        if not m:
            return False
        try:
            parsed = Version(m.group(1))
        except InvalidVersion:
            return False
    if spec.min_version is not None:
        try:
            if parsed < Version(spec.min_version):
                return False
        except InvalidVersion:
            return False
    if spec.max_version_exclusive is not None:
        try:
            if parsed >= Version(spec.max_version_exclusive):
                return False
        except InvalidVersion:
            return False
    return True


def harness_install_spec(key: str) -> HarnessInstallSpec | None:
    """Return the install spec for a family/harness key, or ``None``.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY` (``"pi"``).
    :returns: The :class:`HarnessInstallSpec`, or ``None`` for an unknown key
        (e.g. a gateway-only family with no dedicated CLI).
    """
    return _all_harness_install().get(key)


def harness_cli_version_satisfies(key: str) -> bool:
    """Return whether the installed CLI for *key* satisfies its version range.

    Only harnesses that declare ``min_version`` / ``max_version_exclusive`` in
    their install spec are probed. A missing binary, an unparsable version, or
    a subprocess error is treated as not satisfying the range — the setup flow
    will then prompt for an upgrade before the runtime gate rejects it.

    :param key: A harness family key, e.g. :data:`PI_KEY`.
    :returns: ``True`` when the binary is present and its version falls inside
        the declared range, or when the spec has no version bounds.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return False
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return False
    return _harness_cli_version_satisfies(spec, binary)


def harness_cli_installed(key: str, timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S) -> bool:
    """Return whether the harness's CLI is present and meets its version range.

    "Installed" now means the CLI binary (:func:`resolve_cli_binary`) is
    resolvable **and**, when the harness declares ``min_version`` /
    ``max_version_exclusive`` in its install spec, the binary's
    ``--version`` output satisfies that range. This prevents an outdated
    native CLI (e.g. a Pi release below its approval-override floor)
    from being treated as ready during setup.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY`.
    :param timeout: Seconds to wait for the ``--version`` probe subprocess,
        e.g. ``10.0`` on the readiness path where a hung CLI must not stall
        the refresh.
    :returns: ``True`` when the CLI resolves and is version-compatible;
        ``False`` when it doesn't resolve, the key has no associated CLI,
        or its version falls outside the declared range.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return False
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return False
    return _harness_cli_version_satisfies(spec, binary, timeout)


def harness_cli_version(key: str) -> tuple[str | None, str | None]:
    """Return the installed CLI's version string plus the declared range.

    Useful for human-readable status messages when the CLI is present but
    outside its supported range, so the UI can say "installed vX, required >=Y"
    instead of just "not installed".

    :param key: A harness family key, e.g. :data:`PI_KEY`.
    :returns: ``(version, range_str)``. ``version`` is ``None`` when the binary
        is missing or its ``--version`` output is unparseable. ``range_str``
        is a human-readable summary of the declared ``min_version`` /
        ``max_version_exclusive`` range, or ``None`` when the spec has no
        version bounds.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return None, None
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return None, None
    version = _harness_cli_version_string(spec, binary)
    if version is None:
        return None, _version_range_str(spec)
    return version, _version_range_str(spec)


def _version_range_str(spec: HarnessInstallSpec) -> str | None:
    """Human-readable rendering of a spec's version range, or ``None``."""
    if spec.min_version is None and spec.max_version_exclusive is None:
        return None
    if spec.min_version is not None and spec.max_version_exclusive is None:
        return f">={spec.min_version}"
    if spec.min_version is None and spec.max_version_exclusive is not None:
        return f"<{spec.max_version_exclusive}"
    return f">={spec.min_version}, <{spec.max_version_exclusive}"


def _harness_cli_version_string(
    spec: HarnessInstallSpec,
    binary: str,
    timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S,
) -> str | None:
    """Return the parsed, normalized version string from *binary* ``--version``.

    Successful parses are cached against the binary's ``(path, mtime_ns,
    size)`` signature — the output cannot change until the binary does.
    Failed probes (missing/hung binary, unparseable output) are never
    cached, so a flaky state keeps re-probing like before.

    :param timeout: Seconds to wait for the ``--version`` subprocess, e.g.
        :data:`READINESS_CLI_PROBE_TIMEOUT_S` on the readiness path.
    """
    sig = _binary_signature(binary)
    if sig is not None:
        with _PROBE_CACHE_LOCK:
            cached = _VERSION_PROBE_CACHE.get(sig)
        if cached is not None:
            return cached
    try:
        completed = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = _parse_harness_cli_version(
        (completed.stdout or "") + "\n" + (completed.stderr or "")
    )
    if version is not None and sig is not None:
        with _PROBE_CACHE_LOCK:
            if len(_VERSION_PROBE_CACHE) >= _PROBE_CACHE_MAX_ENTRIES:
                _VERSION_PROBE_CACHE.clear()
            _VERSION_PROBE_CACHE[sig] = version
    return version


def harness_install_command(key: str) -> list[str]:
    """Return the argv that installs the harness CLI.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: The install command, e.g. ``["npm", "install", "-g",
        "@earendil-works/pi-coding-agent"]`` or an explicitly configured vendor installer command,
        wrapped as ``["bash", "-c", <script>]``. For the form to show a user,
        see :func:`harness_install_display`.
    :raises KeyError: If *key* has no install spec (caller should gate on
        :func:`harness_install_spec`).
    :raises ValueError: If *key* has a spec but no npm ``package`` (a CLI
        installed out-of-band by a vendor installer); show its ``install_hint``.
    """
    spec = harness_install_spec(key)
    if spec is None:
        raise KeyError(key)
    if spec.install_command is not None:
        return list(spec.install_command)
    package = spec.package
    if package is None:
        raise ValueError(f"{key!r} has no npm package; show its install_hint instead")
    return ["npm", "install", "-g", package]


def harness_install_display(key: str) -> str:
    """Return the install command in the form to show a user.

    A vendor installer publishes the runnable one-liner in ``install_hint``,
    while :func:`harness_install_command` wraps it as ``bash -c <script>`` for
    ``subprocess``; showing that joined argv would print the wrapper for the
    user to strip by hand. npm harnesses render from the argv as before.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: e.g. ``"curl -fsSL https://claude.ai/install.sh | bash"``.
    :raises KeyError: If *key* has no install spec.
    :raises ValueError: If *key* has neither an ``install_hint`` nor a
        ``package``.
    """
    spec = harness_install_spec(key)
    if spec is not None and spec.install_hint:
        return spec.install_hint
    return " ".join(harness_install_command(key))


class HarnessInstallResult(NamedTuple):
    """Outcome of :func:`try_install_harness_cli`.

    :param installed: Whether the CLI resolves after the attempt, via the same
        :func:`resolve_cli_binary` ladder readiness uses (``PATH`` plus the
        common global install dirs), not bare ``PATH`` alone.
    :param reason: Human-readable failure reason when ``installed`` is False;
        ``None`` on success.
    """

    installed: bool
    reason: str | None


def try_install_harness_cli(key: str) -> HarnessInstallResult:
    """Install the harness CLI, returning whether it landed and why not.

    Same behavior and side effects as :func:`install_harness_cli` (the
    installer's output streams to this process, uncaptured, so failures stay
    visible in the setup terminal / host log), but returns a human-readable
    reason so a UI-driven install can surface "npm is not available on the
    host" instead of a silent boolean failure.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: A :class:`HarnessInstallResult` — ``(True, None)`` once the CLI
        resolves via :func:`resolve_cli_binary` (including the no-op where it
        was already present), otherwise ``(False, reason)`` naming the failure
        (manual-only spec, missing installer, timeout, OS error, non-zero exit,
        or a post-install binary-not-found).
    :raises KeyError: If *key* has no install spec.
    """
    spec = harness_install_spec(key)
    if spec is not None and spec.package is None and spec.install_command is None:
        # Manual-only CLI (no package, no installer): caller shows install_hint.
        return HarnessInstallResult(False, f"{spec.binary!r} is not installable automatically")
    cmd = harness_install_command(key)
    if shutil.which(cmd[0]) is None:
        return HarnessInstallResult(False, f"{cmd[0]!r} is not available on the host")
    try:
        result = subprocess.run(cmd, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        return HarnessInstallResult(False, "install timed out after 300s")
    except OSError as exc:
        return HarnessInstallResult(False, f"install command failed to run: {exc}")
    # harness_install_command would have raised for a spec-less key, so spec is
    # non-None past this point.
    assert spec is not None
    # Resolve the freshly-installed binary via the SAME ladder readiness uses
    # (:func:`resolve_cli_binary` — ``PATH`` plus the nvm/npm-global/homebrew
    # fallback dirs), so the install verdict and the readiness badge can never
    # disagree. A bare ``shutil.which`` here would report "not found" for a
    # binary the host daemon's frozen ``PATH`` omits but readiness still resolves
    # via the ladder — the spurious "failed" toast next to a green "ready" tick.
    resolved = resolve_cli_binary(spec.binary)
    if resolved is not None:
        # Put the resolving dir on ``PATH`` for this process so the setup
        # wizard's *later* steps — harness_login / harness_cli_logged_in /
        # harness_logout — which shell out with the bare binary name and only
        # bare ``shutil.which``, can find it too. Without this, an install that
        # succeeded via a fallback dir (nvm/homebrew/…) would be followed by a
        # login step that can't locate the very binary just installed.
        resolved_dir = str(Path(resolved).resolve().parent)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if resolved_dir not in path_entries:
            os.environ["PATH"] = os.pathsep.join([resolved_dir, *path_entries])
        return HarnessInstallResult(True, None)
    if result.returncode != 0:
        return HarnessInstallResult(False, f"installer exited with code {result.returncode}")
    return HarnessInstallResult(
        False, f"installer completed but {spec.binary!r} could not be found"
    )


def install_harness_cli(key: str) -> bool:
    """Install the harness CLI; return whether it landed on ``PATH``.

    Thin wrapper over :func:`try_install_harness_cli` that discards the failure
    reason, preserving the boolean contract the setup wizard relies on.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: ``True`` when the CLI is on ``PATH`` after the install attempt,
        ``False`` if the installer is missing or the install failed.
    :raises KeyError: If *key* has no install spec.
    """
    return try_install_harness_cli(key).installed


def harness_cli_logged_in(key: str, timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S) -> bool:
    """Return whether the harness CLI itself reports a usable login.

    Asks the CLI's own status command (``claude auth status``) instead of
    reading a credential
    file, because the file location is platform-specific — Claude Code stores
    its tokens in the macOS Keychain rather than ``~/.claude/.credentials.json``
    on macOS, so a file check would falsely report "not logged in" right after a
    successful ``claude auth login``. The status command reads wherever the CLI
    actually stored the credential, so this is correct on every platform.

    Two output shapes are handled, selected explicitly by the spec's
    ``login_status_key``: a CLI that publishes a JSON status object names its
    boolean field there (Claude ``loggedIn``) and
    is read structurally; a CLI with no ``login_status_key`` has no JSON verdict,
    so the exit code decides
    (``0`` only when logged in) and stdout is never parsed.

    Positive verdicts are cached for :data:`_LOGIN_PROBE_CACHE_TTL_S`
    seconds per ``(key, binary)`` so readiness refreshes don't exec the
    vendor CLI on every pass; negative verdicts are never cached, and
    :func:`harness_logout` invalidates the key's entries.

    :param key: A harness family, e.g. ``"anthropic"`` (Claude).
    :param timeout: Seconds to wait for the status subprocess, e.g. ``10.0`` on
        the readiness path where a hung CLI must not stall the refresh.
    :returns: ``True`` when the CLI reports a usable login; ``False`` when the
        key has no status command, the CLI binary is missing, the status
        process failed to spawn, or the CLI reports no login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.status_args is None:
        return False
    binary = shutil.which(spec.binary)
    if binary is None:
        return False
    # Positive verdicts are TTL-cached (see the cache block above): logins
    # are sticky, and the readiness loop re-probing a logged-in CLI every
    # refresh is the ambient storm. Negatives always re-probe so a login
    # made a moment ago is seen immediately.
    cache_key = (key, binary)
    with _PROBE_CACHE_LOCK:
        if time.monotonic() < _LOGIN_PROBE_CACHE.get(cache_key, 0.0):
            return True
    try:
        result = subprocess.run(
            [spec.binary, *spec.status_args],
            check=False,
            timeout=timeout,
            # Concurrent probes must not change or restore a shared terminal's input mode.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Dispatch is explicit per spec: a harness that publishes a JSON status
    # object names its boolean field in ``login_status_key`` (Claude
    # ``loggedIn``). When that key is unset the
    # harness has no JSON verdict, so the
    # exit code is authoritative and stdout is never parsed — output that merely
    # happens to be JSON can't flip the verdict.
    status_key = spec.login_status_key
    logged_in = result.returncode == 0
    if status_key is not None:
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict) and status_key in payload:
            logged_in = bool(payload[status_key])
    if logged_in:
        with _PROBE_CACHE_LOCK:
            if len(_LOGIN_PROBE_CACHE) >= _PROBE_CACHE_MAX_ENTRIES:
                _LOGIN_PROBE_CACHE.clear()
            _LOGIN_PROBE_CACHE[cache_key] = time.monotonic() + _LOGIN_PROBE_CACHE_TTL_S
    return logged_in


def harness_login(key: str) -> bool:
    """Run the harness CLI's interactive subscription login; return logged-in state.

    Lets ``configure harnesses`` be the single place to sign in: when the user
    picks a subscription sign-in, we drive the harness's own auth entrypoint
    (``claude auth login --claudeai``) **in the foreground**
    (inheriting stdio so OAuth / device-code prompts and browser URLs reach
    the user), then confirm via
    :func:`harness_cli_logged_in`.
    If the CLI is already logged in this is a no-op that returns ``True``
    immediately (no redundant re-auth).

    :param key: A harness family, e.g. ``"anthropic"`` (Claude).
    :returns: ``True`` when the harness CLI is logged in after the attempt
        (including the already-logged-in short-circuit); ``False`` when the key
        has no login command, the CLI binary is missing, the login process
        failed to spawn, or the user did not complete the login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.login_args is None:
        return False
    if shutil.which(spec.binary) is None:
        return False
    if harness_cli_logged_in(key):
        return True
    try:
        # Open /dev/tty explicitly so the child process sees a real TTY even
        # when the parent's stdio is piped (e.g. launched via `uv tool run` or
        # another wrapper). The Claude CLI checks isatty() and skips opening the
        # browser when it returns false, which strands the login until it times
        # out. Fall back to inherited stdio when /dev/tty can't be opened (a
        # headless run with no controlling terminal).
        tty_fd: int | None = None
        if not sys.stdin.isatty():
            try:
                tty_fd = os.open("/dev/tty", os.O_RDWR)
            except OSError:
                tty_fd = None
        argv = [spec.binary, *spec.login_args]
        try:
            if tty_fd is not None:
                subprocess.run(
                    argv, check=False, timeout=600, stdin=tty_fd, stdout=tty_fd, stderr=tty_fd
                )
            else:
                subprocess.run(argv, check=False, timeout=600)
        finally:
            if tty_fd is not None:
                os.close(tty_fd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return harness_cli_logged_in(key)


def harness_logout(key: str) -> bool:
    """Run the harness CLI's logout; return whether it is now logged out.

    Drives the harness's own logout command (``claude auth logout``) so
    removing a subscription actually signs the user out of the standalone CLI — otherwise the
    credential persists and ambient detection re-adopts the subscription on the
    next ``configure`` open.

    :param key: A harness family, e.g. ``"anthropic"`` (Claude).
    :returns: ``True`` when the harness CLI is logged out after the attempt;
        ``False`` when the key has no logout command, the binary is missing, the
        process failed to spawn, or a login still resolves afterward.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.logout_args is None:
        return False
    if shutil.which(spec.binary) is None:
        return False
    try:
        subprocess.run([spec.binary, *spec.logout_args], check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Drop cached logged-in verdicts so the confirmation below re-probes —
    # a cached positive would report this successful logout as failed.
    with _PROBE_CACHE_LOCK:
        for cache_key in [k for k in _LOGIN_PROBE_CACHE if k[0] == key]:
            del _LOGIN_PROBE_CACHE[cache_key]
    return not harness_cli_logged_in(key)
