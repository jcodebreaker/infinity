"""
Unit tests for ``sys_session_close`` and its session-tool siblings.

These cover the tools added in 13a alongside the existing
``sys_session_send`` / ``sys_session_list`` family. The tests build a
real :class:`SqlAlchemyConversationStore` over a temp SQLite DB and
monkeypatch the ``omnigent.runtime`` accessors so the tools see them.

The tasks table has been removed. ``_resolve_parent_conversation_id``
now reads ``ctx.conversation_id`` directly instead of looking up the
task row, so the fixture only needs a parent conversation and a child.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime.prompt import SUBAGENT_WAKE_NOTICE_SHAPE
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.spawn import (
    _CLOSED_TITLE_INFIX,
    SysSessionCloseTool,
    SysSessionListTool,
    SysSessionSendTool,
)
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE


@dataclass
class _Fixture:
    """
    Bundle of stores + ids + ctx the test cases reuse.

    Built per-test by :func:`session_fixture` so each test gets an
    independent SQLite database, a parent conversation, and a child
    conversation titled ``"researcher:auth"`` parented by the parent
    conversation.

    ``_resolve_parent_conversation_id`` reads ``ctx.conversation_id``
    directly (tasks table removed), so the fixture sets that field
    instead of creating a task row.

    :param conv_store: Conversation store under test.
    :param parent_conv_id: Parent conversation id.
    :param child_conv_id: Child conversation id (titled
        ``"researcher:auth"`` under the parent).
    :param ctx: Pre-built :class:`ToolContext` carrying
        ``conversation_id=parent_conv_id``.
    """

    conv_store: SqlAlchemyConversationStore
    parent_conv_id: str
    child_conv_id: str
    ctx: ToolContext


@pytest.fixture()
def session_fixture(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Fixture]:
    """
    Build the per-test database state and patch the runtime accessors.

    Creates a parent conversation and a child conversation titled
    ``"researcher:auth"`` parented by the parent. Adds two items to the
    child (one user message, one assistant message). Monkeypatches
    ``omnigent.runtime.get_conversation_store`` so the tool's
    late-bound lookups resolve to the test store.

    ``_resolve_parent_conversation_id`` reads ``ctx.conversation_id``
    directly (tasks table removed), so the fixture sets that field on
    the :class:`ToolContext` instead of creating a task row.

    :param db_uri: Per-test SQLite URI from ``tests/conftest.py``.
    :param monkeypatch: pytest fixture for late-bound runtime patches.
    :yields: A :class:`_Fixture` the test reads from.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)

    parent_conv = conv_store.create_conversation(kind="default")
    child_conv = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_conv.id,
    )
    conv_store.append(
        child_conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_test_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "find the auth bug"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_test_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "looking at handlers.py"}],
                    agent="researcher",
                ),
            ),
        ],
    )

    # The tools' invoke() does ``from omnigent.runtime import
    # get_conversation_store`` per call (lazy bind), so patching the
    # runtime module's attribute is sufficient.
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conv_store)

    # conversation_id is the canonical parent session id; _resolve_parent_conversation_id
    # reads ctx.conversation_id directly (tasks table removed).
    ctx = ToolContext(
        task_id="task_placeholder",
        agent_id="2759f8a97fd91216cb3e0d7a1f60c7f6",
        conversation_id=parent_conv.id,
    )

    yield _Fixture(
        conv_store=conv_store,
        parent_conv_id=parent_conv.id,
        child_conv_id=child_conv.id,
        ctx=ctx,
    )


# ── Schema tests ──────────────────────────────────────────


def test_send_schema_advertises_plain_string_and_purpose_object_args() -> None:
    """
    ``sys_session_send`` accepts either the stable string contract or an object.

    This catches the Nessie regression where its policy required
    ``args.purpose`` but the tool schema only advertised ``args`` as a string,
    causing models to emit policy-denied calls for legitimate review helpers.
    Keeping the string branch verifies the shared tool remains compatible with
    non-Nessie agents.
    """
    tool = SysSessionSendTool(
        {"claude": AgentSpec(spec_version=1, name="claude", description="Review helper.")}
    )

    params = tool.get_schema()["function"]["parameters"]
    args_schema = params["properties"]["args"]

    # Only ``args`` is universally required now: the unified tool accepts
    # either (agent + title) named mode OR session_id mode, so agent/title
    # moved out of ``required`` (the handler enforces the one-of split).
    # session_id is advertised as the by-id-mode addressing field.
    assert params["required"] == ["args"]
    assert "session_id" in params["properties"]
    assert args_schema["anyOf"][0] == {"type": "string"}
    object_schema = args_schema["anyOf"][1]
    assert object_schema["type"] == "object"
    # ``model`` is optional dispatch metadata — only ``input`` may be
    # required, or plain-string sends would break.
    assert object_schema["required"] == ["input"]
    assert object_schema["additionalProperties"] is False
    assert set(object_schema["properties"]) == {
        "input",
        "purpose",
        "model",
        "reasoning_effort",
        "file_ids",
        "cost_budget",
    }
    assert "dispatch metadata" in object_schema["properties"]["purpose"]["description"]
    # The model property must say it is create-time-only and optional,
    # so the LLM doesn't attach it to continuation sends.
    model_desc = object_schema["properties"]["model"]["description"]
    assert object_schema["properties"]["model"]["type"] == "string"
    assert "CREATES" in model_desc
    assert "harness default" in model_desc


def _object_branch_props(tool: SysSessionSendTool) -> set[str]:
    """Return the property names of the object branch of ``args``."""
    params = tool.get_schema()["function"]["parameters"]
    object_schema = next(
        b for b in params["properties"]["args"]["anyOf"] if b.get("type") == "object"
    )
    return set(object_schema["properties"])


def test_send_schema_gates_harness_field_behind_allowlist_opt_in() -> None:
    """
    ``args.harness`` is advertised ONLY when a sub-agent opts in.

    Per design D.4 the runtime harness override is allowlist-gated: the
    schema exposes ``harness`` only when at least one declared sub-agent
    declares a non-empty ``executor.config.allowed_harnesses``. A sub-agent
    without that opt-in keeps the base ``{input, purpose, model, file_ids}``
    args object, so the orchestrator never sees a harness knob it cannot use.
    This mirrors the per-child dispatch guard in tool_dispatch.py — the two
    gates must agree on what "opted in" means.
    """
    # Not opted in: a plain sub-agent (no allowed_harnesses) → base schema.
    plain = SysSessionSendTool(
        {"claude": AgentSpec(spec_version=1, name="claude", description="Review helper.")}
    )
    assert _object_branch_props(plain) == {
        "input",
        "purpose",
        "model",
        "reasoning_effort",
        "file_ids",
        "cost_budget",
    }

    # Opted in: a sub-agent whose executor.config.allowed_harnesses declares a
    # non-empty allowlist (the polly/debby `codex` worker shape) →
    # the schema adds the gated `harness` field.
    opted_in_spec = AgentSpec(
        spec_version=1,
        name="codex",
        description="Codex coding sub-agent.",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "codex-native",
                "allowed_harnesses": ["codex-native", "claude-native"],
            },
        ),
    )
    opted_in = SysSessionSendTool({"codex": opted_in_spec})
    assert _object_branch_props(opted_in) == {
        "input",
        "purpose",
        "model",
        "reasoning_effort",
        "file_ids",
        "harness",
        "cost_budget",
    }
    object_schema = next(
        b
        for b in opted_in.get_schema()["function"]["parameters"]["properties"]["args"]["anyOf"]
        if b.get("type") == "object"
    )
    assert "allowed_harnesses" in object_schema["properties"]["harness"]["description"]
    # additionalProperties stays closed even with the extra gated field, so a
    # spurious arg is still rejected by validation.
    assert object_schema["additionalProperties"] is False

    # Mixed: one opted-in sub-agent among several opts the whole tool's schema
    # in — the dispatch guard still rejects harness for the non-opted children.
    mixed = SysSessionSendTool(
        {
            "claude": AgentSpec(spec_version=1, name="claude", description="Review helper."),
            "codex": opted_in_spec,
        }
    )
    assert _object_branch_props(mixed) == {
        "input",
        "purpose",
        "model",
        "reasoning_effort",
        "file_ids",
        "harness",
        "cost_budget",
    }


def test_send_description_announces_wake_notice() -> None:
    """
    The dispatch tool tells the model the exact wake notice the runtime
    posts when a child finishes, so the notice is recognised as
    runtime-originated instead of read as a user-typed instruction.
    """
    tool = SysSessionSendTool(
        {"claude": AgentSpec(spec_version=1, name="claude", description="Review helper.")}
    )
    desc = tool.get_schema()["function"]["description"]
    assert SUBAGENT_WAKE_NOTICE_SHAPE in desc
    assert "not from a person" in desc


def test_close_schema_required_fields_and_no_extra_props() -> None:
    """
    The ``sys_session_close`` schema requires ``conversation_id``
    only — no ``tail_items``, no extras.

    Extending the schema later would be additive, but a regression
    that introduced an unintended property would expose the LLM
    to ambiguity and is worth catching.
    """
    schema = SysSessionCloseTool().get_schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["conversation_id"]
    assert params["additionalProperties"] is False
    assert set(params["properties"].keys()) == {"conversation_id"}


def test_close_top_level_conversation_in_tree_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Close refuses a top-level conversation_id even when it's in
    the caller's spawn tree.

    The LLM cannot tombstone a
    non-sub-agent conversation, so the tool returns
    ``session_not_a_sub_agent`` and leaves the row's title intact.

    :param session_fixture: Per-test fixture providing the tool ctx
        and parent conversation id.
    """
    tool = SysSessionCloseTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.parent_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_a_sub_agent"
    assert payload["conversation_id"] == session_fixture.parent_conv_id

    parent_after = session_fixture.conv_store.get_conversation(
        session_fixture.parent_conv_id,
    )
    assert parent_after is not None
    assert _CLOSED_TITLE_INFIX not in (parent_after.title or "")


# ── Close invoke tests ────────────────────────────────────


def test_close_marks_closed_and_tombstones_internal_title(session_fixture: _Fixture) -> None:
    """
    Close marks the child closed and internally tombstones its title.

    The explicit ``omnigent.closed=true`` label is the behavioral
    marker write paths consume. The conv_id title suffix still
    guarantees uniqueness against the partial unique index on
    ``(parent_conversation_id, title)`` even if the same logical
    session is closed multiple times across the parent's lifetime.
    A regression that omitted the suffix would let two closes collide
    on the index.
    """
    tool = SysSessionCloseTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload == {
        "closed": True,
        "conversation_id": session_fixture.child_conv_id,
        "agent": "researcher",
        "title": "auth",
    }

    # Inspect the underlying row directly via list_conversations —
    # exercises the same lookup path the next spawn would use.
    children = session_fixture.conv_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=session_fixture.parent_conv_id,
        limit=100,
    )
    titles = [c.title for c in children.data]
    expected = f"researcher:auth{_CLOSED_TITLE_INFIX}{session_fixture.child_conv_id}"
    assert expected in titles, (
        f"expected title {expected!r} in {titles!r} — close did not "
        "rewrite the child's title with the conv_id suffix."
    )
    # And the bare title is gone — proves a follow-up spawn would
    # find no match.
    assert "researcher:auth" not in titles
    refreshed = session_fixture.conv_store.get_conversation(session_fixture.child_conv_id)
    assert refreshed is not None
    assert refreshed.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE


def test_close_succeeds_regardless_of_session_state(session_fixture: _Fixture) -> None:
    """
    Close tombstones the child conversation regardless of any live
    session state.

    The tasks table has been removed and the task-based busy check
    (``_busy_check_or_none``) now always returns ``None``. Close
    proceeds unconditionally; live-session awareness is the
    responsibility of the caller.

    :param session_fixture: Pre-built fixture with a child conversation.
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload.get("closed") is True
    assert payload["conversation_id"] == session_fixture.child_conv_id

    # The title is tombstoned and the closed label is set — proves
    # close ran to completion.
    children = session_fixture.conv_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=session_fixture.parent_conv_id,
        limit=100,
    )
    titles = [c.title for c in children.data]
    assert "researcher:auth" not in titles
    closed_child = session_fixture.conv_store.get_conversation(session_fixture.child_conv_id)
    assert closed_child is not None
    assert closed_child.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE


def test_session_list_skips_label_closed_child_with_original_title(
    session_fixture: _Fixture,
) -> None:
    """
    ``sys_session_list`` treats the closed label as authoritative.

    This covers the behavioral marker independently from the legacy
    title tombstone. If a row has the original human title but is
    labelled closed, it must not reappear as a resumable sub-agent;
    otherwise ``sys_session_send`` by name could keep talking to a
    session the parent explicitly closed.
    """
    session_fixture.conv_store.set_labels(
        session_fixture.child_conv_id,
        {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
    )

    raw = SysSessionListTool().invoke("{}", session_fixture.ctx)
    payload = json.loads(raw)
    assert payload["sub_agents"] == []


def test_session_list_schema_exposes_bounded_pagination() -> None:
    """The harness can discover the same pagination inputs the runner accepts."""

    properties = SysSessionListTool().get_schema()["function"]["parameters"]["properties"]

    assert "default" not in properties["limit"]
    assert properties["limit"]["maximum"] == 100
    assert properties["cursor"]["type"] == "string"


def test_close_unknown_conversation_id_returns_not_found(
    session_fixture: _Fixture,
) -> None:
    """
    Close with an unknown ``conversation_id`` returns
    ``session_not_found`` (no DB mutation).
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": "761ec01d1fb0a45d2e3159d0d47a5e70"}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_found"
    assert payload["conversation_id"] == "761ec01d1fb0a45d2e3159d0d47a5e70"


def test_close_out_of_tree_conversation_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Close refuses a conversation_id from a different spawn tree
    (``session_out_of_tree``) and leaves the row's title intact.

    Without the tree-scope check, any agent could tombstone any
    other agent's session by guessing conversation_ids. The
    regression we care about is silent acceptance — the row
    should remain unchanged on the way out.
    """
    other_parent = session_fixture.conv_store.create_conversation(kind="default")
    other_child = session_fixture.conv_store.create_conversation(
        kind="sub_agent",
        title="other:secret",
        parent_conversation_id=other_parent.id,
    )

    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": other_child.id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_out_of_tree"
    assert payload["conversation_id"] == other_child.id

    # Verify the out-of-tree row's title was not rewritten.
    refreshed = session_fixture.conv_store.get_conversation(other_child.id)
    assert refreshed is not None
    assert refreshed.title == "other:secret"


# ── Argument-parsing edge cases ───────────────────────────


def test_close_missing_required_field_returns_error(session_fixture: _Fixture) -> None:
    """
    Missing ``conversation_id`` argument returns a structured
    error naming the missing field.

    The error message must name the field so the LLM can correct
    and retry. A regression that returned a generic message would
    force the LLM to guess.
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "conversation_id" in payload["error"]


# Tree-scoping resolution via the parent task is exercised by the close
# tests above. No standalone resolver test — the mechanism has no
# observable behavior beyond those outcomes.
