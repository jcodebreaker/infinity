"""What a proxied model call cost, read from the response as it streams past.

The proxy is the one source of truth for the spend of traffic it serves (the per-turn pricing in
the session relay skips those sessions, see :func:`omnigent.server.inference_catalog
.snapshot_uses_model_proxy`). :class:`UsageTap` watches the bytes the proxy forwards, never holds
or delays them, and keeps only the ``usage`` figures:

* OpenRouter reports the real charge as ``usage.cost`` (Chat Completions with ``usage.include``;
  the final SSE chunk carries it when streaming).
* Everything else is priced from token counts with the catalog's per-token rates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from omnigent.llms.context_window import ModelPricing, compute_llm_cost, fetch_model_pricing

_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_LINE_BYTES = 1024 * 1024


@dataclass
class Usage:
    """Figures seen on a response; ``input`` excludes cached tokens (Anthropic's convention)."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float | None = None
    model: str | None = None

    def merge(self, raw: Any) -> None:
        """Take every figure *raw* (a ``usage`` object, either wire format) carries."""
        if not isinstance(raw, dict):
            return
        cache_read = _count(raw.get("cache_read_input_tokens"))
        details = raw.get("prompt_tokens_details")
        if cache_read is None and isinstance(details, dict):
            cache_read = _count(details.get("cached_tokens"))
        prompt = _count(raw.get("prompt_tokens"))
        if cache_read is not None:
            self.cache_read = cache_read
        if prompt is not None:  # OpenAI style: prompt_tokens includes the cached ones
            self.input = max(prompt - self.cache_read, 0)
        for field, key in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("output", "completion_tokens"),
            ("cache_write", "cache_creation_input_tokens"),
        ):
            value = _count(raw.get(key))
            if value is not None:
                setattr(self, field, value)
        cost = raw.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            if math.isfinite(cost) and cost >= 0:
                self.cost = float(cost)

    @property
    def seen(self) -> bool:
        return self.cost is not None or bool(self.input or self.output)


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 else None


class UsageTap:
    """Feed it the forwarded response bytes; :meth:`finish` returns what it saw."""

    def __init__(self, content_type: str) -> None:
        self._sse = "text/event-stream" in content_type.lower()
        self._json = "json" in content_type.lower()
        self._tail = b""
        self._body = bytearray()
        self._usage = Usage()

    def feed(self, chunk: bytes) -> None:
        """Observe one forwarded chunk. Never raises: a malformed stream costs only the figures."""
        try:
            if self._sse:
                self._feed_sse(chunk)
            elif self._json and len(self._body) < _MAX_JSON_BYTES:
                self._body.extend(chunk)
        except Exception:  # noqa: BLE001 - observing must never break the stream
            self._tail = b""

    def _feed_sse(self, chunk: bytes) -> None:
        *lines, self._tail = (self._tail + chunk).split(b"\n")
        if len(self._tail) > _MAX_LINE_BYTES:
            self._tail = b""
        for line in lines:
            if line.startswith(b"data:") and b'"usage"' in line:
                self._absorb(line[5:].strip())

    def _absorb(self, payload: bytes) -> None:
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        model = obj.get("model") or message.get("model")
        if isinstance(model, str):
            self._usage.model = model
        # message_start (input, cache) then message_delta (output) on Anthropic's stream.
        self._usage.merge(message.get("usage"))
        self._usage.merge(obj.get("usage"))

    def finish(self) -> Usage | None:
        """The usage seen, or ``None`` when the response carried none."""
        if self._tail:
            self.feed(b"\n")
        if self._body:
            self._absorb(bytes(self._body))
        return self._usage if self._usage.seen else None


def cost_usd(usage: Usage, *models: str | None) -> float | None:
    """The charge for *usage*: the provider's own figure, else tokens x catalog rates.

    :param models: Candidate model ids for the catalog lookup, best first (the response's, then
        the request's).
    :returns: ``None`` when neither is known (an unpriced model).
    """
    if usage.cost is not None:
        return usage.cost
    for model in (usage.model, *models):
        pricing: ModelPricing | None = fetch_model_pricing(model) if model else None
        if pricing is not None:
            return compute_llm_cost(
                {
                    "input_tokens": usage.input,
                    "output_tokens": usage.output,
                    "cache_read_input_tokens": usage.cache_read,
                    "cache_creation_input_tokens": usage.cache_write,
                },
                pricing,
            )
    return None
