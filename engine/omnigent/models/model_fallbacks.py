"""Owned static model tables for Smart Routing and background titles.

Pre-launch picker listings carry no static stand-ins anymore — the live
harness probes (see ``omnigent.host.connect``) are their source of truth.
What remains here is the router's operational data: rankings, arm menus,
probed exclusions that no discovery API can provide, plus the economy-tier
arms background session-title generation pins per harness family.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StaticModelFallback:
    """A release-curated model list with auditable ownership and provenance."""

    model_ids: tuple[str, ...]
    owner: str
    provenance: str
    discovery_gap: str


# ── Smart Routing ───────────────────────────────────────────────────────────
#
# The router's static tables. A live per-session catalog wins wherever one is
# in reach (``omnigent.server.smart_routing.fetch_runner_models``) and a
# deployment's ``routing.*`` settings override each table wholesale; these are
# what a router that can reach neither falls back to.

_SMART_ROUTING_FALLBACKS: dict[str, StaticModelFallback] = {
    "claude_ladder": StaticModelFallback(
        model_ids=(
            "databricks-claude-haiku-4-5",
            "databricks-claude-sonnet-4-6",
            "databricks-claude-sonnet-5",
            "databricks-claude-opus-4-8",
        ),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="AI Gateway Claude serving endpoints, cheapest → most powerful",
        discovery_gap=(
            "the router picks before a session's live model catalog is reachable, "
            "and a gateway listing ranks models by neither cost nor capability"
        ),
    ),
    "gpt_ladder": StaticModelFallback(
        model_ids=(
            "databricks-gpt-5-4-nano",
            "databricks-gpt-5-4-mini",
            "databricks-gpt-5-4",
            "databricks-gpt-5-5",
        ),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="AI Gateway GPT serving endpoints, cheapest → most powerful",
        discovery_gap=(
            "the router picks before a session's live model catalog is reachable, "
            "and a gateway listing ranks models by neither cost nor capability"
        ),
    ),
    "pi_ladder": StaticModelFallback(
        model_ids=(
            "databricks-gpt-5-4-nano",
            "databricks-claude-haiku-4-5",
            "databricks-gpt-5-4-mini",
            "databricks-claude-sonnet-4-6",
            "databricks-claude-sonnet-5",
            "databricks-gpt-5-4",
            "databricks-gpt-5-5",
            "databricks-claude-opus-4-8",
        ),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="the Claude and GPT ladders interleaved by cost, for multi-model pi",
        discovery_gap=(
            "the router picks before a session's live model catalog is reachable, "
            "and a gateway listing ranks models by neither cost nor capability"
        ),
    ),
    "current_generation_gpt": StaticModelFallback(
        model_ids=(
            "databricks-glm-5-2",
            "databricks-gpt-5-6-luna",
            "databricks-gpt-5-6-sol",
        ),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="the external router's own current arms, offered so a pick keeps its endpoint",
        discovery_gap=(
            "the router picks before a session's live model catalog is reachable, "
            "and a gateway listing ranks models by neither cost nor capability"
        ),
    ),
    "task_v1_claude_arms": StaticModelFallback(
        model_ids=("claude-opus-4-8", "claude-sonnet-5"),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="the task_v1 router's Claude arm menu, which it requires in full",
        discovery_gap="the router's arm menu is part of its request contract, not a catalog",
    ),
    "task_v1_codex_arms": StaticModelFallback(
        model_ids=("glm-5-2", "gpt-5-6-sol", "gpt-5-6-luna"),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="the task_v1 router's codex arm menu, which it requires in full",
        discovery_gap="the router's arm menu is part of its request contract, not a catalog",
    ),
    "family_fallbacks": StaticModelFallback(
        model_ids=("claude-sonnet-5", "gpt-5-6-luna"),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="one arm per family (claude, gpt), both frozen members of the task_v1 menus",
        discovery_gap="a workspace that serves no endpoint for the picked arm needs a pinned one",
    ),
    "pi_excluded": StaticModelFallback(
        model_ids=(
            "databricks-claude-haiku-4-5",
            "databricks-gpt-5-5",
            "databricks-gpt-5-5-pro",
            "databricks-gpt-5-6-luna",
            "databricks-gpt-5-6-terra",
            "databricks-gpt-5-6-sol",
        ),
        owner="Smart Routing (omnigent.server.smart_routing)",
        provenance="probed: pi's own gateway 400s on each of these",
        discovery_gap="a gateway listing advertises these without pi's request-shape limits",
    ),
}

#: Claude serving endpoints the router ranks, cheapest → most powerful.
SMART_ROUTING_CLAUDE_LADDER = _SMART_ROUTING_FALLBACKS["claude_ladder"].model_ids

#: GPT serving endpoints the router ranks, cheapest → most powerful.
SMART_ROUTING_GPT_LADDER = _SMART_ROUTING_FALLBACKS["gpt_ladder"].model_ids

#: Both ladders interleaved by cost, for the multi-model pi harness.
SMART_ROUTING_PI_LADDER = _SMART_ROUTING_FALLBACKS["pi_ladder"].model_ids

#: The router's own current gpt-family arms (GLM included), offered as
#: candidates so a routed arm resolves to its own endpoint.
SMART_ROUTING_CURRENT_GENERATION_GPT = _SMART_ROUTING_FALLBACKS["current_generation_gpt"].model_ids

#: The ``task_v1`` router's Claude arm menu, most powerful first.
SMART_ROUTING_TASK_V1_CLAUDE_ARMS = _SMART_ROUTING_FALLBACKS["task_v1_claude_arms"].model_ids

#: The ``task_v1`` router's codex arm menu.
SMART_ROUTING_TASK_V1_CODEX_ARMS = _SMART_ROUTING_FALLBACKS["task_v1_codex_arms"].model_ids

#: One fixed fallback arm per family, ordered ``(claude, gpt)``.
SMART_ROUTING_FAMILY_FALLBACKS = _SMART_ROUTING_FALLBACKS["family_fallbacks"].model_ids

#: Models pi's own gateway rejects, so the router may not pick them under pi.
SMART_ROUTING_PI_EXCLUDED = _SMART_ROUTING_FALLBACKS["pi_excluded"].model_ids

#: The model family each routable harness serves (``"claude"``, ``"gpt"``, ``"pi"``). Read by
#: smart routing (``omnigent.server.smart_routing``) and by the runner's subagent relay
#: (``omnigent.runner.subagent_routing.harness_family``), so both agree on "in-family".
ROUTING_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": "claude",
    "claude_sdk": "claude",
    "claude-native": "claude",
    "pi": "pi",
    "codex-native": "gpt",
}


_ROLLOVER_SUMMARY_FALLBACK = StaticModelFallback(
    model_ids=("gpt-4o",),
    owner="Rollover checkpoint summaries (omnigent.context.rollover)",
    provenance="the summarizer model used when a rollover session's spec names none",
    discovery_gap=(
        "a rollover summary can run before any spec or live catalog has resolved "
        "the session's model"
    ),
)

#: Summarizer model for a rollover checkpoint when the session names no model.
ROLLOVER_SUMMARY_FALLBACK_MODEL = _ROLLOVER_SUMMARY_FALLBACK.model_ids[0]


_ROLLOVER_TOKEN_COUNT_FALLBACK = StaticModelFallback(
    model_ids=("gpt-4o",),
    owner="Rollover recent-turns budget (omnigent.context.rollover)",
    provenance="the tokenizer used to size the verbatim recent turns sent after a compaction",
    discovery_gap=(
        "the budget is counted before the session's model is known on the resume and "
        "delivery paths; only token counting uses it, no model is called"
    ),
)

#: Tokenizer model for sizing the post-compaction recent turns.
ROLLOVER_TOKEN_COUNT_MODEL = _ROLLOVER_TOKEN_COUNT_FALLBACK.model_ids[0]
