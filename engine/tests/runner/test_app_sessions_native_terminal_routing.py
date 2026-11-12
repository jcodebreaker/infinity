"""Tests for native terminal ensure routing and REPL terminal creation."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.terminal import TerminalInstance
from omnigent.terminals import TerminalRegistry


@dataclass
class _EnsureTerminalCase:
    """
    One routing case for the claude-native ``create_session_terminal``
    ensure-path branch.

    :param case_id: Human-readable id for the parametrize label.
    :param body: The ``POST /resources/terminals`` JSON body.
    :param existing: Whether a live ``claude``/``main`` terminal already
        exists (drives the stubbed ``get_terminal_resource``).
    :param expect_auto_create: Whether the request must route to
        ``_auto_create_claude_terminal`` (the ensure path).
    :param expect_launch: Whether the request must route to the generic
        terminal launch path instead.
    :param expect_name: ``name`` of the resource the route must return —
        identifies which collaborator produced the response.
    """

    case_id: str
    body: dict[str, object]
    existing: bool
    expect_auto_create: bool
    expect_launch: bool
    expect_name: str


@dataclass
class _RecordedPatch:
    """
    A PATCH captured from the REPL terminal auto-create helper.

    :param url: Request path, e.g. ``"/v1/sessions/11c50cd73e9c32ccb0af5b9db291db8b"``.
    :param json: JSON body, e.g. ``{"labels": {"omnigent.ui": "terminal"}}``.
    """

    url: str
    json: dict[str, Any]


@pytest.mark.asyncio
async def test_dead_registered_pane_close_does_not_restore_running(
    tmp_path: Path,
) -> None:
    """A dead-pane close no longer mutates the advisory ``running`` flag.

    ``is_alive()`` sets ``instance.running = False`` as a side effect when
    the pane is dead. ``TerminalInstance.close()`` now uses the private socket
    to decide whether to issue ``tmux kill-server``, so the self-heal path does
    not need to falsify this liveness state before closing the stale entry.

    This test exercises the contract directly on ``TerminalRegistry.close``
    with a tracking ``close()`` stub that captures ``running`` at call time.

    :param tmp_path: Temporary directory fixture.
    :returns: None.
    """
    sid = "deadbeef12345678deadbeef12345678"
    registry = TerminalRegistry()
    dead_instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "dead.sock",
        private_dir=tmp_path / "dead_private",
    )
    dead_instance.running = True
    (tmp_path / "dead_private").mkdir(exist_ok=True)

    async def _fake_is_alive() -> bool:
        dead_instance.running = False
        return False

    dead_instance.is_alive = _fake_is_alive  # type: ignore[assignment]

    running_at_close: list[bool] = []

    async def _tracking_close() -> None:
        running_at_close.append(dead_instance.running)

    dead_instance.close = _tracking_close  # type: ignore[assignment]

    with registry._lock:
        registry._by_conversation[sid] = {("claude", "main"): dead_instance}
        registry._instance_locks[(sid, "claude", "main")] = threading.Lock()

    alive = await dead_instance.is_alive()
    assert alive is False
    assert dead_instance.running is False, "is_alive() should set running=False"

    await registry.close(sid, "claude", "main")

    assert len(running_at_close) == 1, (
        f"Expected close() to be called once; got {len(running_at_close)} calls"
    )
    assert running_at_close[0] is False
    assert registry.get(sid, "claude", "main") is None, (
        "Stale entry must be removed from the registry"
    )
