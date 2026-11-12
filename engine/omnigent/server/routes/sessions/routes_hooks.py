"""Hook routes: policy evaluation and the subagent-routing hook."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, NamedTuple

from fastapi import (
    APIRouter,
    Depends,
    Request,
)
from fastapi.responses import Response

from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import Agent, Conversation, LoadedAgent
from omnigent.errors import ElicitationDeclinedError, ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime import (
    get_agent_cache,
    get_caps,
    get_policy_store,
    pending_inputs,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.runtime.policies.builder import (
    any_policies_apply,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._content_type import (
    require_json_content_type,
)
from omnigent.server.routes._sessions.common import (
    _EVALUATE_HOOK_ELICITATION_ID_RE,
    _TURN_ACTOR_LABEL,
    _llm_response_denied_turns,
    _logger,
    _runner_relay_tasks,
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import (
    _build_actor,
    _build_evaluation_context,
    _emit_server_routing_decision,
    _forward_session_change_to_runner,
    _get_runner_client,
    _native_ask_gate_lock,
    _publish_policy_denied,
    _resolve_harness,
)
from omnigent.server.routes._sessions.orchestration import (
    _hold_native_ask_gate,
    _spawn_gateway_backed,
)
from omnigent.spec.types import (
    Phase,
    PolicyAction,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore

# A fixed stripe of locks, so the set never grows with the number of agents.
_AGENT_LOAD_LOCKS = tuple(threading.Lock() for _ in range(16))


def _load_agent_once(cache: AgentCache, agent: Agent) -> LoadedAgent:
    """
    Load *agent*'s bundle through the process-wide cache, one loader per agent.

    The cache memoises the parsed spec per process; holding the agent's lock
    stripe makes concurrent cold loads after a restart extract the bundle once
    instead of once per waiting request. Runs on a worker thread.

    :param cache: The process's :class:`AgentCache`.
    :param agent: The session's agent row.
    :returns: The loaded agent.
    """
    with _AGENT_LOAD_LOCKS[hash(agent.id) % len(_AGENT_LOAD_LOCKS)]:
        return cache.load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)


def register_hooks_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
) -> None:
    """Register the hooks routes on router."""

    # ── Proto event-type → validation schema, as ONE total mapping ────
    #
    # This used to be three independent structures: which wire types are
    # accepted, which phases require an object payload, and which phases
    # need a tool name and where it may come from. The docstring on each
    # said "one schema per phase" while the code let you add a wire type to
    # the first table alone — the new phase was then accepted, validated as
    # permissively as possible (``dict | str``), and got no tool-name rule
    # at all, because the other two tables simply had no entry for it and
    # every lookup against them used a permissive default (``in a
    # frozenset`` / ``.get(phase, ())``).
    #
    # A NamedTuple with no defaults on either field makes that impossible:
    # you cannot add a wire type without also deciding, right there, both
    # whether its payload must be an object and where its tool name (if any)
    # comes from. There is deliberately no fallback path for a phase this
    # dict doesn't recognize — the "Unknown event type" branch below is the
    # only way to reach past it, and it 400s rather than silently allowing.
    #
    # ``data`` shape: tool and LLM phases carry a structured payload, so a
    # string there is a malformed call. REQUEST carries prompt text and is
    # the only phase where a bare string is a legitimate wire form.
    #
    # The in-process tool dispatch sends the result's tool name in
    # ``request_data.name``.
    class _PhaseSchema(NamedTuple):
        phase: Phase
        data_must_be_object: bool
        tool_name_sources: tuple[tuple[str, str], ...]

    _PHASE_SCHEMA_BY_WIRE_TYPE: dict[str, _PhaseSchema] = {
        "PHASE_TOOL_CALL": _PhaseSchema(
            phase=Phase.TOOL_CALL,
            data_must_be_object=True,
            tool_name_sources=(("data", "event.data.name"),),
        ),
        "PHASE_TOOL_RESULT": _PhaseSchema(
            phase=Phase.TOOL_RESULT,
            data_must_be_object=True,
            tool_name_sources=(("request_data", "event.request_data.name"),),
        ),
        "PHASE_LLM_REQUEST": _PhaseSchema(
            phase=Phase.LLM_REQUEST,
            data_must_be_object=True,
            tool_name_sources=(),
        ),
        "PHASE_LLM_RESPONSE": _PhaseSchema(
            phase=Phase.LLM_RESPONSE,
            data_must_be_object=True,
            tool_name_sources=(),
        ),
        # A native session's UserPromptSubmit hook posts the request phase
        # here (the server-level _evaluate_input_policy skips native message
        # events). The prompt text rides in ``event.data.text``, and this is
        # the only phase where ``data`` as a bare string is legitimate.
        "PHASE_REQUEST": _PhaseSchema(
            phase=Phase.REQUEST,
            data_must_be_object=False,
            tool_name_sources=(),
        ),
    }
    _PHASE_TO_PROTO_ACTION: dict[PolicyAction, str] = {
        PolicyAction.ALLOW: "POLICY_ACTION_ALLOW",
        PolicyAction.DENY: "POLICY_ACTION_DENY",
        PolicyAction.ASK: "POLICY_ACTION_ASK",
    }

    # ── POST /sessions/{session_id}/policies/evaluate ─────────────

    @router.post(
        "/sessions/{session_id}/policies/evaluate",
        # Returns EvaluationResponse JSON; no Pydantic model since the
        # proto-style schema is validated manually.
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def evaluate_policy(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Generic policy evaluation endpoint (proto-compatible).

        Accepts an ``EvaluationRequest`` JSON body whose ``event``
        field carries the phase (``PHASE_TOOL_CALL``,
        ``PHASE_TOOL_RESULT``, ``PHASE_LLM_REQUEST``,
        ``PHASE_LLM_RESPONSE``), the event data, and optional
        context. Returns an ``EvaluationResponse`` with the policy
        verdict (``result``), an optional ``reason``, and optional
        ``data`` for content-rewriting policies.

        Used by the runner to evaluate admin policies on tool calls and
        requests. Also usable by any client that speaks the
        proto-compatible JSON schema.

        :param request: FastAPI request — body is the
            ``EvaluationRequest`` JSON envelope.
        :param session_id: Omnigent conversation id from the URL path.
        :returns: ``EvaluationResponse`` JSON with ``result``,
            ``reason``, and optional ``data``.
        :raises OmnigentError: 404 if the session doesn't exist,
            400 if the body is malformed.
        """
        from omnigent.server.routes import sessions as _sf

        user_id = _sf._get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        is_read_only = access.level is not None and access.level < LEVEL_EDIT
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in policy evaluate body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "Policy evaluate body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event = payload.get("event")
        if not isinstance(event, dict):
            raise OmnigentError(
                "Policy evaluate body must include an 'event' object.",
                code=ErrorCode.INVALID_INPUT,
            )
        event_type = event.get("type")
        if event_type is not None and not isinstance(event_type, str):
            # An unhashable type (list / dict) would raise inside the phase
            # lookup below and surface as a 500 instead of a client error.
            raise OmnigentError(
                f"Policy evaluate 'event.type' must be a string; got {type(event_type).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        schema = _PHASE_SCHEMA_BY_WIRE_TYPE.get(event_type or "")
        if schema is None:
            raise OmnigentError(
                f"Unknown event type: {event_type!r}. "
                f"Expected one of {list(_PHASE_SCHEMA_BY_WIRE_TYPE)}.",
                code=ErrorCode.INVALID_INPUT,
            )
        phase = schema.phase
        # Optional stable re-attach id for hook retries. Validated but not
        # required — absent on non-retrying callers (old hooks, direct API use).
        raw_elicitation_id = payload.get("_omnigent_elicitation_id")
        hook_elicitation_id: str | None = None
        if raw_elicitation_id is not None:
            if not isinstance(raw_elicitation_id, str) or not (
                _EVALUATE_HOOK_ELICITATION_ID_RE.fullmatch(raw_elicitation_id)
            ):
                raise OmnigentError(
                    "Policy evaluate '_omnigent_elicitation_id' must match "
                    "'elicit_evaluate_' + 32 hex chars.",
                    code=ErrorCode.INVALID_INPUT,
                )
            hook_elicitation_id = raw_elicitation_id
        # Validation is driven from the per-phase schema above rather than
        # from a chain of conditionals: rules that live in branches get
        # applied to whichever phase happens to reach that branch. The three
        # rules below are independent and all of them run for every phase.
        #
        # Everything is checked BEFORE normalizing. ``or {}`` turns null,
        # false, 0, "" and [] into an empty object, and a tool phase
        # evaluating with no name gates with ``tool_name=None``, which skips
        # every tool-scoped policy — on a hook that blocks, that fails open.
        raw_data = event.get("data")
        if schema.data_must_be_object:
            # An absent payload is as malformed as a wrongly typed one here:
            # ``None`` normalized to ``{}`` and evaluated as an empty call,
            # which was the original defect's headline vector.
            if not isinstance(raw_data, dict):
                raise OmnigentError(
                    f"Policy evaluate 'event.data' must be an object for "
                    f"{event_type!r}; got {type(raw_data).__name__}.",
                    code=ErrorCode.INVALID_INPUT,
                )
        elif not isinstance(raw_data, (dict, str)):
            # Absent is malformed here too: every first-party producer of this
            # phase sends the prompt text, and ``None`` normalized to ``{}``
            # gates on an empty prompt.
            raise OmnigentError(
                f"Policy evaluate 'event.data' must be an object or string for "
                f"{event_type!r}; got {type(raw_data).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        # A tool-scoped gate cannot run without a tool name, and allowing by
        # default is the wrong direction on a blocking hook. Producers spell
        # the name in different places, so any declared source satisfies the
        # rule; the resolved name is written back to the container the engine
        # reads, so the guard and the gate agree on it.
        name_sources = schema.tool_name_sources
        if name_sources:
            resolved_name: str | None = None
            for container_key, _field_path in name_sources:
                if container_key == "data":
                    candidate = raw_data.get("name") if isinstance(raw_data, dict) else None
                elif container_key == "target":
                    candidate = event.get("target")
                else:
                    container = event.get(container_key)
                    candidate = container.get("name") if isinstance(container, dict) else None
                if isinstance(candidate, str) and candidate:
                    resolved_name = candidate
                    break
            if resolved_name is None:
                paths = " or ".join(repr(path) for _key, path in name_sources)
                raise OmnigentError(
                    f"Policy evaluate requires a non-empty tool name in {paths} "
                    f"for {event_type!r}.",
                    code=ErrorCode.INVALID_INPUT,
                )
            primary_key = name_sources[0][0]
            if primary_key != "data":
                # Normalize onto the container the engine reads, so a producer
                # that names the tool elsewhere gets tool-scoped policies too
                # instead of merely passing validation.
                primary = event.get(primary_key)
                event[primary_key] = (
                    {**primary, "name": resolved_name}
                    if isinstance(primary, dict)
                    else {"name": resolved_name}
                )
        data = raw_data or {}
        raw_event_context = event.get("context")
        if raw_event_context is not None and not isinstance(raw_event_context, dict):
            raise OmnigentError(
                f"Policy evaluate 'event.context' must be an object; got "
                f"{type(raw_event_context).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Reuse the row the ACL check already fetched — same point in the
        # request, so no less fresh than reading it again here, one query
        # fewer on the blocking PreToolUse path. Absent for admin callers
        # (who bypass the conversation lookup) and when permissions are
        # disabled, which fall back to their own read.
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError(
                f"Session {session_id!r} not found.",
                code=ErrorCode.NOT_FOUND,
            )
        # Dedup the native request-phase gate. A native session's
        # ``UserPromptSubmit`` hook posts ``PHASE_REQUEST`` here for *every*
        # prompt, but a web-UI prompt was already gated server-side by
        # ``_evaluate_input_policy`` at POST /events (before injection, so no
        # TUI freeze). Re-gating it here would double-prompt the human. A
        # web-UI prompt in flight has a ``pending_inputs`` entry (recorded at
        # dispatch, drained when the forwarder mirrors it back); a prompt
        # typed directly in the TUI has none and never hit POST /events, so it
        # is gated here — the hook is its only request-phase gate. The signal
        # is "is a web prompt in flight", not text correlation (the native
        # transcript gives no reliable id channel — see ``pending_inputs``).
        if phase == Phase.REQUEST and pending_inputs.snapshot_for(session_id):
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )
        _caps = _sf.get_caps()
        agent_cache = _sf.get_agent_cache()
        _host_conn: dict[str, str] | None = None
        agent: Agent | None = None
        loaded: LoadedAgent | None = None

        def _prepare() -> tuple[str, PolicyEngine | None]:
            """
            Resolve agent, spec and engine in one worker-thread hop.

            The agent read, the bundle load (a tar extraction on a cold cache),
            the fast-path policy check and the engine build all touch the
            database or disk, so none of them may run on the event loop.

            :returns: ``("no_agent", None)``, ``("no_policies", None)``, or
                ``("engine", engine)``.
            """
            nonlocal agent, loaded, _host_conn
            agent = agent_store.get(conv.agent_id) if conv.agent_id else None
            if agent is None:
                return "no_agent", None
            loaded = _load_agent_once(agent_cache, agent)
            # Fast path: if no policies would fire (no agent guardrails, no
            # session policies, no server-wide defaults), skip the engine build
            # entirely. This avoids conversation-store reads for labels/state/
            # usage on every tool call for the common no-policy case. Session
            # policies are LRU-cached so this check is cheap after the first call
            # per session. Users can add policies mid-session — the cache is
            # invalidated on mutation, so newly added policies are visible on
            # the very next call.
            if not any_policies_apply(
                spec=loaded.spec,
                conversation_id=session_id,
                default_policies=_caps.default_policies,
                policy_store=get_policy_store(),
                phase=phase,
                tool_name=data.get("name") if isinstance(data, dict) else None,
                # A sub-agent conversation's own guardrails live on the CHILD
                # spec inside this bundle; without the row the check would
                # fast-path skip a bundle whose only policies are child-declared.
                conversation=conv,
            ):
                return "no_policies", None
            _host_conn = (
                _caps.policy_llm_connection_factory()
                if _caps.policy_llm_connection_factory
                else None
            )
            return "engine", _build_engine(conv)

        def _build_engine(preloaded_conv: Conversation | None = None) -> PolicyEngine:
            """
            Build a policy engine for this session from the loaded spec.

            Re-reads persisted ``session_state`` / usage from the store on
            every call: the engine snapshots that state at construction and
            does not re-query it during ``evaluate``, so a fresh build is the
            only way to observe a concurrent sibling's just-recorded approval.

            :param preloaded_conv: The conversation row this handler already
                loaded, passed on the FIRST build only to skip the builder's
                re-read. Rebuilds that must observe concurrent writes (the
                ASK-gate re-evaluation) pass ``None`` for a fresh read.
            :returns: A :class:`PolicyEngine` seeded with the latest
                persisted state for ``session_id``.
            """
            assert agent is not None and loaded is not None
            return build_policy_engine(
                spec=loaded.spec,
                conversation_id=session_id,
                conversation_store=conversation_store,
                conversation=preloaded_conv,
                # ``agent`` below was resolved from conv.agent_id; the builder
                # re-reads the row and fails closed if it was rebound since.
                expected_agent_id=agent.id,
                default_policies=_caps.default_policies,
                policy_store=get_policy_store(),
                server_llm=_caps.llm,
                host_connection=_host_conn,
            )

        outcome, built = await asyncio.to_thread(_prepare)
        if outcome == "no_agent":
            # No agent — no policies. Return unspecified (pass-through).
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_UNSPECIFIED"}),
                media_type="application/json",
            )
        if built is None:
            return Response(
                content=json.dumps({"result": "POLICY_ACTION_ALLOW"}),
                media_type="application/json",
            )
        engine = built
        # Use the turn-initiating human's identity (persisted at forward time)
        # so per-user policies gate on the correct actor even when the HTTP
        # caller is the runner's service-account credential.  Falls back to
        # user_id for direct API callers and native-terminal sessions (whose
        # turns go via _dispatch_session_event_to_runner, which does not write
        # this label).
        # Read the actor from the engine's label snapshot, not from the row
        # fetched at the top of this handler: the engine's labels come from a
        # read taken after the agent/spec load, so a turn-actor label written
        # in that window still gates on the right principal. (``agent_id``
        # cannot be treated the same way — it selects the spec the engine is
        # built from, so it is necessarily read first.)
        turn_actor = engine.labels.get(_TURN_ACTOR_LABEL)
        ctx = _build_evaluation_context(
            phase, data, event, actor=_build_actor(turn_actor or user_id)
        )
        result = await engine.evaluate(ctx, read_only=is_read_only)

        # URL-based elicitation for blocking phases: on a TOOL_CALL or
        # LLM_REQUEST ASK, hold the gate server-side rather than
        # returning ASK. Returning ASK makes the native hook emit
        # ``defer``, which a permissive ``permission_mode``
        # (acceptEdits / bypassPermissions) auto-approves — bypassing
        # the human. Instead we publish the approval elicitation, park
        # until the human resolves it via the resolve URL, and collapse
        # to a hard ALLOW / DENY so the caller never sees ASK.
        # TOOL_CALL, LLM_REQUEST, and REQUEST are the phases that can block
        # before the action proceeds (tool dispatch / LLM call / a native
        # session's user prompt via the UserPromptSubmit hook — which has no
        # ASK primitive of its own, so the server resolves ASK here).
        if result.action == PolicyAction.ASK and phase in (
            Phase.TOOL_CALL,
            Phase.LLM_REQUEST,
            Phase.REQUEST,
        ):
            if is_read_only:
                # Read-only callers must not enter the ASK gate — parking
                # creates an elicitation (a server-side mutation). Return
                # the ASK verdict directly so the caller sees the policy
                # decision without mutating the session.
                pass
            else:
                # Serialize concurrent native ASK gates for this (session, policy)
                # so parallel tool calls that all trip the same checkpoint prompt
                # the human once. The first ASK to win the lock parks; on approve
                # it records a checkpoint. Siblings then rebuild the engine and
                # re-evaluate UNDER the lock against that freshly persisted state —
                # an ALLOW (or now-hard DENY) collapses the ASK and falls through
                # without a second prompt. Held across the human wait by design;
                # a declined ASK records nothing, so siblings legitimately re-ask.
                deciding_policy = result.deciding_policy
                assert deciding_policy is not None
                async with _native_ask_gate_lock(session_id, deciding_policy):
                    engine = await asyncio.to_thread(_build_engine)
                    result = await engine.evaluate(ctx, read_only=is_read_only)
                    if result.action == PolicyAction.ASK and phase in (
                        Phase.TOOL_CALL,
                        Phase.LLM_REQUEST,
                        Phase.REQUEST,
                    ):
                        try:
                            approved = await _hold_native_ask_gate(
                                request,
                                session_id=session_id,
                                phase=phase,
                                data=data,
                                engine=engine,
                                result=result,
                                conversation_store=conversation_store,
                                elicitation_id=hook_elicitation_id,
                            )
                        except ElicitationDeclinedError as exc:
                            # Explicit user decline: interrupt the native
                            # harness BEFORE returning the hook deny so the
                            # Escape key reaches Claude Code's tmux pane first.
                            # By the time the DENY response reaches the hook
                            # subprocess, the abort signal is already queued.
                            # Best-effort: forwarding failures are swallowed.
                            await _forward_session_change_to_runner(
                                session_id,
                                get_server_runner_router(),
                                {"type": "interrupt"},
                            )
                            decline_body = {
                                "result": "POLICY_ACTION_DENY",
                                "reason": exc.args[0] or "Approval was declined.",
                            }
                            add_audit_attrs(
                                policy_verdict="POLICY_ACTION_DENY",
                                policy_phase=phase.value,
                                policy_reason=decline_body["reason"],
                                policy_gate="declined",
                                policy_workspace_id=result.deciding_policy_workspace_id,
                            )
                            return Response(
                                content=json.dumps(decline_body),
                                media_type="application/json",
                            )
                        approval_body: dict[str, Any] = (
                            {"result": "POLICY_ACTION_ALLOW"}
                            if approved
                            else {
                                "result": "POLICY_ACTION_DENY",
                                "reason": result.reason or "Approval was not granted.",
                            }
                        )
                        add_audit_attrs(
                            policy_verdict=approval_body["result"],
                            policy_phase=phase.value,
                            policy_gate="ask",
                            policy_workspace_id=result.deciding_policy_workspace_id,
                        )
                        if approval_body.get("reason"):
                            add_audit_attrs(policy_reason=approval_body["reason"])
                        return Response(
                            content=json.dumps(approval_body),
                            media_type="application/json",
                        )
                # Re-evaluation collapsed the ASK (a sibling's approval recorded
                # the checkpoint) — fall through to the generic ALLOW/DENY handling
                # below with the rebuilt engine and updated result.

        # ALLOW and DENY already persisted their labels inside ``evaluate``; an
        # ASK on a non-blocking phase carries them unapplied.
        if result.action == PolicyAction.ASK and result.set_labels and not is_read_only:
            await asyncio.to_thread(engine.apply_label_writes, result.set_labels)

        resp_body: dict[str, Any] = {
            "result": _PHASE_TO_PROTO_ACTION.get(result.action, "POLICY_ACTION_UNSPECIFIED"),
        }
        if result.reason:
            resp_body["reason"] = result.reason
        if result.data is not None:
            resp_body["data"] = result.data
        # Tag the audit envelope with the decision so a DENY/ASK is debuggable
        # (a deny returns HTTP 200, so status alone can't tell you the verdict).
        add_audit_attrs(
            policy_verdict=resp_body["result"],
            policy_phase=phase.value,
            policy_workspace_id=result.deciding_policy_workspace_id,
        )
        if result.reason:
            add_audit_attrs(policy_reason=result.reason)
        # Emit a structured log for non-ALLOW verdicts so operators can diagnose
        # policy evaluation failures without needing audit-log access. The
        # workspace id is the deciding policy's owning workspace (None for a
        # YAML / agent-spec policy that is not a workspace-scoped row).
        if result.action in (PolicyAction.DENY, PolicyAction.ASK):
            _logger.info(
                "policy_eval_verdict: session=%s policy_workspace=%s phase=%s "
                "action=%s policy=%s reason=%r tool=%s",
                session_id,
                result.deciding_policy_workspace_id,
                phase.value,
                result.action.value,
                result.deciding_policy,
                result.reason,
                (data.get("name") if isinstance(data, dict) else None),
                extra={"session_id": session_id},
            )
        _policy_tool = data.get("name") if isinstance(data, dict) else None
        if _policy_tool:
            add_audit_attrs(policy_tool=_policy_tool)
        if result.deciding_policy is not None:
            add_audit_attrs(
                policy=getattr(result.deciding_policy, "name", None) or str(result.deciding_policy)
            )
        # A tool-call DENY is decided synchronously here, so nothing else on the
        # stream reflects that the native tool was blocked. Publish a positive
        # signal so observers (web UI, capability bench) see the decision rather
        # than infer it from the blocked tool's absence. Observational, so it is
        # not gated on write access.
        if result.action == PolicyAction.DENY and phase == Phase.TOOL_CALL:
            _publish_policy_denied(session_id, result.reason or "Blocked by policy.", phase.value)
        # An LLM_RESPONSE DENY reaches the harness only after the assistant
        # text already streamed through the runner relay, whose terminal
        # flush would persist it as a normal assistant message. Mark the
        # session so the relay substitutes the deny sentinel for the
        # buffered text instead (see the relay's terminal-flush handling).
        # Gated on write access: a read-only viewer's evaluate call must not
        # be able to poison the owner's in-flight turn. Also gated on an
        # active relay for this session — the marker only means something to
        # a relay flush, and skipping the write otherwise keeps background /
        # non-relayed evaluations from growing the unbounded marker dict
        # (its cleanup rides the relay's teardown).
        if result.action == PolicyAction.DENY and phase == Phase.LLM_RESPONSE and not is_read_only:
            _relay = _runner_relay_tasks.get(session_id)
            if _relay is not None and not _relay.task.done():
                _llm_response_denied_turns[session_id] = result.reason or "Denied by policy"
        return Response(
            content=json.dumps(resp_body),
            media_type="application/json",
        )

    async def _route_subagent_catalog(session_id: str) -> dict[str, list[str]] | None:
        """
        Fetch the session's live model catalog for subagent routing.

        :param session_id: Parent session/conversation id.
        :returns: Worker → servable model ids, or ``None`` when the runner
            is unreachable (callers fall back to the static table).
        """
        from omnigent.server.smart_routing import fetch_runner_models

        try:
            runner_client = await _get_runner_client(
                session_id, runner_router or get_server_runner_router()
            )
            if runner_client is None:
                return None
            return await fetch_runner_models(session_id, runner_client)
        except Exception:
            _logger.debug(
                "route-subagent: live catalog unavailable for session=%s",
                session_id,
                exc_info=True,
            )
            return None

    @router.post(
        "/sessions/{session_id}/hooks/route-subagent",
        # Internal runner relay — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(require_json_content_type)],
    )
    async def route_subagent_hook(
        request: Request,
        session_id: str,
    ) -> Response:
        """
        Decide the model/harness a native subagent spawn may use.

        The runner's loopback router (advertised to harness
        ``PreToolUse`` hooks via ``subagent_router.json``) relays here
        because ``RuntimeCaps.routing_client`` only lives in the server
        process. Request and response follow the frozen route-subagent
        contract; every routed verdict also lands as a
        ``routing_decision`` transcript item.

        The session's subagent-routing switch is two-state and re-read on
        every call (it is togglable mid-session): only an explicit ``"on"``
        routes, and every other session gets its spawn allowed unchanged
        without calling the router. Sessions that start on Smart Routing
        are stamped ``"on"`` at create, so nothing is inherited here.
        Candidate models stay inside the session's own harness family
        unless the session started in auto-harness mode.

        :param request: FastAPI request — body is the route-subagent
            request JSON.
        :param session_id: Parent session/conversation id from the path.
        :returns: The route-subagent decision as JSON.
        :raises OmnigentError: 400 when the body is not a JSON object or
            omits ``harness``.
        """
        from omnigent.runner.subagent_routing import (
            SubagentRouteDecision,
            SubagentRouteRequest,
            auto_harness_session,
            store_persister,
            subagent_routing_enabled,
        )
        from omnigent.server.smart_routing import AUTO_NATIVE_ROUTING_HARNESSES
        from omnigent.server.subagent_route_policy import resolve_subagent_route

        user_id = _get_user_id(request, auth_provider)
        # LEVEL_EDIT, like POST /events: a routed verdict mutates the session
        # (a persisted ``routing_decision`` item, and on the sibling route-turn
        # relay a ``model_override`` pin). A read-only viewer must not be able
        # to steer somebody else's spawns.
        await _require_access(
            user_id, session_id, LEVEL_EDIT, permission_store, conversation_store
        )
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise OmnigentError(
                f"Invalid JSON in route-subagent body: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                "route-subagent body must be a JSON object.",
                code=ErrorCode.INVALID_INPUT,
            )
        try:
            route_request = SubagentRouteRequest.from_payload(payload)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc

        # A relayed spawn is evidence the harness ran its routing hook, but it
        # is deliberately NOT turned into a clear here: the same warning code
        # also carries the spawn-audit verdict ("started on a model the router
        # never approved"), which a relay does not disprove — and every spawn
        # that produces such a verdict is itself relayed, so clearing here
        # wiped exactly the warnings the publisher had just raised (it only
        # re-posts on a transition, so the wipe was permanent). The publisher
        # owns the clear: its next check sees the canary and posts the repair.

        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        parent = None
        if conv is not None and conv.parent_conversation_id is not None:
            parent = await asyncio.to_thread(
                conversation_store.get_conversation, conv.parent_conversation_id
            )
        if conv is None or not subagent_routing_enabled(conv.subagent_routing_override):
            # Allowed unchanged, and deliberately not persisted: an
            # unrouted spawn is not a decision worth a transcript item.
            _logger.info(
                "route-subagent: subagent routing disabled for session=%s harness=%s",
                session_id,
                route_request.harness,
            )
            unrouted = SubagentRouteDecision(
                action="allow",
                rationale="subagent routing disabled for this session",
            )
            return Response(
                content=json.dumps(unrouted.to_payload()),
                media_type="application/json",
            )

        # Only a session started in auto-harness mode may be moved across
        # harness families; everyone else is offered their own family, so a
        # Claude Code session never gets a Codex suggestion.
        cross_harness = auto_harness_session(conv, parent)
        # Which families the spawn may land on decides which router can serve it:
        # off the AI Gateway the built-in judge answers, from the live catalog
        # alone (the static table's databricks-* ids are unreachable there).
        gateway_backed = await _spawn_gateway_backed(
            request,
            conv,
            (AUTO_NATIVE_ROUTING_HARNESSES if cross_harness else (route_request.harness,)),
        )
        # Offer the live catalog: the static table lags model generations, and
        # a pick the workspace serves must not look unservable and get
        # substituted down a tier.
        catalog = await _route_subagent_catalog(session_id)
        decision = await resolve_subagent_route(
            session_id,
            route_request,
            caps=get_caps(),
            catalog=catalog,
            cross_harness=cross_harness,
            gateway_backed=gateway_backed,
            allow_static_fallback=gateway_backed,
            persist=store_persister(session_id, conversation_store),
        )
        return Response(
            content=json.dumps(decision.to_payload()),
            media_type="application/json",
        )
