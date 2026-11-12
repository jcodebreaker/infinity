"""Session labels that switch context ownership between the CLI and Omnigent.

``omnigent.context.mode`` selects the lifecycle: unset means upstream
behavior, untouched; ``"rollover"`` means each native CLI compacts its own
context at Omnigent's threshold, and the session gets the recall tool and
instruction; ``"superside-chat"`` means the Super Chat and its Sub-agents
run on the Claude SDK engine with Omnigent owning context,
sub-agents and memory. See ``docs/context.md``.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Session label selecting how context is managed. Unset means the CLI
#: owns its own context (upstream behaviour, unchanged).
CONTEXT_MODE_LABEL = "omnigent.context.mode"

#: ``CONTEXT_MODE_LABEL`` value for the rollover super chat: Omnigent, not
#: the CLI, decides what the model carries forward.
ROLLOVER_MODE_VALUE = "rollover"

#: ``CONTEXT_MODE_LABEL`` value for the Super Chat and its Sub-agents on the
#: Claude SDK engine. Distinct from ``ROLLOVER_MODE_VALUE``
#: (native CLIs): a session is in exactly one mode. See
#: ``docs/glossary.md``.
SUPERSIDE_CHAT_MODE_VALUE = "superside-chat"

#: Child-session label carrying the dispatch id of the turn a sub-agent runs;
#: lets a parent recover a Result it never drained.
SUBAGENT_DISPATCH_ID_LABEL_KEY = "omnigent.subagent.dispatch_id"

#: Runner-side marker (never stored) added to the tool-gating labels of a
#: sub-agent session, so top-level-only tools are not offered to it.
SUBAGENT_LABEL_KEY = "omnigent.subagent"

#: Stamped on a Helper session created by a parent-bound scheduled fire; its
#: value is the scheduled task id. The runner registers such a child's Result
#: for the parent wake on its first turn.
SCHEDULED_HELPER_LABEL_KEY = "omnigent.subagent.scheduled_task_id"

#: Set by a proactive source on its scheduled Helper before the Helper's Result goes back, to
#: say how the message the Result causes should travel (ADR 0036): a JSON object
#: ``{"urgency": "now" | "today" | "brief", "topic_key": str, "sender": str, "term": str}``,
#: every key optional (urgency defaults to ``today``). The message's kind is the scheduled
#: task's ``kind``.
PROACTIVE_LABEL_KEY = "omnigent.proactive"

#: Stamped by the server on every child session when it creates it. It means "launched, no
#: status reported yet": the Activity Feed reads such a child as starting (and, if it never
#: reports one, as failed to start) instead of guessing from age alone. Inert once the child
#: has a ``live_status``.
LAUNCHING_LABEL_KEY = "omnigent.subagent.launching"

#: Stamped on an ad-hoc Helper (``start_adhoc_helper``: the daily-note pass). It does not wake
#: its parent, but like a scheduled Helper it may read the parent chat (``session_history``).
ADHOC_HELPER_LABEL_KEY = "omnigent.subagent.adhoc"

#: Stamped on a background run whose Result engine code posts (the Lookout's wake, the brief;
#: ADR 0038): nobody is there to answer, so a policy that would ask denies instead.
UNATTENDED_LABEL_KEY = "omnigent.unattended"


def background_task_id(labels: Mapping[str, str] | None) -> str | None:
    """The scheduled task a background Helper runs for: the scheduled label's value, else the
    ad-hoc label's (a scheduled run whose Result is posted by code, ADR 0038, carries that one so
    it never wakes its parent). ``None`` for any other session."""
    found = (labels or {}).get(SCHEDULED_HELPER_LABEL_KEY) or (labels or {}).get(
        ADHOC_HELPER_LABEL_KEY
    )
    return found or None


#: Stamped on a file's studio session (ADR 0024, a ``sub_agent`` of helper type ``editor``); its
#: value is the artifact group id. The studio is outside the Conversation's Helpers, activity,
#: listings and caps; its turns get the file's own prefix.
ARTIFACT_CHAT_LABEL_KEY = "superchat.artifact_chat"


def is_artifact_chat(labels: Mapping[str, str] | None) -> bool:
    """Whether a session is a file's studio chat (it carries :data:`ARTIFACT_CHAT_LABEL_KEY`)."""
    return bool(labels) and ARTIFACT_CHAT_LABEL_KEY in labels  # type: ignore[operator]


#: Stamped on a standalone session a scheduled fire created (not parent-bound);
#: its value is the scheduled task id. Its first user message is the task's
#: system-authored prompt, so memory upkeep never treats it as the person's words.
SCHEDULED_FIRE_LABEL_KEY = "omnigent.scheduled_task_id"

#: Labels the server stamps on a run no person started (a schedule, a Goal's cadence, a pass).
BACKGROUND_LABELS = (SCHEDULED_HELPER_LABEL_KEY, ADHOC_HELPER_LABEL_KEY, SCHEDULED_FIRE_LABEL_KEY)

#: The Round a session's work belongs to now (ADR 0029). On the Conversation it moves when a
#: person's message opens a Round or a Helper's wake brings one back; a Helper gets its parent's
#: at creation and keeps it.
ROUND_LABEL_KEY = "omnigent.superchat.round_id"

#: Prefix of the session labels that hold pending ``state_close`` requests: one label per tag
#: (``<prefix><tag id>``), the value being the reason. Consumed by the next checkpoint, which
#: sets each to an empty string.
STATE_CLOSE_LABEL_PREFIX = "omnigent.state_close."

#: Set on a Helper the person paused (ADR 0027): the pause's epoch seconds; empty once resumed.
PAUSED_LABEL_KEY = "superchat.paused"

#: Prefix of the session labels that hold entries code adds to the working list (a task's
#: decision, ADR 0027): ``<prefix><tag id>`` = the entry's text. Consumed like a close.
STATE_ADD_LABEL_PREFIX = "omnigent.state_add."

# Token count at which a rollover session rolls over. Unset falls back to
# 60% of the model's context window (100k-200k), or 100,000 when unknown
# (see rollover.resolve_rollover_threshold).
ROLLOVER_AT_TOKENS_LABEL = "omnigent.context.rollover_at_tokens"

# Token budget for the kept tail, counted as the model reads it (whole Rounds only; the last
# is always kept; see rollover.select_recent; ADR 0031 part 7). Unset -> DEFAULT_KEEP_TOKENS.
ROLLOVER_KEEP_TOKENS_LABEL = "omnigent.context.rollover_keep_tokens"
DEFAULT_KEEP_TOKENS = 8_000


def is_rollover(labels: Mapping[str, str] | None) -> bool:
    """Whether a session's labels select rollover (Omnigent-owned) context.

    :param labels: The session's labels, or ``None``.
    :returns: ``True`` only when ``CONTEXT_MODE_LABEL`` is exactly
        ``"rollover"``.
    """
    if not labels:
        return False
    return labels.get(CONTEXT_MODE_LABEL) == ROLLOVER_MODE_VALUE


def is_superside_chat(labels: Mapping[str, str] | None) -> bool:
    """Whether a session's labels select superside-chat mode.

    :param labels: The session's labels, or ``None``.
    :returns: ``True`` only when ``CONTEXT_MODE_LABEL`` is exactly
        ``"superside-chat"``.
    """
    if not labels:
        return False
    return labels.get(CONTEXT_MODE_LABEL) == SUPERSIDE_CHAT_MODE_VALUE


def uses_omnigent_context(labels: Mapping[str, str] | None) -> bool:
    """Whether a session is in either Omnigent-owned context mode.

    The shared gate for behaviour both modes use (``session_history``, the
    ``memory_*`` tool family): ``rollover`` (native CLIs) and
    ``superside-chat`` (the Super Chat on the Claude SDK engine) both hand
    context ownership to Omnigent, so both get it. Behaviour specific to one
    mode (engine-feature toggles, which compaction mechanism runs) stays
    gated on :func:`is_rollover` / :func:`is_superside_chat` individually,
    never on this helper.

    :param labels: The session's labels, or ``None``.
    :returns: ``True`` when ``CONTEXT_MODE_LABEL`` is ``"rollover"`` or
        ``"superside-chat"``.
    """
    return is_rollover(labels) or is_superside_chat(labels)


# Every label that configures rollover mode for a session: the set a
# superside-chat session forwards to a sub-agent it creates (see
# ``inheritable_context_labels``): the keys are mode-agnostic, so the same
# set covers a rollover session's tuning labels and a superside-chat
# session's mode label alike.
ROLLOVER_SESSION_LABELS: frozenset[str] = frozenset(
    {
        CONTEXT_MODE_LABEL,
        ROLLOVER_AT_TOKENS_LABEL,
        ROLLOVER_KEEP_TOKENS_LABEL,
    }
)


def inheritable_context_labels(labels: Mapping[str, str] | None) -> dict[str, str]:
    """Filter *labels* down to the ones a created-from-scratch child inherits.

    A fork copies the source's labels wholesale (store-level drop list only)
    and needs no filtering. A sub-agent create instead sends a fresh label
    set to the server, so the mode must be forwarded explicitly — and only
    the mode label plus its rollover tuning labels
    (:data:`ROLLOVER_SESSION_LABELS`), never arbitrary session labels (e.g.
    presentation or permission-mode labels, which are instance-scoped to the
    parent).

    :param labels: The creating session's labels, or ``None``.
    :returns: The subset of *labels* whose keys are in
        :data:`ROLLOVER_SESSION_LABELS`; ``{}`` when *labels* is ``None`` or
        empty, or has no matching keys.
    """
    if not labels:
        return {}
    return {key: value for key, value in labels.items() if key in ROLLOVER_SESSION_LABELS}
