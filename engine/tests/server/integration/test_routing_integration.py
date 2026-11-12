"""L5 integration coverage for intelligent routing with a fake router.

Drives the server's own routing paths against a stub routing client:
the ``sys_session_send`` child override precedence, the native-subagent
relay endpoint (transcript item + child-sessions join), the subagent
fail-mode knob, and the decision cache.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.runner.subagent_routing import (
    AUTO_HARNESS_LABEL_KEY,
    ROUTING_DECISION_LABEL_KEY,
)
from omnigent.server.routes._sessions import orchestration as orchestration_module
from omnigent.server.routes._sessions.common import get_server_host_registry
from omnigent.server.schemas import SessionEventInput
from omnigent.server.smart_routing import RoutingResult
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import (
    FakeCaps,
    FakeRoutingClient,
    create_test_agent,
    echo_runner_client,
)

ROUTED_MODEL = "databricks-claude-opus-4-8"
LLM_PICKED_MODEL = "databricks-claude-sonnet-4-6"
GPT_MODEL = "databricks-gpt-5-5"
GLM_MODEL = "databricks-glm-5-2"
# The spelling the gateway serves GLM under; see ``_SERVABLE_ALIASES``.
GLM_SERVABLE = "system.ai.glm-5-2"
# Uuid-shaped so the host registry's canonical keying accepts it.
_GATEWAY_HOST_ID = "bb22cc33dd44ee55ff66778899001122"

pytestmark = pytest.mark.asyncio


async def _parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    child_model_override: str | None = None,
) -> tuple[dict[str, Any], Any, SqlAlchemyConversationStore]:
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    parent = resp.json()
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="reviewer:auth",
        parent_conversation_id=parent["id"],
        agent_id=agent["id"],
    )
    if child_model_override is not None:
        child = conv_store.update_conversation(child.id, model_override=child_model_override)
    return parent, child, conv_store


def _host_reporting_gateway(gateway: dict[str, bool]) -> Any:  # type: ignore[explicit-any]
    """A host row whose gateway-backing map is already reported to the server.

    Gateway backing never reaches the database: the host delivers it on its
    connect handshake and ``create_app`` publishes the receiving registry, so a
    test stands a host up by recording into that registry.

    :param gateway: Harness spelling → gateway-backed flag to report.
    :returns: A host-row stand-in carrying the id the map was recorded under.
    """
    registry = get_server_host_registry()
    assert registry is not None, "create_app publishes the host registry"
    registry.record_gateway_inference(_GATEWAY_HOST_ID, gateway)
    return SimpleNamespace(host_id=_GATEWAY_HOST_ID)


def _routing_decisions(conv_store: SqlAlchemyConversationStore, session_id: str) -> list[Any]:
    return [
        item
        for item in conv_store.list_items(session_id).data
        if getattr(item, "type", None) == "routing_decision"
    ]


# ── 1. Child spawn: the router wins over ``args.model`` ─────────────


async def test_router_overrides_llm_supplied_child_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-precedence",
        child_model_override=LLM_PICKED_MODEL,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    # The router's pick replaced the orchestrator's ``args.model``.
    assert refreshed.model_override == ROUTED_MODEL
    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    data = decisions[0].data
    assert data.model == ROUTED_MODEL
    assert data.scope == "child_session"
    assert data.attempted_override == LLM_PICKED_MODEL
    assert data.decision_id
    # The decision is joined onto the child row for the sidebar.
    assert refreshed.labels.get(ROUTING_DECISION_LABEL_KEY) == data.decision_id


async def test_another_spelling_of_the_routed_model_is_no_override(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``sys_session_send`` and the router can spell one arm two ways.

    The child path used to compare raw strings, so a GLM ask that routing
    landed exactly was still reported as an overridden attempt.
    """
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-spelling",
        child_model_override=GLM_SERVABLE,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=GLM_MODEL, rationale="delegate down", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "fix the typo"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == GLM_MODEL
    assert decisions[0].data.attempted_override is None


async def test_routed_model_publishes_session_model_event(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A routed pick pushes ``session.model`` so open web pickers update live.

    Routing persists ``model_override`` server-side; without the SSE the
    dropdown keeps showing the launch model until a reload (the PATCH and
    ``external_model_change`` paths publish it — routing must too).
    """
    _parent, child, conv_store = await _parent_and_child(
        client,
        db_uri,
        agent_name="routing-sse",
        child_model_override=LLM_PICKED_MODEL,
    )
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    published: list[tuple[str, dict[str, Any]]] = []
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch.object(
            orchestration_module.session_stream,
            "publish",
            side_effect=lambda sid, payload: published.append((sid, payload)),
        ),
    ):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    model_events = [
        payload
        for sid, payload in published
        if sid == child.id and payload.get("type") == "session.model"
    ]
    assert len(model_events) == 1
    assert model_events[0]["model"] == ROUTED_MODEL


async def test_the_router_source_reaches_the_transcript_item_and_the_sse(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The chip needs to know which router answered, live and on reload."""
    from omnigent.server.routing_backend import RoutingBackends
    from omnigent.server.smart_routing import ExternalRoutingClient

    _parent, child, conv_store = await _parent_and_child(
        client, db_uri, agent_name="routing-source"
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
    )
    # Classified as the external client by type, so a gateway-backed route is
    # stamped "databricks-aigw" rather than the judge's source.
    external = ExternalRoutingClient(base_url="https://ws.example.invalid", router_name="task_v1")
    external.route = routing_client.route  # type: ignore[method-assign]
    caps = FakeCaps(routing_client=external, routing_backends=RoutingBackends(external=external))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    published: list[tuple[str, dict[str, Any]]] = []
    with (
        patch("omnigent.runtime._globals._caps", new=caps),
        patch.object(
            orchestration_module.session_stream,
            "publish",
            side_effect=lambda sid, payload: published.append((sid, payload)),
        ),
    ):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.router_source == "databricks-aigw"
    chips = [
        payload
        for sid, payload in published
        if sid == child.id and payload.get("item", {}).get("type") == "routing_decision"
    ]
    assert chips and chips[0]["item"]["router_source"] == "databricks-aigw"


# ── 2. Native-subagent relay: transcript item + child join ──────────


# ── 3. Dead router ─────────────────────────────────────────────────


# ── 5. Per-session subagent-routing gate ───────────────────────────


SPAWN_PAYLOAD = {
    "harness": "claude-native",
    "task_name": "code-reviewer",
    "prompt": "review the auth module",
    "parent_model": LLM_PICKED_MODEL,
}
DISABLED_RATIONALE = "subagent routing disabled for this session"


async def _session_with_routing_flags(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
    cost_control: str | None = None,
    subagent_routing: str | None = None,
) -> str:
    agent = await create_test_agent(client, name=agent_name)
    body: dict[str, Any] = {"agent_id": agent["id"]}
    if cost_control is not None:
        body["cost_control_mode_override"] = cost_control
    if subagent_routing is not None:
        body["subagent_routing_override"] = subagent_routing
    resp = await client.post("/v1/sessions", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_patch_round_trips_the_subagent_setting_and_rejects_junk(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    session_id = await _session_with_routing_flags(client, agent_name="routing-gate-patch")

    for value in ("on", "off"):
        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"subagent_routing_override": value},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["subagent_routing_override"] == value
        snapshot = await client.get(f"/v1/sessions/{session_id}")
        assert snapshot.json()["subagent_routing_override"] == value

    # Explicit null clears the stored value; the session then reads as Default.
    cleared = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"subagent_routing_override": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["subagent_routing_override"] is None

    rejected = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"subagent_routing_override": "maybe"},
    )
    assert rejected.status_code == 400, rejected.text
    assert "subagent_routing_override" in rejected.text


@pytest.mark.parametrize(
    ("case", "start", "flip_to"),
    [
        # Default → Smart Routing: the very next spawn routes.
        ("off-to-on", None, "on"),
        # Smart Routing → Default: the next spawn stops routing, and the stored
        # value wins whether it was stamped at create or flipped by hand. This
        # is the direction a child of a routed parent needs — its inherited
        # stamp is not special, only stored.
        ("on-to-off", "on", "off"),
    ],
)
async def test_flipping_the_setting_mid_session_takes_effect_on_the_next_spawn(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    start: str | None,
    flip_to: str,
) -> None:
    session_id = await _session_with_routing_flags(
        client,
        agent_name=f"routing-gate-midsession-{case}",
        subagent_routing=start,
    )
    routing_client = FakeRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="deep reasoning", harness="claude_code")
    )
    routes_before = 1 if start == "on" else 0
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        before = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
        assert before.json()["action"] == ("rewrite" if start == "on" else "allow")
        assert len(routing_client.calls) == routes_before
        patched = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"subagent_routing_override": flip_to},
        )
        assert patched.status_code == 200, patched.text
        after = await client.post(
            f"/v1/sessions/{session_id}/hooks/route-subagent",
            json=SPAWN_PAYLOAD,
        )
    if flip_to == "on":
        assert after.json()["action"] == "rewrite"
        assert after.json()["model"] == ROUTED_MODEL
        assert len(routing_client.calls) == routes_before + 1
    else:
        assert after.json()["action"] == "allow"
        assert after.json()["rationale"] == DISABLED_RATIONALE
        # The router is not asked again — declining costs no round trip.
        assert len(routing_client.calls) == routes_before


# ── 6. Harness-family constraint on the candidate set ──────────────


# ── 7. Omnigent child sessions stay in the parent's family ─────────


async def _pinned_parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    harness: str,
) -> tuple[str, Any, SqlAlchemyConversationStore]:
    """Create a routing-on parent pinned to *harness* plus one child session.

    The child goes through the real ``POST /v1/sessions`` create path so the
    forced-auto decision is exercised, not simulated.

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :param agent_name: Agent name to register.
    :param harness: The parent agent's harness, e.g. ``"claude-sdk"``.
    :returns: ``(parent_id, child_conversation, conversation_store)``.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    parent = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert parent.status_code == 201, parent.text
    parent_id = str(parent.json()["id"])
    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "title": "audit routing",
        },
    )
    assert child.status_code == 201, child.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    return parent_id, child_conv, conv_store


@pytest.mark.parametrize(
    ("harness", "picked", "expected_harnesses"),
    [
        ("claude-sdk", ROUTED_MODEL, {"claude-sdk"}),
    ],
)
async def test_child_of_a_pinned_parent_is_routed_in_the_parents_family(
    client: httpx.AsyncClient,
    db_uri: str,
    harness: str,
    picked: str,
    expected_harnesses: set[str],
) -> None:
    parent_id, child, conv_store = await _pinned_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-family-{harness}",
        harness=harness,
    )
    # The auto sentinel and its marker belong to Smart Routing sessions only —
    # a child of a pinned parent must carry neither.
    assert child.harness_override != "auto"
    assert AUTO_HARNESS_LABEL_KEY not in child.labels

    routing_client = FakeRoutingClient(RoutingResult(model=picked, rationale="in-family pick"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "audit routing"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    assert set(routing_client.offered[0]) == expected_harnesses
    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    assert refreshed.model_override == picked
    assert refreshed.harness_override != "auto"
    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.scope == "child_session"
    assert decisions[0].data.harness in expected_harnesses
    # The parent's mirrored copy names the same in-family harness.
    parent_decisions = _routing_decisions(conv_store, parent_id)
    assert [d.data.harness for d in parent_decisions] == [decisions[0].data.harness]


@pytest.mark.parametrize(
    ("case", "executor_config", "create_body", "picked", "prompt"),
    [
        # The client asked for the auto harness outright.
        (
            "client-sentinel",
            {"harness": "claude-sdk"},
            {"cost_control_mode_override": "on", "harness_override": "auto"},
            ROUTED_MODEL,
            "rewrite the router",
        ),
        # The spec opted itself in: a brain pinned to claude-sdk used to clamp
        # every child into the claude family. `smart_routing_harness: auto` starts the parent auto
        # with no harness_override in the request at all.
        (
            "spec-opt-in",
            {"harness": "claude-sdk", "smart_routing_harness": "auto"},
            {"cost_control_mode_override": "on"},
            GPT_MODEL,
            "compare the options",
        ),
    ],
)
async def test_child_of_an_auto_parent_keeps_cross_harness_candidates(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    executor_config: dict[str, Any],
    create_body: dict[str, Any],
    picked: str,
    prompt: str,
) -> None:
    """However the parent became auto, its children are routed against every family.

    ``allowed_family=None``: every family is on offer instead of the child being
    clamped into the brain's claude family.
    """
    agent = await create_test_agent(
        client,
        name=f"routing-child-family-{case}",
        executor={"type": "omnigent", "config": executor_config},
    )
    parent = await client.post("/v1/sessions", json={"agent_id": agent["id"], **create_body})
    assert parent.status_code == 201, parent.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(str(parent.json()["id"]))
    assert parent_conv is not None
    assert parent_conv.harness_override == "auto"
    assert parent_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"

    child = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent.json()["id"]},
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    # A Smart Routing parent still hands its children the auto treatment.
    assert child_conv.harness_override == "auto"
    assert child_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"

    routing_client = FakeRoutingClient(RoutingResult(model=picked, rationale="big task"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child_conv.id,
                child_conv,
                body,
                conv_store,
                runner_client,
            )

    assert set(routing_client.offered[0]) == {"claude-sdk", "pi"}


@pytest.mark.parametrize(
    ("case", "picked", "verdict_harness", "applied"),
    [
        # The reported verdict: the external router named codex + a gpt arm for
        # a child spawned onto pi. Nothing offered runs it, so the chip declines.
        ("cross-family-declines", GPT_MODEL, "codex", False),
        # An in-family pick is applied normally.
        ("in-family-applies", ROUTED_MODEL, "pi", True),
    ],
)
async def test_named_worker_child_of_an_auto_parent_stays_on_its_own_harness(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    picked: str,
    verdict_harness: str,
    applied: bool,
) -> None:
    """A spawn that named a worker keeps that worker's harness.

    The polly shape: a Smart Routing brain whose ``pi`` sub-agent declares its
    own harness. The spawn pins the CLI the child boots on, so the router is
    offered pi alone — it used to be handed every family, and a codex verdict
    was written to the row and stamped "applied" on a pane running pi.
    """
    agent = await create_test_agent(
        client,
        name=f"routing-child-named-worker-{case}",
        executor={
            "type": "omnigent",
            "config": {"harness": "claude-sdk", "smart_routing_harness": "auto"},
        },
        sub_agents=[{"name": "pi", "executor": {"type": "omnigent", "config": {"harness": "pi"}}}],
    )
    parent = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert parent.status_code == 201, parent.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(str(parent.json()["id"]))
    assert parent_conv is not None
    assert parent_conv.harness_override == "auto"

    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent.json()["id"],
            "sub_agent_name": "pi",
            "title": "pi:joke-pi",
        },
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    # The named worker is not handed the auto sentinel: its harness is decided.
    assert child_conv.harness_override != "auto"
    assert AUTO_HARNESS_LABEL_KEY not in child_conv.labels

    routing_client = FakeRoutingClient(
        RoutingResult(model=picked, rationale="cheap and fast", harness=verdict_harness)
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "tell me a joke"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child_conv.id,
                child_conv,
                body,
                conv_store,
                runner_client,
            )

    # Only the child's own harness was on offer.
    assert set(routing_client.offered[0]) == {"pi"}
    # And nothing cross-family lands: the row never moves off pi, and the chip
    # is an in-family pick or an honest decline — never another family stamped
    # "applied" over a pane that is still running pi.
    refreshed = conv_store.get_conversation(child_conv.id)
    assert refreshed is not None
    assert refreshed.harness_override in (None, "pi")
    decisions = _routing_decisions(conv_store, child_conv.id)
    assert len(decisions) == 1
    assert decisions[0].data.scope == "child_session"
    assert decisions[0].data.harness in (None, "pi")
    assert decisions[0].data.applied is applied
    if applied:
        assert refreshed.model_override == decisions[0].data.model
    else:
        assert decisions[0].data.model == "unavailable"
        assert refreshed.model_override is None


# ── 7b. The subagent-routing switch is the child-spawn gate ────────


async def _auto_parent_and_child(
    client: httpx.AsyncClient,
    db_uri: str,
    *,
    agent_name: str,
    cost_control: str | None,
    subagent_routing: str | None,
) -> tuple[str, Any, SqlAlchemyConversationStore]:
    """Create an auto-harness parent with explicit switches, plus one child.

    The child goes through the real ``POST /v1/sessions`` create path, so the
    forced-auto decision and the create stamp are exercised rather than faked.

    :param client: Test HTTP client.
    :param db_uri: Database URI for a direct store handle.
    :param agent_name: Agent name to register.
    :param cost_control: ``cost_control_mode_override`` for the parent.
    :param subagent_routing: ``subagent_routing_override`` for the parent.
    :returns: ``(parent_id, child_conversation, conversation_store)``.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": "claude-sdk"}},
    )
    body: dict[str, Any] = {"agent_id": agent["id"], "harness_override": "auto"}
    if cost_control is not None:
        body["cost_control_mode_override"] = cost_control
    if subagent_routing is not None:
        body["subagent_routing_override"] = subagent_routing
    parent = await client.post("/v1/sessions", json=body)
    assert parent.status_code == 201, parent.text
    parent_id = str(parent.json()["id"])
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent_conv = conv_store.get_conversation(parent_id)
    assert parent_conv is not None
    # The parent really is an auto-harness session either way, so the switch is
    # the only thing that differs between the cases below.
    assert parent_conv.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    assert parent_conv.subagent_routing_override == subagent_routing

    child = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "parent_session_id": parent_id, "title": "audit routing"},
    )
    assert child.status_code == 201, child.text
    child_conv = conv_store.get_conversation(str(child.json()["id"]))
    assert child_conv is not None
    return parent_id, child_conv, conv_store


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "routed"),
    [
        # The switch says off, so the parent's own routing must not leak into its
        # spawns — the child stays on the harness it was created with.
        ("cc-on-switch-off", "on", "off", False),
        # Preservation: the ordinary Smart Routing session, switch left stamped.
        ("cc-on-switch-on", "on", "on", True),
        # The switch alone drives spawns: the parent's own turns aren't routed,
        # but its children are.
        ("cc-off-switch-on", None, "on", True),
    ],
)
async def test_child_spawn_gate_follows_the_parents_subagent_switch(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    routed: bool,
) -> None:
    _parent_id, child, conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-switch-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    # The forced-auto decision at create.
    if routed:
        assert child.harness_override == "auto"
        assert child.labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    else:
        assert child.harness_override != "auto"
        assert AUTO_HARNESS_LABEL_KEY not in child.labels

    routing_client = FakeRoutingClient(RoutingResult(model=ROUTED_MODEL, rationale="big task"))
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "rewrite the router"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    if routed:
        assert len(routing_client.calls) == 1
        assert refreshed.model_override == ROUTED_MODEL
        assert len(_routing_decisions(conv_store, child.id)) == 1
    else:
        assert routing_client.calls == []
        assert refreshed.model_override is None
        assert _routing_decisions(conv_store, child.id) == []


@pytest.mark.parametrize(
    ("case", "cost_control", "subagent_routing", "stamped"),
    [
        ("cc-on-switch-off", "on", "off", None),
        ("cc-off-switch-on", None, "on", "on"),
    ],
)
async def test_child_create_stamp_inherits_the_parents_switch_not_its_cost_control(
    client: httpx.AsyncClient,
    db_uri: str,
    case: str,
    cost_control: str | None,
    subagent_routing: str | None,
    stamped: str | None,
) -> None:
    """The child carries its own copy of the parent's *switch*.

    The gate never re-reads the parent, so what the stamp copies decides whether
    a grandchild spawn routes. Copying the parent's cost-control mode instead
    would revive the switch the UI no longer exposes.
    """
    _parent_id, child, _conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-child-stamp-{case}",
        cost_control=cost_control,
        subagent_routing=subagent_routing,
    )
    assert child.subagent_routing_override == stamped


# ── 7c. A pinned parent cannot create an out-of-family child ───────


async def _pinned_parent(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
    harness: str,
    routing_on: bool = True,
) -> str:
    """Create a parent pinned to *harness*, with Smart Routing on or off.

    :param client: Test HTTP client.
    :param agent_name: Agent name to register.
    :param harness: The parent agent's harness, e.g. ``"claude-sdk"``.
    :param routing_on: Whether the parent routes its spawns.
    :returns: The parent session id.
    """
    agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    body: dict[str, Any] = {"agent_id": agent["id"]}
    if routing_on:
        body["cost_control_mode_override"] = "on"
    parent = await client.post("/v1/sessions", json=body)
    assert parent.status_code == 201, parent.text
    return str(parent.json()["id"])


async def _create_child(
    client: httpx.AsyncClient,
    parent_id: str,
    *,
    agent_name: str,
    harness: str,
) -> httpx.Response:
    """POST a child create bound to an agent running *harness*.

    :param client: Test HTTP client.
    :param parent_id: The parent session id.
    :param agent_name: Agent name to register for the child.
    :param harness: The child agent's harness, e.g. ``"claude-sdk"``.
    :returns: The raw create response.
    """
    child_agent = await create_test_agent(
        client,
        name=agent_name,
        executor={"type": "omnigent", "config": {"harness": harness}},
    )
    return await client.post(
        "/v1/sessions",
        json={"agent_id": child_agent["id"], "parent_session_id": parent_id},
    )


# ── 8. Truthful record on a claude-native turn ─────────────────────


async def _route_one_turn(conv: Any, conv_store: SqlAlchemyConversationStore) -> Any:
    caps = FakeCaps(
        routing_client=FakeRoutingClient(
            RoutingResult(model=ROUTED_MODEL, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=caps):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
            )
    decisions = _routing_decisions(conv_store, conv.id)
    assert len(decisions) == 1
    return decisions[0].data


async def _dispatch_native_turn(
    conv: Any,
    conv_store: SqlAlchemyConversationStore,
    *,
    model: str,
    routing_client: Any = None,  # type: ignore[explicit-any]
) -> tuple[Any, list[dict[str, Any]]]:
    """Send one web message into a native pane with the router returning *model*.

    :param conv: Conversation row to dispatch for (labels must be current).
    :param conv_store: Store the row lives in.
    :param model: Model the fake router picks for this turn.
    :param routing_client: Routing-client double to use, so a caller can read
        back what was offered. ``None`` builds one returning *model*.
    :returns: The last routing-decision item's data and the forwarded bodies.
    """
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    caps = FakeCaps(
        routing_client=routing_client
        if routing_client is not None
        else FakeRoutingClient(
            RoutingResult(model=model, rationale="deep refactor", harness="claude_code")
        )
    )
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    async with httpx.AsyncClient(
        base_url="http://runner.test", transport=httpx.MockTransport(_handler)
    ) as runner_client:
        with (
            patch("omnigent.runtime._globals._caps", new=caps),
            patch(
                "omnigent.server.routes.sessions._get_runner_client",
                new=AsyncMock(return_value=runner_client),
            ),
        ):
            await orchestration_module._dispatch_session_event_to_runner_impl(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
                agent_name="reviewer",
                file_store=None,
                artifact_store=None,
            )
    return _routing_decisions(conv_store, conv.id)[-1].data, forwarded


# ── 10. A routed child's harness is chosen once, not per message ─────


class _SequenceRoutingClient:
    """Routing-client double returning a different verdict per call.

    :param results: One verdict per call, in order; the last repeats.
    :ivar calls: ``(message, offered_models)`` per :meth:`route` call.
    """

    def __init__(self, *results: RoutingResult) -> None:
        self._results = list(results)
        self.last_error: str | None = None
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    async def route(
        self, message: str, available_models: dict[str, list[str]]
    ) -> RoutingResult | None:
        """Record the offer and return this call's verdict."""
        self.calls.append((message, dict(available_models)))
        index = min(len(self.calls) - 1, len(self._results) - 1)
        return self._results[index]


async def _send_child_message(
    child: Any,
    conv_store: SqlAlchemyConversationStore,
    routing_client: Any,
    text: str,
) -> None:
    """Forward one user message for *child* with *routing_client* installed."""
    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": text}]},
    )
    with patch("omnigent.runtime._globals._caps", new=FakeCaps(routing_client=routing_client)):
        async with echo_runner_client() as runner_client:
            await orchestration_module._forward_event_to_runner(
                child.id,
                child,
                body,
                conv_store,
                runner_client,
            )


async def test_a_follow_up_message_cannot_flip_a_routed_childs_harness(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child's harness is decided on its first message and never again.

    The child arm of the message-routing gate deliberately routes past an
    orchestrator-supplied model, and its condition
    (``conv.parent_conversation_id is not None``) was therefore true on EVERY
    message: each follow-up re-ran the judge and re-persisted BOTH overrides, so
    a second turn could move the child onto another harness family mid-session
    — contradicting the create-time-only contract the ``harness_override``
    forward documents, and stranding the pane the child was already running in.
    """
    _parent_id, child, conv_store = await _auto_parent_and_child(
        client,
        db_uri,
        agent_name="routing-child-no-harness-flip",
        cost_control="on",
        subagent_routing="on",
    )
    routing_client = _SequenceRoutingClient(
        RoutingResult(model=ROUTED_MODEL, rationale="first", harness="claude_code"),
        RoutingResult(model=GPT_MODEL, rationale="second", harness="pi"),
    )

    await _send_child_message(child, conv_store, routing_client, "audit routing")

    after_first = conv_store.get_conversation(child.id)
    assert after_first is not None
    assert len(routing_client.calls) == 1
    assert after_first.model_override == ROUTED_MODEL
    first_harness = after_first.harness_override
    assert first_harness not in (None, "auto")
    assert after_first.labels.get(ROUTING_DECISION_LABEL_KEY)

    # A follow-up on the SAME child, with the router now preferring the other
    # family. Nothing may move.
    await _send_child_message(after_first, conv_store, routing_client, "and now the tests")

    after_second = conv_store.get_conversation(child.id)
    assert after_second is not None
    assert len(routing_client.calls) == 1, "the follow-up must not re-run the judge"
    assert after_second.harness_override == first_harness
    assert after_second.model_override == ROUTED_MODEL
    assert len(_routing_decisions(conv_store, child.id)) == 1


# ── A routing OUTAGE never costs the user their turn ────────────────
#
# The owner's ruling: routing must not be blocking when the call fails. These
# drive a raising routing client through each real dispatch path and assert two
# things every time — the work still happens, and the failure is visible as a
# declined decision rather than an error.


#: What a routing outage is recorded as. Not a model, so nothing can be pinned
#: from it; the same placeholder the auto-harness path already used.
UNAVAILABLE_MODEL = "unavailable"


def _outage_caps(exc: Exception) -> Any:  # type: ignore[explicit-any]
    """
    Caps whose routing client raises *exc* on every call.

    :param exc: The failure to raise, e.g. ``httpx.ReadTimeout("...")``.
    :returns: A ``FakeCaps`` carrying the raising client.
    """
    return FakeCaps(routing_client=FakeRoutingClient(None, error=exc))


#: The five failure modes a routing call can present. A gateway 500 and a 401
#: arrive as ``HTTPStatusError``, a wedged router as a read timeout, a garbled
#: body as a decode error, and an unreachable relay as a connect error.
ROUTING_FAILURES: list[tuple[str, Exception]] = [
    (
        "gateway_500",
        httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(500, text="upstream router unavailable"),
        ),
    ),
    ("timeout", httpx.ReadTimeout("routes:select timed out")),
    ("malformed_body", json.JSONDecodeError("Expecting value", "<not json>", 0)),
    (
        "unauthorized",
        httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("POST", "https://ws.invalid/routes:select"),
            response=httpx.Response(401, text="invalid access token"),
        ),
    ),
    ("relay_unreachable", httpx.ConnectError("connection refused")),
]


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_delivers_the_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    The SDK turn path forwards the message and records a declined decision.

    Regression: ``route_turn`` let its client's failure raise, and the events
    POST turned that into a 500 — the message was persisted and then abandoned.
    A router being down is not a reason to lose a turn.
    """
    agent = await create_test_agent(client, name=f"routing-outage-turn-{label}")
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "cost_control_mode_override": "on"},
    )
    assert resp.status_code == 201, resp.text
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(resp.json()["id"])
    assert conv is not None

    body = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": "refactor auth"}]},
    )
    with patch("omnigent.runtime._globals._caps", new=_outage_caps(failure)):
        async with echo_runner_client() as runner_client:
            forwarded = await orchestration_module._forward_event_to_runner(
                conv.id,
                conv,
                body,
                conv_store,
                runner_client,
            )
    # The turn happened.
    assert forwarded.item_id

    # And the outage is on the record rather than swallowed.
    decisions = _routing_decisions(conv_store, conv.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    assert "Routing call failed" in decisions[0].data.rationale

    refreshed = conv_store.get_conversation(conv.id)
    assert refreshed is not None
    # Nothing pinned: the turn ran on the session's own model.
    assert refreshed.model_override is None
    # And the route-once label is NOT claimed — one outage must not be the
    # reason this session never routes again.
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)


@pytest.mark.parametrize(
    ("label", "failure"), ROUTING_FAILURES, ids=[f[0] for f in ROUTING_FAILURES]
)
async def test_a_routing_outage_still_delivers_a_child_spawns_turn(
    client: httpx.AsyncClient,
    db_uri: str,
    label: str,
    failure: Exception,
) -> None:
    """
    A child spawn runs on the model the orchestrator asked for, and says so.

    ``route_session_harness`` already failed open here, but its ``error`` was
    unpacked and dropped — so the one path that could not route left no card at
    all, and an outage was indistinguishable from a router with no opinion.

    A child of a PINNED parent, deliberately: a child of an auto-harness parent
    carries the ``"auto"`` sentinel and is handled by the auto-harness block
    instead, which has always emitted its own unavailable card.
    """
    _parent_id, child, conv_store = await _pinned_parent_and_child(
        client,
        db_uri,
        agent_name=f"routing-outage-child-{label}",
        harness="claude-sdk",
    )

    await _send_child_message(
        child, conv_store, FakeRoutingClient(None, error=failure), "audit routing"
    )

    refreshed = conv_store.get_conversation(child.id)
    assert refreshed is not None
    # Nothing pinned, and the route-once label is not claimed by a failure.
    assert refreshed.model_override is None
    assert not refreshed.labels.get(ROUTING_DECISION_LABEL_KEY)

    decisions = _routing_decisions(conv_store, child.id)
    assert len(decisions) == 1
    assert decisions[0].data.model == UNAVAILABLE_MODEL
    assert decisions[0].data.applied is False
    assert decisions[0].data.rationale
