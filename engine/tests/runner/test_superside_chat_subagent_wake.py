"""Tests for the superside-chat sub-agent wake (slice S3).

On top of the plain-mode wake (``[System: ... waiting in inbox]``, covered by
``test_stranded_wake_retry_on_reconnect.py``), a ``superside_chat`` work entry's
wake notice inlines the sub-agent's Result itself instead of only announcing
"N results waiting".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.context.labels import CONTEXT_MODE_LABEL, SUPERSIDE_CHAT_MODE_VALUE
from omnigent.runner import create_runner_app
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _RecordingServerClient(NullServerClient):
    """Fake server client: serves fixed session snapshots and one parent's Helpers, records
    every POST, and applies label PATCHes to those Helpers as the sessions API does."""

    def __init__(
        self,
        *,
        snapshots: dict[str, dict[str, Any]] | None = None,
        children: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._snapshots = snapshots or {}
        self.children = children or {}
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        request = httpx.Request("GET", f"http://runner.test{url}")
        for session_id, snapshot in self._snapshots.items():
            if url == f"/v1/sessions/{session_id}":
                return httpx.Response(200, request=request, json=snapshot)
        if url.endswith("/child_sessions"):
            payload = {"data": list(self.children.values()), "has_more": False}
            return httpx.Response(200, request=request, json=payload)
        return await super().get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        body = kwargs.get("json")
        if isinstance(body, dict):
            self.posts.append((url, body))
        request = httpx.Request("POST", f"http://runner.test{url}")
        return httpx.Response(200, request=request, json={})

    async def patch(self, url: str, **kwargs: Any) -> Any:
        child = self.children.get(url.rsplit("/", 1)[-1])
        if child is not None:
            child["labels"].update((kwargs.get("json") or {}).get("labels") or {})
        request = httpx.Request("PATCH", f"http://runner.test{url}")
        return httpx.Response(200, request=request, json={})


def _complete_child(app: Any, child_id: str, output: str) -> None:
    """End the child's work as the runner does when a Helper's last turn completes."""
    app.state.mark_subagent_terminal_and_wake(child_id, status="completed", output=output)


def _notice_text(post: tuple[str, dict[str, Any]]) -> str:
    _url, body = post
    content = body["data"]["content"]
    return str(content[0]["text"])


@pytest.mark.asyncio
async def test_superside_chat_wake_inlines_result_once(_no_wake_backoff: list[float]) -> None:
    """A ``superside_chat`` entry's wake carries the Result, not 'waiting in inbox', and
    delivers it once: a runner restarted afterwards does not post it to the Conversation again.
    """
    from omnigent.runner import app as runner_app

    parent_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    child_id = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5"
    work_id = "subagent_a1b2c3d4e5f6"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    child = {
        "id": child_id,
        "tool": "reviewer",
        "session_name": "auth",
        "current_task_status": "completed",
        "labels": {runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: work_id},
    }
    server_client = _RecordingServerClient(children={child_id: child})
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="reviewer",
        title="auth",
        work_id=work_id,
        superside_chat=True,
    )
    try:
        async with _runner_client(app):
            _complete_child(app, child_id, "FINDINGS: the vault key rotated on March 3.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY not in child["labels"]:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no delivered wake observed within 5s")
                await asyncio.sleep(0.01)

        assert len(server_client.posts) == 1
        url, body = server_client.posts[0]
        assert url == f"/v1/sessions/{parent_id}/events"
        # Marked structurally, so nothing downstream sniffs the text to tell it from the person.
        assert body["data"]["is_system_notice"] is True
        text = _notice_text(server_client.posts[0])
        assert "FINDINGS: the vault key rotated on March 3." in text
        assert "waiting in inbox" not in text

        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.wake_inlined is True
        # The Conversation holds the Result, so the receipt names this dispatch.
        assert child["labels"][runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY] == work_id

        # The host goes away while idle; a fresh runner starts with none of that memory.
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._subagent_recovery_done.discard(parent_id)
        restarted = create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
            server_client=server_client,  # type: ignore[arg-type]
        )
        async with _runner_client(restarted):
            await restarted.state.recover_undrained_subagent_results(parent_id)
            await asyncio.sleep(0.05)

        assert len(server_client.posts) == 1
        assert runner_app._session_inboxes_ref[parent_id].empty()
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._subagent_recovery_done.discard(parent_id)


@pytest.mark.asyncio
async def test_a_delivered_helper_result_adds_no_journal_line(
    _no_wake_backoff: list[float], _isolated_knowledge_workspace: Path
) -> None:
    """The Round's one journal entry names the Helper (ADR 0029); the wake itself writes none."""
    from omnigent.runner import app as runner_app

    parent_id = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
    child_id = "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5"
    server_client = _RecordingServerClient()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    runner_app._session_inboxes_ref[parent_id] = asyncio.Queue()
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="reviewer",
        title="auth",
        superside_chat=True,
    )
    try:
        async with _runner_client(app):
            _complete_child(app, child_id, "FINDINGS: the vault key rotated on March 3.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while not server_client.posts:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no wake POST observed within 5s")
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)

        assert not list((_isolated_knowledge_workspace / "memory" / "journal").glob("*.md"))
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_plain_mode_wake_is_unchanged(_no_wake_backoff: list[float]) -> None:
    """A non-superside-chat entry keeps the plain 'N results waiting' notice."""
    from omnigent.runner import app as runner_app

    parent_id = "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"
    child_id = "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1"
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    server_client = _RecordingServerClient()
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )

    runner_app._session_inboxes_ref[parent_id] = session_inbox
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="reviewer",
        title="auth",
    )
    try:
        async with _runner_client(app):
            _complete_child(app, child_id, "FINDINGS: should not be inlined.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while not server_client.posts:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no wake POST observed within 5s")
                await asyncio.sleep(0.01)

        text = _notice_text(server_client.posts[0])
        assert "waiting in inbox" in text
        assert "FINDINGS: should not be inlined." not in text

        entry = runner_app.get_subagent_work(child_id)
        assert entry is not None
        assert entry.wake_inlined is False
    finally:
        runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)


@pytest.mark.asyncio
async def test_helpers_started_together_wake_their_parent_once(
    _no_wake_backoff: list[float],
) -> None:
    """Helpers of one parent turn wake it once, when the last settles, with every Result; a
    restarted runner keeps the group: a recovered Result waits for the sibling still running."""
    from omnigent.runner import app as runner_app

    parent_id = "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    first, second = "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3", "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d5"
    group = "resp_join123"

    def child(child_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        return {
            "id": child_id,
            "tool": "worker",
            "session_name": child_id[:4],
            "current_task_status": status,
            "last_task_error": {"code": "model_error", "message": error} if error else None,
            "labels": {
                runner_app.SUBAGENT_DISPATCH_ID_LABEL_KEY: f"subagent_{child_id[:12]}",
                runner_app.SUBAGENT_GROUP_LABEL_KEY: group,
                CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE,
            },
        }

    server_client = _RecordingServerClient(
        children={
            first: child(first, "failed", "FIRST: the source was down."),
            second: child(second, "in_progress"),
        }
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=server_client,  # type: ignore[arg-type]
    )
    try:
        async with _runner_client(app):
            await app.state.recover_undrained_subagent_results(parent_id)
            await asyncio.sleep(0.05)
            assert server_client.posts == []  # the sibling still runs

            _complete_child(app, second, "SECOND: three fares compared.")
            deadline = asyncio.get_running_loop().time() + 5.0
            while (
                runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY
                not in server_client.children[first]["labels"]
            ):
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError("no joined wake observed within 5s")
                await asyncio.sleep(0.01)

        assert len(server_client.posts) == 1
        text = _notice_text(server_client.posts[0])
        assert "FIRST: the source was down." in text
        assert "SECOND: three fares compared." in text
        assert all(
            c["labels"][runner_app.SUBAGENT_DELIVERED_ID_LABEL_KEY] == f"subagent_{cid[:12]}"
            for cid, c in server_client.children.items()
        )
    finally:
        for child_id in (first, second):
            runner_app.unregister_subagent_work(child_id)
        runner_app._session_inboxes_ref.pop(parent_id, None)
        runner_app._subagent_recovery_done.discard(parent_id)
