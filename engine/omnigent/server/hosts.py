"""Server-launched sandbox hosts for ``host_type="managed"`` sessions.

The external host flow has a human run ``omnigent host`` on their own
machine. The managed flow replaces the human: when a session is created
with ``host_type="managed"``, the server provisions a cloud sandbox,
starts ``omnigent host`` inside it, and waits for that host to
register — after which the session rides the exact same host-launch
machinery an external host uses (binding token, ``host.launch_runner``
frame, runner tunnel).

The host's identity is DURABLE while its sandbox is not: the ``hosts``
row carries the managed columns (launch-token digest + expiry,
provider, sandbox id), and a relaunch overwrites them in place — a new
sandbox generation under the same ``host_id``, so session bindings
survive a sandbox dying at the provider's lifetime cap.

The sandbox host authenticates back with a dedicated launch token the
server mints per launch (see
:meth:`omnigent.stores.host_store.HostStore.register_managed_host`,
:meth:`omnigent.stores.host_store.HostStore.replace_managed_host_sandbox`, and
the managed-token branch in
:mod:`omnigent.server.routes.host_tunnel`) — the user's own
credentials never enter the sandbox.

How a deployment supplies the sandbox backend (two paths, one seam —
:class:`HostProviderConfig` carries a launcher FACTORY, so embedding
deployments inject custom launchers the same way they inject custom
stores into ``create_app``):

1. **Server YAML** (OSS / self-hosted): :func:`parse_host_section`
   builds the config from the ``hosts:`` section
   (``omnigent server -c`` / ``OMNIGENT_CONFIG`` /
   ``<data_dir>/config.yaml``)::

       host:
         provider: docker
         server_url: https://omnigent.example.com
         # For SEVERAL providers, replace `provider:` with a `providers:`
         # list (mutually exclusive); a create picks one by name via
         # `sandbox_provider`, else the first. See parse_host_section.
         keep_warm_s: 1800        # optional; idle seconds before the runner suspends
         reaper:                  # optional; deployment-wide, not per-provider
           enabled: true          # default: false
           terminate_after_offline_days: 30  # default: 30 days
           sweep_interval_s: 86400            # default: 1 day
         host_config:             # optional; provider-agnostic. Verbatim
                                  # in-sandbox ~/.omnigent/config.yaml content,
                                  # installed before `omnigent host` starts
                                  # (e.g. route the `pi` harness through a
                                  # self-hosted gateway). Server-managed:
                                  # entries injected earlier are replaced or
                                  # removed on the next launch/resume; user
                                  # config in the sandbox survives. Keep
                                  # secrets out via api_key_ref: env: —
                                  # resolved in the SANDBOX env (harness
                                  # Secret / provider env lane).
           providers:
             litellm:
               kind: gateway
               default: [pi]
               openai:
                 base_url: http://litellm.litellm.svc.cluster.local/v1
                 api_key_ref: env:LITELLM_API_KEY
                 wire_api: chat

   The host launcher (Superchat's per-Muse sandbox host,
   docs/architecture.md) needs no provider block: it reads
   ``OMNIGENT_SUPERCHAT_SUPERVISOR_URL`` and ``OMNIGENT_SUPERCHAT_SUPERVISOR_TOKEN``
   from the server process environment (12-factor) — see
   :mod:`omnigent.hosts.docker`. Providers contributed
   through the ``omnigent.host_providers`` entry point group take an
   optional ``host.<provider>`` block validated by their declared config model.

2. **Direct construction** (embedding deployments): build
   :class:`HostProviderConfig` with a custom
   :class:`~omnigent.hosts.launcher.ExecModelHostLauncher`
   factory and pass it to ``create_app(sandbox_config=…)``::

       HostProviderConfig(
           server_url=public_url,
           launcher_factory=lambda: MySandboxLauncher(...),
           token_ttl_s=7 * 24 * 3600,
       )

   A managed-only launcher implements ``prepare`` / ``provision`` /
   ``run`` / ``terminate``; the CLI-bootstrap primitives default to
   capability errors and need no overrides.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, cast

import click
from fastapi import HTTPException

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.db.utils import builtin_agent_id, now_epoch
from omnigent.evals.identity import runner_env as eval_runner_env
from omnigent.hosts.launcher import HostGoneError

# RepoWorkspace lives in the launcher's own package so a launcher can accept it
# without importing omnigent.server; re-exported here (its parser is here) so
# existing `from omnigent.server.hosts import RepoWorkspace` keeps working.
from omnigent.hosts.types import GitCloneOptions, RepoWorkspace
from omnigent.stores.host_store import Host, HostStore

if TYPE_CHECKING:
    from omnigent.hosts import HostLauncher
    from omnigent.stores.agent_store import AgentStore

_logger = logging.getLogger(__name__)

# Providers the YAML `hosts:` section accepts. Parsing accepts all
# known providers so a deployment can stage config ahead of support
# landing, but only PROVIDERS_WITH_MANAGED_LAUNCH can actually serve a
# managed session today. (Deployments that construct
# HostProviderConfig directly are not constrained by either set —
# their launcher factory IS the support.)
SUPPORTED_HOST_PROVIDERS: frozenset[str] = frozenset({"docker"})
PROVIDERS_WITH_MANAGED_LAUNCH: frozenset[str] = frozenset({"docker"})

# How long a managed launch waits for the sandboxed host to register
# before declaring failure. The image is pre-baked (no pip install at
# boot), so a healthy launch registers in seconds; the budget covers a
# cold registry pull of the image on first use.
HOST_ONLINE_TIMEOUT_S = 120
_ONLINE_POLL_INTERVAL_S = 1.0

# Launch-token lifetime for the "docker" provider (Superchat's per-person host,
# docs/architecture.md). The host itself has no lifetime cap — it is a persistent,
# Superchat-managed resource that outlives any one launch — so the bound here is policy: seven
# days keeps a long-lived host re-authenticating across tunnel reconnects while still expiring
# tokens nobody renewed. A relaunch mints a fresh token.
HOST_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for a contributed provider that declares none, and the
# placeholder for staged providers whose factory rejects before minting.
DEFAULT_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Where the in-sandbox host process logs — named in launch-failure
# errors so an operator knows where to look inside the sandbox.
_HOST_LOG_PATH = "/tmp/omnigent-host.log"

# How long a message POST waits for an in-flight managed launch to
# settle before giving up (see ManagedLaunchTracker). Covers the full
# launch/wake pipeline ON TOP OF the host-registration wait
# (HOST_ONLINE_TIMEOUT_S): the provider's provision/resume call
# (StartSandbox has no fixed upper bound), the host-tunnel reconnect on
# this replica, and the runner spawn/connect. The 120s slack must cover
# all of those so a slow cold launch/wake doesn't time the parked message
# out before the background launch settles — otherwise the first
# post-dormancy turn is lost even though the wake later succeeds. The wait
# resolves as soon as the launch settles, so this bound only bites a
# genuinely slow launch.
MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S = HOST_ONLINE_TIMEOUT_S + 120

# Server-internal sandbox lifecycle labels — currently the repository a relaunch
# re-clones. A client seed would forge that reconstruction, so session
# create/patch reject any client label here, closing future keys by default.
MANAGED_SANDBOX_LABEL_NAMESPACE = "omnigent.sandbox."

# Base key for the labels recording the repository-URL workspaces a managed
# session was created with (the raw ``<url>[#<branch>]`` request values).
# ``conversations.workspace`` is overwritten with the CLONED path at bind time,
# so these labels are what a sandbox RELAUNCH parses to re-clone the repos into
# the fresh generation's workspace. Stored ONE PER REPO under ``<base>.<index>``
# (``omnigent.sandbox.repo.0``, ``.1``, …): a single joined label would be
# silently truncated at the 256-char label-value cap, losing repos on relaunch.
# Per-session state: a fork never inherits them (the store drops the whole
# ``<base>.`` family, and the fork's own launch re-stamps whatever it resolves),
# while an in-place agent switch keeps them — the sandbox is unchanged.
MANAGED_REPO_LABEL_KEY = "omnigent.sandbox.repo"


def managed_repo_labels(workspaces: Sequence[str]) -> dict[str, str]:
    """
    Build the per-repo relaunch labels for a managed session's workspaces.

    One label per repo, ``<base>.<index>`` → the raw ``<url>[#<branch>]`` value,
    so each stays well under the 256-char label-value cap that would truncate a
    single joined label (and lose repos on relaunch/fork). These indexed labels
    are the source of truth :func:`read_managed_repo_workspaces` reads.

    Backwards-compat shim: also write the legacy bare ``<base>`` key (the
    space-joined value) so a server predating per-repo storage — e.g. after a
    rollback against a shared database — still relaunches the repos through its
    old bare-key read. It's written only when it fits the label-value cap (so a
    truncated value is never stored); a larger set has no bare key and an old
    server would relaunch it empty, but that is the rare many-repo case and only
    a rolled-back reader. The current server ignores the bare key whenever the
    indexed labels are present.

    :param workspaces: The raw repository workspace strings, in order.
    :returns: ``{f"{MANAGED_REPO_LABEL_KEY}.{i}": ws}`` for each, plus the bare
        ``MANAGED_REPO_LABEL_KEY`` when the joined value fits; ``{}`` when
        *workspaces* is empty.
    """
    labels = {f"{MANAGED_REPO_LABEL_KEY}.{i}": ws for i, ws in enumerate(workspaces)}
    if labels:
        joined = " ".join(workspaces)
        if len(joined) <= LABEL_VALUE_MAX_LEN:
            labels[MANAGED_REPO_LABEL_KEY] = joined
    return labels


def read_managed_repo_workspaces(labels: Mapping[str, str]) -> list[str]:
    """
    Recover the ordered repository workspaces from a session's labels.

    Inverse of :func:`managed_repo_labels`: collect every ``<base>.<index>``
    label and return the values ordered by their integer index. Falls back to
    the legacy single space-joined ``<base>`` label so sessions created before
    per-repo storage still relaunch/fork with their repositories.

    :param labels: The session's labels.
    :returns: The raw workspace strings in order; ``[]`` when none are recorded.
    """
    prefix = f"{MANAGED_REPO_LABEL_KEY}."
    indexed: list[tuple[int, str]] = []
    for key, value in labels.items():
        if key.startswith(prefix):
            suffix = key[len(prefix) :]
            if suffix.isdigit():
                indexed.append((int(suffix), value))
    if indexed:
        return [value for _, value in sorted(indexed)]
    # Legacy: a single space-joined label from before per-repo storage.
    legacy = labels.get(MANAGED_REPO_LABEL_KEY)
    return legacy.split() if legacy else []


def resolve_managed_agent_label(
    agent_store: AgentStore,
    agent_id: str | None,
    *,
    session_id: str,
) -> str | None:
    """
    Resolve the agent-classifier value for a managed runner, gated to
    genuine built-ins.

    The runner's ``omnigent.ai/agent`` classifier is what an admission policy
    selects on to inject a privileged credential (e.g. a scoped git token).
    That is only trustworthy for a genuine BUILT-IN (operator-seeded) agent:
    a user can create a *session-scoped* agent whose name matches a built-in's,
    so name alone is spoofable. The gate is ``session_id is None AND id ==
    builtin_agent_id(name)`` — the same identity built-in seeding uses. A
    session-scoped or user-uploaded agent (random id, and/or an owning
    conversation) fails it and its runner gets NO agent label.

    The label is an optimization, never a launch precondition: any failure to
    resolve (no agent, unknown id, not a built-in, or a store error) returns
    ``None`` so the caller omits the label rather than stamping something
    unmatchable or failing the create. The decision is logged either way (the
    "why did this runner get no credential" line).

    :param agent_store: Store to resolve the agent record from.
    :param agent_id: The session's bound agent id, or ``None``.
    :param session_id: Session id, for log correlation only.
    :returns: The built-in agent's registered name to stamp, or ``None`` to
        omit the classifier.
    """
    if agent_id is None:
        _logger.info("session %s: no agent label (session has no bound agent)", session_id)
        return None
    try:
        agent = agent_store.get(agent_id)
    except Exception:
        # create precondition. Any store failure degrades to an unlabeled runner
        # (the admission policy skips it) rather than 500-ing a create that has
        # already committed and announced the session.
        _logger.warning(
            "session %s: agent-label resolve failed for agent %s; omitting label",
            session_id,
            agent_id,
            exc_info=True,
        )
        return None
    if agent is None:
        _logger.warning(
            "session %s: agent id %s did not resolve to an agent; omitting label",
            session_id,
            agent_id,
        )
        return None
    if agent.session_id is not None or agent.id != builtin_agent_id(agent.name):
        _logger.info(
            "session %s: agent %r (%s) is not a genuine built-in; omitting agent label",
            session_id,
            agent.name,
            agent.id,
        )
        return None
    _logger.info(
        "session %s: classifying runner with built-in agent label %r",
        session_id,
        agent.name,
    )
    return agent.name


@dataclass
class ManagedLaunch:
    """
    One session's in-flight (or failed) managed-host launch.

    Created by :meth:`ManagedLaunchTracker.begin` when
    ``POST /v1/sessions`` schedules the background launch, and settled
    by the background task via :meth:`ManagedLaunchTracker.finish` /
    :meth:`ManagedLaunchTracker.fail`.

    :param settled: Set once the launch reaches a terminal state —
        either success (host bound, runner launched) or failure.
        Waiters (a message POST racing the provision) block on this.
    :param error: Failure detail once settled unsuccessfully, e.g.
        ``"managed sandbox launch failed: …"``. ``None`` while
        in flight and on success.
    """

    settled: asyncio.Event
    error: str | None = None


class ManagedLaunchTracker:
    """
    In-memory index of managed-host launches keyed by session id.

    ``POST /v1/sessions`` with ``host_type="managed"`` returns before
    the sandbox exists; this tracker is how the rest of the server
    observes that window. A message POST that arrives mid-provision
    waits on the session's :class:`ManagedLaunch` instead of failing
    with "no runner bound"; a launch failure is recorded here so the
    waiting POST (and any later one) reports the real reason.

    Successful launches are removed on settle — from then on the
    session looks like any host-bound session. Failed launches are
    retained (the session row never got a host, so the recorded error
    is the only trace of why) until the process restarts or a new
    launch for the same session begins.
    """

    def __init__(self) -> None:
        """Initialize the empty session-id → launch index."""
        self._by_session: dict[str, ManagedLaunch] = {}

    def begin(self, session_id: str) -> None:
        """
        Register a new in-flight launch for *session_id*.

        Replaces any prior entry (e.g. a retained failure from an
        earlier attempt).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        self._by_session[session_id] = ManagedLaunch(settled=asyncio.Event())

    def get(self, session_id: str) -> ManagedLaunch | None:
        """
        Look up the launch state for *session_id*.

        :param session_id: Session/conversation identifier.
        :returns: The launch entry, or ``None`` when no managed launch
            is in flight or recorded as failed for this session.
        """
        return self._by_session.get(session_id)

    def finish(self, session_id: str) -> None:
        """
        Settle *session_id*'s launch as successful and forget it.

        Waiters holding the entry observe ``settled`` with
        ``error is None``; later readers find no entry and take the
        normal host-bound paths.

        :param session_id: Session/conversation identifier.
        """
        entry = self._by_session.pop(session_id, None)
        if entry is not None:
            entry.settled.set()

    def fail(self, session_id: str, error: str) -> None:
        """
        Settle *session_id*'s launch as failed, retaining the reason.

        :param session_id: Session/conversation identifier.
        :param error: Human-readable failure detail, e.g.
            ``"managed sandbox launch failed: spend limit reached"``.
        """
        entry = self._by_session.get(session_id)
        if entry is None:
            return
        entry.error = error
        entry.settled.set()


@dataclass
class HostProviderConfig:
    """
    Everything the managed-host flow needs from a deployment.

    Built by :func:`parse_host_section` from the server YAML, or
    constructed directly by embedding deployments to inject a custom
    launcher (see the module docstring).

    :param server_url: Public URL of THIS server that the sandboxed
        host dials back to, e.g. ``"https://omnigent.example.com"``
        (no trailing slash). Explicit — the server cannot reliably
        infer its own public URL behind proxies.
    :param launcher_factory: Zero-argument factory producing the
        :class:`~omnigent.hosts.launcher.ExecModelHostLauncher`
        each launch uses, e.g.
        ``lambda: DockerHostLauncher()``. Called per launch
        (launchers may cache provider handles internally). May raise
        ``HTTPException`` to report an unusable backend — the YAML
        path uses this for providers without managed support.
    :param token_ttl_s: Launch-token lifetime in seconds, e.g.
        ``604800`` (7 days) for ``docker``. Must comfortably exceed the
        provider's maximum sandbox lifetime so a live sandbox can
        always re-authenticate its tunnel across reconnects.
    :param managed_launch_supported: Whether ``launcher_factory`` can
        actually serve a managed launch. The YAML path sets this from
        :data:`PROVIDERS_WITH_MANAGED_LAUNCH` — staged providers
        parse but get ``False``, since their factory
        rejects at launch. Defaults to ``True`` for
        directly-constructed configs (an embedding deployment's
        custom factory IS the support). Drives the unauthenticated
        ``managed_sandboxes_enabled`` capability flag on
        ``GET /v1/info``, which gates the web UI's sandbox option.
    :param provider: Short provider name surfaced to the web UI so the
        new-session sandbox option can be labeled per provider (e.g.
        ``"docker"``). The YAML path sets it from the parsed
        ``host.provider``. ``None`` for directly-constructed
        embedding configs that don't name a provider — the UI then
        falls back to the generic "New Sandbox" label. Exposed (when
        managed launch is supported) on the unauthenticated
        ``GET /v1/info`` as ``sandbox_provider``.
    :param host_config: Verbatim in-sandbox ``~/.omnigent/config.yaml``
        content (e.g. a ``providers:`` block routing a harness through
        a self-hosted gateway) installed into the sandbox's config before
        ``omnigent host`` starts, or ``None``. Server-managed: previously
        injected entries are replaced or removed on each launch/resume so
        the sandbox always reflects the current block. Provider-agnostic:
        forwarded to every launcher's ``start_host`` — see
        :func:`omnigent.hosts.launcher.render_host_config_write_command`.
        Non-secret by design: credentials stay behind
        ``api_key_ref: env:VAR`` indirection, resolved inside the
        sandbox against its own environment. Saved session inference settings
        replace the current template's providers and bindings on restore.
    :param model_discovery: Server-only catalog endpoints and credential references,
        keyed by inference provider name. Never installed in the sandbox.
    :param git_clone: Admin clone policy for fresh or missing repo checkouts.
        Existing retained checkouts are not reconfigured.
    :param runner_env: Names (not values) of environment variables to resolve
        from THIS SERVER PROCESS's own environment at launch and forward to
        the sandboxed host's process — e.g. ``("ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY")``. The host, in turn, forwards them to every runner
        it spawns (:data:`omnigent.host.connect.RUNNER_ENV_PASSTHROUGH_ENV_VAR`).
        Provider-neutral in config; delivered today only by launchers whose
        ``start_host`` accepts a ``runner_env`` argument (the exec-model
        default — see :meth:`~omnigent.hosts.launcher.
        ExecModelHostLauncher.start_host`). Names only, never values: this
        field is safe to log, and nothing here is a secret by itself.
    """

    server_url: str
    launcher_factory: Callable[[], HostLauncher]
    token_ttl_s: int
    managed_launch_supported: bool = True
    provider: str | None = None
    host_config: dict[str, object] | None = None
    model_discovery: dict[str, object] = dataclass_field(default_factory=dict)
    git_clone: GitCloneOptions = dataclass_field(default_factory=GitCloneOptions)
    runner_env: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostReaperConfig:
    """Deployment-wide managed sandbox reaper settings.

    :param enabled: Whether the server lifespan starts the reaper loop.
        Disabled by default because terminating user compute is destructive.
    :param terminate_after_offline_days: Minimum number of days since the
        managed host's last heartbeat/disconnect before its current sandbox
        generation may be terminated.
    :param sweep_interval_s: Seconds between complete deployment-wide sweeps.
    """

    enabled: bool = False
    terminate_after_offline_days: int = 30
    sweep_interval_s: int = 24 * 60 * 60


@dataclass
class HostDeployment:
    """
    The set of host providers a server offers, as a single unit.

    Wraps one :class:`HostProviderConfig` per provider. A
    single-provider deployment holds exactly one; a ``host.providers``
    list holds several. Callers thread this — not a bare
    :class:`HostProviderConfig` — through the managed-host lifecycle
    and resolve the per-provider config through :meth:`for_provider` /
    :meth:`recorded`, so they never branch on which config shape the
    deployment was built from.

    Built by :func:`parse_host_section` from the server YAML, or by
    :meth:`single` around a directly-constructed embedding config.

    :param configs: One single-provider config per offered provider, in
        configured order. Never empty. The first is the deployment
        default a request that names no provider gets.
    :param reaper: One deployment-wide reaper policy shared by every
        configured provider.
    """

    configs: tuple[HostProviderConfig, ...]
    reaper: HostReaperConfig = dataclass_field(default_factory=HostReaperConfig)

    def __post_init__(self) -> None:
        # Enforce the "never empty" invariant the accessors rely on
        # (:attr:`default` indexes ``configs[0]``): the parser already
        # rejects an empty ``providers`` list, so a breach means a
        # direct constructor passed ``configs=()``.
        if not self.configs:
            raise ValueError("HostDeployment requires at least one provider config")

    @classmethod
    def single(
        cls,
        config: HostProviderConfig,
        *,
        reaper: HostReaperConfig | None = None,
    ) -> HostDeployment:
        """
        Wrap one provider config as a one-provider deployment.

        The path an embedding deployment's directly-constructed config
        takes: ``create_app`` wraps it so the rest of the flow sees a
        deployment uniformly.

        :param config: The single provider's config.
        :param reaper: Optional deployment-wide reaper policy.
        :returns: A deployment offering exactly *config*.
        """
        return cls(configs=(config,), reaper=reaper or HostReaperConfig())

    @property
    def default(self) -> HostProviderConfig:
        """
        The provider a request that names none launches on.

        Prefers the first launch-capable provider, so the default
        launcher and the :attr:`managed_launch_supported` flag agree
        even when a staged provider is listed first.
        Falls back to the first entry when none supports launch, so a
        deployment of only staged providers still resolves.

        :returns: The default single-provider config.
        """
        for config in self.configs:
            if config.managed_launch_supported:
                return config
        return self.configs[0]

    @property
    def managed_launch_supported(self) -> bool:
        """Whether ANY offered provider can serve a managed launch."""
        return any(config.managed_launch_supported for config in self.configs)

    def offered(self) -> tuple[HostProviderConfig, ...]:
        """Every provider config this deployment offers, in configured order."""
        return self.configs

    def for_provider(self, provider: str | None) -> HostProviderConfig | None:
        """
        Resolve the config backing one provider name.

        :param provider: Provider short name, e.g. ``"docker"``. ``None``
            selects the deployment :attr:`default`, which is what a
            request that names no provider gets.
        :returns: The matching single-provider config, or ``None`` when
            this deployment does not offer *provider*.
        """
        if provider is None:
            return self.default
        for config in self.configs:
            if config.provider == provider:
                return config
        return None

    def recorded(self, provider: str | None) -> HostProviderConfig:
        """
        Resolve the config to act on a host launched with *provider*.

        Never fails, unlike :meth:`for_provider`: falls back to the
        :attr:`default` when the recorded provider is no longer offered,
        so the caller (which compares the built launcher's provider
        against the host row) still gets a config to try.

        :param provider: Provider recorded on the host row, or ``None``.
        :returns: The config whose launcher should act on that host.
        """
        return self.for_provider(provider) or self.default

    def launchable_providers(self) -> tuple[str, ...]:
        """
        Names of every offered provider that can actually serve a launch.

        Excludes staged providers (parse-but-reject) and
        configs naming no provider, so this is exactly what a client may
        choose from.

        :returns: Provider short names, in configured order.
        """
        return tuple(
            config.provider
            for config in self.configs
            if config.managed_launch_supported and config.provider is not None
        )

    def provider_ui_capabilities(self) -> dict[str, dict[str, bool]]:
        """
        UI-relevant capability flags per launchable provider, for ``/v1/info``.

        Only the flags the web branches on are exposed (not the full internal
        :class:`HostCapabilities`), so the wire contract stays small — add an
        inner key here to surface another flag. Instantiates each launchable
        provider's launcher to read its declared capabilities.

        ponytail: builds launchers on each call; memoize on the deployment only
        if /v1/info shows up hot (provider count is tiny, __init__ does no I/O).

        :returns: Capability flags for each launchable provider, in configured order.
        """
        caps: dict[str, dict[str, bool]] = {}
        for config in self.configs:
            if config.managed_launch_supported and config.provider is not None:
                launcher = config.launcher_factory()
                caps[config.provider] = {"multi_repo": launcher.capabilities.multi_repo}
                from omnigent.inference_config import parse_inference_config

                if parse_inference_config(config.host_config or {}):
                    caps[config.provider]["inference_models"] = True
        return caps


@dataclass
class HostLaunch:
    """
    Result of a successful managed host launch.

    :param host_id: The registered host's identifier, e.g.
        ``"host_a1b2c3d4..."`` — feed this to the same launch-runner
        path an external ``host_id`` takes.
    :param workspace: Absolute workspace path created inside the
        sandbox, e.g. ``"/root/workspace"`` — or the cloned repository
        directory (e.g. ``"/root/workspace/myrepo"``) when the session
        requested a repository-URL workspace.
    """

    host_id: str
    workspace: str


# A full 40-hex object id — rejected as a clone fragment: cloning a
# commit lands the agent on a detached HEAD it cannot push from.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Directory names a repo URL may resolve to. Conservative on purpose:
# the name is interpolated into an in-sandbox shell path.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Characters git forbids in ref names (plus ``#``, which can never
# reach the fragment since the workspace splits on its FIRST ``#`` —
# a second ``#`` means the branch itself contains one, which the
# fragment form does not support).
_BRANCH_FORBIDDEN_CHARS = set(" \t~^:?*[\\#")


def is_repo_workspace(workspace: str) -> bool:
    """
    Return whether *workspace* is a repository-URL workspace.

    Used by the create-session schema to tell the managed form (a git
    URL) apart from the external form (an absolute host path) without
    fully parsing it.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo"`` or ``"/Users/me/repo"``.
    :returns: ``True`` for the ``https://`` / ``git@`` URL forms.
    """
    return workspace.startswith(("https://", "git@"))


def _validate_clone_branch(fragment: str) -> str:
    """
    Validate a ``#<branch>`` fragment as a clonable branch name.

    :param fragment: The fragment text after the first ``#``, e.g.
        ``"release-1.2"``.
    :returns: The validated branch name, unchanged.
    :raises ValueError: When the fragment is empty, is a commit SHA
        (detached HEAD — pin commits via git worktree options
        instead), or violates git ref-name rules.
    """
    if not fragment:
        raise ValueError("the '#' fragment must name a branch, e.g. '#main'")
    if _COMMIT_SHA_RE.fullmatch(fragment):
        raise ValueError(
            "the '#' fragment must be a branch, not a commit SHA — a commit "
            "checkout would leave the agent on a detached HEAD it cannot push"
        )
    if (
        any(c in _BRANCH_FORBIDDEN_CHARS or ord(c) < 0x20 for c in fragment)
        or fragment.startswith(("-", "/"))
        or fragment.endswith(("/", "."))
        or ".." in fragment
        or "@{" in fragment
    ):
        raise ValueError(f"'{fragment}' is not a valid git branch name")
    return fragment


def _derive_repo_name(url: str) -> str:
    """
    Derive the clone directory name from a repository URL.

    :param url: The fragment-stripped clone URL, e.g.
        ``"https://github.com/org/repo.git"``.
    :returns: The last path segment with ``.git`` stripped, e.g.
        ``"repo"``.
    :raises ValueError: When no usable name can be derived (empty
        path, or a name that is not filesystem-safe).
    """
    last = url.rstrip("/").split("/")[-1]
    # scp-style URLs with a single-segment path ("git@host:repo.git")
    # have no "/" after the colon — take what follows it.
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
    if not name or name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        raise ValueError(
            f"could not derive a repository directory name from '{url}' — "
            "the URL must end in the repository name, e.g. "
            "'https://github.com/org/repo'"
        )
    return name


def parse_repo_workspace(workspace: str) -> RepoWorkspace:
    """
    Parse and validate a managed session's repository-URL workspace.

    Grammar (Docker build-context style)::

        <repo>[#<branch>]
        <repo> := https://<host>/<path>  |  git@<host>:<path>

    The fragment splits on the FIRST ``#``; branches containing ``#``
    are not supported in this form. Fails loud on anything malformed
    so a bad workspace 422s at validation instead of surfacing as a
    mid-provision clone error.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo#release-1.2"``.
    :returns: The parsed, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL or branch fragment is malformed.
    """
    url, sep, fragment = workspace.partition("#")
    if any(ch.isspace() for ch in workspace):
        raise ValueError("a repository workspace must not contain whitespace")
    # Each workspace is stored verbatim in its own relaunch label, whose value
    # column is capped; reject an over-long one at parse so it can't be silently
    # truncated on write and relaunch/fork the wrong ref.
    if len(workspace) > LABEL_VALUE_MAX_LEN:
        raise ValueError(
            f"a repository workspace must be at most {LABEL_VALUE_MAX_LEN} characters "
            f"(got {len(workspace)}) — shorten the URL or branch"
        )
    # Reject embedded credentials (``user:token@host``) BEFORE any error that
    # echoes the URL: the token would otherwise be persisted verbatim in a
    # session label and the sandbox launch spec, and leak into the error/logs. A
    # well-formed URL of either form has no ``@`` after the scheme (https) or the
    # ``git@`` user (ssh), so any ``@`` in the remainder is embedded userinfo —
    # for both forms. The message must not echo the URL (it carries the secret);
    # connect the account so the clone authenticates via the credential broker.
    _cred_msg = (
        "a repository URL must not embed credentials (user:token@host) — "
        "connect the account instead of putting a token in the URL"
    )
    if url.startswith("https://"):
        rest = url[len("https://") :]
        if "@" in rest:
            raise ValueError(_cred_msg)
        host, slash, path = rest.partition("/")
        if not host or not slash or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable https repository URL — expected "
                "'https://<host>/<org>/<repo>'"
            )
    elif url.startswith("git@"):
        rest = url[len("git@") :]
        if "@" in rest:
            raise ValueError(_cred_msg)
        host, colon, path = rest.partition(":")
        if not host or not colon or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<org>/<repo>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<org>/<repo>' or 'git@<host>:<org>/<repo>'"
        )
    branch = _validate_clone_branch(fragment) if sep else None
    return RepoWorkspace(url=url, branch=branch, repo_name=_derive_repo_name(url))


def _docker_launcher_factory() -> Callable[[], HostLauncher]:
    """
    Build the launcher factory for the YAML ``provider: docker`` path.

    Needs no ``host.host:`` block: its supervisor URL and shared secret are read from the
    server PROCESS environment (12-factor) —
    :data:`~omnigent.hosts.docker.SUPERVISOR_URL_ENV_VAR` /
    :data:`~omnigent.hosts.docker.SUPERVISOR_TOKEN_ENV_VAR`.

    :returns: A factory producing :class:`~omnigent.hosts.docker.
        DockerHostLauncher` instances.
    """

    def _build() -> HostLauncher:
        """Construct the host launcher (lazy import inside)."""
        from omnigent.hosts.docker import DockerHostLauncher

        return DockerHostLauncher()

    return _build


def _community_sandbox_providers() -> frozenset[str]:
    """Provider names contributed by packages, excluding every built-in.

    The registry merges built-ins and entry-point contributions into one map,
    so the difference against :data:`SUPPORTED_HOST_PROVIDERS` is what
    isolates the contributed ones. Taking the difference rather than reaching
    for the registry's private built-in contribution keeps this to the
    registry's public API.

    Never raises: the registry records a broken plugin and carries on, and a
    server that refuses to start because somebody's optional package is
    malformed would be a worse failure than a provider that is simply absent.

    :returns: Contributed provider names, empty when none are installed.
    """
    from omnigent.hosts import registry as sandbox_registry

    return frozenset(sandbox_registry.available_providers()) - SUPPORTED_HOST_PROVIDERS


def _registry_token_ttl_s(provider: str) -> int:
    """The contributed provider's declared managed-token TTL, or the default.

    :param provider: A contributed provider name.
    :returns: Its ``managed_token_ttl_s``, falling back to
        :data:`DEFAULT_MANAGED_TOKEN_TTL_S` when it declares none.
    """
    from omnigent.hosts import registry as sandbox_registry

    meta = sandbox_registry.get_provider_metadata(provider)
    ttl = meta.managed_token_ttl_s if meta is not None else None
    return ttl if ttl is not None else DEFAULT_MANAGED_TOKEN_TTL_S


def _parse_registry_config(provider: str, section: dict[str, object] | None) -> None:
    """Validate a community provider's config block at parse time.

    Mirrors the built-ins: operator typos in ``host.<provider>`` stop
    server startup rather than surfacing as a runtime error on the first
    managed session.

    :param provider: A contributed provider name.
    :param section: The ``host.<provider>`` mapping, or ``None`` when
        the block is absent (accepted when no ``config_model`` is declared).
    :raises ValueError: When the block fails validation against the
        provider's declared ``config_model``.
    """
    from omnigent.hosts import registry as sandbox_registry

    meta = sandbox_registry.get_provider_metadata(provider)
    if meta is None or meta.config_model is None:
        return
    try:
        meta.config_model(**dict(section or {}))
    except Exception as exc:
        raise ValueError(
            f"server config 'host.{provider}' is invalid for provider '{provider}': {exc}"
        ) from exc


def _registry_launcher_factory(
    provider: str, config: dict[str, object] | None
) -> Callable[[], HostLauncher]:
    """Build the launcher factory for a contributed provider.

    Construction is deferred into the returned callable for the same reason
    every built-in factory defers it: the launcher's SDK import happens in its
    constructor, and a server whose config names a provider should still start
    when that provider's optional dependency is missing. The failure then
    surfaces on the launch that needs it, naming the provider.

    :param provider: A contributed provider name.
    :param config: The provider's ``host.<provider>`` block (already
        validated against ``config_model`` at parse time), or ``None`` when
        the block is absent.
    :returns: A factory producing that provider's launcher.
    """

    def _build() -> HostLauncher:
        """Construct the contributed launcher (lazy import inside)."""
        from omnigent.hosts import registry as sandbox_registry

        return sandbox_registry.instantiate(provider, config=config)

    return _build


def _unsupported_launcher_factory(provider: str) -> Callable[[], HostLauncher]:
    """
    Build a factory that rejects launch for a not-yet-supported provider.

    Lets a deployment stage ``hosts:`` config for a provider before
    managed-launch support lands: parsing succeeds, and the clear 400
    only surfaces if a managed session is actually requested.

    :param provider: The configured provider name, e.g. ``"docker"``.
    :returns: A factory that raises ``HTTPException`` 400 when called.
    """

    def _reject() -> HostLauncher:
        """Reject the launch with the provider named."""
        raise HTTPException(
            status_code=400,
            detail=(
                f"managed hosts are not yet supported for the "
                f"'{provider}' host provider — only "
                f"{', '.join(sorted(PROVIDERS_WITH_MANAGED_LAUNCH))} is implemented"
            ),
        )

    return _reject


_MAX_KEEP_WARM_S = 30 * 24 * 3600  # 30 days; a finite, sane ceiling on runner.idle_timeout_s


def _parse_keep_warm_s(raw: dict[str, object]) -> int | None:
    """Parse the top-level ``host.keep_warm_s`` knob (positive seconds), or None.

    The single sandbox timing knob: how long an idle sandbox stays warm
    (its runner alive) after the last turn before it suspends.
    """
    value = raw.get("keep_warm_s")
    if value is None:
        return None
    # It becomes runner.idle_timeout_s, where the runner does float(value): a
    # value rounding to 0 DISABLES the idle watchdog (never suspends), and an
    # oversized one (e.g. 10**400) overflows float() and stops the runner
    # starting. Require a whole number in [1, _MAX_KEEP_WARM_S] (30 days, far more
    # than any real keep-warm and safely finite). bool is an int subclass (reject
    # first); a float must be finite and integral (2.9 would silently truncate);
    # the range check runs on the int/float directly so a huge int never reaches
    # a float conversion here.
    _bad = "host.keep_warm_s must be a whole number of seconds, 1 to 2592000 (30 days)"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_bad)
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        raise ValueError(_bad)
    if not 1 <= value <= _MAX_KEEP_WARM_S:
        raise ValueError(_bad)
    return int(value)


def _apply_keep_warm(
    host_config: dict[str, object] | None, keep_warm_s: int | None
) -> dict[str, object] | None:
    """Map the ``keep_warm_s`` knob onto the in-sandbox ``runner.idle_timeout_s``.

    ``keep_warm_s`` is the single sandbox timing knob and is authoritative:
    it wins over an explicit ``host_config.runner.idle_timeout_s`` (logging when it
    overrides one) so there is one source of truth. A no-op when unset.
    """
    if keep_warm_s is None:
        return host_config
    merged = dict(host_config or {})
    existing = merged.get("runner")
    runner = dict(existing) if isinstance(existing, dict) else {}
    if runner.get("idle_timeout_s") not in (None, keep_warm_s):
        _logger.info(
            "host.keep_warm_s=%s overrides host_config.runner.idle_timeout_s=%s",
            keep_warm_s,
            runner.get("idle_timeout_s"),
        )
    runner["idle_timeout_s"] = keep_warm_s
    merged["runner"] = runner
    return merged


def deployment_with_inference_snapshot(
    deployment: HostDeployment, snapshot: dict[str, Any] | None
) -> HostDeployment:
    """Restore saved inference settings while retaining current sandbox lifecycle settings."""
    from omnigent.inference_config import snapshot_runtime_config

    runtime = snapshot_runtime_config(snapshot)
    if runtime is None:
        if not any((entry.host_config or {}).get("inference") for entry in deployment.configs):
            return deployment
        runtime = {"inference": {}}
    return replace(
        deployment,
        configs=tuple(
            replace(entry, host_config={**(entry.host_config or {}), **(runtime or {})})
            for entry in deployment.configs
        ),
    )


def _parse_host_config(raw: dict[str, object]) -> dict[str, object] | None:
    """
    Extract and validate the top-level ``host.host_config`` block.

    Verbatim in-sandbox ``~/.omnigent/config.yaml`` content forwarded at
    managed launch (see :class:`HostProviderConfig`). When a
    ``providers`` key is present, its SHAPE is validated through the same
    parser ``omnigent`` itself uses — structurally only: secret
    references (``api_key_ref: env:VAR``) name variables in the
    SANDBOX's environment, not the server's, so they are deliberately
    never resolved here. Validating at parse time matters doubly for
    this block: inside the sandbox a malformed ``providers`` entry
    degrades silently (the harness falls back to its own login), so
    server startup is the only place a typo can fail loud.

    :param raw: The raw ``host`` mapping.
    :returns: The validated ``host_config`` mapping, or ``None`` when
        the key is absent.
    :raises ValueError: When present but not a mapping, or when its
        ``providers`` block fails shape validation.
    """
    host_config = raw.get("host_config")
    if host_config is None:
        return None
    if not isinstance(host_config, dict):
        raise ValueError(
            "server config 'host.host_config' must be a mapping — verbatim "
            "in-sandbox ~/.omnigent/config.yaml content merged in before "
            "'omnigent host' starts"
        )
    # Key presence, not get(): an explicit `providers: null` would skip
    # validation here yet still ride to the sandbox, where the merge writes
    # `providers: null` over any existing block — the silent degradation this
    # parse exists to prevent.
    from omnigent.inference_config import parse_inference_config, validate_inference_credentials

    if parse_inference_config(host_config):
        validate_inference_credentials(host_config)
    if "providers" in host_config:
        providers = host_config["providers"]
        # load_providers silently ignores a non-mapping providers value, so
        # the mapping check must happen here to fail loud.
        if not isinstance(providers, dict):
            raise ValueError("server config 'host.host_config.providers' must be a mapping")
        # Lazy imports, matching the provider branches below: the parse path
        # must not pull the onboarding layer in at module import time.
        from omnigent.errors import OmnigentError
        from omnigent.onboarding.provider_config import get_default_provider, load_providers

        try:
            parsed_providers = load_providers(host_config)
            default_scopes = {
                scope
                for provider in parsed_providers.values()
                for scope in provider.default_families
            }
            for scope in sorted(default_scopes):
                get_default_provider(host_config, scope)
        except OmnigentError as exc:
            raise ValueError(
                f"server config 'host.host_config.providers' is invalid: {exc}"
            ) from exc
        for provider in parsed_providers.values():
            for family_name, family in provider.families.items():
                if family.api_key is not None:
                    raise ValueError(
                        "server config "
                        f"'host.host_config.providers.{provider.name}."
                        f"{family_name}.api_key' must not contain an inline API key — "
                        "use api_key_ref: env:VAR instead"
                    )
    # The block rides json.dumps to the sandbox on every launch, and
    # yaml.safe_load produces values json can't take (an unquoted date
    # becomes datetime.date) — round-trip now so that fails startup, not
    # every launch.
    import json

    try:
        serialized = json.dumps(host_config)
        round_tripped = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"server config 'host.host_config' must be JSON-serializable "
            f"(quote YAML scalars like dates): {exc}"
        ) from exc
    if round_tripped != host_config:
        raise ValueError(
            "server config 'host.host_config' must be JSON-serializable without loss "
            "(mapping keys must be strings and values must preserve their JSON types)"
        )
    return host_config


def parse_host_section(raw: object) -> HostDeployment | None:
    """
    Parse and validate the server config's ``hosts:`` section.

    Fails loud on malformed config (an operator typo should stop server
    startup, not surface as a runtime 502 on the first managed session).

    Takes either a scalar ``provider:`` (one provider) or a ``providers:``
    list (several, offered side by side), never both. ``server_url`` /
    ``host_config`` stay top-level and apply to every entry::

        host:
          server_url: https://omnigent.example.com
          providers:
            - provider: docker

    :param raw: The raw ``host`` value from the server config YAML.
        ``None`` when the section is absent.
    :returns: The parsed deployment, or ``None`` when *raw* is ``None``
        (managed hosts not configured).
    :raises ValueError: When the section is present but malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("server config 'host' must be a mapping")
    reaper = _parse_managed_sandbox_reaper_config(raw)
    if "providers" in raw:
        return _parse_multi_provider_sandbox_config(raw, reaper=reaper)
    return HostDeployment.single(
        _parse_single_provider_sandbox_config(raw),
        reaper=reaper,
    )


def _parse_managed_sandbox_reaper_config(
    raw: dict[str, object],
) -> HostReaperConfig:
    """Parse the deployment-wide ``host.reaper`` block."""
    reaper_raw = raw.get("reaper")
    if reaper_raw is None:
        return HostReaperConfig()
    if not isinstance(reaper_raw, dict):
        raise ValueError("server config 'host.reaper' must be a mapping")
    _reject_unknown_keys(
        reaper_raw,
        {"enabled", "terminate_after_offline_days", "sweep_interval_s"},
        "host.reaper",
    )
    enabled = reaper_raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("server config 'host.reaper.enabled' must be a boolean")
    terminate_after_offline_days = reaper_raw.get("terminate_after_offline_days", 30)
    if (
        not isinstance(terminate_after_offline_days, int)
        or isinstance(terminate_after_offline_days, bool)
        or terminate_after_offline_days <= 0
    ):
        raise ValueError(
            "server config 'host.reaper.terminate_after_offline_days' must be a positive integer"
        )
    sweep_interval_s = reaper_raw.get("sweep_interval_s", 24 * 60 * 60)
    if (
        not isinstance(sweep_interval_s, int)
        or isinstance(sweep_interval_s, bool)
        or sweep_interval_s <= 0
    ):
        raise ValueError("server config 'host.reaper.sweep_interval_s' must be a positive integer")
    return HostReaperConfig(
        enabled=enabled,
        terminate_after_offline_days=terminate_after_offline_days,
        sweep_interval_s=sweep_interval_s,
    )


def _parse_multi_provider_sandbox_config(
    raw: dict[str, object],
    *,
    reaper: HostReaperConfig,
) -> HostDeployment:
    """
    Parse the ``host.providers`` list shape into a deployment.

    Entries go through the single-provider parser with the shared
    top-level keys folded in, so a provider block validates identically
    in either shape.

    :param raw: The raw ``host`` mapping, containing ``providers``.
    :returns: A deployment holding one config per listed provider.
    :raises ValueError: When the list or any entry is malformed.
    """
    if "provider" in raw:
        raise ValueError(
            "server config 'host' must set either 'provider' (one provider) "
            "or 'providers' (several), not both"
        )
    entries = raw.get("providers")
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            "server config 'host.providers' must be a non-empty list of "
            "provider mappings, e.g. [{provider: docker}]"
        )
    # Shared keys describe THIS server, so they ride into every entry
    # instead of being repeated per entry.
    shared = {
        key: value for key, value in raw.items() if key not in {"providers", "provider", "reaper"}
    }
    parsed: list[HostProviderConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"server config 'host.providers[{index}]' must be a mapping "
                "naming a provider, e.g. {provider: docker}"
            )
        if "providers" in entry:
            raise ValueError(f"server config 'host.providers[{index}]' must not nest 'providers'")
        if "reaper" in entry:
            raise ValueError(
                "server config 'host.reaper' is deployment-wide and must be set "
                "next to 'providers', not inside a provider entry"
            )
        config = _parse_single_provider_sandbox_config({**shared, **entry})
        # Always a str: the single-provider parser rejects anything else.
        name = str(config.provider)
        if name in seen:
            raise ValueError(f"server config 'host.providers' lists {name!r} more than once")
        seen.add(name)
        parsed.append(config)
    return HostDeployment(configs=tuple(parsed), reaper=reaper)


def _parse_git_clone_options(raw: object) -> GitCloneOptions:
    if raw is None:
        return GitCloneOptions()
    if not isinstance(raw, dict):
        raise ValueError("server config 'host.git_clone' must be a mapping")
    if raw.keys() - {"depth", "single_branch", "filter", "tags"}:
        raise ValueError("host.git_clone only accepts depth, single_branch, filter, and tags")
    return GitCloneOptions(**raw)


# Env var names only ever look like this — matches the shell convention every
# example in this codebase already follows (ANTHROPIC_API_KEY, SUPERCHAT_CLAUDE_MODEL, …).
_RUNNER_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _parse_runner_env(raw: dict[str, object]) -> tuple[str, ...]:
    """
    Parse the top-level ``host.runner_env`` list of variable NAMES.

    Names only — this never touches values, so there is nothing here that could leak a
    secret into a log line. Values are resolved from this server process's own environment
    at launch time (see :func:`_resolve_runner_env`), never from this parsed config.

    :param raw: The raw ``host`` mapping.
    :returns: The validated names, in the order given (duplicates removed, first occurrence
        kept), or ``()`` when the key is absent.
    :raises ValueError: When present but not a list of upper-snake-case strings.
    """
    value = raw.get("runner_env")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(
            "server config 'host.runner_env' must be a list of environment variable "
            "names, e.g. [ANTHROPIC_API_KEY, OPENROUTER_API_KEY]"
        )
    seen: dict[str, None] = {}
    for name in value:
        if not _RUNNER_ENV_NAME_RE.match(name):
            raise ValueError(
                f"server config 'host.runner_env' entry {name!r} is not a valid "
                "environment variable name (expected UPPER_SNAKE_CASE)"
            )
        seen[name] = None
    return tuple(seen)


def _resolve_runner_env(names: Sequence[str]) -> dict[str, str]:
    """
    Resolve ``host.runner_env`` names against THIS SERVER PROCESS's own environment.

    Only names that are actually set are forwarded — an operator may list a variable that's
    optional in some deployments (e.g. ``OPENROUTER_API_KEY`` when OpenRouter isn't configured),
    and silently skipping an absent one is the right behavior, not an error. Never logs
    values: the names are already non-secret (see :attr:`HostProviderConfig.runner_env`),
    and nothing here ever formats the resolved value into a message.

    :param names: Validated variable names from :func:`_parse_runner_env`.
    :returns: ``{name: value}`` for every name present in ``os.environ``.
    """
    return {name: value for name in names if (value := os.environ.get(name)) is not None}


def _parse_single_provider_sandbox_config(raw: dict[str, object]) -> HostProviderConfig:
    """
    Parse one provider's ``host`` mapping (the scalar ``provider:`` shape).

    :param raw: The raw ``host`` mapping, with a scalar ``provider``.
    :returns: The parsed single-provider config.
    :raises ValueError: When the mapping is malformed.
    """
    provider = raw.get("provider")
    community = _community_sandbox_providers()
    if not isinstance(provider, str) or (
        provider not in SUPPORTED_HOST_PROVIDERS and provider not in community
    ):
        supported = ", ".join(sorted(SUPPORTED_HOST_PROVIDERS | community))
        raise ValueError(
            f"server config 'host.provider' must be one of: {supported} (got {provider!r})"
        )
    git_clone = _parse_git_clone_options(raw.get("git_clone"))
    runner_env = _parse_runner_env(raw)
    server_url = raw.get("server_url")
    if not isinstance(server_url, str) or not server_url.strip():
        raise ValueError(
            "server config 'host.server_url' is required — the public URL "
            "of this server that sandboxed hosts connect back to"
        )
    # Validated regardless of provider (like server_url): a malformed
    # host_config should stop startup even for staged/unsupported providers.
    host_config = _parse_host_config(raw)
    model_discovery = raw.get("model_discovery", {})
    if not isinstance(model_discovery, dict):
        raise ValueError("host.model_discovery must be a mapping")
    for name, discovery in model_discovery.items():
        if not isinstance(discovery, dict) or set(discovery) - {
            "base_url",
            "api_key_ref",
            "auth_command",
            "family",
        }:
            raise ValueError(f"Invalid host.model_discovery entry {name!r}")
        if not isinstance(discovery.get("base_url"), str):
            raise ValueError(f"host.model_discovery.{name} requires base_url")
        if not (discovery.get("api_key_ref") or discovery.get("auth_command")):
            raise ValueError(f"host.model_discovery.{name} requires a credential reference")
    from omnigent.inference_config import validate_inference_credentials

    validate_inference_credentials({}, model_discovery)
    host_config = _apply_keep_warm(host_config, _parse_keep_warm_s(raw))
    if provider == "docker":
        launcher_factory = _docker_launcher_factory()
        token_ttl_s = HOST_TOKEN_TTL_S
    elif provider in community:
        # A provider contributed through the `omnigent.host_providers`
        # entry point group. The registry has validated it (name not shadowing
        # a built-in, launcher class under `omnigent.community.host.*`) at
        # import time, so this only has to validate the config block and build
        # the factory.
        #
        # Gated on `community`, which excludes anything in
        # SUPPORTED_HOST_PROVIDERS, so a contributed provider never shadows
        # a built-in.
        section = _parse_provider_section(raw, provider)
        _parse_registry_config(provider, section)
        launcher_factory = _registry_launcher_factory(provider, section)
        token_ttl_s = _registry_token_ttl_s(provider)
    else:
        launcher_factory = _unsupported_launcher_factory(provider)
        # Never consulted (the factory rejects before any token is
        # minted); the default TTL keeps the field total.
        token_ttl_s = DEFAULT_MANAGED_TOKEN_TTL_S
    return HostProviderConfig(
        server_url=server_url.strip().rstrip("/"),
        launcher_factory=launcher_factory,
        token_ttl_s=token_ttl_s,
        managed_launch_supported=(
            provider in PROVIDERS_WITH_MANAGED_LAUNCH or provider in community
        ),
        provider=provider,
        host_config=host_config,
        model_discovery=model_discovery,
        git_clone=git_clone,
        runner_env=runner_env,
    )


def _reject_unknown_keys(mapping: dict[str, object], allowed: set[str], path: str) -> None:
    """
    Fail loud on any key outside *allowed* — catches typos and misplaced keys
    (e.g. a misspelled ``enabeld``) that would otherwise be silently ignored and
    surface much later as a confusing runtime failure.
    """
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(
            f"server config '{path}' has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _parse_provider_section(raw: dict[str, object], provider: str) -> dict[str, object] | None:
    """
    Extract a provider-specific optional config block.

    :param raw: The raw ``host`` mapping.
    :param provider: Provider block name, e.g. ``"docker"``.
    :returns: The provider mapping, or ``None`` when omitted.
    :raises ValueError: When the block is present but not a mapping.
    """
    section = raw.get(provider)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError(f"server config 'host.{provider}' must be a mapping")
    return section


def _select_provider_config(
    deployment: HostDeployment,
    provider: str | None,
) -> HostProviderConfig:
    """
    Narrow a deployment to the one provider config a launch runs on.

    :param deployment: The deployment's offered providers.
    :param provider: Requested provider short name, or ``None`` for the
        deployment default.
    :returns: The single-provider config to launch with.
    :raises HTTPException: 400 when *provider* is not configured.
    """
    selected = deployment.for_provider(provider)
    if selected is None:
        offered = ", ".join(deployment.launchable_providers()) or "none"
        raise HTTPException(
            status_code=400,
            detail=(
                f"host provider '{provider}' is not configured on this "
                f"server — available: {offered}"
            ),
        )
    return selected


async def launch_host(
    *,
    config: HostDeployment,
    owner: str,
    host_store: HostStore,
    repos: Sequence[RepoWorkspace] = (),
    provider: str | None = None,
    agent_name: str | None = None,
    labels: Mapping[str, str] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> HostLaunch:
    """
    Provision a sandbox, start a host in it, and wait until it registers.

    Sequence: provision sandbox → pre-register the host row with its
    launch-token digest (so the credential resolves by the time the
    host dials the tunnel) → clone the requested repositories → start
    ``omnigent host`` inside the sandbox with the token + identity in its
    environment → poll the hosts table until the host is online. Any
    failure after provisioning terminates the sandbox and deletes the
    host row (which revokes the token) before re-raising.

    :param config: The deployment's offered providers (YAML-parsed or
        wrapped around a directly-constructed embedding config).
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on single-user servers).
    :param host_store: Persistent host registrations — receives the
        pre-registered host row and is polled for the sandbox host
        coming online.
    :param repos: Parsed repository-URL workspaces to clone into the
        sandbox (empty for an empty workspace). One repo becomes the
        session's working directory; several are cloned as siblings and
        the working directory is the parent that holds them. Private
        repositories authenticate via the host image's git credential
        helper when the sandbox env carries ``GIT_TOKEN`` (forwarded via
        ``host.runner_env``).
    :param provider: Which configured provider to launch on, e.g.
        ``"docker"``. ``None`` takes the deployment's default (first)
        provider — what a request that names none gets.
    :param agent_name: Server-resolved built-in agent name the session runs,
        stamped on the runner by providers that declare
        ``classifies_runner_by_agent``,
        or ``None`` to leave it unstamped.
    :param labels: The launching session's labels, forwarded to
        :meth:`~omnigent.hosts.launcher.HostLauncher.prepare_for_launch` —
        e.g. Superchat's ``omnigent.host.owner`` / ``omnigent.tenant`` (see
        :mod:`omnigent.hosts.docker`), which a provider whose sandbox is a
        pre-existing per-identity resource (rather than one it creates fresh) needs to select
        it. ``None``/empty for providers that don't need it.
    :param on_stage: Progress observer invoked as the launch pipeline
        advances, with the stage just entered: ``"cloning"`` (when
        *repo* is set) then ``"starting"``. May be called from a
        worker thread (the sandbox exec steps run via
        ``asyncio.to_thread``), so it must be thread-safe. ``None``
        disables progress reporting.
    :returns: The registered host id + in-sandbox workspace path
        (the cloned repository directory when *repo* is set).
    :raises HTTPException: 400 when *provider* is not configured or the
        selected provider lacks managed-launch support; 502 when
        provisioning, cloning, host startup, or registration fails.
    """
    entry = _select_provider_config(config, provider)
    launcher = entry.launcher_factory()
    repos = _apply_git_clone_options(launcher, repos, entry.git_clone)
    host_id = uuid.uuid4().hex
    # Visible label in the host picker; (owner, name) is the hosts
    # table PK, so embed the host_id's leading hex for uniqueness
    # across a user's managed sandboxes.
    host_name = f"managed-{host_id[:8]}"
    try:
        await asyncio.to_thread(launcher.prepare_for_launch, agent_name=agent_name, labels=labels)
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host_name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox launch failed: {exc.message}",
        ) from exc
    workspace = await _register_and_start_host(
        launcher=launcher,
        config=entry,
        host_store=host_store,
        host_id=host_id,
        host_name=host_name,
        owner=owner,
        sandbox_id=sandbox_id,
        repos=repos,
        agent_name=agent_name,
        on_stage=on_stage,
    )
    return HostLaunch(host_id=host_id, workspace=workspace)


async def relaunch_host(
    *,
    config: HostDeployment,
    host: Host,
    host_store: HostStore,
    repos: Sequence[RepoWorkspace] = (),
    agent_name: str | None = None,
    labels: Mapping[str, str] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> HostLaunch:
    """
    Provision a NEW sandbox generation for an existing managed host.

    The host identity is durable while its sandbox is not: when the
    sandbox dies (the provider's lifetime cap, a crash, a manual
    terminate), the host row and the sessions bound to it remain.
    This relaunch keeps that identity — terminate the old sandbox
    (best-effort; it is usually already gone), provision a fresh one,
    and re-arm the SAME host row with a new token + sandbox id (which
    atomically revokes the previous generation's token).

    The new sandbox starts from the image — workspace contents of the
    dead generation are gone. Passing *repos* re-clones the session's
    repositories so the workspace is restored to its create-time state.

    Unlike a first launch, a failure here keeps the host row (only the
    new sandbox is torn down and the armed token revoked), so the
    session binding survives and a later attempt can retry.

    :param config: The deployment's offered providers.
    :param host: The existing managed host row to relaunch
        (``sandbox_provider`` set; callers guard on that).
    :param host_store: Persistent host registrations.
    :param repos: Repositories to re-clone as the workspace (empty for an
        empty workspace), matching the create-time selection.
    :param agent_name: Server-resolved built-in agent name the session runs,
        re-stamped on the new runner by launchers that declare
        ``classifies_runner_by_agent``, or ``None`` to leave it unstamped.
    :param labels: The bound session's current labels, forwarded to
        ``prepare_for_launch`` alongside ``host.sandbox_id`` as
        ``previous_sandbox_id`` — a provider whose sandbox id encodes identity
        (see :mod:`omnigent.hosts.docker`) can recover from
        either; *labels*, when available, reflects the current session row
        rather than whatever an earlier launch encoded.
    :param on_stage: Progress observer forwarded to
        :func:`_register_and_start_host`; see :func:`launch_host`.
        ``None`` disables progress reporting.
    :returns: The (unchanged) host id + fresh in-sandbox workspace.
    :raises HTTPException: 400 when the host's recorded provider no
        longer matches the configured launcher; 502 when
        provisioning, cloning, host startup, or registration fails.
    """
    launcher = _launcher_for_teardown(host, config)
    if launcher is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the '{host.sandbox_provider}' host provider this host "
                "was launched with is no longer configured on this server"
            ),
        )
    # Stay on the host's provider so the new generation is armed with ITS
    # token TTL, not the deployment default's.
    entry = config.recorded(host.sandbox_provider)
    repos = _apply_git_clone_options(launcher, repos, entry.git_clone)
    # The old generation is normally already dead (that is why we are
    # here), but terminate defensively so a transient tunnel outage
    # can never leave two live sandboxes claiming one host identity.
    if host.sandbox_id is not None:
        await _terminate_sandbox_best_effort(
            launcher,
            host.sandbox_id,
            host_id=host.host_id,
            provider=host.sandbox_provider,
        )
    try:
        await asyncio.to_thread(
            launcher.prepare_for_launch,
            agent_name=agent_name,
            labels=labels,
            previous_sandbox_id=host.sandbox_id,
        )
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host.name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox relaunch failed: {exc.message}",
        ) from exc
    try:
        workspace = await _register_and_start_host(
            launcher=launcher,
            config=entry,
            host_store=host_store,
            host_id=host.host_id,
            host_name=host.name,
            owner=host.user_id,
            sandbox_id=sandbox_id,
            repos=repos,
            agent_name=agent_name,
            on_stage=on_stage,
            keep_host_on_failure=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"managed sandbox relaunch conflicted with host lifecycle: {exc}",
        ) from exc
    return HostLaunch(host_id=host.host_id, workspace=workspace)


def _apply_git_clone_options(
    launcher: HostLauncher,
    repos: Sequence[RepoWorkspace],
    options: GitCloneOptions,
) -> Sequence[RepoWorkspace]:
    """Apply admin clone policy before provisioning or replacing any sandbox."""
    if options == GitCloneOptions():
        return repos
    if not launcher.capabilities.git_clone_options:
        raise HTTPException(
            status_code=400,
            detail=(
                f"host provider '{launcher.provider}' does not support host.git_clone "
                "options; remove the block or use a provider that supports them"
            ),
        )
    return [replace(repo, git_clone=options) for repo in repos]


async def _start_sandbox_host(
    launcher: HostLauncher,
    sandbox_id: str,
    *,
    token: str,
    host_id: str,
    host_name: str,
    server_url: str,
    repos: Sequence[RepoWorkspace],
    host_config: dict[str, object] | None,
    agent_name: str | None = None,
    on_stage: Callable[[str], None] | None = None,
    runner_env: Mapping[str, str] | None = None,
) -> str:
    """Start a host without sending absent optional arguments to legacy launchers."""
    # Gated on the capability, not on the value: start_host is side-effecting and
    # non-idempotent, so we never probe the signature by passing then retrying.
    # `agent_name` is declared on the classifying launcher's `start_host` alone,
    # so the abstract signature does not carry it and the call is cast: the
    # capability is the runtime guarantee the static type cannot express. A
    # launcher declaring it is in-tree and current, so it also takes
    # `host_config`/`on_stage`, keeping the legacy-omission fan-out below
    # one-dimensional.
    if agent_name is not None and launcher.capabilities.classifies_runner_by_agent:
        start_classified = cast(Callable[..., str], launcher.start_host)
        return await asyncio.to_thread(
            start_classified,
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=server_url,
            repos=repos,
            host_config=host_config,
            on_stage=on_stage,
            agent_name=agent_name,
        )
    # Every other optional kwarg is sent only when it carries a real value, so a legacy
    # launcher whose `start_host` override never added it (and therefore doesn't accept it)
    # sees the exact same call it always has. `runner_env` is the newest of these — only the
    # exec-model default (`ExecModelHostLauncher.start_host`) accepts it today.
    kwargs: dict[str, object] = {}
    if host_config is not None:
        kwargs["host_config"] = host_config
    if on_stage is not None:
        kwargs["on_stage"] = on_stage
    if runner_env:
        kwargs["runner_env"] = runner_env
    return await asyncio.to_thread(
        launcher.start_host,
        sandbox_id,
        token=token,
        host_id=host_id,
        host_name=host_name,
        server_url=server_url,
        repos=repos,
        **kwargs,
    )


async def _register_and_start_host(
    *,
    launcher: HostLauncher,
    config: HostProviderConfig,
    host_store: HostStore,
    host_id: str,
    host_name: str,
    owner: str,
    sandbox_id: str,
    repos: Sequence[RepoWorkspace] = (),
    agent_name: str | None = None,
    on_stage: Callable[[str], None] | None = None,
    keep_host_on_failure: bool = False,
) -> str:
    """
    Arm the credential, start the in-sandbox host, and await its
    registration — tearing the sandbox down on any failure.

    The credential is registered BEFORE the host process starts, so
    the token is resolvable by the time the host first dials the
    tunnel. A failure in any later step terminates the sandbox and
    revokes the armed token before re-raising — by deleting the host
    row (first launch: the row would otherwise be an unusable picker
    ghost) or, on a relaunch, by clearing the credential columns only
    (the durable row keeps the session binding alive for a retry).

    :param launcher: The launcher holding the provisioned sandbox.
    :param config: The deployment's sandbox config.
    :param host_store: Persistent host registrations.
    :param host_id: Server-chosen host identity, e.g.
        ``"host_a1b2c3d4..."``.
    :param host_name: Server-chosen host display name, e.g.
        ``"managed-a1b2c3d4"``.
    :param owner: User the managed host acts for, e.g.
        ``"alice@example.com"``.
    :param sandbox_id: The provisioned sandbox, e.g. ``"sb-a1b2c3"``.
    :param repos: Repositories to clone into the workspace (empty for an
        empty workspace). One repo → the agent's cwd is that clone dir;
        several → the workspace root that parents them all.
    :param agent_name: Server-resolved built-in agent name the session runs,
        forwarded to ``start_host`` only for launchers that declare
        ``classifies_runner_by_agent``. ``None`` leaves it unstamped.
    :param on_stage: Progress observer forwarded to the launcher's
        ``start_host``; see :func:`launch_host`. ``None``
        disables progress reporting.
    :param keep_host_on_failure: ``True`` on a relaunch — failure
        cleanup terminates the new sandbox and revokes the token but
        keeps the host row. ``False`` (first launch) deletes the row.
    :returns: The absolute in-sandbox workspace path.
    :raises HTTPException: 502 when cloning, host startup, or
        registration fails.
    """
    token = secrets.token_urlsafe(32)
    record = None
    try:
        if keep_host_on_failure:
            record = await asyncio.to_thread(
                host_store.replace_managed_host_sandbox,
                host_id=host_id,
                user_id=owner,
                token=token,
                provider=launcher.provider,
                sandbox_id=sandbox_id,
                token_expires_at=now_epoch() + config.token_ttl_s,
            )
            if record is None:
                raise ValueError(f"managed host {host_id!r} no longer exists")
        else:
            record = await asyncio.to_thread(
                host_store.register_managed_host,
                host_id=host_id,
                name=host_name,
                user_id=owner,
                token=token,
                provider=launcher.provider,
                sandbox_id=sandbox_id,
                token_expires_at=now_epoch() + config.token_ttl_s,
            )
        # Uniform across providers: provision() fixed the sandbox id and the
        # token was armed against it above, so start_host starts the host with
        # a token that already resolves. The exec-model default execs in; an
        # entrypoint-as-host launcher boots a sandbox running the host.
        workspace = await _start_sandbox_host(
            launcher,
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=config.server_url,
            repos=repos,
            host_config=config.host_config,
            agent_name=agent_name,
            on_stage=on_stage,
            runner_env={**_resolve_runner_env(config.runner_env), **eval_runner_env(owner)},
        )
        await _wait_for_host_online(host_store, host_id)
    except Exception as exc:
        # The provider allocated the sandbox before registration or startup
        # could fail, so both failures must attempt cleanup.
        if record is None:
            current = None
            try:
                current = await asyncio.to_thread(host_store.get_host, host_id)
            except Exception:
                _logger.exception("Could not inspect failed host registration")
            # A provider can reuse an ID already retained for active work or cleanup.
            if (
                current is None
                or current.sandbox_provider != launcher.provider
                or sandbox_id
                not in (
                    current.sandbox_id,
                    current.terminating_sandbox_id,
                )
            ):
                await _terminate_sandbox_best_effort(
                    launcher, sandbox_id, host_id=host_id, provider=launcher.provider
                )
            if isinstance(exc, ValueError):
                raise
        elif keep_host_on_failure:
            await _terminate_sandbox_best_effort(
                launcher,
                sandbox_id,
                host_id=record.host_id,
                provider=record.sandbox_provider,
            )
            await asyncio.to_thread(host_store.revoke_launch_token, host_id)
        else:
            # The row was just armed with THIS single-provider config, so a
            # one-provider deployment tears it back down with the same launcher.
            await terminate_host(record, host_store, HostDeployment.single(config))
        if isinstance(exc, HTTPException):
            raise
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox host startup failed: {message}",
        ) from exc
    return workspace


async def _wait_for_host_online(host_store: HostStore, host_id: str) -> None:
    """
    Poll the hosts table until the sandbox host registers, or time out.

    :param host_store: Persistent host registrations.
    :param host_id: The launched host's identifier.
    :raises HTTPException: 502 when the host does not come online
        within :data:`HOST_ONLINE_TIMEOUT_S`.
    """
    deadline = time.monotonic() + HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if await asyncio.to_thread(host_store.is_online, host_id):
            return
        await asyncio.sleep(_ONLINE_POLL_INTERVAL_S)
    raise HTTPException(
        status_code=502,
        detail=(
            f"managed host did not come online within "
            f"{HOST_ONLINE_TIMEOUT_S}s — check {_HOST_LOG_PATH} "
            "inside the sandbox"
        ),
    )


def _launcher_for_teardown(
    host: Host,
    config: HostDeployment | None,
) -> HostLauncher | None:
    """
    Resolve the launcher that can terminate a managed host's sandbox.

    The deployment's CURRENT launcher factory is only usable when its
    provider matches the provider recorded on the host row at launch —
    a config change between launch and teardown must not aim a
    different provider's terminate at a stale sandbox id.

    The row's recorded provider picks which config to build from: a host
    must be torn down by the provider that launched it, never by
    whichever is configured first.

    :param host: The managed host being torn down.
    :param config: The deployment's current sandbox config, or ``None``
        when the ``hosts:`` section has been removed since launch.
    :returns: A launcher whose provider matches the row, or ``None``
        when no matching launcher is available.
    """
    if config is None:
        return None
    entry = config.recorded(host.sandbox_provider)
    try:
        launcher = entry.launcher_factory()
    except HTTPException:
        # The YAML path's unsupported-provider factory raises; there is
        # no launcher to terminate with.
        return None
    if launcher.provider != host.sandbox_provider:
        return None
    return launcher


def screen_target_for_host(
    host: Host,
    config: HostDeployment | None,
) -> tuple[HostLauncher, str] | None:
    """
    Resolve the launcher and sandbox id that can show a managed host's screen.

    :param host: The managed host a session is bound to.
    :param config: The deployment's current sandbox config, or ``None``.
    :returns: ``(launcher, sandbox_id)`` when the host's provider still matches the
        deployment and declares :attr:`HostCapabilities.screen`; otherwise ``None``.
    """
    if host.sandbox_id is None:
        return None
    launcher = _launcher_for_teardown(host, config)
    if launcher is None or not launcher.capabilities.screen:
        return None
    return launcher, host.sandbox_id


def host_resume_supported(
    host: Host,
    config: HostDeployment | None,
) -> bool:
    """
    Whether :func:`resume_host` could wake this host in place.

    ``True`` iff the host is bound to a sandbox whose provider has a
    stop/resume lifecycle with a persistent volume
    (:attr:`ExecModelHostLauncher.can_resume`) and still matches the deployment's
    current launcher. This is the SAME gate :func:`resume_host`
    applies before a wake, exposed so the open-session snapshot
    (``SessionResponse.host_resumable``) can render a dormant such host as a
    wakeable "asleep" state instead of the terminal ``host_offline``
    dead-end.

    :param host: The session's bound managed host.
    :param config: The deployment's current sandbox config, or ``None``
        when the ``hosts:`` section has been removed since launch.
    :returns: ``True`` when a wake would be attempted; ``False`` for a
        non-managed / non-resumable provider, a dropped config, or a
        host with no recorded ``sandbox_id``.
    """
    launcher = _launcher_for_teardown(host, config)
    return (
        launcher is not None
        and launcher.capabilities.resume_stopped
        and host.sandbox_id is not None
    )


def host_sandbox_is_running(
    host: Host,
    config: HostDeployment | None,
) -> bool | None:
    """
    Ask the matched provider whether this managed host's sandbox is running.

    ``None`` means the provider has no cheap status hook or the deployment no
    longer matches the host's provider. Callers should treat that as unknown
    and fall back to Omnigent liveness checks.
    """
    launcher = _launcher_for_teardown(host, config)
    if launcher is None or host.sandbox_id is None:
        return None
    return launcher.is_running(host.sandbox_id)


# ── Managed-host wake (resume a dormant host on demand) ─────────────────────

# Per-host resume single-flight: one in-flight resume per host_id on this
# replica, else two host processes flap the tunnel registration. Reused across a
# host's many idle-stop/resume cycles, so not reaped — a .pop() could also race
# a resume still holding it; one idle Lock per host woken is negligible.
# custom-lint: disable-next=workspace-scoped-cache -- host_id lock; collision only serializes
_resume_locks: dict[str, asyncio.Lock] = {}


async def resume_host(
    host_id: str,
    host_store: HostStore,
    config: HostDeployment | None,
    *,
    force: bool = False,
    on_stage: Callable[[str], None] | None = None,
    agent_name: str | None = None,
) -> None:
    """
    Wake a dormant managed host so a session bound to it can run again.

    The send-message relaunch path calls this when a host-bound session has no
    live runner. If the host is a *resumable* managed host — a provider whose
    sandbox idle-stops but retains its identity
    (:attr:`ExecModelHostLauncher.can_resume`) — and is currently offline, this
    resumes the sandbox under the SAME sandbox id, re-arms its launch token,
    re-execs ``omnigent host``, and waits for it to re-register. The caller's
    existing relaunch then spawns a fresh runner.

    No-op when the host is already online, is unknown, or its provider cannot
    resume (the caller falls through to its normal host-offline
    behavior, i.e. the user starts a new session). ``force=True`` is reserved
    for the route path that has already proven this server process has no live
    host tunnel even though the cross-replica DB row is still fresh.
    Single-flight and idempotent: concurrent callers serialize on a per-host
    lock and re-check liveness under it, so only the first wakes the host.

    Unlike a launch, a failed wake does NOT tear the sandbox down — the volume
    + workspace are the user's and must survive for a retry.

    :param host_id: The session's bound host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations (cross-replica liveness).
    :param config: The deployment's managed-sandbox config, or ``None`` when
        the ``hosts:`` section has been removed since launch.
    :param force: Skip the DB-liveness no-op gate when the caller has local
        evidence that the tunnel is gone.
    :param on_stage: Progress observer forwarded to the launcher's
        ``start_host`` (via :func:`_start_sandbox_host`), so a wake reports the
        launch-pipeline ``"starting"`` stage to the caller's progress surface
        exactly like a fresh launch (:func:`_register_and_start_host`) — without it a
        wake shows a single frozen ``"provisioning"`` band for its whole
        duration. ``None`` disables progress reporting.
    :param agent_name: Built-in agent classifier to re-stamp on the woken
        runner, or ``None`` to leave it unstamped. A wake rebuilds the runner
        from scratch, so the classifier is not carried over by the resume: the
        caller re-derives it through the same built-in gate a launch uses.
    :raises HostGoneError: When the sandbox generation definitively no
        longer exists, allowing the caller to create a fresh one.
    :raises HTTPException: 502 when the resume or host restart otherwise fails.
    """
    if config is None:
        return
    # Cross-replica DB liveness (freshness-gated): never trust the per-replica
    # registry alone. Cheap gate before taking the lock.
    if not force and await asyncio.to_thread(host_store.is_online, host_id):
        return
    # Single-flight per host (see _resume_locks).
    resume_lock = _resume_locks.setdefault(host_id, asyncio.Lock())
    async with resume_lock:
        # Re-check under the lock: a concurrent waker may have brought the host
        # online while we waited.
        if not force and await asyncio.to_thread(host_store.is_online, host_id):
            return
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            return
        # Provider-matched launcher (None if config dropped / provider changed).
        # Non-resumable providers fall through to a fresh relaunch.
        launcher = _launcher_for_teardown(host, config)
        if launcher is None or not launcher.capabilities.resume_stopped or host.sandbox_id is None:
            return
        # Resumable providers retain their filesystem, so a wake needs no
        # workspace prep.
        entry = config.recorded(host.sandbox_provider)
        sandbox_id = host.sandbox_id
        _logger.info(
            "Waking dormant managed host %s (sandbox %s, provider %s)",
            host.host_id,
            sandbox_id,
            launcher.provider,
        )
        try:
            await asyncio.to_thread(launcher.prepare_for_launch, agent_name=agent_name)
            await asyncio.to_thread(launcher.resume, sandbox_id)
            token = secrets.token_urlsafe(32)
            armed = await asyncio.to_thread(
                host_store.rearm_managed_host,
                host.host_id,
                sandbox_id=sandbox_id,
                expected_updated_at=host.updated_at,
                token=token,
                token_expires_at=now_epoch() + entry.token_ttl_s,
            )
            if armed is None:
                current = await asyncio.to_thread(host_store.get_host, host.host_id)
                if current is None:
                    await _terminate_sandbox_best_effort(
                        launcher,
                        sandbox_id,
                        host_id=host.host_id,
                        provider=host.sandbox_provider,
                    )
                    raise ValueError(f"managed host {host.host_id!r} no longer exists")
                if current.sandbox_id != sandbox_id:
                    terminated = await _terminate_sandbox_best_effort(
                        launcher,
                        sandbox_id,
                        host_id=host.host_id,
                        provider=host.sandbox_provider,
                    )
                    if terminated and current.terminating_sandbox_id == sandbox_id:
                        await asyncio.to_thread(
                            host_store.mark_sandbox_terminated,
                            host.host_id,
                            sandbox_id=sandbox_id,
                        )
                return
            await _start_sandbox_host(
                launcher,
                sandbox_id,
                token=token,
                host_id=host.host_id,
                host_name=host.name,
                server_url=entry.server_url,
                repos=(),
                host_config=entry.host_config,
                on_stage=on_stage,
                agent_name=agent_name,
                runner_env={
                    **_resolve_runner_env(entry.runner_env),
                    **eval_runner_env(host.user_id),
                },
            )
            await _wait_for_host_online(host_store, host.host_id)
        except HostGoneError:
            raise
        except Exception as exc:
            # An ordinary failed wake must NOT tear the sandbox down (the volume
            # is the user's); just surface it. Full teardown is handled above.
            if isinstance(exc, HTTPException):
                raise
            message = exc.message if isinstance(exc, click.ClickException) else str(exc)
            raise HTTPException(
                status_code=502, detail=f"managed host wake failed: {message}"
            ) from exc


async def terminate_host(
    host: Host,
    host_store: HostStore,
    config: HostDeployment | None,
) -> None:
    """
    Terminate a managed host's sandbox and delete its host row.

    The latest row is locked and logically deleted before provider termination.
    This removes the host from user-visible reads, revokes its token, and
    serializes teardown with generation replacement. Recorded sandbox ids remain
    on the tombstone until termination succeeds, allowing the reaper to retry
    transient provider failures.

    :param host: The managed host to tear down. Active and pending sandbox ids
        are both terminated when present.
    :param host_store: Store holding the host row.
    :param config: The deployment's current sandbox config (supplies
        the launcher for the provider-side terminate), or ``None``
        when managed hosts are no longer configured.
    """
    tombstone = await asyncio.to_thread(host_store.delete_host, host.host_id)
    if tombstone is None:
        return
    launcher = _launcher_for_teardown(tombstone, config)
    sandbox_ids = dict.fromkeys((tombstone.sandbox_id, tombstone.terminating_sandbox_id))
    for sandbox_id in sandbox_ids:
        if sandbox_id is not None:
            terminated = await _terminate_sandbox_best_effort(
                launcher,
                sandbox_id,
                host_id=tombstone.host_id,
                provider=tombstone.sandbox_provider,
            )
            if terminated:
                await asyncio.to_thread(
                    host_store.mark_sandbox_terminated,
                    tombstone.host_id,
                    sandbox_id=sandbox_id,
                )


async def remove_host(
    host: Host,
    host_store: HostStore,
    config: HostDeployment | None,
    *,
    home: bool,
) -> bool:
    """
    Stop and remove a managed host through its provider (``remove_host``); with *home*, its
    home too, and the host row is deleted.

    For a person who is going away (an eval person, ADR 0042), never for session cleanup:
    that is :func:`terminate_host`, which a per-person host outlives.

    :returns: Whether the provider removed it; a failure is logged, never raised.
    """
    launcher = _launcher_for_teardown(host, config)
    if launcher is None or host.sandbox_id is None:
        return False
    try:
        await asyncio.to_thread(launcher.remove_host, host.sandbox_id, home=home)
    except Exception:
        _logger.warning(
            "Could not remove host %s (provider=%s)",
            host.host_id,
            host.sandbox_provider,
            exc_info=True,
        )
        return False
    if home:
        tombstone = await asyncio.to_thread(host_store.delete_host, host.host_id)
        if tombstone is not None:
            await asyncio.to_thread(
                host_store.mark_sandbox_terminated, host.host_id, sandbox_id=host.sandbox_id
            )
    return True


async def _terminate_sandbox_best_effort(
    launcher: HostLauncher | None,
    sandbox_id: str,
    *,
    host_id: str,
    provider: str | None,
) -> bool:
    """Terminate one provider sandbox id without touching its host row."""
    if launcher is None:
        _logger.warning(
            "No launcher available for host provider %s; "
            "sandbox %s must be deleted with the provider's own tooling",
            provider,
            sandbox_id,
        )
        return False
    try:
        await asyncio.to_thread(launcher.terminate, sandbox_id)
        return True
    except Exception:
        _logger.warning(
            "Failed to terminate managed sandbox %s (provider=%s) for host %s",
            sandbox_id,
            provider,
            host_id,
            exc_info=True,
        )
        return False
