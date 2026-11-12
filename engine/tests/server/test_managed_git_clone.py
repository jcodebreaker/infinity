"""Admin clone policy validation and managed sandbox lifecycle propagation."""

from dataclasses import replace

import pytest
from fastapi import HTTPException

from omnigent.hosts.types import GitCloneOptions, HostCapabilities
from omnigent.server.hosts import (
    HostDeployment,
    HostProviderConfig,
    launch_host,
    parse_host_section,
    parse_repo_workspace,
    relaunch_host,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher, HostStartInvocation


@pytest.mark.parametrize("block", [None, {}, {"depth": None, "single_branch": None}])
def test_default_clone_policy(block: object) -> None:
    config = parse_host_section(
        {"provider": "docker", "server_url": "https://example.test", "git_clone": block}
    )
    assert config is not None
    assert config.for_provider(None).git_clone == GitCloneOptions()


@pytest.mark.parametrize(
    "block",
    [
        [],
        "--depth=1",
        {"depth": True},
        {"depth": 0},
        {"depth": -1},
        {"depth": "50"},
        {"depth": 1.5},
        {"single_branch": "true"},
        {"single_branch": 1},
        {"tags": None},
        {"tags": "false"},
        {"tags": 0},
        {"filter": "tree:0"},
        {"filter": True},
        {"filter": ["blob:none"]},
        {"flags": "--upload-pack=anything"},
    ],
)
def test_invalid_clone_policy(block: object) -> None:
    with pytest.raises(ValueError, match=r"host\.git_clone"):
        parse_host_section(
            {"provider": "docker", "server_url": "https://example.test", "git_clone": block}
        )


def test_shared_policy_applies_to_provider_entries() -> None:
    config = parse_host_section(
        {
            "server_url": "https://example.test",
            "git_clone": {
                "depth": 50,
                "single_branch": True,
                "filter": "blob:none",
                "tags": False,
            },
            "providers": [{"provider": "docker"}],
        }
    )
    assert config is not None
    assert config.for_provider("docker").git_clone == GitCloneOptions(50, True, "blob:none", False)


def test_provider_entry_overrides_shared_policy() -> None:
    config = parse_host_section(
        {
            "server_url": "https://example.test",
            "git_clone": {"depth": 50},
            "providers": [{"provider": "docker", "git_clone": {}}],
        }
    )
    assert config is not None
    assert config.for_provider("docker").git_clone == GitCloneOptions()


@pytest.mark.asyncio
async def test_policy_reaches_launch_and_relaunch(db_uri: str) -> None:
    store = HostStore(db_uri)

    def connected(invocation: HostStartInvocation) -> None:
        store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id="alice"
        )

    fake = FakeSandboxLauncher(on_host_start=connected)
    options = GitCloneOptions(50, True, "blob:none", False)
    config = HostDeployment.single(
        HostProviderConfig("https://example.test", lambda: fake, 3600, git_clone=options)
    )
    repos = [parse_repo_workspace("https://github.com/org/repo.git#main")]
    launch = await launch_host(config=config, owner="alice", host_store=store, repos=repos)
    host = store.get_host(launch.host_id)
    assert host is not None
    await relaunch_host(host=host, config=config, host_store=store, repos=repos)
    clones = [cmd for cmd in fake.commands if cmd.startswith("git clone")]
    assert len(clones) == 2
    assert all(
        "--branch main --single-branch --depth 50 --filter=blob:none --no-tags --" in cmd
        for cmd in clones
    )
    assert repos[0].git_clone == GitCloneOptions()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["launch", "relaunch"])
async def test_unsupported_policy_rejected_before_lifecycle_changes(
    db_uri: str, operation: str
) -> None:
    class NativeLauncher(FakeSandboxLauncher):
        @property
        def capabilities(self) -> HostCapabilities:
            return replace(super().capabilities, git_clone_options=False)

    store = HostStore(db_uri)

    def connected(invocation: HostStartInvocation) -> None:
        store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id="alice"
        )

    fake = NativeLauncher(on_host_start=connected, can_resume=True)
    initial = HostProviderConfig("https://example.test", lambda: fake, 3600)
    first = await launch_host(
        config=HostDeployment.single(initial), owner="alice", host_store=store
    )
    store.set_offline(first.host_id)
    host = store.get_host(first.host_id)
    assert host is not None
    fake.provisioned_names.clear()
    config = HostDeployment.single(replace(initial, git_clone=GitCloneOptions(depth=50)))
    with pytest.raises(HTTPException, match=r"does not support host\.git_clone") as exc:
        if operation == "launch":
            await launch_host(config=config, owner="alice", host_store=store)
        else:
            await relaunch_host(host=host, config=config, host_store=store)
    assert exc.value.status_code == 400
    assert fake.provisioned_names == []
    assert fake.terminated == []
    assert fake.resumed == []
    assert store.get_host(first.host_id) == host
