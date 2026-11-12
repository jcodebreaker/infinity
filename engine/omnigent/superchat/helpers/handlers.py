"""Runner side of ``start_helper``: one Helper through the generic sub-agent create path.

The call is translated into the ``sys_session_send`` create-path arguments and run by the same
function, so session creation, Activity, the wake/result path, the concurrency caps, the nesting
refusal and the model category are exactly the existing ones.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from omnigent.superchat.feature import HandlerCtx, SpawnRequest, resolve_project
from omnigent.superchat.models.tiers import CATEGORIES, category_of
from omnigent.superchat.subagents import HELPER_DEFAULT_MODEL, HELPER_EFFORT_CHOICES
from omnigent.superchat.titles import tidy_request_title

#: A repeat of the same task from the same chat inside this window is the same hand-off.
DEDUP_WINDOW_S = 10.0
_TITLE_MAX_CHARS = 48
_LEADING_AGENT_PREFIX = re.compile(r"^[^:]*:")

#: ``(chat id, normalized task) -> (monotonic start time, receipt)``.
_recent: dict[tuple[str, str], tuple[float, str]] = {}
_locks: dict[str, asyncio.Lock] = {}


#: A Sub-agent Type's own ``params:`` key declaring its Helper role (``worker`` or ``subworker``).
HELPER_TYPE_PARAM = "helper_type"
_ROLES = ("worker", "subworker")


def declared_role(sub: Any) -> str | None:
    """The Helper role a Sub-agent Type spec declares in ``params.helper_type``, if valid."""
    params = getattr(sub, "params", None)
    value = params.get(HELPER_TYPE_PARAM) if isinstance(params, Mapping) else None
    default = getattr(value, "default", value)  # the inner loader wraps scalars in ParamDef
    return default if default in _ROLES else None


def helper_roles_for(spec: Any) -> dict[str, str]:
    """``{type name: role}`` for the declared Types of ``spec`` that declare a role."""
    return {
        name: role
        for sub in getattr(spec, "sub_agents", None) or []
        if isinstance(name := getattr(sub, "name", None), str)
        and (role := declared_role(sub)) is not None
    }


def helper_role(name: str, roles: Mapping[str, str] | None = None) -> str | None:
    """The role of Type ``name``: its declared role, else ``name`` itself if it is a role."""
    return (roles or {}).get(name) or (name if name in _ROLES else None)


def helper_type(declared: Iterable[str], roles: Mapping[str, str] | None = None) -> str | None:
    """The Helper Type a caller starts: a ``worker`` Type if declared, else a ``subworker`` one.

    A Type's role comes from its ``params.helper_type``; a Type named ``worker`` or ``subworker``
    that declares nothing keeps that role.
    """
    names = [n for n in declared if not n.startswith("__")]
    for wanted in _ROLES:
        for name in names:
            if helper_role(name, roles) == wanted:
                return name
    return None


def helper_type_for(spec: Any) -> str | None:
    """:func:`helper_type` for the Types ``spec`` declares (tool gating, before any call)."""
    return helper_type(
        (
            n
            for sub in getattr(spec, "sub_agents", None) or []
            if isinstance(n := getattr(sub, "name", None), str)
        ),
        helper_roles_for(spec),
    )


def helper_title(task: str, taken: set[str], given: str | None = None) -> str | None:
    """A short sentence-case title, unique among ``taken``.

    ``given`` is the title the Muse chose; without a usable one the first words of ``task``
    are tidied, unless they read as a brief ("The person wants..."), which is no title.
    ``None`` leaves naming to the engine (the Activity feed then names it by the model).
    """
    base = tidy_request_title(given, limit=_TITLE_MAX_CHARS) if given else None
    base = base or tidy_request_title(task, limit=_TITLE_MAX_CHARS)
    if base is None:
        return None
    base = base.replace(":", " ").strip()
    candidate, n = base, 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{base} {n}"
    return candidate


def _normalize(task: str) -> str:
    return " ".join(task.casefold().split())


def _receipt(title: str, helper_id: str, *, agent: str = "worker") -> str:
    """The receipt; ``agent`` is the Type started, ``subworker`` when the caller is a worker."""
    if agent == "subworker":
        message = (
            f"Part started on '{title}'. Its result will arrive here as a message. When every "
            "part you started has reported, write the deliverable yourself; do not tell anyone. "
            "Do not end your turn without the deliverable unless you are waiting for parts."
        )
    else:
        message = (
            f"Helper started on '{title}'. Its result will arrive here as a message when it "
            "finishes. Tell the person in one sentence that you started it, then end your "
            "turn; do not wait or poll."
        )
    return json.dumps(
        {"started": True, "helper_id": helper_id, "title": title, "message": message}
    )


def _refusal(reason: str) -> str:
    return f"Error: start_helper: {reason}"


def _invalid_input(args: dict[str, Any]) -> str | None:
    """Why ``args`` cannot start a Helper (said in ``start_helper`` terms), or ``None``."""
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return "requires a non-empty 'task'"
    if not isinstance(args.get("title", ""), str):
        return "'title' must be a string"
    extra = set(args) - {"task", "title", "model", "reasoning", "files", "project", "path"}
    if extra:
        return f"does not take {', '.join(sorted(extra))}"
    if args.get("project") is not None and args.get("path") is not None:
        return "takes 'project' or 'path', not both"
    if not isinstance(model := args.get("model", HELPER_DEFAULT_MODEL), str) or (
        category_of(model) is None
    ):
        return f"'model' must be one of {', '.join(CATEGORIES)}"
    reasoning = args.get("reasoning")
    if reasoning is not None and reasoning not in HELPER_EFFORT_CHOICES:
        return f"'reasoning' must be one of {', '.join(HELPER_EFFORT_CHOICES)}"
    files = args.get("files")
    if files is not None and not (
        isinstance(files, list) and all(isinstance(f, str) and f for f in files)
    ):
        return "'files' must be a list of uploaded file ids"
    return None


def _helper_folder(args: dict[str, Any]) -> tuple[str, str | None] | str | None:
    """Where the Helper works: ``(folder, Project name)``; ``None`` keeps the caller's; or why not.

    ``project`` resolves exactly as ``open_project`` does. ``path`` is resolved (``..`` and
    symlinks followed) and must land inside the workspace, where a missing folder is made; the
    server's workspace route checks it again on the host.
    """
    slug, path = args.get("project"), args.get("path")
    if slug is not None:
        project = resolve_project(slug) if isinstance(slug, str) else None
        if project is None:
            return f"no Project '{slug}'"
        return str(project[0]), project[1]
    if path is None:
        return None
    if not isinstance(path, str) or not path.strip():
        return "'path' must be a folder in the workspace"
    root = (Path.home() / "workspace").resolve()
    folder = (root / path).resolve()
    if not folder.is_relative_to(root):
        return "'path' must be a folder inside the workspace"
    if folder.exists() and not folder.is_dir():
        return f"'{path}' is a file, not a folder"
    # A folder asked for new work (a new project's) is made here rather than refused.
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"could not make folder '{path}': {exc.strerror or exc}"
    return str(folder), None


def _prune(now: float) -> None:
    """Forget finished dedup entries and the per-chat locks nothing refers to any more."""
    for stale in [k for k, (at, _) in _recent.items() if now - at > DEDUP_WINDOW_S]:
        del _recent[stale]
    live_chats = {chat for chat, _ in _recent}
    for chat in [c for c, lock in _locks.items() if c not in live_chats and not lock.locked()]:
        del _locks[chat]


async def handle_start_helper(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Start one Helper from a ``start_helper`` call.

    :param ctx: The call context (carries the runner's sub-agent launcher).
    :param args: ``task`` plus optional ``model``, ``reasoning``, ``files`` and one of
        ``project`` / ``path`` (where it works; else the caller's current folder).
    :returns: A short receipt JSON, or an ``Error: ...`` string the model can act on.
    """
    host = ctx.sub_agents
    if host is None or not ctx.conversation_id:
        return "Error: start_helper requires server access"
    agent = helper_type(host.declared_types, host.helper_roles)
    if agent is None:
        return "Error: start_helper is not available here"
    invalid = _invalid_input(args)
    if invalid is not None:
        return _refusal(invalid)
    folder = _helper_folder(args)
    if isinstance(folder, str):
        return _refusal(folder)
    task: str = args["task"]
    key = (ctx.conversation_id, _normalize(task))

    async with _locks.setdefault(ctx.conversation_id, asyncio.Lock()):
        _prune(time.monotonic())
        if key in _recent:
            return _recent[key][1]

        taken = {
            _LEADING_AGENT_PREFIX.sub("", title).strip().casefold()
            for title in await host.child_titles()
        }
        title = helper_title(task, taken, args.get("title"))
        result = await host.spawn(
            SpawnRequest(
                agent=agent,
                task=task,
                title=title,
                model=args.get("model", HELPER_DEFAULT_MODEL),
                reasoning_effort=args.get("reasoning"),
                file_ids=tuple(args.get("files") or ()),
                workspace=folder[0] if folder else None,
                project_name=folder[1] if folder else None,
            )
        )
        if result.child_id is None:
            return _refusal(result.error)
        receipt = _receipt(
            title or "your task",
            result.child_id,
            agent=helper_role(agent, host.helper_roles) or "worker",
        )
        _recent[key] = (time.monotonic(), receipt)
        return receipt


def _message_refusal(reason: str) -> str:
    return f"Error: message_helper: {reason}"


async def handle_message_helper(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Pass a message to a Helper that is already working, through the generic send path.

    A running Helper reads it at its next step (the same steer a person's message gets) and
    its single result still arrives in the inbox; the message is never a second task.

    :param ctx: The call context (carries the runner's sub-agent host).
    :param args: ``message`` plus an optional ``helper_id`` (omit it when one Helper runs).
    :returns: A short receipt JSON, or an ``Error: ...`` string the model can act on.
    """
    host = ctx.sub_agents
    if host is None or host.send is None or host.running is None or not ctx.conversation_id:
        return "Error: message_helper requires server access"
    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return _message_refusal("requires a non-empty 'message'")
    if set(args) - {"message", "helper_id"}:
        return _message_refusal("takes only 'message' and 'helper_id'")
    helper_id = args.get("helper_id")
    if helper_id is not None and not (isinstance(helper_id, str) and helper_id):
        return _message_refusal("'helper_id' must be the id start_helper returned")
    running = await host.running()
    if helper_id is None:
        if not running:
            return _message_refusal("no Helper is running")
        if len(running) > 1:
            names = "; ".join(f"{title} ({child})" for child, title in running)
            return _message_refusal(f"several Helpers are running, say which one: {names}")
        helper_id = running[0][0]
    elif helper_id not in {child for child, _ in running}:
        return _message_refusal("that Helper is not running; its result has arrived or will")
    # The Helper decides as the message reaches it: idle by then, it refuses rather than
    # starting a second task (the check above only picks which Helper).
    output = await host.send(helper_id, message.strip())
    try:
        handle = json.loads(output)
    except ValueError:
        handle = None
    if isinstance(handle, dict) and handle.get("error") == "not_running":
        return _message_refusal("that Helper is not running; its result has arrived or will")
    if not (isinstance(handle, dict) and isinstance(handle.get("task_id"), str)):
        reason = output.removeprefix("Error: ").removeprefix("sys_session_send ")
        return _message_refusal(reason)
    return json.dumps(
        {
            "sent": True,
            "helper_id": helper_id,
            "message": (
                "Passed on. The Helper reads it at its next step and keeps working on the same "
                "task; its result still arrives here when it finishes. Do not start another "
                "Helper for this. Tell the person in one short sentence that you passed it on."
            ),
        }
    )
