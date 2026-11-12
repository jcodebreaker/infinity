"""No agent-CLI subprocess may inherit unrelated host secrets (#3445).

This is the parametrized canary the maintainer asked for. The point is not to
re-test each executor's wiring — it is that the *next* harness added to this
repo fails here if it forgets to filter.
"""

from __future__ import annotations

import pytest

from omnigent.harnesses.agent_env import (
    BASE_ALLOW_EXACT,
    BASE_ALLOW_PREFIXES,
    clean_agent_env,
    declared_passthrough,
)

# Families that must never reach a vendor CLI unless the spec asks for them.
# One planted value per family, all distinct so a failure names the leak.
CANARY_SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "canary-aws",
    "AWS_SESSION_TOKEN": "canary-aws-session",
    "DATABRICKS_TOKEN": "canary-databricks",
    "DATABRICKS_CONFIG_PROFILE": "canary-databricks-profile",
    "ANTHROPIC_API_KEY": "canary-anthropic",
    "GEMINI_API_KEY": "canary-gemini",
    "GITHUB_TOKEN": "canary-github",
    "SLACK_BOT_TOKEN": "canary-slack",
    "OPENROUTER_API_KEY": "canary-openrouter",
    "PYTHON_KEYRING_BACKEND": "test_keyring_backend.FileKeyring",
}

# The per-harness families, matching the decision table on #3445.
HARNESS_PREFIXES = {
    "pi": ("PI_", "NODE_"),
}


@pytest.fixture
def hostile_env():
    """A host environment carrying every canary plus the things a CLI needs."""
    return {
        "HOME": "/home/user",
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "HTTPS_PROXY": "http://proxy:8080",
        **CANARY_SECRETS,
    }


# --- the real builders -----------------------------------------------------
#
# The prefix table above is a hand copy of what each executor passes, so on its
# own it only proves clean_agent_env works. These drive each executor's OWN
# spawn-env builder against a planted os.environ, which is what actually makes
# the "next harness fails here" claim true: if a builder reverts to
# os.environ.copy(), or the next harness never calls the helper, this fails.


def _pi_spawn_env():
    from omnigent.harnesses.pi_executor import _clean_pi_env

    return _clean_pi_env()


SPAWN_ENV_BUILDERS = {
    "pi": _pi_spawn_env,
}


def test_every_harness_is_covered_by_a_real_builder():
    """A harness added to the prefix table must also be wired up above."""
    assert set(SPAWN_ENV_BUILDERS) == set(HARNESS_PREFIXES)


@pytest.mark.parametrize("harness", sorted(SPAWN_ENV_BUILDERS))
def test_real_builder_does_not_leak_host_secrets(harness, hostile_env, monkeypatch):
    """Drive the executor's own builder against a planted host environment."""
    monkeypatch.setattr("os.environ", dict(hostile_env))

    env = SPAWN_ENV_BUILDERS[harness]()

    leaked = {k: v for k, v in env.items() if k in CANARY_SECRETS}
    # A canary inside the harness's own family is not a leak.
    leaked = {k: v for k, v in leaked.items() if not k.startswith(HARNESS_PREFIXES[harness] or ())}
    assert not leaked, f"{harness}'s real spawn env leaked {sorted(leaked)}"


@pytest.mark.parametrize("harness", sorted(SPAWN_ENV_BUILDERS))
def test_real_builder_still_yields_a_usable_environment(harness, hostile_env, monkeypatch):
    monkeypatch.setattr("os.environ", dict(hostile_env))

    env = SPAWN_ENV_BUILDERS[harness]()

    assert env["HOME"] == "/home/user"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HTTPS_PROXY"] == "http://proxy:8080"


def test_real_builders_pass_node_extra_ca_certs(monkeypatch):
    """A corporate CA must survive filtering, or every Node CLI breaks on TLS."""
    monkeypatch.setattr("os.environ", {"NODE_EXTRA_CA_CERTS": "/etc/corp-ca.pem"})
    for harness, build in sorted(SPAWN_ENV_BUILDERS.items()):
        env = build()
        assert env.get("NODE_EXTRA_CA_CERTS") == "/etc/corp-ca.pem", harness


def test_real_builders_pass_ssh_auth_sock(monkeypatch):
    """ssh-agent must survive filtering, or git-over-SSH breaks in every harness."""
    sock = "/private/tmp/com.apple.launchd.7Qk/Listeners"
    monkeypatch.setattr("os.environ", {"SSH_AUTH_SOCK": sock})
    for harness, build in sorted(SPAWN_ENV_BUILDERS.items()):
        assert build().get("SSH_AUTH_SOCK") == sock, harness


def test_real_builders_strip_desktop_session(monkeypatch):
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    monkeypatch.setattr("os.environ", {**session_env, "XDG_CONFIG_HOME": "/home/test/.config"})

    for harness, build in sorted(SPAWN_ENV_BUILDERS.items()):
        env = build()
        assert session_env.keys().isdisjoint(env), harness
        assert env["XDG_CONFIG_HOME"] == "/home/test/.config", harness


@pytest.mark.parametrize("explicit", [False, True])
def test_desktop_session_requires_explicit_cli_passthrough(explicit):
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }

    env = clean_agent_env(
        allow_prefixes=("DBUS_", "XDG_"),
        allow_exact=session_env,
        extra_allowed=session_env if explicit else (),
        source=session_env,
    )

    assert env == (session_env if explicit else {})


@pytest.mark.parametrize("harness", sorted(HARNESS_PREFIXES))
def test_no_harness_inherits_unrelated_secrets(harness, hostile_env):
    env = clean_agent_env(allow_prefixes=HARNESS_PREFIXES[harness], source=hostile_env)
    leaked = {k: v for k, v in env.items() if k in CANARY_SECRETS}
    assert not leaked, f"{harness} leaked {sorted(leaked)}"
    for value in CANARY_SECRETS.values():
        assert value not in env.values(), f"{harness} leaked the value {value!r}"


@pytest.mark.parametrize("harness", sorted(HARNESS_PREFIXES))
def test_every_harness_still_gets_a_usable_environment(harness, hostile_env):
    """Filtering must not break the CLI: HOME/PATH/locale/proxy still pass."""
    env = clean_agent_env(allow_prefixes=HARNESS_PREFIXES[harness], source=hostile_env)
    assert env["HOME"] == "/home/user"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["HTTPS_PROXY"] == "http://proxy:8080"


def test_a_harness_still_sees_its_own_family(hostile_env):
    src = {**hostile_env, "PI_CODING_AGENT_DIR": "p", "OPENAI_BASE_URL": "o"}
    pi = clean_agent_env(allow_prefixes=HARNESS_PREFIXES["pi"], source=src)
    assert pi["PI_CODING_AGENT_DIR"] == "p"
    assert "OPENAI_BASE_URL" not in pi, "pi must not see another harness's family"


def test_env_passthrough_is_the_documented_escape_hatch(hostile_env):
    """An explicitly declared variable passes even outside every allowed family."""
    without = clean_agent_env(source=hostile_env)
    assert "GEMINI_API_KEY" not in without
    with_pt = clean_agent_env(extra_allowed=("GEMINI_API_KEY",), source=hostile_env)
    assert with_pt["GEMINI_API_KEY"] == "canary-gemini"


def test_deny_exact_beats_a_matching_prefix(hostile_env):
    """An exact deny strips OPENAI_API_KEY despite an allowed OPENAI_ prefix."""
    src = {**hostile_env, "OPENAI_API_KEY": "sk-dev", "OPENAI_BASE_URL": "https://x"}
    env = clean_agent_env(allow_prefixes=("OPENAI_",), deny_exact=("OPENAI_API_KEY",), source=src)
    assert "OPENAI_API_KEY" not in env
    assert env["OPENAI_BASE_URL"] == "https://x"


def test_source_is_never_mutated(hostile_env):
    before = dict(hostile_env)
    clean_agent_env(allow_prefixes=("PI_",), source=hostile_env)
    assert hostile_env == before


def test_base_does_not_include_identity_vars():
    """USER/LOGNAME/SHELL/TZ stay per-harness: pi passes them, and the shared
    base must not silently widen every harness's set."""
    for name in ("USER", "LOGNAME", "SHELL", "TZ"):
        assert name not in BASE_ALLOW_EXACT
    assert not any(p in BASE_ALLOW_PREFIXES for p in ("AWS_", "DATABRICKS_", "OPENAI_"))


def test_declared_passthrough_tolerates_a_missing_chain():
    class _NoSandbox:
        sandbox = None

    assert declared_passthrough(None) == ()
    assert declared_passthrough(_NoSandbox()) == ()
