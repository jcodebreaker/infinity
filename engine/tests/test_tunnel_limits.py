"""Tests for the shared tunnel budget and the launchers that must apply it.

Scope note (#1116): these guard the shared constants, the tunnel-vs-app-level
invariant, and that every in-tree launcher hands the budget to uvicorn. They do
NOT assert anything about the server-global reach of uvicorn's ``ws_ping_*`` —
that setting applies the same 30 s/90 s budget to every WebSocket route
(session-updates, terminal-attach), which is deliberate: for an idle such socket
the protocol PING/PONG is the only half-open detector, so the only effect is a
slightly later half-open-socket reap (~120 s vs ~40 s), bounded and not a
correctness change. See the comment on ``uvicorn.Config`` in ``omnigent/cli.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn

from omnigent.util.tunnel_limits import (
    RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    uvicorn_tunnel_kwargs,
)

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "deployment" / "entrypoint.py"


def test_max_message_bytes_is_100mb() -> None:
    """The tunnel message size limit matches the design spec: 100 MiB."""
    assert RUNNER_TUNNEL_MAX_MESSAGE_BYTES == 100 * 1024 * 1024


def test_max_message_bytes_is_positive_int() -> None:
    """The constant is a positive integer, not a float or zero."""
    assert isinstance(RUNNER_TUNNEL_MAX_MESSAGE_BYTES, int)
    assert RUNNER_TUNNEL_MAX_MESSAGE_BYTES > 0


def test_keepalive_constants_are_positive_floats() -> None:
    """Ping interval/timeout are positive floats (passed straight to websockets/uvicorn)."""
    assert isinstance(TUNNEL_KEEPALIVE_PING_INTERVAL_S, float)
    assert isinstance(TUNNEL_KEEPALIVE_PING_TIMEOUT_S, float)
    assert TUNNEL_KEEPALIVE_PING_INTERVAL_S > 0
    assert TUNNEL_KEEPALIVE_PING_TIMEOUT_S > 0


def test_keepalive_not_stricter_than_app_level_budget() -> None:
    """The protocol keepalive MUST NOT pre-empt the app-level liveness budget (#1116).

    The server's app-level ``_ping_loop`` declares a peer dead after
    ``PING_INTERVAL_S * PING_MISS_THRESHOLD`` seconds of silence. If the
    websockets/uvicorn protocol keepalive timeout is tighter than that, it drops a
    healthy-but-busy tunnel (event loop stalled) with ``1011`` before the
    deliberate app-level policy ever applies — the regression this guards. Checked
    against BOTH tunnels, which share the same budget.
    """
    from omnigent.server.routes import host_tunnel, runner_tunnel

    for module in (runner_tunnel, host_tunnel):
        app_level_dead_after_s = module.PING_INTERVAL_S * module.PING_MISS_THRESHOLD
        assert app_level_dead_after_s <= TUNNEL_KEEPALIVE_PING_TIMEOUT_S, (
            f"protocol ping_timeout ({TUNNEL_KEEPALIVE_PING_TIMEOUT_S}s) is stricter "
            f"than {module.__name__}'s {app_level_dead_after_s}s app-level budget; it "
            "would drop a busy-but-healthy tunnel with 1011 before the app-level "
            "keepalive fires (issue #1116)."
        )


def test_uvicorn_tunnel_kwargs_name_real_uvicorn_options() -> None:
    """Every key is an option uvicorn accepts, carrying the budget's value.

    Built into a real ``uvicorn.Config`` so a renamed or dropped uvicorn option
    fails here rather than at a launcher's first boot.
    """
    kwargs = uvicorn_tunnel_kwargs()
    assert kwargs == {
        "ws_max_size": RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        "ws_ping_interval": TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        "ws_ping_timeout": TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    }
    config = uvicorn.Config("omnigent.server.app:create_app", factory=True, **kwargs)
    assert config.ws_max_size == RUNNER_TUNNEL_MAX_MESSAGE_BYTES
    assert config.ws_ping_interval == TUNNEL_KEEPALIVE_PING_INTERVAL_S
    assert config.ws_ping_timeout == TUNNEL_KEEPALIVE_PING_TIMEOUT_S


def test_deprecated_runner_path_reexports_the_same_objects() -> None:
    """The vendored-copy alias must stay in lockstep until its 0.16.0 removal.

    universe's launcher imports the pre-move path, so a shim that drifted (or a
    name added here and not there) would hand that deployment a stale budget.
    """
    shim = importlib.import_module("omnigent.runner.transports.ws_tunnel.limits")
    canonical = importlib.import_module("omnigent.util.tunnel_limits")

    for name in shim.__all__:
        assert getattr(shim, name) is getattr(canonical, name), name


def _load_entrypoint(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Load the image's ``deployment/entrypoint.py`` without running ``main()``."""
    spec = importlib.util.spec_from_file_location("_entrypoint_under_test", _ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses resolve annotations through sys.modules while loading.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_docker_entrypoint_hands_the_tunnel_budget_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The engine image must serve tunnels with the 30 s/90 s budget.

    Regression: it passed ``ws_max_size`` alone, leaving uvicorn's 20 s keepalive
    to close busy-but-healthy tunnels, and every session rides one.
    """
    entrypoint = _load_entrypoint(monkeypatch)

    captured: dict[str, Any] = {}
    built = entrypoint._BuiltApp(app=object(), host="0.0.0.0", port=8000)  # type: ignore[arg-type]
    resolved_config = SimpleNamespace(database_url="postgresql://stub/omnigent")
    monkeypatch.setattr(entrypoint, "_resolve_config", lambda: resolved_config)
    monkeypatch.setattr(entrypoint, "run_migrations", lambda url: None)
    monkeypatch.setattr(entrypoint, "build_app", lambda cfg: built)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))

    entrypoint.main()

    assert captured, "main() never reached uvicorn.run"
    for key, value in uvicorn_tunnel_kwargs().items():
        assert captured.get(key) == value, key
