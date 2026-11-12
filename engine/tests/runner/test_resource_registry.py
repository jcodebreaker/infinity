"""Tests for runner-side SessionResourceRegistry (Phase 2)."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.harnesses.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.harnesses.os_env import EditEntry, OpResult, OSEnvironment
from omnigent.harnesses.terminal import TerminalInstance
from omnigent.runner.resource_registry import (
    _TERMINAL_EXIT_OUTPUT_MAX_CHARS,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
    _sanitize_session_id,
    _session_workspace,
    _terminal_exit_diagnostics,
    trim_terminal_output,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import make_test_terminal_instance


@dataclass
class _FakeOSEnvironment(OSEnvironment):
    """Minimal concrete OSEnvironment for registry tests."""

    _closed: bool = False

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        del path, offset, limit
        return {}

    async def write(self, path: str, content: str) -> OpResult:
        del path, content
        return {}

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        del path, old_text, new_text, edits
        return {}

    async def shell(
        self,
        command: str,
        timeout: int | None = None,
    ) -> OpResult:
        del command, timeout
        return {}

    def close(self) -> None:
        self._closed = True


def _agent_spec_with_sandbox_none(cwd: Path) -> SimpleNamespace:
    """
    Return an agent-like object with an explicit sandbox-free OS env.

    :param cwd: Working directory for the OS environment.
    :returns: Object exposing an ``os_env`` attribute.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


def _seed_terminal(
    registry: TerminalRegistry,
    conversation_id: str,
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    os_env: OSEnvironment | None = None,
) -> None:
    """Seed a running terminal in the registry."""
    slot = registry._by_conversation.setdefault(conversation_id, {})
    slot[(name, session_key)] = TerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=tmp_path / f"{name}-{session_key}.sock",
        private_dir=tmp_path / f"{name}-{session_key}",
        os_env=os_env,
        running=True,
    )


def test_list_resources_includes_default_env() -> None:
    """Registry always includes the logical default environment."""
    reg = SessionResourceRegistry()
    page = reg.list_resources("conv_1")

    ids = [r.id for r in page.data]
    assert DEFAULT_ENVIRONMENT_ID in ids
    default = page.data[0]
    assert default.type == "environment"
    assert default.metadata["role"] == "primary"


def test_list_resources_includes_terminals(tmp_path: Path) -> None:
    """Registry includes running terminals from the TerminalRegistry."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    page = reg.list_resources("conv_1")
    ids = [r.id for r in page.data]
    assert "terminal_bash_s1" in ids


def test_list_resources_filters_by_type(tmp_path: Path) -> None:
    """Registry filters by resource_type when specified."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    env_page = reg.list_resources("conv_1", resource_type="environment")
    assert all(r.type == "environment" for r in env_page.data)

    term_page = reg.list_resources("conv_1", resource_type="terminal")
    assert all(r.type == "terminal" for r in term_page.data)


@pytest.mark.asyncio
async def test_terminal_lifecycle_cannot_change_after_observe(tmp_path: Path) -> None:
    """A terminal cannot silently switch between auxiliary and required lifecycle."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("worker", "main", tmp_path)

    await registry.observe_auxiliary_terminal("conv_lifecycle", "worker", "main", instance)

    with pytest.raises(RuntimeError, match="already observed as auxiliary"):
        await registry.observe_required_terminal("conv_lifecycle", "worker", "main", instance)


@pytest.mark.asyncio
async def test_auxiliary_terminal_exit_publishes_resource_exit_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Auxiliary terminal exit is reported with auxiliary lifecycle metadata."""
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("sidecar", "s1", tmp_path)
    instance.command = "worker-cli"
    instance.args = ["--verbose"]
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot("\x1b[31mstartup failed\x1b[0m\nretry login")
    terminal_registry._by_conversation.setdefault("conv_exit", {})[("sidecar", "s1")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_auxiliary_terminal("conv_exit", "sidecar", "s1", instance)
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert [event.lifecycle for event in exits] == [TerminalLifecycle.AUXILIARY]
    assert exits[0].terminal_id == "terminal_sidecar_s1"
    assert exits[0].command == "worker-cli"
    assert exits[0].args_count == 1
    assert exits[0].cwd == str(tmp_path)
    assert exits[0].last_output == "startup failed\nretry login"
    assert terminal_registry.get("conv_exit", "sidecar", "s1") is None
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    )
    assert record.session_id == "conv_exit"
    assert record.attributes["terminal_lifecycle"] == "auxiliary"
    assert record.attributes["terminal_instance_id"] == instance.diagnostic_id
    assert record.attributes["session_status_before_exit"] == "unknown"
    assert record.attributes["superseded"] is False
    # A non-Codex terminal keeps the guarantee: no pane contents, no cwd in
    # the lifecycle-event attributes.
    assert record.attributes["terminal_last_output"] is None
    assert "startup failed" not in str(record.attributes)
    assert str(tmp_path) not in str(record.attributes)


class _FakeStatusPoller:
    """Controllable stand-in for the claude-native status-file poller.

    Lets a test flip :attr:`active` (file resolved and therefore owning the
    session's status, vs. the PTY watcher falling back) and fire status edges
    through the registry's callback, without touching a real
    ``sessions/<pid>.json``.
    """

    def __init__(self, on_status: object) -> None:
        self._on_status = on_status
        self.active = False
        self.blocked_on: str | None = None
        self.ticks = 0
        self.retired = False
        self.resyncs = 0

    def tick(self) -> None:
        self.ticks += 1

    def retire(self) -> None:
        self.retired = True
        self.active = False

    def resync(self) -> None:
        self.resyncs += 1

    def emit(self, status: str, blocked_on: str | None = None) -> None:
        """Simulate the file reporting a new status."""
        self._on_status(status, blocked_on)


def test_trim_terminal_output_keeps_character_tail() -> None:
    # Over the char budget: keep the last N characters, marked as truncated.
    filler = "\n".join(f"line {i} " + "x" * 150 for i in range(30))
    text = filler + "\n--dangerously-skip-permissions cannot be run for security reasons"
    trimmed = trim_terminal_output(text)
    assert trimmed is not None
    marker, body = trimmed.split("\n", 1)
    assert marker.startswith("... omitted ") and marker.endswith(" earlier character(s) ...")
    assert len(body) == _TERMINAL_EXIT_OUTPUT_MAX_CHARS
    assert body.endswith("for security reasons")


def test_trim_terminal_output_keeps_long_line_before_final_line() -> None:
    # A long error line followed by tmux's short "pane is dead" line must not be
    # dropped wholesale, leaving only the pane-dead line.
    usage = "Usage: codex [OPTIONS] " + "z" * (_TERMINAL_EXIT_OUTPUT_MAX_CHARS + 1000)
    trimmed = trim_terminal_output(f"a\nb\n{usage}\npane is dead (status 2)")
    assert trimmed is not None
    assert trimmed.endswith("z\npane is dead (status 2)")
    assert len(trimmed.split("\n", 1)[1]) == _TERMINAL_EXIT_OUTPUT_MAX_CHARS


def test_trim_terminal_output_hard_clips_single_overlong_line() -> None:
    line = "y" * (_TERMINAL_EXIT_OUTPUT_MAX_CHARS + 500)
    trimmed = trim_terminal_output(line)
    assert (
        trimmed
        == f"... omitted 500 earlier character(s) ...\n{'y' * _TERMINAL_EXIT_OUTPUT_MAX_CHARS}"
    )


def test_trim_terminal_output_leaves_short_output_untouched() -> None:
    assert trim_terminal_output("  boom\r\nexit 1\n") == "boom\nexit 1"


def test_terminal_exit_diagnostics_reads_exit_status(tmp_path: Path) -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "claude"
    instance.args = ["--dangerously-skip-permissions"]
    instance._remember_pane_snapshot("boom")
    # Simulate tmux having reported a dead pane with a captured status.
    instance._remember_exit_status("1 42")
    command, args_count, _cwd, last_output, exit_status = _terminal_exit_diagnostics(instance)
    assert command == "claude"
    assert args_count == 1
    assert last_output == "boom"
    assert exit_status == 42


def test_remember_exit_status_ignores_live_pane() -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path=Path("/tmp"))
    # Live pane: pane_dead=0, empty status → nothing recorded.
    instance._remember_exit_status("0 ")
    assert instance.last_exit_status() is None


@pytest.mark.asyncio
async def test_cleanup_session_clears_status_memo(tmp_path: Path) -> None:
    """``cleanup_session`` drops the session's PTY-status memo.

    :param tmp_path: Temporary directory (unused beyond registry construction).
    """
    del tmp_path
    registry = SessionResourceRegistry()
    registry.note_session_turn_started("conv_cleanup")
    assert "conv_cleanup" in registry._last_session_status
    assert registry.session_activity_epoch("conv_cleanup") > 0
    assert registry.session_turn_is_active("conv_cleanup")

    await registry.cleanup_session("conv_cleanup")

    assert "conv_cleanup" not in registry._last_session_status
    assert registry.session_activity_epoch("conv_cleanup") == 0
    assert not registry.session_turn_is_active("conv_cleanup")


def test_get_resource_finds_default() -> None:
    """get_resource finds the default environment."""
    reg = SessionResourceRegistry()
    resource = reg.get_resource("conv_1", DEFAULT_ENVIRONMENT_ID)
    assert resource is not None
    assert resource.type == "environment"


def test_get_resource_returns_none_for_unknown() -> None:
    """get_resource returns None for unknown ids."""
    reg = SessionResourceRegistry()
    assert reg.get_resource("conv_1", "nonexistent") is None


def test_resolve_environment_creates_primary_lazily(
    tmp_path: Path,
) -> None:
    """resolve_environment lazily creates the primary OSEnvironment."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        assert not reg.has_primary_env("conv_1")

        agent_spec = _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace")
        env = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env is not None
        assert reg.has_primary_env("conv_1")

        env2 = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env2 is env
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_default_pins_none_sandbox_when_no_agent_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default env (no agent_spec) must pin ``sandbox.type="none"``.

    Regression test for the resource-endpoint default: the default
    env must work on hosts without a usable sandbox backend. The
    pre-fix code left the default ``OSEnvSpec`` with
    ``sandbox=None``, so it routed through the Linux platform
    default (which would raise when no backend was available). We
    stub ``shutil.which`` to report ``bwrap`` IS present so that, if
    the default env wrongly routed through the platform default, it
    would resolve to an active ``linux_bwrap`` policy — the assertion
    below proves it pins ``none`` instead.

    :param tmp_path: Per-test workspace dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.sandbox.shutil.which",
        lambda name: "/usr/bin/bwrap",
    )
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    reg = SessionResourceRegistry()

    env = reg.resolve_environment("conv_no_spec", DEFAULT_ENVIRONMENT_ID)

    # backend_type="none" + active=False prove sandbox=none was
    # pinned, not picked by the platform-default fallback.
    assert env.sandbox.backend_type == "none"
    assert env.sandbox.active is False


def test_resolve_environment_uses_agent_spec_os_env(
    tmp_path: Path,
) -> None:
    """resolve_environment uses agent_spec.os_env when available."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()

        class _FakeSpec:
            os_env = OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path / "custom-cwd"),
                sandbox=OSEnvSandboxSpec(type="none"),
            )

        (tmp_path / "custom-cwd").mkdir()
        env = reg.resolve_environment(
            "conv_spec",
            DEFAULT_ENVIRONMENT_ID,
            _FakeSpec(),
        )
        assert env is not None
        assert str(env.cwd).endswith("custom-cwd")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_raises_for_unknown_env_id() -> None:
    """resolve_environment raises ValueError for unknown ids."""
    reg = SessionResourceRegistry()
    with pytest.raises(ValueError, match="not found"):
        reg.resolve_environment("conv_1", "env_nonexistent_foo")


def test_resolve_terminal_environment(tmp_path: Path) -> None:
    """resolve_environment resolves terminal environment ids."""
    tr = TerminalRegistry()
    terminal_env = _FakeOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        cwd=tmp_path,
    )
    _seed_terminal(
        tr,
        "conv_1",
        "bash",
        "s1",
        tmp_path,
        os_env=terminal_env,
    )
    reg = SessionResourceRegistry(terminal_registry=tr)

    env = reg.resolve_environment("conv_1", "env_terminal_bash_s1")
    assert env is terminal_env


@pytest.mark.asyncio
async def test_cleanup_session_closes_primary_env(
    tmp_path: Path,
) -> None:
    """cleanup_session closes the primary env and cleans terminals."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_1",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace"),
        )
        assert reg.has_primary_env("conv_1")

        await reg.cleanup_session("conv_1")
        assert not reg.has_primary_env("conv_1")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


# ── Phase 4: cleanup endpoint tests ─────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_endpoint_returns_confirmation(
    tmp_path: Path,
) -> None:
    """DELETE /v1/sessions/{id}/resources returns cleanup confirmation."""
    import httpx

    from omnigent.runner import create_runner_app

    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_cleanup",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_cleanup" / "workspace"),
        )
        from tests.runner.helpers import NullServerClient

        app = create_runner_app(
            resource_registry=reg,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runner",
        ) as client:
            resp = await client.delete(
                "/v1/sessions/conv_cleanup/resources",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "conv_cleanup"
        assert body["cleaned"] is True
        assert not reg.has_primary_env("conv_cleanup")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


@pytest.mark.asyncio
async def test_cleanup_idempotent_for_unknown_session() -> None:
    """DELETE /v1/sessions/{id}/resources is safe for unknown sessions."""
    import httpx

    from omnigent.runner import create_runner_app
    from tests.runner.helpers import NullServerClient

    reg = SessionResourceRegistry()
    app = create_runner_app(
        resource_registry=reg,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runner",
    ) as client:
        resp = await client.delete(
            "/v1/sessions/conv_unknown/resources",
        )
    assert resp.status_code == 200
    assert resp.json()["cleaned"] is True


# ── os_env gate: list_resources ──────────────────────────────────────────────


def test_list_resources_suppresses_default_env_when_spec_has_no_os_env() -> None:
    """list_resources omits the default environment when agent_spec.os_env is None.

    Agents without an os_env block have no primary filesystem environment,
    so the resource listing must not advertise one.
    """

    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    page = reg.list_resources("conv_no_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be absent — the spec has no os_env so there
    # is no primary filesystem environment to expose.  If this assertion
    # fails, the gate was not applied and the UI would show a "Working
    # folder" panel that can never return any files.
    assert DEFAULT_ENVIRONMENT_ID not in ids, (
        f"Default environment should be suppressed when os_env is None, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_spec_has_os_env(
    tmp_path: Path,
) -> None:
    """list_resources keeps the default environment when agent_spec.os_env is set."""
    reg = SessionResourceRegistry()
    spec = _agent_spec_with_sandbox_none(tmp_path / "workspace")

    page = reg.list_resources("conv_with_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be present — the spec has an os_env configured
    # so a primary filesystem environment exists and must be advertised.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when os_env is set, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_no_spec() -> None:
    """list_resources preserves legacy behaviour when agent_spec is None.

    Callers that do not pass an agent_spec (dev/standalone mode) must still
    see the default environment so the filesystem API is usable.
    """
    reg = SessionResourceRegistry()

    page = reg.list_resources("conv_legacy", agent_spec=None)

    ids = [r.id for r in page.data]
    # agent_spec=None is the legacy path; default env must always be present
    # so pre-existing callers that do not supply a spec are unaffected.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when agent_spec=None, but found ids: {ids}"
    )


# ── os_env gate: resolve_environment ────────────────────────────────────────


def test_resolve_environment_raises_when_spec_has_no_os_env() -> None:
    """resolve_environment raises ValueError when agent_spec.os_env is None.

    The registry must not silently fall back to a synthetic default
    environment when the spec explicitly has no os_env configured —
    that would create an environment the agent cannot use.
    """
    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    with pytest.raises(ValueError, match="no os_env"):
        reg.resolve_environment("conv_no_env", DEFAULT_ENVIRONMENT_ID, spec)


# ── Runner workspace overrides agent spec cwd ────────────────────────────


def test_compute_default_env_root_runner_workspace_overrides_relative_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has a relative
    cwd (``"."``), the runner workspace wins.

    This is the common case for CLI-launched sessions: the user's
    terminal cwd flows through ``OMNIGENT_RUNNER_WORKSPACE`` and
    the agent's relative cwd resolves against it.
    """
    workspace = tmp_path / "user-project"
    workspace.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_rel", spec)

    assert root == str(workspace.resolve())


def test_compute_default_env_root_runner_workspace_overrides_absolute_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has an absolute
    cwd, the runner workspace STILL wins.

    This is the new contract under
    designs/SESSION_WORKSPACE_SELECTION.md: an absolute cwd in the
    spec is a session-create-time *boundary*, not a runtime
    override. Host-launched sessions pick a workspace inside the
    boundary and that pick — not the boundary itself — drives the
    runtime cwd. Without this rule, a user picking
    ``~/universe/src/foo`` for an agent declaring ``cwd: ~/universe``
    would be silently relocated up to ``~/universe``.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_abs", spec)

    # Workspace wins, NOT spec_cwd.
    assert root == str(workspace.resolve())
    assert root != str(spec_cwd.resolve())


def test_compute_default_env_root_no_runner_workspace_uses_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is NOT set, an absolute spec cwd is used.

    This pins the fallback path so unit tests / pure local runs
    that construct a spec directly without the env var keep
    working as before.
    """
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(runner_workspace=None)
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_no_workspace", spec)

    assert root == str(spec_cwd.resolve())


def test_compute_default_env_root_no_os_env_returns_none(tmp_path: Path) -> None:
    """
    When the agent spec has no os_env, return None regardless of
    whether runner_workspace is set.

    Headless agents (no ``os_env``) intentionally don't expose the
    filesystem; the runner_workspace override must not bypass that
    gate. Without this check, host-launched headless agents would
    suddenly grow filesystem access.
    """
    reg = SessionResourceRegistry(
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(os_env=None)

    assert reg.compute_default_env_root("conv_headless", spec) is None


def test_resolve_environment_runner_workspace_overrides_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    Materializing the primary OS environment uses runner_workspace
    over an absolute spec cwd.

    Pairs with the compute_default_env_root tests above to cover
    the eager creation path. _create_primary_env and
    compute_default_env_root must agree on cwd, otherwise the
    filesystem-list endpoint and the agent's actual cwd would
    drift apart.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    env = reg.resolve_environment("conv_abs_eager", DEFAULT_ENVIRONMENT_ID, spec)

    assert env is not None
    # Compare via realpath because tmp_path on macOS goes through
    # /var → /private/var symlinks.
    assert os.path.realpath(env.cwd) == os.path.realpath(workspace)


@pytest.mark.parametrize(
    "raw,expected,why",
    [
        ("conv_abc123", "conv_abc123", "an ordinary id is untouched"),
        ("a" * 32, "a" * 32, "a uuid4().hex id is untouched"),
        ("a/b", "a_b", "a POSIX separator cannot survive"),
        ("a\\b", "a_b", "a Windows separator cannot survive either"),
        ("../..", ".._..", "separators go, leaving no traversal component"),
        ("..", "__", "a bare parent reference never survives"),
        (".", "_", "a bare self reference never survives"),
        ("", "_", "an empty id still yields a usable component"),
        ("a\x00b", "a_b", "a NUL cannot reach os.path.join"),
        ("a b", "a_b", "whitespace is normalized rather than quoted downstream"),
    ],
)
def test_sanitize_session_id_yields_one_safe_component(raw: str, expected: str, why: str) -> None:
    """``_sanitize_session_id`` must return a single, non-traversing path component.

    The id reaches the filesystem as a directory name under the runner
    workspace, so anything that could act as a separator or a parent reference
    has to be neutralized here. Uses an allowlist: the previous denylist
    stopped ``/`` and ``..`` but let a backslash through.
    """
    got = _sanitize_session_id(raw)

    assert got == expected, why
    # The invariants that actually matter, restated independently of the
    # table above so a wrong `expected` cannot make this vacuous.
    assert got, "must never be empty — it becomes a path component"
    assert "/" not in got and "\\" not in got, "must be a single component"
    assert set(got) != {"."}, "must not be '.' or '..'"


def test_sanitize_session_id_keeps_traversal_out_of_the_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hostile id cannot walk the session workspace out of the runner root.

    The unit test above pins the component; this pins the property callers
    actually depend on — that the joined path stays under the root.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    resolved = Path(_session_workspace("../../../../etc")).resolve()

    assert resolved.is_relative_to(tmp_path.resolve()), f"escaped the root: {resolved}"


# ── native bridge-dir reaping: live-session regression ──────────────────────


_AUTO_MODE_BILLING_NOTICE_PANE = """────────────────────────────────────────
  We're changing auto mode to no longer charge for classifier requests in Claude Code.
  However, this session isn't eligible because your requests go through
  gateway.example.com, which isn't compatible with this update.
  Nothing breaks: auto mode keeps working, and its classifier requests are billed as before.
  To fix it and access the new version of auto mode, ask your gateway to implement:
  https://code.claude.com/docs/en/auto-mode-classifier-billing
  Enter to continue · Esc to cancel
"""


async def _observe_native_capturing_tick(
    tmp_path: Path,
    session_id: str,
    terminal_name: str,
    resource_role: str,
    env: dict[str, str],
) -> tuple[object, TerminalInstance]:
    """Observe a native terminal and return its watcher ``on_tick`` callback.

    :returns: ``(on_tick, instance)`` for driving the input-ready probe.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    registry.set_session_status_publisher(lambda *_args: None)
    registry.set_terminal_exit_publisher(lambda *_args: None)
    instance = make_test_terminal_instance(terminal_name, "main", tmp_path)
    instance.env = env
    terminal_registry._by_conversation.setdefault(session_id, {})[(terminal_name, "main")] = (
        instance
    )
    callbacks: dict[str, object] = {}

    def _capture_watcher(*_args: object, on_tick: object | None = None, **_kwargs: object) -> None:
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    await registry.observe_required_terminal(
        session_id, terminal_name, "main", instance, resource_role=resource_role
    )
    assert callable(callbacks["on_tick"])
    return callbacks["on_tick"], instance


def _input_ready_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == "native_input_ready"]


_CLAUDE_READY_PANE = "────────────────────\n❯ \n────────────────────"
