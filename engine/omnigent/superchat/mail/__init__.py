"""Mail, a Lookout source (ADR 0039).

Provider-neutral: ``types`` (message, thread, the provider interface), ``triage`` (which new
mail becomes a signal, by code), ``poll`` (the ``mail_poll`` work-queue job and its cursor),
``tools`` / ``handlers`` / ``routes`` (the read-only ``mail_read_thread`` tool, served by the
engine). One provider exists: ``file_provider``, the **file mailbox** for development.

The file mailbox
================

Off unless ``OMNIGENT_DEV_MAILBOX`` names a markdown file the engine can read; never a
production provider. The mailbox belongs to the front matter's ``owner``, else to the single
local person. Appending a section is a new mail arriving::

    ---
    owner: local
    me: alex@example.com
    ---

    ## Any label (ignored)
    From: Dana Reyes <dana@example.org>
    To: Alex Kim <alex@example.com>
    Cc: Sam Ortiz <sam@example.org>
    Date: 2026-10-26T09:12:00+01:00
    Subject: Re: Morocco landing pages
    Thread: morocco-pages
    List-Id: <news.example.org>
    Precedence: bulk

    The body, until the next ``## `` line.

``From``, ``Date`` (ISO 8601 with a time zone) and ``Thread`` are required; ``To``, ``Cc``,
``Subject``, ``List-Id``, ``List-Unsubscribe``, ``Precedence`` and ``Auto-Submitted`` are
optional. A message's id is a hash of its thread, date and sender; ``me`` is the person's own
address (their messages mark a thread they wrote in).

Seams (no other capability is imported; ``superchat/features.py`` wires them):
:func:`set_lookout` hands in the Lookout's ``add_signal``, ``remove_signal`` and its code check
(``check_item``); :func:`set_reader_kinds` names the runs that may read a thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from omnigent.context.labels import background_task_id
from omnigent.superchat.feature import Feature, InstallDeps, ToolManagerCtx, is_helper
from omnigent.superchat.mail.handlers import handle_mail_tool
from omnigent.superchat.mail.poll import JOB_KIND, MailPoll, submit_poll
from omnigent.superchat.mail.tools import MAIL_TOOL_NAMES
from omnigent.superchat.mail.types import Provider

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.superchat.feature import BackgroundJob
    from omnigent.tools.base import Tool

__all__ = ["FEATURE", "active_provider", "set_lookout", "set_reader_kinds"]

_logger = logging.getLogger(__name__)

_poll = MailPoll(lambda: active_provider())
_reader_kinds: tuple[str, ...] = ()


def active_provider() -> Provider | None:
    """The mailbox the source reads now: the file mailbox when its env var is set, else none."""
    from omnigent.superchat.mail.file_provider import from_env

    return from_env()


def set_lookout(
    add_signal: Callable[[Mapping[str, Any]], Any] | None,
    remove_signal: Callable[[str, str], Any] | None,
    check: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None,
) -> None:
    """Hand in the Lookout's entry points and its code check (composition root)."""
    _poll.add_signal, _poll.remove_signal, _poll.check = add_signal, remove_signal, check


def set_reader_kinds(kinds: tuple[str, ...]) -> None:
    """The scheduled-task kinds whose runs may call ``mail_read_thread``."""
    global _reader_kinds
    _reader_kinds = tuple(kinds)


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """Only a background run sees the tool; the engine admits only a reader kind's run."""
    from omnigent.superchat.mail.tools import MailReadThreadTool

    if is_helper(labels) and background_task_id(labels) is not None:
        return [MailReadThreadTool()]
    return []


class _StartPoll:
    """At startup (the work queue is configured by then), queue the mailbox owner's poll."""

    def start(self) -> None:
        box = active_provider()
        if box is None:
            return
        owner = box.owner
        submit_poll(owner)
        _logger.info("mail: the %s mailbox of %s is polled", box.name, owner)

    def shutdown(self) -> None:
        return None


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.server import work_queue
    from omnigent.superchat.mail.routes import create_mail_router

    tasks = deps.scheduled_task_store

    def timezone_of(owner: str) -> str | None:
        prefs = tasks.get_owner_preferences(owner) if tasks is not None else None
        return prefs.timezone if prefs else None

    _poll.timezone_of = timezone_of
    work_queue.register_handler(JOB_KIND, _poll.run_job)
    if tasks is not None:
        app.include_router(
            create_mail_router(
                active_provider,
                lambda: _reader_kinds,
                conversation_store=deps.conversation_store,
                scheduled_task_store=tasks,
                permission_store=deps.permission_store,
                auth_provider=deps.auth_provider,
            ),
            prefix="/v1",
            tags=["mail"],
        )


def _jobs(_app: FastAPI) -> list[BackgroundJob]:
    return [_StartPoll()] if active_provider() is not None else []


FEATURE = Feature(
    name="mail",
    tools=_tools,
    handlers=dict.fromkeys(MAIL_TOOL_NAMES, handle_mail_tool),
    install=_install,
    jobs=_jobs,
)
