"""Embeddings through the model layer: the person's (or organization's) own connection.

Anything that needs vectors (file search today, memory later) asks :class:`ModelEmbeddings`. It
resolves the same way a run does, the person's own connection first, then the organization's,
among the providers whose upstream serves embeddings (:attr:`Upstream.embeddings`); it never falls
back to an operator key. A suspended person, a used-up "stop" budget, no embedding-capable
connection or a provider failure raises :class:`EmbeddingUnavailable` with a short ``reason``, so
the caller can fall back to keyword search and say why. The call goes through ``litellm.embedding``
with that key and base URL, and the cost is added to the person's daily rollup, the same row the
model proxy feeds.

The organization chooses the model (a small JSON row in ``preferences`` under
:data:`WORKSPACE_OWNER`): default ``text-embedding-3-small`` cut to 512 dimensions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.superchat.models.budget import get_budgets
from omnigent.superchat.models.org import WORKSPACE_OWNER, get_suspensions
from omnigent.superchat.models.spend import Usage, cost_usd
from omnigent.superchat.models.store import ModelConnectionStore, resolve_model_connection
from omnigent.superchat.models.upstreams import UPSTREAMS

_logger = logging.getLogger(__name__)

SETTING_KEY = "embedding_model"
DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
DEFAULT_EMBEDDING_DIMENSIONS = 512
#: Texts per ``embedding`` call (providers cap a request at 2048 inputs; this keeps bodies small).
BATCH_SIZE = 64
#: Providers that can embed, best first.
EMBEDDING_PROVIDERS = tuple(name for name, upstream in UPSTREAMS.items() if upstream.embeddings)

REASON_NO_CONNECTION = "no_connection"
REASON_SUSPENDED = "suspended"
REASON_BUDGET = "budget"
REASON_ERROR = "error"


class EmbeddingUnavailable(Exception):
    """Embeddings cannot be made now; ``reason`` is one of the ``REASON_*`` codes."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class EmbeddingSetting:
    """The organization's embedding model.

    :param model: Model id as the provider spells it, e.g. ``openai/text-embedding-3-small``.
    :param dimensions: Vector size to request, or ``None`` for the model's own size.
    """

    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int | None = DEFAULT_EMBEDDING_DIMENSIONS


def parse_setting(raw: Any) -> EmbeddingSetting:
    """A stored setting, falling back to the defaults for anything malformed (never raises)."""
    if not isinstance(raw, dict):
        return EmbeddingSetting()
    model = raw.get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_EMBEDDING_MODEL
    if "dimensions" in raw and raw["dimensions"] is None:
        return EmbeddingSetting(model=model, dimensions=None)
    dims = raw.get("dimensions")
    ok = isinstance(dims, int) and not isinstance(dims, bool) and 8 <= dims <= 8192
    return EmbeddingSetting(model=model, dimensions=dims if ok else DEFAULT_EMBEDDING_DIMENSIONS)


class EmbeddingSettingStore:
    """The organization's embedding model: one JSON row under :data:`WORKSPACE_OWNER`."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        prefix = "omnigent.embedding_setting"
        self._session = make_named_managed_session_maker(engine, query_name_prefix=prefix)
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix=prefix, immediate=True
        )

    def get(self) -> EmbeddingSetting:
        """The setting; the defaults when never set or unreadable."""
        with self._session("select_embedding_setting") as session:
            row = session.get(
                SqlPreference, (current_workspace_id(), WORKSPACE_OWNER, SETTING_KEY)
            )
            raw = row.value if row is not None else None
        try:
            return parse_setting(json.loads(raw)) if raw else EmbeddingSetting()
        except ValueError:
            return EmbeddingSetting()

    def set(self, setting: EmbeddingSetting | None) -> None:
        """Replace the setting; ``None`` removes it (back to the defaults)."""

        def write(session: Any) -> None:
            pk = (current_workspace_id(), WORKSPACE_OWNER, SETTING_KEY)
            row = session.get(SqlPreference, pk)
            if setting is None:
                if row is not None:
                    session.delete(row)
                return
            value = json.dumps({"model": setting.model, "dimensions": setting.dimensions})
            if row is None:
                session.add(SqlPreference(user_id=WORKSPACE_OWNER, key=SETTING_KEY, value=value))
            else:
                row.value = value

        run_write_transaction(self._session_immediate, "upsert_embedding_setting", write)


@dataclass(frozen=True)
class EmbeddingResult:
    """Vectors for the texts asked, in order.

    :param vectors: One float vector per text.
    :param tag: Identifies the model and size (``provider:model:dims``); stored beside each
        vector so a changed setting is noticed.
    :param tokens: Input tokens billed.
    :param cost_usd: What the call cost (0.0 when the model is unpriced).
    """

    vectors: list[list[float]]
    tag: str
    tokens: int
    cost_usd: float


@dataclass(frozen=True)
class EmbeddingPlan:
    """How a host should embed for its owner through the model proxy.

    :param provider: Upstream the proxy forwards to (the ``{provider}`` of its route).
    :param model: Model id as the provider spells it.
    :param dimensions: Vector size to request, or ``None`` for the model's own.
    :param tag: ``provider:model:dims``, stored beside each vector.
    """

    provider: str
    model: str
    dimensions: int | None
    tag: str


def _load_litellm() -> Any:
    """Import litellm lazily (it ships with the ``memory`` extra); tests replace this."""
    try:
        import litellm
    except ImportError as exc:
        raise EmbeddingUnavailable(
            REASON_ERROR, "Embeddings need the 'memory' extra (litellm)."
        ) from exc
    return litellm


def _tag(provider: str, setting: EmbeddingSetting) -> str:
    return f"{provider}:{setting.model}:{setting.dimensions or 0}"


def _priced(tokens: int, model: str, provider_cost: float | None) -> float:
    """The call's charge: the provider's figure, else catalog rates, else litellm's price map."""
    if provider_cost is not None:
        return provider_cost
    bare = model.split("/")[-1]
    cost = cost_usd(Usage(input=tokens, model=model), model, bare)
    if cost is not None:
        return cost
    try:
        prompt_cost, _ = _load_litellm().cost_per_token(
            model=bare, prompt_tokens=tokens, completion_tokens=0
        )
        return float(prompt_cost)
    except Exception:  # noqa: BLE001 - an unpriced model costs nothing rather than failing
        return 0.0


class ModelEmbeddings:
    """Resolves the connection that embeds for an owner and makes the calls."""

    def __init__(self, connections: ModelConnectionStore, settings: EmbeddingSettingStore) -> None:
        self._connections = connections
        self._settings = settings

    def setting(self) -> EmbeddingSetting:
        """The organization's embedding model."""
        return self._settings.get()

    def settings_store(self) -> EmbeddingSettingStore:
        """The store behind :meth:`setting` (the admin route writes it)."""
        return self._settings

    def available(self, owner_id: str) -> str | None:
        """Why *owner_id* cannot embed (a ``REASON_*`` code), or ``None`` when they can."""
        try:
            self._check(owner_id)
            self._connection(owner_id)
        except EmbeddingUnavailable as exc:
            return exc.reason
        return None

    def tag(self, owner_id: str) -> str | None:
        """The model tag vectors for *owner_id* would carry, or ``None`` when they cannot embed."""
        try:
            connection = self._connection(owner_id)
        except EmbeddingUnavailable:
            return None
        return _tag(connection.provider, self._settings.get())

    def plan(self, owner_id: str) -> EmbeddingPlan:
        """The provider and model *owner_id*'s host embeds with through the model proxy.

        Same resolution, suspension and budget checks as :meth:`embed`; the call itself and its
        cost then go through the proxy.

        :raises EmbeddingUnavailable: no connection, suspended or budget used up.
        """
        self._check(owner_id)
        connection = self._connection(owner_id)
        setting = self._settings.get()
        return EmbeddingPlan(
            connection.provider,
            setting.model,
            setting.dimensions,
            _tag(connection.provider, setting),
        )

    def check(self, owner_id: str) -> None:
        """Refuse a suspended owner or one whose "stop" budget is used up (shared by every
        helper that calls a model for a host).

        :raises EmbeddingUnavailable: ``suspended`` or ``budget``.
        """
        self._check(owner_id)

    def _check(self, owner_id: str) -> None:
        suspensions = get_suspensions()
        if suspensions is not None and suspensions.is_suspended(owner_id):
            raise EmbeddingUnavailable(REASON_SUSPENDED, "The account is suspended.")
        budgets = get_budgets()
        if budgets is not None:
            breach = budgets.stop_breach(owner_id)
            if breach is not None:
                raise EmbeddingUnavailable(REASON_BUDGET, breach.message)

    def _connection(self, owner_id: str) -> Any:
        connection = resolve_model_connection(
            self._connections, owner_id=owner_id, preferred=EMBEDDING_PROVIDERS
        )
        if connection is None:
            raise EmbeddingUnavailable(
                REASON_NO_CONNECTION,
                "Add an OpenRouter or LiteLLM key in Settings to search file contents.",
            )
        return connection

    def embed(self, owner_id: str, texts: list[str]) -> EmbeddingResult:
        """Embed *texts* with *owner_id*'s connection (blocking; run it off the event loop).

        :raises EmbeddingUnavailable: no connection, suspended, budget used up or the call failed.
        """
        self._check(owner_id)
        connection = self._connection(owner_id)
        upstream = UPSTREAMS[connection.provider]
        route, api_base = upstream.embeddings, upstream.embeddings_api_base
        assert route is not None and api_base is not None
        setting = self._settings.get()
        litellm = _load_litellm()
        vectors: list[list[float]] = []
        tokens = 0
        total_cost = 0.0
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            kwargs: dict[str, Any] = {
                "model": f"{route.model_prefix}{setting.model}",
                "input": batch,
                "api_key": connection.plaintext,
                "api_base": api_base,
            }
            if setting.dimensions:
                kwargs["dimensions"] = setting.dimensions
            try:
                response = litellm.embedding(**kwargs)
            except Exception as exc:  # noqa: BLE001 - the type is logged, never the text
                _logger.warning("embedding call failed (%s)", type(exc).__name__)
                raise EmbeddingUnavailable(
                    REASON_ERROR, "The embedding provider could not be reached."
                ) from None
            data = sorted(_field(response, "data") or [], key=lambda item: _field(item, "index"))
            if len(data) != len(batch):
                raise EmbeddingUnavailable(REASON_ERROR, "The provider returned too few vectors.")
            vectors.extend([float(x) for x in _field(item, "embedding")] for item in data)
            usage = _field(response, "usage") or {}
            used = int(_field(usage, "prompt_tokens") or _field(usage, "total_tokens") or 0)
            cost = _priced(used, setting.model, _field(usage, "cost"))
            tokens += used
            total_cost += cost
            self._record(owner_id, cost)
        return EmbeddingResult(vectors, _tag(connection.provider, setting), tokens, total_cost)

    @staticmethod
    def _record(owner_id: str, cost: float) -> None:
        """Add the call's cost to the owner's daily rollup; a failure never fails the call."""
        budgets = get_budgets()
        if budgets is None or not cost:
            return
        try:
            budgets.record_spend(owner_id, cost)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("embedding spend not recorded (%s)", type(exc).__name__)


def _field(obj: Any, name: str) -> Any:
    """``obj[name]`` for a dict, else the attribute (litellm returns objects that do both)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


_embeddings: ModelEmbeddings | None = None


def bind_embeddings(embeddings: ModelEmbeddings | None) -> None:
    """Bind the server's embeddings so other capabilities can find them (``None`` unbinds)."""
    global _embeddings
    _embeddings = embeddings


def get_embeddings() -> ModelEmbeddings | None:
    """The bound embeddings, or ``None`` when the server has no model connections."""
    return _embeddings
