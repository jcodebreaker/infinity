"""The mail poll (ADR 0039, "Polling by code, through the work queue, no model").

One ``mail_poll`` job per mailbox owner, keyed ``mail_poll:<owner>``, re-defers itself: every 10
minutes while the person's working hours are open (weekdays 8:00-18:00 in their zone), every 60
outside them, jittered; ``OMNIGENT_MAIL_POLL_SECONDS`` fixes the interval (development).

The cursor is the ids already seen, kept in the job's own payload (no table): a deferral stores
it with the job, so a restart keeps it. Ids of messages no longer in the mailbox are dropped, so
it never grows past the mailbox. A poll reads the provider, triages by code and pushes or removes
signals; it never calls a model.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.superchat.mail.triage import Decision, dedupe_key, triage
from omnigent.superchat.mail.types import Provider

_logger = logging.getLogger(__name__)

JOB_KIND = "mail_poll"
INTERVAL_ENV = "OMNIGENT_MAIL_POLL_SECONDS"
IN_HOURS_SECONDS = 10 * 60
OUT_OF_HOURS_SECONDS = 60 * 60
#: Each wait is stretched or shrunk by up to this share, so owners never poll in step.
JITTER = 0.15
WORK_START, WORK_END = 8, 18


def job_key(owner: str) -> str:
    """The one poll job an owner has."""
    return f"{JOB_KIND}:{owner}"


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def next_poll_in(
    now: float, timezone: str | None, environ: Mapping[str, str] | None = None
) -> float:
    """Seconds until the next poll: the env override, else by the person's working hours."""
    env = environ if environ is not None else os.environ
    try:
        fixed = float(env[INTERVAL_ENV])
        if fixed > 0:
            return fixed
    except (KeyError, ValueError):
        pass
    local = datetime.fromtimestamp(now, _zone(timezone))
    in_hours = local.weekday() < 5 and WORK_START <= local.hour < WORK_END
    base = IN_HOURS_SECONDS if in_hours else OUT_OF_HOURS_SECONDS
    return base * random.uniform(1 - JITTER, 1 + JITTER)


class MailPoll:
    """The poll's handler and its seams (the Lookout's, handed in by the composition root)."""

    def __init__(
        self,
        provider: Callable[[], Provider | None],
        *,
        add_signal: Callable[[Mapping[str, Any]], Any] | None = None,
        remove_signal: Callable[[str, str], Any] | None = None,
        check: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
        timezone_of: Callable[[str], str | None] | None = None,
    ) -> None:
        self._provider = provider
        self.add_signal = add_signal
        self.remove_signal = remove_signal
        self.check = check
        self.timezone_of = timezone_of

    def poll_once(
        self, box: Provider, seen: set[str], now: int
    ) -> tuple[list[Decision], set[str]]:
        """Read *box*, triage what is new and push or remove signals (blocking).

        :returns: ``(decisions, the new cursor)``.
        """
        messages = box.messages()
        owner = box.owner
        if self.check is None or self.add_signal is None:
            _logger.warning("mail poll: the Lookout is not wired; nothing is triaged")
            return [], seen

        def check(fields: Mapping[str, Any]) -> Mapping[str, Any]:
            assert self.check is not None
            return self.check(owner, fields)

        decisions = triage(messages, seen, owner=owner, me=box.me, check=check, now=now)
        for decision in decisions:
            if decision.action == "signal" and decision.signal is not None:
                self.add_signal(decision.signal)
            elif decision.action == "answered" and self.remove_signal is not None:
                self.remove_signal(owner, dedupe_key(decision.thread))
            _logger.info(
                "mail poll (%s) for %s: thread %s: %s (%s)",
                box.name,
                owner,
                decision.thread,
                decision.action,
                decision.reason,
            )
        return decisions, {m.id for m in messages}

    async def run_job(self, job: Any) -> Any:
        """The work queue's handler: poll, then defer to the next poll with the new cursor.

        Ends (no deferral) when the mailbox is gone (the env var unset) or now belongs to someone
        else; a failed read keeps the old cursor and tries again at the next poll.
        """
        from omnigent.server import work_queue

        box = self._provider()
        if box is None:
            _logger.info("mail poll for %s: no mailbox configured; polling stops", job.owner)
            return None
        owner = await asyncio.to_thread(lambda: box.owner)
        if owner != job.owner:
            _logger.info("mail poll: the mailbox now belongs to %s; %s stops", owner, job.owner)
            await asyncio.to_thread(lambda: submit_poll(owner))
            return None
        seen = {str(s) for s in job.payload.get("seen") or ()}
        now = int(time.time())
        try:
            _, seen = await asyncio.to_thread(self.poll_once, box, seen, now)
        except Exception:
            _logger.exception("mail poll for %s failed; the cursor is kept", owner)
        timezone = None
        if self.timezone_of is not None:
            try:
                timezone = await asyncio.to_thread(self.timezone_of, owner)
            except Exception:  # noqa: BLE001 - UTC is a fine fallback for a poll's pace
                timezone = None
        wait = next_poll_in(now, timezone)
        return work_queue.Defer(at=now + wait, payload={"seen": sorted(seen)})


def submit_poll(owner: str) -> str:
    """Queue the owner's poll job now (blocking; idempotent while one is queued or running)."""
    from omnigent.server import work_queue

    return work_queue.submit(owner, JOB_KIND, job_key(owner), {"seen": []})
