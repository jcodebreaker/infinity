"""Scheduled-task entities — persisted in the ``scheduled_tasks`` and
``scheduled_task_runs`` tables.

A :class:`ScheduledTask` is a saved, scheduled instruction that fires an agent
session on a recurring schedule (``rrule``). A
:class:`ScheduledTaskRun` records one firing of a task (its run history). This
module holds the plain dataclasses the store converts ORM rows into; the store
owns the JSON (de)serialization of the Text-backed columns.

Naming: the user-facing product name for this feature is "Automations"
(web UI display copy only). The domain model, DB tables
(``scheduled_tasks`` / ``scheduled_task_runs``), stores, REST paths
(``/v1/scheduled-tasks``), agent tools (``sys_scheduled_task_*``), and
these types deliberately keep the name "scheduled task". Keep code,
comments, and schema on "scheduled task"; only presentation strings say
"Automations".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScheduledTask:
    """
    A scheduled task persisted in the ``scheduled_tasks`` table.

    A task's trigger is a required recurring ``rrule``.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param workspace_id: Tenant partition key that owns this row.
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param rrule: The required RFC 5545 recurrence rule for the recurring
        trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``. Evaluated in
        ``timezone``.
    :param user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for, e.g. ``"alice@example.com"``. ``None`` in single-user mode.
    :param agent_id: The agent bound to this task, e.g. ``"ag_..."``.
    :param timezone: IANA timezone the trigger is evaluated in,
        e.g. ``"America/Los_Angeles"``.
    :param created_at: Unix epoch seconds at row creation.
    :param anchor_at: Unix epoch seconds fixing the phase of ``INTERVAL>1`` rules and holding any
        rule back until its day arrives: midnight of the person's start date in ``timezone``, else
        the moment the rule was set (creation, or the update that changed ``rrule``). ``None`` on
        rows that predate the column; read it through :attr:`anchor_epoch`.
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param permission_mode: Per-task permission mode for native coding harnesses
        that support one (currently Claude Code), e.g. ``"acceptEdits"`` or
        ``"bypassPermissions"``. The fire path turns it into the runner's
        ``["--permission-mode", <value>]`` terminal launch args. ``None`` means
        use the agent's configured default.
    :param max_cost_usd: Optional per-firing cost budget in USD. When set, the
        fire path attaches a ``cost_budget`` policy to each spawned session that
        blocks all models once cumulative spend reaches this limit. ``None``
        means no per-firing cost cap (the session runs unconstrained unless the
        agent spec or server-wide defaults impose one).
    :param workspace: Absolute existing path where a fired session's connected
        host runner should start. ``None`` only for legacy or invalid rows.
    :param base_branch: Reserved legacy column; scheduled tasks currently do
        not create git worktrees at fire time.
    :param execution_target: Reserved legacy column; scheduled tasks currently
        run only on ``"connected_host"``.
    :param host_id: Specific connected host to run on. ``None`` only for legacy
        or invalid rows.
    :param state: Lifecycle state — one of ``"active"``, ``"paused"``,
        ``"deleted"``. Defaults to ``"active"``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: Conversation created by the most recent
        firing, or ``None``.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    :param parent_session_id: When set, each fire creates a sub-agent (Helper)
        child of this session instead of a free-standing session, so the
        Helper's result wakes the parent. ``None`` keeps the default fire.
    :param agent_type: Sub-agent Type the Helper runs as (a sub-agent of the
        parent's agent). Only used with ``parent_session_id``.
    :param kind: Explicit product marker set at creation, e.g. ``"followed_topic"`` for a
        topic the Muse follows for the person. ``None`` for unmarked tasks.
    """

    id: str
    name: str
    prompt: str
    rrule: str
    user_id: str | None
    agent_id: str
    timezone: str
    created_at: int
    workspace_id: int = 0
    model_override: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    max_cost_usd: float | None = None
    workspace: str | None = None
    base_branch: str | None = None
    execution_target: str = "connected_host"
    host_id: str | None = None
    state: str = "active"
    last_run_at: int | None = None
    last_run_conversation_id: str | None = None
    updated_at: int | None = None
    account_generation: str | None = None
    parent_session_id: str | None = None
    agent_type: str | None = None
    kind: str | None = None
    anchor_at: int | None = None

    @property
    def anchor_epoch(self) -> int:
        """The instant the rule's phase is counted from (``anchor_at``, else ``created_at``)."""
        return self.anchor_at if self.anchor_at is not None else self.created_at


@dataclass
class ScheduledTaskRun:
    """
    A single firing of a scheduled task, persisted in the ``scheduled_task_runs``
    table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param scheduled_task_id: The task this run belongs to (a bare 32-char hex
        UUID string).
    :param status: Lifecycle state — one of ``"scheduled"``, ``"running"``,
        ``"succeeded"``, ``"failed"``, ``"skipped"``.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param conversation_id: Conversation created by this firing, or ``None``
        before dispatch / after the conversation is deleted.
    :param fired_at: Unix epoch seconds dispatch began, or ``None``.
    :param finished_at: Unix epoch seconds the run reached a terminal state,
        or ``None``.
    :param error: Failure detail when ``status == "failed"``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retry logic; ``None`` unless
        ``status == "failed"``.
    :param attempt: ``1`` for a regular fire, ``2`` for the single retry of a
        failed parent-bound fire.
    """

    id: str
    scheduled_task_id: str
    status: str
    scheduled_at: int
    conversation_id: str | None = None
    fired_at: int | None = None
    finished_at: int | None = None
    error: str | None = None
    error_code: str | None = None
    attempt: int = 1


@dataclass
class OwnerPreferences:
    """
    Per-owner gating preferences for parent-bound scheduled runs.

    :param user_id: Owner identity; ``"local"`` in single-user mode.
    :param proactivity: ``"off"``, ``"low"`` or ``"normal"``.
    :param quiet_start: Local ``HH:MM`` the quiet window starts, or ``None``.
    :param quiet_end: Local ``HH:MM`` the quiet window ends, or ``None``.
    :param timezone: IANA timezone the quiet window is evaluated in.
    """

    user_id: str
    proactivity: str = "normal"
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str = "UTC"
