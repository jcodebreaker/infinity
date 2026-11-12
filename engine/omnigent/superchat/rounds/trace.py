"""A Round is one trace (ADR 0041): its span is the root, and every turn of it joins.

Only with spans exported (:func:`~omnigent.harnesses.tracing.exports_spans`); otherwise nothing
here runs. :func:`open_span` starts the ``round`` span when the person's message opens the Round,
and its trace and span ids go on the stored ``round.opened``, so the Round view reads its
``trace_id`` and a later turn finds its parent after an engine restart. Each message a session
stores in a Round leaves that Round's ``traceparent`` (:func:`note`) for the forward to the
runner to take (:func:`take`): the runner hands it to the harness, whose turn span is then a
child of the Round's span. That covers the first turn, steers that start a turn, wake turns
after a Helper's result, and each Helper's turns (a Helper's session carries its Round). The
span ends when the Round settles (:func:`end_span`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace

from omnigent.runtime.telemetry import should_capture_content
from omnigent.superchat.rounds.fold import ROUND_OPENED

#: Session labels an eval try carries (``omnigent.eval.set`` ...), stamped on its Round's span
#: as ``eval.set`` ... .
EVAL_LABELS = ("set", "case", "run", "try")
#: Open Round spans kept; past it the oldest is ended (a Round that never settled here).
_KEPT = 1024

#: ``round -> its span``, from :func:`open_span` until :func:`end_span`.
_spans: dict[str, Any] = {}
#: ``session -> the traceparent of the Round of its last stored message``, until forwarded.
_pending: dict[str, str] = {}


def _traceparent(trace_id: str, span_id: str) -> str:
    return f"00-{trace_id}-{span_id}-01"


def open_span(
    round_id: str, conversation_id: str, labels: Mapping[str, str], ask: str
) -> dict[str, str]:
    """Start the Round's ``round`` span, a new trace.

    :param conversation_id: The Conversation, the span's ``session.id``.
    :param labels: The session's labels; an eval try's are stamped on the span.
    :param ask: The person's message, on the span only with content capture on.
    :returns: ``trace_id`` and ``span_id`` (hex) for the ``round.opened`` event.
    """
    attrs: dict[str, Any] = {
        "openinference.span.kind": "AGENT",
        "session.id": conversation_id,
        "superchat.round_id": round_id,
    }
    for name in EVAL_LABELS:
        if value := labels.get(f"omnigent.eval.{name}"):
            attrs[f"eval.{name}"] = value
    if ask and should_capture_content():
        attrs["input.value"] = ask
    # An empty context: the Round roots its own trace, never the request that stored the message.
    span = trace.get_tracer("omnigent").start_span(
        "round", context=otel_context.Context(), attributes=attrs
    )
    _spans[round_id] = span
    while len(_spans) > _KEPT:
        _spans.pop(next(iter(_spans))).end()
    ctx = span.get_span_context()
    return {"trace_id": f"{ctx.trace_id:032x}", "span_id": f"{ctx.span_id:016x}"}


def traceparent(round_id: str, events: Sequence[Mapping[str, Any]] = ()) -> str | None:
    """The Round's ``traceparent``: from its open span, else from its stored *events*
    (``round.opened`` carries the ids); ``None`` for a Round that was not traced."""
    span = _spans.get(round_id)
    if span is not None:
        ctx = span.get_span_context()
        return _traceparent(f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}")
    for ev in events:
        if ev.get("kind") == ROUND_OPENED and ev.get("trace_id") and ev.get("span_id"):
            return _traceparent(str(ev["trace_id"]), str(ev["span_id"]))
    return None


def note(session_id: str, parent: str | None) -> None:
    """Leave *parent* (a ``traceparent``) for the session's next forward to its runner."""
    if parent is None:
        _pending.pop(session_id, None)
    else:
        _pending[session_id] = parent


def take(session_id: str) -> str | None:
    """The ``traceparent`` :func:`note` left for *session_id*, once."""
    return _pending.pop(session_id, None)


def end_span(round_id: str, view: Mapping[str, Any]) -> None:
    """End the settled Round's span at its settlement, with its outcome."""
    span = _spans.pop(round_id, None)
    if span is None:
        return
    words = [w for w in view.get("words") or [] if w.strip()]
    if words and should_capture_content():
        span.set_attribute("output.value", words[-1])
    span.set_attribute("superchat.round.status", view["status"])
    if view["status"] == "failed":
        span.set_status(trace.StatusCode.ERROR)
    span.end(end_time=int(view["settled_at"] * 1e9))
