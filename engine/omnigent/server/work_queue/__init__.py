"""The work queue (ADR 0038): the one line every background model run waits in.

One Postgres table (``work_jobs``). A job is submitted idempotently on ``(kind, key)``, claimed
under a row lock when it is due and fits the limits (one across everyone, ``16`` by default via
``OMNIGENT_WORK_QUEUE_MAX_RUNNING``; one per owner, ``3`` or the host's room, one while the
owner's own turn runs), then run by the handler registered for its kind. A handler that starts a
run (a Helper) binds the job to that run's id; the job holds its slot until the run's turn ends.
A waiting job survives a restart; a claimed one is never run twice by live processes.

Public API: :func:`submit`, :func:`register_handler`, :class:`Defer`, :class:`Job`,
:func:`set_owner_room`, and :func:`default` for the worker (``run_ended``, ``release_run``,
``is_bound``, ``start``, ``shutdown``, ``drain``). The engine installs the Postgres-backed queue
at startup (:func:`configure`); without it the queue lives in memory.
"""

from __future__ import annotations

from omnigent.server.work_queue.queue import (
    DEFAULT_GLOBAL_LIMIT,
    DEFAULT_OWNER_LIMIT,
    Defer,
    Handler,
    WorkQueue,
    configure,
    default,
    global_limit,
    owner_limit,
    register_handler,
    set_owner_room,
    submit,
    with_payload,
)
from omnigent.server.work_queue.store import (
    Job,
    MemoryWorkQueueStore,
    SqlAlchemyWorkQueueStore,
)

__all__ = [
    "DEFAULT_GLOBAL_LIMIT",
    "DEFAULT_OWNER_LIMIT",
    "Defer",
    "Handler",
    "Job",
    "MemoryWorkQueueStore",
    "SqlAlchemyWorkQueueStore",
    "WorkQueue",
    "configure",
    "default",
    "global_limit",
    "owner_limit",
    "register_handler",
    "set_owner_room",
    "submit",
    "with_payload",
]
