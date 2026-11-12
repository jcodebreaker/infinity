"""Tests for the "docker" host provider: a person's host container on plain Docker.

Fully offline: every Docker Engine call goes through ``httpx.MockTransport`` against a fake.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import click
import httpx
import pytest

from omnigent.hosts.docker import (
    DEFAULT_TENANT,
    DockerHostLauncher,
    _demux,
    _parse_sandbox_id,
    container_name,
    home_volume_name,
)
from omnigent.hosts.launcher import RemoteCommandResult, supervise_host_command

KEY = "bot_abc123"
TENANT = "space_xyz789"
IMAGE = "superchat/host:test"
IMAGE_ID = "sha256:image-1"
SANDBOX_ID = f"{KEY}:{TENANT}:c0ffee"


def _frame(kind: int, data: bytes) -> bytes:
    return bytes([kind, 0, 0, 0]) + struct.pack(">I", len(data)) + data


class FakeDocker:
    """A Docker Engine API with one image and the containers a test creates."""

    def __init__(self, *, image_present: bool = True, exit_code: int = 0) -> None:
        self.image_id: str | None = IMAGE_ID if image_present else None
        self.containers: dict[str, dict[str, object]] = {}
        self.requests: list[httpx.Request] = []
        self.exec_output = _frame(1, b"out\n") + _frame(2, b"err\n")
        self.exit_code = exit_code
        self.execs: list[dict[str, object]] = []
        self._next = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle), base_url="http://docker")

    def _by_ref(self, ref: str) -> dict[str, object] | None:
        for c in self.containers.values():
            if ref in (c["Id"], c["Name"]):
                return c
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path.startswith("/images/") and path.endswith("/json"):
            if self.image_id is None:
                return httpx.Response(404, json={"message": "No such image"})
            return httpx.Response(200, json={"Id": self.image_id})
        if path == "/containers/create":
            name = request.url.params["name"]
            if self._by_ref(name):
                return httpx.Response(409, json={"message": "Conflict"})
            self._next += 1
            cid = f"c{self._next}"
            self.containers[cid] = {
                "Id": cid,
                "Name": name,
                "Image": self.image_id,
                "State": {"Running": False},
                "Body": json.loads(request.content),
            }
            return httpx.Response(201, json={"Id": cid})
        if method == "DELETE" and path.startswith("/containers/"):
            self.containers.pop(path.split("/")[2], None)
            return httpx.Response(204)
        if path.startswith("/containers/"):
            parts = path.split("/")
            c = self._by_ref(parts[2])
            if c is None:
                return httpx.Response(404, json={"message": "No such container"})
            action = parts[3]
            if action == "json":
                return httpx.Response(200, json=c)
            if action == "start":
                c["State"] = {"Running": True}
                return httpx.Response(204)
            if action == "exec":
                self.execs.append(json.loads(request.content))
                return httpx.Response(201, json={"Id": f"e{len(self.execs)}"})
        if path.startswith("/exec/") and path.endswith("/start"):
            return httpx.Response(200, content=self.exec_output)
        if path.startswith("/exec/") and path.endswith("/json"):
            return httpx.Response(200, json={"ExitCode": self.exit_code})
        return httpx.Response(500, json={"message": f"unhandled {method} {path}"})


def make_launcher(docker: FakeDocker) -> DockerHostLauncher:
    launcher = DockerHostLauncher(image=IMAGE, client=docker.client())
    launcher.prepare_for_launch(labels={"omnigent.host.key": KEY, "omnigent.tenant": TENANT})
    return launcher


def test_parse_sandbox_id_round_trips() -> None:
    assert _parse_sandbox_id(SANDBOX_ID) == (KEY, TENANT, "c0ffee")


@pytest.mark.parametrize("malformed", ["", "a", "a:b", "a::c", ":b:c"])
def test_parse_sandbox_id_rejects_malformed_ids(malformed: str) -> None:
    with pytest.raises(click.ClickException):
        _parse_sandbox_id(malformed)


def test_identity_comes_from_the_key_label_then_the_owner_label() -> None:
    launcher = DockerHostLauncher(image=IMAGE, client=FakeDocker().client())
    launcher.prepare_for_launch(
        labels={"omnigent.host.owner": "owner-1", "omnigent.tenant": "tenant-1"}
    )
    assert (launcher._key, launcher._tenant) == ("owner-1", "tenant-1")
    launcher.prepare_for_launch(
        labels={
            "omnigent.host.key": "team-1",
            "omnigent.host.owner": "owner-1",
            "omnigent.tenant": "tenant-1",
        }
    )
    assert (launcher._key, launcher._tenant) == ("team-1", "tenant-1")


def test_old_superchat_labels_are_not_read() -> None:
    launcher = DockerHostLauncher(image=IMAGE, client=FakeDocker().client())
    launcher.prepare_for_launch(labels={"superchat.bot": KEY, "superchat.space": TENANT})
    assert (launcher._key, launcher._tenant) == (None, None)


def test_a_session_without_a_tenant_uses_the_default_tenant() -> None:
    launcher = DockerHostLauncher(image=IMAGE, client=FakeDocker().client())
    launcher.prepare_for_launch(labels={"omnigent.host.owner": KEY})
    launcher.prepare()
    assert (launcher._key, launcher._tenant) == (KEY, DEFAULT_TENANT)


def test_relaunch_recovers_identity_from_the_previous_sandbox_id() -> None:
    launcher = DockerHostLauncher(image=IMAGE, client=FakeDocker().client())
    launcher.prepare_for_launch(labels=None, previous_sandbox_id=SANDBOX_ID)
    launcher.prepare()
    assert (launcher._key, launcher._tenant) == (KEY, TENANT)


def test_prepare_fails_without_identity() -> None:
    launcher = DockerHostLauncher(image=IMAGE, client=FakeDocker().client())
    with pytest.raises(click.ClickException, match="identity"):
        launcher.prepare()


def test_prepare_fails_without_a_docker_socket(tmp_path: Path) -> None:
    launcher = DockerHostLauncher(image=IMAGE, socket_path=str(tmp_path / "none.sock"))
    launcher.prepare_for_launch(labels={"omnigent.host.key": KEY, "omnigent.tenant": TENANT})
    with pytest.raises(click.ClickException, match="Docker socket"):
        launcher.prepare()


def test_provision_creates_and_starts_a_hardened_container_with_a_home_volume() -> None:
    docker = FakeDocker()
    sandbox_id = make_launcher(docker).provision("ignored")

    assert sandbox_id == f"{KEY}:{TENANT}:c1"
    container = docker.containers["c1"]
    assert container["Name"] == container_name(KEY)
    assert container["State"] == {"Running": True}
    body = container["Body"]
    assert isinstance(body, dict)
    assert body["Image"] == IMAGE
    assert body["User"] == "1000:1000"
    host = body["HostConfig"]
    assert host["Mounts"] == [
        {"Type": "volume", "Source": home_volume_name(KEY), "Target": "/home/superchat"}
    ]
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Memory"] == 2 * 1024**3
    assert host["NanoCpus"] == 2_000_000_000
    assert host["PidsLimit"] == 2048
    # The Docker socket never reaches the runner.
    assert "docker.sock" not in json.dumps(body)


def test_provision_reuses_the_container_and_starts_it_when_stopped() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    first = launcher.provision("a")
    docker.containers["c1"]["State"] = {"Running": False}
    assert launcher.provision("b") == first
    assert docker.containers["c1"]["State"] == {"Running": True}
    assert len(docker.containers) == 1


def test_provision_recreates_the_container_when_the_runner_image_changed() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    launcher.provision("a")
    docker.image_id = "sha256:image-2"
    assert launcher.provision("b") == f"{KEY}:{TENANT}:c2"
    assert list(docker.containers) == ["c2"]


def test_provision_explains_a_missing_runner_image() -> None:
    with pytest.raises(click.ClickException, match="is not on this Docker host"):
        make_launcher(FakeDocker(image_present=False)).provision("a")


def test_limits_and_names_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_MEMORY", "512m")
    monkeypatch.setenv("OMNIGENT_HOST_CPUS", "1.5")
    monkeypatch.setenv("OMNIGENT_HOST_PIDS_LIMIT", "100")
    monkeypatch.setenv("OMNIGENT_HOST_NAME_PREFIX", "superchat-train")
    docker = FakeDocker()
    make_launcher(docker).provision("a")
    container = docker.containers["c1"]
    # A second stack's engine names its hosts apart from the default stack's.
    assert container["Name"] == f"superchat-train-host-{KEY}" != container_name(KEY)
    body = container["Body"]
    assert isinstance(body, dict)
    host = body["HostConfig"]
    assert host["Mounts"][0]["Source"] == f"superchat-train-home-{KEY}" != home_volume_name(KEY)
    assert (host["Memory"], host["NanoCpus"], host["PidsLimit"]) == (
        512 * 1024**2,
        1_500_000_000,
        100,
    )


def test_run_execs_the_command_and_splits_the_output() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    sandbox_id = launcher.provision("a")
    result = launcher.run(sandbox_id, "echo hi", env={"TOKEN": "secret"})
    assert (result.returncode, result.stdout, result.stderr) == (0, "out\n", "err\n")
    exec_body = docker.execs[-1]
    assert exec_body["Cmd"] == ["sh", "-c", "echo hi"]
    # Env travels as exec environment, never inside the command.
    assert exec_body["Env"] == ["TOKEN=secret"]
    assert "secret" not in json.dumps(exec_body["Cmd"])


def test_run_raises_on_nonzero_exit_only_when_checking() -> None:
    docker = FakeDocker(exit_code=3)
    launcher = make_launcher(docker)
    sandbox_id = launcher.provision("a")
    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run(sandbox_id, "false")
    assert launcher.run(sandbox_id, "false", check=False).returncode == 3


def test_run_follows_a_recreated_container() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    stale = launcher.provision("a")
    docker.containers.clear()
    launcher.run(stale, "true")
    assert [c["Name"] for c in docker.containers.values()] == [container_name(KEY)]


def test_run_background_wraps_the_command_and_returns_immediately() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    sandbox_id = launcher.provision("a")
    launcher.run_background(sandbox_id, "omnigent host --server https://example.test")
    cmd = docker.execs[-1]["Cmd"]
    assert isinstance(cmd, list)
    assert "setsid nohup sh -c" in cmd[2]
    assert "omnigent host --server" in cmd[2]


def test_is_running_reports_the_container_state() -> None:
    docker = FakeDocker()
    launcher = make_launcher(docker)
    sandbox_id = launcher.provision("a")
    assert launcher.is_running(sandbox_id) is True
    docker.containers["c1"]["State"] = {"Running": False}
    assert launcher.is_running(sandbox_id) is False
    assert launcher.is_running(f"{KEY}:{TENANT}:gone") is False
    assert launcher.is_running("malformed") is None


def test_terminate_is_not_supported_since_the_host_outlives_the_session() -> None:
    docker = FakeDocker()
    with pytest.raises(click.ClickException):
        make_launcher(docker).terminate(SANDBOX_ID)
    assert docker.requests == []


def test_capabilities_are_headless_managed_launch_only() -> None:
    caps = make_launcher(FakeDocker()).capabilities
    assert caps.managed_launch is True
    assert caps.screen is False


def test_demux_splits_stdout_and_stderr_frames() -> None:
    stream = _frame(1, b"a") + _frame(2, b"b") + _frame(1, b"c")
    assert _demux(stream) == ("ac", "b")


class _LocalShellLauncher(DockerHostLauncher):
    """Runs the exec'd command in a real local ``sh`` with a throwaway ``$HOME``."""

    home: str = ""

    def run(self, sandbox_id, command, *, check=True, env=None):  # type: ignore[no-untyped-def]
        import subprocess

        done = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            text=True,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
        )
        return RemoteCommandResult(done.returncode, done.stdout, done.stderr)


def _local_launcher(home: str) -> _LocalShellLauncher:
    launcher = _LocalShellLauncher(image=IMAGE, client=FakeDocker().client())
    launcher.home = home
    return launcher


def test_supervisor_check_rejects_a_stale_pidfile_pointing_at_another_process(
    tmp_path: Path,
) -> None:
    """The home volume keeps the pidfile across a container recreate; a pid then reused by an
    unrelated live process must not count as the supervise loop."""
    import subprocess

    (tmp_path / ".omnigent" / "hosts").mkdir(parents=True)
    other = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / ".omnigent" / "hosts" / "abc123.pid").write_text(f"{other.pid}\n")
        assert (
            _local_launcher(str(tmp_path)).is_host_supervisor_running(SANDBOX_ID, "abc123")
            is False
        )
    finally:
        other.kill()
        other.wait()


def test_supervisor_check_accepts_the_real_supervise_loop(tmp_path: Path) -> None:
    import subprocess
    import time

    if not os.path.isdir("/proc/self"):
        pytest.skip("needs /proc")
    script = supervise_host_command("sleep 30", host_id="abc123")
    loop = subprocess.Popen(
        ["sh", "-c", script], env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    )
    try:
        for _ in range(50):
            if (tmp_path / ".omnigent" / "hosts" / "abc123.pid").exists():
                break
            time.sleep(0.05)
        assert (
            _local_launcher(str(tmp_path)).is_host_supervisor_running(SANDBOX_ID, "abc123") is True
        )
    finally:
        loop.kill()
        loop.wait()


def test_supervisor_check_is_false_without_a_pidfile_or_with_junk(tmp_path: Path) -> None:
    launcher = _local_launcher(str(tmp_path))
    assert launcher.is_host_supervisor_running(SANDBOX_ID, "abc123") is False
    (tmp_path / ".omnigent" / "hosts").mkdir(parents=True)
    (tmp_path / ".omnigent" / "hosts" / "abc123.pid").write_text("12; rm -rf /\n")
    assert launcher.is_host_supervisor_running(SANDBOX_ID, "abc123") is False
    assert launcher.is_host_supervisor_running(SANDBOX_ID, "a;b") is False
