"""Live artifact operations over the artifact, live and scheduled-task stores.

Blocking: callers run them in a thread. Rules: ``docs/artifacts.md``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Any

from omnigent.context.rollover import _MID_TURN_LIVE_STATUSES
from omnigent.db.db_models import current_workspace_id
from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.superchat.artifacts.parts import PartEditor, part_editor
from omnigent.superchat.artifacts.routes import producing_turn
from omnigent.superchat.artifacts.store import (
    HAND_ORIGINS,
    Artifact,
    SqlAlchemyArtifactStore,
    VersionConflictError,
)
from omnigent.superchat.artifacts.studio import group_of, is_studio_of, studio_of
from omnigent.superchat.live.logic import Plan, plan_changes, room_for
from omnigent.superchat.live.store import Live, SqlAlchemyLiveStore, new_state

LIVE_TASK_KIND = "live_artifact"
#: A hand edit this recent holds the next run back: the person is still working on the page.
DEFER_SECONDS = 600
#: While the file's studio has a turn running (ADR 0024), the run looks again this much later.
STUDIO_RECHECK_SECONDS = 60
#: Runs in a row with changes they cannot place before the schedule pauses.
MAX_MISSES = 2
#: Failed runs in a row before the schedule pauses.
MAX_FAILURES = 3
#: A source file must stay quiet this long before its page updates (a burst of saves is one run).
CHANGE_DEBOUNCE_SECONDS = 120.0
#: A host reports its load every ~30 s; older than this, nobody knows its room.
LOAD_FRESH_SECONDS = 120.0


#: State an update remembers (the rest, sources, schedule and note, is the person's settings).
_FORGOTTEN = (
    "written",
    "facts",
    "rejected",
    "last_update",
    "open_issue",
    "misses",
    "failures",
    "last_failure",
)


def task_prompt(name: str, kind: str = "html") -> str:
    """The fixed instruction a live artifact's scheduled run starts with."""
    what = "page" if kind == "html" else "sheet"
    return (
        f'Update the live {what} "{name}" from its sources.\n'
        f'1. Call live_update_context with name "{name}".\n'
        "2. Read each source in the host: open a web page in the browser, read a file with "
        "code. Work out every number with code, never guess one. If there are no sources, infer "
        f"them from the {what} and this chat and pass them as sources when you save.\n"
        f"3. Call live_update_save once with only the facts that differ from what the {what} "
        "shows (part id as `ids` in the context says, new text, source) and the facts you read; "
        f"no changes when nothing differs. If it says the {what} changed, start again at step "
        "1.\n"
        "4. End with a one-line Result that follows the tell rule live_update_save returned and "
        "names each change as what it is, before → after ('Saffron chai $18.25 → $19.50').\n"
        "Also follow the `note` live_update_context returned: it is the person's own words on "
        "what to update and what to leave alone, and it wins over your own judgment.\n"
        f"If you cannot finish, say why in the Result and add that the {what} stays as it is "
        "until the next run: the update is yours, nobody should redo it by hand or in chat."
    )


def _err(message: str, code: ErrorCode) -> OmnigentError:
    return OmnigentError(message, code=code)


class LiveService:
    """Every change to a live row and to its scheduled task."""

    def __init__(
        self,
        artifacts: SqlAlchemyArtifactStore,
        live: SqlAlchemyLiveStore,
        tasks: Any,
        conversations: Any,
        scheduler: Callable[[], Any | None],
    ) -> None:
        """``scheduler`` returns the running scheduler, or ``None``."""
        self.artifacts = artifacts
        self.live = live
        self.tasks = tasks
        self.conversations = conversations
        self._scheduler = scheduler
        #: Returns the scheduler's ``run_now(workspace_id, task_id)``, or ``None``.
        self.run_now: Callable[[], Any | None] = lambda: None
        self.debounce = CHANGE_DEBOUNCE_SECONDS
        self._timers: dict[tuple[str, str], asyncio.TimerHandle] = {}
        #: owner -> (room, monotonic time): what their host last said it has room for.
        self._room: dict[tuple[int, str | None], tuple[int, float]] = {}
        self._fires: set[asyncio.Task[None]] = set()

    #: The server's event loop, noted by the routes: the scheduler's timers live on it, and the
    #: service runs in worker threads.
    loop: asyncio.AbstractEventLoop | None = None

    def _on_scheduler(self, call: Callable[[Any], None]) -> None:
        """Run ``call(scheduler)`` on the event loop. Skipped without one: the fire path re-reads
        the task, so a stale timer never runs a paused or deleted task."""
        if (scheduler := self._scheduler()) is None or self.loop is None:
            return
        self.loop.call_soon_threadsafe(call, scheduler)

    def _sync(self, task: Any) -> None:
        self._on_scheduler(lambda scheduler: scheduler.update(task))

    def upsert(
        self,
        item: Artifact,
        *,
        rrule: str,
        sources: list[str] | None = None,
        tell: str | None = None,
        follow_publish: bool | None = None,
        on_change: bool | None = None,
        note: str | None = None,
    ) -> Live:
        """Make the page live, or change its schedule; setting it again resumes a paused one."""
        live = self.live.get(item.parent_session_id, item.name)
        task = self.tasks.get(live.task_id) if live else None
        if task is None:
            parent = self.conversations.get_conversation(item.parent_session_id)
            if parent is None or parent.agent_id is None:
                raise _err("chat not found", ErrorCode.NOT_FOUND)
            prefs = self.tasks.get_owner_preferences(item.user_id or RESERVED_USER_LOCAL)
            task = self.tasks.create(
                scheduled_task_id=uuid.uuid4().hex,
                name=f"Keep {item.title or item.name} up to date",
                prompt=task_prompt(item.name, item.kind),
                rrule=rrule,
                user_id=item.user_id,
                agent_id=parent.agent_id,
                timezone=(prefs and prefs.timezone) or "UTC",
                parent_session_id=item.parent_session_id,
                agent_type="worker",
                kind=LIVE_TASK_KIND,
            )
        else:
            task = self.tasks.update(
                task.id, rrule=rrule, state="active", prompt=task_prompt(item.name, item.kind)
            )
        self._sync(task)
        live = live or Live(item.parent_session_id, item.name, item.user_id, task.id)
        live.task_id = task.id
        live.sources = live.sources if sources is None else sources
        live.tell = tell or live.tell
        live.follow_publish = live.follow_publish if follow_publish is None else follow_publish
        if on_change is not None:
            live.state["on_change"] = on_change
        if note is not None:
            live.state["note"] = note.strip()
        live.state.update(misses=0, open_issue=None, failures=0, last_failure=None)
        self.live.save(live)
        return live

    def remove(self, item: Artifact) -> bool:
        """Stop updating: the task and the row go, the versions stay. ``False`` if not live."""
        live = self.live.get(item.parent_session_id, item.name)
        if live is None:
            return False
        self.tasks.delete(live.task_id)
        task_id = live.task_id
        self._on_scheduler(lambda scheduler: scheduler.remove(task_id))
        self.live.delete(item.parent_session_id, item.name)
        return True

    def reset(self, item: Artifact) -> Live:
        """Start fresh: forget what the updates wrote and declined, keep the settings."""
        live, _ = self.need(item)
        fresh = new_state()
        live.state.update({k: fresh[k] for k in _FORGOTTEN})
        self.live.save(live)
        return live

    def note_load(self, user_id: str | None, mem_free: float | None, load: float | None) -> None:
        """The owner's host reported its free memory share and load per CPU."""
        key = (current_workspace_id(), user_id)
        self._room[key] = (room_for(mem_free, load), time.monotonic())

    def room(self, task: Any) -> int | None:
        """Runs the task owner's host has room for, or ``None`` without a fresh report."""
        owner = None if task.user_id == RESERVED_USER_LOCAL else task.user_id
        room, at = self._room.get((task.workspace_id, owner), (0, 0.0))
        return room if room and time.monotonic() - at < LOAD_FRESH_SECONDS else None

    def watched(self, user_id: str | None) -> list[str]:
        """File paths whose change starts an update (what the host polls)."""
        lives = self.live.of_user(user_id)
        return sorted(
            {s for v in lives if v.state["on_change"] for s in v.sources if "://" not in s}
        )

    def changed(self, user_id: str | None, paths: list[str]) -> list[tuple[str, str]]:
        """Pages to update because a source in ``paths`` changed (never the page's own file)."""
        hit = set(paths)
        return [
            (v.parent_session_id, v.name)
            for v in self.live.of_user(user_id)
            if v.state["on_change"]
            and hit & {s for s in v.sources if s.rsplit("/", 1)[-1] != v.name}
        ]

    def arm(self, key: tuple[str, str]) -> None:
        """(Re)start the page's debounce on the event loop; at the end its update starts."""
        loop = self.loop
        if loop is None:
            return
        if (old := self._timers.pop(key, None)) is not None:
            old.cancel()
        self._timers[key] = loop.call_later(self.debounce, self._spawn, key)

    def _spawn(self, key: tuple[str, str]) -> None:
        self._timers.pop(key, None)
        task = asyncio.ensure_future(self._fire(key))
        self._fires.add(task)
        task.add_done_callback(self._fires.discard)

    async def _fire(self, key: tuple[str, str]) -> None:
        """Start the update; if one is running, look again after another debounce."""
        live = await asyncio.to_thread(self.live.get, *key)
        task = await asyncio.to_thread(self.tasks.get, live.task_id) if live else None
        run_now = self.run_now()
        if live is None or task is None or task.state != "active" or run_now is None:
            return
        if not await run_now(task.workspace_id, task.id):
            self.arm(key)

    def defer_until(self, task: Any) -> float | None:
        """The fire path's hook: a recent hand edit holds the run back until it is old enough,
        and a running studio turn on the file (ADR 0024) until it has ended."""
        live = self.live.by_task(task.id)
        if live is None:
            return None
        group = self.artifacts.versions_of(
            user_id=live.user_id, parent_session_id=live.parent_session_id, name=live.name
        )
        if group and self._studio_running(group[0]):
            return time.time() + STUDIO_RECHECK_SECONDS
        hand = group and group[0].origin in HAND_ORIGINS
        return group[0].created_at + DEFER_SECONDS if hand else None

    def _studio_running(self, item: Artifact) -> bool:
        """Whether the file's studio has a turn running now."""
        studio = self.conversations.get_conversation(studio_of(item))
        return (
            is_studio_of(studio, group_of(item))
            and studio is not None
            and studio.live_status in _MID_TURN_LIVE_STATUSES
        )

    def on_run_outcome(self, task: Any, failure: str | None) -> str | None:
        """The fire path's hook: count failed runs in a row, reset on success. The third pauses the
        schedule and returns the one note for the Conversation."""
        live = self.live.by_task(task.id)
        if live is None:
            return None
        state = live.state
        if failure is None:
            if state["failures"] or state["last_failure"]:
                state.update(failures=0, last_failure=None)
                self.live.save(live)
            return None
        state["failures"] += 1
        state["last_failure"] = {"at": now_epoch(), "reason": failure}
        pause = state["failures"] == MAX_FAILURES
        if pause:
            self._sync(self.tasks.update(live.task_id, state="paused"))
        self.live.save(live)
        if not pause:
            return None
        return (
            f'The updates to "{live.name}" are paused: {MAX_FAILURES} updates in a row failed '
            f"({failure}). Save the page's live settings again to resume."
        )

    def need(self, item: Artifact) -> tuple[Live, Artifact]:
        """The live row and the newest version, or a 404 when the page is not live."""
        live = self.live.get(item.parent_session_id, item.name)
        head = self.artifacts.newest(item)
        if live is None or head is None:
            raise _err("That page is not kept updated", ErrorCode.NOT_FOUND)
        return live, head

    def context(self, item: Artifact) -> dict[str, Any]:
        """What a run needs before it reads the sources."""
        live, head = self.need(item)
        editor = _editor(head)
        return {
            "sources": live.sources,
            "facts": live.state["facts"],
            "note": live.state["note"],
            "version": head.version,
            "source": editor.describe(self.artifacts.read(head)),
            "ids": editor.ids,
        }

    def save(
        self,
        item: Artifact,
        *,
        base_version: int,
        changes: list[dict[str, Any]],
        facts: dict[str, Any],
        sources: list[str] | None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """A run's outcome: at most one ``update`` version; no change moves ``checked_at`` only.

        ``chat_id`` is the run's chat; the version records it and the run's turn.
        """
        live, head = self.need(item)
        if changes and head.version != base_version:
            raise _stale()
        made_by = (
            producing_turn(self.conversations, head.parent_session_id, chat_id)
            if chat_id
            else (None, None)
        )
        plan, created = self._apply(live, head, changes, made_by=made_by)
        state = live.state
        if created is not None:
            unusual = "suggestions" if plan.suggestions else "unplaced" if plan.unplaced else None
            self._follow(live, created, base=head.version, hold=unusual)
        state["misses"] = state["misses"] + 1 if plan.unplaced else 0
        state["open_issue"] = (
            "Could not place these changes on the page: " + ", ".join(plan.unplaced)
            if plan.unplaced
            else None
        )
        if state["misses"] >= MAX_MISSES:
            self._sync(self.tasks.update(live.task_id, state="paused"))
        state.update(facts=facts, checked_at=now_epoch())
        live.sources = live.sources if sources is None else sources
        self.live.save(live)
        return {
            "version": created.version if created else None,
            "applied": plan.applied,
            "suggestions": plan.suggestions,
            "unplaced": plan.unplaced,
            "removed": plan.removed,
            "declined": plan.declined,
            "held": bool(
                created and state["held"] and state["held"]["version"] == created.version
            ),
            "tell": live.tell,
        }

    def undo(self, item: Artifact) -> Artifact:
        """Restore the version before the newest update and reject what that update wrote."""
        live, head = self.need(item)
        last = live.state["last_update"]
        if not last or last["version"] != head.version or head.origin != "update":
            raise _err("Only the newest update can be undone", ErrorCode.CONFLICT)
        previous = self.artifacts.get(head.parent_version_id or "", user_id=head.user_id)
        if previous is None:
            raise _err("Version not found", ErrorCode.NOT_FOUND)
        data = self.artifacts.read(previous)
        created = self._create(head, data, "undid the last update", "restore")
        for change in last["applied"]:
            live.state["rejected"].append([change["id"], change["after"]])
            live.state["written"].pop(change["id"], None)
        last["applied"] = []
        held = live.state["held"]
        if held and held["version"] == head.version:
            live.state["held"] = None  # the published copy never took the update
        else:
            self._follow(live, created, base=head.version)
        self.live.save(live)
        return created

    def accept(self, item: Artifact, element_id: str) -> Artifact:
        """Take the source's value for a part the person changed; it follows the source again."""
        live, head = self.need(item)
        suggestions = (live.state["last_update"] or {}).get("suggestions", [])
        suggestion = next((s for s in suggestions if s["id"] == element_id), None)
        if suggestion is None:
            raise _err("No such suggestion", ErrorCode.NOT_FOUND)
        live.state["written"].pop(element_id, None)
        live.state["rejected"] = [r for r in live.state["rejected"] if r[0] != element_id]
        change = {**suggestion, "after": suggestion["source_value"]}
        _, created = self._apply(live, head, [change], "took the source's value")
        if created is None:
            raise _err("That part of the page can no longer be changed", ErrorCode.CONFLICT)
        held = live.state["held"]
        self._follow(live, created, base=head.version, hold=held and held["reason"])
        self.live.save(live)
        return created

    def publish_held(self, item: Artifact) -> None:
        """Publish the newest version for an update the published copy held back."""
        live, head = self.need(item)
        pub = self.artifacts.publication_for(head)
        if not live.state["held"] or pub is None:
            raise _err("No update is waiting to be published", ErrorCode.CONFLICT)
        self.artifacts.publish(head, audience=pub.audience, version=head.version)
        live.state["held"] = None
        self.live.save(live)

    def _apply(
        self,
        live: Live,
        head: Artifact,
        changes: list[dict[str, Any]],
        summary: str = "",
        made_by: tuple[str | None, str | None] = (None, None),
    ) -> tuple[Plan, Artifact | None]:
        """Plan ``changes`` on ``head``, store one version, update the state in memory.

        :param made_by: ``(chat_id, turn_item_id)`` of the run that made the version.
        """
        state = live.state
        plan = plan_changes(self.artifacts.read(head), changes, state, _editor(head))
        created = None
        if plan.applied:
            count = len(plan.applied)
            summary = summary or f"{count} change{'' if count == 1 else 's'}"
            created = self._create(head, plan.data, summary, "update", made_by)
        kept = (state["last_update"] or {}).get("suggestions", [])
        touched = {c["id"] for c in plan.suggestions + plan.applied}
        suggestions = [s for s in kept if s["id"] not in touched] + plan.suggestions
        if created is not None:
            last = {"version": created.version, "applied": plan.applied, "at": now_epoch()}
            state["last_update"] = {**last, "suggestions": suggestions}
        elif suggestions != kept:
            last = state["last_update"] or {"version": None, "applied": [], "at": now_epoch()}
            state["last_update"] = {**last, "suggestions": suggestions}
        state.update(written=plan.written, rejected=plan.rejected)
        return plan, created

    def _create(
        self,
        head: Artifact,
        data: bytes,
        summary: str,
        origin: str,
        made_by: tuple[str | None, str | None] = (None, None),
    ) -> Artifact:
        try:
            return self.artifacts.create(
                user_id=head.user_id,
                parent_session_id=head.parent_session_id,
                name=head.name,
                title=head.title,
                kind=head.kind,
                mime=head.mime,
                data=data,
                origin=origin,
                parent_version_id=head.id,
                source_path=head.source_path,
                edit_summary=summary,
                base_version=head.version,
                chat_id=made_by[0],
                turn_item_id=made_by[1],
            )
        except VersionConflictError as exc:
            raise _stale() from exc

    def _follow(
        self, live: Live, created: Artifact, *, base: int, hold: str | None = None
    ) -> None:
        """Republish (same audience) when the published copy follows updates.

        An unusual update (``hold``: it has suggestions or unplaced changes) is held back, and so
        is one on top of hand edits the person has not published (``base``, the version it was
        made from, is not the published one): the person publishes it from the page.
        """
        pub = self.artifacts.publication_for(created)
        if not live.follow_publish or pub is None:
            return
        reason = hold or ("edits" if pub.published_version != base else None)
        if reason:
            live.state["held"] = {"version": created.version, "reason": reason}
            return
        self.artifacts.publish(created, audience=pub.audience, version=created.version)
        live.state["held"] = None


def _editor(item: Artifact) -> PartEditor:
    editor = part_editor(item.kind, item.name)
    if editor is None:
        raise _err("Only a page or a sheet can be kept updated", ErrorCode.INVALID_INPUT)
    return editor


def _stale() -> OmnigentError:
    return _err(
        "The page changed since you read it. Call live_update_context again and redo the update.",
        ErrorCode.ARTIFACT_STALE,
    )
