"""Backend selection and command mapping for the local ``browser_*`` backend."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from omnigent.runner import tool_dispatch
from omnigent.tools import browser_backend
from omnigent.tools.browser_backend import LocalBrowserBackend, local_browser_backend_enabled

SNAPSHOT = {
    "ok": True,
    "url": "https://example.com/",
    "title": "Example",
    "elements": [
        {"ref": "eaaa", "role": "link", "name": "More", "tag": "a"},
        {"ref": "ebbb", "role": "textbox", "name": "Q", "tag": "input"},
    ],
    "tree": 'Hello\n- link "More" [eaaa]\n- textbox "Q" [ebbb]',
}


def _backend(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]]):
    backend = LocalBrowserBackend()
    calls: list[list[str]] = []

    async def fake_run(argv: list[str]) -> tuple[bytes, bytes]:
        calls.append(argv)
        return json.dumps(replies.pop(0)).encode(), b""

    monkeypatch.setattr(backend, "_run", fake_run)
    return backend, calls


def test_backend_selected_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_BROWSER_BACKEND", raising=False)
    assert not local_browser_backend_enabled()
    monkeypatch.setenv("OMNIGENT_BROWSER_BACKEND", "local")
    assert local_browser_backend_enabled()


def test_navigate_and_snapshot_map_to_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, calls = _backend(monkeypatch, [{"ok": True, "url": "u"}, SNAPSHOT])
    nav = json.loads(asyncio.run(backend.execute("navigate", {"url": "https://x.test"})))
    assert nav["ok"] is True
    assert calls[0] == ["superchat-page-browser", "navigate", '{"url": "https://x.test"}']
    snap = json.loads(asyncio.run(backend.execute("snapshot", {})))
    assert calls[1][:2] == ["superchat-page-browser", "snapshot"]
    assert "[ref=1]" in snap["tree"] and "[ebbb]" not in snap["tree"]


def _navigate(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> dict[str, Any]:
    backend, _ = _backend(monkeypatch, [reply])
    return json.loads(asyncio.run(backend.execute("navigate", {"url": "https://x.test"})))


def test_navigate_200_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = {"ok": True, "url": "u", "title": "Home", "status": 200, "excerpt": "Hello"}
    nav = _navigate(monkeypatch, reply)
    assert nav == {"ok": True, "url": "u", "title": "Home", "status": 200}


def test_navigate_403_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    nav = _navigate(monkeypatch, {"ok": True, "url": "u", "title": "x", "status": 403})
    assert nav["blocked"] is True and "403" in nav["reason"] and nav["status"] == 403


def test_navigate_challenge_title_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    nav = _navigate(
        monkeypatch, {"ok": True, "url": "u", "title": "Just a moment...", "status": 200}
    )
    assert nav["blocked"] is True


def test_navigate_error_page_and_net_error_are_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    page = {"ok": True, "url": "u", "title": "u", "excerpt": "This page couldn't load"}
    assert _navigate(monkeypatch, page)["blocked"] is True
    err = _navigate(monkeypatch, {"ok": False, "error": "net::ERR_HTTP2_PROTOCOL_ERROR"})
    assert err["blocked"] is True


def test_click_and_type_map_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, calls = _backend(monkeypatch, [SNAPSHOT, SNAPSHOT, SNAPSHOT])
    snap = json.loads(asyncio.run(backend.execute("snapshot", {})))
    asyncio.run(backend.execute("click", {"ref": 0, "snapshot_id": snap["snapshot_id"]}))
    assert json.loads(calls[1][2]) == {"actions": [{"kind": "click", "ref": "eaaa"}]}
    new_id = backend._snapshot_id
    asyncio.run(backend.execute("type", {"ref": 1, "snapshot_id": new_id, "text": "hi"}))
    assert json.loads(calls[2][2]) == {"actions": [{"kind": "fill", "ref": "ebbb", "text": "hi"}]}


def test_stale_snapshot_selector_and_unknown_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, calls = _backend(monkeypatch, [SNAPSHOT])
    asyncio.run(backend.execute("snapshot", {}))
    for args in ({"ref": 0, "snapshot_id": "old"}, {"selector": "a"}, {"ref": 9}):
        assert "error" in json.loads(asyncio.run(backend.execute("click", args)))
    assert len(calls) == 1


def test_launches_browser_once_when_cdp_down(monkeypatch: pytest.MonkeyPatch) -> None:
    down = {"ok": False, "error": "CDP not ready on port 9222: unreachable"}
    backend, calls = _backend(monkeypatch, [down, SNAPSHOT])
    spawned: list[int] = []

    async def fake_spawn() -> None:
        spawned.append(1)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(backend, "_spawn_browser", fake_spawn)
    monkeypatch.setattr(browser_backend.asyncio, "sleep", no_sleep)
    assert json.loads(asyncio.run(backend.execute("snapshot", {})))["ok"] is True
    assert spawned == [1] and len(calls) == 2


def test_screenshot_returns_data_url(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LocalBrowserBackend()

    async def fake_run(argv: list[str]) -> tuple[bytes, bytes]:
        assert argv == ["import", "-window", "root", "png:-"]
        return b"PNG", b""

    monkeypatch.setattr(backend, "_run", fake_run)
    out = json.loads(asyncio.run(backend.execute("screenshot", {})))
    assert out["data_url"] == "data:image/png;base64,UE5H"


def test_dispatch_uses_local_backend_without_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_BROWSER_BACKEND", "local")
    backend, _ = _backend(monkeypatch, [{"ok": True, "url": "u"}])
    monkeypatch.setattr(browser_backend, "_backend", backend)
    out = asyncio.run(
        tool_dispatch._execute_browser_tool(
            "browser_navigate", {"url": "https://x.test"}, server_client=None, conversation_id=None
        )
    )
    assert json.loads(out)["ok"] is True
