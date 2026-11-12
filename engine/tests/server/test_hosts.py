"""Tests for :mod:`omnigent.server.hosts`."""

from __future__ import annotations

import asyncio
import datetime
import re
import sys
import types
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import click
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from omnigent.db.utils import builtin_agent_id, generate_agent_id, now_epoch
from omnigent.entities.agent import Agent
from omnigent.hosts.launcher import (
    HostGoneError,
    HostLauncher,
    render_host_config_write_command,
)
from omnigent.hosts.registry import (
    COMMUNITY_MODULE_PREFIX,
    HostProviderContribution,
    HostProviderMetadata,
    reset_plugin_state_for_tests,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.hosts import (
    HOST_TOKEN_TTL_S,
    MANAGED_REPO_LABEL_KEY,
    HostDeployment,
    HostLaunch,
    HostProviderConfig,
    ManagedLaunch,
    ManagedLaunchTracker,
    RepoWorkspace,
    host_resume_supported,
    launch_host,
    managed_repo_labels,
    parse_host_section,
    parse_repo_workspace,
    read_managed_repo_workspaces,
    relaunch_host,
    resolve_managed_agent_label,
    resume_host,
    terminate_host,
)
from omnigent.server.managed_sandbox_reaper import ManagedSandboxReaper
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import Host, HostStore
from tests.server.helpers import (
    FakeSandboxLauncher,
    HostStartInvocation,
)

pytestmark = pytest.mark.asyncio

_OWNER = "alice@example.com"


def _injected_config(
    fake: FakeSandboxLauncher,
    *,
    server_url: str = "https://srv.example.com",
    token_ttl_s: int = 3600,
    host_config: dict[str, object] | None = None,
) -> HostDeployment:
    """
    Build a one-provider deployment that injects *fake* through the
    launcher-factory seam — the same way an embedding deployment injects
    a custom launcher.

    :param fake: The launcher every launch should use.
    :param server_url: Server URL the sandbox host dials back to.
    :param token_ttl_s: Launch-token lifetime in seconds.
    :param host_config: In-sandbox config.yaml content to forward, or ``None``.
    :returns: A ready one-provider :class:`HostDeployment`.
    """
    return HostDeployment.single(
        HostProviderConfig(
            server_url=server_url,
            launcher_factory=lambda: fake,
            token_ttl_s=token_ttl_s,
            host_config=host_config,
        )
    )


class _ClassifyingFakeSandboxLauncher(FakeSandboxLauncher):
    """
    A fake that declares ``classifies_runner_by_agent`` and records the
    ``agent_name`` threaded to ``start_host``.

    Stands in for a provider that consumes the classifier. The exec-model
    :class:`FakeSandboxLauncher` does NOT declare the capability, so the launch
    path never passes it the keyword.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.agent_names: list[str | None] = []

    @property
    def capabilities(self) -> Any:
        return replace(super().capabilities, classifies_runner_by_agent=True)

    def start_host(self, sandbox_id: str, *, agent_name: str | None = None, **kwargs: Any) -> str:
        """Record the threaded agent name, then run the shared exec-model start."""
        self.agent_names.append(agent_name)
        return super().start_host(sandbox_id, **kwargs)


class _StubAgentStore:
    """Minimal AgentStore stand-in returning crafted agents (or raising)."""

    def __init__(self, agents: dict[str, Agent] | None = None, *, error: bool = False) -> None:
        self._agents = agents or {}
        self._error = error

    def get(self, agent_id: str) -> Agent | None:
        if self._error:
            raise RuntimeError("simulated agent-store failure")
        return self._agents.get(agent_id)


# ── parse_host_section ────────────────────────────────────


def test_parse_absent_section_disables_managed_hosts() -> None:
    """No ``hosts:`` section → managed hosts simply not configured."""
    assert parse_host_section(None) is None


def test_parse_reaper_defaults_disabled_with_thirty_day_cutoff() -> None:
    """Managed sandbox cleanup is opt-in and defaults to a 30-day age."""
    config = parse_host_section({"provider": "docker", "server_url": "https://s.example.com"})
    assert config is not None
    assert config.reaper.enabled is False
    assert config.reaper.terminate_after_offline_days == 30
    assert config.reaper.sweep_interval_s == 86400


def test_parse_reaper_is_one_deployment_wide_policy_for_multiple_providers(
    contributed_acme_provider: None,
) -> None:
    """One top-level reaper config applies across every offered provider."""
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "reaper": {
                "enabled": True,
                "terminate_after_offline_days": 7,
                "sweep_interval_s": 1234,
            },
            "providers": [
                {"provider": "docker"},
                {"provider": "acme"},
            ],
        }
    )
    assert config is not None
    assert config.reaper.enabled is True
    assert config.reaper.terminate_after_offline_days == 7
    assert config.reaper.sweep_interval_s == 1234
    assert [entry.provider for entry in config.offered()] == ["docker", "acme"]


@pytest.mark.parametrize(
    ("reaper", "expected_fragment"),
    [
        (True, "must be a mapping"),
        ({"enabled": "yes"}, "enabled.*boolean"),
        ({"terminate_after_offline_days": 0}, "positive integer"),
        ({"terminate_after_offline_days": True}, "positive integer"),
        ({"terminate_after_offline_days": "30d"}, "positive integer"),
        ({"sweep_interval_s": 0}, "positive integer"),
        ({"sweep_interval_s": True}, "positive integer"),
        ({"sweep_interval_s": "1d"}, "positive integer"),
        ({"offline_after_s": 60}, "unknown key"),
    ],
)
def test_parse_reaper_rejects_malformed_config(
    reaper: object,
    expected_fragment: str,
) -> None:
    """Reaper typos fail server startup instead of silently disabling cleanup."""
    with pytest.raises(ValueError, match=expected_fragment):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "reaper": reaper,
            }
        )


def test_parse_reaper_rejects_per_provider_override(contributed_acme_provider: None) -> None:
    """The reaper loop is configured once, never independently per provider."""
    with pytest.raises(ValueError, match="deployment-wide"):
        parse_host_section(
            {
                "server_url": "https://s.example.com",
                "providers": [
                    {"provider": "docker", "reaper": {"enabled": True}},
                    {"provider": "acme"},
                ],
            }
        )


def test_parse_host_config_threads_verbatim_without_resolving_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A valid host_config lands on the parsed config verbatim, and its
    ``api_key_ref: env:`` reference is NOT resolved at parse time — the
    variable names sandbox environment, not server environment, so parsing
    must succeed with the variable unset on the server.
    """
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_LITELLM_API_KEY", raising=False)
    host_config = {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "default": ["pi"],
                "openai": {
                    "base_url": "http://litellm.litellm.svc.cluster.local/v1",
                    "api_key_ref": "env:LITELLM_API_KEY",
                    "wire_api": "chat",
                },
            }
        }
    }

    cfg = parse_host_section(
        {"provider": "docker", "server_url": "https://s.example.com", "host_config": host_config}
    )

    assert cfg is not None
    cfg = cfg.default
    assert cfg.host_config == host_config


def test_parse_absent_host_config_is_none() -> None:
    """No host_config key → nothing forwarded, existing configs unchanged."""
    cfg = parse_host_section({"provider": "docker", "server_url": "https://s.example.com"})
    assert cfg is not None
    cfg = cfg.default
    assert cfg.host_config is None


def test_parse_runner_env_accepts_valid_names() -> None:
    cfg = parse_host_section(
        {
            "provider": "docker",
            "server_url": "https://s.example.com",
            "runner_env": ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "SUPERCHAT_CLAUDE_MODEL"],
        }
    )
    assert cfg is not None
    cfg = cfg.default
    assert cfg.runner_env == ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "SUPERCHAT_CLAUDE_MODEL")


def test_parse_absent_runner_env_is_empty() -> None:
    cfg = parse_host_section({"provider": "docker", "server_url": "https://s.example.com"})
    assert cfg is not None
    assert cfg.default.runner_env == ()


def test_parse_runner_env_deduplicates_preserving_first_occurrence() -> None:
    cfg = parse_host_section(
        {
            "provider": "docker",
            "server_url": "https://s.example.com",
            "runner_env": ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"],
        }
    )
    assert cfg is not None
    assert cfg.default.runner_env == ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")


@pytest.mark.parametrize(
    "runner_env",
    [
        "ANTHROPIC_API_KEY",  # not a list
        [123],  # not strings
        ["lowercase_name"],
        ["Mixed_Case"],
        ["HAS-DASH"],
        ["HAS SPACE"],
        ["1_LEADS_WITH_DIGIT"],
        [""],
    ],
)
def test_parse_runner_env_rejects_malformed_entries(runner_env: object) -> None:
    with pytest.raises(ValueError, match=r"host\.runner_env"):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "runner_env": runner_env,
            }
        )


def test_parse_runner_env_is_shared_across_multiple_providers(
    contributed_acme_provider: None,
) -> None:
    """A top-level ``runner_env`` (like ``server_url``) applies to every offered provider,
    not just the one that happens to be default."""
    cfg = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "runner_env": ["ANTHROPIC_API_KEY"],
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    assert cfg is not None
    assert all(entry.runner_env == ("ANTHROPIC_API_KEY",) for entry in cfg.offered())


def test_parse_host_config_null_providers_fails_loud() -> None:
    """
    An explicit ``providers: null`` fails parse. Left through, the sandbox
    merge would write ``providers: null`` over any existing block and the
    harness would silently fall back to its own login — the exact
    degradation this parse exists to stop.
    """
    with pytest.raises(ValueError, match=r"host\.host_config\.providers"):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "host_config": {"providers": None},
            }
        )


def test_parse_host_config_duplicate_default_fails_loud() -> None:
    """Duplicate defaults fail at server startup, before sandbox launch."""
    provider = {
        "kind": "gateway",
        "default": ["pi"],
        "openai": {
            "base_url": "https://gateway.example.com/v1",
            "api_key_ref": "env:GATEWAY_API_KEY",
        },
    }

    with pytest.raises(
        ValueError,
        match=r"host\.host_config\.providers.*multiple providers.*'pi' family",
    ):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "host_config": {
                    "providers": {
                        "first": provider,
                        "second": provider,
                    }
                },
            }
        )


def test_parse_host_config_inline_api_key_fails_loud() -> None:
    """Literal provider credentials cannot ride in the managed host config."""
    with pytest.raises(ValueError, match=r"api_key_ref: env:VAR"):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "host_config": {
                    "providers": {
                        "openai": {
                            "kind": "key",
                            "openai": {
                                "base_url": "https://api.openai.com/v1",
                                "api_key": "sk-inline-secret",
                            },
                        }
                    }
                },
            }
        )


def test_parse_host_config_lossy_json_key_collision_fails_loud() -> None:
    """JSON key coercion cannot silently collapse distinct config entries."""
    with pytest.raises(ValueError, match=r"JSON-serializable"):
        parse_host_section(
            {
                "provider": "docker",
                "server_url": "https://s.example.com",
                "host_config": {"metadata": {1: "integer", "1": "string"}},
            }
        )


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        # Non-mapping section.
        ("docker", "must be a mapping"),
        # Unknown / missing provider.
        ({"provider": "bogus", "server_url": "https://s"}, "host.provider"),
        ({"server_url": "https://s"}, "host.provider"),
        # Missing / empty server_url.
        ({"provider": "docker"}, "server_url"),
        ({"provider": "docker", "server_url": "  "}, "server_url"),
        # host_config present but malformed (provider-agnostic top-level key).
        (
            {"provider": "docker", "server_url": "https://s", "host_config": "providers: {}"},
            "host.host_config",
        ),
        (
            {"provider": "docker", "server_url": "https://s", "host_config": {"providers": "x"}},
            "host.host_config.providers",
        ),
        # An invalid provider entry (bad kind) is caught by the same parser
        # omnigent itself uses — inside the sandbox this would degrade
        # silently, so parse time is the only loud failure point.
        (
            {
                "provider": "docker",
                "server_url": "https://s",
                "host_config": {"providers": {"litellm": {"kind": "bogus"}}},
            },
            "host.host_config.providers",
        ),
        # yaml.safe_load turns an unquoted date into datetime.date, which the
        # per-launch json.dumps cannot take — must fail startup, not launches.
        (
            {
                "provider": "docker",
                "server_url": "https://s",
                "host_config": {"last_rotated": datetime.date(2024, 1, 1)},
            },
            "JSON-serializable",
        ),
    ],
)
def test_parse_invalid_config_fails_loud(raw: object, expected_fragment: str) -> None:
    """
    Malformed config raises with the offending key named — this is
    what stops server startup on an operator typo instead of 502-ing
    the first managed session.
    """
    with pytest.raises(ValueError, match="") as exc:
        parse_host_section(raw)
    assert expected_fragment in str(exc.value)


# ── parse_repo_workspace ────────────────────────────────────


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        # Plain https URL — default branch, name from the last segment.
        (
            "https://github.com/org/repo",
            RepoWorkspace(url="https://github.com/org/repo", branch=None, repo_name="repo"),
        ),
        # `.git` suffix stripped from the directory name, kept in the URL.
        (
            "https://github.com/org/repo.git#release-1.2",
            RepoWorkspace(
                url="https://github.com/org/repo.git",
                branch="release-1.2",
                repo_name="repo",
            ),
        ),
        # scp-style ssh form.
        (
            "git@github.com:org/repo.git",
            RepoWorkspace(url="git@github.com:org/repo.git", branch=None, repo_name="repo"),
        ),
        # Branches with slashes are legal git refs.
        (
            "https://github.com/org/repo#feature/x",
            RepoWorkspace(url="https://github.com/org/repo", branch="feature/x", repo_name="repo"),
        ),
    ],
)
def test_parse_repo_workspace_accepts_url_forms(workspace: str, expected: RepoWorkspace) -> None:
    """
    The documented ``<repo>[#<branch>]`` grammar parses into the
    validated spec the clone step consumes — URL, pinned branch, and
    the clone directory name all come from here, so a wrong field
    means a wrong `git clone` invocation.
    """
    assert parse_repo_workspace(workspace) == expected


@pytest.mark.parametrize(
    ("workspace", "expected_fragment"),
    [
        # Absolute paths are the EXTERNAL form — a path points at
        # nothing in a sandbox that doesn't exist yet.
        ("/tmp/w", "not a supported repository URL"),
        # Bare org/repo shorthand is UI-side sugar, never API surface.
        ("org/repo", "not a supported repository URL"),
        # No repo path at all.
        ("https://github.com", "not a usable https repository URL"),
        ("git@github.com", "not a usable ssh repository URL"),
        # Commit SHAs would land the agent on a detached HEAD.
        ("https://github.com/org/repo#" + "a" * 40, "not a commit SHA"),
        # Empty / malformed branch fragments.
        ("https://github.com/org/repo#", "must name a branch"),
        ("https://github.com/org/repo#-flag", "not a valid git branch name"),
        ("https://github.com/org/repo#a..b", "not a valid git branch name"),
        # A second '#' means the branch itself contains '#' —
        # unsupported in the fragment form.
        ("https://github.com/org/repo#a#b", "not a valid git branch name"),
        ("https://github.com/org/repo#a b", "must not contain whitespace"),
        # Embedded credentials would be persisted verbatim in a label + Pod spec —
        # rejected in the https AND the ssh (scp-form) branch.
        ("https://user:token@github.com/org/repo", "must not embed credentials"),
        ("git@x-access-token:tok@github.com:org/repo.git", "must not embed credentials"),
    ],
)
def test_parse_repo_workspace_rejects_malformed(workspace: str, expected_fragment: str) -> None:
    """
    Malformed workspaces fail loud at parse time with the offense
    named — this is what turns into the create's 422 instead of a
    mid-provision clone error inside a half-launched sandbox.
    """
    with pytest.raises(ValueError, match="") as exc:
        parse_repo_workspace(workspace)
    assert expected_fragment in str(exc.value)


@pytest.mark.parametrize(
    "workspace",
    [
        "https://x-access-token:s3cr3t@github.com/org/repo",
        # No path → trips the shape guard; the secret must still not be echoed
        # (the credential check runs before any URL-echoing error).
        "https://user:s3cr3t@github.com",
        "git@x-access-token:s3cr3t@github.com:org/repo.git",
    ],
)
def test_credential_rejection_does_not_echo_the_token(workspace: str) -> None:
    """The credential-URL error must not include the URL — that carries the secret."""
    with pytest.raises(ValueError) as exc:
        parse_repo_workspace(workspace)
    assert "s3cr3t" not in str(exc.value)


def test_managed_repo_labels_round_trip() -> None:
    """Per-repo labels avoid the 256-char single-label cap and read back in order.

    A bare space-joined key rides along as a rollback compat shim (it fits here),
    but the indexed labels are what the current reader uses.
    """
    workspaces = ["https://github.com/org/api#main", "https://github.com/org/web"]
    labels = managed_repo_labels(workspaces)
    assert labels == {
        "omnigent.sandbox.repo.0": "https://github.com/org/api#main",
        "omnigent.sandbox.repo.1": "https://github.com/org/web",
        # Legacy bare key for a rolled-back/older reader (written only when it fits).
        "omnigent.sandbox.repo": "https://github.com/org/api#main https://github.com/org/web",
    }
    assert read_managed_repo_workspaces(labels) == workspaces
    assert managed_repo_labels([]) == {}


def test_managed_repo_labels_omit_bare_key_when_over_cap() -> None:
    """The compat bare key is skipped (never truncated) when the joined value
    would exceed the label-value cap; the indexed labels still carry every repo."""
    # Enough long URLs that the space-joined value exceeds 256 chars.
    workspaces = [f"https://github.com/some-long-org-name/repository-number-{i}" for i in range(6)]
    labels = managed_repo_labels(workspaces)
    assert "omnigent.sandbox.repo" not in labels  # no truncated bare value stored
    assert read_managed_repo_workspaces(labels) == workspaces  # indexed labels intact


def test_read_managed_repo_workspaces_orders_numerically_and_falls_back() -> None:
    """Indices order numerically (not lexically), and the legacy single label still reads."""
    # .10 must sort after .2, not before it (lexical order would break past 9).
    many = {f"omnigent.sandbox.repo.{i}": f"https://github.com/o/r{i}" for i in range(11)}
    assert read_managed_repo_workspaces(many)[-1] == "https://github.com/o/r10"
    # Legacy single space-joined label (pre-per-repo storage) still round-trips.
    legacy = {"omnigent.sandbox.repo": "https://github.com/o/a https://github.com/o/b"}
    assert read_managed_repo_workspaces(legacy) == [
        "https://github.com/o/a",
        "https://github.com/o/b",
    ]
    assert read_managed_repo_workspaces({}) == []


def test_provider_ui_capabilities_reports_multi_repo_per_provider() -> None:
    """The /v1/info map reflects each launchable provider's declared multi_repo,
    keyed by provider name; a provider declaring nothing stays off."""

    class _MultiRepoFake(FakeSandboxLauncher):
        @property
        def capabilities(self) -> Any:
            return replace(super().capabilities, multi_repo=True)

    single = FakeSandboxLauncher()  # exec-model default → multi_repo False
    multi = _MultiRepoFake()
    deployment = HostDeployment(
        configs=(
            HostProviderConfig(
                server_url="https://s",
                provider="single",
                token_ttl_s=3600,
                launcher_factory=lambda: single,
            ),
            HostProviderConfig(
                server_url="https://s",
                provider="multi",
                token_ttl_s=3600,
                launcher_factory=lambda: multi,
            ),
        )
    )
    assert deployment.provider_ui_capabilities() == {
        "single": {"multi_repo": False},
        "multi": {"multi_repo": True},
    }


# ── GET /v1/info: managed_sandboxes_enabled ─────────────────


def _capability_probe_app(
    db_uri: str,
    tmp_path: Path,
    sandbox_config: HostDeployment | None,
    *,
    host_store: HostStore | None = None,
) -> FastAPI:
    """
    Build a real app wired with *sandbox_config* to probe ``GET /v1/info``.

    Minimal store wiring — the probe handler reads only the
    ``sandbox_config`` closure, but the app factory needs real stores.

    :param db_uri: SQLite connection URI for the app's stores.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :param sandbox_config: The sandbox config under test, or ``None``
        when managed hosts are not configured.
    :param host_store: Optional managed-host store for lifespan tests.
    :returns: The assembled FastAPI app.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=host_store,
        sandbox_config=sandbox_config,
    )


@pytest.mark.parametrize("enabled", [False, True])
async def test_lifespan_gates_one_deployment_wide_managed_sandbox_reaper(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    enabled: bool,
    contributed_acme_provider: None,
) -> None:
    """One lifespan task covers all providers and obeys the global enable flag."""
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "reaper": {"enabled": enabled},
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    assert config is not None
    app = _capability_probe_app(
        db_uri,
        tmp_path,
        config,
        host_store=HostStore(db_uri),
    )

    async with app.router.lifespan_context(app):
        reaper = getattr(app.state, "managed_sandbox_reaper", None)
        if enabled:
            assert isinstance(reaper, ManagedSandboxReaper)
            assert reaper._sandbox_config is config
            assert reaper._task is not None
            assert not reaper._task.done()
        else:
            assert reaper is None

    if enabled:
        assert reaper._task is None


@pytest.mark.parametrize(
    ("sandbox_raw", "expected", "expected_provider"),
    [
        # Launch-capable provider configured → the web UI may offer the
        # sandbox option, labeled with the provider name.
        ({"provider": "docker", "server_url": "https://s.example.com"}, True, "docker"),
        # No `hosts:` section → a managed create would 400; the option
        # must not be advertised and no provider is named.
        (None, False, None),
    ],
)
async def test_info_reports_managed_sandboxes_capability(
    db_uri: str,
    tmp_path: Path,
    sandbox_raw: dict[str, object] | None,
    expected: bool,
    expected_provider: str | None,
) -> None:
    """
    ``GET /v1/info`` advertises managed sandboxes iff the wired config
    can actually serve a managed launch, and names the backing provider
    (``sandbox_provider``) so the web UI can label the option per
    provider — but only when the option is actually offered.
    """
    app = _capability_probe_app(db_uri, tmp_path, parse_host_section(sandbox_raw))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed_sandboxes_enabled"] is expected
    # The provider name is surfaced only when the option is offered; an
    # absent config leaks nothing (provider stays None).
    assert body["sandbox_provider"] == expected_provider


async def test_info_reports_enabled_for_injected_custom_launcher(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    The embedding seam: a directly-constructed config (custom launcher
    factory, no YAML) defaults to advertising managed launch — the
    deployment's factory IS the support. With no provider named, the UI
    falls back to the generic "New Sandbox" label (``sandbox_provider``
    is None).
    """
    config = HostDeployment.single(
        HostProviderConfig(
            server_url="https://s.example.com",
            launcher_factory=lambda: FakeSandboxLauncher(),
            token_ttl_s=3600,
        )
    )
    app = _capability_probe_app(db_uri, tmp_path, config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed_sandboxes_enabled"] is True
    # No provider set on the injected config → the UI keeps the generic
    # label rather than inventing a name.
    assert body["sandbox_provider"] is None


# ── launch_host ─────────────────────────────────────


async def test_launch_success_registers_host_and_returns_workspace(db_uri: str) -> None:
    """
    Golden path: provision → pre-register the host row with its token
    → start host → host online.

    The launcher arrives through the config's factory seam (no
    patching), and the fake's ``on_host_start`` connects exactly as
    the real tunnel would after validating the launch token
    (``upsert_on_connect`` against the pre-registered row), so the
    online poll observes a genuine hosts-table transition.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    result = await launch_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
    )

    assert fake.prepared is True
    # The workspace was created in the sandbox's home and returned.
    assert result.workspace == "/root/workspace"
    assert any("mkdir -p /root/workspace" in cmd for cmd in fake.commands)
    # The start command dials back to the configured server URL.
    start = fake.host_starts[0]
    assert "--server https://srv.example.com" in start.command
    assert result.host_id == start.host_id
    # The hosts row carries the managed binding with full content; the
    # provider comes from the LAUNCHER (not config), so injected custom
    # launchers record their own name.
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.user_id == _OWNER
    assert host.name == start.host_name
    assert host.status == "online"
    assert host.sandbox_provider == "docker"
    assert host.sandbox_id == "sb-fake-1"
    # The token injected into the sandbox is the one whose digest was
    # stored: resolving it (the tunnel's auth path) yields this host,
    # which also proves it is unexpired.
    resolved = host_store.resolve_launch_token(start.host_id, start.token)
    assert resolved is not None
    assert resolved.host_id == result.host_id
    # Nothing was torn down on the success path.
    assert fake.terminated == []


async def test_launch_threads_agent_name_only_to_a_classifying_launcher(db_uri: str) -> None:
    """A launcher that DECLARES ``classifies_runner_by_agent`` receives the
    resolved agent name at ``start_host``, so its runner Pod is stamped."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = _ClassifyingFakeSandboxLauncher(on_host_start=_register)
    await launch_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        agent_name="research-agent",
    )
    assert fake.agent_names == ["research-agent"]


async def test_launch_never_passes_agent_name_to_a_non_classifying_launcher(db_uri: str) -> None:
    """
    An exec-model launcher does not declare the capability, so the launch path
    NEVER threads ``agent_name`` into its ``start_host`` — even when a name
    resolved. The gate is on the capability, not on the value; the exec-model
    ``start_host`` has no such keyword, so a launch that reached it would raise
    ``TypeError`` and 502. The launch succeeding proves the keyword was omitted.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    result = await launch_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        agent_name="research-agent",
    )
    # The host started normally through the exec-model path (no TypeError).
    assert len(fake.host_starts) == 1
    assert host_store.get_host(result.host_id) is not None


async def test_relaunch_threads_agent_name_to_classifying_start_host(db_uri: str) -> None:
    """
    A relaunch re-stamps the runner: the agent name reaches the fresh
    generation's ``start_host``, so a reused runner keeps its classifier and the
    admission policy keeps injecting the credential.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = _ClassifyingFakeSandboxLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    gen1 = host_store.get_host(first.host_id)
    assert gen1 is not None

    await relaunch_host(
        config=config, host=gen1, host_store=host_store, agent_name="code-reviewer"
    )
    # First launch had no agent; the relaunch carried it through.
    assert fake.agent_names == [None, "code-reviewer"]


async def test_launch_materializes_host_config_before_host_start(db_uri: str) -> None:
    """
    A configured host_config is written into the sandbox strictly BEFORE
    ``omnigent host`` starts — the whole point of the injection is that the
    host boots with its providers already on disk.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
            managed_token=invocation.token,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    host_config: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}

    await launch_host(
        config=_injected_config(fake, host_config=host_config),
        owner=_OWNER,
        host_store=host_store,
    )

    write_index = fake.commands.index(render_host_config_write_command(host_config))
    host_index = next(i for i, cmd in enumerate(fake.commands) if "omnigent host --server" in cmd)
    assert write_index < host_index


async def test_resume_rematerializes_host_config_before_host_restart(db_uri: str) -> None:
    """
    Waking a dormant sandbox re-runs the config write before re-execing the
    host — resume_host bypasses _register_and_start_host, so this is a
    distinct wiring point, and re-materializing is what lets an operator's
    host_config change land on the next wake without a new sandbox.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register, can_resume=True)
    host_config: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}
    config = _injected_config(fake, host_config=host_config)

    result = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(result.host_id)
    commands_before = len(fake.commands)

    await resume_host(result.host_id, host_store, config)

    assert fake.resumed == ["sb-fake-1"]
    resumed_commands = fake.commands[commands_before:]
    write_index = resumed_commands.index(render_host_config_write_command(host_config))
    host_index = next(
        i for i, cmd in enumerate(resumed_commands) if "omnigent host --server" in cmd
    )
    assert write_index < host_index


async def test_launch_without_host_config_writes_no_config(db_uri: str) -> None:
    """No host_config → the launch issues no config-write command at all."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)

    assert not any(cmd.startswith("python3 -c") for cmd in fake.commands)


async def test_launch_and_resume_without_optional_kwargs_support_legacy_start_host_signature(
    db_uri: str,
) -> None:
    """
    A deployment-injected launcher whose ``start_host`` override predates the
    optional ``host_config`` and ``on_stage`` parameters keeps launching and
    resuming when neither value is set.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    class _LegacySignatureLauncher(FakeSandboxLauncher):
        """Overrides start_host with the pre-host_config explicit signature."""

        def start_host(
            self,
            sandbox_id: str,
            *,
            token: str,
            host_id: str,
            host_name: str,
            server_url: str,
            repos: Sequence[RepoWorkspace] = (),
        ) -> str:
            return super().start_host(
                sandbox_id,
                token=token,
                host_id=host_id,
                host_name=host_name,
                server_url=server_url,
                repos=repos,
            )

    fake = _LegacySignatureLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)

    result = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(result.host_id)
    await resume_host(result.host_id, host_store, config)

    assert [start.host_id for start in fake.host_starts] == [result.host_id, result.host_id]
    assert fake.resumed == ["sb-fake-1"]


async def test_launch_with_injected_custom_launcher(db_uri: str) -> None:
    """
    The embedding seam end to end: a deployment-defined launcher (a
    provider name the YAML path doesn't even know) drives the whole
    managed flow, and its provider is what lands on the host row — so
    teardown later dispatches back to the same custom launcher.
    """
    host_store = HostStore(db_uri)

    class _AcmeLauncher(FakeSandboxLauncher):
        """Custom launcher under a deployment-private provider name."""

        provider: ClassVar[str] = "acme-cloud"

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _AcmeLauncher(on_host_start=_register)
    config = _injected_config(fake)

    result = await launch_host(config=config, owner=_OWNER, host_store=host_store)

    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.sandbox_provider == "acme-cloud"
    assert host.sandbox_id == "sb-fake-1"

    # Teardown resolves the launcher through the same config factory
    # (provider matches the row) — the custom launcher's terminate runs.
    await terminate_host(host, host_store, config)
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.get_host(result.host_id) is None


async def test_launch_provision_failure_maps_to_502(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provider failure before anything exists (preflight) maps to a
    502 with the provider's message, and leaves no host row and
    nothing to terminate.
    """
    fake = FakeSandboxLauncher()

    def _fail_prepare() -> None:
        """Simulate missing provider credentials."""
        raise click.ClickException("No sandbox credentials found.")

    monkeypatch.setattr(fake, "prepare", _fail_prepare)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 502
    assert "No sandbox credentials found." in exc.value.detail
    assert host_store.list_hosts(_OWNER) == []
    assert fake.terminated == []


async def test_launch_host_start_failure_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A failure AFTER provisioning must clean up: terminate the sandbox
    (no orphaned paid compute) and delete the pre-registered host row
    (the minted token must not stay valid, and a never-started host
    must not linger in the picker).
    """
    fake = FakeSandboxLauncher(fail_on_host_start=True)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 502
    assert "simulated in-sandbox host start failure" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []


async def test_launch_non_click_exception_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A raw (non-Click, non-HTTP) exception during host start — a
    provider SDK error or a network failure from the in-sandbox exec —
    must trigger the same cleanup: terminate the sandbox and delete the
    host row. If the cleanup handler only caught ClickException, the
    sandbox would leak running until the provider's lifetime cap and
    the armed token would stay resolvable.
    """

    def _raise_sdk_error(invocation: HostStartInvocation) -> None:
        raise RuntimeError("simulated provider SDK failure")

    fake = FakeSandboxLauncher(on_host_start=_raise_sdk_error)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 502
    assert "simulated provider SDK failure" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []


async def test_launch_online_timeout_terminates_and_deletes_host(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A host that never registers (e.g. bad image, can't reach the
    server) times out with a 502 pointing at the in-sandbox log, and
    cleans up the sandbox + host row (which revokes the token).
    """
    # No on_host_start → the host never registers.
    fake = FakeSandboxLauncher()
    # Shrink the polling budget so the timeout path runs in
    # milliseconds; production values are module constants read at
    # call time.
    monkeypatch.setattr("omnigent.server.hosts.HOST_ONLINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr("omnigent.server.hosts._ONLINE_POLL_INTERVAL_S", 0.01)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 502
    assert "did not come online" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []
    # The start command DID run (the failure was registration, not
    # startup), so its minted token exists — and must be dead.
    assert (
        host_store.resolve_launch_token(fake.host_starts[0].host_id, fake.host_starts[0].token)
        is None
    )


async def test_launch_with_repo_clones_into_workspace(db_uri: str) -> None:
    """
    A repository-URL workspace is cloned inside the sandbox BEFORE the
    host starts, and the cloned directory (not the bare workspace root)
    is what the session binds as its workspace.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    result = await launch_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        repos=[parse_repo_workspace("https://github.com/org/myrepo.git#release-1.2")],
    )

    # The session workspace is the clone directory, named after the repo.
    assert result.workspace == "/root/workspace/myrepo"
    # The exact clone invocation: branch-pinned, single-branch, `--`
    # separating options from the user-supplied URL. A drift here means
    # the sandbox clones the wrong thing (or interprets the URL as a
    # flag).
    clone_cmd = (
        "git clone --branch release-1.2 --single-branch "
        "-- https://github.com/org/myrepo.git /root/workspace/myrepo"
    )
    assert clone_cmd in fake.commands
    # Clone runs before the host starts — the workspace must be ready
    # by the time the runner can launch on the registered host.
    host_start_index = next(i for i, c in enumerate(fake.commands) if "omnigent host" in c)
    assert fake.commands.index(clone_cmd) < host_start_index
    assert fake.terminated == []


async def test_launch_clone_failure_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A failed clone (bad URL, missing branch, private repo) cleans up
    exactly like a host-start failure — sandbox terminated, host row
    (and its token) deleted — and the 502 names the repository so the
    create error tells the user WHAT didn't clone.
    """
    fake = FakeSandboxLauncher(fail_on_command="git clone")
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_host(
            config=_injected_config(fake),
            owner=_OWNER,
            host_store=host_store,
            repos=[parse_repo_workspace("https://github.com/org/private#main")],
        )
    assert exc.value.status_code == 502
    assert "failed to clone repository 'https://github.com/org/private'" in exc.value.detail
    assert "'main'" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []
    # The host never started — the clone failed first.
    assert fake.host_starts == []


class _EntrypointFakeLauncher(FakeSandboxLauncher):
    """
    An entrypoint-as-host fake: ``provision``
    only RESERVES the sandbox id (no box created), and the host is started by a
    ``start_host`` override — not the exec-model base default.

    Records the ``start_host`` call and, to prove the token is armed BEFORE the
    host starts, captures whether the token already resolves at call time (then
    simulates the host dialing back).
    """

    provider: ClassVar[str] = "entrypoint"

    def __init__(self, host_store: HostStore) -> None:
        super().__init__()
        self._host_store = host_store
        self.start_calls: list[dict[str, object]] = []
        self.token_resolved_at_start: bool = False

    def provision(self, name: str) -> str:
        """Reserve a sandbox id (no box created); recorded + deterministic."""
        self.provisioned_names.append(name)
        return f"omnigent-pod-{len(self.provisioned_names)}"

    def run(self, sandbox_id: str, command: str, *, check: bool = True):
        """The entrypoint model never execs in — the base default is overridden."""
        raise AssertionError("entrypoint launcher must not exec via run()")

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
        on_stage=None,
    ) -> str:
        """Record the call, prove the token already resolves, and connect."""
        self.start_calls.append(
            {
                "sandbox_id": sandbox_id,
                "token": token,
                "host_id": host_id,
                "server_url": server_url,
                "repos": list(repos),
            }
        )
        # The token was registered before start_host, so it resolves now.
        self.token_resolved_at_start = (
            self._host_store.resolve_launch_token(host_id, token) is not None
        )
        # Simulate the host's entrypoint dialing back over the tunnel.
        self._host_store.upsert_on_connect(host_id=host_id, name=host_name, user_id=_OWNER)
        # One repo → its clone dir; none or several → the workspace parent.
        return (
            f"/home/omnigent/workspace/{repos[0].repo_name}"
            if len(repos) == 1
            else "/home/omnigent/workspace"
        )


async def test_launch_entrypoint_provider_arms_token_before_launch_host(db_uri: str) -> None:
    """
    Entrypoint-as-host seam: the uniform launch path reserves the sandbox id via
    provision(), registers the token, THEN calls start_host (never run) — so the
    host authenticates the moment its entrypoint dials back, with no race.
    """
    host_store = HostStore(db_uri)
    fake = _EntrypointFakeLauncher(host_store)

    result = await launch_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        repos=[parse_repo_workspace("https://github.com/org/repo.git#main")],
    )

    # start_host ran once, with the reserved id and repo info.
    assert len(fake.start_calls) == 1
    call = fake.start_calls[0]
    assert call["sandbox_id"] == "omnigent-pod-1"
    assert call["server_url"] == "https://srv.example.com"
    assert [r.url for r in call["repos"]] == ["https://github.com/org/repo.git"]
    assert [r.repo_name for r in call["repos"]] == ["repo"]
    # The token was already resolvable when start_host ran (no dial-back race).
    assert fake.token_resolved_at_start is True
    # The workspace (cloned dir) is returned and the host is online + bound.
    assert result.workspace == "/home/omnigent/workspace/repo"
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.status == "online"
    assert host.sandbox_provider == "entrypoint"
    assert host.sandbox_id == "omnigent-pod-1"


async def test_launch_entrypoint_provider_cleans_up_on_launch_failure(db_uri: str) -> None:
    """
    A start_host failure tears the sandbox down (by the reserved id) and deletes
    the host row, exactly like the exec path.
    """
    host_store = HostStore(db_uri)

    class _Failing(_EntrypointFakeLauncher):
        def start_host(self, sandbox_id: str, **kwargs: object) -> str:
            raise click.ClickException("pod could not be scheduled")

    fake = _Failing(host_store)
    with pytest.raises(HTTPException) as exc:
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 502
    assert "pod could not be scheduled" in exc.value.detail
    # The reserved sandbox was terminated and no host row survives.
    assert fake.terminated == ["omnigent-pod-1"]
    assert host_store.list_hosts(_OWNER) == []


# ── relaunch_host ───────────────────────────────────


class _ReusedIdFakeLauncher(FakeSandboxLauncher):
    provider: ClassVar[str] = "reused-id"

    def provision(self, name: str) -> str:
        self.provisioned_names.append(name)
        return name


async def test_relaunch_rejects_pending_reused_id_then_allows_retry(db_uri: str) -> None:
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _ReusedIdFakeLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    active = host_store.get_host(first.host_id)
    assert active is not None
    assert active.sandbox_id is not None
    assert host_store.detach_stale_managed_sandbox(
        active.host_id,
        sandbox_id=active.sandbox_id,
        expected_updated_at=active.updated_at,
    )
    pending = host_store.get_host(active.host_id)
    assert pending is not None

    with pytest.raises(HTTPException) as exc:
        await relaunch_host(config=config, host=pending, host_store=host_store)

    assert exc.value.status_code == 409
    assert "host lifecycle" in exc.value.detail
    assert len(fake.host_starts) == 1
    assert fake.terminated == []
    still_pending = host_store.get_host(active.host_id)
    assert still_pending is not None
    assert still_pending.sandbox_id is None
    assert still_pending.terminating_sandbox_id == active.name

    reaper = ManagedSandboxReaper(host_store=host_store, sandbox_config=config)
    assert await reaper.sweep_once() == 1
    assert fake.terminated == [active.name]
    cleared = host_store.get_host(active.host_id)
    assert cleared is not None
    assert cleared.sandbox_id is None
    assert cleared.terminating_sandbox_id is None

    relaunched = await relaunch_host(config=config, host=cleared, host_store=host_store)
    assert relaunched.host_id == active.host_id
    current = host_store.get_host(active.host_id)
    assert current is not None
    assert current.status == "online"
    assert current.sandbox_id == active.name
    assert current.terminating_sandbox_id is None


async def test_relaunch_rolls_sandbox_generation_under_same_host(db_uri: str) -> None:
    """
    A relaunch terminates the dead generation, provisions a fresh
    sandbox, and re-arms the SAME host row: identity (host_id, name,
    owner) stable, sandbox id rolled, and the NEW token resolving
    while the old one no longer does — a stale token resolving would
    let a dead sandbox's leaked credential impersonate the new host.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    gen1 = host_store.get_host(first.host_id)
    assert gen1 is not None
    gen1_token = fake.host_starts[0].token

    relaunched = await relaunch_host(config=config, host=gen1, host_store=host_store)

    # Same identity, new generation: the session's host binding (which
    # references host_id) survives the roll.
    assert relaunched.host_id == first.host_id
    assert relaunched.workspace == "/root/workspace"
    assert fake.terminated == ["sb-fake-1"]
    host = host_store.get_host(first.host_id)
    assert host is not None
    assert host.sandbox_id == "sb-fake-2"
    assert host.name == gen1.name
    assert host.user_id == _OWNER
    # Generation 2 authenticated with a NEW token; generation 1's is
    # revoked by the re-arm (its digest no longer matches anything).
    gen2_token = fake.host_starts[1].token
    assert gen2_token != gen1_token
    resolved = host_store.resolve_launch_token(fake.host_starts[1].host_id, gen2_token)
    assert resolved is not None and resolved.host_id == first.host_id
    assert host_store.resolve_launch_token(fake.host_starts[0].host_id, gen1_token) is None


async def test_relaunch_failure_keeps_host_row_and_revokes_token(db_uri: str) -> None:
    """
    A FAILED relaunch must not delete the durable host row — deleting
    it would null the session's host binding (FK SET NULL) and make
    the session permanently unrelaunchable. The new sandbox is torn
    down and the armed token revoked, so nothing of the failed
    generation stays live; a later message retries against the kept
    row.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    gen1 = host_store.get_host(first.host_id)
    assert gen1 is not None

    fake.fail_on_host_start = True
    with pytest.raises(HTTPException) as exc:
        await relaunch_host(config=config, host=gen1, host_store=host_store)

    assert exc.value.status_code == 502
    # Both the dead generation 1 and the failed generation 2 sandboxes
    # were terminated — nothing leaks until the provider lifetime cap.
    assert fake.terminated == ["sb-fake-1", "sb-fake-2"]
    # The row SURVIVES the failure (contrast the first-launch failure
    # tests, which delete it), so the session binding stays relaunchable.
    host = host_store.get_host(first.host_id)
    assert host is not None
    # No credential of ANY generation is live: gen 1's was replaced by
    # the re-arm, and the re-armed token was revoked by the failure
    # cleanup (revoke_launch_token — covered directly in the host-store
    # suite). Gen 1's raw token is the only one observable here (the
    # failed start never executed), so assert on it.
    assert (
        host_store.resolve_launch_token(fake.host_starts[0].host_id, fake.host_starts[0].token)
        is None
    )


async def test_relaunch_does_not_recreate_deleted_host(db_uri: str) -> None:
    """A relaunch that loses to full teardown cleans up its new sandbox."""
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="c73ad22fd7be4fe2a62a36b7e0fbcaa8",
        name="managed-deleted-relaunch",
        user_id=_OWNER,
        token="deleted-relaunch-token",
        provider="docker",
        sandbox_id="sb-deleted-old",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.delete_host(host.host_id)
    fake = FakeSandboxLauncher()

    with pytest.raises(HTTPException) as exc:
        await relaunch_host(
            config=_injected_config(fake),
            host=host,
            host_store=host_store,
        )

    assert exc.value.status_code == 409
    assert "no longer exists" in exc.value.detail
    assert fake.terminated == ["sb-deleted-old", "sb-fake-1"]
    assert host_store.get_host(host.host_id) is None


async def test_relaunch_rejects_unconfigured_provider(db_uri: str) -> None:
    """
    A provider mismatch (the ``hosts:`` config changed since launch)
    fails the relaunch with a clear 400 instead of aiming another
    provider's terminate/provision at the recorded sandbox id.
    """
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="8369cb15e751573a1ee641d5fa09c70a",
        name="managed-mismatch",
        user_id=_OWNER,
        token="tok",
        provider="retired",
        sandbox_id="dt-1",
        token_expires_at=now_epoch() + 3600,
    )

    fake = FakeSandboxLauncher()  # provider "docker" != row's "retired"
    with pytest.raises(HTTPException) as exc:
        await relaunch_host(config=_injected_config(fake), host=host, host_store=host_store)

    assert exc.value.status_code == 400
    assert "retired" in exc.value.detail
    # Nothing was provisioned or terminated against the mismatched row.
    assert fake.provisioned_names == []
    assert fake.terminated == []


# ── resume_host ─────────────────────────────────────


class _ResumableFakeLauncher(FakeSandboxLauncher):
    """Fake launcher carrying a resumable provider label for managed resume tests."""

    provider: ClassVar[str] = "resumable"


async def test_host_resume_supported_requires_resumable_matching_launcher(db_uri: str) -> None:
    """The wake gate requires matching provider, sandbox id, and ``can_resume``."""
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="292a6322075a34e482fde44975da10f3",
        name="managed-resume-gate",
        user_id=_OWNER,
        token="tok-resume-gate",
        provider="resumable",
        sandbox_id="sb-resume-gate",
        token_expires_at=now_epoch() + 3600,
    )

    resumable = _ResumableFakeLauncher(can_resume=True)
    assert host_resume_supported(host, _injected_config(resumable)) is True

    non_resumable = _ResumableFakeLauncher(can_resume=False)
    assert host_resume_supported(host, _injected_config(non_resumable)) is False

    mismatched = FakeSandboxLauncher(can_resume=True)  # provider "docker"
    assert host_resume_supported(host, _injected_config(mismatched)) is False

    no_sandbox = host_store.register_managed_host(
        host_id="0c3d744a455047df9a3c0acf432d08dd",
        name="managed-resume-no-sandbox",
        user_id=_OWNER,
        token="tok-resume-no-sandbox",
        provider="resumable",
        sandbox_id="sb-temp",
        token_expires_at=now_epoch() + 3600,
    )
    no_sandbox.sandbox_id = None
    assert host_resume_supported(no_sandbox, _injected_config(resumable)) is False


async def test_resume_managed_host_wakes_same_sandbox_and_refreshes_token(db_uri: str) -> None:
    """A resumable managed host wakes in place under the same sandbox id."""
    host_store = HostStore(db_uri)

    class _WorkspaceObservingResumableFake(_ResumableFakeLauncher):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.started_workspaces: list[str] = []

        def start_host(self, *args: Any, **kwargs: Any) -> str:
            workspace = super().start_host(*args, **kwargs)
            self.started_workspaces.append(workspace)
            return workspace

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host reconnecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _WorkspaceObservingResumableFake(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host = host_store.get_host(first.host_id)
    assert host is not None
    assert host.sandbox_provider == "resumable"
    assert host.sandbox_id == "sb-fake-1"
    first_token = fake.host_starts[0].token

    host_store.set_offline(first.host_id)
    assert host_resume_supported(host_store.get_host(first.host_id), config) is True

    await resume_host(first.host_id, host_store, config)

    assert fake.resumed == ["sb-fake-1"]
    assert fake.started_workspaces == ["/root/workspace", "/root/workspace"]
    assert len(fake.provisioned_names) == 1
    woke = host_store.get_host(first.host_id)
    assert woke is not None
    assert woke.status == "online"
    assert woke.sandbox_provider == "resumable"
    assert woke.sandbox_id == "sb-fake-1"
    second_token = fake.host_starts[1].token
    assert second_token != first_token
    assert host_store.resolve_launch_token(fake.host_starts[0].host_id, first_token) is None
    resolved = host_store.resolve_launch_token(fake.host_starts[1].host_id, second_token)
    assert resolved is not None and resolved.host_id == first.host_id


async def test_resume_managed_host_forwards_on_stage(db_uri: str) -> None:
    """A wake reports launch-pipeline stages through ``on_stage``.

    Parity with the fresh-launch path (``_register_and_start_host``): base
    ``start_host`` emits ``"starting"`` before it execs the host, so a wake
    with an observer must surface at least that stage rather than leaving the
    caller on a single frozen ``"provisioning"`` band for the whole resume.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _ResumableFakeLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(first.host_id)

    stages: list[str] = []
    await resume_host(first.host_id, host_store, config, on_stage=stages.append)

    assert fake.resumed == ["sb-fake-1"]
    assert "starting" in stages


async def test_resume_threads_agent_name_to_classifying_start_host(db_uri: str) -> None:
    """
    A wake re-stamps the runner's classifier.

    The resume rebuilds the runner from scratch under the same sandbox id, so a
    classifier the first launch stamped is NOT carried over by the provider. If
    the wake omits it, the woken runner comes back unclassified, the admission
    policy that selects on the label stops matching, and the runner silently
    loses the credential the label exists to attract.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = _ClassifyingFakeSandboxLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_host(
        config=config, owner=_OWNER, host_store=host_store, agent_name="code-reviewer"
    )
    host_store.set_offline(first.host_id)

    await resume_host(first.host_id, host_store, config, agent_name="code-reviewer")

    assert fake.resumed == ["sb-fake-1"]
    assert fake.agent_names == ["code-reviewer", "code-reviewer"]


async def test_resume_never_passes_agent_name_to_a_non_classifying_launcher(
    db_uri: str,
) -> None:
    """
    The wake gates on the capability, not on the value.

    An exec-model launcher has no ``agent_name`` keyword, so a wake that reached
    it with one would raise ``TypeError`` and 502. The wake succeeding proves the
    keyword was omitted.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = _ResumableFakeLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(first.host_id)

    await resume_host(first.host_id, host_store, config, agent_name="code-reviewer")

    assert fake.resumed == ["sb-fake-1"]
    assert host_store.get_host(first.host_id) is not None


async def test_resume_without_a_classifier_leaves_the_woken_runner_unstamped(
    db_uri: str,
) -> None:
    """No resolved name means the classifying launcher is told nothing to stamp."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id=_OWNER
        )

    fake = _ClassifyingFakeSandboxLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(first.host_id)

    await resume_host(first.host_id, host_store, config)

    assert fake.agent_names == [None, None]


async def test_resume_managed_host_force_wakes_fresh_online_row(db_uri: str) -> None:
    """A local missing-tunnel wake can bypass stale cross-replica DB freshness."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
            managed_token=invocation.token,
        )

    host_store.register_managed_host(
        host_id="62d4405ba38711fe34bebfeb5a7adaf2",
        name="managed-resume-force",
        user_id=_OWNER,
        token="tok-resume-force",
        provider="resumable",
        sandbox_id="sb-resume-force",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.upsert_on_connect(
        host_id="62d4405ba38711fe34bebfeb5a7adaf2",
        name="managed-resume-force",
        user_id=_OWNER,
    )
    assert host_store.is_online("62d4405ba38711fe34bebfeb5a7adaf2") is True
    fake = _ResumableFakeLauncher(on_host_start=_register, can_resume=True)

    await resume_host(
        "62d4405ba38711fe34bebfeb5a7adaf2", host_store, _injected_config(fake), force=True
    )

    assert fake.resumed == ["sb-resume-force"]
    assert len(fake.host_starts) == 1
    assert (
        host_store.resolve_launch_token("62d4405ba38711fe34bebfeb5a7adaf2", "tok-resume-force")
        is None
    )
    resolved = host_store.resolve_launch_token(
        "62d4405ba38711fe34bebfeb5a7adaf2", fake.host_starts[0].token
    )
    assert resolved is not None and resolved.host_id == "62d4405ba38711fe34bebfeb5a7adaf2"


async def test_resume_managed_host_noops_for_non_resumable_provider(db_uri: str) -> None:
    """Non-resumable providers fall through without mutating the host row."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id="249d058fbcde7b2ce941479cdb8c82d7",
        name="managed-resume-noop",
        user_id=_OWNER,
        token="tok-resume-noop",
        provider="docker",
        sandbox_id="sb-resume-noop",
        token_expires_at=now_epoch() + 3600,
    )
    fake = FakeSandboxLauncher(can_resume=False)

    await resume_host("249d058fbcde7b2ce941479cdb8c82d7", host_store, _injected_config(fake))

    assert fake.resumed == []
    assert fake.host_starts == []
    host = host_store.get_host("249d058fbcde7b2ce941479cdb8c82d7")
    assert host is not None
    assert host.status == "offline"
    assert host.sandbox_id == "sb-resume-noop"
    assert (
        host_store.resolve_launch_token("249d058fbcde7b2ce941479cdb8c82d7", "tok-resume-noop")
        is not None
    )


async def test_resume_managed_host_failure_preserves_existing_row_and_token(db_uri: str) -> None:
    """A failed provider resume leaves the dormant generation unchanged."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id="efbef7dede7be6577770cbb1287992f2",
        name="managed-resume-fail",
        user_id=_OWNER,
        token="tok-resume-fail",
        provider="resumable",
        sandbox_id="sb-resume-fail",
        token_expires_at=now_epoch() + 3600,
    )
    fake = _ResumableFakeLauncher(can_resume=True, fail_on_resume=True)

    with pytest.raises(HTTPException) as exc:
        await resume_host("efbef7dede7be6577770cbb1287992f2", host_store, _injected_config(fake))

    assert exc.value.status_code == 502
    assert "managed host wake failed" in exc.value.detail
    assert fake.host_starts == []
    host = host_store.get_host("efbef7dede7be6577770cbb1287992f2")
    assert host is not None
    assert host.status == "offline"
    assert host.sandbox_id == "sb-resume-fail"
    assert (
        host_store.resolve_launch_token("efbef7dede7be6577770cbb1287992f2", "tok-resume-fail")
        is not None
    )


async def test_resume_managed_host_propagates_gone_and_preserves_existing_row(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Definitive sandbox loss is delegated without deleting the host identity."""
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="f359cf76dbd54c8e97568b436688292b",
        name="managed-resume-gone",
        user_id=_OWNER,
        token="tok-resume-gone",
        provider="resumable",
        sandbox_id="sb-resume-gone",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.set_offline(host.host_id)
    fake = _ResumableFakeLauncher(can_resume=True)

    def _gone(_sandbox_id: str) -> None:
        raise HostGoneError("sandbox no longer exists")

    monkeypatch.setattr(fake, "resume", _gone)

    with pytest.raises(HostGoneError, match="no longer exists"):
        await resume_host(host.host_id, host_store, _injected_config(fake))

    preserved = host_store.get_host(host.host_id)
    assert preserved is not None
    assert preserved.status == "offline"
    assert preserved.sandbox_id == "sb-resume-gone"
    assert host_store.resolve_launch_token(host.host_id, "tok-resume-gone") is not None
    assert fake.host_starts == []


async def test_resume_managed_host_does_not_resume_detached_generation(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaper detach that wins the CAS cleans up the resumed stale generation."""
    host_store = HostStore(db_uri)
    host_id = "ade43acaaaf44af59388dc2109a8557f"
    host_store.register_managed_host(
        host_id=host_id,
        name="managed-resume-race",
        user_id=_OWNER,
        token="tok-resume-race",
        provider="resumable",
        sandbox_id="sb-resume-race",
        token_expires_at=now_epoch() + 3600,
    )
    original_rearm = host_store.rearm_managed_host

    def _detach_before_rearm(
        requested_host_id: str,
        *,
        sandbox_id: str,
        expected_updated_at: int,
        token: str,
        token_expires_at: int,
    ) -> Host | None:
        assert host_store.detach_stale_managed_sandbox(
            requested_host_id,
            sandbox_id=sandbox_id,
            expected_updated_at=expected_updated_at,
        )
        return original_rearm(
            requested_host_id,
            sandbox_id=sandbox_id,
            expected_updated_at=expected_updated_at,
            token=token,
            token_expires_at=token_expires_at,
        )

    monkeypatch.setattr(host_store, "rearm_managed_host", _detach_before_rearm)
    fake = _ResumableFakeLauncher(can_resume=True)

    await resume_host(host_id, host_store, _injected_config(fake))

    assert fake.resumed == ["sb-resume-race"]
    assert fake.terminated == ["sb-resume-race"]
    detached = host_store.get_host(host_id)
    assert detached is not None
    assert detached.sandbox_id is None
    assert detached.terminating_sandbox_id is None


async def test_resume_managed_host_does_not_recreate_deleted_host(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake that loses to full teardown cleans up the resumed sandbox."""
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="463013d3d38d4096b2404962c48479b1",
        name="managed-deleted-resume",
        user_id=_OWNER,
        token="deleted-resume-token",
        provider="resumable",
        sandbox_id="sb-deleted-resume",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.set_offline(host.host_id)
    fake = _ResumableFakeLauncher(can_resume=True)

    def _resume(sandbox_id: str) -> None:
        fake.resumed.append(sandbox_id)
        host_store.delete_host(host.host_id)

    monkeypatch.setattr(fake, "resume", _resume)

    with pytest.raises(HTTPException) as exc:
        await resume_host(host.host_id, host_store, _injected_config(fake))

    assert exc.value.status_code == 502
    assert "no longer exists" in exc.value.detail
    assert fake.terminated == ["sb-deleted-resume"]
    assert fake.host_starts == []
    assert host_store.get_host(host.host_id) is None


# ── terminate_host ──────────────────────────────────


async def test_terminate_managed_host_terminates_and_deletes_row(db_uri: str) -> None:
    """
    Cleanup terminates the provider sandbox and deletes the host row —
    one operation that removes the host from the picker AND revokes
    its launch token.
    """
    fake = FakeSandboxLauncher()
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="62a91eb065624754c6a6dfb5869dd7e8",
        name="managed-term1",
        user_id=_OWNER,
        token="tok-term-1",
        provider="docker",
        sandbox_id="sb-term-1",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_host(host, host_store, _injected_config(fake))

    assert fake.terminated == ["sb-term-1"]
    assert host_store.get_host("62a91eb065624754c6a6dfb5869dd7e8") is None
    assert (
        host_store.resolve_launch_token("62a91eb065624754c6a6dfb5869dd7e8", "tok-term-1") is None
    )


async def test_terminate_managed_host_cleans_active_and_pending_generations(db_uri: str) -> None:
    """Full host teardown best-effort terminates both persisted sandbox ids."""
    fake = FakeSandboxLauncher()
    host_store = HostStore(db_uri)
    host_id = "b63810fdd86a4f04988b3e53a930ad08"
    original = host_store.register_managed_host(
        host_id=host_id,
        name="managed-term-both",
        user_id=_OWNER,
        token="tok-term-old",
        provider="docker",
        sandbox_id="sb-term-old",
        token_expires_at=now_epoch() + 3600,
    )
    assert host_store.detach_stale_managed_sandbox(
        host_id,
        sandbox_id="sb-term-old",
        expected_updated_at=original.updated_at,
    )
    replaced = host_store.replace_managed_host_sandbox(
        host_id=host_id,
        user_id=_OWNER,
        token="tok-term-new",
        provider="docker",
        sandbox_id="sb-term-new",
        token_expires_at=now_epoch() + 3600,
    )
    assert replaced is not None
    host = host_store.get_host(host_id)
    assert host is not None

    await terminate_host(host, host_store, _injected_config(fake))

    assert fake.terminated == ["sb-term-new", "sb-term-old"]
    assert host_store.get_host(host_id) is None


async def test_terminate_managed_host_targets_latest_registered_generation(db_uri: str) -> None:
    """A stale teardown snapshot removes and terminates the current generation."""
    fake = FakeSandboxLauncher()
    host_store = HostStore(db_uri)
    stale = host_store.register_managed_host(
        host_id="5233b41530ed474f9860ecf3acfc9131",
        name="managed-term-current",
        user_id=_OWNER,
        token="tok-term-old",
        provider="docker",
        sandbox_id="sb-term-old",
        token_expires_at=now_epoch() + 3600,
    )
    updated = host_store.replace_managed_host_sandbox(
        host_id=stale.host_id,
        user_id=stale.user_id,
        token="tok-term-new",
        provider="docker",
        sandbox_id="sb-term-new",
        token_expires_at=now_epoch() + 3600,
    )
    assert updated is not None

    await terminate_host(stale, host_store, _injected_config(fake))

    assert fake.terminated == ["sb-term-new"]
    assert host_store.get_host(stale.host_id) is None


async def test_terminate_managed_host_deletes_row_before_provider_call(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider teardown runs only after the host can no longer be relaunched."""
    fake = FakeSandboxLauncher()
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="af48b60a7afb4c0696709403116c30c6",
        name="managed-delete-first",
        user_id=_OWNER,
        token="tok-delete-first",
        provider="docker",
        sandbox_id="sb-delete-first",
        token_expires_at=now_epoch() + 3600,
    )

    def _terminate(sandbox_id: str) -> None:
        assert host_store.get_host(host.host_id) is None
        fake.terminated.append(sandbox_id)

    monkeypatch.setattr(fake, "terminate", _terminate)

    await terminate_host(host, host_store, _injected_config(fake))

    assert fake.terminated == ["sb-delete-first"]


async def test_terminate_managed_host_retries_tombstone_after_terminate_fails(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provider failure does not block logical deletion, and the persisted
    sandbox id is retried by the existing reaper.
    """
    fake = FakeSandboxLauncher()
    original_terminate = fake.terminate

    def _explode(sandbox_id: str) -> None:
        """Simulate a provider API failure during termination."""
        raise click.ClickException("provider unavailable")

    monkeypatch.setattr(fake, "terminate", _explode)
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="057e7fa3f1cdb40c0ec393a3d42affc7",
        name="managed-term2",
        user_id=_OWNER,
        token="tok-term-2",
        provider="docker",
        sandbox_id="sb-term-2",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_host(host, host_store, _injected_config(fake))

    assert host_store.get_host("057e7fa3f1cdb40c0ec393a3d42affc7") is None
    assert (
        host_store.resolve_launch_token("057e7fa3f1cdb40c0ec393a3d42affc7", "tok-term-2") is None
    )
    tombstones = [
        host
        for _, host in host_store.list_current_managed_sandbox_hosts_page(
            after=None,
            limit=10,
        )
    ]
    assert len(tombstones) == 1
    assert tombstones[0].deleted_at is not None
    assert tombstones[0].sandbox_id == "sb-term-2"

    monkeypatch.setattr(fake, "terminate", original_terminate)
    reaper = ManagedSandboxReaper(
        host_store=host_store,
        sandbox_config=_injected_config(fake),
    )
    assert await reaper.sweep_once() == 1
    assert fake.terminated == ["sb-term-2"]
    assert host_store.list_current_managed_sandbox_hosts_page(after=None, limit=10) == []
    assert host_store.list_terminating_managed_sandbox_hosts_page(after=None, limit=10) == []


async def test_terminate_managed_host_retains_only_failed_generation(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSandboxLauncher()
    original_terminate = fake.terminate
    host_store = HostStore(db_uri)
    host_id = "9e50e018919e494886b984df0bbec12b"
    original = host_store.register_managed_host(
        host_id=host_id,
        name="managed-term-partial",
        user_id=_OWNER,
        token="tok-term-old-partial",
        provider="docker",
        sandbox_id="sb-term-old-partial",
        token_expires_at=now_epoch() + 3600,
    )
    assert host_store.detach_stale_managed_sandbox(
        host_id,
        sandbox_id="sb-term-old-partial",
        expected_updated_at=original.updated_at,
    )
    host = host_store.replace_managed_host_sandbox(
        host_id=host_id,
        user_id=_OWNER,
        token="tok-term-new-partial",
        provider="docker",
        sandbox_id="sb-term-new-partial",
        token_expires_at=now_epoch() + 3600,
    )
    assert host is not None
    attempts: list[str] = []

    def _terminate(sandbox_id: str) -> None:
        attempts.append(sandbox_id)
        if sandbox_id == "sb-term-new-partial":
            raise click.ClickException("provider unavailable")
        original_terminate(sandbox_id)

    monkeypatch.setattr(fake, "terminate", _terminate)
    await terminate_host(host, host_store, _injected_config(fake))

    assert attempts == ["sb-term-new-partial", "sb-term-old-partial"]
    tombstones = [
        host
        for _, host in host_store.list_current_managed_sandbox_hosts_page(
            after=None,
            limit=10,
        )
    ]
    assert len(tombstones) == 1
    assert tombstones[0].sandbox_id == "sb-term-new-partial"
    assert tombstones[0].terminating_sandbox_id is None

    monkeypatch.setattr(fake, "terminate", original_terminate)
    reaper = ManagedSandboxReaper(
        host_store=host_store,
        sandbox_config=_injected_config(fake),
    )
    assert await reaper.sweep_once() == 1
    assert fake.terminated == ["sb-term-old-partial", "sb-term-new-partial"]
    assert host_store.list_current_managed_sandbox_hosts_page(after=None, limit=10) == []
    assert host_store.list_terminating_managed_sandbox_hosts_page(after=None, limit=10) == []


async def test_terminate_managed_host_skips_mismatched_provider(db_uri: str) -> None:
    """
    A config change between launch and teardown must not aim the new provider's
    terminate at a stale sandbox id. The host becomes invisible and its cleanup
    tombstone remains available if that provider is configured again later.
    """
    fake = FakeSandboxLauncher()  # provider "docker"
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="487212fd2b157b6ab6a6d6d3ef06ce5b",
        name="managed-term3",
        user_id=_OWNER,
        token="tok-term-3",
        # Row launched under a provider the current config doesn't run.
        provider="acme-cloud",
        sandbox_id="sb-term-3",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_host(host, host_store, _injected_config(fake))
    # No cross-provider terminate was attempted.
    assert fake.terminated == []
    assert host_store.get_host("487212fd2b157b6ab6a6d6d3ef06ce5b") is None
    assert (
        host_store.resolve_launch_token("487212fd2b157b6ab6a6d6d3ef06ce5b", "tok-term-3") is None
    )

    # config=None behaves the same: host hidden, cleanup retained.
    host2 = host_store.register_managed_host(
        host_id="b114bf90a8fd155ce6007c3bb262aa79",
        name="managed-term4",
        user_id=_OWNER,
        token="tok-term-4",
        provider="docker",
        sandbox_id="sb-term-4",
        token_expires_at=now_epoch() + 3600,
    )
    await terminate_host(host2, host_store, None)
    assert host_store.get_host("b114bf90a8fd155ce6007c3bb262aa79") is None


# ── multi-provider sandbox config ──────────────────────────────────────


def test_parse_multi_provider_offers_every_provider(contributed_acme_provider: None) -> None:
    """
    A ``providers:`` list configures several providers side by side, each
    carrying its own provider name and TTL.
    """
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    assert config is not None
    assert config.default.provider == "docker"
    assert config.default.token_ttl_s == HOST_TOKEN_TTL_S
    assert config.managed_launch_supported is True
    assert config.launchable_providers() == ("docker", "acme")
    # Each entry carries its own provider name and TTL, not the first's.
    assert [entry.provider for entry in config.offered()] == ["docker", "acme"]
    assert config.offered()[1].token_ttl_s == 1234


def test_parse_single_provider_still_offers_itself() -> None:
    """
    The scalar shape wraps into a one-provider deployment that reads
    through the plural accessors.
    """
    config = parse_host_section({"provider": "docker", "server_url": "https://s.example.com"})
    assert config is not None
    entry = config.default
    assert config.offered() == (entry,)
    assert config.launchable_providers() == ("docker",)
    assert config.for_provider(None) is entry
    assert config.for_provider("docker") is entry
    assert config.for_provider("acme") is None


def test_deployment_rejects_empty_configs() -> None:
    """
    The 'never empty' invariant the accessors rely on is enforced at
    construction, so a direct constructor can't slip past the parser's
    non-empty check and later IndexError inside ``default``.
    """
    with pytest.raises(ValueError, match="at least one provider config"):
        HostDeployment(configs=())


def test_parse_multi_provider_shares_top_level_keys(contributed_acme_provider: None) -> None:
    """
    ``server_url`` / ``host_config`` are written once and ride into
    every entry.
    """
    host_config: dict[str, object] = {"telemetry": {"enabled": False}}
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "host_config": host_config,
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    assert config is not None
    for entry in config.offered():
        assert entry.server_url == "https://s.example.com"
        assert entry.host_config == host_config


def test_parse_multi_provider_validates_provider_blocks(
    contributed_acme_provider: None,
) -> None:
    """
    A per-provider block inside an entry validates as it does in the
    scalar shape: a malformed value fails startup, not a launch.
    """
    with pytest.raises(ValueError, match=re.escape("host.acme")):
        parse_host_section(
            {
                "server_url": "https://s.example.com",
                "providers": [{"provider": "docker"}, {"provider": "acme", "acme": "oops"}],
            }
        )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {
                "server_url": "https://s.example.com",
                "provider": "docker",
                "providers": [{"provider": "acme"}],
            },
            "not both",
        ),
        ({"server_url": "https://s.example.com", "providers": []}, "non-empty list"),
        ({"server_url": "https://s.example.com", "providers": "docker"}, "non-empty list"),
        ({"server_url": "https://s.example.com", "providers": ["docker"]}, "must be a mapping"),
        (
            {
                "server_url": "https://s.example.com",
                "providers": [{"provider": "docker"}, {"provider": "docker"}],
            },
            "more than once",
        ),
        (
            {
                "server_url": "https://s.example.com",
                "providers": [{"provider": "nope"}],
            },
            "must be one of",
        ),
    ],
)
def test_parse_multi_provider_invalid_fails_loud(raw: dict[str, object], message: str) -> None:
    """Malformed multi-provider config stops startup with the reason named."""
    with pytest.raises(ValueError, match=message):
        parse_host_section(raw)


async def test_launch_uses_requested_provider(db_uri: str) -> None:
    """
    A create naming a provider launches on that provider, and the choice
    lands on the host row so teardown dispatches back to it.
    """
    host_store = HostStore(db_uri)

    class _AlphaLauncher(FakeSandboxLauncher):
        provider: ClassVar[str] = "alpha"

    class _BetaLauncher(FakeSandboxLauncher):
        provider: ClassVar[str] = "beta"

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    alpha = _AlphaLauncher(on_host_start=_register)
    beta = _BetaLauncher(on_host_start=_register)
    config = HostDeployment(
        configs=(
            HostProviderConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda: alpha,
                token_ttl_s=3600,
                provider="alpha",
            ),
            HostProviderConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda: beta,
                token_ttl_s=7200,
                provider="beta",
            ),
        ),
    )

    result = await launch_host(config=config, owner=_OWNER, host_store=host_store, provider="beta")

    # Only the requested provider ran.
    assert beta.provisioned_names == ["managed-" + result.host_id[:8]]
    assert alpha.provisioned_names == []
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.sandbox_provider == "beta"
    # Resolved by the row, so the same provider's terminate runs.
    await terminate_host(host, host_store, config)
    assert beta.terminated == ["sb-fake-1"]
    assert alpha.terminated == []


async def test_launch_defaults_to_first_provider(db_uri: str) -> None:
    """
    A create naming no provider takes the first configured one.
    """
    host_store = HostStore(db_uri)

    class _AlphaLauncher(FakeSandboxLauncher):
        provider: ClassVar[str] = "alpha"

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    alpha = _AlphaLauncher(on_host_start=_register)
    config = HostDeployment(
        configs=(
            HostProviderConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda: alpha,
                token_ttl_s=3600,
                provider="alpha",
            ),
        ),
    )

    result = await launch_host(config=config, owner=_OWNER, host_store=host_store)
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.sandbox_provider == "alpha"


async def test_launch_unknown_provider_rejects_before_provisioning(
    db_uri: str, contributed_acme_provider: None
) -> None:
    """
    An unoffered provider is a 400 naming the available ones, with
    nothing provisioned.
    """
    host_store = HostStore(db_uri)
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    assert config is not None
    with pytest.raises(HTTPException) as exc:
        await launch_host(config=config, owner=_OWNER, host_store=host_store, provider="nope")
    assert exc.value.status_code == 400
    assert "nope" in exc.value.detail
    assert "docker, acme" in exc.value.detail
    assert host_store.list_hosts(_OWNER) == []


async def test_teardown_never_crosses_providers(db_uri: str) -> None:
    """
    A host is never torn down by another provider's launcher, even when
    both are configured.
    """
    host_store = HostStore(db_uri)

    class _AlphaLauncher(FakeSandboxLauncher):
        provider: ClassVar[str] = "alpha"

    class _BetaLauncher(FakeSandboxLauncher):
        provider: ClassVar[str] = "beta"

    alpha = _AlphaLauncher()
    beta = _BetaLauncher()
    config = HostDeployment(
        configs=(
            HostProviderConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda: alpha,
                token_ttl_s=3600,
                provider="alpha",
            ),
            HostProviderConfig(
                server_url="https://srv.example.com",
                launcher_factory=lambda: beta,
                token_ttl_s=3600,
                provider="beta",
            ),
        ),
    )
    # A row recorded against the SECOND provider.
    beta_host_id = uuid.uuid4().hex
    host_store.register_managed_host(
        host_id=beta_host_id,
        name="managed-beta",
        user_id=_OWNER,
        token="tok",
        provider="beta",
        sandbox_id="sb-beta",
        token_expires_at=now_epoch() + 3600,
    )
    host = host_store.get_host(beta_host_id)
    assert host is not None

    await terminate_host(host, host_store, config)
    assert beta.terminated == ["sb-beta"]
    assert alpha.terminated == []


async def test_info_lists_every_offered_provider(
    db_uri: str, tmp_path: Path, contributed_acme_provider: None
) -> None:
    """
    ``GET /v1/info`` reports every launch-capable provider, while
    ``sandbox_provider`` keeps naming the first for older bundles.
    """
    config = parse_host_section(
        {
            "server_url": "https://s.example.com",
            "providers": [{"provider": "docker"}, {"provider": "acme"}],
        }
    )
    app = _capability_probe_app(db_uri, tmp_path, config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed_sandboxes_enabled"] is True
    assert body["sandbox_provider"] == "docker"
    assert body["sandbox_providers"] == ["docker", "acme"]


async def test_info_lists_single_provider(db_uri: str, tmp_path: Path) -> None:
    """
    A single-provider server reports its one provider in the list too, so
    the picker can render from the list alone.
    """
    config = parse_host_section({"provider": "docker", "server_url": "https://s.example.com"})
    app = _capability_probe_app(db_uri, tmp_path, config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sandbox_provider"] == "docker"
    assert body["sandbox_providers"] == ["docker"]


# ── resolve_managed_agent_label (built-in gate) ─────────────


def test_resolve_agent_label_stamps_genuine_builtin(db_uri: str) -> None:
    """A genuine built-in (session_id None, deterministic id) is classified by name."""
    store = SqlAlchemyAgentStore(db_uri)
    store.create(builtin_agent_id("code-reviewer"), "code-reviewer", "bundle/loc")
    resolved = resolve_managed_agent_label(
        store, builtin_agent_id("code-reviewer"), session_id="conv_1"
    )
    assert resolved == "code-reviewer"


def test_resolve_agent_label_omits_ordinary_template(db_uri: str) -> None:
    """
    A user-registered template gets a RANDOM id, so it never matches
    builtin_agent_id(name) — it is not classified even under a built-in's name.
    """
    store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    store.create(agent_id, "code-reviewer", "bundle/loc")
    assert resolve_managed_agent_label(store, agent_id, session_id="conv_1") is None


def test_resolve_agent_label_omits_session_scoped_impostor() -> None:
    """
    The anti-spoof: a SESSION-SCOPED agent named like a built-in is omitted even
    when its id collides with the deterministic built-in id — session_id being
    set fails the gate, so it cannot self-attract the credential.
    """
    impostor = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id="conv_owner",
    )
    store = _StubAgentStore({impostor.id: impostor})
    assert resolve_managed_agent_label(store, impostor.id, session_id="conv_1") is None


def test_resolve_agent_label_omits_unknown_id(db_uri: str) -> None:
    """An id that resolves to no agent yields None (never stamps the raw id)."""
    store = SqlAlchemyAgentStore(db_uri)
    assert resolve_managed_agent_label(store, generate_agent_id(), session_id="conv_1") is None


def test_resolve_agent_label_omits_when_session_has_no_agent(db_uri: str) -> None:
    """A session with no bound agent yields None."""
    store = SqlAlchemyAgentStore(db_uri)
    assert resolve_managed_agent_label(store, None, session_id="conv_1") is None


def test_resolve_agent_label_survives_store_error() -> None:
    """A store error degrades to None (label is an optimization, never fails create)."""
    store = _StubAgentStore(error=True)
    assert resolve_managed_agent_label(store, generate_agent_id(), session_id="conv_1") is None


# ── relaunch re-derivation (claim-then-resolve) ─────────────


async def test_kick_managed_relaunch_defers_the_classifier_to_the_launch_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The relaunch hands the agent store and the bound agent id to the launch task
    rather than resolving the classifier itself, and reads the store not at all —
    so the claim-to-task region stays synchronous and only the winning caller
    ever pays for the read.
    """
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(orchestration, "_run_managed_launch", _capture)

    tracker = ManagedLaunchTracker()
    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )

    reads: list[str] = []

    class _RecordingStore(_StubAgentStore):
        def get(self, agent_id: str) -> Agent | None:
            reads.append(agent_id)
            return super().get(agent_id)

    store = _RecordingStore({builtin.id: builtin})
    conv = SimpleNamespace(labels={}, host_id="host_1", agent_id=builtin.id)
    before = set(orchestration._managed_launch_tasks)
    orchestration._kick_managed_relaunch(
        session_id="conv_1",
        conv=conv,
        host=SimpleNamespace(user_id=_OWNER),
        sandbox_config=SimpleNamespace(configs=()),
        tracker=tracker,
        conversation_store=SimpleNamespace(),
        host_store=SimpleNamespace(),
        app_state=SimpleNamespace(agent_store=store),
    )
    scheduled = set(orchestration._managed_launch_tasks) - before
    assert scheduled, "the claim was taken but no task was scheduled to settle it"
    await asyncio.gather(*scheduled)
    assert reads == [], "the kick must not read the agent store; the launch task does"
    assert captured["agent_store"] is store
    assert captured["agent_id"] == builtin.id


async def test_relaunch_claim_and_launch_task_are_one_synchronous_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Claiming the single-flight entry and scheduling the task that settles it must
    stay in one uninterrupted synchronous step.

    An ``await`` between them is cancellable — a request timeout or a shutdown
    drain there leaves the entry claimed but unsettled with nothing scheduled to
    settle it, and that state does not heal:
    :func:`_maybe_relaunch_managed_sandbox` skips the re-kick whenever an
    unsettled entry exists, so every later message on the session waits out the
    full rendezvous timeout and then fails for the remaining life of the process.

    Keeping the function synchronous is what forecloses the window, so that is
    what this asserts — a later change back to ``async def`` reopens it.
    """
    import inspect

    from omnigent.server.routes._sessions import orchestration

    assert not inspect.iscoroutinefunction(orchestration._kick_managed_relaunch), (
        "_kick_managed_relaunch must stay synchronous: an await between "
        "tracker.begin and create_task is cancellable and leaks the claim"
    )

    async def _noop(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(orchestration, "_run_managed_launch", _noop)

    tracker = ManagedLaunchTracker()
    conv = SimpleNamespace(labels={}, host_id="host_1", agent_id=builtin_agent_id("code-reviewer"))
    before = set(orchestration._managed_launch_tasks)
    orchestration._kick_managed_relaunch(
        session_id="conv_1",
        conv=conv,
        host=SimpleNamespace(user_id=_OWNER),
        sandbox_config=SimpleNamespace(configs=()),
        tracker=tracker,
        conversation_store=SimpleNamespace(),
        host_store=SimpleNamespace(),
        app_state=SimpleNamespace(agent_store=_StubAgentStore()),
    )
    scheduled = set(orchestration._managed_launch_tasks) - before
    assert scheduled, "the claim was taken but no task was scheduled to settle it"
    assert tracker.get("conv_1") is not None
    await asyncio.gather(*scheduled)


async def test_kick_managed_relaunch_without_agent_store_threads_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No agent store on app.state (a stripped app) degrades the relaunch to an
    unclassified runner rather than raising — a fail-safe deny, never a spurious
    label."""
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(orchestration, "_run_managed_launch", _capture)

    conv = SimpleNamespace(labels={}, host_id="host_1", agent_id=builtin_agent_id("code-reviewer"))
    before = set(orchestration._managed_launch_tasks)
    orchestration._kick_managed_relaunch(
        session_id="conv_1",
        conv=conv,
        host=SimpleNamespace(user_id=_OWNER),
        sandbox_config=SimpleNamespace(configs=()),
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(),
        host_store=SimpleNamespace(),
        app_state=SimpleNamespace(),
    )
    scheduled = set(orchestration._managed_launch_tasks) - before
    await asyncio.gather(*scheduled)
    assert captured["agent_store"] is None


@pytest.mark.parametrize(
    ("agent_kwargs", "expected"),
    [
        pytest.param({}, None, id="no-store-no-id"),
        pytest.param({"agent_id": builtin_agent_id("code-reviewer")}, None, id="id-without-store"),
    ],
)
async def test_run_managed_launch_leaves_the_runner_unclassified(
    monkeypatch: pytest.MonkeyPatch,
    agent_kwargs: dict[str, object],
    expected: str | None,
) -> None:
    """Without both an agent store and a bound agent id there is nothing to
    resolve, and the launch proceeds with an unclassified runner rather than
    raising — the same fail-safe the gate itself applies."""
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _provision(**kwargs: object) -> None:
        captured.update(kwargs)
        return

    monkeypatch.setattr(orchestration, "_provision_managed_sandbox", _provision)

    await orchestration._run_managed_launch(
        session_id="conv_1",
        owner=_OWNER,
        sandbox_config=SimpleNamespace(configs=()),
        repos=[],
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: None),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        **agent_kwargs,
    )
    assert captured["agent_name"] is expected


async def test_run_managed_launch_resolves_the_classifier_on_its_own_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The launch task resolves the bound agent through the built-in gate and passes
    the name down to the provision step.

    This is where the read belongs: the task already owns the single-flight claim,
    so the request path never awaits between claiming and spawning, and a losing
    concurrent message never reaches this function at all.
    """
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _provision(**kwargs: object) -> None:
        captured.update(kwargs)
        return

    monkeypatch.setattr(orchestration, "_provision_managed_sandbox", _provision)

    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )
    await orchestration._run_managed_launch(
        session_id="conv_1",
        owner=_OWNER,
        sandbox_config=SimpleNamespace(configs=()),
        repos=[],
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: None),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        agent_store=_StubAgentStore({builtin.id: builtin}),
        agent_id=builtin.id,
    )
    assert captured["agent_name"] == "code-reviewer"


async def test_run_managed_launch_omits_the_classifier_for_a_session_scoped_impostor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-scoped agent named after a built-in resolves to no classifier
    even now that the resolve runs on the launch task — the gate travels with the
    read, so moving it did not weaken the anti-spoof property."""
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _provision(**kwargs: object) -> None:
        captured.update(kwargs)
        return

    monkeypatch.setattr(orchestration, "_provision_managed_sandbox", _provision)

    impostor = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id="conv_1",
    )
    await orchestration._run_managed_launch(
        session_id="conv_1",
        owner=_OWNER,
        sandbox_config=SimpleNamespace(configs=()),
        repos=[],
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: None),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        agent_store=_StubAgentStore({impostor.id: impostor}),
        agent_id=impostor.id,
    )
    assert captured["agent_name"] is None


async def test_kick_managed_wake_defers_the_classifier_to_the_wake_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The wake hands the agent store and the bound agent id to the wake task rather
    than resolving the classifier itself, matching the relaunch path: the
    claim-to-task region stays synchronous and only the winning caller pays for
    the read.
    """
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(orchestration, "_run_managed_wake", _capture)

    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )

    reads: list[str] = []

    class _RecordingStore(_StubAgentStore):
        def get(self, agent_id: str) -> Agent | None:
            reads.append(agent_id)
            return super().get(agent_id)

    store = _RecordingStore({builtin.id: builtin})
    conv = SimpleNamespace(labels={}, host_id="host_1", agent_id=builtin.id)
    before = set(orchestration._managed_launch_tasks)
    orchestration._kick_managed_wake_impl(
        session_id="conv_1",
        conv=conv,
        sandbox_config=SimpleNamespace(configs=()),
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(),
        host_store=SimpleNamespace(),
        app_state=SimpleNamespace(agent_store=store),
    )
    scheduled = set(orchestration._managed_launch_tasks) - before
    assert scheduled, "the claim was taken but no task was scheduled to settle it"
    await asyncio.gather(*scheduled)
    assert reads == [], "the kick must not read the agent store; the wake task does"
    assert captured["agent_store"] is store
    assert captured["agent_id"] == builtin.id


async def test_run_managed_wake_re_stamps_the_woken_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The wake task resolves the bound agent through the built-in gate and forwards
    the name into the resume, so the rebuilt runner carries its classifier again.
    """
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _resume(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.server.hosts.resume_host", _resume)

    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )
    conv = SimpleNamespace(
        inference_snapshot=None, labels={}, host_id="host_1", agent_id=builtin.id
    )
    await orchestration._run_managed_wake(
        session_id="conv_1",
        conv=conv,
        sandbox_config=SimpleNamespace(configs=()),
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: None),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        agent_store=_StubAgentStore({builtin.id: builtin}),
        agent_id=builtin.id,
    )
    assert captured["agent_name"] == "code-reviewer"


async def test_run_managed_wake_omits_the_classifier_for_a_session_scoped_impostor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A session-scoped agent named after a built-in resolves to no classifier on
    the wake path too, so a wake cannot be used to obtain a label the initial
    launch would have refused.
    """
    from omnigent.server.routes._sessions import orchestration

    captured: dict[str, object] = {}

    async def _resume(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.server.hosts.resume_host", _resume)

    impostor = Agent(
        id="9f2c1b7e4a5d4c8fa1b2c3d4e5f60718",
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id="conv_1",
    )
    conv = SimpleNamespace(
        inference_snapshot=None, labels={}, host_id="host_1", agent_id=impostor.id
    )
    await orchestration._run_managed_wake(
        session_id="conv_1",
        conv=conv,
        sandbox_config=SimpleNamespace(configs=()),
        tracker=ManagedLaunchTracker(),
        conversation_store=SimpleNamespace(get_conversation=lambda _sid: None),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        agent_store=_StubAgentStore({impostor.id: impostor}),
        agent_id=impostor.id,
    )
    assert captured["agent_name"] is None


async def test_run_managed_wake_recreates_a_definitively_gone_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gone wake reuses the host identity and create-time session metadata."""
    from omnigent.server.routes._sessions import orchestration

    async def _resume(*args: object, **kwargs: object) -> None:
        raise HostGoneError("sandbox no longer exists")

    captured: dict[str, object] = {}

    async def _launch(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.server.hosts.resume_host", _resume)
    monkeypatch.setattr(orchestration, "_run_managed_launch", _launch)

    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )
    agent_store = _StubAgentStore({builtin.id: builtin})
    host = SimpleNamespace(host_id="host_1", user_id=_OWNER)
    host_store = SimpleNamespace(get_host=lambda _host_id: host)
    sandbox_config = SimpleNamespace(configs=())
    conversation_store = SimpleNamespace()
    host_registry = SimpleNamespace()
    tunnel_registry = SimpleNamespace()
    conv = SimpleNamespace(
        inference_snapshot=None,
        labels={MANAGED_REPO_LABEL_KEY: "https://github.com/omnigent-ai/omnigent#main"},
        host_id=host.host_id,
        agent_id=builtin.id,
    )

    await orchestration._run_managed_wake(
        session_id="conv_1",
        conv=conv,
        sandbox_config=sandbox_config,
        tracker=ManagedLaunchTracker(),
        conversation_store=conversation_store,
        host_store=host_store,
        host_registry=host_registry,
        tunnel_registry=tunnel_registry,
        agent_store=agent_store,
        agent_id=builtin.id,
    )

    assert captured["session_id"] == "conv_1"
    assert captured["owner"] == _OWNER
    assert captured["sandbox_config"] is sandbox_config
    assert captured["conversation_store"] is conversation_store
    assert captured["host_store"] is host_store
    assert captured["host_registry"] is host_registry
    assert captured["tunnel_registry"] is tunnel_registry
    assert captured["host_to_relaunch"] is host
    assert captured["agent_store"] is agent_store
    assert captured["agent_id"] == builtin.id
    repos = captured["repos"]
    assert isinstance(repos, list)
    assert len(repos) == 1
    assert isinstance(repos[0], RepoWorkspace)
    assert repos[0].url == "https://github.com/omnigent-ai/omnigent"
    assert repos[0].branch == "main"
    assert repos[0].repo_name == "omnigent"


async def test_recreated_sandbox_records_and_publishes_workspace_reset_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each fresh generation records and publishes one visible reset notice."""
    from omnigent.server.routes._sessions import orchestration

    appended: list[object] = []
    surfaced: list[object] = []

    class _ConversationStore:
        def set_host_id(self, session_id: str, host_id: str, workspace: str) -> object:
            assert session_id == "conv_1"
            assert host_id == "host_1"
            assert workspace == "/root/workspace/omnigent"
            return SimpleNamespace(id=session_id, host_id=host_id, workspace=workspace)

        def append(self, session_id: str, items: list[object]) -> list[object]:
            assert session_id == "conv_1"
            appended.extend(items)
            return items

    published: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        orchestration,
        "_publish_sandbox_status",
        lambda session_id, stage, detail=None: published.append((session_id, stage, detail)),
    )
    monkeypatch.setattr(
        orchestration,
        "_publish_external_conversation_item",
        lambda session_id, item: surfaced.append(item),
    )
    tracker = ManagedLaunchTracker()
    tracker.begin("conv_1")

    await orchestration._bind_and_launch_managed_runner(
        session_id="conv_1",
        managed=HostLaunch(
            host_id="host_1",
            workspace="/root/workspace/omnigent",
        ),
        sandbox_config=SimpleNamespace(configs=()),
        tracker=tracker,
        conversation_store=_ConversationStore(),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        host_to_relaunch=SimpleNamespace(host_id="host_1", sandbox_provider="ephemeral"),
    )

    assert tracker.get("conv_1") is None
    assert [item.type for item in appended] == ["error"]
    [visible] = appended
    assert visible.data.code == "managed_sandbox_workspace_reset"
    assert visible.data.level == "info"
    assert surfaced == [visible]
    assert published[-1] == ("conv_1", "ready", None)


async def test_recreated_sandbox_that_keeps_its_files_records_no_reset_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose workspace survives a recreate (the host's home is on durable
    disk) lost nothing, so the session gets no "files are gone" notice."""
    from omnigent.server.routes._sessions import orchestration

    appended: list[object] = []

    class _ConversationStore:
        def set_host_id(self, session_id: str, host_id: str, workspace: str) -> object:
            return SimpleNamespace(id=session_id, host_id=host_id, workspace=workspace)

        def append(self, session_id: str, items: list[object]) -> list[object]:
            appended.extend(items)
            return items

    monkeypatch.setattr(
        orchestration, "_publish_sandbox_status", lambda session_id, stage, detail=None: None
    )
    monkeypatch.setattr(
        orchestration, "_publish_external_conversation_item", lambda session_id, item: None
    )
    tracker = ManagedLaunchTracker()
    tracker.begin("conv_1")

    await orchestration._bind_and_launch_managed_runner(
        session_id="conv_1",
        managed=HostLaunch(host_id="host_1", workspace="/home/superchat/workspace"),
        sandbox_config=SimpleNamespace(configs=()),
        tracker=tracker,
        conversation_store=_ConversationStore(),
        host_store=SimpleNamespace(),
        host_registry=None,
        tunnel_registry=None,
        host_to_relaunch=SimpleNamespace(host_id="host_1", sandbox_provider="docker"),
    )

    assert appended == []


async def test_concurrent_relaunch_messages_kick_a_single_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two messages racing to relaunch the same dead sandbox kick exactly ONE
    launch: the check→begin region carries no ``await``, so the loser
    rendezvouses on the winner's entry instead of double-launching a second Pod.
    """
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "host_resume_supported", lambda *a, **k: False)

    calls: list[dict[str, object]] = []
    tracker = ManagedLaunchTracker()

    # The racing message has to reach its tracker check while the winner's claim
    # is still UNSETTLED, because an unsettled claim is what it must rendezvous
    # on. Both callers await asyncio.to_thread twice before that check, and an
    # executor hop takes an unpredictable number of event-loop turns to deliver,
    # so holding the winner open for a fixed number of turns does not establish
    # the ordering: on a loaded machine the racer arrives after the claim
    # settled, takes the retry branch, and kicks a second launch. That retry is
    # intended behaviour and is guarded in production by the is_online check
    # above, which this test stubs False forever — so a fixed-turn hold made the
    # test fail for a reason unrelated to the invariant.
    #
    # Hold until the racer has demonstrably checked instead. Three reads are the
    # whole exchange: the winner checks and re-reads after kicking, and the racer
    # checks once and rendezvouses.
    raced = asyncio.Event()
    reads = 0
    tracker_get = tracker.get

    def _counting_get(session_id: str) -> ManagedLaunch | None:
        nonlocal reads
        reads += 1
        if reads >= 3:
            raced.set()
        return tracker_get(session_id)

    monkeypatch.setattr(tracker, "get", _counting_get)

    async def _capture(**kwargs: object) -> None:
        calls.append(kwargs)
        tracker_arg = kwargs["tracker"]
        session_arg = kwargs["session_id"]
        assert isinstance(tracker_arg, ManagedLaunchTracker)
        assert isinstance(session_arg, str)
        # Unbounded on purpose. Any duration here would be a second timing
        # assumption, and this test exists because the first one was wrong; the
        # suite's own timeout is the backstop if a refactor stops the racer
        # reaching its check.
        await raced.wait()
        # Settle so the message rendezvous (_await_settled_managed_launch) returns.
        tracker_arg.finish(session_arg)

    monkeypatch.setattr(orchestration, "_run_managed_launch", _capture)

    builtin = Agent(
        id=builtin_agent_id("code-reviewer"),
        created_at=now_epoch(),
        name="code-reviewer",
        bundle_location="bundle/loc",
        session_id=None,
    )
    dead_host = SimpleNamespace(
        sandbox_provider="docker", user_id=_OWNER, status="offline", updated_at=0
    )
    app_state = SimpleNamespace(
        host_store=SimpleNamespace(get_host=lambda _hid: dead_host, is_online=lambda _hid: False),
        sandbox_config=SimpleNamespace(configs=()),
        managed_launches=tracker,
        agent_store=_StubAgentStore({builtin.id: builtin}),
        host_registry=None,
        tunnel_registry=None,
    )
    conv = SimpleNamespace(labels={}, host_id="host_1", agent_id=builtin.id)

    engaged = await asyncio.gather(
        *(
            orchestration._maybe_relaunch_managed_sandbox(
                session_id="conv_1",
                conv=conv,
                app_state=app_state,
                conversation_store=SimpleNamespace(),
            )
            for _ in range(2)
        )
    )
    # Drain any relaunch task the winner scheduled.
    await asyncio.gather(*list(orchestration._managed_launch_tasks))
    assert engaged == [True, True]
    assert len(calls) == 1
    assert calls[0]["agent_id"] == builtin.id


# ── contributed host providers ───────────────────────────


class _AcmeConfig(BaseModel):
    """Config model for the contributed provider used below."""

    namespace: str = "default"


class _AcmeLauncher(HostLauncher):
    """Minimal contributed launcher; records the config it was handed."""

    provider: ClassVar[str] = "acme"

    def __init__(self, *, config: _AcmeConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        """No preflight needed for a test double."""

    def provision(self, name: str) -> str:
        """Reserve a name, matching the entrypoint-as-host providers."""
        return name

    def start_host(self, sandbox_id: str, **kwargs: Any) -> str:
        """Return a workspace path without starting anything."""
        return "/workspace"

    def terminate(self, sandbox_id: str) -> None:
        """Nothing to tear down."""


@pytest.fixture
def contributed_acme_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register an `acme` provider through the entry point group.

    Installs a real module under the `omnigent.community.host.*` namespace
    the registry requires, so `instantiate` can import the launcher class
    rather than only resolve its name.
    """
    module_name = f"{COMMUNITY_MODULE_PREFIX}acme"
    module = types.ModuleType(module_name)
    module.AcmeSandboxLauncher = _AcmeLauncher  # type: ignore[attr-defined]
    module.AcmeConfig = _AcmeConfig  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)
    # The registry requires the config model to live in that namespace too,
    # not just the launcher, so both classes have to claim the fake module.
    monkeypatch.setattr(_AcmeLauncher, "__module__", module_name)
    monkeypatch.setattr(_AcmeConfig, "__module__", module_name)

    def _contribution() -> HostProviderContribution:
        return HostProviderContribution(
            name="omnigent-acme",
            providers={
                "acme": HostProviderMetadata(
                    name="acme",
                    launcher_class=f"{module_name}:AcmeSandboxLauncher",
                    config_model=_AcmeConfig,
                    managed_token_ttl_s=1234,
                )
            },
        )

    monkeypatch.setattr(
        "omnigent.hosts.registry._entry_points",
        lambda: (SimpleNamespace(name="acme", load=lambda: _contribution),),
    )
    reset_plugin_state_for_tests()
    yield
    reset_plugin_state_for_tests()


def test_parse_contributed_provider_builds_registry_backed_factory(
    contributed_acme_provider: None,
) -> None:
    """
    A provider contributed through the entry point group is configurable and
    serves managed sessions, without being named in this module.
    """
    cfg = parse_host_section(
        {
            "provider": "acme",
            "server_url": "https://s.example.com",
            "acme": {"namespace": "sandboxes"},
        }
    )
    assert cfg is not None
    cfg = cfg.default
    assert cfg.provider == "acme"
    # Contributed providers register in order to serve, so the web UI must be
    # told the sandbox option works rather than offered one that 400s.
    assert cfg.managed_launch_supported is True
    assert cfg.token_ttl_s == 1234

    launcher = cfg.launcher_factory()
    assert isinstance(launcher, _AcmeLauncher)
    # The `host.acme` block reached the launcher through the config_model
    # the registry documents, rather than being silently dropped.
    assert launcher.config.namespace == "sandboxes"


def test_contributed_provider_config_is_validated(
    contributed_acme_provider: None,
) -> None:
    """A malformed provider block fails at parse time with the provider named."""
    with pytest.raises(ValueError, match=r"acme"):
        parse_host_section(
            {
                "provider": "acme",
                "server_url": "https://s.example.com",
                "acme": {"namespace": ["not", "a", "string"]},
            }
        )


def test_contributed_provider_non_mapping_block_is_rejected(
    contributed_acme_provider: None,
) -> None:
    """A non-mapping ``host.<provider>`` block is rejected at parse time."""
    with pytest.raises(ValueError, match=r"host\.acme"):
        parse_host_section(
            {
                "provider": "acme",
                "server_url": "https://s.example.com",
                "acme": "oops",
            }
        )


def test_unknown_provider_is_still_rejected(contributed_acme_provider: None) -> None:
    """Consulting the registry does not make every name acceptable."""
    with pytest.raises(ValueError, match=r"host\.provider"):
        parse_host_section({"provider": "nope", "server_url": "https://s.example.com"})


def test_keep_warm_sets_runner_idle_timeout() -> None:
    """keep_warm_s maps onto runner.idle_timeout_s and is authoritative (wins over
    an explicit value, preserves other runner keys); a no-op when unset."""
    from omnigent.server.hosts import _apply_keep_warm, _parse_keep_warm_s

    assert _parse_keep_warm_s({"keep_warm_s": 30}) == 30
    assert _parse_keep_warm_s({"keep_warm_s": 30.0}) == 30  # whole float ok
    assert _parse_keep_warm_s({"keep_warm_s": 2592000}) == 2592000  # 30-day ceiling ok
    # An oversized value must be rejected at config time: it becomes
    # runner.idle_timeout_s, and the runner's float() would OverflowError on a
    # value this large, stopping the runner from starting.
    for bad in (0, 0.5, 2.9, -5, True, "x", float("nan"), float("inf"), 2592001, 10**400):
        with pytest.raises(ValueError):
            _parse_keep_warm_s({"keep_warm_s": bad})
    assert _apply_keep_warm(None, 30) == {"runner": {"idle_timeout_s": 30}}
    assert (
        _apply_keep_warm({"runner": {"idle_timeout_s": 999}}, 30)["runner"]["idle_timeout_s"] == 30
    )
    assert _apply_keep_warm({"runner": {"foo": 1}}, 30)["runner"] == {
        "foo": 1,
        "idle_timeout_s": 30,
    }
    assert _parse_keep_warm_s({}) is None
    assert _apply_keep_warm({"x": 1}, None) == {"x": 1}
    with pytest.raises(ValueError):
        _parse_keep_warm_s({"keep_warm_s": -5})


@pytest.mark.parametrize("relaunch", [False, True])
async def test_account_deleted_after_provision_terminates_unregistered_sandbox(db_uri, relaunch):
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(db_uri)
    account = accounts.create_user_with_password(_OWNER, "test-password-hash")
    hosts = HostStore(db_uri)

    def connect(invocation):
        hosts.upsert_on_connect(invocation.host_id, invocation.host_name, _OWNER)

    fake = FakeSandboxLauncher(on_host_start=connect)
    config = _injected_config(fake)
    host = None
    if relaunch:
        first = await launch_host(config=config, owner=_OWNER, host_store=hosts)
        host = hosts.get_host(first.host_id)
    provision = fake.provision
    unregistered = []

    def delete_after_provision(name):
        sandbox_id = provision(name)
        unregistered.append(sandbox_id)
        assert accounts.delete_user(_OWNER) is True
        return sandbox_id

    fake.provision = delete_after_provision
    with account_authority_scope(_OWNER, account.account_generation):
        with pytest.raises(HTTPException) as error:
            if relaunch:
                await relaunch_host(config=config, host=host, host_store=hosts)
            else:
                await launch_host(config=config, owner=_OWNER, host_store=hosts)
    assert "revoked" in str(error.value.detail)
    assert len(unregistered) == 1
    assert unregistered[0] in fake.terminated
    assert len(fake.host_starts) == (1 if relaunch else 0)


async def test_registration_database_failure_still_attempts_provider_cleanup(db_uri, monkeypatch):
    hosts = HostStore(db_uri)
    fake = FakeSandboxLauncher()

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(hosts, "register_managed_host", unavailable)
    monkeypatch.setattr(hosts, "get_host", unavailable)
    with pytest.raises(HTTPException, match="database unavailable"):
        await launch_host(config=_injected_config(fake), owner=_OWNER, host_store=hosts)
    assert len(fake.terminated) == 1
    assert fake.host_starts == []
