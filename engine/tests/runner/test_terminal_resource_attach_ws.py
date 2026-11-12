"""Tests for the runner's
``WS /v1/sessions/{id}/resources/terminals/{terminal_id}/attach`` endpoint.

The endpoint resolves the opaque terminal resource id back to the
runner-local registry entry and bridges tmux control-mode output to the
browser-facing WebSocket. These tests pin the route boundary and registry
lookup while stubbing the control bridge itself.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.harnesses.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.direct_attach import (
    allowed_origin_for_server,
    create_direct_attach_app,
    start_direct_attach_listener,
)
from omnigent.terminals import TerminalRegistry
from tests.runner.helpers import NullServerClient, make_test_terminal_instance


def _make_running_instance(name: str, session_key: str, tmp_path: Path) -> TerminalInstance:
    """A :class:`TerminalInstance` flagged running, bypassing real tmux.

    :param name: Terminal name from the spec, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param tmp_path: Pytest tmp directory used as the socket parent.
    :returns: The seeded :class:`TerminalInstance`.
    """
    return make_test_terminal_instance(name, session_key, tmp_path, running=True)


def _seed_registry(
    registry: TerminalRegistry,
    conversation_id: str,
    instance: TerminalInstance,
) -> None:
    """Insert *instance* into *registry* under *conversation_id*.

    :param registry: The :class:`TerminalRegistry` under test.
    :param conversation_id: Owning conversation/session id.
    :param instance: The :class:`TerminalInstance` to insert.
    """
    slot = registry._by_conversation.setdefault(conversation_id, {})
    slot[(instance.name, instance.session_key)] = instance


def _patch_control_attach(
    monkeypatch: pytest.MonkeyPatch,
    on_attach: Callable[[str, str, bool], None],
) -> None:
    """Patch the control bridge at the runner route boundary."""

    async def fake_control(
        websocket: object,
        *,
        socket_path: str,
        tmux_target: str,
        read_only: bool,
        on_client_interaction: object = None,
    ) -> None:
        del websocket, on_client_interaction
        on_attach(socket_path, tmux_target, read_only)
        raise RuntimeError("bridge stopped")

    monkeypatch.setattr(
        "omnigent.runner.app.bridge_tmux_control_to_websocket",
        fake_control,
    )


def test_runner_resource_attach_ignores_obsolete_transport_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale PTY query is ignored and the terminal uses control mode."""
    registry = TerminalRegistry()
    instance = _make_running_instance("bash", "s1", tmp_path)
    _seed_registry(registry, "conv_abc", instance)
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    calls: list[tuple[str, str, bool]] = []
    _patch_control_attach(monkeypatch, lambda *args: calls.append(args))

    with pytest.raises(RuntimeError, match="bridge stopped"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?transport=pty"
        ):
            pass

    assert calls == [(str(tmp_path / "bash-s1.sock"), "main", False)]


def test_runner_resource_attach_passes_read_only_to_control_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?read_only=true`` reaches the control bridge."""
    registry = TerminalRegistry()
    _seed_registry(registry, "conv_abc", _make_running_instance("bash", "s1", tmp_path))
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    calls: list[tuple[str, str, bool]] = []
    _patch_control_attach(monkeypatch, lambda *args: calls.append(args))

    with pytest.raises(RuntimeError, match="bridge stopped"):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach?read_only=true"
        ):
            pass

    assert calls == [(str(tmp_path / "bash-s1.sock"), "main", True)]


def test_runner_resource_attach_unknown_terminal_closes_4404(tmp_path: Path) -> None:
    """An unknown terminal id closes with 4404.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_nope/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_defunct_terminal_closes_4404(tmp_path: Path) -> None:
    """A registry entry with ``running=False`` closes with 4404.

    Defunct entries exist briefly between tmux session death and the
    registry's eviction sweep; attaching to one would race the
    cleanup. Closing 4404 lets the browser show the same "no such
    terminal" path the list endpoint shows.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    defunct = _make_running_instance("bash", "stale", tmp_path)
    defunct.running = False
    _seed_registry(registry, "conv_abc", defunct)
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_stale/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


def test_runner_resource_attach_dead_tmux_with_stale_flag_closes_4404(
    tmp_path: Path,
) -> None:
    """
    A stale ``running=True`` flag still closes with 4404 when tmux is gone.

    This pins the Claude-native exit bug: after Claude exits, the
    runner can still have an in-memory terminal entry marked running.
    Reattaching to that socket makes tmux print ``"no sessions"``.
    The attach route must probe tmux liveness first and surface the
    same terminal-gone close code the wrapper already treats as a
    normal end-of-session.

    :param tmp_path: Pytest tmp directory.
    """
    registry = TerminalRegistry()
    stale = _make_running_instance("bash", "stale", tmp_path)

    async def dead_tmux() -> bool:
        """
        Simulate tmux ``has-session`` reporting no live session.

        :returns: ``False`` after flipping the optimistic running flag.
        """
        stale.running = False
        return False

    stale.is_alive = dead_tmux  # type: ignore[method-assign]
    _seed_registry(registry, "conv_abc", stale)
    app = create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_stale/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    # 4404 is the wrapper's terminal-gone signal. A generic close
    # would look like a server bounce and restart the reconnect loop.
    assert exc_info.value.code == 4404
    assert stale.running is False


def test_runner_resource_attach_without_registry_closes_4404() -> None:
    """Without a registry wired in, the endpoint closes 4404 rather
    than crashing.

    The runner scaffold path (``create_runner_app()`` with no args)
    hits this branch. Production paths always pass a registry.
    """
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    with TestClient(app).websocket_connect(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/attach"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_bytes()

    assert exc_info.value.code == 4404


# ── Loopback direct-attach listener (runner/direct_attach.py) ─────────


def _make_direct_app(
    events: list[tuple[str, str, bool]],
) -> FastAPI:
    """A direct-attach app whose attach handler records its arguments.

    The stub stands in for the runner app's real
    ``terminal_resource_attach_ws`` so these tests pin the listener's
    auth gate and parameter forwarding without touching tmux.
    """

    async def stub_handler(
        websocket: object,
        session_id: str,
        terminal_id: str,
        read_only: bool = False,
    ) -> None:
        events.append((session_id, terminal_id, read_only))
        await websocket.accept()  # type: ignore[attr-defined]
        await websocket.close()  # type: ignore[attr-defined]

    return create_direct_attach_app(
        stub_handler,
        token="sekret-token",
        allowed_origins=frozenset({"https://app.example"}),
    )


def test_direct_attach_probe_accepts_valid_token_without_origin() -> None:
    """Non-browser handshakes (no Origin header) pass on the token alone."""
    app = _make_direct_app([])
    with TestClient(app).websocket_connect("/probe?token=sekret-token"):
        pass


def test_direct_attach_probe_accepts_allow_listed_origin() -> None:
    app = _make_direct_app([])
    with TestClient(app).websocket_connect(
        "/probe?token=sekret-token",
        headers={"origin": "https://app.example"},
    ):
        pass


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://127.8.9.10:5173",
        "https://[::1]:5173",
        "https://[::ffff:127.0.0.1]:5173",
    ],
)
def test_direct_attach_probe_accepts_loopback_dev_origin(origin: str) -> None:
    """A tokened same-machine dev UI may use the relay-free listener."""
    app = _make_direct_app([])
    with TestClient(app).websocket_connect(
        "/probe?token=sekret-token",
        headers={"origin": origin},
    ):
        pass


@pytest.mark.parametrize(
    "path",
    [
        "/probe",
        "/probe?token=wrong",
        "/v1/sessions/conv1/resources/terminals/t1/attach",
        "/v1/sessions/conv1/resources/terminals/t1/attach?token=wrong",
    ],
)
def test_direct_attach_rejects_missing_or_wrong_token(path: str) -> None:
    events: list[tuple[str, str, bool]] = []
    app = _make_direct_app(events)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(path):
            pass
    assert exc_info.value.code == 1008
    assert events == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost.evil.example:5173",
        "http://10.0.0.5:5173",
        "chrome-extension://localhost",
    ],
)
def test_direct_attach_rejects_foreign_origin_despite_valid_token(origin: str) -> None:
    """A DNS-rebinding-style page presents its own origin — refused."""
    events: list[tuple[str, str, bool]] = []
    app = _make_direct_app(events)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv1/resources/terminals/t1/attach?token=sekret-token",
            headers={"origin": origin},
        ):
            pass
    assert exc_info.value.code == 1008
    assert events == []


def test_direct_attach_forwards_attach_params_to_handler() -> None:
    """The attach wrapper hands session/terminal/read-only through unchanged."""
    events: list[tuple[str, str, bool]] = []
    app = _make_direct_app(events)
    with contextlib.suppress(WebSocketDisconnect):
        with TestClient(app).websocket_connect(
            "/v1/sessions/conv1/resources/terminals/terminal_bash_s1/attach"
            "?token=sekret-token&read_only=true",
            headers={"origin": "https://app.example"},
        ):
            pass
    assert events == [("conv1", "terminal_bash_s1", True)]


def test_allowed_origin_for_server_strips_path_and_keeps_port() -> None:
    assert (
        allowed_origin_for_server("https://omnigents.example.databricksapps.com/some/base")
        == "https://omnigents.example.databricksapps.com"
    )
    assert allowed_origin_for_server("http://127.0.0.1:6767") == "http://127.0.0.1:6767"
    assert allowed_origin_for_server("not a url") is None
    assert allowed_origin_for_server("ftp://example.com") is None


@pytest.mark.asyncio
async def test_direct_attach_listener_serves_probe_on_loopback() -> None:
    """The uvicorn listener binds 127.0.0.1:0 and answers the probe route."""
    import websockets

    events: list[tuple[str, str, bool]] = []
    listener = await start_direct_attach_listener(_make_direct_app(events))
    assert listener is not None, "listener failed to start on loopback"
    try:
        assert listener.port > 0
        # Valid token: the handshake completes (the route accepts then
        # closes, which is the probe contract).
        async with websockets.connect(f"ws://127.0.0.1:{listener.port}/probe?token=sekret-token"):
            pass
        # Wrong token: rejected before accept — an HTTP 403 handshake
        # denial, which the client surfaces as InvalidStatus.
        with pytest.raises(websockets.exceptions.InvalidStatus):
            async with websockets.connect(f"ws://127.0.0.1:{listener.port}/probe?token=wrong"):
                pass
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_direct_attach_listener_preserves_existing_log_handlers() -> None:
    """Starting the nested listener must not reconfigure runner logging."""

    class TrackingHandler(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.was_closed = False

        def emit(self, record: logging.LogRecord) -> None:
            pass

        def close(self) -> None:
            self.was_closed = True
            super().close()

    handler = TrackingHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    listener = None
    try:
        listener = await start_direct_attach_listener(_make_direct_app([]))
        assert listener is not None
        assert not handler.was_closed
    finally:
        if listener is not None:
            await listener.stop()
        root_logger.removeHandler(handler)
        handler.close()
