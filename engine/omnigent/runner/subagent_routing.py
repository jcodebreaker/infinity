"""Native-subagent routing: the runner's loopback relay and the shapes both halves share.

The claude-sdk executor's in-process ``PreToolUse`` hook (Claude ``Task``)
calls this before a native subagent spawn to ask which model the spawn should
run on. The gate is **advisory**: every transport failure, unreachable
endpoint, unparseable verdict or hook timeout allows the spawn unchanged,
because a spawn that dies on routing infrastructure is worse than a spawn
on the inherited model. Two halves live here:

* **Runner side** — a loopback HTTP relay (:func:`start_subagent_router`)
  on ``127.0.0.1:0``, bearer-token authenticated, advertised to the hook
  via ``subagent_router.json`` in the session bridge dir. Same
  rendezvous as ``tool_relay.json``.
* **Server side** — the policy
  (:func:`omnigent.server.subagent_route_policy.resolve_subagent_route`),
  which runs where ``RuntimeCaps.routing_client`` lives and persists every
  decision as a ``routing_decision`` transcript item.

The two halves are joined by the server relay route
:data:`SERVER_ROUTE_PATH` (registered in
``omnigent/server/routes/sessions/routes_hooks.py``); the runner's
handler forwards to it with :func:`make_server_relay_resolver`.

**Timeout budget.** Four hops wait on each other, so each one's budget is
strictly larger than the hop it waits on — otherwise an inner hop's
fail-open branch can never run. Canonical values, outermost first:

1. harness hook timeout — 12s (``HOOK_TIMEOUT_S`` for the in-process
   claude-sdk hook). Strictly larger than hop 2, never equal to it: a hook killed at the
   same instant its request gives up never reaches its fail-open branch.
2. hook HTTP request — :data:`HOOK_REQUEST_TIMEOUT_S` (8s, defined
   as ``REQUEST_TIMEOUT_S`` in ``omnigent.harnesses.hook_scripts.subagent_router``,
   which stays stdlib-only and cannot import this module)
3. runner loopback relay wait — :data:`RELAY_TIMEOUT_S` (7s)
4. server relay hop — :data:`SERVER_HOP_TIMEOUT_S` (6s), inside which the
   routing call itself runs on
   :data:`omnigent.server.smart_routing.ROUTING_REQUEST_TIMEOUT_S` (5s)

**The ladder is tight on purpose.** A spawn gate holds the parent agent's
tool call open until it answers, so a fail-open that takes 30 seconds stalls
the agent even though nothing errored. One second per step keeps a wedged
server to a visible pause while leaving every hop room for its own fail-open
branch.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from omnigent._platform import stable_user_id
from omnigent.models.model_fallbacks import ROUTING_HARNESS_FAMILY

if TYPE_CHECKING:
    import httpx

    from omnigent.entities.conversation import RoutingDecisionData

_logger = logging.getLogger(__name__)

#: Bridge-dir file that advertises the loopback endpoint to hook scripts.
ADVERTISEMENT_FILE = "subagent_router.json"

#: Path served by the loopback relay, per the frozen endpoint contract.
ROUTE_PATH_TEMPLATE = "/v1/sessions/{session_id}/route-subagent"

#: Server relay route the runner-side handler forwards to (the server
#: process is the only one holding ``RuntimeCaps.routing_client``).
SERVER_ROUTE_PATH = "/v1/sessions/{session_id}/hooks/route-subagent"

#: Seconds the runner's loopback relay waits for a verdict. Hop 3 of the
#: timeout budget in the module docstring.
RELAY_TIMEOUT_S = 14.0

#: Seconds the runner waits on the server relay route. Hop 4, inside which
#: the whole server-side route runs — candidate preparation and then the
#: routing call (``ROUTING_REQUEST_TIMEOUT_S``), not the call alone.
SERVER_HOP_TIMEOUT_S = 13.0

#: Hop 2 of the budget, restated for the docstring cross-reference. The hook
#: script owns the value; it cannot import this module (stdlib-only). Sized
#: like the first-message hook: it must outlast a healthy route, preparation
#: included, or a verdict that arrived is thrown away.
HOOK_REQUEST_TIMEOUT_S = 15.0

#: Conversation label carrying the routing decision behind a session's
#: ``model_override``, so the child-sessions API can join the two without
#: a new column or a transcript scan.
ROUTING_DECISION_LABEL_KEY = "omnigent.routing.decision_id"

#: Conversation label marking a session created in auto-harness mode. The
#: ``harness_override`` sentinel is replaced the moment first-message routing
#: resolves a harness, so this label is the durable record that the router
#: may still move this session's subagents across harness families.
AUTO_HARNESS_LABEL_KEY = "omnigent.routing.auto_harness"

#: Agent-authored, so bounded before it is stored or echoed anywhere.
_TASK_NAME_CAP = 200
#: Ledger entries kept per session; a long-running session can spawn
#: without limit, so the oldest verdicts are dropped.
_RELAYED_CAP = 500

# Harnesses whose in-process spawn hook reads a router advertisement. Every
# other harness gets no router env vars at all.
_CLAUDE_HOOK_HARNESSES = frozenset({"claude-sdk", "claude_sdk"})

Resolver = Callable[[str, "SubagentRouteRequest"], Awaitable["SubagentRouteDecision"]]


async def _awaited(pending: Awaitable[SubagentRouteDecision]) -> SubagentRouteDecision:
    """Wrap a resolver's awaitable as a coroutine for the handler thread.

    ``asyncio.run_coroutine_threadsafe`` accepts coroutines only, while a
    :data:`Resolver` may return any awaitable.

    :param pending: The resolver's awaitable result.
    :returns: The resolved decision.
    """
    return await pending


# ── Enablement gate ────────────────────────────────────────────────────────


def routing_enabled(
    cost_control_mode: str | None,
    *,
    parent_cost_control_mode: str | None = None,
) -> bool:
    """Report whether smart routing is on for one session.

    The per-session toggle: its own, or its parent's for a spawned child (the
    server routes children of a routed parent). Subagent spawns read
    :func:`subagent_routing_enabled`, an independent two-state switch stamped
    at create.

    :param cost_control_mode: The session's ``cost_control_mode_override``,
        e.g. ``"on"``.
    :param parent_cost_control_mode: The parent session's value for a
        spawned child. ``None`` for a top-level session.
    :returns: ``True`` when routing applies to this session.
    """
    return cost_control_mode == "on" or parent_cost_control_mode == "on"


@dataclass(frozen=True)
class SessionRoutingClass:
    """Which Smart Routing additions one session is entitled to.

    Three classes, and the codex paths treat them differently (the product
    ruling): a **plain** session (both flags false) must be
    byte-for-byte a plain codex session — no catalog replacement, no
    spawn-routing hooks, no extra tool pre-approvals. A **pinned**
    Smart Routing session (``routing_enabled`` only) adds the extended
    model catalog, because a routed turn can land on an arm codex's
    bundled catalog has no entry for, plus — on a native terminal — the
    spawn-routing gate and the tool pre-approvals its routed spawns need
    to run at all. An **auto-harness** session (both) additionally lets
    those spawns cross into the counterpart harness family; a pinned one
    routes within its own.

    :param routing_enabled: The session launched with Smart Routing as its
        model.
    :param auto_harness: The session also let Smart Routing pick the
        harness, so the router may move its spawns across families.
    """

    routing_enabled: bool = False
    auto_harness: bool = False


#: The class a session with no recorded routing state is treated as. Read by
#: the codex launch paths, so "unknown" has to mean "plain".
PLAIN_SESSION = SessionRoutingClass()


def routing_class_from_snapshot(
    *,
    cost_control_mode: str | None,
    harness_override: str | None,
    labels: Mapping[str, str] | None,
) -> SessionRoutingClass:
    """Derive a session's routing class from its persisted state.

    The one reader of the three fields that carry it, so the runner-side
    launch paths agree on what "pinned" and "auto-harness" mean.

    :param cost_control_mode: ``cost_control_mode_override``, e.g. ``"on"``.
    :param harness_override: ``harness_override``; the ``"auto"`` sentinel
        marks auto-harness until first-message routing replaces it.
    :param labels: Conversation labels, which carry the durable
        :data:`AUTO_HARNESS_LABEL_KEY` record and the
        :data:`ROUTING_DECISION_LABEL_KEY` a completed route stamps.
    :returns: The session's class.
    """
    auto = harness_override == "auto" or (
        labels is not None and labels.get(AUTO_HARNESS_LABEL_KEY) == "1"
    )
    # An auto-harness session is a Smart Routing session by construction, so
    # the label implies routing is on even for a row whose cost-control field
    # was never stamped.
    routes = routing_enabled(cost_control_mode) or auto
    return SessionRoutingClass(routing_enabled=routes, auto_harness=auto)


def subagent_routing_enabled(subagent_routing_override: str | None) -> bool:
    """Report whether subagent spawns are routed for one session.

    Two-state: ``"on"`` routes spawns, and anything else leaves them to
    the harness — ``"off"``, unset, and a legacy row still carrying the
    old tri-state ``"inherit"`` all read the same, so no stored value
    has to be rewritten for the gate to be sound. Sessions that start on
    Smart Routing are stamped ``"on"`` at create, so unset genuinely
    means Default rather than "ask somewhere else". Read per spawn (not
    at launch) so a mid-session flip takes effect on the next spawn.

    :param subagent_routing_override: The session's
        ``subagent_routing_override`` — ``"on"``, ``"off"``, or ``None``.
        Legacy rows may hold any other string; only ``"on"`` enables.
    :returns: ``True`` when subagent spawns should be routed.
    """
    return subagent_routing_override == "on"


# ── Wire types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubagentRouteRequest:
    """One native-subagent spawn awaiting a routing verdict.

    :param harness: Requesting harness id, e.g. ``"claude-native"``.
    :param task_name: Subagent type / task name from the spawn payload,
        e.g. ``"code-reviewer"``.
    :param task_description: The spawn's human task label (Claude's
        ``Task`` ``description``, e.g. ``"Research auth flows"``) — what
        distinguishes same-typed spawns in a fan-out. ``None`` when the
        harness's spawn payload names none.
    :param prompt: Raw task text. ``None`` on codex, whose spawn message
        is encrypted in hook payloads.
    :param fork: ``True`` when the spawn is a fork of the parent session.
    :param parent_model: Model the parent session runs on, e.g.
        ``"databricks-claude-sonnet-4-6"``.
    :param requested_model: Model the spawning agent explicitly asked for
        in the spawn arguments, if any. Never bypasses the router: the
        router always decides, the ask is called honored only when the
        pick names the same arm, and a mismatch is recorded as
        ``attempted_override``.
    """

    harness: str
    task_name: str = ""
    task_description: str | None = None
    prompt: str | None = None
    fork: bool = False
    parent_model: str | None = None
    requested_model: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubagentRouteRequest:
        """Parse a request body into a :class:`SubagentRouteRequest`.

        :param payload: Decoded JSON object from the hook script.
        :returns: Parsed request.
        :raises ValueError: If ``harness`` is missing or not a string.
        """
        harness = payload.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise ValueError("route-subagent body requires a non-empty 'harness' string")
        task_name = payload.get("task_name")
        task_description = payload.get("task_description")
        prompt = payload.get("prompt")
        parent_model = payload.get("parent_model")
        requested_model = payload.get("requested_model")
        return cls(
            harness=harness.strip(),
            task_name=task_name[:_TASK_NAME_CAP] if isinstance(task_name, str) else "",
            # Agent-authored like task_name, so bounded the same way before
            # it is stored or rendered.
            task_description=(
                task_description.strip()[:_TASK_NAME_CAP].rstrip() or None
                if isinstance(task_description, str)
                else None
            ),
            prompt=prompt if isinstance(prompt, str) and prompt else None,
            fork=bool(payload.get("fork")),
            parent_model=parent_model if isinstance(parent_model, str) and parent_model else None,
            requested_model=(
                requested_model if isinstance(requested_model, str) and requested_model else None
            ),
        )


@dataclass(frozen=True)
class SubagentRouteDecision:
    """The verdict a hook script enforces on a spawn.

    ``model`` is always a servable catalog id (e.g.
    ``"databricks-claude-sonnet-5"``) — never a harness's own tool
    vocabulary. Translating it is the harness hook's job: Claude Code's
    Agent/Task ``model`` parameter, for instance, only accepts the tier
    aliases ``sonnet``/``opus``/``haiku``/``fable`` and rejects a catalog id
    outright, so its hook inverse-maps before rewriting ``updatedInput``.

    :param action: ``"allow"`` (spawn unchanged), ``"rewrite"`` (same
        harness, injected model), ``"redirect"`` (cross-harness — deny
        and tell the model to use ``sys_session_send``) or ``"deny"``.
    :param model: Servable model id; set for rewrite/redirect.
    :param harness: Target harness; set for redirect.
    :param raw_model: Router-vocabulary pick before resolution.
    :param rationale: One-line explanation, surfaced to the model and UI.
    :param decision_id: Identity shared by the response and the transcript
        item.
    :param router_source: Which router answered — ``"databricks-aigw"`` or
        ``"oss-llm"``. ``None`` when nothing was routed (a fail-open allow) or
        on a payload written before the field existed.
    """

    action: Literal["allow", "rewrite", "redirect", "deny"]
    rationale: str
    model: str | None = None
    harness: str | None = None
    raw_model: str | None = None
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    router_source: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the frozen response shape.

        :returns: JSON-ready response body.
        """
        return {
            "action": self.action,
            "model": self.model,
            "harness": self.harness,
            "raw_model": self.raw_model,
            "rationale": self.rationale,
            "decision_id": self.decision_id,
            "router_source": self.router_source,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SubagentRouteDecision:
        """Parse a response body (used by the runner-side relay).

        :param payload: Decoded JSON response from the server route.
        :returns: Parsed decision; unknown actions degrade to ``allow``.
        """
        action = payload.get("action")
        if action not in ("allow", "rewrite", "redirect", "deny"):
            action = "allow"
        rationale = payload.get("rationale")
        decision_id = payload.get("decision_id")
        return cls(
            action=action,
            rationale=rationale if isinstance(rationale, str) else "",
            model=_opt_str(payload.get("model")),
            harness=_opt_str(payload.get("harness")),
            raw_model=_opt_str(payload.get("raw_model")),
            decision_id=decision_id if isinstance(decision_id, str) else str(uuid.uuid4()),
            router_source=_opt_str(payload.get("router_source")),
        )


# ── Policy ─────────────────────────────────────────────────────────────────


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def harness_family(harness: str | None) -> str | None:
    """Return the model family *harness* belongs to.

    The single family lookup shared by the hook path (native subagent
    spawns) and the omnigent child-session path, so "in-family" means the
    same thing for both.

    :param harness: Harness id, e.g. ``"codex-native"``. ``None`` or the
        unresolved ``"auto"`` sentinel carry no family.
    :returns: ``"claude"``, ``"gpt"``, ``"pi"``, or ``None`` when the
        harness is unknown / multi-family.
    """
    if harness is None or harness == "auto":
        return None
    return ROUTING_HARNESS_FAMILY.get(harness)


def auto_harness_session(conv: Any, parent: Any = None) -> bool:
    """Report whether a session may cross harness families.

    True only for a session started in Smart Routing (auto) harness mode,
    or a child of one: those are the sessions whose harness the router
    owns. Everyone else is pinned to the family they started on, so a
    codex session never gets Claude children and vice versa.

    :param conv: Conversation row for the session, or ``None``.
    :param parent: Conversation row for its parent, when known. ``None``
        for a top-level session or when the parent was not loaded.
    :returns: ``True`` when cross-family picks are allowed.
    """
    for row in (conv, parent):
        if row is None:
            continue
        if getattr(row, "labels", None) and row.labels.get(AUTO_HARNESS_LABEL_KEY) == "1":
            return True
        if getattr(row, "harness_override", None) == "auto":
            return True
    return False


def model_in_family(family: str | None, model: str) -> bool:
    """Report whether *model* can run on a harness in *family*.

    The ``"gpt"`` family means codex-compatible, not literally GPT: it
    admits the GLM and Kimi ids codex serves over the same Responses wire.

    :param family: Family from :func:`harness_family`. ``None`` (unknown
        or multi-family harness) accepts every model.
    :param model: Model id, e.g. ``"databricks-gpt-5-5"``.
    :returns: ``True`` when the pairing is servable.
    """
    if family is None or family == "pi":
        return True
    from omnigent.models.model_catalog import model_family_token

    token = model_family_token(model)
    return token == "claude" if family == "claude" else token == "openai"


def _unavailable_decision(reason: str) -> SubagentRouteDecision:
    """Allow the spawn unchanged and say why nothing was routed."""
    return SubagentRouteDecision(
        action="allow",
        rationale=f"Routing unavailable ({reason}); spawn allowed unchanged",
    )


# ── Transcript persistence ─────────────────────────────────────────────────


async def persist_subagent_decision(
    session_id: str,
    conversation_store: Any,
    record: RoutingDecisionData,
) -> None:
    """Persist and publish *record* as a ``routing_decision`` item.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``append``.
    :param record: Decision payload.
    """
    from omnigent.entities.conversation import NewConversationItem
    from omnigent.runtime import session_stream

    item_data = record.model_dump()
    item = NewConversationItem(
        type="routing_decision",
        response_id=f"routing_{uuid.uuid4().hex}",
        data=record,
    )
    try:
        persisted = await asyncio.to_thread(conversation_store.append, session_id, [item])
        persisted_id: str | None = persisted[0].id if persisted else None
    except Exception:
        _logger.exception(
            "route-subagent: routing_decision persist failed for session=%s", session_id
        )
        persisted_id = None

    session_stream.publish(
        session_id,
        {
            "type": "response.output_item.done",
            "item": {"id": persisted_id, "type": "routing_decision", **item_data},
        },
    )


def store_persister(
    session_id: str,
    conversation_store: Any,
) -> Callable[[RoutingDecisionData], Awaitable[None]]:
    """Bind :func:`persist_subagent_decision` to a session and store.

    :param session_id: Session/conversation identifier.
    :param conversation_store: Store exposing ``append``.
    :returns: Coroutine function accepting a record.
    """

    async def _persist(record: RoutingDecisionData) -> None:
        await persist_subagent_decision(session_id, conversation_store, record)

    return _persist


# ── Runner-side loopback relay ─────────────────────────────────────────────


def write_advertisement(
    bridge_dir: Path,
    *,
    url: str,
    token: str,
    session_id: str | None = None,
) -> Path:
    """Advertise the endpoint to hook scripts.

    :param bridge_dir: Session bridge directory.
    :param url: Endpoint base URL, e.g. ``"http://127.0.0.1:53421"``.
    :param token: Bearer token hook scripts must present.
    :param session_id: Session the endpoint serves. Included so a hook
        with no session env var of its own still knows which session to
        route for.
    :returns: Path of the written advertisement.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / ADVERTISEMENT_FILE
    payload: dict[str, Any] = {
        "url": url,
        "token": token,
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    if session_id is not None:
        payload["session_id"] = session_id
    # The file carries a bearer token, so it is never world-readable — not even
    # for the instant between the write and a follow-up chmod. A unique temp
    # name also keeps two concurrent re-advertisements from clobbering each
    # other's partial file.
    fd, tmp_name = tempfile.mkstemp(dir=bridge_dir, prefix=f"{ADVERTISEMENT_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(payload))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


@dataclass
class SubagentRouter:
    """Handle for the running loopback router.

    :param bridge_dir: Bridge dir holding the advertisement file.
    :param url: Base URL the hook scripts POST to.
    :param token: Bearer token hook scripts present.
    :param httpd: The backing HTTP server.
    """

    bridge_dir: Path
    url: str
    token: str
    httpd: ThreadingHTTPServer
    advertised_dirs: set[Path] = field(default_factory=set)
    _closed: bool = False

    def advertise(self, bridge_dir: Path, session_id: str | None = None) -> Path:
        """Write this router's advertisement into *bridge_dir* and track it.

        :param bridge_dir: Directory to advertise in.
        :param session_id: Session the endpoint serves.
        :returns: Path of the written advertisement.
        """
        path = write_advertisement(
            bridge_dir, url=self.url, token=self.token, session_id=session_id
        )
        self.advertised_dirs.add(bridge_dir)
        return path

    def close(self) -> None:
        """Stop the server and remove the advertisements it still owns.

        Idempotent and safe to call from any thread, so a session-close path
        can call it without knowing whether an earlier one already did.
        Sessions that fork/clear/resume keep the same bridge dir, so a newer
        router may have overwritten an advertisement with its own url;
        unlinking unconditionally would kill that live router's routing. Only
        files still naming this router's url are removed.
        """
        if self._closed:
            return
        self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        for bridge_dir in (*self.advertised_dirs, self.bridge_dir):
            path = bridge_dir / ADVERTISEMENT_FILE
            try:
                advertised = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(advertised, dict) and advertised.get("url") == self.url:
                with contextlib.suppress(OSError):
                    path.unlink()


def start_subagent_router(
    *,
    bridge_dir: Path,
    session_id: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float = RELAY_TIMEOUT_S,
) -> SubagentRouter:
    """Start the loopback router and advertise it in *bridge_dir*.

    :param bridge_dir: Session bridge directory.
    :param session_id: Session this router serves; requests for any
        other session id are rejected.
    :param resolver: Coroutine function returning a decision.
    :param loop: Event loop that owns *resolver*.
    :param request_timeout_s: Seconds to wait for a verdict before allowing
        the spawn unchanged. Hop 3 of the module's timeout budget.
    :returns: Started router handle; call :meth:`SubagentRouter.close`
        when the session ends.
    """
    token = secrets.token_urlsafe(32)
    handler_cls = _handler_factory(session_id, token, resolver, loop, request_timeout_s)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    # Handler threads must not hold up ``close()``: the default joins every
    # in-flight request (up to the relay timeout), which stalls session
    # teardown and widens the window for a relaunch to race the shutdown.
    httpd.daemon_threads = True
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}"
    router = SubagentRouter(bridge_dir=bridge_dir, url=url, token=token, httpd=httpd)
    router.advertise(bridge_dir, session_id)
    threading.Thread(
        target=httpd.serve_forever,
        name="omnigent-subagent-router",
        daemon=True,
    ).start()
    return router


def _handler_factory(
    session_id: str,
    token: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class for one session's router."""
    expected_path = ROUTE_PATH_TEMPLATE.format(session_id=session_id)
    # Compared as bytes: ``compare_digest`` raises TypeError on a non-ASCII
    # str, and http.server decodes headers as latin-1, so a request with a
    # non-ASCII Authorization byte would crash the handler instead of 401ing.
    expected_auth = f"Bearer {token}".encode()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            presented = (self.headers.get("Authorization") or "").encode("latin-1", "replace")
            if not secrets.compare_digest(presented, expected_auth):
                self._reject(401, "unauthorized")
                return
            if self.path.rstrip("/") != expected_path:
                self._reject(404, "not found")
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                req = SubagentRouteRequest.from_payload(payload)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _awaited(resolver(session_id, req)), loop
                )
                decision = future.result(timeout=request_timeout_s)
            except Exception:
                _logger.warning(
                    "route-subagent: resolver failed for session=%s", session_id, exc_info=True
                )
                decision = _unavailable_decision("routing endpoint failed")
            self._send(200, decision.to_payload())

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _reject(self, status: int, error: str) -> None:
            # Keep-alive is on (HTTP/1.1), so an undrained body would be parsed
            # as the next request's start line. Drain, then close the connection.
            with contextlib.suppress(OSError, ValueError):
                self._read_body()
            self.close_connection = True
            self._send(status, {"error": error}, close=True)

        def _send(self, status: int, body: dict[str, Any], *, close: bool = False) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            _logger.debug("subagent-router: " + format, *args, extra={"session_id": session_id})

    return _Handler


def make_server_relay_resolver(
    server_client: Any,
    *,
    timeout_s: float = SERVER_HOP_TIMEOUT_S,
) -> Resolver:
    """Build a resolver that forwards to the server's relay route.

    :param server_client: Async HTTP client pointed at the AP server.
    :param timeout_s: Per-request timeout in seconds. Hop 4 (innermost) of
        the module's timeout budget.
    :returns: Resolver for :func:`start_subagent_router`.
    """

    async def _resolve(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        body = {
            "harness": req.harness,
            "task_name": req.task_name,
            "task_description": req.task_description,
            "prompt": req.prompt,
            "fork": req.fork,
            "parent_model": req.parent_model,
            "requested_model": req.requested_model,
        }
        try:
            resp = await server_client.post(
                SERVER_ROUTE_PATH.format(session_id=session_id),
                json=body,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            _logger.warning(
                "route-subagent: server relay failed for session=%s", session_id, exc_info=True
            )
            return _unavailable_decision("routing server unreachable")
        if not isinstance(payload, dict):
            return _unavailable_decision("unreadable verdict from routing server")
        return SubagentRouteDecision.from_payload(payload)

    return _resolve


# ── Per-session router lifecycle (runner side) ─────────────────────────────

_session_routers: dict[str, SubagentRouter] = {}
# Models the relay actually handed back, per session — the ledger the
# codex ``SubagentStart`` audit is reconciled against.
_relayed: dict[str, list[dict[str, Any]]] = {}
# Routing class per session, stamped at session init. The SDK harness paths
# build their spawn env long after the init envelope is gone, so the class has
# to outlive it.
_session_routing_classes: dict[str, SessionRoutingClass] = {}
_lifecycle_lock = threading.Lock()


def remember_session_routing_class(session_id: str, routing_class: SessionRoutingClass) -> None:
    """Record which Smart Routing additions *session_id* is entitled to.

    :param session_id: Session/conversation identifier.
    :param routing_class: The class derived from the session's snapshot.
    """
    with _lifecycle_lock:
        _session_routing_classes[session_id] = routing_class


def session_routing_class(session_id: str) -> SessionRoutingClass:
    """Return the recorded routing class for *session_id*.

    :param session_id: Session/conversation identifier.
    :returns: The recorded class, or :data:`PLAIN_SESSION` when none was
        recorded — an unknown session must not pay any routing-path cost.
    """
    with _lifecycle_lock:
        return _session_routing_classes.get(session_id, PLAIN_SESSION)


def forget_session_routing_class(session_id: str) -> None:
    """Drop the recorded routing class for a finished session.

    :param session_id: Session/conversation identifier.
    """
    with _lifecycle_lock:
        _session_routing_classes.pop(session_id, None)


def relayed_decisions(session_id: str) -> tuple[dict[str, Any], ...]:
    """Return the verdicts this runner relayed for *session_id*.

    :param session_id: Session/conversation identifier.
    :returns: Records ``{decision_id, action, model, harness, task_name}``,
        oldest first.
    """
    with _lifecycle_lock:
        return tuple(_relayed.get(session_id, ()))


def routed_models(session_id: str) -> frozenset[str]:
    """Return every model the router approved for *session_id*.

    Session-wide view of the ledger; the codex spawn audit reconciles
    against :func:`relayed_decisions` instead, so it can join per spawn.

    :param session_id: Session/conversation identifier.
    :returns: Approved model ids; empty when nothing was routed.
    """
    return frozenset(
        str(record["model"])
        for record in relayed_decisions(session_id)
        if record.get("model") and record.get("action") in ("rewrite", "allow")
    )


def _recording_resolver(resolver: Resolver) -> Resolver:
    """Wrap *resolver* so each verdict lands in the reconciliation ledger."""

    async def _resolve(session_id: str, req: SubagentRouteRequest) -> SubagentRouteDecision:
        decision = await resolver(session_id, req)
        with _lifecycle_lock:
            ledger = _relayed.setdefault(session_id, [])
            ledger.append(
                {
                    "decision_id": decision.decision_id,
                    "action": decision.action,
                    "model": decision.model,
                    "harness": decision.harness or req.harness,
                    "task_name": req.task_name[:_TASK_NAME_CAP],
                }
            )
            if len(ledger) > _RELAYED_CAP:
                del ledger[: len(ledger) - _RELAYED_CAP]
        return decision

    return _resolve


def ensure_session_router(
    session_id: str,
    *,
    bridge_dir: Path,
    server_client: httpx.AsyncClient,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SubagentRouter:
    """Start (once) the loopback router serving *session_id*.

    Idempotent: a second call for a session already served returns the
    running router after re-asserting its advertisement, so a resumed or
    re-launched terminal finds the file the hook scripts read. The whole
    start runs under ``_lifecycle_lock`` — releasing it between the lookup
    and the insert let two concurrent launches each bind a socket, and the
    loser was never closed.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Directory the advertisement is written into — the
        same directory the harness's hooks are pointed at.
    :param server_client: Runner→server client used to relay verdicts.
    :param loop: Event loop owning the relay. ``None`` uses the running
        loop.
    :returns: The running router handle.
    """
    resolver_loop = loop if loop is not None else asyncio.get_running_loop()
    with _lifecycle_lock:
        existing = _session_routers.get(session_id)
        if existing is not None:
            existing.advertise(bridge_dir, session_id)
            return existing
        router = start_subagent_router(
            bridge_dir=bridge_dir,
            session_id=session_id,
            resolver=_recording_resolver(make_server_relay_resolver(server_client)),
            loop=resolver_loop,
        )
        _session_routers[session_id] = router
    # Nothing from the rendezvous is logged — neither the URL nor the paths
    # and ids that reach it — so a log file can never carry the bearer token
    # or the identifiers that address it. The advertisement on disk names both.
    _logger.info("subagent router started", extra={"session_id": session_id})
    return router


def ensure_session_router_quietly(
    session_id: str,
    *,
    bridge_dir: Path | None = None,
    server_client: httpx.AsyncClient | None,
    harness: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    caps: Any = None,
    routing_class: SessionRoutingClass = PLAIN_SESSION,
) -> SubagentRouter | None:
    """Start the session's router, or return ``None`` instead of failing.

    The launch-site wrapper: a session with no server client has nowhere to
    relay verdicts to, and neither a router that cannot bind a socket nor a
    bridge root that fails its ownership check may take the harness launch
    down with it. Only the claude-sdk harness, whose in-process spawn hook
    reads the advertisement, gets a router at all — every other harness is
    handed no router env vars, so starting one for it would only risk
    failing its session init.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Directory the advertisement is written into.
        ``None`` resolves the session's private router dir, inside the
        failure guard.
    :param server_client: Runner→server client. ``None`` skips the router.
    :param harness: Harness being launched; gates the start and is logged
        on failure.
    :param loop: Event loop owning the relay. ``None`` uses the running
        loop.
    :param caps: Accepted and ignored; kept so launch sites can pass the
        ``RuntimeCaps`` they already hold.
    :param routing_class: The session's Smart Routing class, which decides
        whether it gets an endpoint at all. A **plain** session gets none:
        the loopback server, its bearer token on disk and the per-spawn hook
        round trip are Smart Routing's costs to pay, not a plain session's.
        Every **routed** session gets one, pinned or auto-harness. Stamped
        at create, so flipping the gear's subagent-routing toggle on for a plain session
        stays inert until it is recreated.
    :returns: The running router handle, or ``None`` when it could not
        start.
    """
    del caps
    if server_client is None:
        return None
    if harness not in _CLAUDE_HOOK_HARNESSES:
        return None
    if not routing_class.routing_enabled:
        return None
    try:
        return ensure_session_router(
            session_id,
            bridge_dir=bridge_dir
            if bridge_dir is not None
            else router_dir_for_session(session_id),
            server_client=server_client,
            loop=loop,
        )
    except (OSError, RuntimeError):
        # ``router_dir_for_session`` raises RuntimeError when the shared
        # bridge root fails its ownership check — never a reason to fail
        # session creation for a harness that may not even use routing.
        _logger.warning(
            "subagent router could not start for session=%s harness=%s",
            session_id,
            harness,
            exc_info=True,
        )
        return None


def shutdown_session_router(session_id: str, router: SubagentRouter | None = None) -> None:
    """Stop the router serving *session_id* and forget its state.

    Cheap and safe to call for a session that never had a router, and safe
    to call twice — :meth:`SubagentRouter.close` is idempotent — so every
    teardown path can call it unconditionally.

    :param session_id: Session/conversation identifier.
    :param router: The handle the caller started. When given, the teardown
        is identity-scoped: a delayed teardown from a previous launch of
        the same session must not close the router a re-create has since
        installed, which would leave routing silently dead behind a
        still-valid advertisement. ``None`` tears down whatever is
        registered (session-delete paths that hold no handle).
    """
    with _lifecycle_lock:
        current = _session_routers.get(session_id)
        if router is not None and current is not router:
            return
        _session_routers.pop(session_id, None)
        _relayed.pop(session_id, None)
    if current is None:
        return
    router = current
    with contextlib.suppress(Exception):
        router.close()
    _prune_router_dirs(router)


def subagent_router_root() -> Path:
    """Root for the subagent router's per-session advertisement directories.

    Namespaced by uid under the temp dir (``$TMPDIR/omnigent-<uid>/subagent-router``)
    so other Unix users on the host cannot read the bearer token or pre-create
    the tree as a symlink — see :func:`_ensure_owner_only_dir`.

    :returns: The subagent-router root directory (not created here).
    """
    return Path(tempfile.gettempdir()) / f"omnigent-{stable_user_id()}" / "subagent-router"


def _ensure_owner_only_dir(target: Path) -> None:
    """Create or validate *target* as an owner-only directory chain.

    ``Path.mkdir(mode=0o700, parents=True)`` only applies the mode to the leaf
    and silently trusts any pre-existing ancestor. Walk every ancestor below the
    temp dir instead: create missing ones ``0o700`` and reject a symlink, a
    non-directory, or a directory owned by another uid; reset a repairable
    group/other mode on a directory we own. On Windows, where Python exposes no
    POSIX uid/mode model, protection relies on the OS ACLs.

    :param target: Directory to ensure, below the temp dir.
    :raises RuntimeError: If *target* is not below the temp dir or an ancestor
        fails validation.
    """
    trusted_parent = Path(os.path.abspath(tempfile.gettempdir()))
    target = Path(os.path.abspath(os.fspath(target)))
    if not target.is_relative_to(trusted_parent) or target == trusted_parent:
        raise RuntimeError(f"router dir {target!s} is not under {trusted_parent!s}")
    getuid = getattr(os, "getuid", None)
    my_uid = getuid() if getuid is not None else None
    for ancestor in reversed([target, *target.parents]):
        if not ancestor.is_relative_to(trusted_parent) or ancestor == trusted_parent:
            continue
        try:
            os.mkdir(ancestor, mode=0o700)
            continue
        except FileExistsError:
            pass
        st = os.lstat(ancestor)
        if stat.S_ISLNK(st.st_mode):
            raise RuntimeError(f"refusing to use {ancestor!s}: is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"refusing to use {ancestor!s}: not a directory")
        if my_uid is not None and st.st_uid != my_uid:
            raise RuntimeError(
                f"refusing to use {ancestor!s}: owned by uid {st.st_uid}, "
                f"not current user ({my_uid})"
            )
        if my_uid is not None and (st.st_mode & 0o077) != 0:
            os.chmod(ancestor, 0o700)


def _prune_router_dirs(router: SubagentRouter) -> None:
    """Remove the router's own (now empty) advertisement dirs.

    Only dirs under the subagent-router root are removed; any other advertised
    dir belongs to its owner, not to this router.
    """
    root = subagent_router_root()
    for bridge_dir in (*router.advertised_dirs, router.bridge_dir):
        # Strictly below the root: the root itself is shared by every session.
        if bridge_dir != root and bridge_dir.is_relative_to(root):
            with contextlib.suppress(OSError):
                bridge_dir.rmdir()


def router_dir_for_session(session_id: str) -> Path:
    """Return the advertisement directory for a session with no bridge dir.

    The claude-agent-sdk harness has no bridge directory of its own, so the
    router gets a private owner-only one. Created through
    :func:`_ensure_owner_only_dir`: the advertisement holds a bearer token, and
    ``mkdir(mode=0o700, parents=True)`` would trust a pre-created
    world-writable or symlinked ancestor.

    :param session_id: Session/conversation identifier.
    :returns: Created directory, mode ``0o700``.
    :raises RuntimeError: If an ancestor fails the ownership check.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    path = subagent_router_root() / digest
    _ensure_owner_only_dir(path)
    return path


def session_router_env(session_id: str, harness: str | None = None) -> dict[str, str]:
    """Return the router env for a session, or ``{}`` when it has no router.

    Read at harness-spawn time so a session whose router was started at
    session init hands every harness process the same rendezvous.

    :param session_id: Session/conversation identifier.
    :param harness: Harness being launched, so only its own vars are set.
    :returns: Env-var overrides, empty when the session has no router.
    """
    with _lifecycle_lock:
        router = _session_routers.get(session_id)
    if router is None:
        return {}
    return router_env(session_id, router.bridge_dir, harness=harness)


def router_env(session_id: str, router_dir: Path, harness: str | None = None) -> dict[str, str]:
    """Build the env that points a harness process at the router.

    The claude-agent-sdk executor reads the advertisement out of the
    directory named in its env var and registers its in-process hook from it.

    :param session_id: Session/conversation identifier.
    :param router_dir: Directory holding ``subagent_router.json``.
    :param harness: Harness being launched. ``None`` sets the vars anyway,
        for callers that cannot name the harness.
    :returns: Env-var overrides for the harness process; empty for a
        harness with no routing hook.
    """
    from omnigent.harnesses.hook_scripts.subagent_router import (
        ROUTER_DIR_ENV_VAR,
        SESSION_ID_ENV_VAR,
    )

    if harness is not None and harness not in _CLAUDE_HOOK_HARNESSES:
        return {}
    return {ROUTER_DIR_ENV_VAR: str(router_dir), SESSION_ID_ENV_VAR: session_id}
