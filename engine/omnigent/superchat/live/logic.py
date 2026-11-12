"""What a live update may change: the rules of ``docs/artifacts.md`` as a pure function.

Per change: apply it, turn it into a suggestion (the person owns that part now), skip it (the
person undid that value, or deleted the part) or report it unplaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omnigent.superchat.artifacts.parts import PartEditor, PartError

#: Most update runs a host takes at once, however idle it is.
MAX_ROOM = 6


def room_for(mem_free: float | None, load: float | None) -> int:
    """How many runs a host has room for: 1 to :data:`MAX_ROOM`.

    ``mem_free`` is the share of its memory still free (0 to 1) and ``load`` its one-minute load
    per CPU; the tighter of the two decides, and a load of 1.5 per CPU or more leaves room for one.
    A reading it could not take does not count against it.
    """
    memory = 1.0 if mem_free is None else min(max(mem_free, 0.0), 1.0)
    cpu = 1.0 if load is None else 1.0 - min(max(load, 0.0) / 1.5, 1.0)
    return max(1, min(MAX_ROOM, round(MAX_ROOM * min(memory, cpu))))


@dataclass
class Plan:
    """The outcome of one run, before anything is stored."""

    data: bytes
    written: dict[str, str]
    rejected: list[list[str]]
    applied: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    #: Values the person undid before: left as they are.
    declined: list[str] = field(default_factory=list)


def plan_changes(
    data: bytes, changes: list[dict[str, Any]], state: dict[str, Any], editor: PartEditor
) -> Plan:
    """Decide what each ``{id, after, source?, notable?}`` does to the newest bytes ``data``;
    ``editor`` reads and writes the file's parts (a page's elements, a sheet's cells)."""
    plan = Plan(data, dict(state["written"]), list(state["rejected"]))
    for change in changes:
        ident, after, source = str(change["id"]), str(change["after"]), change.get("source")
        if [ident, after] in plan.rejected:
            plan.declined.append(ident)
            continue
        try:
            current = editor.read(plan.data, ident)
        except PartError:
            plan.unplaced.append(ident)
            continue
        if current is None:  # the person deleted a part an update wrote, or the id is wrong
            (plan.removed if ident in plan.written else plan.unplaced).append(ident)
        elif editor.same(current, after):
            plan.written[ident] = after
        elif ident in plan.written and not editor.same(current, plan.written[ident]):
            plan.suggestions.append(
                {"id": ident, "yours": current, "source_value": after, "source": source}
            )
        else:
            try:
                plan.data = editor.write(plan.data, ident, after)
            except PartError:
                plan.unplaced.append(ident)
                continue
            notable = bool(change.get("notable"))
            plan.applied.append(
                {
                    "id": ident,
                    "before": current,
                    "after": after,
                    "source": source,
                    "notable": notable,
                }
            )
            plan.written[ident] = after
            plan.rejected = [r for r in plan.rejected if r[0] != ident]
    return plan
