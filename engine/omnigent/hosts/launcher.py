"""
Provider-agnostic interface for running Omnigent hosts in remote sandboxes.

A *host launcher* wraps one host provider (e.g. the Docker-backed
``docker``) behind the small set of transport / lifecycle primitives that the
server's managed-host code composes: provision a sandbox, run commands in it,
ship files into it, stream a PTY-backed process out of it, forward a local port
into it, and hold a foreground process open. Everything provider-specific
lives behind a :class:`ExecModelHostLauncher` implementation.

Injected host config uses the loader's ``OMNIGENT_CONFIG_HOME`` resolution,
atomically replaces its config and ownership-marker files, and removes a
previously injected value only while it remains unchanged by the user.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import secrets
import shlex
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.host import HOST_FATAL_EXIT_CODE, HOST_SIGTERM_EXIT_CODE
from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.hosts import types as _host_types

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from omnigent.hosts.types import RepoWorkspace


DEFAULT_HOST_IMAGE: str = "ghcr.io/omnigent-ai/omnigent-host:latest"
"""Default sandbox image across providers: the official prebaked
Omnigent host image, published by CI from the ``host`` target of
``deploy/docker/Dockerfile`` (``:latest`` tracks main; ``:sha-<short>``
pins a commit). It bakes the full omnigent install plus git / tmux /
curl and the coding-harness CLIs, so sandbox creation skips the
in-sandbox dependency install. Providers layer their own override
mechanisms (env var / server config) on top of this default."""

_logger = logging.getLogger(__name__)

MANAGED_KEEPALIVE_INTERVAL_ENV_VAR: str = "OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S"
"""Environment variable overriding the managed-sandbox keepalive cadence (seconds)."""

# Default keepalive cadence. Providers' keep_alive is idempotent ("configure
# once"), so a slow cadence keeps provider calls cheap.
_DEFAULT_MANAGED_KEEPALIVE_INTERVAL_S: float = 600.0
_MIN_MANAGED_KEEPALIVE_INTERVAL_S: float = 5.0
# Ceiling so a finite-but-huge override (e.g. 1e308) cannot overflow the
# window-floor math (ceil(2 * interval)); an hour is already far past useful.
_MAX_MANAGED_KEEPALIVE_INTERVAL_S: float = 3600.0


def resolve_managed_keepalive_interval_s() -> float:
    """
    How often the server refreshes a live managed sandbox's liveness, in seconds.

    Defaults to 600s. :data:`MANAGED_KEEPALIVE_INTERVAL_ENV_VAR` overrides it
    when set (advanced/experimental — the operator-facing knob is
    ``keep_warm_s``), floored at a small minimum so a typo cannot spin the loop.
    Resolved live from the env on each call (no snapshot).
    """
    default = _DEFAULT_MANAGED_KEEPALIVE_INTERVAL_S
    raw = os.environ.get(MANAGED_KEEPALIVE_INTERVAL_ENV_VAR, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        _logger.warning(
            "ignoring %s=%r (not a number); using %ss",
            MANAGED_KEEPALIVE_INTERVAL_ENV_VAR,
            raw,
            default,
        )
        return default
    if not math.isfinite(parsed):
        # "nan"/"inf" parse cleanly but blow up downstream in int/ceil(2 * x);
        # a non-finite typo must fail safe like any other bad value.
        _logger.warning(
            "ignoring %s=%r (not a finite number); using %ss",
            MANAGED_KEEPALIVE_INTERVAL_ENV_VAR,
            raw,
            default,
        )
        return default
    if parsed < _MIN_MANAGED_KEEPALIVE_INTERVAL_S:
        _logger.warning(
            "%s=%r is below the %ss minimum; using %ss",
            MANAGED_KEEPALIVE_INTERVAL_ENV_VAR,
            raw,
            _MIN_MANAGED_KEEPALIVE_INTERVAL_S,
            _MIN_MANAGED_KEEPALIVE_INTERVAL_S,
        )
        return _MIN_MANAGED_KEEPALIVE_INTERVAL_S
    if parsed > _MAX_MANAGED_KEEPALIVE_INTERVAL_S:
        _logger.warning(
            "%s=%r is above the %ss maximum; using %ss",
            MANAGED_KEEPALIVE_INTERVAL_ENV_VAR,
            raw,
            _MAX_MANAGED_KEEPALIVE_INTERVAL_S,
            _MAX_MANAGED_KEEPALIVE_INTERVAL_S,
        )
        return _MAX_MANAGED_KEEPALIVE_INTERVAL_S
    return parsed


# Ceiling for the in-sandbox host restart backoff, so a host that crashes on
# every attempt settles into a slow retry instead of a hot loop.
_RESTART_MAX_DELAY_S: int = 30


def host_image_wheel_install_command(remote_tgz_path: str) -> str:
    """
    Build the remote shell command that overlays locally-built wheels
    onto a sandbox booted from the prebaked host image
    (:data:`DEFAULT_HOST_IMAGE`).

    Shared by every launcher whose sandboxes boot from that image:
    the right pip flags are a property of the image,
    not the provider.

    ``--force-reinstall`` is required because the host image bakes
    omnigent at the same ``0.1.0`` version. Without it, pip sees the
    version satisfied and silently skips, leaving the sandbox on the
    baked code while the CLI reports success.

    ``--no-deps`` skips the (already baked) dependency tree, so the
    overlay is just the three local wheels. A local checkout that adds
    a brand-new dependency surfaces as ImportError at runtime until
    the official image rebuilds with it (next main commit) — one-time
    manual pip-install of that package per affected sandbox in the
    meantime.

    The image's venv pip is first on PATH, so the install lands in the
    venv and entry points stay in ``/opt/venv/bin``.

    :param remote_tgz_path: Sandbox path of the shipped tarball, e.g.
        ``"/tmp/oa-wheels.tgz"``.
    :returns: Shell command string for :meth:`ExecModelHostLauncher.run`.
    """
    return (
        "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
        f"tar xzf {remote_tgz_path} -C oa-wheels --warning=no-unknown-keyword && "
        "pip install --quiet --force-reinstall --no-deps "
        "--no-warn-script-location oa-wheels/*.whl"
    )


# Prefix for the private dir a launcher creates under world-writable ``/tmp``
# to record the pid of an exec'd foreground process (several providers' SDKs
# expose no kill handle for exec'd processes, so the pid is recorded and a
# second exec signals it on detach). The dir carries an unpredictable random
# suffix and is created mode 700 with a bare ``mkdir`` that fails closed if
# the path already exists, so a co-tenant on the sandbox can't pre-create,
# symlink-redirect, or read the pidfile in ``/tmp``.
_FOREGROUND_RUNDIR_PREFIX: str = "/tmp/oa-foreground-"


def foreground_pidfile() -> tuple[str, str]:
    """Allocate a private, unpredictably-named pidfile under ``/tmp``.

    Used by :meth:`ExecModelHostLauncher.exec_foreground` implementations whose
    SDK cannot kill an exec'd process through its handle. The caller records the remote pid with
    :func:`foreground_record_prefix` and tears it down with
    :func:`foreground_kill_command`, both of which operate on the paths
    returned here.

    The pidfile lives in a fresh dir created ``mode 700`` with a bare
    ``mkdir`` (no ``-p``) so it **fails closed** if the path already
    exists — a co-tenant can't pre-seed a symlink we'd write through, nor
    read our pid back, in the world-writable ``/tmp`` it lives under.

    :returns: A ``(run_dir, pidfile)`` pair of absolute ``/tmp`` paths.
        ``run_dir`` is ``/tmp/oa-foreground-<32 hex chars>`` and
        ``pidfile`` is ``<run_dir>/pid``.
    """
    run_dir = f"{_FOREGROUND_RUNDIR_PREFIX}{secrets.token_hex(16)}"
    return run_dir, f"{run_dir}/pid"


def foreground_record_prefix(pidfile: str) -> str:
    """Shell prefix that creates the run dir and records the shell pid.

    ``mkdir -m 700`` (no ``-p``) fails closed if the path exists, and
    ``echo $$`` writes the shell pid before the caller swaps in the real
    command via ``exec`` (which keeps the pid across the swap). Both
    paths are :func:`shlex.quote`-d before interpolation so the function
    stays safe even if a future caller passes a non-hex path; the
    standard hex paths from :func:`foreground_pidfile` quote harmlessly.

    :param pidfile: The pidfile path returned by :func:`foreground_pidfile`.
    :returns: A shell fragment such as ``"mkdir -m 700 /tmp/… && echo $$ > /tmp/…/pid && "``
        to prepend before the foreground command.
    """
    run_dir = pidfile.rsplit("/", 1)[0]
    q_dir = shlex.quote(run_dir)
    q_pid = shlex.quote(pidfile)
    return f"mkdir -m 700 {q_dir} && echo $$ > {q_pid} && "


def foreground_kill_command(pidfile: str) -> str:
    """Shell command that signals the recorded pid and drops the run dir.

    Only a fully-numeric pid read back from the private pidfile is ever
    signalled — the ``case`` rejects empty and non-numeric content, so
    unvalidated file contents never reach ``kill``. The run dir is then
    removed so a successful foreground run leaves nothing behind in
    ``/tmp``.

    :param pidfile: The pidfile path returned by :func:`foreground_pidfile`.
    :returns: A self-contained shell command string for a second exec.
    """
    run_dir = pidfile.rsplit("/", 1)[0]
    q_dir = shlex.quote(run_dir)
    q_pid = shlex.quote(pidfile)
    return (
        f"pid=$(cat {q_pid} 2>/dev/null); "
        f'case "$pid" in ""|*[!0-9]*) ;; *) kill "$pid" 2>/dev/null ;; esac; '
        f"rm -rf {q_dir}"
    )


# In-sandbox write of an injected host config, run via ``python3 -c``.
# Self-contained on purpose (stdlib + yaml, both baked into any image that can
# run ``omnigent host``): importing merge logic from the sandbox's installed
# omnigent package would tie the feature to the IMAGE's package version, and
# operator-supplied images may predate it. ``__PAYLOAD__`` is replaced with a
# base64 Python literal — its alphabet has no quote or shell metacharacter, so
# arbitrary YAML content can never break out of the script.
#
# A marker records the previous payload. Each run removes exactly the names it
# injected last time — the server OWNS the names/keys it injects, so a renamed
# gateway or a removed block never strands a stale entry that could collide on a
# ``default`` scope. User-created entries under OTHER names are never in the
# marker and so are never touched. A missing or corrupt marker skips removal
# entirely — never delete without evidence of what was injected.
_HOST_CONFIG_WRITE_SCRIPT: str = """\
import base64, json, os, tempfile, yaml

config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
config_dir = config_home if config_home else os.path.join(os.path.expanduser("~"), ".omnigent")
path = os.path.join(config_dir, "config.yaml")
marker = os.path.join(config_dir, ".injected_host_config.json")
existing = {}
if os.path.exists(path):
    with open(path) as f:
        loaded = yaml.safe_load(f)
    if isinstance(loaded, dict):
        existing = loaded
previous = {}
try:
    with open(marker) as f:
        loaded = json.load(f)
    if isinstance(loaded, dict):
        previous = loaded
except (OSError, ValueError):
    pass
for key, value in previous.items():
    if key == "providers" and isinstance(value, dict):
        current = existing.get(key)
        if isinstance(current, dict):
            for name in value:
                current.pop(name, None)
            if not current:
                existing.pop(key, None)
    else:
        existing.pop(key, None)
injected = json.loads(base64.b64decode(__PAYLOAD__).decode())
for key, value in injected.items():
    current = existing.get(key)
    if key == "providers" and isinstance(current, dict) and isinstance(value, dict):
        existing[key] = {**current, **value}
    else:
        existing[key] = value

def atomic_write(path, dump):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path), delete=False) as f:
            temp_path = f.name
            dump(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise

if injected or previous:
    os.makedirs(config_dir, exist_ok=True)
    atomic_write(
        path,
        lambda f: yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=True),
    )
if injected:
    atomic_write(marker, lambda f: json.dump(injected, f))
elif previous:
    os.remove(marker)
"""


def render_host_config_write_command(host_config: dict[str, object]) -> str:
    """
    Build the remote command that installs *host_config* into the
    sandbox's config directory before ``omnigent host`` starts. The directory
    is ``$OMNIGENT_CONFIG_HOME`` when truthy, otherwise ``~/.omnigent``, exactly
    matching :func:`omnigent.onboarding.provider_config._config_path`.

    Server-managed replacement semantics: the server OWNS the names/keys it
    injects. Entries recorded in the previous marker are removed first BY NAME,
    then the current payload merges in with
    ``omnigent.cli._save_global_config``'s
    ``deep_merge_keys=("providers",)`` semantics — ``providers`` one
    level deep and every other top-level key wholesale. Removing by name (rather
    than only when unchanged) is deliberate: a renamed gateway must not leave
    its old entry behind, since two entries claiming the same ``default`` scope
    is a sandbox load error. User-created config under names the server never
    injects is never in the marker and so always survives; a name the server
    injects is server-managed, and an in-sandbox edit to it does not persist
    across the next replacement. An empty *host_config* renders a pure cleanup
    command. A missing or corrupt marker skips removal rather than guessing
    ownership. Shared by both launch seams — the exec-model
    :meth:`ExecModelHostLauncher.start_host` and entrypoint-as-host launchers — so the
    behavior cannot drift between providers.

    Both config and marker writes use a fully-written, fsynced temporary file
    in the destination directory followed by :func:`os.replace`, so an
    interrupted write cannot expose a truncated destination file. A pre-existing
    ``config.yaml`` symlink is replaced by a real file — durability of the write
    is favored over following the link, which an internal sandbox config never
    relies on.

    The payload travels as base64-encoded JSON substituted into a fixed
    Python script, and the whole script is ``shlex.quote``-wrapped —
    operator-supplied YAML content (quotes, ``$``, newlines) never
    reaches shell or Python quoting.

    :param host_config: The validated ``host.host_config`` mapping
        (see :func:`omnigent.server.hosts.parse_host_section`),
        or ``{}`` to only remove previously injected entries.
    :returns: A ``python3 -c '<script>'`` shell command, safe to pass to
        :meth:`ExecModelHostLauncher.run` or embed in a larger shell script.
    """
    payload = base64.b64encode(json.dumps(host_config).encode()).decode()
    script = _HOST_CONFIG_WRITE_SCRIPT.replace("__PAYLOAD__", repr(payload))
    return f"python3 -c {shlex.quote(script)}"


class HostCapabilityError(click.ClickException, _host_types.HostError):
    """
    Raised when a launcher does not support an optional primitive.

    The only optional primitive today is
    :meth:`ExecModelHostLauncher.forward_local_port` — providers without a
    local-to-sandbox forwarding path raise this, and the
    OAuth flow surfaces the message (which should name the ``--no-auth``
    escape hatch) to the user.
    """


class HostGoneError(click.ClickException, _host_types.HostError):
    """Raised when a sandbox generation definitively no longer exists.

    Resumable providers use this only for a definitive absence, never for a
    timeout, connectivity failure, or unknown state. The managed-host wake path
    catches it and provisions a fresh sandbox generation instead.
    """


@dataclass
class RemoteCommandResult:
    """
    Outcome of a command run inside a sandbox via
    :meth:`ExecModelHostLauncher.run`.

    :param returncode: The remote command's exit code, e.g. ``0``.
    :param stdout: Captured standard output. Providers that merge the
        two streams put the combined output here.
    :param stderr: Captured standard error; empty for providers that
        merge streams into ``stdout``.
    """

    returncode: int
    stdout: str
    stderr: str


class RemoteProcess(ABC):
    """
    A streaming remote process spawned by
    :meth:`ExecModelHostLauncher.stream_exec`.

    Callers interleave reads of :attr:`lines` with control calls — the
    OAuth flow reads lines until it finds the verification URL, opens a
    port forward, then keeps reading the same stream until the process
    exits.
    """

    @property
    @abstractmethod
    def lines(self) -> Iterator[str]:
        """
        Line iterator over the process's combined stdout/stderr.

        Repeated accesses MUST return the same underlying iterator so a
        caller can consume a few lines, do other work, and resume the
        stream where it left off.

        :returns: Iterator yielding output lines (trailing newlines
            included, matching ``subprocess.Popen`` text-mode streams).
        """

    @abstractmethod
    def wait(self) -> int:
        """
        Block until the process exits.

        :returns: The process's exit code, e.g. ``0``.
        """

    @abstractmethod
    def close(self) -> None:
        """
        Terminate the process if it is still running and reap it.

        Idempotent: safe to call after :meth:`wait` returned or after a
        prior ``close``.
        """


class HostLifecycle(ABC):
    """
    Lifecycle + capability primitives for one host provider.

    Every provider — exec-model or entrypoint-as-host — implements this
    base. It carries the provider identity, capability flags, and the
    provisioning / teardown / resume lifecycle. Transport (``run``, ``put``,
    …) lives on :class:`HostExecTransport`; the default host launch lives
    on :class:`ExecModelHostLauncher`.
    """

    # Short provider name used in CLI ``--provider`` choices and error
    # messages, e.g. ``"docker"``.
    provider: ClassVar[str]

    # Package index URL exported as ``UV_INDEX_URL`` for the local wheel
    # build, or ``None`` to use ambient uv configuration. Providers tied
    # to networks where public PyPI is unreachable override this.
    wheel_build_index_url: ClassVar[str | None] = None

    # Legacy class vars — kept for backward compat while providers migrate
    # to explicit ``capabilities`` objects. New providers should override
    # the ``capabilities`` property directly instead of setting these.
    supports_local_port_forward: ClassVar[bool] = False
    supports_cli_bootstrap: ClassVar[bool] = True
    can_resume: ClassVar[bool] = False
    supports_managed_launch: ClassVar[bool] = True

    @property
    def capabilities(self) -> _host_types.HostCapabilities:
        """
        Feature flags this provider declares.

        The returned object is derived from the provider's class vars and
        from which optional transport methods it has overridden. It is a
        transition shim: providers override this property with an explicit
        :class:`~omnigent.hosts.types.HostCapabilities`
        object directly.
        """
        return _host_types.HostCapabilities(
            cli_bootstrap=self.supports_cli_bootstrap,
            managed_launch=self.supports_managed_launch,
            local_port_forward=self.supports_local_port_forward,
            resume_stopped=self.can_resume,
            programmatic_terminate=self._is_capability_overridden("terminate"),
            file_copy=self._is_capability_overridden("put"),
            streaming_exec=self._is_capability_overridden("stream_exec"),
            foreground_exec=self._is_capability_overridden("exec_foreground"),
            git_clone_options=(
                getattr(type(self), "start_host", None) is ExecModelHostLauncher.start_host
                and getattr(type(self), "materialize_workspace", None)
                is ExecModelHostLauncher.materialize_workspace
            ),
        )

    def _is_capability_overridden(self, name: str) -> bool:
        """
        Return whether this provider overrides the named optional method.

        Used while the refactor is in transition so the capability object
        can reflect overridden methods without provider authors touching it.
        """
        self_method = getattr(type(self), name, None)
        if self_method is None:
            return False
        base_method = getattr(ExecModelHostLauncher, name, None)
        if base_method is None:
            return False
        return self_method is not base_method

    @abstractmethod
    def prepare(self) -> None:
        """
        Run local preflight: install/verify provider tooling and
        credentials on the machine invoking the CLI.

        Idempotent — called at the start of every bootstrap.

        :raises click.ClickException: When required local tooling or
            credentials are missing and cannot be installed.
        """

    @abstractmethod
    def provision(self, name: str) -> str:
        """
        Create a new sandbox and return its id.

        Exec-model providers create the box here. Entrypoint-as-host providers
        (whose sandbox boots running the host) may instead just RESERVE the id
        and defer materialization to :meth:`start_host` — which lets the server
        register the launch token against the id before the box exists, closing
        the host dial-back race by construction.

        :param name: Human-readable label for the sandbox, e.g.
            ``"omnigent-host"``.
        :returns: The provider-assigned (or reserved) sandbox id, e.g.
            ``"lovable-wattlebird-1530"``.
        :raises click.ClickException: If provisioning fails.
        """

    def attach(self, sandbox_id: str) -> None:
        """
        Validate / refresh access to an existing sandbox so subsequent
        primitives can resolve it.

        CLI-bootstrap capability — the server's managed-host flow never
        attaches to pre-existing sandboxes, so launchers that exist
        only for managed launches (e.g. a deployment-injected custom
        launcher) need not override the raising default.

        :param sandbox_id: The sandbox to attach to, e.g.
            ``"lovable-wattlebird-1530"``.
        :raises HostCapabilityError: When the provider does not
            support attaching.
        :raises click.ClickException: If the sandbox cannot be resolved.
        """
        raise self._capability_error("attach to an existing sandbox")

    def keep_alive(self, sandbox_id: str) -> bool | None:
        """
        Keep the sandbox from being reclaimed while it is still in use,
        so long agent runs don't lose their host. Soft-fail:
        implementations should warn rather than raise when the provider
        rejects the setting. Return ``False`` when an extension was attempted
        but could not be confirmed (a soft failure the provider already logged),
        so the managed keepalive loop can skip its success line; ``None`` or
        ``True`` otherwise.

        Called BOTH once after a CLI bootstrap provision AND periodically
        by the managed path for as long as the sandbox has a live runner
        (:mod:`omnigent.server.host_keepalive`), so an implementation
        must be idempotent and cheap enough to repeat. Either shape
        satisfies it: "configure once to maximize lifetime" (disable idle
        autostop, restate a cap) or "push a deadline forward" (refresh an
        absolute expiry). Managed-only launchers that cannot extend a
        sandbox keep the raising default and are skipped.

        :param sandbox_id: The sandbox to configure.
        :raises HostCapabilityError: When the provider does not
            support keep-alive configuration.
        """
        raise self._capability_error("configure keep-alive")

    def forward_capability_error(self) -> HostCapabilityError:
        """
        Build the error explaining that this provider cannot forward a
        local port into the sandbox (and therefore cannot run the
        in-sandbox App OAuth flow).

        Single source for the message: raised both by the default
        :meth:`forward_local_port` and by the bootstrap's fail-fast
        check on :attr:`supports_local_port_forward`.

        :returns: The capability error, naming the ``--no-auth`` escape
            hatch.
        """
        return HostCapabilityError(
            f"The '{self.provider}' provider cannot forward a local port into the "
            "sandbox, which the in-sandbox Databricks App auth flow requires — "
            "use this provider with servers that don't need App auth."
        )

    def forward_local_port(self, sandbox_id: str, port: int) -> AbstractContextManager[None]:
        """
        Forward ``localhost:<port>`` on the local machine into the
        sandbox (``ssh -L`` semantics), yielding once the local port is
        bound and tearing the forward down on exit.

        Optional capability: the default implementation raises
        :class:`HostCapabilityError`. Providers with an inbound
        forwarding path override it AND set
        :attr:`supports_local_port_forward` to ``True``.

        :param sandbox_id: Target sandbox.
        :param port: Local + remote loopback port to bridge, e.g.
            ``8022``.
        :returns: Context manager holding the forward open.
        :raises HostCapabilityError: When the provider has no
            local-to-sandbox forwarding path.
        """
        raise self.forward_capability_error()

    def terminate(self, sandbox_id: str) -> None:
        """
        Terminate a sandbox, releasing its compute.

        Optional capability: the default implementation raises
        :class:`HostCapabilityError` — providers whose SDK exposes
        programmatic termination override it. Used by the server's
        managed-host cleanup when a managed session is deleted. Implementations
        must treat an already-absent sandbox as success so cleanup can retry
        safely after a crash or database failure.

        :param sandbox_id: The sandbox to terminate, e.g.
            ``"sb-a1b2c3"``.
        :raises HostCapabilityError: When the provider has no
            programmatic termination path — delete the sandbox with
            the provider's own tooling instead.
        """
        raise HostCapabilityError(
            f"The '{self.provider}' provider does not support programmatic "
            "sandbox termination — delete the sandbox with the provider's "
            "own tooling."
        )

    def remove_host(self, sandbox_id: str, *, home: bool) -> None:
        """
        Stop and remove a host for good, and its home with *home*.

        Unlike :meth:`terminate` (session cleanup, which a per-person host outlives), this is for
        a person who is going away: an eval person whose run ended (home kept for review), then
        deleted (home too). Optional capability: the default raises
        :class:`HostCapabilityError`. An already-absent host or home is success.

        :param sandbox_id: The host to remove, a value :meth:`provision` returned.
        :param home: Also delete the host's home (its persistent volume).
        :raises HostCapabilityError: When the provider cannot remove a host.
        """
        raise HostCapabilityError(f"The '{self.provider}' provider cannot remove a host.")

    def resume(self, sandbox_id: str) -> None:
        """
        Resume a stopped sandbox in place, reattaching its persistent
        volume, so a dormant managed host can be revived under the SAME
        sandbox id.

        Optional capability: the default implementation raises
        :class:`HostCapabilityError`. Providers whose backend has a
        stop/resume lifecycle with a persistent volume override it AND set
        :attr:`can_resume` to ``True``. Used by the server's managed-host
        wake path; the host process itself is restarted separately (resume
        only brings the compute + volume back).

        :param sandbox_id: The stopped sandbox to resume, e.g.
            ``"sb-a1b2c3"``.
        :raises HostCapabilityError: When the provider cannot resume a
            stopped sandbox (ephemeral sandboxes / no persistent volume).
        :raises HostGoneError: When the sandbox generation definitively no
            longer exists.
        :raises click.ClickException: If the resume fails.
        """
        raise self._capability_error("resume a stopped sandbox")

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether the provider reports this sandbox as running.

        Optional capability: ``None`` means the launcher cannot cheaply answer
        and callers should preserve their existing liveness behavior.

        :param sandbox_id: The sandbox to inspect, e.g. ``"sb-a1b2c3"``.
        :returns: ``True`` when running, ``False`` when not running, or ``None``
            when the provider status is unknown.
        """
        del sandbox_id
        return None

    def screen_url(
        self,
        sandbox_id: str,
        *,
        interactive: bool,
        control_token: str | None = None,
    ) -> str:
        """
        Return a URL that shows this sandbox's screen.

        Optional capability (:attr:`HostCapabilities.screen`).

        :param sandbox_id: The sandbox to show, e.g. ``"sb-a1b2c3"``.
        :param interactive: ``True`` for a URL that also accepts mouse and keyboard input
            (the person takes over); ``False`` for a view-only URL.
        :param control_token: Secret the interactive URL is bound to; required when
            *interactive* is ``True``.
        :returns: The viewer URL.
        :raises click.ClickException: If the provider has no screen.
        """
        del sandbox_id, interactive, control_token
        raise self._capability_error("show the sandbox screen")

    def release_control(self, sandbox_id: str) -> None:
        """
        Revoke any interactive access granted through :meth:`screen_url`.

        Optional capability (:attr:`HostCapabilities.screen`).

        :param sandbox_id: The sandbox whose interactive access is revoked.
        :raises click.ClickException: If the provider has no screen.
        """
        del sandbox_id
        raise self._capability_error("revoke interactive screen access")

    def _capability_error(self, action: str) -> HostCapabilityError:
        """
        Build the error for an optional primitive this provider lacks.

        :param action: Human phrase for the unsupported primitive,
            e.g. ``"ship files into the sandbox"``.
        :returns: The capability error to raise.
        """
        return HostCapabilityError(
            f"The '{self.provider}' provider does not support the ability to {action}."
        )


# Directory (relative to $HOME) a supervised host loop records its own pid under, keyed by
# host id — see :func:`supervised_host_pidfile_path`.
_SUPERVISED_HOST_PIDFILE_DIR = ".omnigent/hosts"


def supervised_host_pidfile_path(host_id: str) -> str:
    """
    Shell expression for the pidfile a supervised ``omnigent host`` loop for
    *host_id* records its own pid at.

    Rendered with a literal ``$HOME`` so the REMOTE shell expands it at run
    time (not this process) — matches wherever the sandbox's home actually
    is. ``host_id`` is always a server-generated hex string
    (``uuid.uuid4().hex``), so no shell metacharacter ever reaches this path.

    Shared by :func:`supervise_host_command` (which writes the pid) and
    :meth:`HostExecTransport.is_host_supervisor_running` (which reads it
    back) so a repeated :meth:`ExecModelHostLauncher.start_host` call can tell
    whether a previous launch's loop is still alive in the same sandbox.

    :param host_id: Server-chosen host identity, e.g. ``"host_a1b2c3d4..."``.
    :returns: A ``$HOME``-relative path expression, e.g.
        ``"$HOME/.omnigent/hosts/host_a1b2c3d4....pid"``.
    """
    return f"$HOME/{_SUPERVISED_HOST_PIDFILE_DIR}/{host_id}.pid"


def supervise_host_command(command: str, *, host_id: str | None = None) -> str:
    """
    Wrap a host launch in a restart loop so a crash does not strand the sandbox.

    The sandbox container outlives the host process: PID 1 is a placeholder
    (``sleep infinity``) or the provider's own init, so a dead host leaves a
    healthy, still-billing sandbox with nothing running in it. The only recovery
    is the server re-provisioning a fresh sandbox on the next message, which
    discards the workspace — restarting in place keeps the clone and the
    installed dependencies.

    The loop stands down on a clean exit, on
    :data:`~omnigent.host.HOST_FATAL_EXIT_CODE` (a credential / version failure
    that can never succeed), and on SIGTERM (a deliberate stop). Anything else
    is a crash, retried with a doubling delay. A signal-kill of the host alone
    (SIGKILL → 137) counts as a crash on purpose: that is what an OOM kill looks
    like, and restarting is the wanted response.

    A path that means to STOP the host must therefore signal the supervisor too,
    not just the host — otherwise the loop faithfully restarts it. Both in-sandbox
    stop paths already do: ``foreground_kill_command`` signals the process the
    pidfile recorded (the supervisor, which is what ``exec``s under it).

    The attempt counter in the restart log makes a persistently-crashing host
    observable — the loop never gives up, so a wedged box would otherwise be
    silent apart from indistinguishable repeats.

    When *host_id* is given, the loop records its OWN pid (stable across
    restart attempts — it is the supervising shell, not the host process) at
    :func:`supervised_host_pidfile_path`, written before the first attempt so
    a sandbox whose provider persists across launches (a relaunch onto a
    still-live sandbox, or Superchat's ``docker`` provider, which never
    terminates — see :mod:`omnigent.hosts.docker`) can detect
    an already-running loop for the same host id and skip starting a second
    one (see :meth:`HostExecTransport.is_host_supervisor_running`).

    :param command: The host launch, e.g. ``"OMNIGENT_HOST_TOKEN=… omnigent
        host --server https://…"``. Env prefixes are re-applied per attempt.
    :param host_id: Server-chosen host identity to record the loop's pid
        under, or ``None`` to skip pidfile bookkeeping entirely.
    :returns: A POSIX ``sh`` script ending in ``done``, so callers can append
        redirections to it directly.
    """
    stop_codes = f"0|{HOST_FATAL_EXIT_CODE}|{HOST_SIGTERM_EXIT_CODE}"
    record_prefix = ""
    if host_id is not None:
        pidfile = supervised_host_pidfile_path(host_id)
        record_prefix = f'mkdir -p "$(dirname {pidfile})" && echo $$ > {pidfile}\n'
    return (
        f"{record_prefix}"
        "delay=1\n"
        "attempt=0\n"
        "while :; do\n"
        f"  {command}\n"
        "  rc=$?\n"
        f'  case "$rc" in {stop_codes}) exit "$rc";; esac\n'
        "  attempt=$((attempt + 1))\n"
        '  echo "omnigent host exited ($rc); attempt $attempt; '
        'restarting in ${delay}s" >&2\n'
        '  sleep "$delay"\n'
        f'  delay=$((delay * 2)); [ "$delay" -gt {_RESTART_MAX_DELAY_S} ] '
        f"&& delay={_RESTART_MAX_DELAY_S}\n"
        "done"
    )


class HostExecTransport(HostLifecycle):
    """
    Exec-transport primitives for providers that exec into a running sandbox.

    Providers whose sandbox is a bare box the server execs into (e.g.
    ``docker``) inherit this via :class:`ExecModelHostLauncher`.
    Entrypoint-as-host providers (whose sandbox boots running the host) inherit
    :class:`HostLauncher` instead and need NOT implement any of these methods.
    """

    @abstractmethod
    def run(
        self,
        sandbox_id: str,
        command: str,
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> RemoteCommandResult:
        """
        Run a shell command inside the sandbox and capture its output.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"pip install --user /tmp/pkg.whl"``. Quote paths yourself
            if they must survive the remote shell.
        :param check: When ``True``, raise on non-zero exit.
        :param env: Extra environment variables for the command's own
            process, delivered through the provider's exec transport
            (never interpolated into *command* itself, so values never
            appear in argv/``ps``/logs). Optional capability: a provider
            whose exec transport cannot carry a separate env mapping may
            omit this parameter from its override — callers only pass a
            non-``None`` value when they need it (today: `host.runner_env`
            delivery to the ``docker`` provider's ``run_background``), so
            providers without the need are unaffected.
        :returns: The completed command's exit code and output.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """

    def run_background(
        self,
        sandbox_id: str,
        command: str,
        *,
        log_path: str = "/tmp/omnigent-host.log",
        host_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RemoteCommandResult:
        """
        Start *command* under a supervisor as a detached background process.

        The command is wrapped in :func:`supervise_host_command` (restart on
        crash) and then in ``setsid nohup sh -c '…' & echo launched`` so it
        survives the exec session ending. The ``sh -c`` wrapper is load-bearing:
        callers pass env-prefixed commands (e.g. ``"ENV=val omnigent host …"``),
        and ``nohup`` does NOT honor shell ``VAR=val`` assignment syntax —
        ``nohup ENV=val cmd`` makes nohup try to exec a program literally named
        ``ENV=val`` ("No such file or directory"). Re-parsing the command under
        ``sh -c`` lets the inner shell apply the assignments before running the
        program. Providers where backgrounded processes are reaped on exec
        return override this to hold the exec stream open
        instead — they supervise too, just without the detach.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to background, e.g.
            ``"ENV=val omnigent host --server https://…"``.
        :param log_path: Where stdout/stderr of the supervisor and every host
            attempt are redirected inside the sandbox.
        :param host_id: Forwarded to :func:`supervise_host_command` so the loop
            records its pid for :meth:`is_host_supervisor_running`, or ``None``
            to skip that bookkeeping.
        :param env: Extra environment for the backgrounded process, forwarded
            to :meth:`run` only when non-empty — callers whose provider
            doesn't need it (the common case: no `host.runner_env`
            configured) see the exact same call as before this parameter
            existed.
        :returns: A synthetic result with ``stdout="launched\\n"`` on success.
        :raises click.ClickException: If the launch command fails.
        """
        wrapped = (
            f"setsid nohup sh -c {shlex.quote(supervise_host_command(command, host_id=host_id))} "
            f">> {log_path} 2>&1 < /dev/null & echo launched"
        )
        if env:
            return self.run(sandbox_id, wrapped, env=env)
        return self.run(sandbox_id, wrapped)

    def is_host_supervisor_running(self, sandbox_id: str, host_id: str) -> bool:
        """
        Whether a supervised ``omnigent host`` loop for *host_id* is still alive in the sandbox.

        Reads back :func:`supervised_host_pidfile_path` and signals the recorded pid with
        ``kill -0`` (checks liveness without affecting it). Only a fully-numeric pidfile
        content is ever used — matches :func:`foreground_kill_command`'s validation so
        unexpected file contents can never reach ``kill``. A missing pidfile, or a pid that no
        longer answers, means "not running" (covers both "never launched" and "crashed/exited
        past the supervisor loop's own lifetime").

        :param sandbox_id: Target sandbox.
        :param host_id: The host id a prior :meth:`ExecModelHostLauncher.start_host` call
            launched (or attempted to launch).
        :returns: ``True`` iff the recorded pid answers ``kill -0``.
        """
        pidfile = supervised_host_pidfile_path(host_id)
        check = (
            f"pid=$(cat {pidfile} 2>/dev/null); "
            'case "$pid" in ""|*[!0-9]*) exit 1;; esac; '
            'kill -0 "$pid" 2>/dev/null'
        )
        result = self.run(sandbox_id, check, check=False)
        return result.returncode == 0

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox.

        CLI-bootstrap capability (wheel shipping) — managed-only
        launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param local_path: Path on the local machine to read from.
        :param remote_path: Destination path on the sandbox, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :raises HostCapabilityError: When the provider does not
            support file shipping.
        :raises click.ClickException: If the transfer fails.
        """
        raise self._capability_error("ship files into the sandbox")

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its output line by
        line.

        CLI-bootstrap capability (the in-sandbox OAuth login) —
        managed-only launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"databricks auth login --host https://… --profile oss"``.
        :param pty: When ``True``, allocate a remote PTY. Required for
            CLIs that suppress output when not attached to a terminal.
        :returns: A handle streaming the process's combined output.
        :raises HostCapabilityError: When the provider does not
            support streaming execs.
        """
        raise self._capability_error("stream a remote process")

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run a command in the sandbox with stdio inherited from the
        current terminal, blocking until it exits (Ctrl-C detaches and
        tears the remote process down).

        Used to hold ``omnigent host`` open while the sandbox is
        registered with the App. CLI-bootstrap capability —
        managed-only launchers need not override the raising default.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://… --profile oss"``.
        :returns: The remote command's exit code.
        :raises HostCapabilityError: When the provider does not
            support foreground execs.
        """
        raise self._capability_error("run a foreground process")

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Build the remote shell command that unpacks the shipped wheel
        tarball and pip-installs the wheels.

        Provider-specific because the right pip flags depend on the
        sandbox image (e.g. an image that bakes omnigent and its deps
        requires ``--force-reinstall --no-deps``).
        CLI-bootstrap capability — managed-only launchers run from
        pre-baked images and need not override the raising default.

        :param remote_tgz_path: Where the wheel tarball was shipped, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :returns: A shell command string for :meth:`run`.
        :raises HostCapabilityError: When the provider does not
            support wheel installs.
        """
        raise self._capability_error("install shipped wheels")


class HostLauncher(HostLifecycle):
    """
    Lifecycle + host-launch contract for a host provider.

    Every managed-host provider — exec-model or entrypoint-as-host — implements
    this. :meth:`start_host` is abstract here; the exec-model default lives on
    :class:`ExecModelHostLauncher`. Entrypoint-as-host providers and
    provider-native host launchers inherit this
    class directly and override :meth:`start_host` without needing any exec
    transport.
    """

    def prepare_for_launch(
        self,
        *,
        agent_name: str | None = None,
        labels: Mapping[str, str] | None = None,
        previous_sandbox_id: str | None = None,
    ) -> None:
        """Set request context before provider preparation, allocation, or resume.

        Providers with pre-created resources can use the resolved agent name
        to select and validate compatible infrastructure before allocation.

        :param agent_name: Server-resolved built-in agent name the session runs, as passed by
            every call site today.
        :param labels: The launching session's labels (e.g. Superchat's ``omnigent.host.owner`` /
            ``omnigent.tenant`` — see :mod:`omnigent.hosts.docker`), on a first
            launch. ``None`` on a relaunch/resume of an existing host, which has no session labels
            handy.
        :param previous_sandbox_id: The host's previous :meth:`HostLifecycle.provision` /
            :meth:`HostLauncher.start_host` return value, on a relaunch or resume of an
            existing host, or ``None`` for a first launch. Lets a provider whose sandbox id
            encodes identity (rather than being an opaque handle) recover that identity when no
            *labels* are available.
        """

    def reaper_identity(self, workspace_id: int) -> AbstractContextManager[None]:
        """Bind credentials needed for background cleanup in one workspace."""
        return nullcontext()

    @abstractmethod
    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Start ``omnigent host`` in the sandbox and return the workspace path.

        :param sandbox_id: The sandbox from :meth:`provision`.
        :param token: The raw launch token the host authenticates with.
        :param host_id: Server-chosen host identity, e.g. ``"host_a1b2c3d4..."``.
        :param host_name: Server-chosen host display name, e.g.
            ``"managed-a1b2c3d4"``.
        :param server_url: URL of this server the host dials back to.
        :param repos: Repositories to clone into ``<workspace>/<repo_name>``
            (empty for an empty workspace). The returned path is the single
            clone directory when exactly one repo is cloned, else the
            workspace root that parents them all.
        :param host_config: Deployment-supplied ``~/.omnigent/config.yaml``
            content installed into the sandbox's config BEFORE the host starts.
        :param on_stage: Progress observer invoked with ``"cloning"`` and
            ``"starting"``.
        :returns: The absolute in-sandbox workspace path — the working
            directory the host starts the agent in.
        """


class ExecModelHostLauncher(HostLauncher, HostExecTransport):
    """
    Default exec-model host launcher for providers that exec into a running
    sandbox (e.g. ``docker``).

    Provides the default :meth:`start_host`, :meth:`run_background`, and
    :meth:`materialize_workspace` that compose :meth:`run` into the full
    managed-host bootstrap. A provider that only needs to change how the
    repository is obtained overrides :meth:`materialize_workspace` alone.

    Entrypoint-as-host and provider-native host launchers inherit
    :class:`HostLauncher` directly and do NOT need ``run()`` or any
    exec transport.
    """

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
        runner_env: Mapping[str, str] | None = None,
    ) -> str:
        """
        Start ``omnigent host`` in the sandbox and return the workspace path.

        The default is the EXEC model: probe ``$HOME``, create
        ``<HOME>/workspace``, clone each requested repo into it (via
        :meth:`materialize_workspace`), merge any *host_config* into
        ``~/.omnigent/config.yaml``, and start the host detached
        (``setsid``-backgrounded, identity + token in the process
        environment) — all driven through :meth:`run` / :meth:`run_background`.

        The working directory is the single clone directory when exactly one
        repo is cloned, else the workspace root parenting them all (or an empty
        workspace when none are requested). Clones run sequentially here.

        Idempotent against piling up hosts: when a supervised ``omnigent
        host`` loop for *host_id* is already running in the sandbox (see
        :meth:`HostExecTransport.is_host_supervisor_running`) — a RELAUNCH
        onto a sandbox that never actually died, or a repeat launch onto a
        provider whose sandbox outlives any one session (Superchat's ``docker``
        provider never terminates its container — see
        :mod:`omnigent.hosts.docker`) — this skips cloning
        and starting a second loop and returns the already-running host's
        workspace path unchanged, computed the same way without touching
        the live workspace.

        :param runner_env: Resolved ``{name: value}`` pairs from the server's
            own process environment (``host.runner_env`` — see
            :mod:`omnigent.server.hosts`), delivered to the host
            process via :meth:`run_background`'s ``env`` (never the command
            string, so values never appear in argv/``ps``/logs), together
            with :data:`~omnigent.host.connect.RUNNER_ENV_PASSTHROUGH_ENV_VAR`
            naming them so :mod:`omnigent.host.connect` forwards them to every
            runner the host spawns. ``None``/empty is a no-op.
        :returns: The absolute in-sandbox workspace path.
        """
        if any(repo.git_clone != _host_types.GitCloneOptions() for repo in repos):
            if not self.capabilities.git_clone_options:
                raise click.ClickException(
                    f"host provider '{self.provider}' does not support host.git_clone options"
                )
        home = self.run(sandbox_id, 'printf %s "$HOME"').stdout.strip()
        if not home:
            raise click.ClickException(
                f"could not resolve $HOME inside sandbox '{sandbox_id}' — "
                "the configured image must provide a usable shell environment"
            )
        workspace = f"{home}/workspace"
        already_running = self.is_host_supervisor_running(sandbox_id, host_id)
        if repos:
            if already_running:
                # Don't touch the live host's workspace — just resolve the same
                # deterministic path a fresh clone would have landed in.
                clone_dirs = [
                    f"{workspace}/{dirname}" for dirname in _host_types.clone_dir_names(repos)
                ]
            else:
                if on_stage is not None:
                    on_stage("cloning")
                self.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
                # Distinct URLs can derive the same repo_name (e.g. two orgs' "api");
                # disambiguate so they don't clone into one colliding directory.
                clone_dirs = []
                for repo, dirname in zip(repos, _host_types.clone_dir_names(repos), strict=True):
                    materialize = self.materialize_workspace
                    # Preserve the old override signature when no policy was configured.
                    if repo.git_clone != _host_types.GitCloneOptions():
                        materialize = partial(materialize, git_clone=repo.git_clone)
                    clone_dirs.append(
                        materialize(
                            sandbox_id,
                            workspace=workspace,
                            repo_url=repo.url,
                            repo_branch=repo.branch,
                            repo_name=dirname,
                        )
                    )
            # One repo → drop the agent straight into it; several → the
            # workspace root that parents them all.
            workspace = clone_dirs[0] if len(clone_dirs) == 1 else workspace
        if already_running:
            return workspace
        if not repos:
            self.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
        if on_stage is not None:
            on_stage("starting")
        if host_config is not None or self.capabilities.resume_stopped:
            self.run(sandbox_id, render_host_config_write_command(host_config or {}))
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in (
                (HOST_TOKEN_ENV_VAR, token),
                (HOST_ID_ENV_VAR, host_id),
                (HOST_NAME_ENV_VAR, host_name),
            )
        )
        host_env: dict[str, str] = dict(runner_env or {})
        if host_env:
            from omnigent.host.connect import RUNNER_ENV_PASSTHROUGH_ENV_VAR

            host_env[RUNNER_ENV_PASSTHROUGH_ENV_VAR] = ",".join(host_env)
        self.run_background(
            sandbox_id,
            f"{env_prefix} omnigent host --server {shlex.quote(server_url)}",
            host_id=host_id,
            env=host_env,
        )
        return workspace

    def materialize_workspace(
        self,
        sandbox_id: str,
        *,
        workspace: str,
        repo_url: str,
        repo_branch: str | None,
        repo_name: str | None,
        on_stage: Callable[[str], None] | None = None,
        git_clone: _host_types.GitCloneOptions = _host_types.GitCloneOptions(),
    ) -> str:
        """
        Materialize the requested repository into the sandbox and return the
        working directory the host should start in.

        The default is ``git clone`` into ``<workspace>/<repo_name>``. Override
        to resolve a local checkout instead of cloning.
        """
        if on_stage is not None:
            on_stage("cloning")
        clone_dir = f"{workspace}/{repo_name}"
        command = shlex.join(
            ["git", "clone", *git_clone.clone_args(repo_branch), "--", repo_url, clone_dir]
        )
        try:
            self.run(
                sandbox_id,
                command,
            )
        except click.ClickException as exc:
            raise click.ClickException(
                f"failed to clone repository '{repo_url}'"
                f"{f' (branch {repo_branch!r})' if repo_branch else ''}: {exc.message}"
            ) from exc
        return clone_dir
