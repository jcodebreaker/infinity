"""Everything the engine stores per person, and how deleting a person removes it.

``delete_user`` used to remove what it knew about (sessions, hosts, keys, schedules) and left
the rest behind: skills, artifacts and their blobs, publications, memory rows. Every row is keyed
by the person's id (their email), which a returning account reuses, so whatever was left came
back with it, published apps included.

This module makes the delete complete by construction. Every table in the schema is classified
here, and ``tests/superchat/admin/test_user_data.py`` fails when a table is not: a new per-person
table cannot ship without deciding how it is removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from omnigent.db.db_models import OmnigentBase, current_workspace_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class Direct:
    """Rows carry the person's id in ``column``: delete where it equals that id."""

    column: str = "user_id"


@dataclass(frozen=True)
class Child:
    """Rows that hang off a person's rows in ``parent``, with no foreign key.

    ``column`` here names ``parent_column`` there.
    """

    parent: str
    column: str
    parent_column: str


@dataclass(frozen=True)
class Handled:
    """Removed by a dedicated step of ``delete_user`` (stores with side effects)."""

    reason: str


@dataclass(frozen=True)
class NotPerUser:
    """Not a person's data: organization-wide, or conversation-scoped (goes with the session)."""

    reason: str


Rule = Direct | Child | Handled | NotPerUser

_SESSION = NotPerUser("conversation-scoped: removed with the person's sessions")

USER_DATA: dict[str, Rule] = {
    # -- the person's own rows ------------------------------------------------------------
    "preferences": Direct(),
    "account_tokens": Direct(),
    "device_grants": Direct(),
    "session_permissions": Direct(),
    "projects": Direct(),
    "hosts": Direct(),
    "user_daily_cost": Direct(),
    "scheduled_tasks": Direct(),
    "owner_preferences": Direct(),
    "suggestions": Direct(),
    "artifacts": Direct(),
    "artifact_publications": Direct(),
    "artifact_live": Direct(),
    "objectives": Direct(),
    "memory_claims": Direct(),
    "memory_upkeep_runs": Direct(),
    "approval_rules": Direct(),
    "approval_settings": Direct(),
    "approval_spend": Direct(),
    "approval_pending": Direct(),
    "vault_secrets": Direct(),
    "vault_requests": Direct(),
    "vault_audit": Direct(),
    "daily_notes": Direct("owner"),
    "tasks": Direct("owner"),
    "lookout_signals": Direct("owner"),
    "lookout_owners": Direct("owner"),
    "lookout_wakes": Direct("owner"),
    "lookout_terms": Direct("owner"),
    "topics": Direct("owner"),
    "outbox_items": Direct("owner"),
    "outbox_cursors": Direct("owner"),
    "outbox_feedback": Direct("owner"),
    "rhythm_events": Direct("owner"),
    "rhythm_tallies": Direct("owner"),
    "reach_rules": Direct("owner"),
    "work_jobs": Direct("owner"),
    # -- rows that hang off those, with no foreign key ------------------------------------
    "artifact_views": Child("artifact_publications", "slug", "slug"),
    "objective_tasks": Child("objectives", "objective_id", "id"),
    "objective_proposals": Child("objectives", "objective_id", "id"),
    "scheduled_task_runs": Child("scheduled_tasks", "scheduled_task_id", "id"),
    # -- removed by dedicated steps in delete_user ----------------------------------------
    "users": Handled("the account itself, deleted last by the account store"),
    "connections": Handled("the model connection store, with its vault side effects"),
    "model_connection": Handled("the connection store; owner_id is shared with org scope"),
    # -- not a person's data --------------------------------------------------------------
    "agents": NotPerUser("organization-wide definitions; created_by is only a label"),
    "policies": NotPerUser("organization-wide; created_by is only a label"),
    "files": _SESSION,
    "comments": _SESSION,
    "activity_titles": _SESSION,
    "message_artifacts": _SESSION,
    "card_answers": _SESSION,
    "round_events": _SESSION,
    "omnigent_conversation_metadata": _SESSION,
}

#: Column names that mean "this row belongs to a person". A table with one of these may not be
#: classified ``NotPerUser``: the test enforces it.
PERSON_COLUMNS = ("user_id", "owner_user_id", "owner_id", "owner")


def _scoped(table: Any, *conditions: Any) -> list[Any]:
    out = list(conditions)
    if "workspace_id" in table.c:
        out.append(table.c.workspace_id == current_workspace_id())
    return out


def artifact_blob_keys(connection: Connection, user_id: str) -> list[str]:
    """Blob keys of the person's artifacts: delete these from the blob store before the rows."""
    table = OmnigentBase.metadata.tables["artifacts"]
    rows = connection.execute(
        select(table.c.blob_key).where(*_scoped(table, table.c.user_id == user_id))
    )
    return [row[0] for row in rows]


def delete_user_rows(connection: Connection, user_id: str) -> dict[str, int]:
    """Delete every row the engine stores for *user_id*; returns rows removed per table.

    Children go first (their parents' keys are read before the parents disappear), then the
    person's own rows. Idempotent: a person already gone deletes nothing.
    """
    tables = OmnigentBase.metadata.tables
    removed: dict[str, int] = {}
    for name, rule in USER_DATA.items():
        if not isinstance(rule, Child):
            continue
        child, parent = tables[name], tables[rule.parent]
        parent_rule = USER_DATA[rule.parent]
        assert isinstance(parent_rule, Direct)
        keys = select(parent.c[rule.parent_column]).where(
            *_scoped(parent, parent.c[parent_rule.column] == user_id)
        )
        result = connection.execute(
            delete(child).where(*_scoped(child, child.c[rule.column].in_(keys)))
        )
        removed[name] = result.rowcount or 0
    for name, rule in USER_DATA.items():
        if not isinstance(rule, Direct):
            continue
        table = tables[name]
        result = connection.execute(
            delete(table).where(*_scoped(table, table.c[rule.column] == user_id))
        )
        removed[name] = result.rowcount or 0
    return removed
