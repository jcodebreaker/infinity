"""A tool call is stored the moment it starts, not when it finishes (ADR 0026).

The engine stores a ``function_call`` only in its ``completed`` form, and a harness sends that
form once the tool has returned: until then the Activity panel and the work strip had no step to
show, however long the tool ran. The runner's relay sees the call start earlier, as an
``in_progress`` (a call the harness runs itself) or ``action_required`` (a call dispatched back
to Omnigent) frame carrying the full name and arguments. :class:`ToolStarts` turns that first
frame into the stored form right away and drops the repeat that arrives when the tool finishes,
so each call is stored once, at its start. The ``function_call_output`` still lands when the tool
returns, which is what marks the step done.

The engine persists a ``function_call`` only with status ``completed``, so the stored start
carries that status; what says a step finished is its output. A call that never got one (the
turn was stopped, the harness died) is closed by :meth:`ToolStarts.unfinished` when the stream
ends, so it reads as stopped rather than done.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The ``function_call`` status the engine stores.
_STORED_STATUS = "completed"
#: The output a call stored at its start gets when the turn ended before the tool returned.
STOPPED_OUTPUT = "Stopped before it finished."


def _function_call(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if event.get("type") != "response.output_item.done":
        return None
    item = event.get("item")
    if not isinstance(item, Mapping) or item.get("type") != "function_call":
        return None
    call_id = item.get("call_id")
    return item if isinstance(call_id, str) and call_id else None


class ToolStarts:
    """One turn's tool calls already stored, by ``call_id``."""

    def __init__(self) -> None:
        self._stored: set[str] = set()
        self._open: dict[str, None] = {}

    def started(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        """The stored form of a call that just started, the first time its call is seen.

        :param event: One frame from the harness stream.
        :returns: A ``response.output_item.done`` frame carrying the call as ``completed``, to
            send ahead of *event*; ``None`` for any other frame, or a call already stored.
        """
        item = _function_call(event)
        if item is None or item.get("status") == _STORED_STATUS:
            return None
        call_id = item["call_id"]
        if call_id in self._stored:
            return None
        self._stored.add(call_id)
        self._open[call_id] = None
        return {
            "type": "response.output_item.done",
            "item": {**item, "status": _STORED_STATUS},
        }

    def is_repeat(self, event: Mapping[str, Any]) -> bool:
        """Whether *event* is the completed form of a call already stored at its start.

        A completed call seen for the first time (a harness that only reports finished calls)
        is new: it is remembered and passes through.
        """
        output = event.get("item")
        if (
            event.get("type") == "response.output_item.done"
            and isinstance(output, Mapping)
            and output.get("type") == "function_call_output"
        ):
            self._open.pop(str(output.get("call_id")), None)
        item = _function_call(event)
        if item is None or item.get("status") != _STORED_STATUS:
            return False
        call_id = item["call_id"]
        if call_id in self._stored:
            return True
        self._stored.add(call_id)
        return False

    def unfinished(self) -> list[dict[str, Any]]:
        """Outputs closing the calls stored at their start that never returned, oldest first.

        Called as the turn's terminal frame arrives, so they are stored ahead of it; each call
        is closed only once.
        """
        closing = [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": STOPPED_OUTPUT,
                },
            }
            for call_id in self._open
        ]
        self._open.clear()
        return closing
