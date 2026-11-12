"""A person's three model picks per harness: **Foundation**, **Builder** and **Light** (ADR 0027).

All three come from the person's own catalog for the harness (what their connection serves), never
from a model name in configuration. A person's explicit pick in Settings, Models (stored in
:class:`~omnigent.superchat.models.selection.ModelPreferenceStore`) always wins while the catalog
serves it; otherwise the automatic choice applies:

* **Foundation** (the Assistant, judgement): the default the connection's settings name, else the
  dearest priced model.
* **Builder** (helpers that write): the middle priced model; Foundation's when fewer than three
  models are priced.
* **Light** (titles, summaries, rerank, fetch-and-compare helpers): the cheapest priced model.

Prices order by input price, then output price (the price the connection reports for the model,
else the shared model catalog's; ties keep catalog order). With nothing priced all three are the
catalog's default, the connection's own.

A Super Chat helper names ``light``, ``builder`` or ``foundation`` (``fast`` and ``strong`` are
:data:`ALIASES` for Light and Foundation, for one release). Background work (memory upkeep, the
daily note, Dreaming, reranking) runs on Light (:mod:`.background`). The runner (inside a host, no
database) asks ``GET /v1/sessions/{id}/helper-models`` (:mod:`.selection`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omnigent.errors import OmnigentError
from omnigent.inference_config import normalize_inference_harness

_logger = logging.getLogger(__name__)

#: The three categories, most capable first: the one definition of their names.
CATEGORIES = ("foundation", "builder", "light")
#: Earlier helper choices, accepted for one release: ``fast`` is Light, ``strong`` Foundation.
ALIASES = {"fast": "light", "strong": "foundation"}


def category_of(name: str) -> str | None:
    """The category *name* is (resolving an alias), or ``None`` when it names none."""
    category = ALIASES.get(name, name)
    return category if category in CATEGORIES else None


Price = tuple[float, float]


@dataclass(frozen=True)
class PersonModels:
    """One person's effective models on one harness (``None``: the catalog is not usable)."""

    foundation: str | None
    builder: str | None
    light: str | None
    #: What the person picked themselves per category (``None``: automatic).
    picks: Mapping[str, str | None] = field(default_factory=dict)
    #: What each category is when the person has not picked one.
    automatic: Mapping[str, str | None] = field(default_factory=dict)

    def model(self, category: str) -> str | None:
        """The model *category* resolves to."""
        return getattr(self, category) if category in CATEGORIES else None


def catalog_price(model: str) -> Price | None:
    """``(input, output)`` USD per token from the shared model catalog, or ``None``. Blocking."""
    from omnigent.llms.context_window import fetch_model_pricing

    try:
        pricing = fetch_model_pricing(model)
    except Exception:  # noqa: BLE001 - an unreachable catalog only means "unpriced"
        return None
    return (pricing.input_per_token, pricing.output_per_token) if pricing is not None else None


def row_price(row: Mapping[str, Any], fallback: Callable[[str], Price | None]) -> Price | None:
    """The price the connection reported for a catalog *row*, else *fallback* (the catalog's)."""
    reported = row.get("costPerToken")
    if isinstance(reported, Mapping):
        given, out = reported.get("input"), reported.get("output")
        if isinstance(given, (int, float)) and not isinstance(given, bool):
            if not isinstance(out, (int, float)) or isinstance(out, bool):
                out = float("inf")
            return float(given), float(out)
    return fallback(row["id"])


def priced_rows(
    rows: Sequence[Mapping[str, Any]], price: Callable[[str], Price | None] | None = None
) -> list[str]:
    """The priced ids of a ready catalog's *rows*, cheapest first (input price, then output
    price, then catalog order). Blocking."""
    lookup = price or catalog_price
    priced = [
        (cost, index, row["id"])
        for index, row in enumerate(rows)
        if (cost := row_price(row, lookup)) is not None
    ]
    return [model for _, _, model in sorted(priced)]


def cheapest_priced(
    rows: Sequence[Mapping[str, Any]], price: Callable[[str], Price | None] | None = None
) -> str | None:
    """The cheapest priced id of a ready catalog's *rows*, or ``None`` when no row is priced.
    Blocking."""
    ordered = priced_rows(rows, price)
    return ordered[0] if ordered else None


def automatic_models(
    rows: Sequence[Mapping[str, Any]],
    default: str | None,
    price: Callable[[str], Price | None] | None = None,
) -> dict[str, str | None]:
    """``{category: id}`` when the person picked nothing (see the module docstring). Blocking."""
    ordered = priced_rows(rows, price)
    if not ordered:
        fallback = default if default is not None else (rows[0]["id"] if rows else None)
        return dict.fromkeys(CATEGORIES, fallback)
    foundation = ordered[-1]
    return {
        "foundation": foundation,
        "builder": ordered[(len(ordered) - 1) // 2] if len(ordered) >= 3 else foundation,
        "light": ordered[0],
    }


def person_models_from_catalog(
    catalog: Mapping[str, Any],
    picks: Mapping[str, str | None],
    price: Callable[[str], Price | None] | None = None,
) -> PersonModels:
    """Resolve the three categories from a catalog and the person's saved *picks*
    (``{category: id}``). Blocking."""
    chosen = {category: picks.get(category) for category in CATEGORIES}
    if catalog.get("status") != "ready":
        return PersonModels(None, None, None, chosen, dict.fromkeys(CATEGORIES))
    rows = catalog.get("models") or []
    served = {row["id"] for row in rows}
    catalog_default = catalog.get("default_model") or (rows[0]["id"] if rows else None)
    automatic = automatic_models(rows, catalog_default, price)
    if catalog.get("default_configured"):
        automatic["foundation"] = catalog_default
    resolved = {
        category: chosen[category] if chosen[category] in served else automatic[category]
        for category in CATEGORIES
    }
    return PersonModels(**resolved, picks=chosen, automatic=automatic)


def _owner(owner: str | None) -> str:
    from omnigent.server.auth import RESERVED_USER_LOCAL

    return owner or RESERVED_USER_LOCAL


async def person_models(app_state: Any, owner: str | None, harness: str) -> PersonModels:
    """*owner*'s three models on *harness*, from the catalog a new session would get."""
    from omnigent.server.inference_catalog import SandboxInferenceService

    harness = normalize_inference_harness(harness)
    service = getattr(app_state, "inference_catalog", None) or SandboxInferenceService(app_state)
    try:
        snapshot = await service.prepare(None, harness, owner)
    except (OmnigentError, ValueError) as exc:
        # No key, no reachable catalog: nothing to resolve yet.
        _logger.info("no models for %s: %s", harness, type(exc).__name__)
        snapshot = None
    catalog = (snapshot or {}).get("catalog") or {}
    store = getattr(app_state, "model_preference_store", None)
    saved = await asyncio.to_thread(store.read, _owner(owner)) if store is not None else None
    picks = saved.for_harness(harness) if saved is not None else {}
    return await asyncio.to_thread(person_models_from_catalog, catalog, picks)


async def tier_models(
    app_state: Any, owner: str | None, harness: str | None
) -> dict[str, str | None]:
    """``{category: id}`` for *owner* on *harness* (``None``: inherit the parent's model)."""
    if not harness or app_state is None:
        return dict.fromkeys(CATEGORIES)
    models = await person_models(app_state, owner, harness)
    return {category: models.model(category) for category in CATEGORIES}
