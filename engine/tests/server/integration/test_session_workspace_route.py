"""``PUT /v1/sessions/{id}/workspace``: change a session's working directory."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.routes import _workspace_validation as wv
from omnigent.server.routes._sessions.helpers import _RunnerForwardResult
from omnigent.server.routes.sessions import routes_workspace
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_HOST = "3f866cafac81246fb60ae6ceb1a738da"
_ROOT = "/home/superchat/workspace"
_PROJECT = f"{_ROOT}/projects/q3-deck"


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    """A second handle on the app's database, to seed and read session rows."""
    return SqlAlchemyConversationStore(db_uri)


async def _host_session(client: httpx.AsyncClient, store: SqlAlchemyConversationStore) -> str:
    """A session bound to a host with the workspace root as its working directory."""
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": []}
    )
    assert created.status_code == 201, created.text
    session_id: str = created.json()["id"]
    store.set_host_id(session_id, _HOST, _ROOT)
    return session_id


def _stored_workspace(store: SqlAlchemyConversationStore, session_id: str) -> str | None:
    conv = store.get_conversation(session_id)
    return conv.workspace if conv else None


@pytest.fixture()
def accept_workspace(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Skip the host round-trip: the validator returns the path it was given."""
    validate = AsyncMock(side_effect=lambda **kw: kw["workspace"])
    monkeypatch.setattr(routes_workspace, "_validate_session_workspace", validate)
    return validate


async def test_put_stores_workspace_then_tells_the_runner(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is updated before the runner is told to forget its cached copy."""
    session_id = await _host_session(client, store)
    seen: list[tuple[str | None, Any]] = []

    async def forward(sid: str, router: object, event: dict[str, Any], **_: Any) -> None:
        seen.append((_stored_workspace(store, sid), event))

    monkeypatch.setattr(routes_workspace, "_forward_session_change_to_runner", forward)

    resp = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": _PROJECT})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"workspace": _PROJECT}
    assert _stored_workspace(store, session_id) == _PROJECT
    assert seen == [(_PROJECT, {"type": "workspace_change"})]
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.json()["workspace"] == _PROJECT


async def test_put_records_the_project_name_and_clears_it_on_leaving(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opened Project's card name is kept as a label; leaving the Project drops it."""
    session_id = await _host_session(client, store)
    monkeypatch.setattr(
        routes_workspace, "_forward_session_change_to_runner", AsyncMock(return_value=None)
    )

    opened = await client.put(
        f"/v1/sessions/{session_id}/workspace",
        json={"workspace": _PROJECT, "project_name": "  Q3   board deck "},
    )
    assert opened.status_code == 200, opened.text
    conv = store.get_conversation(session_id)
    assert conv is not None and conv.labels["omnigent.project.name"] == "Q3 board deck"

    left = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": _ROOT})
    assert left.status_code == 200, left.text
    conv = store.get_conversation(session_id)
    assert conv is not None and "omnigent.project.name" not in conv.labels


async def test_put_requires_the_owner(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An editor is refused; nothing is stored and the runner is not told."""
    session_id = await _host_session(client, store)
    deny = AsyncMock(side_effect=OmnigentError("no", code=ErrorCode.FORBIDDEN))
    forward = AsyncMock()
    monkeypatch.setattr(routes_workspace, "_require_access", deny)
    monkeypatch.setattr(routes_workspace, "_forward_session_change_to_runner", forward)

    resp = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": _PROJECT})

    assert resp.status_code == 403
    assert deny.await_args.args[2] == LEVEL_OWNER
    assert _stored_workspace(store, session_id) == _ROOT
    forward.assert_not_awaited()


async def test_put_refused_path_changes_nothing(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path the boundary check refuses is a 400 with the workspace and runner untouched."""
    session_id = await _host_session(client, store)
    refuse = AsyncMock(
        side_effect=OmnigentError(
            "outside the agent's required path", code=ErrorCode.INVALID_INPUT
        )
    )
    forward = AsyncMock()
    monkeypatch.setattr(routes_workspace, "_validate_session_workspace", refuse)
    monkeypatch.setattr(routes_workspace, "_forward_session_change_to_runner", forward)

    resp = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": "/etc"})

    assert resp.status_code == 400
    assert _stored_workspace(store, session_id) == _ROOT
    forward.assert_not_awaited()


async def test_put_without_a_host_is_refused(
    client: httpx.AsyncClient, accept_workspace: AsyncMock
) -> None:
    """A session with no host workspace has nothing to change."""
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": []}
    )
    session_id = created.json()["id"]

    resp = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": _PROJECT})

    assert resp.status_code == 400


async def test_runner_rejection_is_surfaced(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the runner answers with an error the caller is told, not left with a stale cache."""
    session_id = await _host_session(client, store)
    failed = AsyncMock(return_value=_RunnerForwardResult(status_code=500, body=""))
    monkeypatch.setattr(routes_workspace, "_forward_session_change_to_runner", failed)

    resp = await client.put(f"/v1/sessions/{session_id}/workspace", json={"workspace": _PROJECT})

    assert resp.status_code >= 500


@pytest.fixture()
def fake_host_fs(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Replace the host.stat round-trip with a dict: path -> canonical path (absent: missing)."""
    fs: dict[str, str] = {}

    async def stat(*, host_registry: object, host_conn: object, path: str) -> dict[str, Any]:
        canonical = fs.get(path)
        return {
            "status": "ok",
            "exists": canonical is not None,
            "type": "directory" if canonical else None,
            "canonical_path": canonical,
        }

    monkeypatch.setattr(wv, "_ask_host_stat", stat)
    return fs


class _Registry:
    def get(self, host_id: str) -> object:
        return object()


async def _validate(workspace: str) -> str:
    return await wv.validate_workspace(
        host_registry=_Registry(),  # type: ignore[arg-type]
        host_id="host_test",
        workspace=workspace,
        spec_cwd=_ROOT,
    )


async def test_boundary_allows_a_folder_inside_the_workspace_root(
    fake_host_fs: dict[str, str],
) -> None:
    fake_host_fs.update({_ROOT: _ROOT, _PROJECT: _PROJECT})
    assert await _validate(_PROJECT) == _PROJECT


async def test_boundary_refuses_a_symlink_or_dotdot_that_leaves_the_root(
    fake_host_fs: dict[str, str],
) -> None:
    """The host resolves the path first, so a link out of the root is judged by where it lands."""
    sneaky = f"{_ROOT}/projects/link"
    fake_host_fs.update({_ROOT: _ROOT, sneaky: "/etc", f"{_ROOT}/../etc": "/etc"})
    for path in (sneaky, f"{_ROOT}/../etc"):
        with pytest.raises(wv.WorkspaceValidationError, match="outside"):
            await _validate(path)


async def test_a_helper_starts_in_its_parents_current_directory(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child on the parent's runner gets the parent's working directory as of now."""
    monkeypatch.setattr(
        routes_workspace, "_forward_session_change_to_runner", AsyncMock(return_value=None)
    )
    parent_id = await _host_session(client, store)
    store.set_runner_id(parent_id, "3f866cafac81246fb60ae6ceb1a738dd")
    moved = await client.put(f"/v1/sessions/{parent_id}/workspace", json={"workspace": _PROJECT})
    assert moved.status_code == 200, moved.text
    parent = store.get_conversation(parent_id)
    assert parent is not None

    created = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent.agent_id,
            "parent_session_id": parent_id,
            "initial_items": [],
        },
    )

    assert created.status_code == 201, created.text
    assert _stored_workspace(store, created.json()["id"]) == _PROJECT


async def test_a_helper_is_moved_on_its_parents_host_and_stays_unbound(
    client: httpx.AsyncClient,
    store: SqlAlchemyConversationStore,
    accept_workspace: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child with no host of its own is checked on its parent's host; its row keeps no host."""
    monkeypatch.setattr(
        routes_workspace, "_forward_session_change_to_runner", AsyncMock(return_value=None)
    )
    parent_id = await _host_session(client, store)
    parent = store.get_conversation(parent_id)
    assert parent is not None
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": parent.agent_id, "parent_session_id": parent_id, "initial_items": []},
    )
    child_id = created.json()["id"]

    resp = await client.put(f"/v1/sessions/{child_id}/workspace", json={"workspace": _PROJECT})

    assert resp.status_code == 200, resp.text
    assert accept_workspace.await_args.kwargs["host_id"] == _HOST
    child = store.get_conversation(child_id)
    assert child is not None
    assert (child.host_id, child.workspace) == (None, _PROJECT)
