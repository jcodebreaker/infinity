"""``message_helper`` through the runner: the steer reaches the Helper's running turn.

The Helper is a child session; the message is posted to its events (where the runner steers it
into the turn it is running) and the Helper's one result still comes back through the inbox.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE

PARENT = "conv_muse"
HELPER = "conv_helper_1"


def _server(
    posts: list[dict[str, Any]], rows: list[dict[str, Any]], idle: bool = False
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"/v1/sessions/{PARENT}":
            return httpx.Response(200, json={"labels": {"omnigent.turn_actor": "alice@x.test"}})
        if request.method == "GET" and path == f"/v1/sessions/{PARENT}/child_sessions":
            return httpx.Response(200, json={"data": rows})
        if request.method == "GET" and path == f"/v1/sessions/{HELPER}":
            return httpx.Response(
                200,
                json={
                    "id": HELPER,
                    "parent_session_id": PARENT,
                    "title": "worker:Deck",
                    "busy": True,
                },
            )
        if request.method == "POST" and path == f"/v1/sessions/{HELPER}/events":
            posts.append(json.loads(request.content))
            if idle:  # the Helper's turn ended before the note reached it
                return httpx.Response(409, json={"error": "not_running"})
            return httpx.Response(202, json={"queued": True, "delivery": "steer"})
        return httpx.Response(404, json={"error": str(request.url)})

    return httpx.MockTransport(handler)


async def _call(
    arguments: dict[str, Any],
    rows: list[dict[str, Any]],
    idle: bool = False,
    entry_status: str = "running",
    after: list[Any] | None = None,
) -> tuple[str, list[Any]]:
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    posts: list[dict[str, Any]] = []
    entry = runner_app.register_subagent_work(
        parent_session_id=PARENT, child_session_id=HELPER, agent="worker", title="Deck"
    )
    entry.status = entry_status
    async with httpx.AsyncClient(
        transport=_server(posts, rows, idle), base_url="http://s"
    ) as client:
        try:
            output = await execute_tool(
                tool_name="message_helper",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id=PARENT,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                session_inbox=asyncio.Queue(),
                labels={CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE},
            )
        finally:
            if after is not None:
                after.append(runner_app.get_subagent_work(HELPER))
            runner_app.unregister_subagent_work(HELPER)
            runner_app._session_inboxes_ref.pop(PARENT, None)
    return output, posts


@pytest.mark.asyncio
async def test_the_message_is_posted_into_the_running_helper_without_a_new_one() -> None:
    rows = [{"id": HELPER, "title": "worker:Deck", "busy": True}]

    output, posts = await _call({"message": "use the blue template"}, rows)

    assert json.loads(output)["sent"] is True
    assert len(posts) == 1  # one message to the Helper, no second task started
    assert posts[0]["data"]["content"][0]["text"] == "use the blue template"
    assert posts[0]["if_running"] is True  # the Helper refuses it if its turn is over


@pytest.mark.asyncio
async def test_a_helper_whose_turn_ended_meanwhile_refuses_the_note() -> None:
    rows = [{"id": HELPER, "title": "worker:Deck", "busy": True}]

    output, posts = await _call({"message": "use the blue template"}, rows, idle=True)

    assert len(posts) == 1
    assert output == (
        "Error: message_helper: that Helper is not running; its result has arrived or will"
    )


@pytest.mark.asyncio
async def test_a_helper_that_already_finished_is_not_started_again() -> None:
    rows = [{"id": HELPER, "title": "worker:Deck", "busy": False, "current_task_status": "done"}]

    output, posts = await _call({"message": "use the blue template"}, rows)

    assert output.startswith("Error: message_helper: no Helper is running")
    assert posts == []


@pytest.mark.asyncio
async def test_a_note_never_registers_new_work_for_the_helper() -> None:
    # The tracked turn reads as ended locally while the child still took the note into its
    # running turn: no fresh work entry may appear, or a phantom result would be awaited.
    rows = [{"id": HELPER, "title": "worker:Deck", "busy": True}]
    seen: list[Any] = []
    output, _posts = await _call(
        {"message": "use the blue template"}, rows, entry_status="done", after=seen
    )
    assert json.loads(output)["sent"] is True
    assert seen[0] is not None and seen[0].status == "done"  # the same, untouched entry
