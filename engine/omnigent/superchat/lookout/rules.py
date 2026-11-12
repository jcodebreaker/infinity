"""The Lookout's code check (ADR 0038, moved from 0035's topic watch): noise and matching.

Pure functions over plain values, no I/O: a signal is read as an :class:`Item`, noise is dropped
and each survivor is matched to one watched topic (strong, medium or weak) with the person's
mutes and the common-term filter applied. The limits (one message per topic per 3 hours, the
daily cap of the person's level) live in the rhythm capability, never here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The longest item text a source hands on (and the wake run reads).
MAX_EXCERPT_CHARS = 1500

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"
_STRENGTH_ORDER = {STRONG: 3, MEDIUM: 2, WEAK: 1}

# Term specificity (ADR 0035, amended 2026-10-23): a term everywhere in the person's own mail is
# not a signal. A term is common when it is in at least COMMON_SHARE of the items seen while it
# was watched, over the last COMMON_WINDOW_DAYS, once COMMON_MIN_ITEMS items were seen.
COMMON_SHARE = 0.05
COMMON_MIN_ITEMS = 20
COMMON_WINDOW_DAYS = 14
#: A term watched by this many open topics or more is shared, not distinctive.
SHARED_TOPICS = 3
#: Terms shorter than this never count.
MIN_TERM_CHARS = 3
#: Generic work words that never pick a topic out.
STOPLIST = frozenset(
    {
        "deck",
        "report",
        "slides",
        "meeting",
        "update",
        "q1",
        "q2",
        "q3",
        "q4",
        "email",
        "project",
        "draft",
        "team",
    }
)

_NO_REPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]daemon|postmaster|bounces?"
    r"|notifications?)"
    r"([-_.+].*)?@",
    re.IGNORECASE,
)
_AUTO_SUBJECT = re.compile(
    r"^\s*(auto(matic)?[ -]?(reply|response)|out of (the )?office|autoreply|away:)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(\.[\w-]+)+")


@dataclass(frozen=True)
class Item:
    """One change a source read, before any filtering.

    :param source: ``mail``, ``calendar``, ``files`` or ``web``.
    :param id: The source's own id of this change (a message, an event revision, a version).
    :param title: Subject, event title, file name or page title.
    :param text_excerpt: The item's text, at most :data:`MAX_EXCERPT_CHARS` characters.
    :param at: Epoch seconds the change happened.
    :param thread_ref: The thread, meeting or file the change belongs to, if any.
    :param sender: Who sent or made it (``"Name <address>"`` or an address), if known.
    :param people: Attendees, recipients or editors.
    :param refs: Other hard references the item answers to (a file's name or path).
    :param headers: Mail headers that say what kind of mail it is, names lower-cased.
    :param from_self: The person (or the assistant working for them) made it.
    :param response: A calendar change that is only an attendee's reply: ``accepted``,
        ``tentative`` or ``declined``.
    :param topic_key: The one topic a web item was fetched for.
    """

    source: str
    id: str
    title: str
    text_excerpt: str
    at: int
    thread_ref: str | None = None
    sender: str | None = None
    people: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    from_self: bool = False
    response: str | None = None
    topic_key: str | None = None

    @property
    def key(self) -> str:
        """The seen-set key, ``"<source>:<id>"``."""
        return f"{self.source}:{self.id}"[:255]

    @property
    def thread(self) -> str:
        """What "the same thread" means for this item: its thread, else the item itself."""
        return f"{self.source}:{self.thread_ref}"[:255] if self.thread_ref else self.key


@dataclass(frozen=True)
class WatchedTopic:
    """An open topic as the Lookout reads it (from the topics capability).

    :param key: The topic's stable key.
    :param title: Its 2-5 word title.
    :param left_off: Where it stands now, one sentence.
    :param open: Its open lines, ``{kind, text, due?}``.
    :param people: Watched names and addresses.
    :param terms: Watched terms (at most 8).
    :param refs: Hard references: thread, event and file ids it touched.
    :param web: Whether the topic asked to be watched on the web.
    """

    key: str
    title: str
    left_off: str = ""
    open: tuple[Mapping[str, str], ...] = ()
    people: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    web: bool = False


@dataclass(frozen=True)
class Match:
    """The one topic an item matched and how strongly.

    :param topic_key: The topic.
    :param strength: ``strong``, ``medium`` or ``weak``.
    :param reason: Plain words: which reference, person or terms it hit.
    """

    topic_key: str
    strength: str
    reason: str


def watched_topic(raw: Mapping[str, Any]) -> WatchedTopic | None:
    """A topic from ``list_watched_topics`` as :class:`WatchedTopic`; ``None`` without a key."""
    key = str(raw.get("key") or "").strip()
    if not key:
        return None
    watch = raw.get("watch") if isinstance(raw.get("watch"), Mapping) else {}

    def strings(value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(s for s in (str(v).strip() for v in value if v is not None) if s)

    open_lines = tuple(
        {
            "kind": str(o.get("kind") or ""),
            "text": str(o.get("text") or ""),
            **({"due": str(o["due"])[:10]} if o.get("due") else {}),
        }
        for o in raw.get("open") or ()
        if isinstance(o, Mapping) and o.get("text")
    )
    return WatchedTopic(
        key=key,
        title=str(raw.get("title") or key),
        left_off=str(raw.get("left_off") or ""),
        open=open_lines,
        people=strings(watch.get("people")),
        terms=strings(watch.get("terms"))[:8],
        refs=strings(watch.get("refs")),
        web=bool(watch.get("web")),
    )


def _address(text: str | None) -> str | None:
    found = _EMAIL.search(text or "")
    return found.group(0).casefold() if found else None


def noise_reason(item: Item) -> str | None:
    """Why *item* is noise and dropped before matching, or ``None`` when it is not.

    Own mail and own work, no-reply and bounce senders, list and bulk mail (newsletters),
    auto-replies, and a calendar reply that only accepts or holds the slot.
    """
    if item.from_self:
        return "own"
    headers = {k.casefold(): str(v).casefold() for k, v in item.headers.items()}
    address = _address(item.sender)
    if address and _NO_REPLY.match(address):
        return "no_reply"
    if "list-id" in headers or "list-unsubscribe" in headers:
        return "list"
    if headers.get("precedence") in ("bulk", "list", "junk"):
        return "bulk"
    auto = headers.get("auto-submitted")
    if (auto and auto != "no") or "x-autoreply" in headers or "x-autorespond" in headers:
        return "auto_reply"
    if item.source == "mail" and _AUTO_SUBJECT.match(item.title):
        return "auto_reply"
    if (
        item.source == "calendar"
        and item.response in ("accepted", "tentative")
        and not item.text_excerpt.strip()
    ):
        return "calendar_reply"
    return None


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def norm_term(value: str) -> str:
    """A watched term as matching and the term counts read it (case and spacing folded)."""
    return _norm(value)


def term_hits(item: Item, terms: Iterable[str]) -> set[str]:
    """Which of the normalized *terms* *item* contains as whole words (each counted once)."""
    text = _norm(f"{item.title}\n{item.text_excerpt}")
    return {t for t in terms if _has_phrase(text, t)}


def common_terms(
    topics: Sequence[WatchedTopic],
    *,
    own: Iterable[str] = (),
    stats: Mapping[str, tuple[int, int]] | None = None,
) -> dict[str, str]:
    """The watched terms that are common for this person, each with why (normalized keys).

    - ``seed:org``: the person's own organisation or email domain (*own*), or its leading words
      (``northwind`` for "Northwind Capital Markets").
    - ``seed:shared``: watched by :data:`SHARED_TOPICS` or more of the open topics.
    - ``stoplist``: shorter than :data:`MIN_TERM_CHARS`, or a generic work word.
    - ``"23% of 140 items"``: in at least :data:`COMMON_SHARE` of the items seen while it was
      watched (*stats*: ``term -> (items containing it, items seen)``), once
      :data:`COMMON_MIN_ITEMS` were seen; below that only the seeds and the stoplist apply.
    """
    owned = [o for o in (_norm(x) for x in own) if o]
    by_term: dict[str, set[str]] = {}
    for topic in topics:
        for term in topic.terms:
            normalized = _norm(term)
            if normalized:
                by_term.setdefault(normalized, set()).add(topic.key)
    common: dict[str, str] = {}
    for term, keys in by_term.items():
        hits, seen = (stats or {}).get(term, (0, 0))
        if any(term == o or o.startswith(f"{term} ") for o in owned):
            common[term] = "seed:org"
        elif len(keys) >= SHARED_TOPICS:
            common[term] = "seed:shared"
        elif len(term) < MIN_TERM_CHARS or term in STOPLIST:
            common[term] = "stoplist"
        elif seen >= COMMON_MIN_ITEMS and hits >= COMMON_SHARE * seen:
            common[term] = f"{round(100 * hits / seen)}% of {seen} items"
    return common


def _has_phrase(text: str, phrase: str) -> bool:
    """Whether *phrase* occurs in *text* as whole words (both already normalized)."""
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _person_hit(item: Item, people: Iterable[str]) -> str | None:
    """The watched person who sent, attends or edited *item*, if any."""
    who = [w for w in (item.sender, *item.people) if w]
    addresses = {a for a in (_address(w) for w in who) if a}
    names = [_norm(_EMAIL.sub(" ", w).replace("<", " ").replace(">", " ")) for w in who]
    for person in people:
        address = _address(person)
        if address and address in addresses:
            return person
        name = _norm(_EMAIL.sub(" ", person).replace("<", " ").replace(">", " "))
        if name and any(_has_phrase(n, name) for n in names):
            return person
    return None


def _muted(mutes: Sequence[Mapping[str, Any]], topic_key: str) -> tuple[set[str], set[str]]:
    """``(muted sender addresses or names, muted terms)`` for one topic, normalized."""
    senders: set[str] = set()
    terms: set[str] = set()
    for mute in mutes:
        if str(mute.get("topic_key") or "") != topic_key:
            continue
        if mute.get("sender"):
            sender = str(mute["sender"])
            senders.add(_address(sender) or _norm(sender))
        if mute.get("term"):
            terms.add(_norm(str(mute["term"])))
    return senders, terms


def topic_muted(mutes: Sequence[Mapping[str, Any]], topic_key: str) -> bool:
    """Whether a *Not useful* muted the whole topic (a mute naming neither sender nor term)."""
    return any(
        str(m.get("topic_key") or "") == topic_key and not m.get("sender") and not m.get("term")
        for m in mutes
    )


def muted_for(mutes: Sequence[Mapping[str, Any]], topic_key: str, sender: str | None) -> bool:
    """Whether a *Not useful* mute holds a signal on *topic_key*: the whole topic, or its sender
    on that topic (the Judge's topic gets the same mutes as the code's match)."""
    if topic_muted(mutes, topic_key):
        return True
    if not sender:
        return False
    muted_senders, _ = _muted(mutes, topic_key)
    return (_address(sender) or _norm(sender)) in muted_senders or _norm(sender) in muted_senders


def _match_one(
    item: Item,
    topic: WatchedTopic,
    mutes: Sequence[Mapping[str, Any]],
    common: Mapping[str, str] | frozenset[str],
) -> Match | None:
    if topic_muted(mutes, topic.key):
        return None
    muted_senders, muted_terms = _muted(mutes, topic.key)
    if item.sender and (
        (_address(item.sender) or "") in muted_senders or _norm(item.sender) in muted_senders
    ):
        return None
    refs = {_norm(r) for r in topic.refs}
    for ref in (item.thread_ref, item.id, *item.refs):
        if ref and _norm(ref) in refs:
            return Match(topic.key, STRONG, f"watched reference {ref}")
    text = _norm(f"{item.title}\n{item.text_excerpt}")
    terms = {
        t
        for t in (_norm(term) for term in topic.terms)
        if t and t not in muted_terms and t not in common
    }
    hits = sorted(t for t in terms if _has_phrase(text, t))
    person = _person_hit(item, topic.people)
    if person and hits:
        return Match(topic.key, MEDIUM, f"from {person}, mentions {', '.join(hits)}")
    if len(hits) >= 2:
        return Match(topic.key, WEAK, f"mentions {', '.join(hits)}")
    return None


def sender_match(
    item: Item,
    topics: Sequence[WatchedTopic],
    mutes: Sequence[Mapping[str, Any]] = (),
) -> Match | None:
    """The first topic whose watch list names *item*'s sender (ADR 0039: mail from a watched
    person is a signal by itself), weak; a muted topic or sender never matches."""
    if not item.sender:
        return None
    from_only = Item(
        source=item.source, id=item.id, title="", text_excerpt="", at=item.at, sender=item.sender
    )
    for topic in topics:
        if topic_muted(mutes, topic.key):
            continue
        muted_senders, _ = _muted(mutes, topic.key)
        if (_address(item.sender) or _norm(item.sender)) in muted_senders:
            continue
        person = _person_hit(from_only, topic.people)
        if person:
            return Match(topic.key, WEAK, f"from {person}, on the topic's watch list")
    return None


def match(
    item: Item,
    topics: Sequence[WatchedTopic],
    mutes: Sequence[Mapping[str, Any]] = (),
    common: Mapping[str, str] | frozenset[str] = frozenset(),
) -> Match | None:
    """The strongest topic *item* hits, or ``None`` (ADR 0035 stage 3).

    Strong: a hard reference (thread, meeting or file the topic touched). Medium: a watched
    person sent, attends or edited it and it contains a specific watched term. Weak: it contains
    two or more distinct specific watched terms of one topic. A *common* term (normalized, from
    :func:`common_terms`) never counts, so a topic watched only by common terms matches by its
    references alone. A muted sender never matches that topic and a muted term never counts for
    it. A web item matches only the topic it was fetched for. Ties go to the earlier topic in
    *topics*.
    """
    best: Match | None = None
    for topic in topics:
        if item.topic_key is not None and topic.key != item.topic_key:
            continue
        found = _match_one(item, topic, mutes, common)
        if found and (
            best is None or _STRENGTH_ORDER[found.strength] > _STRENGTH_ORDER[best.strength]
        ):
            best = found
    return best
