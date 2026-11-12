"""A Helper whose turn ends while its own child Helpers still run is waiting, not finished."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _Fixture:
    def __init__(self) -> None:
        self.muse = uuid.uuid4().hex
        self.coord = uuid.uuid4().hex
        self.parts = [uuid.uuid4().hex for _ in range(2)]
        self.posts: dict[str, list[str]] = {}
        fixture = self

        class _Client(NullServerClient):
            async def post(self, url: str, **kwargs: Any) -> Any:
                for sid in (fixture.muse, fixture.coord):
                    if url.rstrip("/").endswith(f"/v1/sessions/{sid}/events"):
                        text = (kwargs.get("json") or {})["data"]["content"][0]["text"]
                        fixture.posts.setdefault(sid, []).append(text)
                return self._Response()

        self.app = create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
            server_client=_Client(),  # type: ignore[arg-type]
        )
        runner_app._session_inboxes_ref[self.muse] = asyncio.Queue()
        runner_app._session_inboxes_ref[self.coord] = asyncio.Queue()
        self.coord_entry = runner_app.register_subagent_work(
            parent_session_id=self.muse,
            child_session_id=self.coord,
            agent="worker",
            title="Compare",
        )
        for i, part in enumerate(self.parts):
            runner_app.register_subagent_work(
                parent_session_id=self.coord,
                child_session_id=part,
                agent="subworker",
                title=f"Part {i}",
            )
            runner_app.mark_subagent_work_started(part)
        runner_app.mark_subagent_work_started(self.coord)

    def end_turn(self, sid: str) -> None:
        self.app.state.on_proxy_stream_end(sid)

    async def settle(self) -> None:
        # The wake notices are posted from background tasks: run them to completion.
        me = asyncio.current_task()
        while pending := [t for t in asyncio.all_tasks() if t is not me and not t.done()]:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=5)

    def close(self) -> None:
        for sid in (self.coord, *self.parts):
            runner_app.unregister_subagent_work(sid)
        for sid in (self.muse, self.coord):
            runner_app._session_inboxes_ref.pop(sid, None)


async def test_coordinator_with_live_children_does_not_wake_parent() -> None:
    f = _Fixture()
    try:
        f.end_turn(f.coord)
        await f.settle()
        assert f.posts.get(f.muse) is None
        assert f.coord_entry.status == "running"
        assert f.coord_entry.delivered is False
        assert runner_app._session_inboxes_ref[f.muse].empty()
    finally:
        f.close()


async def test_last_child_result_resumes_coordinator_and_its_final_turn_wakes_muse_once() -> None:
    f = _Fixture()
    try:
        f.end_turn(f.coord)
        # One part fails, one completes: both count as done, each wakes the coordinator.
        f.app.state.mark_subagent_terminal_and_wake(
            f.parts[0], status="failed", output="Error: boom"
        )
        await f.settle()
        assert f.posts.get(f.coord) and "failed" in f.posts[f.coord][0]
        f.app.state.mark_subagent_terminal_and_wake(
            f.parts[1], status="completed", output="Milvus notes"
        )
        await f.settle()
        assert runner_app.get_subagent_work(f.parts[1]).status == "completed"
        assert f.posts.get(f.muse) is None

        f.end_turn(f.coord)  # the coordinator's turn after the last result: no live children
        await f.settle()
        assert f.coord_entry.status == "completed"
        assert len(f.posts[f.muse]) == 1
        assert "finished (completed)" in f.posts[f.muse][0]
    finally:
        f.close()
