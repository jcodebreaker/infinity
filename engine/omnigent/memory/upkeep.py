"""``docs/context.md``: the memory upkeep job.

The self-improvement loop: window -> gate -> extract -> verify -> apply ->
record (section 4). Pure pipeline logic lives here;
``omnigent/server/memory_upkeep.py`` wires it to the server's background
scheduling and the compaction trigger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from omnigent.context.attachments import strip_legacy_attachment_context
from omnigent.context.labels import (
    SCHEDULED_FIRE_LABEL_KEY,
    SCHEDULED_HELPER_LABEL_KEY,
    SUBAGENT_DISPATCH_ID_LABEL_KEY,
)
from omnigent.db.utils import now_epoch
from omnigent.entities import MemoryEvidenceLink, MemoryUpkeepRun
from omnigent.entities.conversation import is_system_notice
from omnigent.memory.service import INFERRED_CONFIDENCE, STATED_CONFIDENCE, VALID_KINDS
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_upkeep_store import MemoryUpkeepStore

_logger = logging.getLogger(__name__)

# ── configurable thresholds (section 6 / "make the interval/thresholds
# configurable constants") ──────────────────────────────────────────────

#: Gate: fewer substantive new user messages than this skips the run
#: (``disposition: "no_new_signal"``), and the watermark does not advance.
GATE_MIN_USER_MESSAGES = 3

#: Window scan bounds — a reference-implementation safety valve, not a
#: correctness requirement: a user with more unread sessions/messages than
#: this just needs another sweep to catch up (the watermark never skips
#: anything, it only moves as far as this run actually read).
WINDOW_SESSIONS_LIMIT = 200
# One extraction call per run: keep the newest messages within this budget
# (~30k tokens) and clip each one, so a long backlog can't overflow the model.
WINDOW_MAX_CHARS = 120_000
WINDOW_ITEM_MAX_CHARS = 2_000
WINDOW_ITEMS_PAGE_SIZE = 50
WINDOW_MAX_ITEMS_PER_SESSION = 500

#: Earlier messages of the person shown beside the new ones, so a pattern across runs (the same
#: topic, audience or way of working) is visible to the extractor. Context only, never cited.
LOOKBACK_SECONDS = 14 * 24 * 3600
LOOKBACK_MAX_CHARS = 16_000
LOOKBACK_ITEM_MAX_CHARS = 400

#: A claim candidate longer than this is rejected as malformed.
MAX_CLAIM_TEXT_CHARS = 500

#: How many existing claims the apply step compares a candidate against.
APPLY_SEARCH_LIMIT = 5

#: Confidence bump on "same" (reinforce), per MEMORY-PLAN.md section 6.
REINFORCE_INCREMENT_STATED = 0.05
REINFORCE_INCREMENT_INFERRED = 0.2

#: Hourly sweep interval (omnigent/server/memory_upkeep.py).
HOURLY_SWEEP_INTERVAL_S = 3600

#: Env var overriding the upkeep hourly-sweep interval. Lower it for testing.
UPKEEP_SWEEP_INTERVAL_SECONDS_ENV = "OMNIGENT_UPKEEP_SWEEP_INTERVAL_SECONDS"

#: Env var overriding how many upkeep runs may run concurrently across users.
UPKEEP_MAX_CONCURRENCY_ENV = "OMNIGENT_UPKEEP_MAX_CONCURRENCY"
DEFAULT_UPKEEP_MAX_CONCURRENCY = 4

#: Budget for one extraction/classification LLM call. A hung provider call
#: must not wedge the upkeep run (and, by extension, the single-run lease)
#: forever — see :func:`extract_candidates` / :func:`classify_relation`.
UPKEEP_LLM_CALL_TIMEOUT_S = 120.0


def resolve_upkeep_sweep_interval_seconds() -> int:
    """Resolve the upkeep hourly-sweep interval (``MemoryUpkeepScheduler``).

    Reads :data:`UPKEEP_SWEEP_INTERVAL_SECONDS_ENV`, default
    :data:`HOURLY_SWEEP_INTERVAL_S`. A missing, non-integer, or
    non-positive value falls back to the default.
    """
    raw = os.environ.get(UPKEEP_SWEEP_INTERVAL_SECONDS_ENV)
    if raw is None:
        return HOURLY_SWEEP_INTERVAL_S
    try:
        value = int(raw)
    except ValueError:
        return HOURLY_SWEEP_INTERVAL_S
    return value if value > 0 else HOURLY_SWEEP_INTERVAL_S


def resolve_upkeep_max_concurrency() -> int:
    """Resolve how many upkeep runs may run concurrently (``MemoryUpkeepCoordinator``).

    Reads :data:`UPKEEP_MAX_CONCURRENCY_ENV`, default
    :data:`DEFAULT_UPKEEP_MAX_CONCURRENCY` (4). A missing, non-integer, or
    non-positive value falls back to the default.
    """
    raw = os.environ.get(UPKEEP_MAX_CONCURRENCY_ENV)
    if raw is None:
        return DEFAULT_UPKEEP_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UPKEEP_MAX_CONCURRENCY
    return value if value > 0 else DEFAULT_UPKEEP_MAX_CONCURRENCY


# ── extraction / classification prompts (module constants, per plan) ────
# Portions modified from getnao/nao
# apps/backend/src/components/ai/memory-system-prompt.tsx@5bde830, Apache-2.0; changes: only the
# rules are merged (one-off task instructions are left out, instructions without a permanence
# signal are kept only as inferred evidence while profile facts need none, a changed fact or
# replaced instruction supersedes the old claim, a withdrawal is never stored); Superchat's kinds,
# repeated-behaviour evidence, verbatim-quote check and evidence scoring are its own.

#: What a lasting memory about the person is, shared by every writer that saves one from the
#: person's words (this upkeep and the daily note's quiet pass).
MEMORY_POLICY = """\
Keep a useful picture of the person, built only from their own words and repeated behaviour:
- Who they are: role, organisation, field, the clients, teams and people they mention.
- What they work on: recurring topics, kinds of work and audiences, ongoing projects.
- How they like work done: a way of working they ask for again and again, or state as a rule \
("always", "never", "from now on", "I prefer", "in general").
A topic, audience or way of working that comes up in several of their messages is a pattern: \
keep it as "inferred" (e.g. "The user often asks for short slide decks on places and history in \
Peru."). Said once, it is not a pattern.
Leave out:
- One-off task instructions: a slide count, a deadline, "stop researching", "just build it with \
what you have", "make it shorter". They belong to that task, unless the person asks for the same \
way of working again or says it is a rule.
- Anything the assistant, a worker or a tool chose or wrote (a theme, a layout, a headline rule, \
a brief's constraints, a result's report), even when the person accepted it. A short answer to \
the assistant's question or a tapped option ("Corporate Clean", "yes") is a choice for that \
request, never a preference.
- Questions about the assistant itself and test pings ("which model are you", "reply with one \
word"), small talk, and anything you doubt.
- Secrets and credentials (passwords, tokens, keys, account or card numbers) and private details \
about other people that the person did not ask you to keep."""

MEMORY_UPKEEP_EXTRACTION_PROMPT = (
    """\
You are extracting durable long-term memory claims about one user from a \
transcript of their own messages.

"""
    + MEMORY_POLICY
    + """

The transcript has two parts. EARLIER messages are the user's older messages, \
shown so you can see what repeats; never cite one as the main evidence. NEW \
messages are what arrived since the last run; every claim's "item_id" and \
"quote" come from a NEW user message. Assistant lines and "[System: ...]" \
reports are context only — never a source of a claim.

A pattern claim ("explicitness": "inferred") may list in "repeats" the other \
user messages (EARLIER or NEW) that show the same thing, each with its \
"item_id" and an exact "quote"; list only real repeats.

A standing instruction ("kind": "instruction", how the user wants the \
assistant to behave from now on) is "stated" only when the user's own words \
carry a permanence signal ("always", "never", "from now on", "every time", \
"in general", "don't ever", "remember that I"); a repeated one without it is \
"inferred". Profile facts the user states directly ("I'm the head of sales", \
"I work at X", "call me...") are "stated" and need no special wording.

A withdrawal is not a claim: when the user only takes something back ("I no \
longer want French", "forget that"), extract nothing for it and never write a \
negation. A replacement that states the new thing ("reply in German from now \
on") is extracted as the new claim.

Each "claim_text" is one self-contained third-person sentence ("The user \
..."); never give the user a name they didn't state as their own. Each \
"quote" is the user's exact words copied from the cited item — do not \
paraphrase, fix spelling or add punctuation.

Respond with a JSON object of this exact shape and nothing else:
{"candidates": [{"kind": "preference"|"instruction"|"fact"|"decision"|
"person"|"project"|"working_style", "claim_text": "...", "quote": "...",
"item_id": "...", "explicitness": "stated"|"inferred",
"repeats": [{"item_id": "...", "quote": "..."}],
"valid_until": "<ISO date or null>"}]}
Return {"candidates": []} when nothing durable is present.
Output JSON only — no markdown, no commentary."""
)

MEMORY_UPKEEP_CLASSIFY_PROMPT = """\
Decide how a new memory candidate about a user relates to their existing \
memory claims, listed below.

Respond with a JSON object of this exact shape and nothing else:
{"relation": "same"|"new"|"contradicts"|"withdrawal", "claim_id": "<id or null>"}
Use "same" when the candidate restates or paraphrases one of the listed \
claims with the same meaning — set "claim_id" to that claim's id. Use \
"contradicts" only when the candidate clearly conflicts with one specific \
listed claim — set "claim_id" to that claim's id; this includes a changed fact \
(new role, new employer) and an instruction the user replaced with a new one. \
The old claim is then superseded (kept as history, no longer active); if the \
user wrote or edited it themselves it is kept and the new claim sits beside it. \
A candidate that only withdraws or negates something ("no longer wants French") \
is not a claim to store: use "withdrawal" with "claim_id": null, never "new" and \
never a recorded negation. Never guess at a contradiction the text doesn't name. \
Otherwise use "new" with "claim_id": null. Output JSON only — no markdown, no commentary."""

# ── secret / one-time guards (verify step) ───────────────────────────────

_SECRET_PATTERNS = (
    re.compile(r"\b(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),  # OpenAI-style secret key
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),  # GitHub token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),  # IBAN-like
)

_ONE_TIME_MARKERS = (
    "this time",
    "just this once",
    "just for now",
    "just now",
    "for this report",
    "for this one",
    "today only",
    "only for today",
)


# ── data shapes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowItem:
    """One message item inside an upkeep run's window.

    :param role: ``"user"`` or ``"assistant"``. Only ``"user"`` items are
        ever evidence; ``"assistant"`` items are context-only (section 2
        of the window step).
    """

    session_id: str
    item_id: str
    role: str
    text: str
    created_at: int


@dataclass(frozen=True)
class VerifiedCandidate:
    """An extraction candidate that passed every deterministic check."""

    kind: str
    claim_text: str
    quote: str
    item_id: str
    session_id: str
    explicitness: str
    valid_until: int | None
    #: Other messages of the person showing the same pattern: ``(session_id, item_id)``.
    repeats: tuple[tuple[str, str], ...] = ()


class UpkeepLLMCaller(Protocol):
    """One LLM call: a system prompt plus a user turn, returning raw text."""

    async def __call__(self, *, instructions: str, input_text: str) -> str: ...


class UpkeepMemory(Protocol):
    """The subset of :class:`~omnigent.memory.service.MemoryService` the
    apply step needs — a ``Protocol`` so tests can pass a plain fake
    instead of a real (txtai-backed) service."""

    def search(
        self, user_id: str, query: str, *, kind: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    def record_claim(
        self,
        user_id: str,
        kind: str,
        text: str,
        *,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = STATED_CONFIDENCE,
        run_id: str | None = None,
        valid_until: int | None = None,
    ) -> dict[str, Any]: ...

    def reinforce_claim(
        self, claim_id: str, user_id: str, *, confidence_increment: float
    ) -> dict[str, Any] | None: ...

    def supersede_claim(
        self,
        old_claim_id: str,
        user_id: str,
        *,
        kind: str,
        text: str,
        quote: str | None = None,
        speaker: str | None = None,
        evidence: list[MemoryEvidenceLink] | None = None,
        explicitness: str = "stated",
        confidence: float = STATED_CONFIDENCE,
        run_id: str | None = None,
    ) -> dict[str, Any]: ...


# ── 1. window ─────────────────────────────────────────────────────────────


def _role_of(item: Any) -> str | None:
    role = getattr(item.data, "role", None)
    return role if role in ("user", "assistant") else None


def _message_text(item: Any) -> str:
    content = getattr(item.data, "content", None)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    text = " ".join(parts)
    # The person's evidence is their words, never document text an older gateway stored with them.
    return strip_legacy_attachment_context(text) if _role_of(item) == "user" else text


def _is_person_session(conv: Any) -> bool:
    """``False`` for sessions whose "user" messages are not the person's words.

    A sub-agent / Helper session (including a parent-bound scheduled Helper)
    and a standalone scheduled-fire session open with a system-authored
    prompt sent as the first user message. Memory claims must be backed by
    the person's own quote, so those sessions are never evidence.
    """
    if getattr(conv, "kind", "default") == "sub_agent":
        return False
    if getattr(conv, "parent_conversation_id", None) is not None and getattr(
        conv, "sub_agent_name", None
    ):
        return False
    labels = getattr(conv, "labels", None) or {}
    return not any(
        key in labels
        for key in (
            SCHEDULED_HELPER_LABEL_KEY,
            SCHEDULED_FIRE_LABEL_KEY,
            SUBAGENT_DISPATCH_ID_LABEL_KEY,
        )
    )


def _session_window_items(
    conversation_store: ConversationStore,
    session_id: str,
    since: int,
) -> list[WindowItem]:
    """User + assistant message items in *session_id* newer than *since*.

    Pages newest-first and stops as soon as an item at or before the
    watermark is reached — items only get older from there.
    """
    collected: list[WindowItem] = []
    cursor: str | None = None
    while len(collected) < WINDOW_MAX_ITEMS_PER_SESSION:
        page = conversation_store.list_items(
            session_id,
            limit=WINDOW_ITEMS_PAGE_SIZE,
            before=cursor,
            order="desc",
            type="message",
        )
        if not page.data:
            break
        reached_watermark = False
        for item in page.data:
            if item.created_at <= since:
                reached_watermark = True
                break
            role = _role_of(item)
            if role is None:
                continue
            text = _message_text(item)
            # Runtime notices injected as user-role items are not the person's words.
            if role == "user" and is_system_notice(item.data):
                continue
            collected.append(
                WindowItem(
                    session_id=session_id,
                    item_id=item.id,
                    role=role,
                    text=text,
                    created_at=item.created_at,
                )
            )
        if reached_watermark or not page.has_more:
            break
        cursor = page.last_id
    collected.reverse()  # chronological (ascending) order
    return collected


def gather_window(
    conversation_store: ConversationStore,
    user_id: str,
    since: int,
) -> list[WindowItem]:
    """User-authored message items (plus the assistant turn they reply to)
    across *user_id*'s sessions since *since*. Never tool outputs,
    documents, web content, or assistant text as evidence — the apply step
    enforces that by only ever citing a ``role == "user"`` item.
    """
    items: list[WindowItem] = []
    cursor: str | None = None
    scanned = 0
    while scanned < WINDOW_SESSIONS_LIMIT:
        page = conversation_store.list_conversations(
            owned_by=user_id,
            kind=None,
            limit=min(50, WINDOW_SESSIONS_LIMIT - scanned),
            after=cursor,
            order="desc",
            sort_by="updated_at",
        )
        if not page.data:
            break
        reached_watermark = False
        for conv in page.data:
            scanned += 1
            if conv.updated_at < since:
                reached_watermark = True
                break
            if not _is_person_session(conv):
                continue
            items.extend(_session_window_items(conversation_store, conv.id, since))
        if reached_watermark or not page.has_more:
            break
        cursor = page.last_id
    items.sort(key=lambda i: i.created_at)
    return items


def bound_window(window_items: list[WindowItem]) -> list[WindowItem]:
    """The newest *window_items* within :data:`WINDOW_MAX_CHARS`, each clipped
    to :data:`WINDOW_ITEM_MAX_CHARS`, in chronological order."""
    kept: list[WindowItem] = []
    total = 0
    for item in reversed(window_items):
        clipped = replace(item, text=item.text[:WINDOW_ITEM_MAX_CHARS])
        total += len(clipped.text)
        if total > WINDOW_MAX_CHARS and kept:
            break
        kept.append(clipped)
    kept.reverse()
    return kept


# ── 2. gate ───────────────────────────────────────────────────────────────


def gate(
    window_items: list[WindowItem], *, min_user_messages: int = GATE_MIN_USER_MESSAGES
) -> bool:
    """``True`` when there is enough new signal to bother extracting."""
    substantive = sum(1 for i in window_items if i.role == "user" and len(i.text.strip()) >= 2)
    return substantive >= min_user_messages


# ── 3. extract ────────────────────────────────────────────────────────────


class UnreadableExtraction(ValueError):
    """The extraction model's reply was not the JSON object asked for."""


_CODE_FENCE_PATTERN = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


def _balanced_json_object(text: str) -> str | None:
    """The first balanced top-level ``{...}`` object in *text*, honoring
    string literals (a ``}`` or ``{`` inside a quoted string never counts).

    :returns: The matched substring, or ``None`` if no balanced object
        starts in *text* (an unterminated ``{``, or no ``{`` at all).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _strip_code_fence(text: str) -> str:
    """The JSON object in a model reply, even when wrapped in a fence or prose.

    Three attempts, each a fallback for the last: the whole (stripped) reply
    as-is, since a well-behaved model needs nothing stripped; the content of
    a ` ```json ` fence, narrowed to its first balanced object (a model
    sometimes wraps the JSON in commentary even inside the fence); finally a
    balanced-brace scan over the whole reply for the first top-level object.
    A naive "first ``{`` to last ``}``" scan (the prior implementation) is
    wrong whenever prose before or after the JSON itself contains braces.
    """
    stripped = text.strip()
    try:
        json.loads(stripped)
    except (TypeError, ValueError):
        pass
    else:
        return stripped
    fence_match = _CODE_FENCE_PATTERN.search(stripped)
    if fence_match is not None:
        fenced_object = _balanced_json_object(fence_match.group(1))
        if fenced_object is not None:
            return fenced_object
    balanced = _balanced_json_object(stripped)
    if balanced is not None:
        return balanced
    return stripped


def json_object_text(text: str) -> str:
    """The JSON object in a model reply, even when wrapped in a fence or prose (the reading
    upkeep's extraction uses; other direct model calls that ask for JSON reuse it)."""
    return _strip_code_fence(text)


def bound_earlier(earlier_items: list[WindowItem]) -> list[WindowItem]:
    """The person's newest earlier messages within :data:`LOOKBACK_MAX_CHARS`, each clipped to
    :data:`LOOKBACK_ITEM_MAX_CHARS`, in chronological order (assistant turns dropped)."""
    kept: list[WindowItem] = []
    total = 0
    for item in reversed(earlier_items):
        if item.role != "user" or len(item.text.strip()) < 2:
            continue
        clipped = replace(item, text=item.text[:LOOKBACK_ITEM_MAX_CHARS])
        total += len(clipped.text)
        if total > LOOKBACK_MAX_CHARS:
            break
        kept.append(clipped)
    kept.reverse()
    return kept


def _format_window_for_extraction(
    window_items: list[WindowItem], earlier_items: list[WindowItem] | None = None
) -> str:
    lines: list[str] = []
    if earlier_items:
        lines.append('EARLIER messages (to see what repeats; cite only in "repeats"):')
        lines.extend(f'[item_id="{i.item_id}"] USER: {i.text}' for i in earlier_items)
        lines.append("")
    lines.append("NEW messages:")
    for item in window_items:
        if item.role == "user":
            lines.append(f'[item_id="{item.item_id}"] USER: {item.text}')
        else:
            lines.append(f"ASSISTANT (context only, not evidence): {item.text}")
    return "\n".join(lines)


async def extract_candidates(
    llm_caller: UpkeepLLMCaller,
    window_items: list[WindowItem],
    earlier_items: list[WindowItem] | None = None,
) -> list[dict[str, Any]]:
    """Call the extraction model; return raw candidate dicts (unverified).

    A timeout or a reply that is not JSON raises (:class:`UnreadableExtraction`): the run is
    then recorded as failed with the reason and its window is read again next time, instead of
    passing as "nothing to remember".
    """
    try:
        raw_text = await asyncio.wait_for(
            llm_caller(
                instructions=MEMORY_UPKEEP_EXTRACTION_PROMPT,
                input_text=_format_window_for_extraction(window_items, earlier_items),
            ),
            timeout=UPKEEP_LLM_CALL_TIMEOUT_S,
        )
    except TimeoutError:
        _logger.warning("memory upkeep extraction timed out after %ss", UPKEEP_LLM_CALL_TIMEOUT_S)
        raise
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (TypeError, ValueError):
        _logger.warning("memory upkeep extraction returned non-JSON output")
        raise UnreadableExtraction from None
    if not isinstance(parsed, dict):
        raise UnreadableExtraction
    candidates = parsed.get("candidates")
    return candidates if isinstance(candidates, list) else []


# ── 4. verify (deterministic) ────────────────────────────────────────────


def _looks_like_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    """*text* with every match of the secret guard withheld: the person's words a new session
    is given verbatim (ADR 0031 part 6) pass the same filter memory does."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[withheld]", text)
    return text


def _looks_like_one_time(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ONE_TIME_MARKERS)


def _normalize_for_verbatim(text: str) -> str:
    return " ".join(text.lower().split())


def _quote_is_verbatim(quote: str, source_text: str) -> bool:
    return _normalize_for_verbatim(quote) in _normalize_for_verbatim(source_text)


def _parse_valid_until(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive ISO datetime (no offset) must be interpreted as UTC, not the
    # server process's local timezone — datetime.timestamp() assumes local
    # time for a naive value, which would shift every claim's expiry by the
    # server's UTC offset and make it depend on where the process runs.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


_PERMANENCE_RE = re.compile(
    r"\b(always|never|from now on|every time|in general|by default|default to|"
    r"i prefer|i like|i love|i want|i hate|i don't like|remember that|don't ever|going forward)\b",
    re.IGNORECASE,
)
_CHOICE_KINDS = frozenset({"preference", "instruction", "working_style"})
#: A message this short with no permanence signal is a pick answering a question, not a rule.
_CHOICE_MAX_WORDS = 6


def _is_choice_answer(text: str) -> bool:
    """Whether *text* reads as a short pick (a tapped card option or a one-word reply)."""
    return len(text.split()) <= _CHOICE_MAX_WORDS and not _PERMANENCE_RE.search(text)


def _verified_repeats(
    raw: Any, main_item_id: str, repeat_items_by_id: dict[str, WindowItem]
) -> tuple[tuple[str, str], ...]:
    """The ``repeats`` whose item is one of the person's messages and whose quote is verbatim.

    A repeat that fails is dropped, not the claim: it only adds evidence."""
    if not isinstance(raw, list):
        return ()
    seen: set[str] = {main_item_id}
    verified: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item = repeat_items_by_id.get(str(entry.get("item_id") or ""))
        quote = entry.get("quote")
        if item is None or item.item_id in seen or not isinstance(quote, str) or not quote.strip():
            continue
        if _looks_like_secret(quote) or not _quote_is_verbatim(quote, item.text):
            continue
        seen.add(item.item_id)
        verified.append((item.session_id, item.item_id))
    return tuple(verified)


def verify_candidate(
    raw: Any,
    items_by_id: dict[str, WindowItem],
    repeat_items_by_id: dict[str, WindowItem] | None = None,
) -> tuple[VerifiedCandidate | None, str | None]:
    """Deterministic checks (section 4). ``(candidate, None)`` on success,
    else ``(None, reason)``.

    :param items_by_id: The new user items: the only ones a claim may cite.
    :param repeat_items_by_id: Every user item shown (earlier and new), which an inferred
        claim's ``repeats`` may name as more evidence of the same pattern.
    """
    if not isinstance(raw, dict):
        return None, "malformed"
    kind = raw.get("kind")
    claim_text = raw.get("claim_text")
    quote = raw.get("quote")
    item_id = raw.get("item_id")
    explicitness = raw.get("explicitness")
    if not all(
        isinstance(v, str) and v.strip() for v in (kind, claim_text, quote, item_id, explicitness)
    ):
        return None, "malformed"
    assert isinstance(kind, str)
    assert isinstance(claim_text, str)
    assert isinstance(quote, str)
    assert isinstance(item_id, str)
    assert isinstance(explicitness, str)
    if kind not in VALID_KINDS:
        return None, "invalid_kind"
    if explicitness not in ("stated", "inferred"):
        return None, "invalid_explicitness"
    if len(claim_text) > MAX_CLAIM_TEXT_CHARS or len(quote) > MAX_CLAIM_TEXT_CHARS:
        return None, "overlong"
    item = items_by_id.get(item_id)
    if item is None:
        # Not in the window, not owned by this user, or not a user item —
        # the gather_window/items_by_id boundary is the only evidence
        # source, so an unknown id is rejected rather than trusted.
        return None, "unknown_item"
    if _looks_like_secret(quote) or _looks_like_secret(claim_text):
        return None, "secret"
    if _looks_like_one_time(quote):
        return None, "one_time"
    if not _quote_is_verbatim(quote, item.text):
        return None, "quote_not_verbatim"
    if kind in _CHOICE_KINDS and _is_choice_answer(item.text):
        return None, "choice_answer"
    return (
        VerifiedCandidate(
            kind=kind,
            claim_text=claim_text.strip(),
            quote=quote,
            item_id=item_id,
            session_id=item.session_id,
            explicitness=explicitness,
            valid_until=_parse_valid_until(raw.get("valid_until")),
            repeats=_verified_repeats(raw.get("repeats"), item_id, repeat_items_by_id or {})
            if explicitness == "inferred"
            else (),
        ),
        None,
    )


# ── 5. apply ──────────────────────────────────────────────────────────────


def _starting_confidence(candidate: VerifiedCandidate) -> float:
    """Stated starts high; inferred starts low and each verified repeat counts as one
    reinforcement, so a pattern seen three times starts where a twice-reinforced one would."""
    if candidate.explicitness == "stated":
        return STATED_CONFIDENCE
    raised = INFERRED_CONFIDENCE + REINFORCE_INCREMENT_INFERRED * len(candidate.repeats)
    return min(raised, STATED_CONFIDENCE)


async def classify_relation(
    llm_caller: UpkeepLLMCaller, candidate: VerifiedCandidate, matches: list[dict[str, Any]]
) -> tuple[str, str | None]:
    """One classification call: how does *candidate* relate to *matches*?

    A reply this function cannot parse (a timeout, non-JSON output, or a
    missing/invalid ``relation`` field) returns ``"skip"`` — the caller
    (:func:`apply_candidate`) must reject the candidate outright rather than
    defaulting to ``"new"``: guessing "new" on an unparseable reply risks
    inserting a duplicate of a claim the model may well have been about to
    reinforce or supersede.
    """
    listing = "\n".join(f'- claim_id="{m["claim_id"]}": {m["text"]}' for m in matches)
    input_text = f'Candidate: "{candidate.claim_text}"\n\nExisting claims:\n{listing}'
    try:
        raw_text = await asyncio.wait_for(
            llm_caller(instructions=MEMORY_UPKEEP_CLASSIFY_PROMPT, input_text=input_text),
            timeout=UPKEEP_LLM_CALL_TIMEOUT_S,
        )
    except TimeoutError:
        _logger.warning(
            "memory upkeep classification timed out after %ss", UPKEEP_LLM_CALL_TIMEOUT_S
        )
        return "skip", None
    try:
        parsed = json.loads(_strip_code_fence(raw_text))
    except (TypeError, ValueError):
        return "skip", None
    if not isinstance(parsed, dict):
        return "skip", None
    relation = parsed.get("relation")
    claim_id = parsed.get("claim_id")
    if relation not in ("same", "new", "contradicts", "withdrawal"):
        return "skip", None
    return relation, claim_id if isinstance(claim_id, str) else None


async def apply_candidate(
    memory: UpkeepMemory,
    user_id: str,
    candidate: VerifiedCandidate,
    run_id: str,
    llm_caller: UpkeepLLMCaller,
) -> str:
    """Apply one verified candidate. Returns ``"inserted"``, ``"reinforced"``,
    ``"superseded"``, or a rejection reason string (``"withdrawal"`` when the candidate only
    takes something back: nothing is stored).

    Every ``memory.*`` call is synchronous (``UpkeepMemory`` is backed by a
    DB + a txtai index), so each one runs in a worker thread
    (:func:`asyncio.to_thread`) rather than blocking the event loop — the
    same treatment :func:`gather_window` already gets.
    """
    evidence = [MemoryEvidenceLink(session_id=candidate.session_id, item_id=candidate.item_id)]
    evidence += [MemoryEvidenceLink(session_id=s, item_id=i) for s, i in candidate.repeats]
    confidence = _starting_confidence(candidate)
    matches = await asyncio.to_thread(
        memory.search, user_id, candidate.claim_text, limit=APPLY_SEARCH_LIMIT
    )
    if not matches:
        await asyncio.to_thread(
            memory.record_claim,
            user_id,
            candidate.kind,
            candidate.claim_text,
            quote=candidate.quote,
            speaker="user",
            evidence=evidence,
            explicitness=candidate.explicitness,
            confidence=confidence,
            run_id=run_id,
            valid_until=candidate.valid_until,
        )
        return "inserted"

    relation, named_claim_id = await classify_relation(llm_caller, candidate, matches)
    matched_ids = {m["claim_id"] for m in matches}

    if relation == "skip":
        # The classifier's reply didn't parse (timeout, non-JSON, or a
        # missing/invalid relation) — reject rather than guess "new" and
        # risk inserting a duplicate of a claim it may have meant to
        # reinforce or supersede.
        return "unparseable"

    if relation == "withdrawal":
        # A negation is never stored; forgetting belongs to the live conversation.
        return "withdrawal"

    if relation == "same":
        target_id = str(
            named_claim_id if named_claim_id in matched_ids else matches[0]["claim_id"]
        )
        increment = (
            REINFORCE_INCREMENT_STATED
            if candidate.explicitness == "stated"
            else REINFORCE_INCREMENT_INFERRED
        )
        reinforced = await asyncio.to_thread(
            memory.reinforce_claim, target_id, user_id, confidence_increment=increment
        )
        return "reinforced" if reinforced is not None else "reinforce_target_missing"

    if relation == "contradicts":
        # Never act on a contradiction the classifier didn't name explicitly.
        if named_claim_id is None or named_claim_id not in matched_ids:
            return "contradiction_not_named"
        result = await asyncio.to_thread(
            memory.supersede_claim,
            named_claim_id,
            user_id,
            kind=candidate.kind,
            text=candidate.claim_text,
            quote=candidate.quote,
            speaker="user",
            evidence=evidence,
            explicitness=candidate.explicitness,
            confidence=confidence,
            run_id=run_id,
        )
        return "contradiction_target_missing" if "error" in result else "superseded"

    await asyncio.to_thread(
        memory.record_claim,
        user_id,
        candidate.kind,
        candidate.claim_text,
        quote=candidate.quote,
        speaker="user",
        evidence=evidence,
        explicitness=candidate.explicitness,
        confidence=confidence,
        run_id=run_id,
        valid_until=candidate.valid_until,
    )
    return "inserted"


# ── orchestration: window -> gate -> extract -> verify -> apply -> record
# ────────────────────────────────────────────────────────────────────────


def _new_counts() -> dict[str, Any]:
    return {
        "seen": 0,
        "candidates": 0,
        "inserted": 0,
        "reinforced": 0,
        "superseded": 0,
        "rejected": 0,
        "rejected_reasons": {},
    }


def _reject(counts: dict[str, Any], reason: str) -> None:
    counts["rejected"] += 1
    counts["rejected_reasons"][reason] = counts["rejected_reasons"].get(reason, 0) + 1


async def run_upkeep_once(
    *,
    user_id: str,
    conversation_store: ConversationStore,
    memory: UpkeepMemory,
    upkeep_store: MemoryUpkeepStore,
    llm_caller: UpkeepLLMCaller | None,
    now: int | None = None,
    min_user_messages: int = GATE_MIN_USER_MESSAGES,
) -> MemoryUpkeepRun | None:
    """Run the upkeep job once for *user_id*. ``None`` if a run is already
    in flight for this user (the lease — see
    :meth:`~omnigent.stores.memory_upkeep_store.MemoryUpkeepStore.start_run`)
    — a silent skip, never surfaced to the user, never recorded as a row.
    """
    now_ts = now if now is not None else now_epoch()
    watermark_run = upkeep_store.get_last_succeeded_run(user_id)
    since = watermark_run.window_until if watermark_run is not None else 0
    run_id = uuid.uuid4().hex
    started = upkeep_store.start_run(run_id, user_id, since, now_ts)
    if started is None:
        return None

    counts = _new_counts()
    try:
        # One scan reaches back past the watermark: the new items are the window, the person's
        # older messages are shown beside them so a repeat across runs is visible.
        scanned = await asyncio.to_thread(
            gather_window, conversation_store, user_id, max(0, since - LOOKBACK_SECONDS)
        )
        window_items = bound_window([i for i in scanned if i.created_at > since])
        earlier_items = bound_earlier([i for i in scanned if i.created_at <= since])
        counts["seen"] = sum(1 for i in window_items if i.role == "user")

        if not gate(window_items, min_user_messages=min_user_messages):
            return upkeep_store.finish_run(
                run_id, state="skipped", counts=counts, disposition="no_new_signal"
            )
        if llm_caller is None:
            return upkeep_store.finish_run(
                run_id, state="skipped", counts=counts, disposition="no_llm_configured"
            )

        raw_candidates = await extract_candidates(llm_caller, window_items, earlier_items)
        counts["candidates"] = len(raw_candidates)
        items_by_id = {i.item_id: i for i in window_items if i.role == "user"}
        repeat_items_by_id = {**{i.item_id: i for i in earlier_items}, **items_by_id}

        for raw in raw_candidates:
            candidate, reason = verify_candidate(raw, items_by_id, repeat_items_by_id)
            if candidate is None:
                _reject(counts, reason or "malformed")
                continue
            outcome = await apply_candidate(memory, user_id, candidate, run_id, llm_caller)
            if outcome in ("inserted", "reinforced", "superseded"):
                counts[outcome] += 1
            else:
                _reject(counts, outcome)

        # The watermark for the NEXT run: the newest item actually scanned
        # this run, never wall-clock "now" — an item whose write isn't yet
        # visible when the scan starts but whose created_at predates "now"
        # would otherwise fall before the next run's `since` and be skipped
        # forever. ``since`` itself when nothing was scanned (defensive;
        # gate() already requires a non-empty window to reach here).
        window_until = max((item.created_at for item in window_items), default=since)
        return upkeep_store.finish_run(
            run_id,
            state="succeeded",
            counts=counts,
            disposition="ok",
            window_until=window_until,
        )
    except Exception as exc:
        # The class name only: an exception message may carry a provider's request details.
        counts["error"] = type(exc).__name__
        upkeep_store.finish_run(run_id, state="failed", counts=counts, disposition="error")
        raise


_FAILURE_WORDS = {
    "TimeoutError": "the model took too long to answer",
    "UnreadableExtraction": "the model's answer could not be read",
}


def describe_upkeep_run(run: MemoryUpkeepRun) -> str:
    """One run's outcome in plain words, from its stored state and counts.

    e.g. ``"Read 4 messages · nothing new to remember"`` or ``"Read 6 messages · saved 2 things
    about you"``. Shown on the Automations row and returned to the Assistant by memory_search.
    """
    counts = run.counts or {}
    seen = int(counts.get("seen") or 0)
    read = f"Read {seen} message{'' if seen == 1 else 's'}"
    if run.state == "running":
        return "Reading your new messages now"
    if run.state == "failed":
        reason = _FAILURE_WORDS.get(str(counts.get("error") or ""), "something went wrong")
        return f"Failed · {reason}; it tries again on the next run"
    if run.state == "skipped":
        if run.disposition == "no_llm_configured":
            return "Skipped · no model is set up for background work"
        return (
            f"Only {seen} new message{'' if seen == 1 else 's'} · "
            f"waits for {GATE_MIN_USER_MESSAGES} before reading"
        )
    changes = [
        (word, int(counts[key]))
        for key, word in (
            ("inserted", "saved"),
            ("reinforced", "confirmed"),
            ("superseded", "updated"),
        )
        if counts.get(key)
    ]
    if not changes:
        return f"{read} · nothing new to remember"
    # "saved 2 things about you, confirmed 1"
    (word, n), rest = changes[0], changes[1:]
    parts = [f"{word} {n} thing{'' if n == 1 else 's'} about you", *(f"{w} {k}" for w, k in rest)]
    return f"{read} · {', '.join(parts)}"


def build_upkeep_llm_caller() -> UpkeepLLMCaller | None:
    """Build the production :class:`UpkeepLLMCaller` from the server-level
    ``llm:`` config (``RuntimeCaps.llm``).

    Reuses the policy engine's server-LLM resolution
    (``omnigent.runtime.policies.builder``) — the same "how does the
    server itself call an LLM, with which credentials" answer function
    policies already use, so upkeep extraction needs no separate
    credential story. ``None`` when no server ``llm:`` config is present;
    callers then skip extraction (the run is recorded as ``skipped`` /
    ``no_llm_configured``).
    """
    from omnigent.runtime import get_caps
    from omnigent.runtime.policies.builder import (
        _build_policy_llm_client,
        _resolve_server_llm_connection,
    )

    server_llm = get_caps().llm
    if server_llm is None:
        return None
    connection = _resolve_server_llm_connection(server_llm)
    client = _build_policy_llm_client(server_llm, connection)
    if client is None:
        return None

    async def _call(*, instructions: str, input_text: str) -> str:
        from omnigent.llms.summarize import extract_summary_text

        response = await client.create(
            input=[{"role": "user", "content": input_text}],
            instructions=instructions,
        )
        return extract_summary_text(response)

    return _call


async def resolve_upkeep_llm_caller(
    user_id: str, *, category: str | None = None
) -> UpkeepLLMCaller | None:
    """The upkeep caller for *user_id*'s run: the server ``llm:`` config when present, else the
    deployment's background model for that owner (:mod:`omnigent.llms.background`).

    :param category: The owner's pick of this category instead (``builder`` for the Lookout's
        Judge), falling back to the background model when they have none.

    ``None`` when neither can call a model yet; the run is then recorded as skipped.
    """
    from omnigent.llms.background import resolve_background_llm

    resolved = await resolve_background_llm(user_id, category=category)
    return llm_caller(*resolved) if resolved is not None else None


def llm_caller(model: str, connection: dict[str, str] | None) -> UpkeepLLMCaller:
    """A direct call of *model* over *connection* (a resolved background or session route)."""
    from omnigent.llms.client import Client

    client = Client()

    async def _call(*, instructions: str, input_text: str) -> str:
        from omnigent.llms.summarize import extract_summary_text

        response = await client.responses.create(
            input=[{"role": "user", "content": input_text}],
            instructions=instructions,
            model=model,
            connection_params=connection,
        )
        return extract_summary_text(response)

    _call.model = model  # type: ignore[attr-defined]  # for the caller's log line
    return _call
