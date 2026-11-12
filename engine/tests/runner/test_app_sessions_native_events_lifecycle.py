"""Tests for native session lifecycle, status, interrupt, and stop events."""

from __future__ import annotations

from typing import Any

from omnigent.runner import create_runner_app
from tests.runner.helpers import NullServerClient


class _EventRecordingServerClient(NullServerClient):
    """Records Omnigent ``external_*`` event POSTs for assertion.

    Subclasses :class:`NullServerClient` so all other runner→AP calls still
    succeed silently; captures ``external_conversation_item`` bodies so a
    test can assert that NO interrupt marker was persisted, and
    ``external_mcp_startup`` bodies so Stop tests can assert the cancelled
    MCP map was published.
    """

    def __init__(self) -> None:
        self.posted_items: list[dict[str, Any]] = []
        self.posted_mcp_startup: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record ``external_conversation_item`` / ``external_mcp_startup`` bodies."""
        del url
        body = kwargs.get("json")
        if isinstance(body, dict) and body.get("type") == "external_conversation_item":
            self.posted_items.append(body.get("data") or {})
        if isinstance(body, dict) and body.get("type") == "external_mcp_startup":
            self.posted_mcp_startup.append(body.get("data") or {})
        return self._Response()


class _RecordingCodexAppServerClient:
    """
    Test double for Codex app-server JSON-RPC controls.

    :param transport: Transport passed to
        :func:`omnigent.harnesses.codex_native.app_server.client_for_transport`, e.g.
        ``"ws://127.0.0.1:1234"``.
    :param client_name: App-server client name, e.g.
        ``"omnigent-codex-native-runner"``.
    """

    def __init__(self, transport: str, client_name: str) -> None:
        self.transport = transport
        self.client_name = client_name
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.model_list_responses: list[dict[str, Any]] = []

    async def connect(self) -> None:
        """
        Mark the fake client connected.

        :returns: None.
        """
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture a JSON-RPC request.

        :param method: JSON-RPC method, e.g. ``"turn/interrupt"``.
        :param params: JSON-RPC params, e.g.
            ``{"threadId": "thread_123", "turnId": "turn_123"}``.
        :returns: Empty successful JSON-RPC result.
        """
        self.requests.append((method, params))
        if method == "model/list" and self.model_list_responses:
            return self.model_list_responses.pop(0)
        return {"result": {}}

    async def close(self) -> None:
        """
        Mark the fake client closed.

        :returns: None.
        """
        self.closed = True


def test_interrupted_sessions_isolated_per_app_instance() -> None:
    """
    Each ``create_runner_app()`` gets its own ``_interrupted_sessions`` set.

    Regression guard: when ``_interrupted_sessions`` was a module-global,
    interrupt flags leaked between distinct app instances in the same
    process — app1 flagging a conv made app2 append a bogus
    ``[System: interrupted]`` marker on a normal turn for the same conv id.
    Keeping the set closure-local (exposed on ``app.state`` only for test
    inspection) prevents that.
    """
    app1 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app2 = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]

    # Distinct objects: a shared module-global would make these identical, so
    # a flag added to one app would be visible from the other.
    assert app1.state.interrupted_sessions is not app2.state.interrupted_sessions, (
        "Each app instance must own its _interrupted_sessions set; if they are "
        "the same object, the set is module-global again and flags leak across apps."
    )

    app1.state.interrupted_sessions.add("8af356d908005a65f872c246158c6293")
    assert "8af356d908005a65f872c246158c6293" not in app2.state.interrupted_sessions, (
        "app2 must not observe app1's interrupt flag. If it does, "
        "_interrupted_sessions is shared process-global state and a stale flag "
        "would fire a bogus [System: interrupted] marker on app2's next turn."
    )
