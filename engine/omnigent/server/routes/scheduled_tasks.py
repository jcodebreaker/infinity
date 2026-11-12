"""REST CRUD for scheduled tasks (``/v1/scheduled-tasks``).

A scheduled task is a saved instruction that fires an agent session on a
recurring RRULE schedule. These endpoints let a client create, list, read,
update, and delete tasks; the live :class:`ScheduledTaskScheduler` is kept in
sync on every mutation so a change takes effect without a restart.

Ownership mirrors hosts: tasks are scoped to the calling user (``"local"`` when
auth is disabled). The RRULE is validated on create/update with
:func:`validate_rrule` — an invalid rule (bad syntax, never-fires, fires-once, or
below the minimum-interval floor) is a 400.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.db.account_authority import account_generation, current_account_user
from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.entities import OwnerPreferences, ScheduledTask, ScheduledTaskRun
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import (
    validate_existing_host_workspace,
    validate_permission_mode_agent_support,
    validate_session_agent,
    validate_session_model_metadata,
    validate_session_permission_mode,
)
from omnigent.server.scheduled.fire import is_firing
from omnigent.server.scheduled.rrule import (
    RRuleValidationError,
    next_fire_times,
    validate_rrule,
)
from omnigent.server.scheduled.run_reconciler import force_fail_stale_runs
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_logger = logging.getLogger(__name__)

# Execution targets a task may run on. ``connected_host`` pins/resolves the
# owner's own machine; ``managed_sandbox`` provisions a FRESH server-managed
# sandbox per fire, using the server's normal sandbox lifecycle.
_VALID_EXECUTION_TARGETS = frozenset({"connected_host", "managed_sandbox"})
_MANAGED_SANDBOX_WITH_HOST_MSG = (
    "a managed_sandbox task runs in a fresh sandbox each fire; do not set host_id or workspace"
)


class CreateScheduledTaskRequest(BaseModel):
    """Body for ``POST /v1/scheduled-tasks``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    prompt: str
    rrule: str
    # Optional only with ``parent_session_id`` (then the parent's agent is used).
    agent_id: str | None = None
    # Bind each fire to this session as a sub-agent (Helper) child; its result
    # wakes the parent. ``agent_type`` is the sub-agent Type to run.
    parent_session_id: str | None = Field(default=None, min_length=1)
    agent_type: str | None = Field(default=None, min_length=1)
    # Explicit product marker, e.g. ``followed_topic``.
    kind: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    # ``None`` = the owner's preference timezone (``/v1/me/proactivity``), else UTC.
    timezone: str | None = None
    model_override: str | None = None
    reasoning_effort: str | None = None
    # Native-harness permission mode (Claude Code), e.g. "acceptEdits". The fire
    # path derives the runner's --permission-mode launch arg from it.
    permission_mode: str | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)
    # Optional: no PINNED host/workspace. When both are unset the fire path
    # resolves the owner's online host at fire time and defaults the workspace to
    # that host's home directory (a failed run is recorded if none is online) —
    # it does not run hostless. ``min_length=1`` still rejects an empty string
    # (the field is unset via omission / null, not ""), mirroring the PATCH
    # request. PATCH still cannot null an already-set workspace/host_id (see
    # ``UpdateScheduledTaskRequest``).
    workspace: str | None = Field(default=None, min_length=1)
    host_id: str | None = Field(default=None, min_length=1)
    # ``managed_sandbox`` runs the task in a fresh server-provisioned sandbox
    # (no host_id/workspace); default keeps the connected-host behavior.
    execution_target: str = "connected_host"
    # First day the schedule may fire, in the task timezone. It also fixes the phase of an
    # ``INTERVAL>1`` rule ("every other Friday starting next week"). Omit to start now.
    starts_on: date | None = None

    @model_validator(mode="after")
    def _validate_create(self) -> CreateScheduledTaskRequest:
        if self.execution_target not in _VALID_EXECUTION_TARGETS:
            raise ValueError("execution_target must be 'connected_host' or 'managed_sandbox'")
        if self.execution_target == "managed_sandbox" and (
            self.host_id is not None or self.workspace is not None
        ):
            raise ValueError(_MANAGED_SANDBOX_WITH_HOST_MSG)
        if self.parent_session_id is None:
            if self.agent_id is None:
                raise ValueError("agent_id is required unless parent_session_id is set")
            if self.agent_type is not None:
                raise ValueError("agent_type requires parent_session_id")
        elif self.agent_type is None:
            raise ValueError("agent_type is required with parent_session_id")
        return self


class PreviewScheduleRequest(BaseModel):
    """Body for ``POST /v1/scheduled-tasks/preview-schedule``."""

    model_config = ConfigDict(extra="forbid")

    rrule: str
    # ``None`` = the owner's preference timezone, else UTC (same as task creation).
    timezone: str | None = None
    # First day the schedule may fire, in the task timezone. It also fixes the phase of an
    # ``INTERVAL>1`` rule ("every other Friday starting next week"). Omit to start now.
    starts_on: date | None = None
    # An existing task to preview an edit of: its own anchor applies while its rule is unchanged.
    scheduled_task_id: str | None = None


class UpdateScheduledTaskRequest(BaseModel):
    """Body for ``PATCH /v1/scheduled-tasks/{id}``. Unset fields are unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    prompt: str | None = None
    rrule: str | None = None
    # Rebinding the agent switches the harness the task fires with. The
    # per-agent settings below do not survive the switch — see the PATCH
    # handler, which clears any the caller does not resend.
    agent_id: str | None = Field(default=None, min_length=1)
    timezone: str | None = None
    model_override: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)  # null clears the cap
    workspace: str | None = Field(default=None, min_length=1)
    host_id: str | None = Field(default=None, min_length=1)
    execution_target: str | None = Field(default=None, min_length=1)
    state: str | None = None
    # Rebind to another parent / Type; null for both unbinds the task.
    parent_session_id: str | None = Field(default=None, min_length=1)
    agent_type: str | None = Field(default=None, min_length=1)
    # Re-anchor an interval rule on this day (task timezone). A changed ``rrule`` otherwise
    # re-anchors at the moment of the edit.
    starts_on: date | None = None

    @model_validator(mode="after")
    def _validate_patch(self) -> UpdateScheduledTaskRequest:
        """Keep the public update surface to active/paused connected-host runs."""
        if self.state is not None and self.state not in {"active", "paused"}:
            raise ValueError("state must be 'active' or 'paused'; use DELETE to delete a task")
        if "workspace" in self.model_fields_set and self.workspace is None:
            raise ValueError("workspace cannot be null")
        if "host_id" in self.model_fields_set and self.host_id is None:
            raise ValueError("host_id cannot be null")
        if self.execution_target is not None and self.execution_target not in (
            _VALID_EXECUTION_TARGETS
        ):
            raise ValueError("execution_target must be 'connected_host' or 'managed_sandbox'")
        if self.execution_target == "managed_sandbox" and (
            "host_id" in self.model_fields_set or "workspace" in self.model_fields_set
        ):
            raise ValueError(_MANAGED_SANDBOX_WITH_HOST_MSG)
        if "agent_id" in self.model_fields_set and self.agent_id is None:
            raise ValueError("agent_id cannot be null")
        return self


_PROACTIVITY_LEVELS = frozenset({"off", "low", "normal", "high"})
_QUIET_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ProactivityRequest(BaseModel):
    """Body for ``PUT /v1/me/proactivity``. Omitted fields keep their stored value."""

    model_config = ConfigDict(extra="forbid")

    proactivity: str | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str | None = None

    @model_validator(mode="after")
    def _validate_prefs(self) -> ProactivityRequest:
        if self.proactivity is not None:
            _validate_prefs_fields(self.proactivity, self.quiet_start, self.quiet_end)
        elif ("quiet_start" in self.model_fields_set) != ("quiet_end" in self.model_fields_set):
            raise ValueError("quiet_start and quiet_end must be set together")
        else:
            _validate_prefs_fields("normal", self.quiet_start, self.quiet_end)
        return self


def _validate_prefs_fields(
    proactivity: str, quiet_start: str | None, quiet_end: str | None
) -> None:
    """Validate the merged proactivity level and quiet hours."""
    if proactivity not in _PROACTIVITY_LEVELS:
        raise ValueError("proactivity must be 'off', 'low', 'normal' or 'high'")
    for value in (quiet_start, quiet_end):
        if value is not None and not _QUIET_TIME_RE.match(value):
            raise ValueError("quiet_start/quiet_end must be local 'HH:MM'")
    if (quiet_start is None) != (quiet_end is None):
        raise ValueError("quiet_start and quiet_end must be set together")


def _prefs_to_response(prefs: OwnerPreferences) -> dict[str, Any]:
    """Serialize :class:`OwnerPreferences` to a JSON-safe dict."""
    return {
        "proactivity": prefs.proactivity,
        "quiet_start": prefs.quiet_start,
        "quiet_end": prefs.quiet_end,
        "timezone": prefs.timezone,
    }


def _anchor_epoch(starts_on: date, timezone: str) -> int:
    """Midnight of ``starts_on`` in ``timezone`` as epoch seconds."""
    return int(datetime.combine(starts_on, time.min, tzinfo=ZoneInfo(timezone)).timestamp())


def _upcoming_fires(
    rrule: str, timezone: str, anchor_epoch: int | None, count: int = 3
) -> list[str]:
    """The next ``count`` fire times as ISO-8601 strings in the task's own timezone.

    The Muse reads these back to the person ("Mondays at 09:00, starting Oct 12"), so they are
    local wall-clock times with the offset, never UTC. Uses the same stable anchor as the live
    scheduler, so an ``INTERVAL>1`` rule previews exactly what will fire. A preview must never
    take down the response around it, so any failure is logged and yields no fires.
    """
    try:
        zone = ZoneInfo(timezone)
        anchor = datetime.fromtimestamp(anchor_epoch, tz=zone) if anchor_epoch else None
        fires = next_fire_times(rrule, datetime.now(zone), zone, count, anchor)
    except Exception:
        _logger.exception("could not preview fires for rrule %r", rrule)
        return []
    return [fire.isoformat(timespec="minutes") for fire in fires]


def _to_response(
    task: ScheduledTask,
    *,
    last_run_status: str | None = None,
    next_run_at: str | None = None,
) -> dict[str, Any]:
    """Serialize a :class:`ScheduledTask` to a JSON-safe dict.

    :param last_run_status: The status of the task's most recent run
        (``succeeded`` / ``failed`` / ``skipped`` / ``running`` / ``scheduled``),
        or ``None`` when the task has never run. Surfaced so the Tasks list can
        render a completion badge without an extra per-row ``/runs`` fetch.
    :param next_run_at: ISO-8601 timestamp of the task's next scheduled fire as
        computed by the live scheduler (the server's authoritative anchor), or
        ``None`` when the task is paused / not armed. Deliberately server-sourced
        — the client must never recompute next-run (it can't match the server
        anchor for INTERVAL>1 rules).
    """
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "rrule": task.rrule,
        # JSON key preserved for API/UI stability; the DB column + entity
        # attribute are now ``user_id``.
        "owner_user_id": task.user_id,
        "agent_id": task.agent_id,
        "timezone": task.timezone,
        "created_at": task.created_at,
        "anchor_at": task.anchor_epoch,
        "model_override": task.model_override,
        "reasoning_effort": task.reasoning_effort,
        "permission_mode": task.permission_mode,
        "max_cost_usd": task.max_cost_usd,
        "workspace": task.workspace,
        "host_id": task.host_id,
        "execution_target": task.execution_target,
        "state": task.state,
        "parent_session_id": task.parent_session_id,
        "agent_type": task.agent_type,
        "kind": task.kind,
        "last_run_at": task.last_run_at,
        "last_run_status": last_run_status,
        "last_run_conversation_id": task.last_run_conversation_id,
        "next_run_at": next_run_at,
        "next_fire_times": (
            _upcoming_fires(task.rrule, task.timezone, task.anchor_epoch)
            if task.state == "active"
            else []
        ),
        "updated_at": task.updated_at,
    }


def _run_to_response(run: ScheduledTaskRun) -> dict[str, Any]:
    """Serialize a :class:`ScheduledTaskRun` to a JSON-safe dict.

    Excludes the free-text ``error`` blob (never SQL-queried, potentially
    large); ``error_code`` carries the queryable failure classification.
    """
    return {
        "id": run.id,
        "scheduled_task_id": run.scheduled_task_id,
        "status": run.status,
        "scheduled_at": run.scheduled_at,
        "conversation_id": run.conversation_id,
        "fired_at": run.fired_at,
        "finished_at": run.finished_at,
        "error_code": run.error_code,
        "attempt": run.attempt,
    }


def _validate_rrule_or_400(rrule: str) -> None:
    """Raise a 400 ``OmnigentError`` if the RRULE is invalid."""
    try:
        validate_rrule(rrule)
    except RRuleValidationError as exc:
        raise OmnigentError(f"invalid rrule: {exc}", code=ErrorCode.INVALID_INPUT) from exc


def _validate_timezone_or_400(timezone: str) -> None:
    """Raise a 400 ``OmnigentError`` if *timezone* is not a valid IANA timezone."""
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise OmnigentError(
            f"invalid timezone {timezone!r}: must be a valid IANA timezone name",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def create_scheduled_tasks_router(
    store: ScheduledTaskStore,
    *,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None = None,
    agent_cache: Any | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the scheduled-tasks router.

    Mounted with ``prefix="/v1"`` so paths are ``/v1/scheduled-tasks[/{id}]``.

    :param store: The shared :class:`ScheduledTaskStore`.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` disables auth (owner resolves to ``"local"``).
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        """Resolve the calling user, mapping the auth-disabled case to
        ``RESERVED_USER_LOCAL`` so single-user rows are always owned."""
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _scheduler(request: Request) -> Any | None:
        """The live scheduler off app state, or ``None`` if not running."""
        return getattr(request.app.state, "scheduled_task_scheduler", None)

    async def _validate_launch_inputs(
        request: Request,
        *,
        owner: str,
        agent_id: str,
        host_id: str | None,
        workspace: str | None,
        model_override: str | None,
        reasoning_effort: str | None,
        permission_mode: str | None,
        execution_target: str = "connected_host",
    ) -> tuple[str | None, str | None, str | None]:
        """Validate inputs that scheduled tasks persist into future sessions.

        Workspace is always optional. When it is unset the canonical workspace
        persists as ``None`` and the fire path defaults it to the launch host's
        home directory — this holds whether the host was pinned or is resolved
        from the owner's live hosts at fire time. Only a workspace pinned WITHOUT
        a host is an error (a path with no machine is meaningless). When both a
        host and a workspace are supplied, the workspace is validated against the
        host boundary here so a bad pin fails fast at create.

        A ``managed_sandbox`` target has no host/workspace to validate — the fire
        provisions a fresh sandbox — but the server must be able to launch one, so
        an unconfigured server rejects the task at create instead of failing every
        fire.
        """
        user_id = None if owner == RESERVED_USER_LOCAL else owner
        agent = await validate_session_agent(
            user_id=user_id,
            agent_id=agent_id,
            agent_store=agent_store,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        validated_model, validated_effort = validate_session_model_metadata(
            model_override=model_override,
            reasoning_effort=reasoning_effort,
        )
        # No available harness takes a permission_mode launch flag.
        await validate_permission_mode_agent_support(
            permission_mode=permission_mode,
            agent=agent,
            agent_cache=agent_cache,
        )
        if execution_target == "managed_sandbox":
            sandbox_config = getattr(request.app.state, "sandbox_config", None)
            if sandbox_config is None or not sandbox_config.managed_launch_supported:
                raise OmnigentError(
                    "managed sandboxes are not configured on this server",
                    code=ErrorCode.INVALID_INPUT,
                )
            return None, validated_model, validated_effort
        if host_id is not None:
            host_store = getattr(request.app.state, "host_store", None)
            if host_store is not None:
                host = await asyncio.to_thread(
                    resolve_host_owner,
                    user_id=user_id,
                    host_id=host_id,
                    host_store=host_store,
                )
                if host.sandbox_provider is not None:
                    raise OmnigentError(
                        "automations cannot use an existing sandbox; "
                        "select a new sandbox for each run",
                        code=ErrorCode.INVALID_INPUT,
                    )
        if workspace is None:
            # The fire resolves an omitted workspace to the authorized host's HOME.
            return None, validated_model, validated_effort
        if host_id is None:
            raise OmnigentError(
                "host_id required when workspace is set",
                code=ErrorCode.INVALID_INPUT,
            )
        canonical_workspace = await validate_existing_host_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=workspace,
            agent=agent,
            agent_cache=agent_cache,
            host_store=getattr(request.app.state, "host_store", None),
            host_registry=getattr(request.app.state, "host_registry", None),
        )
        return canonical_workspace, validated_model, validated_effort

    async def _validate_parent_binding(owner: str, parent_session_id: str, agent_type: str) -> str:
        """Check the parent is the owner's and ``agent_type`` is one of its
        sub-agent Types; returns the parent's agent id."""
        from omnigent.server.routes._sessions.helpers import _require_declared_subagent
        from omnigent.superchat.subagents import scheduled_helper_type

        # Session grants are keyed by the owner as-is, the ``"local"`` sentinel included:
        # mapping it to ``None`` here made require_access answer 401 on a single-user server.
        await require_access(
            owner, parent_session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        parent = await asyncio.to_thread(conversation_store.get_conversation, parent_session_id)
        if parent is None or parent.agent_id is None:
            raise OmnigentError("parent session not found", code=ErrorCode.NOT_FOUND)
        agent = await asyncio.to_thread(agent_store.get, parent.agent_id)
        if agent is None:
            raise OmnigentError("parent session agent not found", code=ErrorCode.NOT_FOUND)
        await asyncio.to_thread(
            _require_declared_subagent,
            agent=agent,
            # A retired Type (e.g. the Study's ``analyst``) is stored by name but runs as a
            # ``worker``, so a bundle only needs to declare the Type it runs as.
            sub_agent_name=scheduled_helper_type(agent_type),
            agent_cache=agent_cache,
        )
        return str(parent.agent_id)

    def _retry_stale(request: Request) -> Any:
        """Reconciler hook: a force-failed run of a parent-bound task gets its retry."""
        retry = getattr(request.app.state, "scheduled_task_retry", None)
        if retry is None:
            return None

        def _on_failed(run: ScheduledTaskRun) -> None:
            task = store.get(run.scheduled_task_id)
            if task is not None and task.parent_session_id is not None:
                retry(task.workspace_id, task.id, run.attempt)

        return _on_failed

    def _owns_task(task: ScheduledTask, owner: str | None) -> bool:
        return task.user_id == owner and (
            current_account_user() is None
            or task.account_generation == account_generation(owner or RESERVED_USER_LOCAL)
        )

    def _require_owned(scheduled_task_id: str, owner: str | None) -> ScheduledTask:
        """Load a task the caller owns, or raise 404.

        A task owned by someone else 404s (not 403) so tasks aren't
        enumerable across users.
        """
        task = store.get(scheduled_task_id)
        if task is None or not _owns_task(task, owner):
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        return task

    @router.post("/scheduled-tasks/preview-schedule")
    async def preview_schedule(request: Request, body: PreviewScheduleRequest) -> dict[str, Any]:
        """Validate an RRULE and show its next fire times in the person's timezone, unsaved."""
        owner = _owner(request)
        timezone = body.timezone
        if timezone is None:
            prefs = await asyncio.to_thread(store.get_owner_preferences, owner)
            timezone = prefs.timezone if prefs is not None else "UTC"
        _validate_timezone_or_400(timezone)
        _validate_rrule_or_400(body.rrule)
        # Same anchor rules as saving: the given start day, else the task's own anchor while
        # its rule is unchanged, else now (a new or edited rule counts from the moment it is set).
        anchor: int | None = None
        if body.starts_on is not None:
            anchor = _anchor_epoch(body.starts_on, timezone)
        elif body.scheduled_task_id is not None:
            owner_id = None if owner == RESERVED_USER_LOCAL else owner
            existing = _require_owned(body.scheduled_task_id, owner_id)
            if existing.rrule == body.rrule:
                anchor = existing.anchor_epoch
        return {
            "rrule": body.rrule,
            "timezone": timezone,
            "next_fire_times": _upcoming_fires(body.rrule, timezone, anchor),
        }

    @router.post("/scheduled-tasks")
    async def create_scheduled_task(
        request: Request,
        body: CreateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Create a scheduled task and arm it on the live scheduler."""
        owner = _owner(request)
        timezone = body.timezone
        if timezone is None:
            prefs = await asyncio.to_thread(store.get_owner_preferences, owner)
            timezone = prefs.timezone if prefs is not None else "UTC"
        _validate_timezone_or_400(timezone)
        rrule = body.rrule
        _validate_rrule_or_400(rrule)
        permission_mode = validate_session_permission_mode(body.permission_mode)
        agent_id = body.agent_id
        if body.parent_session_id is not None and body.agent_type is not None:
            agent_id = await _validate_parent_binding(
                owner, body.parent_session_id, body.agent_type
            )
        assert agent_id is not None  # guaranteed by the request validator
        workspace, model_override, reasoning_effort = await _validate_launch_inputs(
            request,
            owner=owner,
            agent_id=agent_id,
            host_id=body.host_id,
            workspace=body.workspace,
            model_override=body.model_override,
            reasoning_effort=body.reasoning_effort,
            permission_mode=permission_mode,
            execution_target=body.execution_target,
        )
        task = store.create(
            scheduled_task_id=uuid.uuid4().hex,
            name=body.name,
            prompt=body.prompt,
            rrule=rrule,
            user_id=None if owner == RESERVED_USER_LOCAL else owner,
            agent_id=agent_id,
            parent_session_id=body.parent_session_id,
            agent_type=body.agent_type,
            kind=body.kind,
            timezone=timezone,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            permission_mode=permission_mode,
            max_cost_usd=body.max_cost_usd,
            workspace=workspace,
            host_id=body.host_id,
            execution_target=body.execution_target,
            anchor_at=(
                _anchor_epoch(body.starts_on, timezone) if body.starts_on is not None else None
            ),
        )
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.add(task)
        # A freshly created task has no runs (last_run_status is None); its
        # next_run_at is available now that the scheduler armed it above.
        return _to_response(
            task,
            next_run_at=scheduler.next_run_at(task.id) if scheduler is not None else None,
        )

    @router.get("/scheduled-tasks")
    async def list_scheduled_tasks(
        request: Request,
        parent_session_id: str | None = Query(default=None),
    ) -> dict[str, list[dict[str, Any]]]:
        """List the caller's scheduled tasks.

        Lazy-on-read stale backstop: before returning, force-fail any of this
        owner's runs still ``running`` past the 6h max age (``incomplete``), so
        a future Tasks-list "last-run status" badge never shows a stale orphan
        as ``running``. Pure age check — one indexed, owner-scoped query for the
        owner's running runs, then a conditional ``update_run``; NO per-run
        conversation I/O. Young in-flight runs are untouched, and completion of
        a normal run is handled event-driven (the ``_publish_status`` hook), not
        here.
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        if parent_session_id is not None:
            try:
                uuid_to_bytes(parent_session_id)
            except InvalidUuidError:
                return {"scheduled_tasks": []}  # not a session id: nothing is bound to it
        tasks = [
            t
            for t in store.list(owner_user_id=owner_id, parent_session_id=parent_session_id)
            if _owns_task(t, owner_id)
        ]
        task_ids = [t.id for t in tasks]
        running = store.list_running_runs_for_tasks(task_ids)
        # Force-fail stale orphans FIRST so the completion badge below reports a
        # dead run as ``failed`` rather than a stuck ``running``.
        force_fail_stale_runs(store, running, on_force_failed=_retry_stale(request))
        latest_status = store.list_latest_run_status_for_tasks(task_ids)
        scheduler = _scheduler(request)
        return {
            "scheduled_tasks": [
                _to_response(
                    t,
                    # A run-now returns before its run row exists: report it running meanwhile.
                    last_run_status=(
                        "running" if is_firing(t.workspace_id, t.id) else latest_status.get(t.id)
                    ),
                    next_run_at=scheduler.next_run_at(t.id) if scheduler is not None else None,
                )
                for t in tasks
            ]
        }

    @router.get("/scheduled-tasks/{scheduled_task_id}")
    async def get_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Fetch one of the caller's scheduled tasks."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        task = _require_owned(scheduled_task_id, owner_id)
        running = store.list_running_runs_for_tasks([task.id])
        force_fail_stale_runs(store, running, on_force_failed=_retry_stale(request))
        latest_status = store.list_latest_run_status_for_tasks([task.id])
        scheduler = _scheduler(request)
        return _to_response(
            task,
            last_run_status=latest_status.get(task.id),
            next_run_at=scheduler.next_run_at(task.id) if scheduler is not None else None,
        )

    @router.get("/scheduled-tasks/{scheduled_task_id}/runs")
    async def list_scheduled_task_runs(
        request: Request,
        scheduled_task_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        after: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List the run history for one of the caller's scheduled tasks.

        Owner-scoped: a task owned by someone else (or absent) 404s via
        ``_require_owned``, so runs aren't enumerable across users. Runs come
        back most-recent-first (``scheduled_at DESC``); an empty history is an
        empty list.

        Cursor-paginated so history is never silently truncated: pass ``limit``
        (1-1000, default 100) and ``after`` (a prior page's ``next_cursor``).
        Each run carries its ``conversation_id`` (the session the fire created).
        The response is ``{"runs": [...], "next_cursor": <id or null>}``; a
        ``null`` cursor marks the last page.

        Lazy-on-read backstop: before listing, force-fail any of this task's
        runs still ``running`` past the 6h max age (``incomplete``). Completion
        itself is event-driven (the ``_publish_status`` hook); this only
        catches a genuine orphan — a run whose terminal event never fired (host
        died mid-turn) — so the "every run eventually terminal" invariant holds
        without a background poll or startup sweep. Pure age check (no
        conversation I/O); a young in-flight run is untouched, and the
        conditional ``update_run`` never clobbers an already-terminal row.
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(scheduled_task_id, owner_id)
        runs, next_cursor = store.list_runs(scheduled_task_id, limit=limit, after_id=after)
        runs = force_fail_stale_runs(store, runs, on_force_failed=_retry_stale(request))
        return {
            "runs": [_run_to_response(r) for r in runs],
            "next_cursor": next_cursor,
        }

    @router.post("/scheduled-tasks/{scheduled_task_id}/run", status_code=202)
    async def run_scheduled_task_now(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Trigger an immediate fire of one of the caller's scheduled tasks.

        A manual override that reuses the SHARED scheduled-fire path (the same
        ``on_fire`` machinery — session create, owner grant, runner launch,
        prompt dispatch, run recording) via ``app.state.scheduled_task_run_now``.
        It does NOT re-implement the fire logic and cannot collide with a
        scheduled fire of the same task (both share the in-flight overlap guard).

        Paused tasks ARE runnable here — run-now is an explicit manual override,
        so it does not require the ``active`` state the scheduler enforces.

        Fire-and-forget: like the scheduler, the session create + launch runs in
        the background, so this returns ``202 Accepted`` with the task id rather
        than the finished run. The new run row (status ``running`` / ``failed`` /
        ``skipped``) appears in the task's ``/runs`` history and in the LIST
        endpoint's ``last_run_status`` once recorded. Returns ``409`` when a fire
        for this task is already in flight, and ``503`` when the scheduler
        subsystem is not running (no trigger wired).
        """
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        task = _require_owned(scheduled_task_id, owner_id)
        run_now = getattr(request.app.state, "scheduled_task_run_now", None)
        if run_now is None:
            raise OmnigentError(
                "scheduled task scheduler is not running",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        started = await run_now(task.workspace_id, task.id)
        if not started:
            # The row exists (we just loaded it) and paused is allowed, so the
            # only skip reason is an already-in-flight fire for this task.
            raise OmnigentError(
                "a run for this scheduled task is already in flight",
                code=ErrorCode.CONFLICT,
            )
        return {"triggered": True, "id": task.id}

    @router.patch("/scheduled-tasks/{scheduled_task_id}")
    async def update_scheduled_task(
        request: Request,
        scheduled_task_id: str,
        body: UpdateScheduledTaskRequest,
    ) -> dict[str, Any]:
        """Update mutable fields of a task and re-sync the scheduler."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        existing = _require_owned(scheduled_task_id, owner_id)
        if body.timezone is not None:
            _validate_timezone_or_400(body.timezone)
        fields = body.model_dump(exclude_unset=True)
        starts_on = fields.pop("starts_on", None)
        if starts_on is not None:
            fields["anchor_at"] = _anchor_epoch(
                starts_on, fields.get("timezone") or existing.timezone
            )
        if not fields:
            raise OmnigentError(
                "nothing to update: send at least one field to change",
                code=ErrorCode.INVALID_INPUT,
            )
        if fields.get("rrule") is not None:
            _validate_rrule_or_400(fields["rrule"])
        target_agent_id = fields.get("agent_id") or existing.agent_id
        agent_changed = target_agent_id != existing.agent_id
        if agent_changed:
            # A harness switch invalidates the per-agent settings stored beside
            # it: a model id is provider-bound and permission_mode is Claude-only.
            # Clear whichever the caller did not resend so a switched task never
            # fires the new harness with the old one's flags.
            for stale in ("model_override", "reasoning_effort", "permission_mode"):
                fields.setdefault(stale, None)
        if {"model_override", "reasoning_effort"}.intersection(fields):
            model_override, reasoning_effort = validate_session_model_metadata(
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
            )
            if "model_override" in fields:
                fields["model_override"] = model_override
            if "reasoning_effort" in fields:
                fields["reasoning_effort"] = reasoning_effort
        if "permission_mode" in fields:
            new_mode = validate_session_permission_mode(fields["permission_mode"])
            fields["permission_mode"] = new_mode
            # Gate a newly-SET mode on the (immutable) agent's harness. Clearing
            # to null needs no gate — the fire path injects nothing for null.
            if new_mode is not None:
                agent = await validate_session_agent(
                    user_id=owner_id,
                    agent_id=target_agent_id,
                    agent_store=agent_store,
                    permission_store=permission_store,
                    conversation_store=conversation_store,
                )
                await validate_permission_mode_agent_support(
                    permission_mode=new_mode,
                    agent=agent,
                    agent_cache=agent_cache,
                )
        if {"parent_session_id", "agent_type"}.intersection(fields):
            new_parent = fields.get("parent_session_id", existing.parent_session_id)
            new_type = fields.get("agent_type", existing.agent_type)
            if (new_parent is None) != (new_type is None):
                raise OmnigentError(
                    "parent_session_id and agent_type must be set (or cleared) together",
                    code=ErrorCode.INVALID_INPUT,
                )
            if new_parent is not None and new_type is not None:
                await _validate_parent_binding(owner, new_parent, new_type)
        target_execution = fields.get("execution_target") or existing.execution_target
        switching_to_managed = target_execution == "managed_sandbox"
        # Reject pinning a host/workspace on a managed-sandbox task rather than
        # silently dropping it. The request model already rejects this when
        # execution_target is explicitly in the PATCH; this also covers a PATCH
        # that pins a field on an ALREADY-managed task (no execution_target sent),
        # validating against the EFFECTIVE target.
        if switching_to_managed and (
            fields.get("host_id") is not None or fields.get("workspace") is not None
        ):
            raise OmnigentError(
                _MANAGED_SANDBOX_WITH_HOST_MSG,
                code=ErrorCode.INVALID_INPUT,
            )
        if agent_changed or {"workspace", "host_id", "execution_target"}.intersection(fields):
            # On a switch this runs the full create-time gauntlet against the NEW
            # agent: existence + bindability, and the pinned workspace re-checked
            # against that agent's os_env boundary (the boundary is per-agent, so
            # a workspace valid for the old harness need not be valid here). A
            # managed_sandbox target validates against no host/workspace (the fire
            # provisions a fresh sandbox) and checks the server can launch one.
            workspace, _, _ = await _validate_launch_inputs(
                request,
                owner=owner,
                agent_id=target_agent_id,
                host_id=None if switching_to_managed else fields.get("host_id", existing.host_id),
                workspace=(
                    None if switching_to_managed else fields.get("workspace", existing.workspace)
                ),
                model_override=fields.get("model_override", existing.model_override),
                reasoning_effort=fields.get("reasoning_effort", existing.reasoning_effort),
                permission_mode=None,
                execution_target=target_execution,
            )
            if switching_to_managed:
                # A managed-sandbox task carries no pinned host or workspace. Clear
                # BOTH (workspace via the store's explicit-null) so a later switch
                # back to connected execution isn't rejected for "workspace without
                # host", and the fire never binds a dead pin.
                fields["host_id"] = None
                fields["workspace"] = None
            elif "workspace" in fields:
                fields["workspace"] = workspace
        updated = store.update(scheduled_task_id, **fields)
        if updated is None:
            raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.update(updated)
        latest_status = store.list_latest_run_status_for_tasks([updated.id])
        return _to_response(
            updated,
            last_run_status=latest_status.get(updated.id),
            next_run_at=scheduler.next_run_at(updated.id) if scheduler is not None else None,
        )

    @router.delete("/scheduled-tasks/{scheduled_task_id}")
    async def delete_scheduled_task(
        request: Request,
        scheduled_task_id: str,
    ) -> dict[str, Any]:
        """Delete a task and drop its timer from the scheduler."""
        owner = _owner(request)
        owner_id = None if owner == RESERVED_USER_LOCAL else owner
        _require_owned(scheduled_task_id, owner_id)
        store.delete(scheduled_task_id)
        scheduler = _scheduler(request)
        if scheduler is not None:
            scheduler.remove(scheduled_task_id)
        return {"deleted": True, "id": scheduled_task_id}

    def _prefs_key(request: Request) -> str:
        return _owner(request)

    @router.get("/me/proactivity")
    async def get_proactivity(request: Request) -> dict[str, Any]:
        """The caller's proactivity level and quiet hours (defaults when unset)."""
        owner = _prefs_key(request)
        prefs = await asyncio.to_thread(store.get_owner_preferences, owner)
        return _prefs_to_response(prefs or OwnerPreferences(user_id=owner))

    @router.put("/me/proactivity")
    async def put_proactivity(request: Request, body: ProactivityRequest) -> dict[str, Any]:
        """Replace the caller's proactivity level and quiet hours."""
        owner = _prefs_key(request)
        if body.timezone is not None:
            _validate_timezone_or_400(body.timezone)
        current = await asyncio.to_thread(store.get_owner_preferences, owner)
        current = current or OwnerPreferences(user_id=owner)
        sent = body.model_fields_set
        saved = await asyncio.to_thread(
            store.set_owner_preferences,
            OwnerPreferences(
                user_id=owner,
                proactivity=body.proactivity or current.proactivity,
                quiet_start=body.quiet_start if "quiet_start" in sent else current.quiet_start,
                quiet_end=body.quiet_end if "quiet_end" in sent else current.quiet_end,
                timezone=body.timezone or current.timezone,
            ),
        )
        return _prefs_to_response(saved)

    return router
