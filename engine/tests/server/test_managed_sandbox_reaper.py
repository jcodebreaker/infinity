"""Tests for :mod:`omnigent.server.managed_sandbox_reaper`."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.server.hosts import (
    HostDeployment,
    HostProviderConfig,
    HostReaperConfig,
)
from omnigent.server.managed_sandbox_reaper import ManagedSandboxReaper
from omnigent.stores.host_store import Host, ManagedSandboxScanCursor, ManagedSandboxScanRow

pytestmark = pytest.mark.asyncio

_DAY_S = 24 * 60 * 60


class _RecordingLauncher:
    def __init__(
        self,
        provider: str,
        *,
        fail: bool = False,
        identity_fail: bool = False,
        on_terminate: Callable[[str], None] | None = None,
    ) -> None:
        self.provider = provider
        self.fail = fail
        self.identity_fail = identity_fail
        self.on_terminate = on_terminate
        self.factory_calls = 0
        self.identity_entries: list[int] = []
        self.identity_threads: list[int] = []
        self.terminated: list[str] = []
        self.termination_threads: list[int] = []

    @contextlib.contextmanager
    def reaper_identity(self, workspace_id: int) -> Iterator[None]:
        self.identity_entries.append(workspace_id)
        self.identity_threads.append(threading.get_ident())
        if self.identity_fail:
            raise RuntimeError(f"{self.provider} identity unavailable")
        yield

    def terminate(self, sandbox_id: str) -> None:
        self.termination_threads.append(threading.get_ident())
        if self.on_terminate is not None:
            self.on_terminate(sandbox_id)
        if self.fail:
            raise RuntimeError(f"{self.provider} unavailable")
        self.terminated.append(sandbox_id)


class _FakeHostStore:
    def __init__(self, hosts: dict[int, list[Host]]) -> None:
        self.hosts = {
            workspace_id: {host.host_id: host for host in workspace_hosts}
            for workspace_id, workspace_hosts in hosts.items()
        }
        self.detached: list[tuple[int, str, str]] = []
        self.cleared: list[tuple[int, str, str]] = []
        self.refresh_on_detach: set[str] = set()
        self.current_page_queries: list[tuple[ManagedSandboxScanCursor | None, int]] = []
        self.terminating_page_queries: list[tuple[ManagedSandboxScanCursor | None, int]] = []

    def list_current_managed_sandbox_hosts_page(
        self,
        *,
        after: ManagedSandboxScanCursor | None,
        limit: int,
    ) -> list[ManagedSandboxScanRow]:
        self.current_page_queries.append((after, limit))
        rows = sorted(
            (
                (host.sandbox_id, workspace_id, host.host_id, host)
                for workspace_id, hosts in self.hosts.items()
                for host in hosts.values()
                if host.sandbox_id is not None
            ),
            key=lambda row: row[:3],
        )
        if after is not None:
            rows = [row for row in rows if row[:3] > after]
        return [(workspace_id, host) for _, workspace_id, _, host in rows[:limit]]

    def list_terminating_managed_sandbox_hosts_page(
        self,
        *,
        after: ManagedSandboxScanCursor | None,
        limit: int,
    ) -> list[ManagedSandboxScanRow]:
        self.terminating_page_queries.append((after, limit))
        rows = sorted(
            (
                (host.terminating_sandbox_id, workspace_id, host.host_id, host)
                for workspace_id, hosts in self.hosts.items()
                for host in hosts.values()
                if host.terminating_sandbox_id is not None
            ),
            key=lambda row: row[:3],
        )
        if after is not None:
            rows = [row for row in rows if row[:3] > after]
        return [(workspace_id, host) for _, workspace_id, _, host in rows[:limit]]

    def detach_stale_managed_sandbox(
        self,
        host_id: str,
        *,
        sandbox_id: str,
        expected_updated_at: int,
    ) -> bool:
        workspace_id = current_workspace_id()
        workspace_hosts = self.hosts[current_workspace_id()]
        host = workspace_hosts.get(host_id)
        if host is not None and host_id in self.refresh_on_detach:
            host = replace(host, status="online", updated_at=4_000_000)
            workspace_hosts[host_id] = host
            self.refresh_on_detach.remove(host_id)
        if (
            host is None
            or host.deleted_at is not None
            or host.sandbox_id != sandbox_id
            or host.updated_at != expected_updated_at
            or host.terminating_sandbox_id is not None
        ):
            return False
        workspace_hosts[host_id] = replace(
            host,
            status="offline",
            sandbox_id=None,
            terminating_sandbox_id=sandbox_id,
        )
        self.detached.append((workspace_id, host_id, sandbox_id))
        return True

    def mark_sandbox_terminated(
        self,
        host_id: str,
        *,
        sandbox_id: str,
    ) -> bool:
        workspace_id = current_workspace_id()
        host = self.hosts[workspace_id].get(host_id)
        if host is None:
            return False
        if host.deleted_at is None:
            if host.terminating_sandbox_id != sandbox_id:
                return False
            self.hosts[workspace_id][host_id] = replace(
                host,
                terminating_sandbox_id=None,
            )
            self.cleared.append((workspace_id, host_id, sandbox_id))
            return True
        if sandbox_id not in {host.sandbox_id, host.terminating_sandbox_id}:
            return False
        updated = replace(
            host,
            sandbox_id=None if host.sandbox_id == sandbox_id else host.sandbox_id,
            terminating_sandbox_id=(
                None if host.terminating_sandbox_id == sandbox_id else host.terminating_sandbox_id
            ),
        )
        if updated.sandbox_id is None and updated.terminating_sandbox_id is None:
            del self.hosts[workspace_id][host_id]
        else:
            self.hosts[workspace_id][host_id] = updated
        self.cleared.append((workspace_id, host_id, sandbox_id))
        return True


def _host(
    host_id: str,
    *,
    provider: str,
    sandbox_id: str | None,
    updated_at: int,
    status: str = "offline",
    terminating_sandbox_id: str | None = None,
    deleted_at: int | None = None,
) -> Host:
    return Host(
        host_id=host_id,
        name=host_id,
        user_id="alice@example.com",
        status=status,
        created_at=updated_at - 100,
        updated_at=updated_at,
        sandbox_provider=provider,
        sandbox_id=sandbox_id,
        terminating_sandbox_id=terminating_sandbox_id,
        deleted_at=deleted_at,
    )


def _deployment(
    launchers: list[_RecordingLauncher],
    *,
    terminate_after_offline_days: int = 1,
    sweep_interval_s: int = 86400,
) -> HostDeployment:
    configs = []
    for launcher in launchers:

        def launcher_factory(launcher: _RecordingLauncher = launcher) -> _RecordingLauncher:
            launcher.factory_calls += 1
            return launcher

        configs.append(
            HostProviderConfig(
                server_url="https://s.example.com",
                launcher_factory=launcher_factory,
                token_ttl_s=3600,
                provider=launcher.provider,
            )
        )
    return HostDeployment(
        configs=tuple(configs),
        reaper=HostReaperConfig(
            enabled=True,
            terminate_after_offline_days=terminate_after_offline_days,
            sweep_interval_s=sweep_interval_s,
        ),
    )


async def test_sweep_reaps_stale_host_rows_without_scanning_sessions() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha")
    beta = _RecordingLauncher("beta")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_recent",
                    provider="alpha",
                    sandbox_id="sb-recent",
                    updated_at=now - _DAY_S + 1,
                ),
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - _DAY_S - 1,
                ),
                _host(
                    "host_alpha_2",
                    provider="alpha",
                    sandbox_id="sb-alpha-2",
                    updated_at=now - 2 * _DAY_S,
                ),
            ],
            9: [
                _host(
                    "host_beta",
                    provider="beta",
                    sandbox_id="sb-beta",
                    updated_at=now - 2 * _DAY_S,
                    status="online",
                )
            ],
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha, beta]),
    )

    assert await reaper.sweep_once(now=now) == 3

    assert alpha.terminated == ["sb-alpha-2", "sb-alpha"]
    assert beta.terminated == ["sb-beta"]
    assert hosts.detached == [
        (7, "host_alpha_2", "sb-alpha-2"),
        (7, "host_alpha", "sb-alpha"),
        (9, "host_beta", "sb-beta"),
    ]
    assert hosts.cleared == hosts.detached
    assert alpha.factory_calls == 1
    assert alpha.identity_entries == [7]
    assert beta.factory_calls == 1
    assert beta.identity_entries == [9]
    assert hosts.hosts[7]["host_alpha"].sandbox_id is None
    assert hosts.hosts[7]["host_alpha"].terminating_sandbox_id is None
    assert hosts.hosts[7]["host_recent"].sandbox_id == "sb-recent"
    assert hosts.terminating_page_queries == [(None, 500)]
    assert hosts.current_page_queries == [(None, 500)]


async def test_sweep_completely_traverses_bounded_slot_pages() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_current_a",
                    provider="alpha",
                    sandbox_id="sb-current-a",
                    updated_at=now - 2 * _DAY_S,
                ),
                _host(
                    "host_current_b",
                    provider="alpha",
                    sandbox_id="sb-current-b",
                    updated_at=now - 3 * _DAY_S,
                ),
                _host(
                    "host_current_c",
                    provider="alpha",
                    sandbox_id="sb-current-c",
                    updated_at=now - 4 * _DAY_S,
                ),
                _host(
                    "host_pending_a",
                    provider="alpha",
                    sandbox_id=None,
                    terminating_sandbox_id="sb-pending-a",
                    updated_at=now,
                ),
                _host(
                    "host_pending_b",
                    provider="alpha",
                    sandbox_id=None,
                    terminating_sandbox_id="sb-pending-b",
                    updated_at=now,
                ),
                _host(
                    "host_pending_c",
                    provider="alpha",
                    sandbox_id=None,
                    terminating_sandbox_id="sb-pending-c",
                    updated_at=now,
                ),
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
        scan_batch_size=2,
    )

    assert await reaper.sweep_once(now=now) == 6
    assert set(alpha.terminated) == {
        "sb-current-a",
        "sb-current-b",
        "sb-current-c",
        "sb-pending-a",
        "sb-pending-b",
        "sb-pending-c",
    }
    assert hosts.terminating_page_queries == [
        (None, 2),
        (("sb-pending-b", 7, "host_pending_b"), 2),
    ]
    assert hosts.current_page_queries == [
        (None, 2),
        (("sb-current-b", 7, "host_current_b"), 2),
    ]


async def test_provider_failure_does_not_stop_other_sandboxes() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha", fail=True)
    beta = _RecordingLauncher("beta")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - 2 * _DAY_S,
                ),
                _host(
                    "host_beta",
                    provider="beta",
                    sandbox_id="sb-beta",
                    updated_at=now - 2 * _DAY_S,
                ),
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha, beta]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert hosts.hosts[7]["host_alpha"].sandbox_id is None
    assert hosts.hosts[7]["host_alpha"].terminating_sandbox_id == "sb-alpha"
    assert hosts.hosts[7]["host_beta"].sandbox_id is None
    assert beta.terminated == ["sb-beta"]


async def test_deleted_host_cleanup_retries_without_offline_age() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha", fail=True)
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_deleted",
                    provider="alpha",
                    sandbox_id="sb-deleted",
                    updated_at=now,
                    status="online",
                    deleted_at=now,
                )
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
    )

    assert await reaper.sweep_once(now=now) == 0
    assert hosts.hosts[7]["host_deleted"].sandbox_id == "sb-deleted"

    alpha.fail = False
    assert await reaper.sweep_once(now=now) == 1
    assert alpha.terminated == ["sb-deleted"]
    assert "host_deleted" not in hosts.hosts[7]


async def test_provider_identity_failure_does_not_stop_other_sandboxes() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha", identity_fail=True)
    beta = _RecordingLauncher("beta")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - 2 * _DAY_S,
                ),
                _host(
                    "host_beta",
                    provider="beta",
                    sandbox_id="sb-beta",
                    updated_at=now - 2 * _DAY_S,
                ),
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha, beta]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert alpha.terminated == []
    assert beta.terminated == ["sb-beta"]
    assert hosts.hosts[7]["host_alpha"].sandbox_id == "sb-alpha"
    assert hosts.hosts[7]["host_beta"].sandbox_id is None


async def test_reaper_rereads_liveness_before_termination() -> None:
    now = 4_000_000
    alpha = _RecordingLauncher("alpha")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )
    hosts.refresh_on_detach.add("host_alpha")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
    )

    assert await reaper.sweep_once(now=now) == 0
    assert alpha.terminated == []
    assert hosts.hosts[7]["host_alpha"].sandbox_id == "sb-alpha"


async def test_successful_termination_preserves_new_active_generation() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )

    def _launch_new_generation(sandbox_id: str) -> None:
        assert sandbox_id == "sb-alpha"
        current = hosts.hosts[7]["host_alpha"]
        assert current.sandbox_id is None
        assert current.terminating_sandbox_id == "sb-alpha"
        hosts.hosts[7]["host_alpha"] = replace(
            current,
            sandbox_id="sb-alpha-new",
            status="online",
            updated_at=now,
        )

    alpha = _RecordingLauncher("alpha", on_terminate=_launch_new_generation)
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert alpha.terminated == ["sb-alpha"]
    assert hosts.hosts[7]["host_alpha"].sandbox_id == "sb-alpha-new"
    assert hosts.hosts[7]["host_alpha"].terminating_sandbox_id is None


async def test_pending_termination_retries_without_detaching_active_generation() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha-new",
                    terminating_sandbox_id="sb-alpha-old",
                    updated_at=now,
                    status="online",
                )
            ]
        }
    )
    alpha = _RecordingLauncher("alpha")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert alpha.terminated == ["sb-alpha-old"]
    assert hosts.detached == []
    assert hosts.hosts[7]["host_alpha"].sandbox_id == "sb-alpha-new"
    assert hosts.hosts[7]["host_alpha"].terminating_sandbox_id is None


async def test_pending_id_equal_to_active_id_is_not_terminated() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_reused",
                    provider="reused",
                    sandbox_id="managed-host_reused",
                    terminating_sandbox_id="managed-host_reused",
                    updated_at=now,
                    status="online",
                )
            ]
        }
    )
    reused = _RecordingLauncher("reused")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([reused]),
    )

    assert await reaper.sweep_once(now=now) == 0
    assert reused.terminated == []
    current = hosts.hosts[7]["host_reused"]
    assert current.sandbox_id == "managed-host_reused"
    assert current.terminating_sandbox_id == "managed-host_reused"


async def test_identity_and_termination_run_off_event_loop_thread() -> None:
    now = 4_000_000
    event_loop_thread = threading.get_ident()
    alpha = _RecordingLauncher("alpha")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_alpha",
                    provider="alpha",
                    sandbox_id="sb-alpha",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([alpha]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert alpha.identity_threads == alpha.termination_threads
    assert alpha.identity_threads[0] != event_loop_thread
