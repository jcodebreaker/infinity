"""
Tests for the client-side elicitation wiring.

Covers:

- :func:`omnigent_client._sse._parse_event` —
  ``response.elicitation_request`` events parse to
  :class:`ElicitationRequest` with the MCP-shape ``params``
  block surfaced as flat fields on the dataclass.
- :func:`omnigent_client._sse._parse_output_item` — regular
  ``function_call`` items still parse to :class:`ToolCall`
  (guards against accidental re-introduction of a
  reserved-name carve-out).
- :func:`omnigent_client._responses._handle_elicitation_request`
  — calls the registered hook, POSTs the verdict to the
  elicitation's dedicated resolve URL, and fail-closes when no hook
  is registered.
- REPL ``_make_elicitation_prompt`` — renders the elicitation
  and returns ``True`` / ``False`` based on the user's y/n
  answer.

Real HTTP is stubbed via a minimal ``_FakeHttpClient`` — these
tests exercise the SDK's branching logic, not the server.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# The editable-install of ``omnigent_client`` points at the
# sibling worktree. Load this worktree's copy under a distinct
# module name so we're actually testing the code we just
# edited. Same pattern the e2e suite uses via PYTHONPATH.
_SDK_ROOT = (
    Path(__file__).resolve().parents[2].parent / "sdks" / "python-client" / "omnigent_client"
)


def _load_sdk_module(name: str) -> Any:
    """
    Load ``omnigent_client.<name>`` from this worktree
    regardless of which ``omnigent_client`` is resolved
    globally. Registering under ``_apc_under_test.<name>``
    so parent-package resolution works for helpers that
    reference sibling submodules.

    :param name: The module's basename without the ``.py``,
        e.g. ``"_events"``.
    :returns: The freshly-loaded module object.
    """
    parent_name = "_apc_under_test"
    if parent_name not in sys.modules:
        parent_spec = importlib.util.spec_from_file_location(
            parent_name,
            _SDK_ROOT / "__init__.py",
            submodule_search_locations=[str(_SDK_ROOT)],
        )
        assert parent_spec is not None and parent_spec.loader is not None
        parent_mod = importlib.util.module_from_spec(parent_spec)
        sys.modules[parent_name] = parent_mod
        parent_spec.loader.exec_module(parent_mod)
    full = f"{parent_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _SDK_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_events = _load_sdk_module("_events")
_sse = _load_sdk_module("_sse")
_tool_handler = _load_sdk_module("_tool_handler")
_responses = _load_sdk_module("_responses")

ElicitationRequest = _events.ElicitationRequest
ToolCall = _events.ToolCall
MCP_ELICITATION_METHOD = _events.MCP_ELICITATION_METHOD
ElicitationRequestCtx = _tool_handler.ElicitationRequestCtx
StreamHooks = _tool_handler.StreamHooks


class _FakeResponse:
    """Minimal httpx.Response stand-in for POST result checking."""

    def __init__(self, status_code: int = 202) -> None:
        """Initialize with the simulated status code.

        :param status_code: Status the fake response reports.
            Default 202 mirrors the session event endpoint's
            success contract.
        """
        self.status_code = status_code
        self.text = ""


class _FakeHttpClient:
    """
    Minimal async HTTP stub — records POSTs without opening
    a socket.

    Only ``post`` is exercised by the elicitation flow; PATCH
    is not used here, and any other method would raise
    ``AttributeError`` on access — fail-loud rather than
    silently hitting the real server.
    """

    def __init__(self) -> None:
        """Initialize with no recorded calls."""
        self.post_calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        """Record the POST and return a fake 202.

        :param url: The full URL the SDK posted to.
        :param json: The JSON body (kwarg-only on httpx; keyword
            here too so the test signature mirrors httpx exactly).
        :param timeout: The per-call timeout the SDK sets.
        :returns: A 202 :class:`_FakeResponse`.
        """
        self.post_calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(status_code=202)


# ── SSE parse: response.elicitation_request ────────────────


def test_sse_parses_elicitation_request_event() -> None:
    """
    A ``response.elicitation_request`` event with an MCP-shape
    ``params`` block parses to :class:`ElicitationRequest` with
    every field surfaced as a flat dataclass attribute. This is
    what the streaming path keys off of to dispatch the hook.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_abc",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "approve web search?",
            "requestedSchema": {},
            "phase": "tool_call",
            "policy_name": "ask_search",
            "content_preview": "q=classified",
        },
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert isinstance(event, ElicitationRequest)
    assert event.elicitation_id == "elicit_abc"
    assert event.message == "approve web search?"
    assert event.requested_schema == {}
    assert event.mode == "form"
    assert event.phase == "tool_call"
    assert event.policy_name == "ask_search"
    assert event.content_preview == "q=classified"
    assert event.target_session_id is None


def test_sse_elicitation_request_tolerates_missing_extras() -> None:
    """
    The MCP-spec required fields are ``mode`` / ``message`` /
    ``requestedSchema``; producer extras (``phase``,
    ``policy_name``, ``content_preview``) may be absent. The
    parser must coerce missing extras to empty string rather
    than raising — defensive shape keeps a stray protocol skew
    from crashing the whole stream.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_bare",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "are you sure?",
            "requestedSchema": {},
        },
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert isinstance(event, ElicitationRequest)
    assert event.message == "are you sure?"
    # Extras default to empty string when absent.
    assert event.phase == ""
    assert event.policy_name == ""
    assert event.content_preview == ""


def test_sse_elicitation_request_skips_when_id_missing() -> None:
    """
    Without an ``elicitation_id`` there's no correlation key
    for the reply POST — the parser returns None and logs
    rather than fabricating an id. Forward-compat: an upstream
    that emits the event with a different correlation surface
    won't crash the stream; it just gets dropped.
    """
    raw = {
        "type": "response.elicitation_request",
        # No elicitation_id key.
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "?", "requestedSchema": {}},
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert event is None


def test_sse_elicitation_request_skips_when_params_not_dict() -> None:
    """
    ``params`` must be a dict per MCP spec. Anything else is
    malformed; parser drops it with a log line rather than
    accessing fields on a non-dict and crashing.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_x",
        "method": "elicitation/create",
        "params": "this is not a dict",
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert event is None


def test_sse_parses_regular_function_call_as_toolcall() -> None:
    """
    A ``function_call`` with ANY name still parses to
    :class:`ToolCall`. Regression guard for the deleted
    reserved-name carve-out: pre-refactor, a function_call
    named ``request_approval`` was diverted to a separate
    event; post-refactor that's gone. If a future regression
    re-introduces a carve-out, this test catches it because
    the ``request_approval`` name returns a regular ToolCall.
    """
    raw = {
        "item": {
            "type": "function_call",
            "call_id": "call_real",
            "name": "request_approval",
            "status": "action_required",
            "arguments": json.dumps({"path": "x.py"}),
        },
    }
    event = _sse._parse_output_item(raw)
    assert isinstance(event, ToolCall)
    # The name is now just data — no special semantics.
    assert event.name == "request_approval"


# ── _handle_elicitation_request: hook + POST wiring ────────


@pytest.mark.asyncio
async def test_target_session_id_routes_elicitation_verdict_to_child_session() -> None:
    """
    Mirrored child prompts carry ``params.target_session_id``. The
    legacy responses stream must preserve that field into the hook
    context and POST the verdict to the child session's resolve URL,
    not the ancestor stream's own session id.
    """
    http = _FakeHttpClient()
    seen: list[ElicitationRequestCtx] = []

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        seen.append(ctx)
        return True

    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_child",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "approve child tool?",
            "requestedSchema": {},
            "phase": "tool_call",
            "policy_name": "child_gate",
            "content_preview": "child work",
            "target_session_id": "conv_child",
        },
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert isinstance(event, ElicitationRequest)
    assert event.target_session_id == "conv_child"

    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        StreamHooks(on_elicitation_request=_hook),
        event,
        response_id="resp_parent",
        session_id="conv_parent",
    )

    assert len(seen) == 1
    assert seen[0].target_session_id == "conv_child"
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_child/elicitations/elicit_child/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "accept"}


@pytest.mark.asyncio
async def test_hook_accept_posts_accept_action() -> None:
    """
    A hook that returns ``True`` → SDK POSTs ``{"action": "accept"}``
    to the elicitation's dedicated resolve URL
    ``/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve``
    (URL-based elicitation).
    """
    http = _FakeHttpClient()
    seen: list[ElicitationRequestCtx] = []

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        seen.append(ctx)
        return True

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_accept",
        message="tainted",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="deny_tainted",
        content_preview="args",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_1",
        session_id="conv_1",
    )

    # The hook fired exactly once with the right context.
    # 1 = guards against the future being awaited twice or
    # the dispatcher invoking the hook in a loop.
    assert len(seen) == 1, (
        f"Expected hook to fire exactly once; got {len(seen)} "
        f"calls. >1 would mean the dispatcher is double-firing."
    )
    assert seen[0].elicitation_id == "elicit_accept"
    assert seen[0].policy_name == "deny_tainted"

    # Exactly one POST.
    assert len(http.post_calls) == 1
    call = http.post_calls[0]
    assert call["url"] == (
        "http://localhost:8000/v1/sessions/conv_1/elicitations/elicit_accept/resolve"
    )
    # Verdict body is the bare MCP ElicitResult — the elicitation id
    # rides in the URL path, not the body.
    assert call["json"] == {"action": "accept"}


@pytest.mark.asyncio
async def test_hook_decline_posts_decline_action() -> None:
    """
    Hook returns ``False`` → SDK POSTs ``{"action": "decline"}`` to
    the elicitation's resolve URL. Server treats this identically to
    timeout / cancel — the parked workflow wakes and the enforcement
    site short-circuits with a DENY sentinel.
    """
    http = _FakeHttpClient()

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        return False

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_decline",
        message="",
        requested_schema={},
        mode="form",
        phase="response",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_2",
        session_id="conv_2",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_2/elicitations/elicit_decline/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_no_hook_fails_closed() -> None:
    """
    No hook registered → SDK resolves the elicitation with
    ``{"action": "decline"}``. POLICIES.md §7.2: an unhandled
    elicitation must DENY. Silently swallowing it would stall the
    parked workflow until ``ask_timeout`` expired — declining
    fail-closed is the right default.
    """
    http = _FakeHttpClient()
    hooks = StreamHooks()  # no on_elicitation_request
    event = ElicitationRequest(
        elicitation_id="elicit_nohook",
        message="",
        requested_schema={},
        mode="form",
        phase="request",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_3",
        session_id="conv_3",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_3/elicitations/elicit_nohook/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_hook_exception_fails_closed() -> None:
    """
    Hook raises → SDK catches, logs, resolves the elicitation with
    ``{"action": "decline"}``. A buggy elicitation handler must not
    crash the stream or stall the workflow; fail-closed keeps the
    invariant.
    """
    http = _FakeHttpClient()

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        raise RuntimeError("bug in handler")

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_bug",
        message="",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_4",
        session_id="conv_4",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_4/elicitations/elicit_bug/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_hook_accepts_sync_callable() -> None:
    """
    Hooks can be sync or async. A sync ``def hook(ctx) -> bool``
    must work too — the client awaits only when the return is
    awaitable.
    """
    http = _FakeHttpClient()

    def _hook(ctx: ElicitationRequestCtx) -> bool:
        return True

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_sync",
        message="",
        requested_schema={},
        mode="form",
        phase="response",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_5",
        session_id="conv_5",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_5/elicitations/elicit_sync/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "accept"}


# ── REPL elicitation prompt wiring ────────────────────────
#
# The REPL's flow doesn't call ``input()`` — that path fought
# prompt_toolkit's ``patch_stdout`` (characters vanishing
# mid-type, auto-delete jank). Instead, the hook creates an
# :class:`asyncio.Future` that the main input loop resolves
# when the user types ``y`` / ``n`` at the pinned prompt.
# These tests drive the future directly; the main-loop
# wiring in :func:`run_repl` is exercised via the e2e harness.


class _FakeHost:
    """TerminalHost stub — records everything the hook prints."""

    def __init__(self) -> None:
        """Initialize with an empty output log."""
        self.outputs: list[Any] = []

    def output(self, item: Any) -> None:
        """Record the item.

        :param item: Whatever the hook handed to
            ``host.output(...)`` — usually a Rich
            :class:`Text` instance, sometimes a plain string.
        """
        self.outputs.append(item)


class _FakeFmt:
    """Formatter stub — the hook reads style names off it."""

    warning = "yellow"
    muted = "dim"
    accent = "cyan"


def _make_ctx(
    *,
    elicitation_id: str = "c1",
    message: str = "needs review",
    policy_name: str = "gatekeeper",
    phase: str = "tool_call",
    content_preview: str = '{"tool": "search"}',
    response_id: str = "r1",
) -> ElicitationRequestCtx:
    """Build an :class:`ElicitationRequestCtx` for hook tests.

    Producer extras (phase / policy_name / content_preview)
    are required dataclass fields, so the tests must supply
    every one — empty strings stand in for "not applicable"
    where the test doesn't care.

    :param elicitation_id: Unique id; tests use synthetic
        ``c1``/``c2`` rather than real ``elicit_...`` prefixes
        to keep the values readable in failure messages.
    :param message: Human-readable prompt the renderer should
        display.
    :param policy_name: Producer extra — deciding ASK policy.
    :param phase: Producer extra — phase that produced the ASK.
    :param content_preview: Producer extra — truncated content
        snapshot.
    :param response_id: Audit-only response id; the hook never
        posts on behalf of this id (the elicitation_id is the
        correlation key).
    :returns: A fully-populated context.
    """
    return ElicitationRequestCtx(
        elicitation_id=elicitation_id,
        message=message,
        requested_schema={},
        mode="form",
        phase=phase,
        policy_name=policy_name,
        content_preview=content_preview,
        response_id=response_id,
    )


# ── Three-way verdict parser ─────────────────────────────
#
# The parser is the precise seam between user keystrokes and
# the elicitation state. It must: (a) accept the short forms
# users reach for (``y``, ``a``, ``n``), (b) disambiguate
# ``a`` as ALWAYS (not as a random non-``y`` character that
# falls through to refuse), and (c) fail-closed on anything
# outside the vocabulary.


# ── Session auto-approve cache ────────────────────────────
