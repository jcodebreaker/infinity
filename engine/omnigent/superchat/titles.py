"""Plain-language wording helpers shared across capabilities: titles, gists, markdown strip.

Pure text in, text out (no store, no model). Activity's title rules build on these.
"""

from __future__ import annotations

import re

TITLE_MAX_CHARS = 60
TITLE_MAX_WORDS = 8
SUMMARY_MAX_CHARS = 110
SUMMARY_MAX_WORDS = 16

_FILLER_PREFIX = re.compile(
    r"^(?:(?:yo+|hey+|hi+|hello|hiya|howdy|sup|ok(?:ay)?|so|well|um+|uh+|please|pls|"
    r"thanks|thank you|can you|could you|would you|will you|"
    r"i(?:'d| would) like (?:you )?to|i (?:want|need) (?:you )?to|help me|let'?s)\b"
    r"[\s,!.:;-]*)+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = re.compile(r"[\s?!.,;:]+$")


_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_NOISE = re.compile(r"[*`#>]+|(?<!\w)_+|_+(?!\w)|^\s*[-+]\s+", re.MULTILINE)


def plain_text(text: str) -> str:
    """``text`` as one line of plain words: markdown marks dropped, links kept as their label."""
    return " ".join(_MARKDOWN_NOISE.sub(" ", _MARKDOWN_LINK.sub(r"\1", text)).split())


_INTERNAL_PHRASES = (
    (re.compile(r"\bbackground[\s-]+tasks?\b", re.IGNORECASE), "task"),
    (re.compile(r"\bsessions?\b", re.IGNORECASE), "chat"),
)
_INTERNAL_WORDS = re.compile(r"\b(?:sub[\s-]?agents?|helpers?|tools?)\b", re.IGNORECASE)


def scrub_internal_words(text: str) -> str:
    """``text`` without the engine words (sub-agent, Helper, background task, session, tool)."""
    for pattern, replacement in _INTERNAL_PHRASES:
        text = pattern.sub(replacement, text)
    return " ".join(_INTERNAL_WORDS.sub(" ", text).split())


def sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


#: Acknowledgement openers a reply starts with before getting to the point.
_REPLY_FILLER = re.compile(
    r"^(?:(?:perfect|great|sure|got it|alright|all right|okay|ok|absolutely|of course|done|"
    r"awesome|excellent|certainly|good|nice|right)\b[\s.!,:;—–-]*"
    r"|now i (?:have|can|know|see)\b[^.!?]*[.!?]\s*"
    r"|working on it\b[\s.!,:;—–-]*)+",
    re.IGNORECASE,
)


def strip_reply_filler(text: str | None) -> str | None:
    """``text`` without leading acknowledgements ("Perfect.", "Got it.", "Working on it —")."""
    if not text:
        return text
    stripped = _REPLY_FILLER.sub("", text.strip(), count=1).strip()
    return sentence_case(stripped) if stripped else None


#: A brief written about the person ("The person wants...", "Your task is...") is instruction to
#: a Helper, not a title; deriving one from its first words would show the brief's wording.
_BRIEF_SPEAK = re.compile(
    r"^(?:(?:the|this)\s+)?(?:person|user|human|customer|client)\b"
    r"|^(?:your|the)\s+(?:task|job|goal|brief)\s+is\b"
    r"|^you\s+(?:are|will|should|must)\b"
    r"|^task\s*:",
    re.IGNORECASE,
)


def tidy_request_title(text: str | None, *, limit: int = TITLE_MAX_CHARS) -> str | None:
    """A request as a short title: fillers dropped, sentence case, trimmed to ``limit``.

    :param text: The person's message (or a delegated task prompt).
    :param limit: Maximum characters, ellipsis included.
    :returns: The tidied title, or ``None`` when nothing meaningful is left.
    """
    if not text:
        return None
    collapsed = scrub_internal_words(plain_text(text))
    if _BRIEF_SPEAK.match(collapsed):
        return None
    stripped = TRAILING_PUNCTUATION.sub("", _FILLER_PREFIX.sub("", collapsed, count=1))
    if len(stripped) < 2:
        return None
    return clip(sentence_case(stripped), limit)


def truncate(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"


#: A reply that is a code block or raw JSON (a Helper's machine-readable Result) says
#: nothing in a one-line gist.
STRUCTURED_TEXT = re.compile(r"\s*(?:```|[{\[])")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def one_line_summary(text: str | None) -> str | None:
    """The first sentence of ``text`` as plain words, at most :data:`SUMMARY_MAX_CHARS`."""
    if not text or STRUCTURED_TEXT.match(text):
        return None
    plain = strip_reply_filler(plain_text(text))
    if not plain:
        return None
    first = _SENTENCE_END.split(plain, maxsplit=1)[0]
    if len(first) < 10:
        first = plain
    return truncate(first, SUMMARY_MAX_CHARS)
