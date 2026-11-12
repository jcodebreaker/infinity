"""The fold and the checkpoint of superside-chat sessions (ADR 0031).

A session's past is folded into its checkpoint (the tagged working list and the journal lines)
by :func:`roll_over_session`, the one summarizer. The fold runs in the engine after a turn
(:func:`fold_session`, scheduled by ``server/memory_upkeep.py``) when the settled-turn lines a
new session would list pass 2k tokens or the warm context reaches :func:`fold_at`, on the
owner's Light model. It only writes the checkpoint: it never drops a warm client, and a turn
never waits on it. The manual ``/compact`` (``runner/app.py``) runs the same function and then
drops the warm client. A new session is assembled from the checkpoint and the record after its
anchor (``_load_history_as_input``); a restart or a return from idle never summarizes. A warm
client is replaced only when it needs compacting (size), decided in :func:`handoff_reason`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

import cachetools
import httpx

from omnigent.context.labels import is_superside_chat
from omnigent.context.rollover import (
    CheckpointCause,
    build_rollover_item,
    is_placeholder_summary,
    reported_context_window,
    resolve_keep_tokens,
    resolve_rollover_threshold,
    split_at_latest_compaction,
)
from omnigent.context.state_file import (
    close_label_key,
    entry_label_key,
    pending_closes,
    pending_entries,
)
from omnigent.entities import CompactionData

logger = logging.getLogger(__name__)

#: Idle period after which the first message back carries a gap line ("away for N hours"); a
#: return keeps the warm session. Lower it (e.g. a few seconds) for testing.
OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV = "OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS"

#: 12 hours.
DEFAULT_IDLE_REFRESH_SECONDS = 43_200


def resolve_idle_refresh_seconds() -> int:
    """Resolve the idle period after which a superside-chat turn carries the gap line.

    Reads :data:`OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV`, default
    :data:`DEFAULT_IDLE_REFRESH_SECONDS` (12 hours). A missing, non-integer,
    or non-positive value falls back to the default.
    """
    raw = os.environ.get(OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS_ENV)
    if raw is None:
        return DEFAULT_IDLE_REFRESH_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDLE_REFRESH_SECONDS
    return value if value > 0 else DEFAULT_IDLE_REFRESH_SECONDS


#: Context tokens one background-job (Helper) turn may reach before it is asked to wrap up.
#: Rollover acts between turns only and the harness's own compaction is off, so without this a
#: long tool loop grows up to the model's window. The Conversation keeps its between-turn rule.
OMNIGENT_HELPER_TURN_CONTEXT_CEILING_ENV = "OMNIGENT_HELPER_TURN_CONTEXT_CEILING"

DEFAULT_HELPER_TURN_CONTEXT_CEILING = 300_000


def resolve_helper_turn_context_ceiling() -> int:
    """The in-turn context ceiling for a Helper turn.

    Reads :data:`OMNIGENT_HELPER_TURN_CONTEXT_CEILING_ENV`, default
    :data:`DEFAULT_HELPER_TURN_CONTEXT_CEILING`. A missing, non-integer or non-positive value
    falls back to the default.
    """
    raw = os.environ.get(OMNIGENT_HELPER_TURN_CONTEXT_CEILING_ENV)
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_HELPER_TURN_CONTEXT_CEILING


#: Fraction of the model's context window at which another turn would overflow it. At or past
#: this fill the warm client is dropped and the turn starts from the checkpoint plus the newest
#: whole turns that fit; nothing waits on a fold.
HARD_CONTEXT_WINDOW_FRACTION = 0.95


def exceeds_hard_context_limit(
    labels: Mapping[str, str] | None,
    *,
    context_tokens: int | None,
) -> bool:
    """Whether *context_tokens* is at/over the hard fraction of the session's window.

    ``False`` when the tokens or the session's reported window are unknown.
    """
    window = reported_context_window(labels)
    if context_tokens is None or window is None:
        return False
    return context_tokens >= int(window * HARD_CONTEXT_WINDOW_FRACTION)


#: What a new session costs at most, in tokens: system prompt and tools (4k + 9k), the working
#: list, turn lines, the person's words and the kept tail at their budgets (ADR 0031).
_ASSEMBLED_FIXED_TOKENS = 13_000 + 4_000 + 4_000 + 3_000

HandoffReason = Literal["size"]
#: Sessions already warned that their window is too small for a size handoff to help.
_small_window_warned: set[str] = set()


def assembled_estimate(labels: Mapping[str, str] | None) -> int:
    """The most a new session's first call carries, in tokens (the 2x rule's yardstick)."""
    return _ASSEMBLED_FIXED_TOKENS + resolve_keep_tokens(labels)


def handoff_reason(
    labels: Mapping[str, str] | None,
    *,
    context_tokens: int | None,
    conversation_id: str | None = None,
) -> HandoffReason | None:
    """Whether a warm client should be dropped before this turn (``"size"``), or ``None``.

    A warm session is replaced only when it needs compacting (ADR 0031, owner decision 4): its
    context reached the handoff threshold T and is over twice what a new session costs (else a
    small window would hand off every turn; that is logged once and the hard limit applies).
    A return after a break, a cache expiry and a model switch keep the warm session. The turn's
    own history load then assembles.

    :param context_tokens: The warm context the last turn reported, or ``None``.
    """
    if not is_superside_chat(labels) or context_tokens is None:
        return None
    threshold = resolve_rollover_threshold(labels)
    if context_tokens < threshold:
        return None
    if context_tokens > 2 * assembled_estimate(labels):
        return "size"
    if conversation_id and conversation_id not in _small_window_warned:
        _small_window_warned.add(conversation_id)
        logger.warning(
            "superside-chat %s: the handoff threshold (%d tokens) is below twice a new "
            "session's size (%d), so a size handoff would not help; the hard limit applies",
            conversation_id,
            threshold,
            2 * assembled_estimate(labels),
        )
    return None


# ---------------------------------------------------------------------------
# Build + persist the next rollover item
# ---------------------------------------------------------------------------

# Sessions with a fold being built and stored in this process. A second call for the same session
# (the engine's fold after another turn, or a /compact) is a no-op rather than racing the first;
# across processes the stale-anchor guard refuses the loser. See :func:`roll_over_session`.
_in_flight: set[str] = set()


class ItemsPageFetchError(Exception):
    """A page of ``GET .../items`` came back non-200 while scanning for rollover."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


#: Page size when reading the record from the checkpoint on (the route's maximum).
_RECORD_PAGE_LIMIT = "1000"
#: Page size when looking up the latest checkpoint (compaction items are rare).
_COMPACTION_LOOKUP_LIMIT = "20"


async def _get_items_page(
    server_client: httpx.AsyncClient, conversation_id: str, params: dict[str, str]
) -> dict[str, Any]:
    """One ``GET /v1/sessions/{id}/items`` page.

    :raises ItemsPageFetchError: the route answered non-200.
    """
    resp = await server_client.get(
        f"/v1/sessions/{conversation_id}/items", params=params, timeout=10.0
    )
    if resp.status_code != 200:
        raise ItemsPageFetchError(
            f"GET items for {conversation_id!r} returned {resp.status_code}", resp.status_code
        )
    return resp.json()


async def fetch_latest_checkpoint(
    server_client: httpx.AsyncClient,
    conversation_id: str,
    *,
    skip_placeholders: bool = False,
) -> dict[str, Any] | None:
    """The session's latest ``compaction`` item, or ``None`` when it has none.

    With *skip_placeholders*, a compaction whose summary is a harness stand-in
    (:func:`~omnigent.context.rollover.is_placeholder_summary`) holds no state
    and is passed over for the previous real one.

    :raises ItemsPageFetchError: a page came back non-200.
    """
    params = {"limit": _COMPACTION_LOOKUP_LIMIT, "order": "desc", "type": "compaction"}
    while True:
        page = await _get_items_page(server_client, conversation_id, params)
        data = page.get("data", [])
        for item in data:
            if item.get("type") != "compaction":
                continue
            if skip_placeholders and is_placeholder_summary(item.get("summary")):
                continue
            return item
        if not data or not page.get("has_more", False):
            return None
        params = {**params, "after": data[-1]["id"]}


async def iter_items_from_checkpoint(
    server_client: httpx.AsyncClient,
    conversation_id: str,
    *,
    skip_placeholders: bool = False,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Page the record from the latest checkpoint on, not from item 1.

    Yields pages (``limit=1000``, oldest first) that
    :func:`~omnigent.context.rollover.split_at_latest_compaction` splits exactly
    as it would split the whole record. A checkpoint's ``last_item_id`` is the
    last item it summarized; its kept tail lives in its own
    ``compacted_messages``, so the items still needed are the ones after that
    anchor (turns that ran while the summarizer worked, the compaction item
    itself, and everything since). The first page therefore starts after the
    anchor and carries a stand-in ``{"id": anchor}`` item in front, which is
    the only thing the split reads from it. An anchor the record does not hold
    (a native forwarder's synthetic id) reads after the compaction item
    instead, with the item itself in front. With no checkpoint the record is
    read from the start, as before. Placeholder checkpoints (see
    :func:`fetch_latest_checkpoint`) are left out of the yielded pages.

    :raises ItemsPageFetchError: a page came back non-200.
    """
    checkpoint = await fetch_latest_checkpoint(
        server_client, conversation_id, skip_placeholders=skip_placeholders
    )
    anchor = checkpoint.get("last_item_id") if checkpoint is not None else None
    prefix: list[dict[str, Any]] = []
    after: str | None = None
    if checkpoint is not None:
        after = anchor or checkpoint["id"]
        prefix = [{"id": anchor}] if anchor else [checkpoint]
    first = True
    while True:
        params = {"limit": _RECORD_PAGE_LIMIT, "order": "asc"}
        if after is not None:
            params["after"] = after
        try:
            page = await _get_items_page(server_client, conversation_id, params)
        except ItemsPageFetchError as exc:
            if first and anchor and exc.status_code == 400:
                after, anchor, prefix = checkpoint["id"], None, [checkpoint]
                continue
            raise
        data = page.get("data", [])
        if data:
            after = data[-1].get("id")
        if skip_placeholders:
            data = [
                it
                for it in data
                if not (
                    it.get("type") == "compaction" and is_placeholder_summary(it.get("summary"))
                )
            ]
        if data or (first and anchor is None and prefix):
            yield prefix + data
            prefix = []
        first = False
        if not page.get("data") or not page.get("has_more", False):
            return


async def _fetch_items_since_last_compaction(
    server_client: httpx.AsyncClient,
    conversation_id: str,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Read this session's record from its latest checkpoint and split it there.

    Reads through :func:`iter_items_from_checkpoint`, the same anchored read
    ``_load_history_as_input`` (``runner/app.py``) uses to cold-start a
    session, so a rollover costs the size of the epoch, not of the session. A
    placeholder checkpoint is not state: the read goes back to the previous
    real one, so the summary is carried forward and the turns a placeholder
    covered are summarized again.

    :returns: ``(items_since, previous_summary, previous_compaction_id)`` —
        the chronological items after the latest real ``compaction`` item (the
        whole record when there is none), that item's summary text, and its
        item id (all ``None`` for the session's first rollover).
    :raises ItemsPageFetchError: a page came back non-200 — the scan is
        incomplete, so the caller must not treat the partial result as the
        full record since the last compaction.
    """
    items: list[dict[str, Any]] = []
    async for page_items in iter_items_from_checkpoint(
        server_client, conversation_id, skip_placeholders=True
    ):
        items.extend(page_items)
    items_since, checkpoint = split_at_latest_compaction(items)
    if checkpoint is None:
        return items, None, None
    return items_since, checkpoint.get("summary"), checkpoint.get("id")


def _compaction_event_body(
    compaction: CompactionData, *, expected_previous_compaction_id: str | None
) -> dict[str, Any]:
    """Shape a :class:`CompactionData` into the ``POST .../events`` ``data`` body.

    Matches the shape ``runner/app.py``'s ``_handle_harness_compaction``
    posts for a native CLI's own compaction, so both producers write the
    same ``compaction`` item shape. ``expected_previous_compaction_id`` is
    the cross-process single-flight guard: the server rejects (409) this
    write if the session's latest compaction item id has since moved —
    always set (``None`` is a valid expectation: "no prior compaction"),
    so two processes racing the session's very first rollover are guarded
    too, not just later ones.
    """
    body: dict[str, Any] = {
        "type": "compaction",
        "summary": compaction.summary,
        "last_item_id": compaction.last_item_id,
        "model": compaction.model,
        "token_count": compaction.token_count,
        "expected_previous_compaction_id": expected_previous_compaction_id,
    }
    if compaction.compacted_messages:
        body["compacted_messages"] = compaction.compacted_messages
    for name in ("journal_lines", "journal_refs", "evicted_entries", "applied_closes", "repairs"):
        value = getattr(compaction, name)
        if value is not None:
            body[name] = value
    return body


def summarizer_model(
    model: str, connection: Mapping[str, str] | None, wire: str | None = None
) -> str:
    """
    The model string the summarizer is called with: provider-prefixed.

    A harness reports its model bare (``claude-sonnet-5-5``), and the LLM router reads a bare
    name as OpenAI's. When the session's own connection is known, its *wire* decides (the
    session's provider speaks Anthropic Messages, or Chat Completions), whatever the model is
    called: ``team-model`` on an Anthropic-wire connection is an Anthropic call. Without one (a
    session with no saved inference binding), a bare Claude model, or any bare model on an
    Anthropic base URL, is routed to the Anthropic adapter; anything already prefixed is left
    as it is.

    :param model: The turn's reported model.
    :param connection: The summarizer's connection overrides, if any.
    :param wire: The LLM client provider the session's connection speaks (``anthropic``,
        ``openai_compatible`` or ``openai``), from
        :func:`omnigent.llms.background.provider_connection`; ``None`` when unknown.
    :returns: ``provider/model``.
    """
    if wire is not None:
        return f"{wire}/{model}"
    if "/" in model:
        return model
    base_url = (connection or {}).get("base_url", "")
    if model.startswith("claude") or "anthropic.com" in base_url:
        return f"anthropic/{model}"
    return model


async def _current_labels(
    server_client: httpx.AsyncClient, conversation_id: str
) -> dict[str, str] | None:
    """The session's labels now, or ``None`` when they cannot be read (nothing is cleared)."""
    try:
        resp = await server_client.get(f"/v1/sessions/{conversation_id}/labels", timeout=10.0)
        labels = resp.json().get("labels") if resp.status_code == 200 else None
    except (httpx.HTTPError, RuntimeError, ValueError, AttributeError):
        return None
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else None


def _consumed_labels(
    closes: Mapping[str, str],
    adds: Mapping[str, str] | None,
    current: Mapping[str, str] | None,
) -> dict[str, str]:
    """The ``state_close`` and added-entry labels a stored checkpoint consumed, emptied.

    Labels are upsert-only, so "cleared" is an empty value. An added entry is cleared only when
    its label still holds the text the checkpoint wrote (compare and clear): a newer decision
    written while the summary was built stays pending for the next checkpoint.
    """
    consumed = {close_label_key(tag): "" for tag in closes}
    if adds:
        consumed.update(
            {
                entry_label_key(tag): ""
                for tag, text in adds.items()
                if current is not None and current.get(entry_label_key(tag), "").strip() == text
            }
        )
    return consumed


class SessionRecord(Protocol):
    """Where :func:`roll_over_session` reads a session's window and stores its checkpoint: the
    runner over HTTP (:class:`HttpRecord`), the engine over its store (:class:`StoreRecord`)."""

    async def window(self) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """``(items since the latest real checkpoint, its summary, its id)``.

        :raises ItemsPageFetchError: the record could not be read in full.
        """
        ...

    async def store(self, body: dict[str, Any]) -> bool:
        """Store a ``compaction`` event body; ``False`` when another checkpoint won the race or
        the write failed."""
        ...

    async def clear(self, closes: Mapping[str, str], adds: Mapping[str, str] | None) -> None:
        """Empty the labels the stored checkpoint consumed (:func:`_consumed_labels`)."""
        ...


class HttpRecord:
    """The record as the runner reaches it: the engine's session routes."""

    def __init__(self, server_client: httpx.AsyncClient, conversation_id: str) -> None:
        self._client = server_client
        self._id = conversation_id

    async def window(self) -> tuple[list[dict[str, Any]], str | None, str | None]:
        return await _fetch_items_since_last_compaction(self._client, self._id)

    async def store(self, body: dict[str, Any]) -> bool:
        try:
            resp = await self._client.post(
                f"/v1/sessions/{self._id}/events",
                json={"type": "compaction", "data": body},
                timeout=10.0,
            )
        except (httpx.HTTPError, RuntimeError):
            logger.warning(
                "superside-chat rollover: failed to persist the rollover item for %s",
                self._id,
                exc_info=True,
            )
            return False
        if resp.status_code == 409:
            # Someone else (another process for this same session) already rolled over past
            # the checkpoint this summary was built from: not a failure, just a lost race.
            return False
        if resp.status_code < 200 or resp.status_code >= 300:
            logger.warning(
                "superside-chat rollover: persisting the rollover item for %s returned %s",
                self._id,
                resp.status_code,
            )
            return False
        return True

    async def clear(self, closes: Mapping[str, str], adds: Mapping[str, str] | None) -> None:
        current = await _current_labels(self._client, self._id) if adds else None
        consumed = _consumed_labels(closes, adds, current)
        if not consumed:
            return
        try:
            await self._client.patch(
                f"/v1/sessions/{self._id}", json={"labels": consumed}, timeout=10.0
            )
        except (httpx.HTTPError, RuntimeError):
            # The close stays pending; the next checkpoint finds it already closed: harmless.
            logger.warning(
                "superside-chat rollover: failed to clear applied closes for %s",
                self._id,
                exc_info=True,
            )


class StoreRecord:
    """The record as the engine holds it: its conversation store, no HTTP hop (the fold)."""

    def __init__(self, conversation_store: Any, conversation_id: str) -> None:
        self._store = conversation_store
        self._id = conversation_id

    def _read(self) -> tuple[list[dict[str, Any]], str | None, str | None]:
        from omnigent.errors import StaleCursorError

        checkpoint: dict[str, Any] | None = None
        after: str | None = None
        while checkpoint is None:
            page = self._store.list_items(
                self._id, limit=20, after=after, order="desc", type="compaction"
            )
            checkpoint = next(
                (
                    item.to_api_dict()
                    for item in page.data
                    if not is_placeholder_summary(item.to_api_dict().get("summary"))
                ),
                None,
            )
            if checkpoint is None and (not page.data or not page.has_more):
                break
            after = page.data[-1].id if page.data else None
        anchor = checkpoint.get("last_item_id") if checkpoint is not None else None
        items: list[dict[str, Any]] = []
        prefix: list[dict[str, Any]] = []
        after = None
        if checkpoint is not None:
            after = anchor or checkpoint["id"]
            prefix = [{"id": anchor}] if anchor else [checkpoint]
        while True:
            try:
                page = self._store.list_items(self._id, limit=1000, after=after, order="asc")
            except StaleCursorError:
                if checkpoint is None or after == checkpoint["id"]:
                    raise
                # An anchor the record does not hold reads after the checkpoint itself.
                after, prefix, items = checkpoint["id"], [checkpoint], []
                continue
            items.extend(item.to_api_dict() for item in page.data)
            if not page.data or not page.has_more:
                break
            after = page.data[-1].id
        items = [
            it
            for it in prefix + items
            if not (it.get("type") == "compaction" and is_placeholder_summary(it.get("summary")))
        ]
        since, found = split_at_latest_compaction(items)
        if found is None:
            return items, None, None
        return since, found.get("summary"), found.get("id")

    async def window(self) -> tuple[list[dict[str, Any]], str | None, str | None]:
        import asyncio

        try:
            return await asyncio.to_thread(self._read)
        except Exception as exc:
            raise ItemsPageFetchError(f"record of {self._id!r} not read: {exc}", 500) from exc

    def _append(self, body: dict[str, Any]) -> bool:
        import uuid

        from omnigent.entities import NewConversationItem
        from omnigent.entities.conversation import parse_item_data

        # The stale-anchor guard the events route applies: a checkpoint built on one that is no
        # longer the latest is dropped, never stored over it.
        latest = self._store.list_items(self._id, limit=1, order="desc", type="compaction")
        latest_id = latest.data[0].id if latest.data else None
        if latest_id != body.get("expected_previous_compaction_id"):
            return False
        data = {k: v for k, v in body.items() if k != "expected_previous_compaction_id"}
        item = NewConversationItem(
            type="compaction",
            response_id=f"compact_{uuid.uuid4().hex}",
            data=parse_item_data("compaction", data),
        )
        self._store.append(self._id, [item])
        return True

    async def store(self, body: dict[str, Any]) -> bool:
        import asyncio

        try:
            return await asyncio.to_thread(self._append, body)
        except Exception:
            logger.warning("fold: could not store the checkpoint of %s", self._id, exc_info=True)
            return False

    async def clear(self, closes: Mapping[str, str], adds: Mapping[str, str] | None) -> None:
        import asyncio

        try:
            conv = await asyncio.to_thread(self._store.get_conversation, self._id)
            current = dict(conv.labels) if conv is not None else None
            consumed = _consumed_labels(closes, adds, current)
            if consumed:
                await asyncio.to_thread(self._store.set_labels, self._id, consumed)
        except Exception:
            logger.warning("fold: could not clear applied closes for %s", self._id, exc_info=True)


async def roll_over_session(
    conversation_id: str,
    *,
    labels: Mapping[str, str] | None,
    model: str,
    server_client: httpx.AsyncClient | None = None,
    record: SessionRecord | None = None,
    llm_client: Any = None,
    connection: dict[str, str] | None = None,
    on_rolled_over: Callable[[], Awaitable[None]] | None = None,
    wire: str | None = None,
    cause: CheckpointCause = "size",
    on_stored: Callable[[CompactionData, list[dict[str, Any]]], Awaitable[None]] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
) -> bool:
    """Fold the turns since the latest checkpoint, up to the newest ones, into the next one.

    Reads this session's record since the latest ``compaction`` item through *record* (else
    over *server_client*), leaves out the newest whole turns a new session shows in full (the
    kept tail, :func:`~omnigent.context.rollover.select_recent`) and at most
    :data:`~omnigent.context.rollover.FOLD_TURNS_MAX_CHARS` of the rest, oldest first, folds
    those into the next checkpoint (:func:`~omnigent.context.rollover.build_rollover_item`) and
    stores it anchored after the last of them. The newest turns stay after the anchor, so a new
    session's assembled block keeps them, and its turn lines shrink to what came after.

    The fold (``server/memory_upkeep.py``) leaves *on_rolled_over* unset: it only writes the
    checkpoint and never drops a warm client (ADR 0031). The manual ``/compact`` passes the
    runner's drop. Single-flight per *conversation_id*: a call that arrives while another is
    already building for the same session returns ``False`` at once. Every failure (history
    load, summarization, a cut-off reply, the store, a hook) is logged and swallowed, and the
    previous checkpoint stays.

    *connection* and *wire* say how the summarizer is called (see :func:`summarizer_model`).
    *cause* names the reason in the checkpoint's header. *on_stored* gets the stored item and
    the window it folded once it is persisted, before *on_rolled_over*. *on_failed* gets the
    error when building the checkpoint failed (the summarizer call, or a refused reply).

    :returns: ``True`` iff a new compaction item was stored.
    """
    import asyncio

    from omnigent.context.rollover import fold_window, select_recent
    from omnigent.models.model_fallbacks import ROLLOVER_TOKEN_COUNT_MODEL

    if conversation_id in _in_flight:
        return False
    if record is None:
        assert server_client is not None, "roll_over_session needs a record or a server client"
        record = HttpRecord(server_client, conversation_id)
    _in_flight.add(conversation_id)
    try:
        try:
            items_since, previous_summary, previous_compaction_id = await record.window()
        except (httpx.HTTPError, ValueError, ItemsPageFetchError):
            logger.warning(
                "superside-chat rollover: failed to load history for %s",
                conversation_id,
                exc_info=True,
            )
            return False

        # The newest whole turns stay after the anchor, as the history load shows them. Off
        # the loop: the engine's relay runs on it, and counting the tail tokenizes the window.
        def _window() -> list[dict[str, Any]]:
            kept = select_recent(
                items_since,
                keep_tokens=resolve_keep_tokens(labels),
                model=ROLLOVER_TOKEN_COUNT_MODEL,
            )
            return fold_window(items_since[: len(items_since) - len(kept)])

        window = await asyncio.to_thread(_window)
        if not window:
            return False
        closes = pending_closes(labels)
        adds = pending_entries(labels)
        try:
            compaction = await build_rollover_item(
                window,
                previous_summary=previous_summary,
                model=summarizer_model(model, connection, wire),
                llm_client=llm_client,
                connection=connection,
                conversation_id=conversation_id,
                cause=cause,
                pending_closes=closes,
                pending_entries=adds,
            )
        except Exception as exc:  # a rollover failure must never fail the turn
            logger.warning(
                "superside-chat rollover: failed to build the rollover item for %s",
                conversation_id,
                exc_info=True,
            )
            if on_failed is not None:
                on_failed(exc)
            return False
        body = _compaction_event_body(
            compaction, expected_previous_compaction_id=previous_compaction_id
        )
        if not await record.store(body):
            return False
        await record.clear(closes, adds)
        logger.info(
            "superside-chat rollover: stored a checkpoint for %s on %s: %d items folded, "
            "anchor %s, %d newer items kept after it",
            conversation_id,
            compaction.model,
            len(window),
            compaction.last_item_id,
            len(items_since) - len(window),
        )
        if on_stored is not None:
            try:
                await on_stored(compaction, window)
            except Exception:  # the item is already persisted; log and move on
                logger.warning(
                    "superside-chat rollover: on_stored failed for %s",
                    conversation_id,
                    exc_info=True,
                )
        if on_rolled_over is not None:
            try:
                await on_rolled_over()
            except Exception:  # the item is already persisted; log and move on
                logger.warning(
                    "superside-chat rollover: failed to drop the warm client for %s",
                    conversation_id,
                    exc_info=True,
                )
        return True
    finally:
        _in_flight.discard(conversation_id)


# ---------------------------------------------------------------------------
# The fold: when, and on which model (ADR 0031, "When the past is folded")
# ---------------------------------------------------------------------------

#: Settled-turn lines (tokens, as rendered) past which the turns are folded. Lower it (e.g. 200)
#: to see a fold on a short test chat.
OMNIGENT_FOLD_TURN_LINES_TOKENS_ENV = "OMNIGENT_FOLD_TURN_LINES_TOKENS"
DEFAULT_FOLD_TURN_LINES_TOKENS = 2_000
#: ``fold_on_light``: ``0`` folds on the session's own model instead of the owner's Light model.
OMNIGENT_FOLD_ON_LIGHT_ENV = "OMNIGENT_FOLD_ON_LIGHT"
#: Failed folds on Light after which a session folds on its own model.
_LIGHT_FAILURES_BEFORE_FALLBACK = 2
#: Failed Light calls in a row per session; cleared by a fold that lands. Bounded and
#: forgotten after six hours, so a gone session leaves nothing and an outage is retried on Light.
_light_failures: cachetools.TTLCache[str, int] = cachetools.TTLCache(maxsize=1024, ttl=6 * 3600)


def resolve_fold_turn_lines_tokens() -> int:
    """:data:`OMNIGENT_FOLD_TURN_LINES_TOKENS_ENV`, default 2k; invalid falls back."""
    raw = os.environ.get(OMNIGENT_FOLD_TURN_LINES_TOKENS_ENV)
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        value = 0
    return value if value > 0 else DEFAULT_FOLD_TURN_LINES_TOKENS


def fold_on_light() -> bool:
    """The ``fold_on_light`` flag (default on)."""
    return os.environ.get(OMNIGENT_FOLD_ON_LIGHT_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def fold_at(threshold: int) -> int:
    """The warm context at which the turns are folded: ``T − min(32k, 0.25 × T)``."""
    return threshold - min(32_000, threshold // 4)


def fold_due(
    items_since: list[dict[str, Any]],
    labels: Mapping[str, str] | None,
    *,
    context_tokens: int | None,
    model_window: int | None,
) -> str | None:
    """Why the turns since the checkpoint should be folded now, or ``None``.

    ``"turn lines"`` when the settled turns a new session would list one line each (those
    before the kept tail) pass :func:`resolve_fold_turn_lines_tokens`; ``"context"`` when the
    warm context reached :func:`fold_at` of the handoff threshold.
    """
    from omnigent.context.rollover import (
        select_recent,
        transcript_turn_lines,
        turn_lines_tokens,
    )
    from omnigent.models.model_fallbacks import ROLLOVER_TOKEN_COUNT_MODEL

    kept = select_recent(
        items_since, keep_tokens=resolve_keep_tokens(labels), model=ROLLOVER_TOKEN_COUNT_MODEL
    )
    retired = items_since[: len(items_since) - len(kept)]
    if not retired:
        return None
    lines = transcript_turn_lines(retired)
    if turn_lines_tokens(lines, ROLLOVER_TOKEN_COUNT_MODEL) > resolve_fold_turn_lines_tokens():
        return "turn lines"
    threshold = resolve_rollover_threshold(labels, model_window=model_window)
    if context_tokens is not None and context_tokens >= fold_at(threshold):
        return "context"
    return None


async def fold_session(
    conversation_store: Any,
    conversation_id: str,
    *,
    context_tokens: int | None,
    session_model: str | None,
) -> bool:
    """Fold a superside-chat session's past in the engine, if it is due (one session at a time).

    Runs in the background after a turn, never in it: it reads the record from the store,
    checks :func:`fold_due` and calls :func:`roll_over_session` on the owner's Light model for
    the session's harness, over the session's own provider connection (``resolve_session_llm``;
    keys never reach the host), with ``on_rolled_over`` unset, so the warm client is never
    dropped. With ``fold_on_light`` off, no Light model, or after two failed Light calls in a
    row, it folds on *session_model* over that same connection. The folded turns reach recall
    through the runner's backfill, which follows the anchor.

    :returns: ``True`` iff a checkpoint was stored.
    """
    import asyncio
    import functools

    from omnigent.context.labels import is_superside_chat
    from omnigent.llms.background import resolve_session_llm
    from omnigent.llms.client import Client
    from omnigent.llms.context_window import find_model_context_window

    if conversation_id in _in_flight:
        return False
    conv = await asyncio.to_thread(conversation_store.get_conversation, conversation_id)
    if conv is None or not is_superside_chat(conv.labels):
        return False
    record = StoreRecord(conversation_store, conversation_id)
    try:
        items_since, _, _ = await record.window()
    except ItemsPageFetchError:
        logger.warning("fold: could not read %s", conversation_id, exc_info=True)
        return False
    window = None
    if session_model:
        try:
            window = await asyncio.to_thread(find_model_context_window, session_model)
        except Exception:  # noqa: BLE001 — a miss keeps the label or the default
            window = None
    # Off the loop: counting the tail tokenizes the window.
    reason = await asyncio.to_thread(
        functools.partial(
            fold_due,
            items_since,
            conv.labels,
            context_tokens=context_tokens,
            model_window=window,
        )
    )
    if reason is None:
        return False
    owner = await asyncio.to_thread(conversation_store.get_session_owner, conversation_id)
    snapshot = conv.inference_snapshot
    light = fold_on_light() and (
        _light_failures.get(conversation_id, 0) < _LIGHT_FAILURES_BEFORE_FALLBACK
    )
    # Both over the session's own connection: its words never reach another provider, tenant
    # or region than the Conversation's own (ADR 0031, "Privacy").
    route = await resolve_session_llm(owner, snapshot) if light else None
    on_light = route is not None
    if route is None and session_model:
        route = await resolve_session_llm(owner, snapshot, session_model)
    if route is None:
        logger.info("fold: no model to fold %s on; skipped", conversation_id)
        return False
    model, connection = route
    logger.info(
        "fold: folding %s on %s (%s; %s)",
        conversation_id,
        model,
        "Light" if on_light else "the session's model",
        reason,
    )
    errors: list[Exception] = []
    stored = await roll_over_session(
        conversation_id,
        labels=conv.labels,
        model=model,
        record=record,
        llm_client=Client(),
        connection=connection,
        on_failed=errors.append,
    )
    if on_light:
        # Only a failed Light call counts (not a refused reply, a lost race or an empty
        # window); a fold that lands clears the count.
        if stored:
            _light_failures.pop(conversation_id, None)
        elif any(not isinstance(error, ValueError) for error in errors):
            _light_failures[conversation_id] = _light_failures.get(conversation_id, 0) + 1
    return stored
