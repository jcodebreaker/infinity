"""A new deck's look: the model's template is built; the engine chooses only when there is none.

ADR 0026 (amending ADR 0025): ``look_from`` is a record, never a gate. ``deck_new`` builds any
valid ``template`` the model passed, whatever ``look_from`` says, and records ``look_from`` as
given (one of :data:`LOOK_SOURCES`, else :data:`MODEL`). Only when ``template`` is missing does
the engine settle it: the theme the person picked on the Round's card (:data:`PICKED`, read from
the option id), else its own choice (:func:`choose_look`, from the topic and the person's taste,
:data:`CHOSEN`), and the result says which look it used and why. Nothing here forces a second
call.

The one refusal is :data:`HELPER_REFUSAL`: background work (a scheduled or ad-hoc Helper run, or
a scheduled fire) with no template and no topic to choose from.

The latest message, the card answer the Round's message carried and the background flag come
from the server's view of the Conversation (a Helper's: the chat that started it, and the Round
it was started in), see :mod:`omnigent.superchat.tasks`; it is read only when there is no
template.

Limits: matching is on words, case-insensitive. A mood word in passing ("the tech team") counts
as naming that mood.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from omnigent.superchat.decks import kit
from omnigent.superchat.decks.tools import LOOK_SOURCES
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.prompt_prefix import fetch_memory_profile
from omnigent.superchat.tasks.runtime import read_tasks

#: Mood words the person may use instead of a theme name -> the themes that mood fits. The one
#: list: the deck rules tell the Muse to name a theme for a mood, or leave the choice here.
MOODS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(
        ("formal", "board", "bank report", "finance", "management"),
        ("corporate-clean", "blue-professional", "swiss-grid", "arctic-cool"),
    ),
    **dict.fromkeys(("minimal", "clean", "simple", "calm"), ("minimal-white", "japanese-minimal")),
    **dict.fromkeys(("dark", "night", "tech", "developer"), ("nord", "tokyo-night")),
    **dict.fromkeys(("academic", "research", "paper", "thesis"), ("academic-paper",)),
    **dict.fromkeys(
        ("editorial", "magazine", "storytelling", "narrative"),
        ("editorial-serif", "magazine-mono", "cartesian", "magazine-bold"),
    ),
    **dict.fromkeys(
        ("fun", "playful", "bold", "colourful", "colorful", "creative"),
        ("bauhaus", "midcentury", "editorial-tri-tone", "sharp-mono"),
    ),
    **dict.fromkeys(("pitch", "investors", "startup"), ("pitch-deck-vc",)),
}

HELPER_REFUSAL = (
    "The person hasn't chosen a look and the request has no topic to choose one from. Stop and "
    "report back that the look must be settled first; do not pick one."
)
#: Recorded as ``look_from`` when the engine chose the look.
CHOSEN = "chosen"
#: Recorded as ``look_from`` when the model passed a template and did not say where it came from.
MODEL = "model"
#: Recorded as ``look_from`` for the theme the person picked on the Round's card.
PICKED = "picked"
#: The budget for each server read the engine's choice needs (ADR 0026: bounded metadata calls).
_READ_BUDGET = 2.0


def _has(text: str, phrase: str) -> bool:
    """Whether *phrase* occurs in *text* as whole words (both already lower-case)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _named(text: str, theme_id: str) -> bool:
    template = kit.templates()[theme_id]
    return any(
        _has(text, n) for n in {template.name.lower(), theme_id, theme_id.replace("-", " ")}
    )


def named_themes(text: str) -> set[str]:
    """Themes *text* names outright (name or id)."""
    low = text.lower()
    return {theme_id for theme_id in kit.templates() if _named(low, theme_id)}


def mood_themes(text: str) -> set[str]:
    """Themes a mood word in *text* fits."""
    low = text.lower()
    return {t for mood, themes in MOODS.items() if _has(low, mood) for t in themes}


#: What a deck is about -> the kind of look that suits it. Words match whole and lower-case, digits
#: included, so "2026 sales" finds "sales" and "Q3" finds "q3".
TOPICS: dict[str, tuple[str, ...]] = {
    "professional": (
        *("sales", "revenue", "revenues", "quarter", "quarterly", "q1", "q2", "q3", "q4"),
        *("finance", "financial", "budget", "budgets", "board", "kpi", "kpis", "report"),
        *("review", "forecast", "metrics", "results", "earnings", "operations", "strategy"),
        *("audit", "compliance", "bank", "client", "clients", "annual", "growth", "pipeline"),
        *("profit", "margin", "plan", "hiring", "management", "update"),
    ),
    "academic": (
        *("research", "study", "paper", "thesis", "lecture", "science", "scientific"),
        *("university", "seminar", "literature", "methodology", "academic", "course"),
    ),
    "editorial": (
        *("travel", "trip", "culture", "food", "story", "history", "art", "city", "museum"),
        *("book", "writing", "festival", "wine", "journey", "heritage", "magazine"),
    ),
    "bold": (
        *("pitch", "launch", "startup", "investor", "investors", "product", "campaign"),
        *("marketing", "brand", "fundraising", "event", "kickoff", "announcement"),
    ),
    "dark": (
        *("tech", "technical", "engineering", "engineers", "developer", "developers", "api"),
        *("architecture", "platform", "security", "infrastructure", "software", "code", "ai"),
    ),
}
#: Each kind's looks, best fit first: a sales deck gets Corporate Clean or Blue Professional (the
#: first two rank alike; the request breaks the tie).
LEADS: dict[str, tuple[str, ...]] = {
    "professional": (
        *("corporate-clean", "blue-professional", "swiss-grid", "arctic-cool", "minimal-white"),
    ),
    "academic": ("academic-paper", "minimal-white", "swiss-grid"),
    "editorial": (
        *("editorial-serif", "magazine-bold", "cartesian", "japanese-minimal", "magazine-mono"),
    ),
    "bold": ("pitch-deck-vc", "bauhaus", "midcentury", "editorial-tri-tone", "sharp-mono"),
    "dark": ("tokyo-night", "nord", "sharp-mono", "magazine-mono"),
}
#: The kind a request with no topic word gets: decks.md's default is a professional look.
DEFAULT_KIND = "professional"
_STOP = {"with", "from", "that", "this", "your", "deck", "slides", "make", "about", "into"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - _STOP


def _turn(seed: str, theme_id: str) -> str:
    """A stable tiebreak: the same request ranks equal themes the same way, others differently."""
    return hashlib.sha256(f"{seed}|{theme_id}".encode()).hexdigest()


def topic_kinds(hint: str) -> list[str]:
    """The kinds of look *hint*'s topic words call for, most words first (none: the default)."""
    words = _words(hint)
    hits = {kind: len(words & set(keys)) for kind, keys in TOPICS.items()}
    return sorted((k for k, n in hits.items() if n), key=lambda k: -hits[k]) or [DEFAULT_KIND]


def rank(hint: str, *, seed: str = "") -> list[str]:
    """Every theme for *hint* (the deck's title and request), best fit first: the one ranking.

    A named theme counts most, then a mood word (:data:`MOODS`), then the topic's kind
    (:data:`TOPICS`, whose :data:`LEADS` order the looks), then the dictionary's own words
    (``best_for`` / ``mood`` / ``tagline``). Equal themes are ordered by a hash of *seed* (the
    request), so the choice varies by request without being random.
    """
    templates = kit.templates()
    words = {w for w in _words(hint) if len(w) > 3}
    named, moods, kinds = named_themes(hint), mood_themes(hint), topic_kinds(hint)
    weight = {k: 3 * len(_words(hint) & set(TOPICS[k])) for k in kinds}

    def lead(theme_id: str) -> int:
        """Where *theme_id* stands in the topic's looks, else in its own category's."""
        for k in kinds:
            if theme_id in LEADS[k]:
                return LEADS[k].index(theme_id)
        own = LEADS.get(templates[theme_id].category, ())
        return 10 + own.index(theme_id) if theme_id in own else 20

    def key(theme_id: str) -> tuple[int, int, str]:
        t = templates[theme_id]
        score = (
            8 * (theme_id in named)
            + 4 * (theme_id in moods)
            + sum(w for k, w in weight.items() if theme_id in LEADS[k])
            + len(words & _words(f"{t.best_for} {t.description} {t.tagline}"))
        )
        return -score, lead(theme_id) // 2, _turn(seed or hint, theme_id)

    return sorted(templates, key=key)


def has_topic(text: str) -> bool:
    """Whether *text* says anything a look can be chosen from (a theme, a mood or a topic)."""
    words = _words(text)
    return bool(
        named_themes(text) or mood_themes(text) or any(words & set(k) for k in TOPICS.values())
    )


def remembered_themes(profile: str | None) -> list[str]:
    """Themes the person's Memory Profile names, in the order it names them."""
    low = (profile or "").lower()
    found: list[tuple[int, str]] = []
    for theme_id in named_themes(low):
        template = kit.templates()[theme_id]
        spots = [
            m.start()
            for n in (template.name.lower(), theme_id, theme_id.replace("-", " "))
            if (m := re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low))
        ]
        found.append((min(spots), theme_id))
    return [theme_id for _, theme_id in sorted(found)]


def choose_look(topic_text: str, memory_order: Sequence[str] = ()) -> str:
    """The look for a new deck about *topic_text*: the first theme in the ordering.

    A theme or mood the topic names comes first (:func:`rank`). Otherwise the
    person's remembered taste (*memory_order*) leads where it fits the topic's kind of look
    (:data:`TOPICS` / :data:`LEADS`), or anywhere when the topic says nothing; then the topic's
    best fit.
    """
    ranked = rank(topic_text, seed=topic_text)
    if named_themes(topic_text) or mood_themes(topic_text):
        return ranked[0]
    words = _words(topic_text)
    kinds = [k for k, keys in TOPICS.items() if words & set(keys)]
    fits = {theme for k in kinds for theme in LEADS[k]}
    templates = kit.templates()
    for theme_id in memory_order:
        if theme_id in templates and (not kinds or theme_id in fits):
            return theme_id
    return ranked[0]


def picked_look(view: dict) -> str | None:
    """The theme the person picked on a card in this Round, by the option's id (a ``deck-theme``
    option's id is its theme id), or ``None``. The pick is the card answer the Round's message
    carried (the view's ``answer``), so a Helper the Round started reads it too."""
    answer = view.get("answer")
    pick = answer.get("option_id") if isinstance(answer, dict) else None
    return pick if isinstance(pick, str) and pick in kit.templates() else None


@dataclass(frozen=True)
class Look:
    """How a new deck's look was settled.

    :param theme: The theme to build in.
    :param look_from: What the deck records: the model's ``look_from``, :data:`MODEL`,
        :data:`PICKED` or :data:`CHOSEN`.
    :param refusal: Why nothing may be built (background work with no topic), else ``None``.
    :param why: Why the engine chose, for the model (empty unless :attr:`chosen`).
    """

    theme: str
    look_from: str
    refusal: str | None = None
    why: str = ""

    @property
    def chosen(self) -> bool:
        """The engine chose the look (no template was given and nothing was picked)."""
        return self.look_from == CHOSEN


async def settle_look(
    ctx: HandlerCtx | None, look_from: object, template: str | None, title: str
) -> Look:
    """The look a new deck titled *title* is built in (see the module doc)."""
    if template is not None and template in kit.templates():
        return Look(
            template,
            look_from if isinstance(look_from, str) and look_from in LOOK_SOURCES else MODEL,
        )
    client = ctx.server_client if ctx is not None else None
    session = ctx.conversation_id if ctx is not None else None
    view = await read_tasks(client, session) or {}
    picked = picked_look(view)
    if picked is not None:
        return Look(picked, PICKED)
    topic = f"{title} {view.get('message') or ''}".strip()
    if view.get("background") is True and not has_topic(topic):
        return Look("", CHOSEN, HELPER_REFUSAL)
    try:
        profile = await asyncio.wait_for(fetch_memory_profile(client, session), _READ_BUDGET)
    except TimeoutError:
        profile = None
    remembered = remembered_themes(profile)
    theme = choose_look(topic, remembered)
    why = "no template was given"
    if theme in remembered:
        why += "; their Memory Profile names it"
    return Look(theme, CHOSEN, why=why)
