"""Tests for the native-subagent routing endpoint and policy."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.conversation import RoutingDecisionData
from omnigent.harnesses.hook_scripts.subagent_router import read_router_endpoint
from omnigent.runner.subagent_routing import (
    ADVERTISEMENT_FILE,
    PLAIN_SESSION,
    SessionRoutingClass,
    SubagentRouteDecision,
    SubagentRouteRequest,
    ensure_session_router,
    ensure_session_router_quietly,
    forget_session_routing_class,
    make_server_relay_resolver,
    persist_subagent_decision,
    relayed_decisions,
    remember_session_routing_class,
    routed_models,
    router_dir_for_session,
    session_router_env,
    session_routing_class,
    shutdown_session_router,
    subagent_routing_enabled,
    write_advertisement,
)
from omnigent.server.smart_routing import RoutingResult, RoutingSettings
from omnigent.server.subagent_route_policy import (
    candidate_models,
    decision_record,
    resolve_subagent_route,
)
from tests.server.helpers import FakeCaps, FakeRoutingClient

CLAUDE_MODEL = "databricks-claude-opus-4-8"
GPT_MODEL = "databricks-gpt-5-5"
GLM_MODEL = "databricks-glm-5-2"
# The spelling the gateway actually serves GLM under; see ``_SERVABLE_ALIASES``.
GLM_SERVABLE = "system.ai.glm-5-2"
KIMI_MODEL = "databricks-kimi-k2-6"
PARENT_MODEL = "databricks-claude-sonnet-4-6"

# ── Stubs ───────────────────────────────────────────────────────────


@dataclass
class _PersistedItem:
    id: str


@dataclass
class _FakeStore:
    appended: list[Any] = field(default_factory=list)  # type: ignore[explicit-any]

    def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:  # type: ignore[explicit-any]
        del session_id
        self.appended.extend(items)
        return [_PersistedItem(id=f"item_{len(self.appended)}")]


def _request(**overrides: Any) -> SubagentRouteRequest:
    kwargs: dict[str, Any] = {
        "harness": "claude-native",
        "task_name": "code-reviewer",
        "prompt": "review the diff",
        "parent_model": PARENT_MODEL,
    }
    kwargs.update(overrides)
    return SubagentRouteRequest(**kwargs)


# ── Candidate set ───────────────────────────────────────────────────


def test_the_glm_top_up_does_not_widen_a_multi_model_harness() -> None:
    """pi admits every family, but nothing proves its CLI can serve GLM."""
    assert GLM_SERVABLE not in candidate_models("pi")["pi"]


class _Conv:
    """Minimal conversation row for the auto-harness gate."""

    def __init__(self, labels: dict[str, str] | None = None, harness_override: str | None = None):
        self.labels = labels or {}
        self.harness_override = harness_override


# ── Policy ──────────────────────────────────────────────────────────


async def test_same_family_pick_rewrites() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "rewrite"
    assert decision.model == CLAUDE_MODEL
    assert decision.harness is None
    assert decision.raw_model is None
    assert decision.rationale == "deep reasoning"
    assert len(decision.decision_id) == 36


async def test_the_router_is_called_even_when_the_spawn_names_a_model() -> None:
    """Regression: a ``requested_model`` used to skip ``client.route`` entirely,
    so every rationale read "it is a routable arm" and no rule trace existed."""
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning"))
    decision = await resolve_subagent_route(
        "conv_1",
        _request(requested_model=CLAUDE_MODEL),
        caps=FakeCaps(routing_client=client),
    )
    assert len(client.calls) == 1
    assert decision.rationale.startswith("deep reasoning")
    assert "it is a routable arm" not in decision.rationale


async def test_a_long_context_suffix_is_the_same_arm() -> None:
    """``[1m]`` names a context window, not another arm."""
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning"))
    req = _request(requested_model=f"{CLAUDE_MODEL}[1m]")
    decision = await resolve_subagent_route("conv_1", req, caps=FakeCaps(routing_client=client))
    assert "honored" in decision.rationale
    assert decision_record(req, decision).attempted_override is None


@pytest.mark.parametrize(
    ("picked", "harness", "cross_harness", "expected_action", "rationale_head"),
    [
        # The router keeping the parent model still answers the ask.
        (PARENT_MODEL, None, False, "allow", "parent model fits"),
        # A cross-harness pick the spawn asked for is still the ask, honored.
        (GPT_MODEL, "codex", True, "redirect", "narrow change"),
    ],
    ids=["allow", "redirect"],
)
async def test_a_matching_ask_is_honored_on_every_action(
    picked: str,
    harness: str | None,
    cross_harness: bool,
    expected_action: str,
    rationale_head: str,
) -> None:
    """Honoring is about agreement, not about the verdict's shape.

    ``rewrite`` is covered by the strict-adherence matrix above; these are the
    two other actions a matching ask can ride, and both must read the same.
    """
    client = FakeRoutingClient(
        RoutingResult(model=picked, rationale=rationale_head, harness=harness)
    )
    req = _request(requested_model=picked)
    decision = await resolve_subagent_route(
        "conv_1", req, caps=FakeCaps(routing_client=client), cross_harness=cross_harness
    )
    assert decision.action == expected_action
    assert decision.rationale == (
        f"{rationale_head} Spawn requested {picked}; honored — the router picked the same arm."
    )
    assert decision_record(req, decision).attempted_override is None


async def test_a_deny_claims_nothing_about_the_ask() -> None:
    """Nothing spawned, so there is no ask to call honored or overridden."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1",
        _request(requested_model=CLAUDE_MODEL),
        caps=FakeCaps(routing_client=client),
    )
    assert decision.action == "deny"
    assert "honored" not in decision.rationale
    assert "overridden" not in decision.rationale


@pytest.mark.parametrize(
    "client",
    [
        FakeRoutingClient(error=RuntimeError("router down")),
        FakeRoutingClient(None, last_error="menu mismatch"),
        None,
    ],
    ids=["outage", "no-verdict", "no-client"],
)
async def test_fail_open_leaves_the_spawn_on_the_model_it_asked_for(
    client: FakeRoutingClient | None,
) -> None:
    """A router outage allows the spawn with no model, so the hook leaves
    ``tool_input`` alone and the spawn runs on exactly what it named — the
    audit record has to say so rather than name the inherited model."""
    req = _request(requested_model=GLM_SERVABLE)
    decision = await resolve_subagent_route("conv_1", req, caps=FakeCaps(routing_client=client))
    assert decision.action == "allow"
    assert decision.model is None
    assert "honored" not in decision.rationale
    record = decision_record(req, decision)
    assert record.model == GLM_SERVABLE
    assert record.applied is False
    assert record.attempted_override is None


async def test_raw_model_is_omitted_when_it_matches_the_resolved_model() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model=CLAUDE_MODEL)
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model is None


async def test_raw_model_is_preserved_when_the_router_named_another_arm() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="r", raw_model="claude-opus-4-8-thinking")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.model == CLAUDE_MODEL
    assert decision.raw_model == "claude-opus-4-8-thinking"
    assert decision_record(_request(), decision).raw_model == "claude-opus-4-8-thinking"


async def test_pick_outside_candidate_set_denies() -> None:
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "deny"
    assert decision.model is None
    assert "cannot run" in decision.rationale
    # The deny message still names the pick it rejected.
    assert decision.raw_model == "databricks-kimi-k2"


async def test_parent_model_pick_allows_unchanged() -> None:
    client = FakeRoutingClient(
        RoutingResult(model=PARENT_MODEL, rationale="parent model fits", harness="claude-sdk")
    )
    decision = await resolve_subagent_route(
        "conv_1", _request(), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL


async def test_fork_is_exempt_and_never_calls_router() -> None:
    client = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="x"))
    decision = await resolve_subagent_route(
        "conv_1", _request(fork=True), caps=FakeCaps(routing_client=client)
    )
    assert decision.action == "allow"
    assert decision.model == PARENT_MODEL
    assert client.calls == []


@pytest.mark.parametrize(
    ("client", "reason_fragment"),
    [
        # A router outage allows the spawn unchanged: the gate is advisory.
        (FakeRoutingClient(error=RuntimeError("router down")), "Routing unavailable"),
        # A no-verdict (not an outage) carries the router's own reason so the
        # user sees why nothing was routed.
        (FakeRoutingClient(None, last_error="menu mismatch"), "menu mismatch"),
        # No routing client configured at all is the same advisory outcome.
        (None, "no routing client configured"),
    ],
)
async def test_router_failure_allows_the_spawn_unchanged(
    client: FakeRoutingClient | None,
    reason_fragment: str,
) -> None:
    caps = FakeCaps(routing_client=client, routing_settings=RoutingSettings())
    decision = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert decision.action == "allow"
    assert reason_fragment in decision.rationale


async def test_a_swallowed_router_exception_still_names_its_cause() -> None:
    """A client that raises past its own reporting leaves the cause on the chip.

    ``last_error`` is unset when ``route`` raises before it can record one, and
    the decline then said only "router returned no verdict" while the real
    cause (a timeout, which stringifies empty) lived in the server log alone.
    """
    import httpx

    caps = FakeCaps(
        routing_client=FakeRoutingClient(error=httpx.ReadTimeout("")),
        routing_settings=RoutingSettings(),
    )
    decision = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert decision.action == "allow"
    assert "router call failed: ReadTimeout" in decision.rationale


async def test_identical_spawns_are_each_routed() -> None:
    """Decisions are not cached: a decision_id is an identity, not a key."""
    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )
    caps = FakeCaps(routing_client=client)
    first = await resolve_subagent_route("conv_1", _request(), caps=caps)
    second = await resolve_subagent_route("conv_1", _request(), caps=caps)
    assert len(client.calls) == 2
    assert second.decision_id != first.decision_id


async def test_task_name_is_capped_at_parse_time() -> None:
    """``task_name`` is agent-authored, so it is bounded before it is stored."""
    req = SubagentRouteRequest.from_payload({"harness": "codex", "task_name": "x" * 5000})
    assert len(req.task_name) == 200


# ── Decision persistence ────────────────────────────────────────────


async def test_deny_is_persisted_unapplied() -> None:
    """An unoffered pick is the only deny left, and it persists unapplied."""
    client = FakeRoutingClient(
        RoutingResult(model="databricks-kimi-k2", rationale="unavailable", harness="claude-sdk")
    )
    records: list[RoutingDecisionData] = []

    async def _persist(record: RoutingDecisionData) -> None:
        records.append(record)

    caps = FakeCaps(routing_client=client)
    await resolve_subagent_route("conv_1", _request(), caps=caps, persist=_persist)
    assert records[0].applied is False
    assert records[0].model == PARENT_MODEL


async def test_persist_failure_does_not_break_the_decision() -> None:
    class _BoomStore:
        def append(self, session_id: str, items: list[Any]) -> list[_PersistedItem]:
            raise RuntimeError("db down")

    client = FakeRoutingClient(
        RoutingResult(model=CLAUDE_MODEL, rationale="deep reasoning", harness="claude-sdk")
    )

    async def _persist(record: RoutingDecisionData) -> None:
        await persist_subagent_decision("conv_1", _BoomStore(), record)

    decision = await resolve_subagent_route(
        "conv_1",
        _request(),
        caps=FakeCaps(routing_client=client),
        persist=_persist,
    )
    assert decision.action == "rewrite"


# ── Advertisement + loopback endpoint ───────────────────────────────


def test_advertisement_roundtrip(tmp_path: Path) -> None:
    path = write_advertisement(tmp_path, url="http://127.0.0.1:1234", token="tok")
    assert path.name == ADVERTISEMENT_FILE
    endpoint = read_router_endpoint(tmp_path)
    assert endpoint is not None
    assert (endpoint.url, endpoint.token) == ("http://127.0.0.1:1234", "tok")


def _post(url: str, body: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def test_server_relay_resolver_allows_on_hop_failure() -> None:
    class _DeadClient:
        async def post(self, path: str, *, json: dict[str, Any], timeout: float) -> Any:
            raise RuntimeError("connection refused")

    decision = await make_server_relay_resolver(_DeadClient())("conv_1", _request())
    assert decision.action == "allow"
    assert "unreachable" in decision.rationale


# ── Enablement gate + session router lifecycle (P7) ─────────────────
#
# Two gates, deliberately named apart. ``routing_enabled`` is the TURN gate: it
# decides whether a session's own turns are routed, and a child reads its
# parent's cost-control mode because the server routes children of a routed
# parent. ``subagent_routing_enabled`` is the SPAWN gate: two-state, stamped at
# create, and it reads one explicit switch. The turn-gate tests below used to be
# named ``test_routing_enabled_*``, which read as the spawn gate.


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        # Two-state: only an explicit "on" routes spawns. A routed create is
        # stamped "on", so unset is Default and reads exactly like "off".
        ("on", True),
        ("off", False),
        (None, False),
        # A row written before the switch became two-state can still hold the
        # old tri-state "inherit"; it reads as not-enabled, so no stored value
        # needs rewriting for the gate to be sound.
        ("inherit", False),
        ("", False),
    ],
)
def test_subagent_routing_enabled_is_two_state(override: str | None, expected: bool) -> None:
    assert subagent_routing_enabled(override) is expected


# ── Session routing class ───────────────────────────────────────────
#
# The three-way distinction the codex launch paths key off. Kept as its own
# value rather than folded into one bool: "Smart Routing is on" and "Smart
# Routing also owns the harness" buy different things on codex, and conflating
# them is what made a plain session pay the routing path's costs.


def test_the_recorded_routing_class_survives_until_the_session_ends() -> None:
    assert session_routing_class("conv_class") == PLAIN_SESSION
    auto = SessionRoutingClass(routing_enabled=True, auto_harness=True)
    remember_session_routing_class("conv_class", auto)
    try:
        assert session_routing_class("conv_class") == auto
    finally:
        forget_session_routing_class("conv_class")
    assert session_routing_class("conv_class") == PLAIN_SESSION


def test_router_dir_for_session_is_owner_only(tmp_path: Path) -> None:
    path = router_dir_for_session("conv_router_dir")
    assert path.is_dir()
    assert path.stat().st_mode & 0o777 == 0o700


def test_ensure_session_router_is_idempotent_and_advertises_everywhere(
    tmp_path: Path,
) -> None:
    class _DeadClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("server down")

    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"

    async def _run() -> None:
        router = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=first_dir,
            server_client=_DeadClient(),
        )
        again = ensure_session_router(
            "conv_lifecycle",
            bridge_dir=second_dir,
            server_client=_DeadClient(),
        )
        assert again is router
        # Same rendezvous advertised in both directories.
        assert read_router_endpoint(first_dir) == read_router_endpoint(second_dir)
        env = session_router_env("conv_lifecycle")
        assert env["OMNIGENT_SUBAGENT_ROUTER_SESSION_ID"] == "conv_lifecycle"
        assert env["OMNIGENT_SUBAGENT_ROUTER_DIR"] == str(first_dir)
        shutdown_session_router("conv_lifecycle")
        assert session_router_env("conv_lifecycle") == {}

    asyncio.run(_run())


def test_router_startup_log_omits_the_rendezvous(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The handle carries the bearer token, so neither url nor token is logged."""

    class _DeadClient:
        async def post(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("server down")

    async def _run() -> None:
        with caplog.at_level(logging.INFO, logger="omnigent.runner.subagent_routing"):
            router = ensure_session_router(
                "conv_log_redaction",
                bridge_dir=tmp_path,
                server_client=_DeadClient(),
            )
        try:
            text = caplog.text
            assert "subagent router started" in text
            assert router.token not in text
            assert router.url not in text
            # Nothing that addresses the rendezvous is logged either.
            assert "conv_log_redaction" not in text
            assert str(tmp_path) not in text
        finally:
            shutdown_session_router("conv_log_redaction")

    asyncio.run(_run())


def test_ensure_session_router_quietly_skips_without_a_server_client(tmp_path: Path) -> None:
    assert (
        ensure_session_router_quietly(
            "conv_no_client",
            bridge_dir=tmp_path,
            server_client=None,
        )
        is None
    )
    assert not (tmp_path / ADVERTISEMENT_FILE).exists()


def test_relayed_decisions_start_empty() -> None:
    assert relayed_decisions("conv_never_routed") == ()
    assert routed_models("conv_never_routed") == frozenset()


# ── Router source ───────────────────────────────────────────────────
#
# Gateway backing selects WHICH router answers a spawn, and the verdict records
# it so the chip can say so. Off the gateway the static ``infer_models`` table is
# unusable (all ``databricks-*`` ids), so only the live catalog may supply
# candidates.


async def test_an_off_gateway_spawn_is_routed_by_the_built_in_judge() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_oss", _request(), caps=caps, gateway_backed=False
    )
    assert decision.router_source == "oss-llm"
    assert decision.rationale == "local"
    assert external.calls == []


async def test_a_gateway_backed_spawn_is_routed_by_the_external_router() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_aigw", _request(), caps=caps, gateway_backed=True
    )
    assert decision.router_source == "databricks-aigw"
    assert local.calls == []


async def test_a_spawn_falls_back_to_the_judge_when_the_router_fails() -> None:
    """A workspace with no routing API must not cost every spawn its decision."""
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(
        None, last_error="router returned HTTP 404: routes:select is not enabled"
    )
    local = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="local"))
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_fallback", _request(), caps=caps, gateway_backed=True
    )
    assert decision.router_source == "oss-llm"
    assert decision.model == CLAUDE_MODEL
    assert external.calls != []


async def test_a_spawn_keeps_the_routers_reason_when_nothing_can_answer() -> None:
    """Fail-open is unchanged: the spawn runs, the chip carries the reason."""
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(None, last_error="router returned HTTP 404: not enabled")
    local = FakeRoutingClient(None, last_error="the judge had no opinion")
    caps = FakeCaps(
        routing_client=external,
        routing_backends=RoutingBackends(external=external, local=local),
    )
    decision = await resolve_subagent_route(
        "conv_none", _request(), caps=caps, gateway_backed=True
    )
    assert decision.action == "allow"
    assert decision.model is None
    assert decision.router_source is None
    assert "the judge had no opinion" in decision.rationale


async def test_an_external_only_deployment_cannot_route_an_off_gateway_spawn() -> None:
    from omnigent.server.routing_backend import RoutingBackends

    external = FakeRoutingClient(RoutingResult(model=CLAUDE_MODEL, rationale="external"))
    caps = FakeCaps(routing_client=external, routing_backends=RoutingBackends(external=external))
    decision = await resolve_subagent_route(
        "conv_no_source", _request(), caps=caps, gateway_backed=False
    )
    assert decision.action == "allow"
    assert decision.model is None
    assert decision.router_source is None
    assert external.calls == []


async def test_router_source_survives_the_payload_round_trip() -> None:
    decision = SubagentRouteDecision(
        action="rewrite",
        rationale="deep reasoning",
        model=CLAUDE_MODEL,
        router_source="databricks-aigw",
    )
    payload = decision.to_payload()
    assert payload["router_source"] == "databricks-aigw"
    assert SubagentRouteDecision.from_payload(payload).router_source == "databricks-aigw"


def test_a_payload_without_a_router_source_parses_as_none() -> None:
    parsed = SubagentRouteDecision.from_payload(
        {"action": "rewrite", "rationale": "x", "model": CLAUDE_MODEL}
    )
    assert parsed.router_source is None
    # A blank string is no source at all, not an empty one.
    assert SubagentRouteDecision.from_payload({"router_source": ""}).router_source is None


def test_decision_record_copies_the_router_source() -> None:
    req = _request()
    decision = SubagentRouteDecision(
        action="rewrite",
        rationale="deep reasoning",
        model=CLAUDE_MODEL,
        router_source="oss-llm",
    )
    assert decision_record(req, decision).router_source == "oss-llm"
    # A fail-open allow routed nothing, so it names no source.
    unrouted = SubagentRouteDecision(action="allow", rationale="Routing unavailable")
    assert decision_record(req, unrouted).router_source is None


def test_decision_record_names_the_task_it_governed() -> None:
    """The persisted item carries the spawn's label, so a fan-out's chips
    can each be tied to the sub-agent/task their decision governed."""
    decision = SubagentRouteDecision(
        action="rewrite", rationale="deep reasoning", model=CLAUDE_MODEL
    )
    record = decision_record(_request(task_description="Implement token refresh"), decision)
    assert record.task_description == "Implement token refresh"
    # A spawn that carried no label records none.
    assert decision_record(_request(), decision).task_description is None
