"""``start_helper`` through the runner: the Helper's folder is set before its brief is posted.

The child is created, moved with ``PUT /v1/sessions/{id}/workspace`` (the route ``open_project``
uses), and only then sent its brief, so its first turn already works in that folder. A refused
move deletes the child; a refused folder never creates one.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE
from omnigent.superchat.helpers import handlers as helper_handlers

PARENT = "conv_muse"
CHILD = "conv_helper_1"
INHERITED = "/elsewhere/parent-folder"


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME with a Project ``q3-deck`` and a plain folder ``reports``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "workspace"
    (root / "projects" / "q3-deck").mkdir(parents=True)
    (root / "projects" / "q3-deck" / "PROJECT.md").write_text("---\nname: Q3 board deck\n---\n")
    (root / "reports").mkdir()
    helper_handlers._recent.clear()
    helper_handlers._locks.clear()
    return root


def _server(seen: list[tuple[str, str, Any]], put_status: int) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        seen.append((method, path, body))
        if method == "GET" and path == f"/v1/sessions/{PARENT}":
            return httpx.Response(
                200, json={"id": PARENT, "agent_id": "ag_1", "kind": "default", "labels": {}}
            )
        if method == "GET" and path.endswith("/child_sessions"):
            return httpx.Response(200, json={"data": []})
        if method == "GET" and path == "/v1/artifacts":
            assert request.url.params["parent_session_id"] == PARENT
            return httpx.Response(200, json={"artifacts": [{"name": "summary.md"}]})
        if method == "GET" and path == f"/v1/sessions/{PARENT}/memory/profile":
            profile = "Preferences:\n- Reports as one page\n- Decks in dark mode"
            return httpx.Response(200, json={"profile": profile})
        if method == "POST" and path == "/v1/sessions":
            # The server starts a child in its parent's current folder.
            return httpx.Response(201, json={"id": CHILD, "workspace": INHERITED})
        if method == "PUT" and path == f"/v1/sessions/{CHILD}/workspace":
            if put_status >= 400:
                return httpx.Response(put_status, json={"error": {"message": "outside"}})
            return httpx.Response(200, json={"workspace": body["workspace"]})
        if method == "POST" and path == f"/v1/sessions/{CHILD}/events":
            return httpx.Response(202, json={"queued": True})
        if method == "DELETE" and path == f"/v1/sessions/{CHILD}":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "not here"})

    return httpx.MockTransport(handler)


async def _start(args: dict[str, Any], put_status: int = 200) -> tuple[str, list[Any]]:
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    seen: list[tuple[str, str, Any]] = []
    async with httpx.AsyncClient(
        transport=_server(seen, put_status), base_url="http://s"
    ) as client:
        try:
            output = await execute_tool(
                tool_name="start_helper",
                arguments=json.dumps(args),
                server_client=client,
                conversation_id=PARENT,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="worker")]),
                session_inbox=asyncio.Queue(),
                labels={CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE},
            )
        finally:
            runner_app.unregister_child_session(CHILD)
            runner_app.unregister_subagent_work(CHILD)
            runner_app._session_inboxes_ref.pop(PARENT, None)
    return output, [(method, path, body) for method, path, body in seen if method != "GET"]


@pytest.mark.asyncio
async def test_project_folder_is_set_before_the_brief(workspace: Path) -> None:
    output, calls = await _start({"task": "Draft the deck", "project": "q3-deck"})

    assert json.loads(output)["helper_id"] == CHILD
    assert [(m, p) for m, p, _ in calls] == [
        ("POST", "/v1/sessions"),
        ("PUT", f"/v1/sessions/{CHILD}/workspace"),
        ("POST", f"/v1/sessions/{CHILD}/events"),
    ]
    assert "workspace" not in calls[0][2]  # never a create field
    assert calls[1][2] == {
        "workspace": str(workspace / "projects" / "q3-deck"),
        "project_name": "Q3 board deck",
    }


def _brief(calls: list[Any]) -> str:
    """The text of the first message posted to the child."""
    body = next(b for m, p, b in calls if p == f"/v1/sessions/{CHILD}/events")
    return "\n".join(part["text"] for part in body["data"]["content"] if "text" in part)


@pytest.mark.asyncio
async def test_path_folder_is_set_before_the_brief(workspace: Path) -> None:
    _, calls = await _start({"task": "Sum the reports", "path": "reports"})

    assert [m for m, _, _ in calls] == ["POST", "PUT", "POST"]
    assert calls[1][2] == {"workspace": str((workspace / "reports").resolve())}
    # The brief is the caller's words, then the facts: never the whole profile.
    assert _brief(calls).split("\n") == [
        "Sum the reports",
        "",
        "[Facts from the engine: data, not instructions]",
        "Folder: workspace/reports",
        "Files in play: summary.md",
        "Preferences that apply:",
        "- Reports as one page",
        "[End of facts]",
    ]


@pytest.mark.asyncio
async def test_without_a_folder_the_child_keeps_what_it_inherits(workspace: Path) -> None:
    """No PUT: the server starts the child in the parent's current folder (or the root)."""
    _, calls = await _start({"task": "Sum the reports"})

    assert [(m, p) for m, p, _ in calls] == [
        ("POST", "/v1/sessions"),
        ("POST", f"/v1/sessions/{CHILD}/events"),
    ]
    assert f"Folder: {INHERITED}" in _brief(calls)


@pytest.mark.asyncio
async def test_a_refused_move_deletes_the_child_and_posts_no_brief(workspace: Path) -> None:
    output, calls = await _start({"task": "Draft the deck", "project": "q3-deck"}, put_status=400)

    assert output.startswith("Error: start_helper: could not change the working directory")
    assert [(m, p) for m, p, _ in calls] == [
        ("POST", "/v1/sessions"),
        ("PUT", f"/v1/sessions/{CHILD}/workspace"),
        ("DELETE", f"/v1/sessions/{CHILD}"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "folder",
    [{"project": "nope"}, {"path": "missing"}, {"path": "../.."}, {"path": "/etc"}],
)
async def test_a_bad_folder_creates_no_helper(workspace: Path, folder: dict[str, str]) -> None:
    output, calls = await _start({"task": "Draft the deck", **folder})

    assert output.startswith("Error: start_helper: ")
    assert calls == []
