"""The brief page: what a run's content may hold, and the HTML code lays it out in (ADR 0037).

The run fills content (``brief_save``); this module owns the layout. Pure: no I/O.

Sections, in order: *Needs you today* (at most 3, each with its reason and a ready draft or one
next step, never sent), *Your day*, *Since yesterday*, *Worth knowing* (at most 3). An empty
section is left out; with nothing under *Needs you today* the page and its line say
"Nothing needs you today". Code trims what is too long or too many; it refuses only an item that
lacks what makes it useful (a title, a reason, a draft or next step).
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: The one file every brief is a version of, in the person's Conversation.
BRIEF_NAME = "brief.html"
NOTHING_LINE = "Nothing needs you today"
#: Carries the page's first line, so a client reads it without parsing the layout.
LINE_META = "superchat-brief-line"
#: Carries the Home lines (one per topic) as JSON, for the same reason.
HOME_META = "superchat-brief-home"

MAX_NEEDS = 3
MAX_WORTH = 3
MAX_DAY = 12
MAX_SINCE = 8
#: Home's "This morning" panel: one line per topic.
MAX_HOME = 4
_HOME_TEXT, _HOME_BOLD = 140, 90

_TITLE, _TEXT, _DRAFT, _SHORT = 140, 500, 4000, 40


def brief_title(day: date) -> str:
    """``Brief · Fri 23 Oct``."""
    return f"Brief · {day:%a} {day.day} {day:%b}"


def day_label(day: date) -> str:
    """``Fri 23 Oct``."""
    return f"{day:%a} {day.day} {day:%b}"


@dataclass
class Need:
    title: str
    why: str
    tag: str = ""
    source: str = ""
    next_step: str = ""
    draft_to: str = ""
    draft: str = ""


@dataclass
class Slot:
    time: str
    title: str
    prep: str = ""


@dataclass
class Moved:
    topic: str
    text: str


@dataclass
class HomeLine:
    """One topic's line on Home: plain *text* (what happened or was done), then *bold* (what is
    ready for the person, or what needs them)."""

    topic_key: str
    topic: str
    text: str
    bold: str

    def to_dict(self) -> dict[str, str]:
        return {
            "topic_key": self.topic_key,
            "topic": self.topic,
            "text": self.text,
            "bold": self.bold,
        }


@dataclass
class Brief:
    day: date
    summary: str = ""
    needs: list[Need] = field(default_factory=list)
    your_day: list[Slot] = field(default_factory=list)
    since: list[Moved] = field(default_factory=list)
    worth: list[str] = field(default_factory=list)
    home: list[HomeLine] = field(default_factory=list)

    @property
    def title(self) -> str:
        return brief_title(self.day)

    @property
    def line(self) -> str:
        """The first line of *Needs you today*, or :data:`NOTHING_LINE`."""
        return self.needs[0].title if self.needs else NOTHING_LINE


def _text(value: Any, limit: int) -> str:
    """One trimmed line of at most ``limit`` characters (whitespace collapsed)."""
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _block(value: Any, limit: int) -> str:
    """Multi-line text (a draft) trimmed to ``limit`` characters, line breaks kept."""
    if not isinstance(value, str):
        return ""
    clean = "\n".join(line.rstrip() for line in value.strip().splitlines())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


_KEY_RE = re.compile(r"[^a-z0-9]+")


def _key(value: Any) -> str:
    """A topic key as the topics capability writes it (lowercase letters, digits, hyphens)."""
    if not isinstance(value, str):
        return ""
    return _KEY_RE.sub("-", value.lower()).strip("-")[:64].strip("-")


def _items(args: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = args.get(key) or []
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be a list")
    return [item for item in raw if isinstance(item, dict)]


def parse_brief(args: dict[str, Any], today: date) -> Brief:
    """The run's content as a :class:`Brief`.

    :param args: ``brief_save``'s arguments.
    :param today: The day used when ``date`` is missing or not ``YYYY-MM-DD``.
    :raises ValueError: An item lacks its title, its reason, or a draft or next step.
    """
    try:
        day = date.fromisoformat(str(args.get("date") or ""))
    except ValueError:
        day = today
    brief = Brief(day=day, summary=_text(args.get("summary"), _TEXT))
    for i, item in enumerate(_items(args, "needs_you")[:MAX_NEEDS], start=1):
        draft = item.get("draft") if isinstance(item.get("draft"), dict) else {}
        need = Need(
            title=_text(item.get("title"), _TITLE),
            why=_text(item.get("why"), _TEXT),
            tag=_text(item.get("tag"), _SHORT),
            source=_text(item.get("source"), 80),
            next_step=_text(item.get("next_step"), _TEXT),
            draft_to=_text(draft.get("to"), _TITLE),
            draft=_block(draft.get("text"), _DRAFT),
        )
        if not need.title or not need.why:
            raise ValueError(f"needs_you item {i} needs a title and why it needs the person today")
        if not need.draft and not need.next_step:
            raise ValueError(f"needs_you item {i} needs a ready draft or one next step")
        brief.needs.append(need)
    for item in _items(args, "your_day")[:MAX_DAY]:
        slot = Slot(_text(item.get("time"), 16), _text(item.get("title"), _TITLE))
        slot.prep = _text(item.get("prep"), _TEXT)
        if slot.title:
            brief.your_day.append(slot)
    for item in _items(args, "since_yesterday")[:MAX_SINCE]:
        moved = Moved(_text(item.get("topic"), 80), _text(item.get("text"), _TEXT))
        if moved.text:
            brief.since.append(moved)
    for item in _items(args, "home_lines")[:MAX_HOME]:
        line = HomeLine(
            topic_key=_key(item.get("topic_key")),
            topic=_text(item.get("topic"), 80),
            text=_text(item.get("text"), _HOME_TEXT),
            bold=_text(item.get("bold"), _HOME_BOLD),
        )
        if line.text or line.bold:
            brief.home.append(line)
    raw_worth = args.get("worth_knowing") or []
    if not isinstance(raw_worth, list):
        raise ValueError("worth_knowing must be a list")
    for entry in raw_worth:
        text = _text(entry.get("text") if isinstance(entry, dict) else entry, _TEXT)
        if text and len(brief.worth) < MAX_WORTH:
            brief.worth.append(text)
    return brief


_LINE_RE = re.compile(
    r'<meta\s+name="' + re.escape(LINE_META) + r'"\s+content="([^"]*)"', re.IGNORECASE
)


def page_line(data: bytes) -> str | None:
    """The first line a saved brief page carries, or ``None`` for a page without one."""
    found = _LINE_RE.search(data[:4096].decode("utf-8", "replace"))
    return html.unescape(found.group(1)) if found else None


_HOME_RE = re.compile(
    r'<meta\s+name="' + re.escape(HOME_META) + r'"\s+content="([^"]*)"', re.IGNORECASE
)


def page_home_lines(data: bytes) -> list[dict[str, str]]:
    """The Home lines a saved brief page carries (``[{topic_key, topic, text, bold}]``), or
    ``[]`` for a page without them."""
    found = _HOME_RE.search(data[:16384].decode("utf-8", "replace"))
    if not found:
        return []
    try:
        raw = json.loads(html.unescape(found.group(1)))
    except ValueError:
        return []
    keys = ("topic_key", "topic", "text", "bold")
    return [
        {k: str(item.get(k) or "") for k in keys}
        for item in (raw if isinstance(raw, list) else [])[:MAX_HOME]
        if isinstance(item, dict)
    ]


def _e(text: str) -> str:
    return html.escape(text, quote=True)


_COUNT_WORDS = {1: "One thing needs", 2: "Two things need", 3: "Three things need"}


def _need(need: Need) -> str:
    top = []
    if need.tag:
        top.append(f'<span class="pill tint">{_e(need.tag)}</span>')
    top.append(f"<h3>{_e(need.title)}</h3>")
    if need.source:
        top.append(f'<span class="when">{_e(need.source)}</span>')
    parts = [f'<div class="item-top">{"".join(top)}</div>', f"<p>{_e(need.why)}</p>"]
    if need.next_step:
        parts.append(
            f'<p class="next"><span class="lbl">Next step</span> {_e(need.next_step)}</p>'
        )
    if need.draft:
        to = f" · to {_e(need.draft_to)}" if need.draft_to else ""
        parts.append(
            '<details class="draft"><summary><span class="pill plain">Draft ready</span>'
            f'<span class="to">Not sent{to}</span></summary>'
            f'<div class="body">{_e(need.draft)}</div></details>'
        )
    return f'<article class="item">{"".join(parts)}</article>'


def _card(label: str, body: str) -> str:
    return f'<section class="card"><h2 class="eyebrow">{label}</h2>{body}</section>'


def render_page(brief: Brief) -> bytes:
    """The brief as one self-contained HTML page (no script, no web fonts, light and dark)."""
    if brief.needs:
        count = len(brief.needs)
        headline = f"{_COUNT_WORDS.get(count, f'{count} things need')} you today."
    else:
        headline = f"{NOTHING_LINE}."
    lede = f'<p class="lede">{_e(brief.summary)}</p>' if brief.summary else ""
    sections = []
    if brief.needs:
        items = "".join(_need(n) for n in brief.needs)
        sections.append(
            f'<section class="needs"><h2 class="eyebrow">Needs you today</h2>{items}</section>'
        )
    cards = []
    if brief.your_day:
        slots = "".join(
            f'<div class="slot"><span class="t">{_e(s.time)}</span><span><strong>'
            f"{_e(s.title)}</strong>"
            + (f'<span class="muted">{_e(s.prep)}</span>' if s.prep else "")
            + "</span></div>"
            for s in brief.your_day
        )
        cards.append(_card("Your day", slots))
    if brief.since:
        moved = "".join(
            f"<p><strong>{_e(m.topic)}</strong> {_e(m.text)}</p>"
            if m.topic
            else f"<p>{_e(m.text)}</p>"
            for m in brief.since
        )
        cards.append(_card("Since yesterday", moved))
    if brief.worth:
        cards.append(_card("Worth knowing", "".join(f"<p>{_e(w)}</p>" for w in brief.worth)))
    if cards:
        sections.append(f'<div class="grid">{"".join(cards)}</div>')
    footer = (
        '<footer><span class="pill ok">Nothing was sent for you</span></footer>'
        if brief.needs
        else ""
    )
    home_meta = (
        f'<meta name="{HOME_META}" content="'
        + _e(json.dumps([h.to_dict() for h in brief.home], ensure_ascii=False))
        + '">\n'
        if brief.home
        else ""
    )
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="{LINE_META}" content="{_e(brief.line)}">
{home_meta}<title>{_e(brief.title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<main class="brief">
<header class="hello">
<span class="orb" aria-hidden="true"></span>
<div>
<p class="eyebrow">Morning brief · {_e(day_label(brief.day))}</p>
<h1>{_e(headline)}</h1>
{lede}
</div>
</header>
{"".join(sections)}
{footer}
</main>
</body>
</html>
"""
    return page.encode()


# The Routines mock-up's morning brief, as tokens: light by default, dark with the system.
_STYLE = """
:root{--bg:#f5f5f7;--surface:#fff;--surface-2:#f2f2f7;--ink:#1d1d1f;--ink-2:#4a4a50;--ink-3:#6e6e73;
--line:rgba(60,60,67,.13);--tint-soft:#e8f1fc;--tint-ink:#0058b0;--ok:#1b6b33;--ok-soft:#e7f8ec;
--shadow:0 1px 2px rgba(0,0,0,.04),0 18px 40px -18px rgba(0,0,0,.18);color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--surface:#1d242c;--surface-2:#252c35;
--ink:#eef1f4;--ink-2:#c2cad2;--ink-3:#a9b3bd;--line:rgba(255,255,255,.1);
--tint-soft:rgba(10,132,255,.16);--tint-ink:#9fcdff;--ok:#7ee2a0;--ok-soft:rgba(48,209,88,.15);
--shadow:0 1px 2px rgba(0,0,0,.3),0 18px 40px -18px rgba(0,0,0,.6);color-scheme:dark}}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
Arial,sans-serif;
-webkit-font-smoothing:antialiased}
h1,h2,h3,p{margin:0}
h1,h3{text-wrap:balance}
.brief{max-width:880px;margin:0 auto;padding:40px clamp(16px,4vw,48px) 48px;display:flex;
flex-direction:column;gap:24px}
.eyebrow{font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.hello{display:flex;gap:14px;align-items:flex-start}
.orb{flex:none;width:32px;height:32px;margin-top:2px;border-radius:50%;
background:radial-gradient(circle at 35% 30%,#9fdcf5,#0071e3 60%,#003d80)}
.hello h1{font-size:clamp(24px,3.4vw,32px);line-height:1.15;font-weight:600;letter-spacing:-.015em;
margin-top:4px}
.lede{color:var(--ink-2);margin-top:8px;max-width:62ch}
.needs{display:flex;flex-direction:column;gap:10px}
.item{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
display:flex;flex-direction:column;gap:10px;box-shadow:var(--shadow)}
.item-top{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.item-top h3{font-size:16px;font-weight:600}
.item-top .when{margin-left:auto;font-size:12px;color:var(--ink-3)}
.item p{font-size:14px;color:var(--ink-2)}
.item .next{color:var(--ink)}
.lbl{font-size:12px;font-weight:600;color:var(--ink-3);margin-right:6px}
.pill{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:12px;
font-weight:600;white-space:nowrap}
.pill.tint{background:var(--tint-soft);color:var(--tint-ink)}
.pill.plain{background:var(--surface-2);color:var(--ink-2);font-weight:500}
.pill.ok{background:var(--ok-soft);color:var(--ok)}
.draft{border-top:1px solid var(--line);padding-top:10px}
.draft summary{cursor:pointer;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
list-style:none}
.draft summary::-webkit-details-marker{display:none}
.draft .to{font-size:12px;color:var(--ink-3)}
.draft .body{margin-top:10px;padding:12px 14px;border-radius:10px;background:var(--surface-2);
font-size:14px;white-space:pre-line}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
display:flex;flex-direction:column;gap:10px;font-size:14px;min-width:0}
.card p{color:var(--ink-2)}
.card strong{color:var(--ink);font-weight:600}
.slot{display:flex;gap:12px}
.slot .t{flex:none;width:48px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.slot .muted{display:block;color:var(--ink-3);font-size:13px}
footer{display:flex}
"""
