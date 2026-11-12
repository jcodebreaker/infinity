"""Harness readiness checks used by the host daemon.

The daemon reports a per-harness readiness map in its hello frame, refreshes
it while connected (so the web agent picker can warn accurately), and
re-checks the session's harness before spawning a runner (so an unconfigured
launch fails clearly instead of dying inside the executor).

"Configured" here is deliberately narrow: the **only** thing the daemon
can reliably determine locally is whether a harness's wrapped CLI binary
is on ``PATH``. That gates the native CLI harnesses (Claude Code / Codex
via ``claude`` / ``codex``) and ``pi`` — the common "I picked Claude Code
but never ran ``omnigent setup`` to install it" case.

The in-process SDK harness (``claude-sdk``) runs without any CLI and
resolves its model credentials at runtime. The **launch gate** still never
blocks it: the spec's ``executor.auth`` (with ``${ENV}`` expansion) is
invisible here, so a hard gate would break launches that actually work. The **picker map**,
however, checks the credential sources the daemon *can* see locally —
locally resolvable provider entries (including ambient-detected local
providers such as a reachable Ollama), ambient env API keys, Claude Code's
own login / managed settings — and reports
``"needs-auth"`` when none is visible, so a credential-less host warns
before the first turn dies instead of after. Unknown harnesses fail
open on both axes.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from omnigent._platform import resolve_cli_binary
from omnigent.harness_aliases import HARNESS_ALIASES, canonicalize_harness
from omnigent.harness_availability import (
    HARNESS_BINARY_MISSING,
    HARNESS_NEEDS_AUTH,
    HARNESS_VERSION_TOO_LOW,
    HarnessAvailability,
)
from omnigent.harness_plugins import harness_install_keys, valid_harnesses
from omnigent.onboarding.harness_install import (
    PI_KEY,
    READINESS_CLI_PROBE_TIMEOUT_S,
    harness_cli_installed,
    harness_install_spec,
    required_cli_for_harness,
)
from omnigent.onboarding.provider_config import (
    _EXECUTOR_TYPE_HARNESS_ALIASES,
    _HARNESS_FAMILY,
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    OPENAI_FAMILY,
    PI_SURFACE,
    SUBSCRIPTION_KIND,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
)

_logger = logging.getLogger(__name__)

# Bound startup and periodic readiness work: the slow checks are independent
# CLI version/auth probes, but an unbounded process burst would be unfriendly on
# smaller hosts.
_READINESS_PROBE_MAX_WORKERS = 4

# In-process SDK harnesses: no CLI binary to gate on. The launch gate never
# blocks them (spec-level auth is invisible here), but the picker map reports
# ``needs-auth`` when no locally visible credential source could serve them
# (see :func:`_sdk_harness_availability`). The executor-type spelling
# (``claude_sdk``) and the ``claude`` alias normalize onto this first.
_SDK_HARNESSES: frozenset[str] = frozenset({"claude-sdk"})

# The CLI-wrapping pi harness. Unlike claude it has no ``_HARNESS_FAMILY``
# entry — pi uses the ``PI_SURFACE`` sentinel — so it must be gated explicitly
# or it fails open like an unknown harness.
_PI_HARNESSES: frozenset[str] = frozenset({PI_SURFACE})


def _canonical_harness(harness: str) -> str:
    """Normalize a harness id to its canonical spelling.

    Folds the user-facing alias (``claude`` → ``claude-sdk``) and the
    executor-type spellings :attr:`AgentSpec.harness_kind` returns
    (``claude_sdk`` → ``claude-sdk``) onto the canonical ids keyed in ``_HARNESS_FAMILY``.

    :param harness: A harness id, e.g. ``"claude"``, ``"claude_sdk"``,
        or ``"codex-native"``.
    :returns: The canonical spelling, e.g. ``"claude-sdk"`` or
        ``"codex-native"``; unknown names are returned unchanged.
    """
    canonical = canonicalize_harness(harness) or harness
    return _EXECUTOR_TYPE_HARNESS_ALIASES.get(canonical, canonical)


def _install_key(canonical: str) -> str:
    """Return the install-spec key whose CLI binary *canonical* requires.

    :param canonical: A canonical CLI-wrapping harness id keyed in
        ``_HARNESS_FAMILY`` (e.g. ``"codex-native"``) or a pi spelling.
    :returns: ``"anthropic"`` / ``"openai"`` for the claude/codex CLIs, or
        :data:`~omnigent.onboarding.harness_install.PI_KEY` for pi.
    """
    return _HARNESS_FAMILY.get(canonical) or PI_KEY


def _harness_availability_core(harness: str) -> HarnessAvailability:
    """Return the detailed availability state for *harness*.

    Mirrors :func:`harness_is_configured` but preserves the distinction
    between "CLI missing", "CLI present but version too old", and other
    structured states so the web UI and setup dialogs can show actionable
    copy.

    :param harness: A harness id, e.g. ``"claude-native"``,
        ``"codex-native"``, ``"claude_sdk"``, ``"pi"``, or ``"pi-native"``.
    :returns: A :data:`HarnessAvailability` value.``True`` when launchable;
        ``False`` or a reason string otherwise.
    """
    canonical = _canonical_harness(harness)
    if canonical in _SDK_HARNESSES:
        # Launch gate only: never block an SDK launch (spec-level auth is
        # invisible here). The picker map's credential check lives in
        # :func:`_sdk_harness_availability`.
        return True
    if canonical not in _HARNESS_FAMILY and canonical not in _PI_HARNESSES:
        required_cli = required_cli_for_harness(canonical) or required_cli_for_harness(harness)
        if required_cli is not None:
            return resolve_cli_binary(required_cli.binary) is not None
        # Unknown harness — the daemon has no install metadata for it, so
        # it can't assess readiness. Fail open (custom/newer harnesses,
        # version skew).
        return True
    return _installer_only_availability(_install_key(canonical))


# Provider kinds the in-process SDK executors cannot consume. Launch's
# spawn-env builder (``configure_agent_harness_with_provider`` in
# :mod:`omnigent.runtime.workflow`) rejects ``bedrock``; a ``subscription``
# is the CLI's own
# login, unusable outside that CLI. Counting any of them for an SDK harness
# would report ready for a source the launch would reject.
_SDK_UNUSABLE_PROVIDER_KINDS: frozenset[str] = frozenset({SUBSCRIPTION_KIND, BEDROCK_KIND})


def _provider_entry_locally_credentialed(
    provider: ProviderEntry,
    family: str | None,
    unusable_kinds: frozenset[str] = frozenset({SUBSCRIPTION_KIND}),
) -> bool:
    """Whether *provider* is a credential source that resolves on this host.

    A provider entry only readies a harness when launch could actually
    consume it:

    - Its kind must be usable by the consuming harness (*unusable_kinds*
      names the kinds launch rejects for it — always ``subscription``, whose
      CLI login is judged separately by :func:`harness_cli_logged_in`, plus
      :data:`_SDK_UNUSABLE_PROVIDER_KINDS` for the in-process SDK harnesses).
    - The secret it references must resolve locally: launch resolves the
      entry's family through
      :meth:`~omnigent.onboarding.provider_config.ProviderEntry.family`
      (which expands ``$VAR`` / ``api_key_ref`` via :func:`resolve_secret`)
      and fails rather than skipping the provider, so an entry pointing at an
      unset ``env:`` variable or a missing keychain secret is a first-turn
      auth failure, not a credential. Usable kinds that carry no inline
      family config (``databricks``) resolve their credential elsewhere at
      launch and keep counting as sources here.

    Local env/keychain resolution only — no network I/O — and never raises.

    :param provider: The selected
        :class:`~omnigent.onboarding.provider_config.ProviderEntry`.
    :param family: The family the harness consumes (``"anthropic"`` /
        ``"openai"``), or ``None`` for an unmapped harness (``pi``), which
        may consume either family.
    :param unusable_kinds: Provider kinds launch rejects for the consuming
        harness.
    :returns: ``True`` when the entry is launch-consumable and its credential
        resolves locally (or needs no local resolution), else ``False``.
    """
    if provider.kind in unusable_kinds:
        return False
    candidates = (family,) if family is not None else (ANTHROPIC_FAMILY, OPENAI_FAMILY)
    inline = [name for name in candidates if name in provider.families]
    if not inline:
        # Nothing to resolve locally (a family served structurally rather
        # than inline).
        return True
    for name in inline:
        try:
            provider.family(name)
            return True
        except Exception as exc:
            # Class-only: resolver errors can embed secret refs or material.
            _logger.debug(
                "readiness: provider credential for %r did not resolve (%s)",
                name,
                type(exc).__name__,
            )
    return False


def _family_provider_configured(harness: str) -> bool:
    """Whether a locally credentialed default provider ENTRY serves *harness*'s family.

    Reads the local ``providers:`` config the same way the ``omnigent setup``
    overview does (:func:`surface_default_provider` / :func:`default_provider_for_harness`,
    which resolve the harness's family and — for ``pi`` — its cross-family
    fallback). A ``subscription``-kind default is NOT counted here: it lives in
    the harness CLI's own login, judged separately by :func:`harness_cli_logged_in`,
    so counting it would double-count the CLI-login path and mask a genuine
    "installed but no key" state.

    The entry counts only when its credential actually resolves locally
    (:func:`_provider_entry_locally_credentialed`): launch resolves the
    entry's secret the same way and fails rather than skipping the provider,
    so a default whose ``api_key_ref`` points at an unset ``env:``/``$VAR``
    or a missing keychain secret is a first-turn auth failure, not a
    credential. The launch gate stays binary-only regardless.

    Local, synchronous, side-effect free (config file / env / keychain reads
    only) and never raises: any resolver/config error fails to ``False`` so a
    broken config reports "needs-auth" rather than crashing the readiness
    refresh.

    :param harness: A canonical harness spelling, e.g. ``"claude-native"`` or
        ``"pi"``.
    :returns: ``True`` when a locally credentialed non-subscription default
        provider entry is present for the harness's family, else ``False``.
    """
    try:
        provider = default_provider_for_harness(load_config(), harness)
        if provider is None:
            return False
        unusable = (
            _SDK_UNUSABLE_PROVIDER_KINDS
            if harness in _SDK_HARNESSES
            else frozenset({SUBSCRIPTION_KIND})
        )
        return _provider_entry_locally_credentialed(
            provider, _HARNESS_FAMILY.get(harness), unusable
        )
    except Exception as exc:
        # Readiness must never raise; a broken/unreadable config fails to
        # "no credential" (yellow) rather than crashing the refresh.
        # Class-only: provider config errors may embed credential material.
        _logger.debug("readiness: provider check failed for %r (%s)", harness, type(exc).__name__)
        return False


def _family_fallback_provider_configured(harness: str) -> bool:
    """Whether ANY locally credentialed provider entry serves *harness*'s family.

    Mirrors the launch-time last resort: with no family default configured,
    the runner's spawn-env resolution still credentials the head from a
    configured provider serving the family
    (:func:`~omnigent.onboarding.provider_config.first_available_provider`,
    consumed with ``for_launch=True`` in :mod:`omnigent.runtime.workflow`), so
    such an entry is a real credential source even though it is not the
    default. Like runtime resolution, the config is read over the ambient
    detection merge
    (:func:`~omnigent.onboarding.detected.effective_config_with_detected`),
    so automatically detected local providers — a reachable keyless Ollama —
    count exactly as they do at launch. Provider kinds the SDK launch would
    reject (:data:`_SDK_UNUSABLE_PROVIDER_KINDS` — ``subscription``,
    ``bedrock``) are skipped, and an entry counts only when
    its credential resolves locally
    (:func:`_provider_entry_locally_credentialed`). Local config/env reads
    plus ambient detection's localhost-only probes; never raises.

    :param harness: A canonical SDK harness id, e.g. ``"claude-sdk"``.
    :returns: ``True`` when a locally credentialed non-subscription provider
        entry (configured or ambient-detected) serves the harness's family.
    """
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return False
    try:
        from omnigent.onboarding.detected import effective_config_with_detected
        from omnigent.onboarding.provider_config import load_providers, provider_families

        config = effective_config_with_detected(load_config())
        for entry in load_providers(config).values():
            if family not in provider_families(entry):
                continue
            if _provider_entry_locally_credentialed(entry, family, _SDK_UNUSABLE_PROVIDER_KINDS):
                return True
    except Exception as exc:
        # Class-only: provider config errors may embed credential material.
        _logger.debug(
            "readiness: fallback provider check failed for %r (%s)",
            harness,
            type(exc).__name__,
        )
    return False


def _claude_managed_gateway_configured() -> bool:
    """Whether Claude Code's own settings chain carries a usable credential.

    The structural companion to :func:`_family_provider_configured` (which sees
    only omnigent's ``providers:`` config). An enterprise install configures
    Claude Code directly — a gateway ``ANTHROPIC_BASE_URL`` + ``apiKeyHelper`` in
    its managed settings — and Claude Code applies that at its own launch, so
    the harness is genuinely usable with nothing in ``config.yaml`` and no CLI
    subscription login. Crediting it here is what stops an enterprise host from
    reading "needs-auth" (the "Claude Code isn't configured on <host>" banner).

    Local, synchronous, side-effect free (one JSON file read) and never raises:
    any error fails to ``False`` so readiness falls through to the CLI status
    probe rather than crashing the refresh.

    :returns: ``True`` when Claude Code's managed settings deliver a credential.
    """
    try:
        from omnigent.onboarding.ambient import claude_managed_gateway

        return claude_managed_gateway()[1]
    except Exception:
        _logger.debug("readiness: claude managed-settings check failed", exc_info=True)
        return False


def _ambient_family_env_key_configured(family: str) -> bool:
    """Whether an ambient env API key serving *family* is set.

    Checks the same vendor env vars ambient detection adopts
    (:data:`~omnigent.onboarding.ambient._ENV_KEY_FAMILY` over
    :data:`~omnigent.onboarding.providers.PROVIDER_ENV_VARS`), including their
    ``OMNIGENT_``-prefixed variants — e.g. ``ANTHROPIC_API_KEY`` for the
    ``anthropic`` family, ``OPENAI_API_KEY`` / ``OPENROUTER_API_KEY`` for
    ``openai``. The daemon spawns runners with its own environment, so a key
    visible here is a key the harness will inherit.

    :param family: A model family, e.g. ``"anthropic"`` or ``"openai"``.
    :returns: ``True`` when any such variable is set and non-empty.
    """
    from omnigent.onboarding.ambient import _ENV_KEY_FAMILY
    from omnigent.onboarding.providers import PROVIDER_ENV_VARS
    from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

    for provider, env_var in PROVIDER_ENV_VARS.items():
        if _ENV_KEY_FAMILY.get(provider) != family:
            continue
        if getenv_nonempty_with_omnigent_prefix(env_var) is not None:
            return True
    return False


def _claude_token_env_configured() -> bool:
    """Whether an ambient Claude token env credential is visible.

    The host forwards these to its runners (the harness credential allowlist
    in :mod:`omnigent.host.connect`) and Claude Code resolves them directly:
    ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token`` subscription auth) and
    ``ANTHROPIC_AUTH_TOKEN`` (gateway bearer, usually paired with
    ``ANTHROPIC_BASE_URL``), including their ``OMNIGENT_``-prefixed variants.
    Env reads only; never raises.

    :returns: ``True`` when either token variable is set and non-empty.
    """
    from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

    return any(
        getenv_nonempty_with_omnigent_prefix(var) is not None
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
    )


def _claude_code_login_configured() -> bool:
    """Whether Claude Code's own subscription login is present on this machine.

    The claude-sdk harness drives Claude Code, so the CLI's login is a
    complete credential for it even with nothing in omnigent's config. Local
    and never raises: any detection error reads as "no login".

    :returns: ``True`` when a usable Claude Code login is detected.
    """
    try:
        from omnigent.onboarding import ambient

        return ambient._claude_login_detected()
    except Exception:
        _logger.debug("readiness: claude login check failed", exc_info=True)
        return False


def _global_auth_configured() -> bool:
    """Whether the user-level global ``auth:`` block can serve the SDK builders.

    Both SDK executors inherit the ``auth:`` mapping from the user's global
    ``config.yaml`` (``_load_global_auth`` in :mod:`omnigent.runtime.workflow`)
    when the agent spec declares no ``executor.auth``, so a parsed global
    api-key auth block is a complete credential source. Local
    file read only; never raises.

    :returns: ``True`` when the global ``auth:`` block parses into a usable
        auth configuration.
    """
    try:
        from omnigent.runtime.workflow import _load_global_auth

        return _load_global_auth() is not None
    except Exception as exc:
        # Class-only: the auth block (or its parse error) may carry secrets.
        _logger.debug("readiness: global auth check failed (%s)", type(exc).__name__)
        return False


def _sdk_harness_availability(canonical: str) -> HarnessAvailability:
    """Picker-facing readiness for an in-process SDK harness.

    ``True`` when any locally visible credential source could serve the
    harness at run time, else ``"needs-auth"``. The check errs toward ready
    and detects credential *sources*, not guaranteed-usable credentials:
    spec-level ``executor.auth`` and other runtime-only sources are invisible
    here, and a configured source may still fail to resolve at launch. Only
    a host where *nothing* local could authenticate reads ``"needs-auth"``
    — and that signal warns in the picker, it never blocks the launch
    (:func:`harness_is_configured` stays ungated for SDK harnesses).

    :param canonical: A canonical SDK harness id from :data:`_SDK_HARNESSES`.
    :returns: ``True`` or ``"needs-auth"``.
    """
    if _family_provider_configured(canonical):
        return True
    if _family_fallback_provider_configured(canonical):
        return True
    if _global_auth_configured():
        return True
    family = _HARNESS_FAMILY.get(canonical)
    if family is not None and _ambient_family_env_key_configured(family):
        return True
    if family == ANTHROPIC_FAMILY and (
        _claude_token_env_configured()
        or _claude_managed_gateway_configured()
        or _claude_code_login_configured()
    ):
        return True
    return HARNESS_NEEDS_AUTH


def _installer_only_availability(install_key: str) -> HarnessAvailability:
    """Return availability for a binary-gated harness without login commands.

    Mirrors :func:`_binary_availability_reason` but keeps the historical bare
    ``False`` shape for a missing binary, so existing web/clients that expect a
    simple boolean get that and only learn about structured reasons when the
    binary is present but on an unsupported version.
    """
    state = _binary_availability_reason(install_key)
    if state == HARNESS_BINARY_MISSING:
        return False
    return state


def _binary_availability_reason(install_key: str) -> HarnessAvailability:
    """Return the readiness reason when a CLI-backed harness can't be used.

    Distinguishes a genuinely missing CLI from one that is on ``PATH`` but
    outside the version range the native harness requires. The latter is
    exposed to the web UI as ``"version-too-low"`` so the user sees a prompt
    to upgrade rather than "binary-missing".
    """
    if harness_cli_installed(install_key, timeout=READINESS_CLI_PROBE_TIMEOUT_S):
        return True
    spec = harness_install_spec(install_key)
    if spec is not None and resolve_cli_binary(spec.binary) is not None:
        return HARNESS_VERSION_TOO_LOW
    return HARNESS_BINARY_MISSING


def _harness_availability(canonical: str) -> HarnessAvailability:
    """Return picker-facing availability for one canonical harness spelling."""
    if canonical in _PI_HARNESSES:
        # pi has no CLI login — its only credential is either an omnigent-managed
        # provider (an API key / gateway) or a pi-subscription ("Pi original auth",
        # which signals "use Pi's own ~/.pi/agent as-is"). So the two-step signal
        # is binary + provider: installed-but-no-provider is the yellow "needs-auth"
        # state the setup dialog acts on.
        binary_state = _binary_availability_reason(PI_KEY)
        if binary_state is not True:
            return binary_state
        if _family_provider_configured(PI_SURFACE):
            return True
        # A pi subscription (original auth) is also a valid configured state —
        # it means Pi will use its own ~/.pi/agent credentials, no omnigent
        # provider needed.
        try:
            provider = default_provider_for_harness(load_config(), PI_SURFACE)
            if (
                provider is not None
                and provider.kind == SUBSCRIPTION_KIND
                and provider.cli == "pi"
            ):
                return True
        except Exception:
            pass
        return "needs-auth"
    if canonical in _SDK_HARNESSES:
        return _sdk_harness_availability(canonical)
    return _harness_availability_core(canonical)


def harness_is_configured(harness: str) -> bool:
    """Return whether *harness* can be launched on this machine.

    Only CLI-wrapping harnesses are assessed (native Claude/Codex and
    ``pi`` / ``pi-native``): they cannot run without their binary on
    ``PATH``, and that is the one thing the daemon can check reliably and
    locally. SDK harnesses and unknown harnesses always return ``True`` —
    spec-level credentials are invisible here, so blocking them would risk
    false negatives that break working launches. (The picker map separately
    reports ``"needs-auth"`` for an SDK harness with no locally visible
    credential — see :func:`_sdk_harness_availability` — but that signal
    warns, it never gates a launch.)

    The check is binary-only: an installed-but-not-logged-in CLI still
    returns ``True`` because auth failures surface at run time rather than
    blocking dispatch.

    :param harness: A harness id, e.g. ``"claude-native"``,
        ``"codex-native"``, ``"claude_sdk"``, ``"pi"``, or ``"pi-native"``.
    :returns: ``True`` when launchable (CLI installed, or a harness the
        daemon doesn't gate); ``False`` when the binary is missing or on
        an unsupported version.
    """
    return _harness_availability_core(harness) is True


def configured_harness_map() -> dict[str, HarnessAvailability]:
    """Return per-harness readiness for every accepted harness spelling.

    Built so the server/web UI can do a plain dict lookup with whatever
    spelling it holds — canonical ids, executor-type spellings, the
    ``claude`` alias, and ``pi``. Unknown harnesses map to ``True`` (never
    gated); CLI-wrapping harnesses map to whether their binary is on
    ``PATH``; SDK harnesses map to ``True`` when a locally visible credential
    source could serve them, else ``"needs-auth"``. Codex entries use a
    structured string reason when unavailable: ``"binary-missing"`` or
    ``"needs-auth"``.

    :returns: Mapping of harness spelling to readiness, e.g.
        ``{"claude-native": False, "codex-native": "needs-auth",
        "claude-sdk": "needs-auth", "pi": True}``.
    """
    spellings: set[str] = set(valid_harnesses())
    spellings.update(harness_install_keys())
    spellings.update(_EXECUTOR_TYPE_HARNESS_ALIASES)
    spellings.update(HARNESS_ALIASES)
    spellings.update(_PI_HARNESSES)
    canonical_by_cache_key: dict[tuple[str, ...], str] = {}
    cache_key_by_spelling: dict[str, tuple[str, ...]] = {}
    for spelling in spellings:
        canonical = _canonical_harness(spelling)
        cache_key = ("harness", canonical)
        canonical_by_cache_key.setdefault(cache_key, canonical)
        cache_key_by_spelling[spelling] = cache_key

    with ThreadPoolExecutor(
        max_workers=min(_READINESS_PROBE_MAX_WORKERS, len(canonical_by_cache_key)),
        thread_name_prefix="harness-readiness",
    ) as executor:
        futures = {
            cache_key: executor.submit(_harness_availability, canonical)
            for cache_key, canonical in canonical_by_cache_key.items()
        }
        availability_cache = {cache_key: future.result() for cache_key, future in futures.items()}

    return {
        spelling: availability_cache[cache_key]
        for spelling, cache_key in cache_key_by_spelling.items()
    }
