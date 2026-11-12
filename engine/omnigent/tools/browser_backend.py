"""Backend seam for the ``browser_*`` tools.

The default backend relays each action to the Omnigent desktop app's
embedded browser through the server (see ``_execute_browser_tool`` in
``omnigent/runner/tool_dispatch.py``). Setting
``OMNIGENT_BROWSER_BACKEND=local`` instead drives a Chromium running on the
runner's own machine, on the runner's ``DISPLAY``, over CDP through the
``superchat-page-browser`` helper (override with ``OMNIGENT_BROWSER_HELPER``).
Chromium is started with ``superchat-browser`` (override with
``OMNIGENT_BROWSER_LAUNCHER``) when its debug port is not up.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

BROWSER_BACKEND_ENV = "OMNIGENT_BROWSER_BACKEND"
_LAUNCH_RETRIES = 8


def local_browser_backend_enabled() -> bool:
    """:returns: True when ``OMNIGENT_BROWSER_BACKEND`` selects ``local``."""
    return os.environ.get(BROWSER_BACKEND_ENV, "").strip().lower() == "local"


# Found in the title or the first text of the page: a bot challenge or Chromium's error page.
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
    "this page couldn't load",
    "this site can't be reached",
)
# Too common in normal pages to trust in body text: only the title, or an error status.
_WEAK_MARKERS = ("access denied", "captcha")


def _flag_blocked(data: dict[str, Any]) -> dict[str, Any]:
    """
    Add ``blocked`` and ``reason`` to a navigate result when the site refused the browser.

    Looks at the main document's HTTP status and a short title/text excerpt (bot
    challenges, Chromium's own error page). Other fields are left as they are; the helper's
    ``excerpt`` is consumed here.

    :param data: The helper's navigate result.
    :returns: The same dict, with ``blocked: True`` and ``reason`` when blocked.
    """
    excerpt = str(data.pop("excerpt", "") or "")
    if not data.get("ok"):
        if "net::err" in str(data.get("error", "")).lower():
            data.update(blocked=True, reason=f"page failed to load: {str(data['error'])[:100]}")
        return data
    status = data.get("status")
    title = str(data.get("title") or "").lower()
    text = f"{title} {excerpt.lower()}"
    if status in (401, 403, 429):
        data.update(blocked=True, reason=f"site answered HTTP {status}")
    elif any(m in text for m in _CHALLENGE_MARKERS) or any(
        m in title or (isinstance(status, int) and status >= 400 and m in text)
        for m in _WEAK_MARKERS
    ):
        data.update(blocked=True, reason="bot challenge or error page")
    return data


class LocalBrowserBackend:
    """Executes ``browser_*`` actions against the runner's own Chromium."""

    def __init__(self) -> None:
        self._helper = os.environ.get("OMNIGENT_BROWSER_HELPER", "superchat-page-browser")
        self._launcher = os.environ.get("OMNIGENT_BROWSER_LAUNCHER", "superchat-browser")
        self._snapshot_id: str | None = None
        self._refs: dict[int, str] = {}

    async def execute(self, action: str, args: dict[str, Any]) -> str:
        """
        Run one browser action.

        :param action: Tool name minus the ``browser_`` prefix.
        :param args: Parsed tool arguments.
        :returns: JSON result string (``ok`` plus fields, or ``error``).
        """
        try:
            if action == "navigate":
                return json.dumps(
                    _flag_blocked(
                        await self._helper_call("navigate", {"url": args.get("url", "")})
                    )
                )
            if action == "snapshot":
                return json.dumps(self._format_snapshot(await self._helper_call("snapshot", {})))
            if action in ("click", "type"):
                return await self._act(action, args)
            if action == "screenshot":
                return await self._screenshot()
            return json.dumps({"error": f"unknown browser action: {action}"})
        except Exception as exc:
            return json.dumps({"error": f"browser_{action} failed: {exc}"})

    async def _act(self, action: str, args: dict[str, Any]) -> str:
        if args.get("selector") is not None or not isinstance(args.get("ref"), int):
            return json.dumps(
                {"error": "this browser supports `ref` from browser_snapshot only, not selector"}
            )
        snapshot_id = args.get("snapshot_id")
        if snapshot_id is not None and snapshot_id != self._snapshot_id:
            return json.dumps({"error": "stale snapshot_id; call browser_snapshot again"})
        element = self._refs.get(args["ref"])
        if element is None:
            return json.dumps({"error": f"unknown ref {args['ref']}; call browser_snapshot again"})
        step: dict[str, Any] = {"kind": "click" if action == "click" else "fill", "ref": element}
        if action == "type":
            step["text"] = str(args.get("text", ""))
        result = await self._helper_call("act", {"actions": [step]})
        return json.dumps(self._format_snapshot(result))

    async def fill_login(
        self, *, ref: int, field: str, fetch: Callable[[], Awaitable[tuple[str, str]]]
    ) -> str:
        """
        Type one vault value into a field of the page, straight over CDP.

        ``fetch`` returns ``(value, site)`` from the server. The value rides to the helper on
        stdin only (never argv), the helper refuses unless the page is on ``site`` over HTTPS at
        fill time, and the result never contains the value: it says ``filled`` or why not.

        :param ref: Integer field ref from the last ``browser_snapshot``.
        :param field: ``username`` or ``password`` (for the message only).
        :param fetch: Async callable returning ``(value, site)``.
        :returns: JSON result string.
        """
        element = self._refs.get(ref)
        if element is None:
            return json.dumps({"error": f"unknown ref {ref}; call browser_snapshot again"})
        value, site = await fetch()
        step = {"kind": "fill", "ref": element, "text": value, "origin": site}
        data = await self._helper_call("act", {"actions": [step]}, secret_stdin=True)
        # The helper's page snapshot is dropped: only the outcome leaves this function.
        if data.get("ok"):
            return json.dumps({"ok": True, "result": f"filled {field}"})
        error = str(data.get("error", "fill failed")).replace(value, "[redacted]")
        return json.dumps({"ok": False, "error": error[:200]})

    def _format_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        """Swap the helper's opaque element ids for integer ``[ref=N]`` ids."""
        if not data.get("ok"):
            return data
        self._snapshot_id = uuid.uuid4().hex[:12]
        self._refs = {}
        tree = str(data.get("tree", ""))
        for index, element in enumerate(data.get("elements") or []):
            self._refs[index] = element["ref"]
            tree = tree.replace(f"[{element['ref']}]", f"[ref={index}]")
        return {
            "ok": True,
            "snapshot_id": self._snapshot_id,
            "url": data.get("url"),
            "title": data.get("title"),
            "tree": tree,
        }

    async def _helper_call(
        self, command: str, payload: dict[str, Any], *, secret_stdin: bool = False
    ) -> dict[str, Any]:
        """Run the CDP helper, launching Chromium once if its debug port is down."""
        launched = False
        data: dict[str, Any] = {}
        for _ in range(_LAUNCH_RETRIES):
            if secret_stdin:
                out, err = await self._run(
                    [self._helper, command],
                    stdin=json.dumps(payload).encode() + b"\n",
                    env={**os.environ, "SUPERCHAT_BROWSER_ARGS_STDIN": "1"},
                )
            else:
                out, err = await self._run([self._helper, command, json.dumps(payload)])
            try:
                data = json.loads(out)
            except ValueError:
                return {"ok": False, "error": err.decode("utf-8", "replace")[:200] or "no output"}
            if data.get("ok") or "CDP not ready" not in str(data.get("error", "")):
                return data
            if not launched:
                launched = True
                await self._spawn_browser()
            await asyncio.sleep(1.0)
        return data

    async def _screenshot(self) -> str:
        """Capture the whole display: the Chromium window is what the Muse sees."""
        png, err = await self._run(["import", "-window", "root", "png:-"])
        if not png:
            return json.dumps({"error": err.decode("utf-8", "replace")[:200] or "no image"})
        data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        return json.dumps({"ok": True, "data_url": data_url})

    async def _spawn_browser(self) -> None:
        await asyncio.create_subprocess_exec(
            self._launcher,
            "about:blank",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    @staticmethod
    async def _run(
        argv: list[str], *, stdin: bytes | None = None, env: dict[str, str] | None = None
    ) -> tuple[bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        return await asyncio.wait_for(proc.communicate(stdin), timeout=60.0)


_backend: LocalBrowserBackend | None = None


def get_local_browser_backend() -> LocalBrowserBackend:
    """:returns: The process-wide local backend (holds the last snapshot's refs)."""
    global _backend
    if _backend is None:
        _backend = LocalBrowserBackend()
    return _backend
