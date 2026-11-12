"""Tests for the ``schedule`` tool surface.

Covers the runner-side half:

- ``_execute_scheduled_task_tool``: the create/list/update/delete actions proxy
  the correct ``/v1/scheduled-tasks`` REST verb + payload over ``server_client``,
  pass the JSON body through verbatim, and translate a server 4xx/5xx and a
  missing field into clean error JSON.
- Registration: ``schedule`` is always registered by ``ToolManager`` (no spec opt-in).
"""

from __future__ import annotations

import json

import pytest

from omnigent.runner.tool_dispatch import (
    _execute_scheduled_task_tool,
)

_TASK_ID = "0123456789abcdef0123456789abcdef"


class _Resp:
    def __init__(self, *, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    """Records the verb/url/json of each call and returns a scripted response."""

    def __init__(self, response: _Resp | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self._response = response or _Resp(body={"id": "t1"})

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("GET", url, None))
        return self._response

    async def post(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        self.calls.append(("POST", url, json))
        return self._response

    async def patch(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        self.calls.append(("PATCH", url, json))
        return self._response

    async def delete(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("DELETE", url, None))
        return self._response


@pytest.mark.asyncio
async def test_create_posts_payload() -> None:
    client = _RecordingClient(_Resp(body={"id": "t1", "name": "nightly"}))
    out = await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "create",
                "name": "nightly",
                "prompt": "go",
                "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                "agent_id": "ag_1",
                "workspace": "/repo",
                "host_id": "host_1",
                "permission_mode": "bypassPermissions",
                "model_override": "any-model",
                "base_branch": "main",
                "unexpected": "dropped",
            }
        ),
        server_client=client,
    )
    verb, url, body = client.calls[0]
    assert (verb, url) == ("POST", "/v1/scheduled-tasks")
    # Fields outside the schema (agent, host, workspace, permission mode, model) keep the
    # server's defaults: they are never forwarded.
    assert body == {
        "name": "nightly",
        "prompt": "go",
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    }
    assert json.loads(out)["id"] == "t1"


@pytest.mark.asyncio
async def test_list_gets() -> None:
    client = _RecordingClient(_Resp(body={"scheduled_tasks": []}))
    out = await _execute_scheduled_task_tool(json.dumps({"action": "list"}), server_client=client)
    assert client.calls[0] == ("GET", "/v1/scheduled-tasks", None)
    assert json.loads(out) == {"scheduled_tasks": []}


@pytest.mark.asyncio
async def test_update_patches_by_id() -> None:
    client = _RecordingClient(_Resp(body={"id": "t1", "state": "paused"}))
    await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "update",
                "scheduled_task_id": _TASK_ID,
                "state": "paused",
                "workspace": "/repo2",
                "agent_id": "ag_pi",
                "max_cost_usd": 2.5,
            }
        ),
        server_client=client,
    )
    verb, url, body = client.calls[0]
    assert (verb, url) == ("PATCH", f"/v1/scheduled-tasks/{_TASK_ID}")
    assert body == {"state": "paused"}


@pytest.mark.asyncio
async def test_delete_by_id() -> None:
    client = _RecordingClient(_Resp(body={"deleted": True, "id": "t1"}))
    await _execute_scheduled_task_tool(
        json.dumps({"action": "delete", "scheduled_task_id": _TASK_ID.upper()}),
        server_client=client,
    )
    assert client.calls[0] == ("DELETE", f"/v1/scheduled-tasks/{_TASK_ID}", None)


@pytest.mark.asyncio
async def test_update_rejects_path_confusion_task_id() -> None:
    client = _RecordingClient()
    out = await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "update",
                "scheduled_task_id": "../0123456789abcdef0123456789abcdef",
                "state": "paused",
            }
        ),
        server_client=client,
    )
    assert "32-character hex" in json.loads(out)["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_update_without_id_errors() -> None:
    client = _RecordingClient()
    out = await _execute_scheduled_task_tool(
        json.dumps({"action": "update", "state": "paused"}), server_client=client
    )
    # The error names the missing field and the action.
    assert json.loads(out)["error"] == "schedule update needs 'scheduled_task_id'"
    assert client.calls == []  # never hit the server


@pytest.mark.asyncio
async def test_server_error_becomes_clean_json() -> None:
    client = _RecordingClient(_Resp(status_code=400, body={"error": {"message": "bad rrule"}}))
    out = await _execute_scheduled_task_tool(
        json.dumps({"action": "create", "name": "x", "prompt": "p", "rrule": "FREQ=SECONDLY"}),
        server_client=client,
    )
    assert "server returned 400" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_no_server_client_errors() -> None:
    out = await _execute_scheduled_task_tool('{"action": "list"}', server_client=None)
    assert "requires server access" in json.loads(out)["error"]


def test_tools_registered_without_spec_optin() -> None:
    """``schedule`` registers on a minimal spec (always-on, like policy)."""
    from omnigent.spec.types import AgentSpec
    from omnigent.tools.manager import ToolManager

    mgr = ToolManager(AgentSpec(spec_version=1))
    names = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert "schedule" in names


def test_schema_leaves_out_the_fields_no_caller_sets() -> None:
    from omnigent.tools.builtins.scheduled_tasks import ScheduleTool

    schema = ScheduleTool().get_schema()["function"]["parameters"]
    properties = schema["properties"]
    for dropped in (
        "permission_mode",
        "execution_target",
        "host_id",
        "workspace",
        "max_cost_usd",
        "model_override",
        "reasoning_effort",
        "agent_id",
    ):
        assert dropped not in properties
    # A flat schema requires only the action; the handler checks each action's fields.
    assert schema["required"] == ["action"]
    assert properties["action"]["enum"] == ["create", "list", "update", "delete"]


class _KindClient(_RecordingClient):
    """Serves ``GET /v1/sessions/{id}`` with a scripted session kind."""

    def __init__(self, kind: str) -> None:
        super().__init__(_Resp(body={"id": "t1"}))
        self._kind = kind

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        if url.startswith("/v1/sessions/"):
            return _Resp(body={"kind": self._kind, "parent_session_id": None})
        return await super().get(url, timeout=timeout)


@pytest.mark.asyncio
async def test_sub_agent_caller_is_refused() -> None:
    client = _KindClient("sub_agent")
    out = await _execute_scheduled_task_tool(
        json.dumps({"action": "create", "name": "n", "prompt": "go", "rrule": "FREQ=DAILY"}),
        server_client=client,
        conversation_id="conv_child",
    )
    assert json.loads(out)["error"] == "schedule create is not available to sub-agent sessions"
    assert not [c for c in client.calls if c[0] == "POST"]


@pytest.mark.asyncio
async def test_sub_agent_caller_may_list() -> None:
    client = _KindClient("sub_agent")
    out = await _execute_scheduled_task_tool(
        '{"action": "list"}', server_client=client, conversation_id="conv_child"
    )
    assert "error" not in json.loads(out)
    assert [c for c in client.calls if c[0] == "GET" and "scheduled-tasks" in c[1]]


@pytest.mark.asyncio
async def test_self_parent_resolves_to_calling_session() -> None:
    client = _KindClient("default")
    await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "create",
                "name": "feed",
                "prompt": "go",
                "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                "parent_session_id": "self",
                "agent_type": "reviewer",
            }
        ),
        server_client=client,
        conversation_id="conv_chat",
    )
    _, _, body = next(c for c in client.calls if c[0] == "POST")
    assert body is not None
    assert body["parent_session_id"] == "conv_chat"
    assert body["agent_type"] == "reviewer"


@pytest.mark.asyncio
async def test_create_binds_to_calling_chat_as_worker() -> None:
    """A new task schedules for the chat that made it, even when an agent is named."""
    client = _KindClient("default")
    await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "create",
                "name": "remind",
                "prompt": "go",
                "rrule": "FREQ=DAILY;BYHOUR=9",
                "agent_id": "ag_1",
            }
        ),
        server_client=client,
        conversation_id="conv_chat",
    )
    _, _, body = next(c for c in client.calls if c[0] == "POST")
    assert body is not None
    assert (body["parent_session_id"], body["agent_type"]) == ("conv_chat", "worker")
    assert "agent_id" not in body


@pytest.mark.asyncio
async def test_update_forwards_a_new_rrule_and_timezone_and_nothing_else() -> None:
    # A rule change must reach the server: dropping it would send an empty PATCH.
    client = _RecordingClient(_Resp(body={"id": "t1"}))
    await _execute_scheduled_task_tool(
        json.dumps(
            {
                "action": "update",
                "scheduled_task_id": _TASK_ID,
                "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0",
                "timezone": "Europe/Paris",
                "schedule": "dropped: the Muse writes the rule itself",
            }
        ),
        server_client=client,
    )
    assert client.calls[0][2] == {
        "rrule": "FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0",
        "timezone": "Europe/Paris",
    }
