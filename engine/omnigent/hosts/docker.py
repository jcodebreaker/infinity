"""
The ``docker`` host provider: one Omnigent host container per person, on plain Docker.

A person's host is a long-lived container started from the host image (Omnigent's host image
plus the deployment's additions), with the person's home on a named Docker volume. The engine
talks to the Docker Engine API over its socket directly; nothing sits in between.

Implements :class:`~omnigent.hosts.launcher.HostLauncher` (via
:class:`~omnigent.hosts.launcher.ExecModelHostLauncher`, whose default
:meth:`~omnigent.hosts.launcher.ExecModelHostLauncher.start_host` composes ``run`` /
``run_background`` into the full host launch) — ``managed_launch`` only.

Identity: the host is keyed by the session's person, not by the launch-chosen ``name``. The
gateway stamps every session with ``omnigent.host.key`` / ``omnigent.host.owner`` /
``omnigent.tenant`` labels (a session with no tenant uses :data:`DEFAULT_TENANT`);
:meth:`prepare_for_launch` reads them, or on a relaunch recovers them from the previous sandbox id
``"<key>:<tenant>:<container_id>"``.

Idempotent: :meth:`provision` reuses the person's container (starting it if stopped) and only
recreates it when the host image changed; the home volume survives a recreate. ``start_host``
wraps the host process in the shared restart-on-crash loop, so a crashed host self-heals inside
the same container.

Deliberately does NOT implement ``terminate`` / ``keep_alive``: the host outlives any one
session, and Omnigent's best-effort sandbox cleanup must never destroy it. A host is removed
only on purpose, with :meth:`DockerHostLauncher.remove_host` (an eval person's, ADR 0042).
"""

from __future__ import annotations

import logging
import os
import re
import struct
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

import click
import httpx

from omnigent.hosts.launcher import (
    ExecModelHostLauncher,
    RemoteCommandResult,
    supervised_host_pidfile_path,
)
from omnigent.hosts.types import HostCapabilities, HostError

if TYPE_CHECKING:
    from collections.abc import Mapping

_logger = logging.getLogger(__name__)

IMAGE_ENV_VAR: str = "OMNIGENT_HOST_IMAGE"
"""Image each host starts from. Defaults to ``superchat/host:local``."""

DOCKER_SOCKET_ENV_VAR: str = "OMNIGENT_HOST_DOCKER_SOCKET"
"""Path of the Docker Engine socket. Defaults to ``/var/run/docker.sock``."""

NETWORK_ENV_VAR: str = "OMNIGENT_HOST_NETWORK"
"""Docker network a host joins. Defaults to ``bridge``."""

MEMORY_ENV_VAR: str = "OMNIGENT_HOST_MEMORY"
"""Memory limit, e.g. ``2g``. Defaults to ``2g``."""

CPUS_ENV_VAR: str = "OMNIGENT_HOST_CPUS"
"""CPU limit, e.g. ``2`` or ``1.5``. Defaults to ``2``."""

PIDS_LIMIT_ENV_VAR: str = "OMNIGENT_HOST_PIDS_LIMIT"
"""Process limit. Defaults to ``2048``."""

NAME_PREFIX_ENV_VAR: str = "OMNIGENT_HOST_NAME_PREFIX"
"""Prefix of each host's container and home volume names, so two engines on one Docker daemon
never share a host. Defaults to ``superchat``, giving ``superchat-host-<key>`` and
``superchat-home-<key>``."""

LABEL_HOST_KEY: str = "omnigent.host.key"
"""Session label naming the host to run in (shared by every session with the same key)."""

LABEL_HOST_OWNER: str = "omnigent.host.owner"
"""Session label naming the host's owner; used as the key when no key label is set."""

LABEL_TENANT: str = "omnigent.tenant"
"""Session label naming the tenant (space) the host belongs to."""

#: The tenant of a session with none: an engine without tenants (one organization, or the
#: single local person) still places each person's host.
DEFAULT_TENANT: str = "default"

_DEFAULT_IMAGE: str = "superchat/host:local"
_DEFAULT_SOCKET: str = "/var/run/docker.sock"
_DEFAULT_NAME_PREFIX: str = "superchat"

#: The host image's user and home; the home volume is mounted there.
_HOST_USER: str = "1000:1000"
_HOST_HOME: str = "/home/superchat"
#: The Muse's shell: the system Python (with the data stack) first, the person's own installs
#: under ~/.local before that. Omnigent's /opt/venv stays off it.
_HOST_ENV: tuple[str, ...] = (
    f"HOME={_HOST_HOME}",
    f"PATH={_HOST_HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    f"NPM_CONFIG_PREFIX={_HOST_HOME}/.local",
    "PIP_USER=1",
)
_SHM_SIZE_BYTES: int = 256 * 1024 * 1024
_MANAGED_LABEL: str = "omnigent.host"

_REQUEST_TIMEOUT_S: float = 30.0
_PROVISION_TIMEOUT_S: float = 120.0
_EXEC_TIMEOUT_S: float = 300.0

# Docker error bodies are echoed into ClickException messages; capped so a misbehaving daemon
# can't flood the log.
_MAX_ERROR_BODY_CHARS: int = 500


class DockerHostError(HostError):
    """A call to the Docker Engine for a host failed."""


def tenant_from_labels(labels: Mapping[str, str]) -> str | None:
    """The tenant a session's labels place its host in."""
    return labels.get(LABEL_TENANT) or None


def _parse_sandbox_id(sandbox_id: str) -> tuple[str, str, str]:
    """
    Split a launcher-issued sandbox id into ``(key, tenant, container_id)``.

    :param sandbox_id: A value :meth:`DockerHostLauncher.provision` returned.
    :returns: The three segments, in order.
    :raises click.ClickException: When *sandbox_id* is not three non-empty colon-joined segments.
    """
    parts = sandbox_id.split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise click.ClickException(
            f"malformed 'docker' sandbox id {sandbox_id!r} — expected "
            "'<key>:<tenant>:<container_id>'"
        )
    key, tenant, container_id = parts
    return key, tenant, container_id


def _safe_name(key: str) -> str:
    """A Docker-safe name fragment for *key* (letters, digits, ``_.-``; at most 40 chars)."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "", key)[:40]
    if not safe:
        raise click.ClickException(f"host key {key!r} has no usable characters")
    return safe


def container_name(key: str, prefix: str = _DEFAULT_NAME_PREFIX) -> str:
    """Name of the host container for *key*."""
    return f"{prefix}-host-{_safe_name(key)}"


def home_volume_name(key: str, prefix: str = _DEFAULT_NAME_PREFIX) -> str:
    """Name of the Docker volume holding the host's home for *key*."""
    return f"{prefix}-home-{_safe_name(key)}"


def _nano_cpus(value: str) -> int:
    return int(float(value) * 1_000_000_000)


def _memory_bytes(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgKMG]?)[bB]?\s*", value)
    if not match:
        raise click.ClickException(f"{MEMORY_ENV_VAR}={value!r} is not a size like '2g'")
    number, unit = float(match.group(1)), match.group(2).lower()
    return int(number * {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit])


def _demux(stream: bytes) -> tuple[str, str]:
    """Split Docker's multiplexed exec output (8-byte frame headers) into stdout and stderr."""
    out: list[bytes] = []
    err: list[bytes] = []
    pos = 0
    while pos + 8 <= len(stream):
        kind = stream[pos]
        (size,) = struct.unpack(">I", stream[pos + 4 : pos + 8])
        chunk = stream[pos + 8 : pos + 8 + size]
        (err if kind == 2 else out).append(chunk)
        pos += 8 + size
    return (
        b"".join(out).decode("utf-8", "replace"),
        b"".join(err).decode("utf-8", "replace"),
    )


class DockerHostLauncher(ExecModelHostLauncher):
    """
    Host launcher that runs Omnigent hosts in a person's host container.

    Registered as the ``"docker"`` provider; select it with ``host.provider: docker``.
    Configured only through the environment variables above.
    """

    provider: ClassVar[str] = "docker"
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_managed_launch: ClassVar[bool] = True

    def __init__(
        self,
        *,
        image: str | None = None,
        socket_path: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """
        :param image: Override for :data:`IMAGE_ENV_VAR`.
        :param socket_path: Override for :data:`DOCKER_SOCKET_ENV_VAR`.
        :param client: Pre-built HTTP client, e.g. one wired to a fake Docker API via
            ``httpx.MockTransport`` in tests. ``None`` builds one on the socket lazily.
        """
        self._image = image or os.environ.get(IMAGE_ENV_VAR) or _DEFAULT_IMAGE
        self._socket_path = socket_path or os.environ.get(DOCKER_SOCKET_ENV_VAR) or _DEFAULT_SOCKET
        self._network = os.environ.get(NETWORK_ENV_VAR) or "bridge"
        self._memory = os.environ.get(MEMORY_ENV_VAR) or "2g"
        self._cpus = os.environ.get(CPUS_ENV_VAR) or "2"
        self._pids_limit = int(os.environ.get(PIDS_LIMIT_ENV_VAR) or "2048")
        self._name_prefix = os.environ.get(NAME_PREFIX_ENV_VAR) or _DEFAULT_NAME_PREFIX
        self._injected_client = client
        # Set by prepare_for_launch; only provision() relies on it (every other method
        # re-derives identity from the sandbox id it is handed).
        self._key: str | None = None
        self._tenant: str | None = None

    @property
    def capabilities(self) -> HostCapabilities:
        return HostCapabilities(managed_launch=True, screen=False)

    def prepare_for_launch(
        self,
        *,
        agent_name: str | None = None,
        labels: Mapping[str, str] | None = None,
        previous_sandbox_id: str | None = None,
    ) -> None:
        """
        Resolve this launch's person from session labels, or a relaunch's prior sandbox id.

        :param agent_name: Unused.
        :param labels: The launching session's labels, or ``None``.
        :param previous_sandbox_id: The host's previous :meth:`provision` value on a relaunch.
        """
        del agent_name
        found = labels or {}
        key = found.get(LABEL_HOST_KEY) or found.get(LABEL_HOST_OWNER)
        tenant = tenant_from_labels(found) or DEFAULT_TENANT
        if key:
            self._key, self._tenant = key, tenant
            return
        if previous_sandbox_id:
            self._key, self._tenant, _container_id = _parse_sandbox_id(previous_sandbox_id)

    def prepare(self) -> None:
        if not os.path.exists(self._socket_path) and self._injected_client is None:
            raise click.ClickException(
                f"no Docker socket at {self._socket_path} — the 'docker' provider starts each "
                f"person's host with Docker (set {DOCKER_SOCKET_ENV_VAR})"
            )
        if not self._key or not self._tenant:
            raise click.ClickException(
                "the 'docker' host provider could not resolve this host's identity — the "
                f"launching session must carry a '{LABEL_HOST_KEY}' "
                f"(or '{LABEL_HOST_OWNER}') label"
            )

    def _client(self) -> httpx.Client:
        if self._injected_client is None:
            self._injected_client = httpx.Client(
                transport=httpx.HTTPTransport(uds=self._socket_path), base_url="http://docker"
            )
        return self._injected_client

    def _docker(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("timeout", _REQUEST_TIMEOUT_S)
        try:
            return self._client().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise click.ClickException(f"Docker request failed ({method} {path}): {exc}") from exc

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            return response.text[:_MAX_ERROR_BODY_CHARS]
        except Exception:
            # Best-effort error text for an exception message; never worth failing over.
            return ""

    def _fail(self, what: str, response: httpx.Response) -> click.ClickException:
        return click.ClickException(f"{what} ({response.status_code}): {self._detail(response)}")

    def _image_id(self) -> str:
        response = self._docker("GET", f"/images/{quote(self._image, safe='')}/json")
        if response.status_code == 404:
            raise click.ClickException(
                f"host image {self._image} is not on this Docker host — build it first "
                f"(see the README's Run with Docker) or set {IMAGE_ENV_VAR}"
            )
        if response.status_code >= 400:
            raise self._fail("could not inspect the host image", response)
        return str(response.json().get("Id", ""))

    def _create_body(self, key: str, tenant: str) -> dict[str, Any]:
        return {
            "Image": self._image,
            "User": _HOST_USER,
            "Env": list(_HOST_ENV),
            "WorkingDir": _HOST_HOME,
            "Labels": {
                _MANAGED_LABEL: "true",
                LABEL_HOST_KEY: key,
                LABEL_TENANT: tenant,
            },
            "HostConfig": {
                "Mounts": [
                    {
                        "Type": "volume",
                        "Source": home_volume_name(key, self._name_prefix),
                        "Target": _HOST_HOME,
                    }
                ],
                "ShmSize": _SHM_SIZE_BYTES,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Memory": _memory_bytes(self._memory),
                "NanoCpus": _nano_cpus(self._cpus),
                "PidsLimit": self._pids_limit,
                "NetworkMode": self._network,
                # The runner dials the engine back at its public URL; native Linux needs this.
                "ExtraHosts": ["host.docker.internal:host-gateway"],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
        }

    def _ensure_container_id(self, key: str, tenant: str) -> str:
        """Return the id of *key*'s running host, creating or recreating it as needed."""
        name = container_name(key, self._name_prefix)
        image_id = self._image_id()
        response = self._docker("GET", f"/containers/{name}/json")
        if response.status_code == 200:
            info = response.json()
            if info.get("Image") == image_id:
                if not (info.get("State") or {}).get("Running"):
                    self._start(str(info["Id"]))
                return str(info["Id"])
            # A new host image: replace the container; the home volume stays.
            removed = self._docker("DELETE", f"/containers/{info['Id']}", params={"force": "true"})
            if removed.status_code >= 400 and removed.status_code != 404:
                raise self._fail("could not replace the host", removed)
        elif response.status_code != 404:
            raise self._fail("could not inspect the host", response)
        created = self._docker(
            "POST",
            "/containers/create",
            params={"name": name},
            json=self._create_body(key, tenant),
            timeout=_PROVISION_TIMEOUT_S,
        )
        if created.status_code == 409:
            # Another launch created it first; use that one.
            again = self._docker("GET", f"/containers/{name}/json")
            if again.status_code >= 400:
                raise self._fail("could not inspect the host", again)
            container_id = str(again.json()["Id"])
        elif created.status_code >= 400:
            raise self._fail("could not create the host", created)
        else:
            container_id = str(created.json()["Id"])
        self._start(container_id)
        return container_id

    def _start(self, container_id: str) -> None:
        response = self._docker("POST", f"/containers/{container_id}/start")
        if response.status_code not in (204, 304):
            raise self._fail("could not start the host", response)

    def provision(self, name: str) -> str:
        """
        Ensure this person's host is running and return its encoded sandbox id.

        :param name: Unused — hosts are keyed by person, not by the launch label.
        """
        del name
        if not self._key or not self._tenant:
            # prepare() already enforces this; guards type narrowing below.
            raise click.ClickException("the 'docker' host provider has no identity")
        container_id = self._ensure_container_id(self._key, self._tenant)
        return f"{self._key}:{self._tenant}:{container_id}"

    def _live_container_id(self, sandbox_id: str) -> str:
        """The sandbox id's container, or the person's current one if it was recreated."""
        key, tenant, container_id = _parse_sandbox_id(sandbox_id)
        response = self._docker("GET", f"/containers/{quote(container_id, safe='')}/json")
        if response.status_code == 200 and (response.json().get("State") or {}).get("Running"):
            return container_id
        return self._ensure_container_id(key, tenant)

    def run(
        self,
        sandbox_id: str,
        command: str,
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> RemoteCommandResult:
        container_id = self._live_container_id(sandbox_id)
        body: dict[str, Any] = {
            "Cmd": ["sh", "-c", command],
            "AttachStdout": True,
            "AttachStderr": True,
            "WorkingDir": _HOST_HOME,
        }
        if env:
            # Passed as exec environment, never in `command`: values stay out of argv and ps.
            body["Env"] = [f"{k}={v}" for k, v in env.items()]
        created = self._docker("POST", f"/containers/{container_id}/exec", json=body)
        if created.status_code >= 400:
            result = RemoteCommandResult(
                returncode=1, stdout="", stderr=f"exec failed: {self._detail(created)}"
            )
        else:
            exec_id = str(created.json()["Id"])
            started = self._docker(
                "POST",
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                timeout=_EXEC_TIMEOUT_S,
            )
            stdout, stderr = _demux(started.content)
            inspected = self._docker("GET", f"/exec/{exec_id}/json")
            code = inspected.json().get("ExitCode") if inspected.status_code == 200 else None
            result = RemoteCommandResult(
                returncode=int(code) if isinstance(code, int) else 1,
                stdout=stdout,
                stderr=stderr,
            )
        if check and result.returncode != 0:
            raise click.ClickException(
                f"command failed in the host (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result

    def remove_host(self, sandbox_id: str, *, home: bool) -> None:
        """Force-remove the person's host container (stopping it), and with *home* its home
        volume. By name, so a recreated container goes too; already gone is success."""
        key, _tenant, _container_id = _parse_sandbox_id(sandbox_id)
        name = container_name(key, self._name_prefix)
        removed = self._docker("DELETE", f"/containers/{name}", params={"force": "true"})
        if removed.status_code >= 400 and removed.status_code != 404:
            raise self._fail("could not remove the host", removed)
        if home:
            volume = home_volume_name(key, self._name_prefix)
            dropped = self._docker("DELETE", f"/volumes/{volume}")
            if dropped.status_code >= 400 and dropped.status_code != 404:
                raise self._fail("could not remove the host's home", dropped)

    def is_running(self, sandbox_id: str) -> bool | None:
        """Whether the sandbox id's container is running; ``None`` when Docker can't say."""
        try:
            _key, _tenant, container_id = _parse_sandbox_id(sandbox_id)
            response = self._docker("GET", f"/containers/{quote(container_id, safe='')}/json")
        except click.ClickException:
            return None
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            return None
        return bool((response.json().get("State") or {}).get("Running"))

    def is_host_supervisor_running(self, sandbox_id: str, host_id: str) -> bool:
        """
        Whether the supervised host loop for *host_id* is alive in the host container.

        Stricter than the base check: the home volume keeps the pidfile across a container
        recreate while pids restart, so a pid counts only when it answers ``kill -0`` AND its
        ``/proc/<pid>/cmdline`` carries this host's pidfile path.

        :param sandbox_id: Target host.
        :param host_id: Host id of a prior launch; server-generated, validated here anyway.
        :returns: ``True`` iff the recorded pid is alive and is this host's supervise loop.
        """
        if not host_id.replace("-", "").replace("_", "").isalnum():
            return False
        pidfile = supervised_host_pidfile_path(host_id)
        marker = f"hosts/{host_id}.pid"
        check = (
            f"pid=$(cat {pidfile} 2>/dev/null); "
            'case "$pid" in ""|*[!0-9]*) exit 1;; esac; '
            'kill -0 "$pid" 2>/dev/null || exit 1; '
            f"tr '\\0' ' ' < \"/proc/$pid/cmdline\" 2>/dev/null | grep -qF -- '{marker}'"
        )
        return self.run(sandbox_id, check, check=False).returncode == 0
