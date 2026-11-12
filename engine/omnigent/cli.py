"""The ``omnigent`` console script: the ``host`` command group.

Superchat runs one command through it, ``omnigent host --server <url>``, in
each user's host container.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

import click
import psutil
import yaml

from omnigent.config import (
    _merge_effective_config,
    global_config_path,
    load_global_config,
    load_local_config,
)
from omnigent.host.daemon_lifecycle import (
    HostDaemonRecord as _HostDaemonRecord,
)
from omnigent.host.daemon_lifecycle import (
    daemon_record_path as _daemon_record_path_for,
)
from omnigent.host.daemon_lifecycle import (
    daemon_registry_dir as _daemon_registry_dir_for,
)
from omnigent.host.daemon_lifecycle import (
    normalize_daemon_target as _normalize_daemon_target_impl,
)
from omnigent.host.daemon_lifecycle import pid_alive as _pid_alive
from omnigent.host.daemon_lifecycle import (
    record_flock_is_held as _record_flock_is_held,
)
from omnigent.host.daemon_lifecycle import write_daemon_record as _write_daemon_record_impl
from omnigent.process_logging import (
    LOG_LEVEL_ENV_VAR,
    LOG_TO_STDERR_ENV_VAR,
    data_dir,
)

_GLOBAL_CONFIG_PATH: Path = Path.home() / ".omnigent" / "config.yaml"


# Project-level config relative to cwd, analogous to .git/config.
# Resolved at call time so tests can control cwd.
_LOCAL_CONFIG_RELPATH: Path = Path(".omnigent") / "config.yaml"


_HOST_DAEMON_STOP_GRACE_S = 5.0


_HOST_SESSION_STOP_MAX_WORKERS = 8


_HOST_SESSION_ACTIVE_STATUSES = frozenset({"running", "waiting"})


_HOST_DAEMON_PROXY_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


_HostJsonValue: TypeAlias = (
    str | int | float | bool | list["_HostJsonValue"] | dict[str, "_HostJsonValue"] | None
)


_HostJsonObject: TypeAlias = dict[str, _HostJsonValue]


_HostSessionRow: TypeAlias = dict[str, _HostJsonValue]


_HostPayload: TypeAlias = dict[str, _HostJsonValue]


def _effective_global_config_path() -> Path:
    """
    Return the path to the user-level Omnigent config.

    :returns: ``$OMNIGENT_CONFIG_HOME/config.yaml`` when the env
        override is set, otherwise :data:`_GLOBAL_CONFIG_PATH`.
    """
    return global_config_path(_GLOBAL_CONFIG_PATH)


def _display_path(path: Path) -> str:
    """
    Format a filesystem path for display, collapsing the home prefix to ``~``.

    A path under the user's home directory is shown as ``~/...`` for
    readability; anything else is shown as its plain string. Unlike a
    hardcoded ``~/.omnigent/...`` literal, this reflects the *actual*
    effective path — so a state dir outside ``$HOME`` (an
    ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` override) renders as
    its real location rather than a misleading ``~``.

    :param path: The path to display, e.g.
        ``Path("/Users/alice/.omnigent/logs/server/server-ab12.log")``.
    :returns: ``"~/.omnigent/..."`` when *path* is under ``$HOME``,
        otherwise ``str(path)``.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        # Not under $HOME (e.g. an OMNIGENT_DATA_DIR outside home).
        return str(path)


def _load_global_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the global omnigent config from ``~/.omnigent/config.yaml``.

    Returns an empty dict when the file does not exist or is empty. The
    host reads its ``server`` key when no ``--server`` is passed.

    :returns: Parsed YAML as a dict, e.g. ``{"server": "https://engine.example.com"}``.
    """
    return load_global_config(_effective_global_config_path())


def _load_local_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Load the project-level config from ``.omnigent/config.yaml`` in cwd.

    Returns an empty dict when the file does not exist or is empty.

    :returns: Parsed YAML as a dict.
    """
    return load_local_config(Path.cwd() / _LOCAL_CONFIG_RELPATH)


def _load_effective_config() -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Merge global and project-level config.

    Precedence (highest last): global (``~/.omnigent/config.yaml``)
    → local (``.omnigent/config.yaml`` in cwd).  Project config
    always wins so per-repo settings override user defaults.

    The ``harness`` mapping is deep-merged (per-harness sub-keys, local
    winning per-field) via :func:`omnigent.config._merge_effective_config`
    so a project's per-harness overrides augment — rather than replace —
    the user's global ones. Every other key is a shallow replace.

    :returns: Merged config dict.
    """
    return _merge_effective_config(_load_global_config(), _load_local_config())


def _print_version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """Click callback that prints the package version and exits."""
    if not value or ctx.resilient_parsing:
        return
    from omnigent.version import VERSION

    click.echo(f"omnigent {VERSION}")
    ctx.exit()


def _set_debug_logging(
    _ctx: click.Context,
    _param: click.Parameter,
    value: bool,
) -> bool:
    if value:
        os.environ[LOG_LEVEL_ENV_VAR] = "DEBUG"
    return value


def _set_log_to_stderr(
    _ctx: click.Context,
    _param: click.Parameter,
    value: bool,
) -> bool:
    if value:
        os.environ[LOG_TO_STDERR_ENV_VAR] = "1"
    return value


def _extract_global_logging_flags(argv: list[str]) -> tuple[list[str], bool, bool]:
    """Remove global logging flags before run-shorthand rewriting."""
    debug_logging = False
    log_to_stderr = False
    remaining: list[str] = []
    passthrough = False
    for token in argv:
        if token == "--":
            passthrough = True
            remaining.append(token)
        elif not passthrough and token == "--debug":
            debug_logging = True
        elif not passthrough and token == "--log-to-stderr":
            log_to_stderr = True
        else:
            remaining.append(token)
    return remaining, debug_logging, log_to_stderr


@click.group()
@click.option(
    "--debug",
    "debug_logging",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_set_debug_logging,
    help="Enable verbose DEBUG logging for Omnigent processes.",
)
@click.option(
    "--log-to-stderr",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_set_log_to_stderr,
    help="Mirror process logs to the terminal when stderr is interactive.",
)
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version_callback,
    expose_value=False,
    is_eager=True,
    help="Show the version and exit.",
)
def cli() -> None:
    """Omnigent CLI."""


def main() -> None:
    """Console-script entry point for ``omnigent``.

    Superchat runs exactly one command through it: ``omnigent host --server
    <url>`` in each user's host container. Global logging flags may precede
    the command (``omnigent --debug host ...``).
    """
    argv, debug_logging, log_to_stderr = _extract_global_logging_flags(sys.argv[1:])
    if debug_logging:
        os.environ[LOG_LEVEL_ENV_VAR] = "DEBUG"
    if log_to_stderr:
        os.environ[LOG_TO_STDERR_ENV_VAR] = "1"
    cli(args=argv or ["--help"], prog_name="omnigent")


def _is_server_url(value: str) -> bool:
    """Return whether *value* is a server URL.

    :param value: CLI argument value, e.g. ``"http://localhost:6767"``.
    :returns: ``True`` for ``http://`` or ``https://`` URLs.
    """
    return value.startswith(("http://", "https://"))


_HOST_PID_PATH = data_dir() / "host.pid"


@dataclass(frozen=True)
class _HostHttpResult:
    """
    Decoded Omnigent management HTTP response.

    :param status_code: HTTP status code, e.g. ``200``. ``0`` means no
        HTTP response was received because the request failed locally.
    :param body: Decoded JSON object or response text, e.g.
        ``{"data": []}`` or ``"not found"``.
    :param unreachable: ``True`` when the request failed because nothing
        answered at a loopback address (connection refused against the local
        server), as opposed to a slow or erroring server or a transient
        failure against a remote one.
    """

    status_code: int
    body: _HostJsonObject | str
    unreachable: bool = False


@dataclass(frozen=True)
class _DaemonSessionsResult:
    """
    Sessions fetched for one daemon target.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://engine.example.com"``.
    :param sessions: Session rows owned by the daemon host id.
    :param error: Human-readable error text, or ``None`` on success.
    :param unreachable: ``True`` when the error means the server is not
        answering at all (dead or gone), not merely slow or erroring.
    """

    base_url: str
    sessions: list[_HostSessionRow]
    error: str | None
    unreachable: bool = False


@dataclass(frozen=True)
class _SessionsPageResult:
    """
    Decoded sessions page.

    :param sessions: Session rows returned by the page.
    :param last_id: Last session id in the page, e.g. ``"conv_abc123"``.
    :param has_more: Whether another page should be fetched.
    :param error: Human-readable error text, or ``None`` on success.
    :param unreachable: ``True`` when the page fetch failed because the
        server is not answering at all.
    """

    sessions: list[_HostSessionRow]
    last_id: str | None
    has_more: bool
    error: str | None
    unreachable: bool = False


@dataclass(frozen=True)
class _SessionPagesResult:
    """
    Accumulated sessions from a paginated query.

    :param sessions: Session rows across all fetched pages.
    :param error: Human-readable error text, or ``None`` on success.
    :param unreachable: ``True`` when the query failed because the server
        is not answering at all.
    """

    sessions: list[_HostSessionRow]
    error: str | None
    unreachable: bool = False


def _normalize_daemon_target(server_url: str | None) -> str:
    """
    Normalize a daemon target key.

    :param server_url: Requested Omnigent server URL, e.g.
        ``"https://engine.example.com/"``.
    :returns: The URL without a trailing slash.
    """
    return _normalize_daemon_target_impl(server_url)


def _daemon_registry_dir() -> Path:
    """
    Return the directory containing per-target daemon registry records.

    Tests patch :data:`_HOST_PID_PATH`, so derive the registry root from
    the pidfile's parent instead of capturing ``Path.home()`` separately.

    :returns: Registry directory path, e.g.
        ``Path("~/.omnigent/daemons")``.
    """
    return _daemon_registry_dir_for(_HOST_PID_PATH.parent)


def _daemon_record_path(target: str) -> Path:
    """
    Return the registry JSON path for *target*.

    :param target: Normalized daemon target, e.g.
        ``"https://engine.example.com"`` or ``"local"``.
    :returns: JSON registry path for the target.
    """
    return _daemon_record_path_for(target, base_dir=_HOST_PID_PATH.parent)


def _record_from_json(raw: _HostJsonObject) -> _HostDaemonRecord | None:
    """
    Parse a daemon record from decoded JSON.

    :param raw: Decoded JSON object, e.g.
        ``{"pid": 4242, "target": "local", "mode": "local"}``.
    :returns: Parsed :class:`_HostDaemonRecord`, or ``None`` if the
        record is malformed.
    """
    try:
        pid_raw = raw["pid"]
        if not isinstance(pid_raw, str | int) or isinstance(pid_raw, bool):
            return None
        pid = int(pid_raw)
        target = str(raw["target"])
        mode = str(raw["mode"])
        started_at_raw = raw["started_at"]
        if not isinstance(started_at_raw, str | int) or isinstance(started_at_raw, bool):
            return None
        started_at = int(started_at_raw)
    except (KeyError, TypeError, ValueError):
        return None
    if mode not in {"local", "server"} or not target:
        return None
    server_url = raw.get("server_url")
    log_path = raw.get("log_path")
    host_id = raw.get("host_id")
    resolved_server_url = raw.get("resolved_server_url")
    config_sig = raw.get("config_sig")
    return _HostDaemonRecord(
        pid=pid,
        target=target,
        mode=mode,
        server_url=server_url if isinstance(server_url, str) and server_url else None,
        log_path=log_path if isinstance(log_path, str) and log_path else None,
        started_at=started_at,
        host_id=host_id if isinstance(host_id, str) and host_id else None,
        resolved_server_url=(
            resolved_server_url
            if isinstance(resolved_server_url, str) and resolved_server_url
            else None
        ),
        config_sig=config_sig if isinstance(config_sig, str) and config_sig else None,
    )


def _read_daemon_record(path: Path) -> _HostDaemonRecord | None:
    """
    Read a daemon registry record from disk.

    :param path: JSON file path to read, e.g.
        ``Path("~/.omnigent/daemons/abc.json")``.
    :returns: Parsed daemon record, or ``None`` if unreadable or malformed.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _record_from_json(cast(_HostJsonObject, raw))


def _write_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Persist a daemon registry record.

    :param record: Record to write, e.g. a local daemon record with
        ``target == "local"``.
    """
    _write_daemon_record_impl(record, base_dir=_HOST_PID_PATH.parent)


def _delete_daemon_record(record: _HostDaemonRecord) -> None:
    """
    Delete a daemon registry record if it exists.

    :param record: Record whose target path should be removed.
    """
    with contextlib.suppress(OSError):
        _daemon_record_path(record.target).unlink()


def _list_daemon_records() -> list[_HostDaemonRecord]:
    """
    List daemon registry records.

    :returns: Records ordered by ``started_at`` descending.
    """
    records: dict[str, _HostDaemonRecord] = {}
    registry = _daemon_registry_dir()
    if registry.exists():
        for path in registry.glob("*.json"):
            record = _read_daemon_record(path)
            if record is not None:
                records[record.target] = record
    return sorted(records.values(), key=lambda r: r.started_at, reverse=True)


def _find_daemon_record(target: str) -> _HostDaemonRecord | None:
    """
    Find a daemon record by target.

    :param target: Normalized daemon target, e.g. ``"local"``.
    :returns: Matching daemon record, including a pre-canonicalization record,
        or ``None``.
    """
    records = _list_daemon_records()
    for record in records:
        if record.target == target:
            return record
    for record in records:
        if _normalize_daemon_target(record.target) == target:
            return record
    return None


def _load_existing_host_id() -> str | None:
    """
    Load the existing local host id without creating one.

    :returns: Host id from config, e.g. ``"host_abc123"``, or ``None``.
    """
    from omnigent.host.identity import load_host_identity_if_present

    try:
        identity = load_host_identity_if_present()
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return identity.host_id if identity is not None else None


def _daemon_owner_is_live(record: _HostDaemonRecord) -> bool:
    """Whether the daemon that wrote *record* is still alive.

    A held record flock is a definitive live owner (the kernel drops it on
    death), so it is the fast positive signal. When the lock is free or can't
    be probed (no ``fcntl``, unreadable), fall back to whether the PID is
    alive and still names the recorded daemon (see
    :func:`_pid_is_recorded_daemon`) — so a daemon still mid-startup (hasn't
    grabbed the lock yet) is not reaped, while a pid recycled to an unrelated
    process is. Reaping therefore requires a free lock and a dead-or-foreign
    PID.

    :param record: Existing daemon record whose original target identifies the
        lock path held by its owner.
    :returns: ``True`` if the daemon should be treated as alive.
    """
    if _record_flock_is_held(_daemon_record_path(record.target)) is True:
        return True
    return _pid_is_recorded_daemon(record)


def _foreground_daemon_record(
    *,
    target: str,
    host_id: str | None,
) -> _HostDaemonRecord:
    """
    Build the registry record for the current foreground host process.

    :param target: Normalized server URL, e.g. ``"https://engine.example.com"``.
    :param host_id: Local host id, e.g. ``"host_abc123"``.
    :returns: Daemon registry record for ``os.getpid()``.
    """
    return _HostDaemonRecord(
        pid=os.getpid(),
        target=target,
        mode="server",
        server_url=target,
        log_path=None,
        started_at=int(time.time()),
        host_id=host_id,
        resolved_server_url=None,
        config_sig=None,
    )


_DAEMON_PID_START_TOLERANCE_S = 5.0


def _describe_pid(pid: int) -> str:
    """Name what actually holds *pid* — user and command — for error copy.

    Best-effort: any psutil failure degrades to the bare pid.
    """
    try:
        proc = psutil.Process(pid)
        user = proc.username()
        name = proc.name()
        return f"pid={pid} ({name}, user {user})"
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return f"pid={pid}"


def _pid_is_recorded_daemon(record: _HostDaemonRecord) -> bool:
    """Whether *record*'s pid still names the recorded daemon, not a recycled pid.

    A bare existence check keeps trusting the pid after a reboot: the kernel
    recycles low pids, and a fresh system daemon can then hold the recorded
    pid forever — the host refuses to start and ``host stop`` tries to signal
    an unrelated process. The record's own ``started_at`` is the identity: a
    process created *after* the record's start time (beyond a small clock
    tolerance) cannot be the daemon that wrote it. The check is one-sided on
    purpose — a daemon always exists before it writes its record, so a
    creation time *earlier* than ``started_at`` (even by minutes of slow
    startup or sign-in) is still ours. Legacy records without a start time,
    and pids whose creation time can't be read, fall back to alive-only.
    """
    if not _pid_alive(record.pid):
        return False
    if record.started_at <= 0:
        return True
    try:
        created = psutil.Process(record.pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except Exception:  # noqa: BLE001 — unreadable creation time: alive-only
        return True
    return created <= record.started_at + _DAEMON_PID_START_TOLERANCE_S


def _live_daemon_conflict(record: _HostDaemonRecord) -> _HostDaemonRecord | None:
    """
    Find a live daemon that already serves a foreground record target.

    Liveness uses the record flock, falling back to a PID check (see
    :func:`_daemon_owner_is_live`).

    :param record: Foreground daemon record this process wants to claim.
    :returns: Conflicting live record, or ``None``.
    """
    existing = _find_daemon_record(record.target)
    if existing is None or existing.pid == record.pid:
        return None
    if _daemon_owner_is_live(existing):
        return existing
    # Dead, or alive but not our daemon (pid recycled after a reboot):
    # the record is stale, not a conflict. Prune it and start normally.
    if _pid_alive(existing.pid):
        click.echo(
            f"Removing stale daemon record for {existing.target!r}: "
            f"{_describe_pid(existing.pid)} is not this daemon (pid recycled).",
            err=True,
        )
    _delete_daemon_record(existing)
    return None


def _claim_foreground_daemon_record(
    record: _HostDaemonRecord,
) -> _HostDaemonRecord | None:
    """
    Persist a foreground daemon record unless a live duplicate exists.

    :param record: Foreground process record, e.g. one with
        ``pid == os.getpid()``.
    :returns: Previous record for the same target, or ``None``.
    :raises click.ClickException: If a live daemon already serves the
        same target.
    """
    conflict = _live_daemon_conflict(record)
    if conflict is not None:
        raise click.ClickException(
            "A host daemon is already running for this server "
            f"({_describe_pid(conflict.pid)}, target={conflict.target}). "
            "Run `omnigent host status` to inspect it or "
            f"`omnigent host stop --server {conflict.target}` to stop it first."
        )
    previous = _find_daemon_record(record.target)
    if (
        previous is not None
        and previous.pid != record.pid
        and not _pid_is_recorded_daemon(previous)
    ):
        # Stale for the same recycled-pid reason as the conflict path: the
        # recorded pid exists but is not our daemon.
        _delete_daemon_record(previous)
        previous = None
    elif previous is not None and not _pid_alive(previous.pid):
        _delete_daemon_record(previous)
        previous = None
    _write_daemon_record(record)
    return previous


def _restore_replaced_daemon_record(
    record: _HostDaemonRecord,
    previous: _HostDaemonRecord | None,
) -> None:
    """
    Restore the record replaced by a foreground host process.

    If another process has already written a newer record for the same
    target, this function leaves it untouched.

    :param record: Foreground daemon record written by this process.
    :param previous: Previous record returned by
        :func:`_claim_foreground_daemon_record`, or ``None``.
    """
    current = _read_daemon_record(_daemon_record_path(record.target))
    if current is None:
        return
    if current.pid != record.pid or current.started_at != record.started_at:
        return
    if previous is None:
        _delete_daemon_record(record)
        return
    _write_daemon_record(previous)


def _load_or_create_host_id() -> str | None:
    """
    Load or create the host id used by a foreground host process.

    :returns: Host id from local config, e.g. ``"host_abc123"``, or
        ``None`` if the identity file cannot be created.
    """
    host_id = _load_existing_host_id()
    if host_id is not None:
        return host_id
    from omnigent.host.identity import load_or_create_host_identity

    try:
        return load_or_create_host_identity().host_id
    except (OSError, ValueError):
        # OSError: identity file unwritable. ValueError: a malformed persisted /
        # env host_id — a foreground host has nothing to key on, so degrade to
        # None; the loud fail-fast is on the `omnigent host` connect path.
        return None


def _build_host_daemon_env() -> dict[str, str]:
    """
    Build the environment for the host service process.

    The service connects to an already-running Omnigent server, so it only
    needs process essentials, TLS trust, proxy selectors and the host
    identity. Runners launched by the host still pass through
    :func:`omnigent.host.connect._build_runner_env`.

    :returns: Environment dict for the service definition.
    """
    from omnigent.host.connect import (
        _RUNNER_ENV_ALLOWLIST,
        _RUNNER_ENV_ALLOWLIST_PREFIXES,
    )
    from omnigent.host.identity import (
        HOST_ID_ENV_VAR,
        HOST_NAME_ENV_VAR,
        HOST_TOKEN_ENV_VAR,
    )

    identity_env_vars = frozenset(
        {
            HOST_ID_ENV_VAR,
            HOST_NAME_ENV_VAR,
            HOST_TOKEN_ENV_VAR,
        }
    )

    # Allowlist the daemon's environment: process essentials, TLS trust,
    # standard proxy selectors and the host identity, but not unrelated
    # provider secrets like ANTHROPIC_API_KEY / OPENAI_API_KEY.
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _RUNNER_ENV_ALLOWLIST
        or key in _HOST_DAEMON_PROXY_ENV_ALLOWLIST
        or key in identity_env_vars
        or key.startswith(_RUNNER_ENV_ALLOWLIST_PREFIXES)
    }
    # The daemon outlives the dispatch that spawned it and is reused by later
    # invocations, so a dispatch-scoped caller trace context must not stick to
    # it — a reused daemon would funnel every later run into the first
    # caller's (long-dead) trace.
    from omnigent.runtime.telemetry import (
        DISPATCH_TRACEPARENT_ENV_VAR,
        DISPATCH_TRACESTATE_ENV_VAR,
    )

    env.pop(DISPATCH_TRACEPARENT_ENV_VAR, None)
    env.pop(DISPATCH_TRACESTATE_ENV_VAR, None)
    return env


class _HostGroup(click.Group):
    """
    ``host`` group that accepts a server URL as a positional argument.

    ``omnigent host <url>`` is shorthand for ``omnigent host --server <url>``
    when ``<url>`` is an HTTP(S) URL. A leading token that names a subcommand
    still dispatches to it; other tokens get Click's unknown-command error.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        """Redirect a leading URL positional into ``--server`` before Click parses."""
        return super().parse_args(ctx, self._rewrite_positional_server(ctx, list(args)))

    def _rewrite_positional_server(self, ctx: click.Context, args: list[str]) -> list[str]:
        """
        Rewrite a leading URL positional into an explicit ``--server``.

        :param ctx: Click context for the ``host`` group.
        :param args: Raw argument tokens for the group.
        :returns: Possibly-rewritten argument tokens.
        :raises click.UsageError: If the positional URL is combined with
            ``--server`` or with extra positionals.
        """
        if ctx.resilient_parsing or not args:
            return args
        try:
            parser = self.make_parser(ctx)
            # Classify options after the positional URL as options.
            parser.allow_interspersed_args = True
            opts, positionals, _ = parser.parse_args(list(args))
        except click.UsageError:
            return args
        if not positionals or positionals[0] in self.commands:
            return args
        if not _is_server_url(positionals[0]):
            return args
        url = positionals[0]
        if opts.get("server") is not None:
            raise click.UsageError(
                "Pass the server URL either positionally or via --server, not both."
            )
        if positionals[1:]:
            raise click.UsageError(f"Unexpected extra argument(s): {' '.join(positionals[1:])}")
        remaining = list(args)
        remaining.remove(url)
        return ["--server", url, *remaining]


@cli.group("host", cls=_HostGroup, invoke_without_command=True)
@click.option("--server", default=None, help="Omnigent server URL to register with.")
@click.option(
    "--non-interactive",
    "non_interactive",
    is_flag=True,
    default=False,
    help="Never prompt. Accepted for service and script callers.",
)
@click.pass_context
def host(
    ctx: click.Context,
    server: str | None,
    non_interactive: bool,
) -> None:
    """
    Register this machine as a host with a server and run until stopped.

    \b
    Examples:
      omnigent host https://engine.example.com
      omnigent host --server https://engine.example.com
      omnigent host enable   # install and start a per-user system service
      omnigent host disable  # stop and remove the per-user system service

    The server URL may be given positionally (``omnigent host <url>``) or
    via ``--server <url>``; without either, the ``server`` config key is
    used. A leading ``status``, ``stop``, ``enable``, ``disable``,
    ``stop-session``, or ``reset-id`` token runs that subcommand instead.

    \f
    :param ctx: Click invocation context. ``ctx.invoked_subcommand`` is
        set when a management subcommand such as ``"status"`` is running.
    :param server: Omnigent server URL, e.g. ``"https://engine.example.com"``.
        ``None`` falls back to config.
    :param non_interactive: Kept for callers that pass it; the host never
        prompts.
    """
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    ctx.obj["non_interactive"] = non_interactive
    if ctx.invoked_subcommand is not None:
        return
    resolved = _resolve_host_server(server)
    if resolved is None:
        raise click.UsageError("Pass the server URL: omnigent host --server <url>.")

    from omnigent.host.connect import run_host_process

    target = _normalize_daemon_target(resolved)
    record = _foreground_daemon_record(
        target=target,
        host_id=_load_or_create_host_id(),
    )
    previous = _claim_foreground_daemon_record(record)
    try:
        run_host_process(server_url=resolved, daemon_target=target)
    except KeyboardInterrupt:
        pass
    finally:
        _restore_replaced_daemon_record(record, previous)


def _host_group_option(ctx: click.Context, key: str) -> str | None:
    """
    Read a group-level ``omnigent host`` option for a subcommand.

    :param ctx: Click context passed to a host subcommand.
    :param key: Group option key, e.g. ``"server"``.
    :returns: The string option value, or ``None``.
    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _resolve_host_server(server: str | None) -> str | None:
    """
    Resolve a host-management server from CLI or config.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://engine.example.com"``. ``None`` or empty falls back to the
        ``server`` config key.
    :returns: Normalized server URL, or ``None`` when none is configured.
    """
    if not server:
        configured = _load_effective_config().get("server")
        server = str(configured) if configured else None
    return _resolve_server_url(server) if server else None


def _daemon_base_url(record: _HostDaemonRecord) -> str:
    """
    Resolve the Omnigent server URL for a daemon record.

    :param record: Daemon registry record to inspect.
    :returns: Omnigent server URL, e.g. ``"https://engine.example.com"``.
    """
    return (record.server_url or record.target).rstrip("/")


def _selected_daemon_records(
    *,
    server: str | None,
    all_targets: bool,
    default_all: bool,
) -> list[_HostDaemonRecord]:
    """
    Select daemon records for a host-management command.

    :param server: Explicit ``--server`` value, e.g.
        ``"https://engine.example.com"``. ``None`` may mean
        all targets or config/local depending on ``default_all``.
    :param all_targets: Whether ``--all`` was passed.
    :param default_all: Whether no selector should mean all records.
    :returns: Matching daemon records.
    :raises click.ClickException: If ``--server`` and ``--all`` conflict.
    """
    if all_targets and server is not None:
        raise click.ClickException("Use either --server or --all, not both.")
    if all_targets or (server is None and default_all):
        return _list_daemon_records()
    target = _normalize_daemon_target(_resolve_host_server(server))
    record = _find_daemon_record(target)
    return [] if record is None else [record]


def _host_http_json(
    *,
    base_url: str,
    method: str,
    path: str,
    params: dict[str, str | int] | None = None,
    json_body: _HostJsonObject | None = None,
    timeout_s: float = 10.0,
    host_id: str | None = None,
) -> _HostHttpResult:
    """
    Send one management request to an Omnigent server.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://engine.example.com"``.
    :param method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
    :param path: Request path beginning with ``/``, e.g.
        ``"/v1/hosts/host_abc"``.
    :param params: Optional query parameters, e.g. ``{"limit": 1000}``.
    :param json_body: Optional JSON body, e.g.
        ``{"type": "stop_session", "data": {}}``.
    :param timeout_s: Request timeout in seconds, e.g. ``2.0`` for a
        quick liveness probe. Defaults to ``10.0`` for management calls.
    :param host_id: The host this request is scoped to (host-control, or a
        host-backed session event like stop_session), so it reaches the replica
        holding that host's tunnel. ``None`` for non-host-scoped calls; the
        builder emits the routing header only on the workspace-hosted server.
    :returns: Decoded HTTP result.
    """
    import httpx
    from omnigent_client._http import is_loopback_url

    from omnigent.cli_auth import databricks_request_headers, load_token

    token = os.environ.get("OMNIGENT_REMOTE_AUTH_TOKEN", "").strip() or load_token(base_url)
    headers = databricks_request_headers(base_url, bearer_token=token or None, host_id=host_id)
    try:
        with httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout_s,
            trust_env=not is_loopback_url(base_url),
        ) as client:
            resp = client.request(method, path, params=params, json=json_body)
    except (httpx.HTTPError, OSError, ImportError) as exc:
        # httpx raises ImportError while building the client when the ambient
        # environment selects a proxy whose optional extra is missing (e.g. an
        # ``ALL_PROXY=socks5://…`` shell without ``httpx[socks]`` installed).
        return _HostHttpResult(
            status_code=0,
            body=f"{type(exc).__name__}: {exc}",
            # Nothing accepted the connection at a loopback address: the
            # local server is gone, not merely slow (ReadTimeout) or erroring
            # (HTTP status). Remote connect failures stay ``False`` — DNS
            # hiccups, network blips, or TLS faults can be transient against
            # a live server, so callers keep the loud ``--force`` guidance.
            unreachable=(
                is_loopback_url(base_url)
                and isinstance(exc, (httpx.ConnectError, ConnectionRefusedError))
            ),
        )
    body: _HostJsonObject | str
    try:
        decoded = resp.json()
    except ValueError:
        body = resp.text
    else:
        body = cast(_HostJsonObject, decoded) if isinstance(decoded, dict) else str(decoded)
    return _HostHttpResult(status_code=resp.status_code, body=body)


def _host_error_text(body: _HostJsonObject | str) -> str:
    """
    Extract a concise error string from an Omnigent response body.

    :param body: Response body decoded by :func:`_host_http_json`.
    :returns: Human-readable error text.
    """
    if isinstance(body, str):
        return body[:400]
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    error = body.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return json.dumps(body)[:400]


def _daemon_session_request_params(
    *,
    connected_only: bool,
    after: str | None,
) -> dict[str, str | int]:
    """
    Build query parameters for one sessions page.

    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :param after: Optional cursor from the prior page, e.g.
        ``"conv_abc123"``.
    :returns: Query parameters for ``GET /v1/sessions``.
    """
    params: dict[str, str | int] = {
        "limit": 1000,
        "include_archived": "true",
        "visibility": "all",
    }
    if connected_only:
        params["connected"] = "true"
    if after is not None:
        params["after"] = after
    return params


def _decode_sessions_page(
    result: _HostHttpResult,
) -> _SessionsPageResult:
    """
    Decode one ``GET /v1/sessions`` response page.

    :param result: HTTP result returned by :func:`_host_http_json`.
    :returns: Decoded page result. ``error`` is ``None`` on success.
    """
    if result.status_code == 0:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=f"session list failed: {_host_error_text(result.body)}",
            unreachable=result.unreachable,
        )
    if result.status_code >= 400:
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error=(f"session list failed ({result.status_code}): {_host_error_text(result.body)}"),
            # No producer sets ``unreachable`` alongside an HTTP status today;
            # propagate defensively so a future one is not silently dropped.
            unreachable=result.unreachable,
        )
    if not isinstance(result.body, dict):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="session list returned a non-object response",
        )
    data = result.body.get("data")
    if not isinstance(data, list):
        return _SessionsPageResult(
            sessions=[],
            last_id=None,
            has_more=False,
            error="session list returned a malformed data field",
        )
    rows = [s for s in data if isinstance(s, dict)]
    last_id = result.body.get("last_id")
    has_more = result.body.get("has_more")
    return _SessionsPageResult(
        sessions=rows,
        last_id=last_id if isinstance(last_id, str) and last_id else None,
        has_more=has_more if isinstance(has_more, bool) else False,
        error=None,
    )


def _fetch_session_pages(
    *,
    base_url: str,
    connected_only: bool,
) -> _SessionPagesResult:
    """
    Fetch every available session page from a server.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://engine.example.com"``.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Accumulated sessions result. ``error`` is ``None`` on success.
    """
    after: str | None = None
    sessions: list[_HostSessionRow] = []
    while True:
        page_result = _host_http_json(
            base_url=base_url,
            method="GET",
            path="/v1/sessions",
            params=_daemon_session_request_params(
                connected_only=connected_only,
                after=after,
            ),
        )
        page = _decode_sessions_page(page_result)
        if page.error is not None:
            # A server that already served a page is provably alive, so a
            # mid-pagination failure is never ``unreachable``: only the very
            # first request (``after is None``) may carry the flag through.
            return _SessionPagesResult(
                sessions=[],
                error=page.error,
                unreachable=page.unreachable and after is None,
            )
        sessions.extend(page.sessions)
        if not page.has_more or page.last_id is None:
            return _SessionPagesResult(sessions=sessions, error=None)
        after = page.last_id


def _sessions_for_daemon(
    record: _HostDaemonRecord,
    *,
    connected_only: bool = False,
) -> _DaemonSessionsResult:
    """
    Fetch sessions owned by a daemon's host id.

    :param record: Daemon record whose sessions should be listed.
    :param connected_only: When ``True``, ask the server for connected
        sessions only.
    :returns: Sessions result. ``error`` is ``None`` on success.
    """
    base_url = _daemon_base_url(record)
    host_id = record.host_id or _load_existing_host_id()
    if not host_id:
        return _DaemonSessionsResult(
            base_url=base_url,
            sessions=[],
            error="host id is not available in local config",
        )
    pages = _fetch_session_pages(
        base_url=base_url,
        connected_only=connected_only,
    )
    if pages.error is not None:
        return _DaemonSessionsResult(
            base_url=base_url,
            sessions=[],
            error=pages.error,
            unreachable=pages.unreachable,
        )
    owned = [s for s in pages.sessions if s.get("host_id") == host_id]
    return _DaemonSessionsResult(base_url=base_url, sessions=owned, error=None)


def _base_daemon_status_payload(record: _HostDaemonRecord) -> _HostPayload:
    """
    Build daemon metadata for status output.

    :param record: Daemon registry record to inspect.
    :returns: JSON-serializable daemon metadata.
    """
    base_url = _daemon_base_url(record)
    host_id = record.host_id or _load_existing_host_id()
    return {
        "target": record.target,
        "mode": record.mode,
        "server_url": base_url,
        "pid": record.pid,
        "process": "online" if _pid_is_recorded_daemon(record) else "offline",
        "log_path": record.log_path,
        "host_id": host_id,
        "host_status": None,
        "sessions": [],
        "error": None,
    }


def _add_daemon_host_status(
    payload: _HostPayload,
) -> None:
    """
    Add host status or host status error to a daemon payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    """
    # A dead process cannot have an online tunnel — skip the round-trip.
    if payload.get("process") != "online":
        payload["host_status"] = "offline"
        return
    base_url = payload.get("server_url")
    host_id = payload.get("host_id")
    if not isinstance(base_url, str):
        return
    if not isinstance(host_id, str) or not host_id:
        payload["error"] = "host id is not available in local config"
        return
    from urllib.parse import quote

    host_result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/hosts/{quote(host_id, safe='')}",
        host_id=host_id,
    )
    if host_result.status_code == 200 and isinstance(host_result.body, dict):
        status = host_result.body.get("status")
        payload["host_status"] = status if isinstance(status, str) else None
    elif host_result.status_code == 0:
        payload["error"] = f"host status failed: {_host_error_text(host_result.body)}"
    elif host_result.status_code >= 400:
        payload["error"] = (
            f"host status failed ({host_result.status_code}): {_host_error_text(host_result.body)}"
        )


def _add_daemon_sessions(
    payload: _HostPayload,
    record: _HostDaemonRecord,
    *,
    connected_sessions_only: bool,
) -> None:
    """
    Add the sessions this daemon's host owns to its payload.

    :param payload: Payload from :func:`_base_daemon_status_payload`.
    :param record: Daemon registry record to inspect.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    """
    sessions_result = _sessions_for_daemon(
        record,
        connected_only=connected_sessions_only,
    )
    payload["sessions"] = cast(_HostJsonValue, sessions_result.sessions)
    if sessions_result.error is not None and payload["error"] is None:
        payload["error"] = sessions_result.error


def _daemon_status_payload(
    record: _HostDaemonRecord,
    *,
    include_sessions: bool,
    connected_sessions_only: bool,
) -> _HostPayload:
    """
    Build a display payload for one daemon.

    :param record: Daemon registry record to inspect.
    :param include_sessions: Whether to include session rows.
    :param connected_sessions_only: Whether session listing should use
        the server's connected filter.
    :returns: JSON-serializable status payload.
    """
    payload = _base_daemon_status_payload(record)
    _add_daemon_host_status(payload)
    if include_sessions:
        _add_daemon_sessions(
            payload,
            record,
            connected_sessions_only=connected_sessions_only,
        )
    return payload


def _echo_daemon_payloads(payloads: list[_HostPayload]) -> None:
    """
    Render host status as one block per daemon target.

    :param payloads: Payloads from :func:`_daemon_status_payload`.
    """
    if not payloads:
        click.echo("No host daemons found.")
        return
    for idx, payload in enumerate(payloads):
        if idx:
            click.echo()
        click.echo(str(payload.get("server_url") or payload.get("target")))
        click.echo(
            f"  pid={payload.get('pid')}  process={payload.get('process') or 'unknown'}  "
            f"host={payload.get('host_status') or 'unknown'}"
        )
        click.echo(f"  host_id={payload.get('host_id') or '-'}")
        if payload.get("error"):
            click.echo(f"  error={payload.get('error')}")
        sessions = payload.get("sessions")
        if isinstance(sessions, list):
            for session in sessions:
                if isinstance(session, dict):
                    click.echo(
                        f"  session {session.get('id')}  status={session.get('status') or '-'}"
                    )


@host.command("enable")
@click.option("--server", default=None, help="Server target for the persistent service.")
@click.pass_context
def host_enable(
    ctx: click.Context,
    server: str | None,
) -> None:
    """Install and start the host as a per-user system service.

    :param ctx: Click context carrying group-level options.
    :param server: Server target; ``None`` falls back to the group option
        and then to config.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    resolved_server = _resolve_host_server(server)
    if resolved_server is None:
        raise click.UsageError("Pass the server URL with --server.")
    target = _normalize_daemon_target(resolved_server)
    record = _find_daemon_record(target)
    if record is not None:
        _terminate_daemon(record, force=False)

    from omnigent.host.service import HostServiceError, enable_user_host_service

    try:
        service = enable_user_host_service(
            resolved_server,
            environment=_build_host_daemon_env(),
        )
    except HostServiceError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Enabled the Omnigent host user service for {target}.")
    click.echo(f"Service definition: {_display_path(service.path)}")
    if service.log_path is not None:
        click.echo(f"Service output: {_display_path(service.log_path)}")


@host.command("disable")
def host_disable() -> None:
    """Stop and remove the current user's host system service."""
    from omnigent.host.service import HostServiceError, disable_user_host_service

    try:
        service = disable_user_host_service()
    except HostServiceError as exc:
        raise click.ClickException(str(exc)) from exc
    # Service managers stop with SIGTERM, which can leave the foreground
    # daemon's registry record behind after the process has exited.
    for record in _list_daemon_records():
        if not _pid_alive(record.pid):
            _delete_daemon_record(record)
    click.echo("Disabled the Omnigent host user service.")
    click.echo(f"Removed service definition: {_display_path(service.path)}")


@host.command("status")
@click.option("--server", default=None, help="Inspect only this server target.")
@click.option("--all", "all_targets", is_flag=True, help="Inspect all known daemon targets.")
@click.option("--sessions", is_flag=True, help="Include session table.")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.pass_context
def host_status(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    sessions: bool,
    json_output: bool,
) -> None:
    """
    Inspect host daemon and session status.

    :param ctx: Click context carrying group-level options.
    :param server: Optional server target to inspect, e.g.
        ``"https://engine.example.com"``.
    :param all_targets: Whether to inspect every known daemon target.
    :param sessions: Whether to include the session table.
    :param json_output: Whether to emit machine-readable JSON.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=True)

    # Build payloads in parallel so HTTP calls to independent servers overlap.
    # Dead-process records skip the network entirely (see _add_daemon_host_status),
    # but live ones each take a full auth-resolution + round-trip — parallelising
    # them saves wall time proportional to the number of live daemons.
    def _build(record: _HostDaemonRecord) -> _HostPayload:
        return _daemon_status_payload(
            record,
            include_sessions=sessions,
            connected_sessions_only=True,
        )

    with concurrent.futures.ThreadPoolExecutor() as pool:
        payloads = list(pool.map(_build, records))
    if json_output:
        click.echo(json.dumps({"daemons": payloads}, indent=2, sort_keys=True))
        return
    _echo_daemon_payloads(payloads)


def _stop_session_on_server(
    *,
    base_url: str,
    session_id: str,
) -> None:
    """
    Stop one Omnigent session via the server lifecycle event API.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://engine.example.com"``.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :raises click.ClickException: If the server rejects the stop event.
    """
    from urllib.parse import quote

    # This is a standalone CLI process with an empty session→host map, so read
    # the session's host from its record first: the stop_session event is a
    # server→runner forward and must reach the replica holding the runner's
    # tunnel. The metadata GET itself is host-agnostic (served from any replica).
    host_id: str | None = None
    info = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/sessions/{quote(session_id, safe='')}",
    )
    if info.status_code == 200 and isinstance(info.body, dict):
        host_value = info.body.get("host_id")
        if isinstance(host_value, str) and host_value:
            host_id = host_value

    result = _host_http_json(
        base_url=base_url,
        method="POST",
        path=f"/v1/sessions/{quote(session_id, safe='')}/events",
        json_body={"type": "stop_session", "data": {}},
        host_id=host_id,
    )
    if result.status_code == 0:
        raise click.ClickException(
            f"Failed to stop session {session_id!r}: {_host_error_text(result.body)}"
        )
    if result.status_code >= 400:
        raise click.ClickException(
            f"Failed to stop session {session_id!r} ({result.status_code}): "
            f"{_host_error_text(result.body)}"
        )


def _stop_daemon_sessions(
    record: _HostDaemonRecord,
) -> int:
    """
    Stop active sessions owned by a daemon before terminating it.

    :param record: Daemon record whose host-bound sessions should stop.
    :returns: Number of sessions successfully stopped.
    :raises click.ClickException: If session listing or any stop fails.
    """
    result = _sessions_for_daemon(record)
    if result.error is not None:
        if result.unreachable:
            # A dead server holds no reachable sessions to stop; failing here
            # would strand the daemon and its record until the user discovers
            # --force. Degrade to a daemon-only stop instead.
            click.echo(
                f"{record.target}: server is unreachable; skipping session stop: {result.error}",
                err=True,
            )
            return 0
        raise click.ClickException(
            f"{record.target}: {result.error} — retry with --force to stop the "
            f"daemon anyway, or --daemon-only to skip the session stop entirely."
        )
    session_ids = [
        session_id
        for session in result.sessions
        if isinstance((session_id := session.get("id")), str)
        and session_id
        and session.get("status") in _HOST_SESSION_ACTIVE_STATUSES
    ]
    if not session_ids:
        return 0

    total = len(session_ids)
    click.echo(f"Stopping {total} active session(s)...")
    failures: list[str] = []
    stopped = 0
    max_workers = min(total, _HOST_SESSION_STOP_MAX_WORKERS)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _stop_session_on_server,
                base_url=result.base_url,
                session_id=session_id,
            ): session_id
            for session_id in session_ids
        }
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            session_id = futures[future]
            try:
                future.result()
            except click.ClickException as exc:
                failures.append(str(exc))
                click.echo(f"Failed session {session_id} ({completed}/{total}): {exc}", err=True)
            else:
                stopped += 1
                click.echo(f"Stopped session {session_id} ({completed}/{total}).")

    if failures:
        summary = f"Failed to stop {len(failures)} of {total} active session(s)"
        raise click.ClickException(
            f"{summary}; daemon left running. "
            "Retry, or use --force to stop the daemon immediately."
        )
    return stopped


def _signal_daemon_pid(record: _HostDaemonRecord, sig: int) -> bool:
    """
    Signal a daemon's recorded PID, tolerating a stale foreign entry.

    A record can outlive its process, and the PID may since belong to another
    user's process; ``os.kill`` then raises ``PermissionError``, which means
    the record is stale rather than a daemon to kill.

    :param record: Daemon record whose PID should be signalled.
    :param sig: Signal number to send, e.g. ``signal.SIGTERM``.
    :returns: ``True`` if the record is stale and the caller should drop it
        and stop (either the PID is not ours, or it already exited); ``False``
        if the signal was delivered and termination should proceed as usual.
    """
    try:
        os.kill(record.pid, sig)
    except ProcessLookupError:
        # The process exited between the liveness check and the signal —
        # nothing left to kill, so the record is stale.
        return True
    except PermissionError:
        # Not our daemon: the record points at another user's process (PID
        # reuse, or a daemon started under a different account). Drop the
        # stale record and warn rather than crashing the CLI on the EPERM.
        click.echo(
            f"Skipping stale daemon record for {record.target!r}: pid "
            f"{record.pid} is owned by another user and is not this daemon.",
            err=True,
        )
        return True
    return False


def _terminate_daemon(record: _HostDaemonRecord, *, force: bool) -> None:
    """
    Terminate one local daemon process.

    :param record: Daemon record whose process should terminate.
    :param force: Send SIGKILL after the SIGTERM grace period.
    :raises click.ClickException: If the process stays alive.
    """
    if not _pid_is_recorded_daemon(record):
        # Dead, or alive with a recycled pid (often another user's system
        # daemon) — never signal it; the record is stale, so drop it.
        if _pid_alive(record.pid):
            click.echo(
                f"Skipping stale daemon record for {record.target!r}: "
                f"{_describe_pid(record.pid)} is not this daemon (pid recycled).",
                err=True,
            )
        _delete_daemon_record(record)
        return
    if _signal_daemon_pid(record, signal.SIGTERM):
        _delete_daemon_record(record)
        return
    deadline = time.monotonic() + _HOST_DAEMON_STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(record.pid):
            _delete_daemon_record(record)
            return
        time.sleep(0.1)
    if force:
        if _signal_daemon_pid(record, getattr(signal, "SIGKILL", signal.SIGTERM)):
            _delete_daemon_record(record)
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not _pid_alive(record.pid):
                _delete_daemon_record(record)
                return
            time.sleep(0.1)
    raise click.ClickException(
        f"Daemon {record.pid} for {record.target!r} did not exit; retry with --force."
    )


@host.command("stop")
@click.option("--server", default=None, help="Stop only this server target.")
@click.option("--all", "all_targets", is_flag=True, help="Stop all known daemon targets.")
@click.option(
    "--daemon-only",
    is_flag=True,
    help="Terminate daemon processes without first stopping sessions.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip session draining and use SIGKILL if needed.",
)
@click.pass_context
def host_stop(
    ctx: click.Context,
    server: str | None,
    all_targets: bool,
    daemon_only: bool,
    force: bool,
) -> None:
    """
    Stop host daemon sessions, then stop daemon processes.

    :param ctx: Click context carrying group-level options.
    :param server: Optional server target to stop, e.g.
        ``"https://engine.example.com"``.
    :param all_targets: Whether to stop every known daemon target.
    :param daemon_only: Skip server-side session stop calls when ``True``.
    :param force: Skip session draining and use SIGKILL if needed.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    records = _selected_daemon_records(server=server, all_targets=all_targets, default_all=False)
    if not records:
        click.echo("No matching host daemon found.")
        return
    for record in records:
        stopped = 0
        if not daemon_only and not force:
            stopped = _stop_daemon_sessions(record)
        _terminate_daemon(record, force=force)
        click.echo(f"Stopped {record.target} daemon pid={record.pid}; sessions_stopped={stopped}.")


@host.command("stop-session")
@click.argument("session_ids", nargs=-1, required=True)
@click.option("--server", default=None, help="Server that owns the sessions.")
@click.option("--force", is_flag=True, help="Continue after individual stop failures.")
@click.pass_context
def host_stop_session(
    ctx: click.Context,
    session_ids: Sequence[str],
    server: str | None,
    force: bool,
) -> None:
    """
    Stop specific sessions without stopping a daemon.

    :param ctx: Click context carrying group-level options.
    :param session_ids: Session ids to stop, e.g.
        ``["conv_abc123", "conv_def456"]``.
    :param server: Omnigent server URL that owns the sessions, e.g.
        ``"https://engine.example.com"``. ``None`` falls back to config.
    :param force: Continue after individual stop failures.
    """
    if server is None:
        server = _host_group_option(ctx, "server")
    resolved_server = _resolve_host_server(server)
    if resolved_server is None:
        raise click.UsageError("Pass the server URL with --server.")
    for session_id in session_ids:
        try:
            _stop_session_on_server(
                base_url=resolved_server,
                session_id=session_id,
            )
        except click.ClickException:
            if not force:
                raise
            click.echo(f"Failed to stop session {session_id!r}.", err=True)
            continue
        click.echo(f"Stopped session {session_id}.")


@host.command("reset-id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def host_reset_id(yes: bool) -> None:
    """
    Mint a fresh host id for this machine.

    The recovery path for ``HTTP 409: this machine is already registered
    to a different account``: the server keys host registrations by the
    persisted host id, so once another identity owns it, this machine
    cannot re-register under yours. Resetting the id lets the next
    ``omnigent host`` register as a brand-new host under the identity you
    are signed in as. The host name and other config are preserved.

    :param yes: When ``True``, skip the confirmation prompt.
    """
    from omnigent.host.identity import (
        HOST_ID_ENV_VAR,
        HOST_NAME_ENV_VAR,
        host_identity_env_override_active,
        reset_host_id,
    )

    # With OMNIGENT_HOST_ID / OMNIGENT_HOST_NAME set, the host takes its
    # identity from the environment, not config.yaml (both set → env identity
    # used; only one set → startup errors). Either way resetting the id in the
    # file will not change the machine's identity, so refuse and point at the
    # env vars rather than let a config write mislead the user.
    if host_identity_env_override_active():
        raise click.ClickException(
            f"This machine's host identity is controlled by the {HOST_ID_ENV_VAR} / "
            f"{HOST_NAME_ENV_VAR} environment variable(s), so resetting the id in the "
            "config file will not change it. This is normally a server-managed "
            f"sandbox host, whose identity the server owns. Unset {HOST_ID_ENV_VAR} "
            f"and {HOST_NAME_ENV_VAR} first if you need to reset the persisted id."
        )

    running = [record for record in _list_daemon_records() if _pid_alive(record.pid)]
    if running:
        raise click.ClickException(
            "A host daemon is running; stop it first with "
            "`omnigent host stop --all`, then re-run "
            "`omnigent host reset-id`."
        )
    if not yes:
        click.confirm(
            "Mint a fresh host id? Servers will see this machine as a new "
            "host; the registration owned by the previous id is left behind "
            "for an administrator to clean up",
            abort=True,
        )
    old_host_id, new_host_id = reset_host_id()
    if old_host_id is None:
        click.echo(f"No previous host id was persisted; created {new_host_id}.")
    else:
        click.echo(f"Host id reset: {old_host_id} -> {new_host_id}.")
    click.echo("Run `omnigent host` to register this machine under the new id.")


def _resolve_server_url(server: str) -> str:
    """
    Normalize a user-supplied ``--server`` value to the server's API base.

    Strips surrounding whitespace and a trailing slash, and defaults a
    schemeless URL to ``https`` (``http`` for loopback hosts).

    :param server: A non-empty ``--server`` value, e.g. ``"engine.example.com"``.
    :returns: The normalized base URL, e.g. ``"https://engine.example.com"``.
    """
    from urllib.parse import urlsplit

    server = server.strip().rstrip("/")
    if "://" in server:
        return server
    hostname = urlsplit(f"https://{server}").hostname or ""
    scheme = "http" if hostname in {"localhost", "127.0.0.1", "::1"} else "https"
    return f"{scheme}://{server}"


if __name__ == "__main__":
    main()
