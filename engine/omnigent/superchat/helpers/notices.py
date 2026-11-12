"""A Helper's progress and flag notices to the chat that started it (ADR 0022).

Beside the completion notice, a Helper says two things while it works, each one line through a
tool only Helpers have: ``report_progress`` (a step it finished) and ``raise_flag`` (a judgement
call or surprise the person should know now). Each reaches the parent as a runtime notice; the
Helper's transcript never does.

* A **flag** always wakes the parent, by the same path as the completion notice: the engine
  steers it into the parent's running turn, or starts a turn when the parent is idle.
* A **progress** notice is steered into the parent's running turn (``if_running``). When the
  parent is idle (the usual case: it speaks first, starts its Helpers and its turn ends), the
  progress wakes it, at most once per :data:`WAKE_INTERVAL_S` per parent, carrying every
  Helper's newest waiting line; a flag's or a completion's wake carries them too and resets the
  interval. One line per Helper, the newest only.
* **Throttle**: a progress notice replaces the same Helper's undelivered one, and a Helper's
  progress is steered at most once per :data:`PROGRESS_INTERVAL_S`; one held back goes when
  the interval ends. Flags are never held back or merged.

The state is the runner process's own (Helpers run on their parent's runner): what waits, when
each Helper's progress last went out, and when each parent was last woken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.superchat.feature import HandlerCtx
from omnigent.util.session_lifecycle import title_without_closed_marker

_logger = logging.getLogger(__name__)

REPORT_PROGRESS_TOOL_NAME = "report_progress"
RAISE_FLAG_TOOL_NAME = "raise_flag"
PROGRESS, FLAG = "progress", "flag"
#: Least time between two progress deliveries of one Helper.
PROGRESS_INTERVAL_S = 5.0
#: Least time between two progress wakes of one idle parent (a model turn each).
WAKE_INTERVAL_S = 30.0
#: What a notice POST came to: the parent's running turn took it, no turn runs, or it failed.
TAKEN, IDLE, FAILED = "taken", "idle", "failed"
#: Longest notice text kept; a notice is one line.
MAX_TEXT_CHARS = 240
#: How long a notice POST may wait for the parent's verdict (it can park on an approval gate).
_POST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def notice_text(kind: str, name: str, text: str) -> str:
    """The notice the parent reads: ``[helper <name>] progress: <text>`` (or ``flag:``)."""
    return f"[helper {name}] {kind}: {text}"


def one_line(text: object) -> str:
    """*text* as one trimmed line of at most :data:`MAX_TEXT_CHARS` characters."""
    line = " ".join(str(text or "").split())
    return line if len(line) <= MAX_TEXT_CHARS else line[: MAX_TEXT_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True)
class HelperInfo:
    """Who a Helper reports to and what it is called there.

    :param parent_id: The chat that started it.
    :param name: Its title, as the person reads it.
    :param started_at: Epoch seconds it was created.
    """

    parent_id: str
    name: str
    started_at: float


class HelperNotices:
    """The progress notices waiting per parent, and each Helper's last progress delivery.

    :param clock: Monotonic seconds (tests pass a fake).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: parent id -> helper id -> notice text (the newest undelivered progress).
        self._waiting: dict[str, dict[str, str]] = {}
        #: helper id -> monotonic time its progress last went out.
        self._sent_at: dict[str, float] = {}
        #: helper id -> the timer that sends its held-back progress.
        self._timers: dict[str, asyncio.TimerHandle] = {}
        #: Helpers whose first progress went out (for the one measurement log line).
        self._reported: set[str] = set()
        #: parent id -> monotonic time it was last woken (progress, flag or completion).
        self._woke_at: dict[str, float] = {}
        #: parent id -> the timer that wakes it with the progress waiting then.
        self._wake_timers: dict[str, asyncio.TimerHandle] = {}

    def hold(self, parent_id: str, helper_id: str, notice: str) -> float:
        """Keep *notice* as *helper_id*'s waiting progress (replacing an older one).

        :returns: Seconds until it may go out (0: now).
        """
        self._waiting.setdefault(parent_id, {})[helper_id] = notice
        last = self._sent_at.get(helper_id)
        if last is None:
            return 0.0
        return max(0.0, last + PROGRESS_INTERVAL_S - self._clock())

    def waiting(self, parent_id: str, helper_id: str) -> str | None:
        """*helper_id*'s waiting progress for *parent_id*, if any."""
        return self._waiting.get(parent_id, {}).get(helper_id)

    def waiting_items(self, parent_id: str) -> list[tuple[str, str]]:
        """Every (helper id, notice) waiting for *parent_id*, oldest Helper first; kept."""
        return list(self._waiting.get(parent_id, {}).items())

    def wake_delay(self, parent_id: str) -> float:
        """Seconds until *parent_id* may be woken for progress again (0: now)."""
        last = self._woke_at.get(parent_id)
        if last is None:
            return 0.0
        return max(0.0, last + WAKE_INTERVAL_S - self._clock())

    def woke(self, parent_id: str) -> None:
        """*parent_id* was woken (by progress, a flag or a completion): the interval restarts."""
        self._woke_at[parent_id] = self._clock()
        timer = self._wake_timers.pop(parent_id, None)
        if timer is not None:
            timer.cancel()

    def has_wake_timer(self, parent_id: str) -> bool:
        """Whether a progress wake of *parent_id* is already timed."""
        return parent_id in self._wake_timers

    def set_wake_timer(self, parent_id: str, timer: asyncio.TimerHandle) -> None:
        """Remember the timer that wakes *parent_id* with its waiting progress."""
        self._wake_timers[parent_id] = timer

    def clear_wake_timer(self, parent_id: str) -> None:
        """The timed wake fired: the timer is done."""
        self._wake_timers.pop(parent_id, None)

    def sent(self, parent_id: str, helper_id: str, notice: str) -> bool:
        """Record that *notice* went out; ``True`` when it was this Helper's first progress."""
        waiting = self._waiting.get(parent_id, {})
        if waiting.get(helper_id) == notice:
            del waiting[helper_id]
            if not waiting:
                self._waiting.pop(parent_id, None)
        self._sent_at[helper_id] = self._clock()
        first = helper_id not in self._reported
        self._reported.add(helper_id)
        return first

    def take(self, parent_id: str, *, done: Iterable[str] = ()) -> list[str]:
        """Every progress waiting for *parent_id*, oldest Helper first, for the next wake.

        :param done: Helpers that just finished: their progress is dropped, their results say
            more.
        """
        waiting = self._waiting.pop(parent_id, {})
        for helper_id in done:
            waiting.pop(helper_id, None)
            self.forget(helper_id)
        self.woke(parent_id)
        return list(waiting.values())

    def forget(self, helper_id: str) -> None:
        """Drop what is kept about a Helper that finished."""
        timer = self._timers.pop(helper_id, None)
        if timer is not None:
            timer.cancel()
        self._sent_at.pop(helper_id, None)
        self._reported.discard(helper_id)
        for parent_id in [p for p, w in self._waiting.items() if helper_id in w]:
            del self._waiting[parent_id][helper_id]
            if not self._waiting[parent_id]:
                del self._waiting[parent_id]

    def has_timer(self, helper_id: str) -> bool:
        """Whether a held-back progress of *helper_id* is already timed to go out."""
        return helper_id in self._timers

    def set_timer(self, helper_id: str, timer: asyncio.TimerHandle) -> None:
        """Remember the timer that sends *helper_id*'s held-back progress."""
        self._timers[helper_id] = timer

    def clear_timer(self, helper_id: str) -> None:
        """The held-back progress went (or was dropped): the timer is done, or cancelled."""
        timer = self._timers.pop(helper_id, None)
        if timer is not None:
            timer.cancel()


#: The runner process's notices.
NOTICES = HelperNotices()
_helpers: dict[str, HelperInfo] = {}
_background: set[asyncio.Task[Any]] = set()


def take_waiting_progress(parent_id: str, *, done: Iterable[str] = ()) -> list[str]:
    """The progress lines the next wake of *parent_id* carries (see :meth:`HelperNotices.take`)."""
    done = list(done)
    for helper_id in done:
        _helpers.pop(helper_id, None)
    return NOTICES.take(parent_id, done=done)


async def _helper_info(client: httpx.AsyncClient, helper_id: str) -> HelperInfo | None:
    """The calling Helper's parent, title and start, from the engine (kept once known)."""
    known = _helpers.get(helper_id)
    if known is not None:
        return known
    try:
        resp = await client.get(f"/v1/sessions/{helper_id}", timeout=10.0)
        data: Mapping[str, Any] = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    parent = data.get("parent_session_id") if isinstance(data, Mapping) else None
    if not isinstance(parent, str) or not parent:
        return None
    title = title_without_closed_marker(data.get("title")) or ""
    name = title.partition(":")[2].strip() if ":" in title else title.strip()
    created = data.get("created_at")
    info = HelperInfo(
        parent_id=parent,
        name=name or "Helper",
        started_at=float(created) if isinstance(created, (int, float)) else time.time(),
    )
    _helpers[helper_id] = info
    return info


async def post_notice(
    client: httpx.AsyncClient,
    parent_id: str,
    notice: str,
    *,
    if_running: bool,
    helper_id: str | None = None,
) -> str:
    """Post *notice* to the parent as a runtime notice, through the engine's message path.

    Without *if_running* it is steered into the parent's running turn or starts one (the wake);
    with it, only a running turn takes it (409 ``not_running`` otherwise, nothing kept). The
    notice names the Helper it is from, so it joins that Helper's Round (ADR 0029).

    :returns: :data:`TAKEN`, :data:`IDLE` (no turn runs to take it) or :data:`FAILED`.
    """
    try:
        resp = await client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "is_system_notice": True,
                    "content": [{"type": "input_text", "text": notice}],
                },
                **({"if_running": True} if if_running else {}),
                **({"context": {"helper_id": helper_id}} if helper_id else {}),
            },
            timeout=_POST_TIMEOUT,
        )
    except httpx.HTTPError:
        _logger.warning("helper notice to %s failed", parent_id, extra={"session_id": parent_id})
        return FAILED
    if resp.status_code < 400:
        return TAKEN
    if resp.status_code == 409:
        return IDLE  # ``not_running``: no turn runs to take an ``if_running`` notice
    _logger.warning(
        "helper notice to %s refused (%s)",
        parent_id,
        resp.status_code,
        extra={"session_id": parent_id},
    )
    return FAILED


def _log_first_progress(parent_id: str, helper_id: str, notice: str) -> None:
    """The one measurement line, when *notice* was this Helper's first progress to go out."""
    if not NOTICES.sent(parent_id, helper_id, notice):
        return
    info = _helpers.get(helper_id)
    started = info.started_at if info is not None else time.time()
    _logger.info(
        "first progress for %s %.1fs after its helper started",
        parent_id,
        max(0.0, time.time() - started),
        extra={"session_id": parent_id},
    )


async def _wake_with_progress(client: httpx.AsyncClient, parent_id: str) -> None:
    """Wake the idle parent with every Helper's waiting progress, within the wake interval."""
    NOTICES.clear_wake_timer(parent_id)
    delay = NOTICES.wake_delay(parent_id)
    if delay > 0:
        _schedule_wake(client, parent_id, delay)
        return
    items = NOTICES.waiting_items(parent_id)
    if not items:
        return
    body = "\n".join(notice for _, notice in items)
    if (
        await post_notice(client, parent_id, body, if_running=False, helper_id=items[0][0])
        == FAILED
    ):
        return  # kept; the next progress, flag or completion tries again
    NOTICES.woke(parent_id)
    for helper_id, notice in items:
        _log_first_progress(parent_id, helper_id, notice)


def _schedule_wake(client: httpx.AsyncClient, parent_id: str, delay: float) -> None:
    """Wake *parent_id* with the progress waiting once its wake interval ends."""
    if NOTICES.has_wake_timer(parent_id):
        return
    loop = asyncio.get_running_loop()

    def fire() -> None:
        task = loop.create_task(_wake_with_progress(client, parent_id))
        _background.add(task)
        task.add_done_callback(_background.discard)

    NOTICES.set_wake_timer(parent_id, loop.call_later(delay, fire))


async def _deliver_progress(client: httpx.AsyncClient, helper_id: str, info: HelperInfo) -> None:
    """Steer *helper_id*'s waiting progress into the parent's running turn, else wake it."""
    NOTICES.clear_timer(helper_id)
    notice = NOTICES.waiting(info.parent_id, helper_id)
    if notice is None:
        return
    result = await post_notice(
        client, info.parent_id, notice, if_running=True, helper_id=helper_id
    )
    if result == TAKEN:
        _log_first_progress(info.parent_id, helper_id, notice)
    elif result == IDLE:
        await _wake_with_progress(client, info.parent_id)
    # FAILED: kept; the Helper's next report or the next wake carries it.


def _schedule(client: httpx.AsyncClient, helper_id: str, info: HelperInfo, delay: float) -> None:
    """Send the held-back progress of *helper_id* once its interval ends."""
    if NOTICES.has_timer(helper_id):
        return  # the timer already sends whatever is newest by then
    loop = asyncio.get_running_loop()

    def fire() -> None:
        task = loop.create_task(_deliver_progress(client, helper_id, info))
        _background.add(task)
        task.add_done_callback(_background.discard)

    NOTICES.set_timer(helper_id, loop.call_later(delay, fire))


async def report(ctx: HandlerCtx, kind: str, args: Mapping[str, Any]) -> str:
    """Handle ``report_progress`` / ``raise_flag`` for the calling Helper."""
    text = one_line(args.get("text"))
    if not text:
        return json.dumps({"error": "text is required: one line"})
    if ctx.server_client is None or not ctx.conversation_id:
        return json.dumps({"error": f"{ctx.tool_name} requires server access"})
    info = await _helper_info(ctx.server_client, ctx.conversation_id)
    if info is None:
        return json.dumps({"error": f"{ctx.tool_name} is only for Helpers"})
    notice = notice_text(kind, info.name, text)
    helper_id = ctx.conversation_id
    if kind == FLAG:
        # A flag wakes the parent; progress waiting for that wake rides along.
        items = NOTICES.waiting_items(info.parent_id)
        body = "\n".join([*(line for _, line in items), notice])
        result = await post_notice(
            ctx.server_client, info.parent_id, body, if_running=False, helper_id=helper_id
        )
        if result != FAILED:
            NOTICES.woke(info.parent_id)
            for other_id, line in items:
                _log_first_progress(info.parent_id, other_id, line)
        return json.dumps({"ok": result != FAILED})
    delay = NOTICES.hold(info.parent_id, helper_id, notice)
    if delay > 0:
        _schedule(ctx.server_client, helper_id, info, delay)
    else:
        await _deliver_progress(ctx.server_client, helper_id, info)
    return json.dumps({"ok": True})


async def handle_report_progress(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """``report_progress``: one line on the step just finished."""
    return await report(ctx, PROGRESS, args)


async def handle_raise_flag(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """``raise_flag``: one line the person should know now."""
    return await report(ctx, FLAG, args)
