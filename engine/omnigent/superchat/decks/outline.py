"""A deck's slide outline, derived by code for a file's studio turn (ADR 0024).

One line per slide: its number, then each named element (``data-superchat-id``) that holds
text, with that text, so most requests ("make the title on slide 3 shorter") are answered
without reading the whole file. An element with no text and no named element inside it (a
chart's canvas, an image) is listed by its id alone. The slide footer (``class="foot"``) repeats
on every slide and is left out. Bounded: at most :data:`MAX_SLIDES` slides and
:data:`MAX_CHARS` characters in all.

A read by element (ADR 0031): one element's source by its ``data-superchat-id``, or a run of
slides, cut from the file as written, so an edit reads what it changes and never the file.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: How the block opens (a reader cuts the block from a stored message by it).
HEADER = "Slide outline (element ids and their text, from the newest version):"
MAX_SLIDES = 40
MAX_CHARS = 2000
#: Characters of one element's text shown.
MAX_TEXT = 60
_CUT = "… (outline cut here; read the file for the rest)"
_SKIP_TEXT_IN = frozenset({"script", "style", "template", "noscript"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}
    | {"param", "source", "track", "wbr"}
)


@dataclass
class _Element:
    id: str
    text: list[str] = field(default_factory=list)
    has_named_child: bool = False


@dataclass
class _Open:
    tag: str
    element: _Element | None
    skip: bool


class _OutlineParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.slides: list[list[_Element]] = []
        self._stack: list[_Open] = []
        self._slide_depth: int | None = None

    def _skipping(self) -> bool:
        return any(entry.skip for entry in self._stack)

    def _named(self) -> _Element | None:
        return next((e.element for e in reversed(self._stack) if e.element is not None), None)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "section" and "slide" in classes and self._slide_depth is None:
            self.slides.append([])
            self._slide_depth = len(self._stack)
            # The slide's own id names the slide, not an element of it.
            self._stack.append(_Open(tag, None, skip=False))
            return
        in_slide = self._slide_depth is not None
        skip = tag in _SKIP_TEXT_IN or "foot" in classes
        element: _Element | None = None
        element_id = values.get("data-superchat-id")
        if in_slide and element_id and not skip and not self._skipping():
            element = _Element(element_id.strip())
            parent = self._named()
            if parent is not None:
                parent.has_named_child = True
            if tag == "img" and values.get("alt"):
                element.text.append(values["alt"] or "")
            self.slides[-1].append(element)
        if tag not in _VOID:
            self._stack.append(_Open(tag, element, skip))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Recorded, then closed at once: a self-closed tag holds no text of its own.
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self._stack) - 1, -1, -1):
            if self._stack[at].tag == tag:
                del self._stack[at:]
                if self._slide_depth is not None and len(self._stack) <= self._slide_depth:
                    self._slide_depth = None
                return

    def handle_data(self, data: str) -> None:
        if self._slide_depth is None or self._skipping():
            return
        named = self._named()
        if named is not None:
            named.text.append(data)


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


def _slide_line(number: int, elements: list[_Element]) -> str:
    parts: list[str] = []
    for element in elements:
        text = _clip("".join(element.text))
        if text:
            parts.append(f'{element.id} "{text}"')
        elif not element.has_named_child:
            parts.append(element.id)
    return f"Slide {number}: " + (" · ".join(parts) if parts else "(no named text)")


def deck_outline(source: str | bytes) -> str | None:
    """The outline block of a deck's HTML, or ``None`` when it has no slides.

    :param source: The deck file (``<section class="slide">`` per slide).
    :returns: :data:`HEADER` and one line per slide, within :data:`MAX_CHARS`.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    parser = _OutlineParser()
    parser.feed(text)
    parser.close()
    if not parser.slides:
        return None
    lines = [HEADER]
    size = len(HEADER)
    slides = parser.slides
    for number, elements in enumerate(slides[:MAX_SLIDES], start=1):
        line = _slide_line(number, elements)
        if size + 1 + len(line) > MAX_CHARS - len(_CUT) - 1:
            lines.append(_CUT)
            return "\n".join(lines)
        lines.append(line)
        size += 1 + len(line)
    if len(slides) > MAX_SLIDES:
        lines.append(_CUT)
    return "\n".join(lines)


#: Characters of one element's source a read returns, and of a run of slides.
MAX_ELEMENT_CHARS = 4000
MAX_RANGE_CHARS = 8000


_CHART = "data-superchat-chart"
_CANVAS_ID_RE = re.compile(r"<canvas\b[^>]*\bid=\"([^\"]+)\"")


class _SpanParser(HTMLParser):
    """Where each outermost element a start-tag test accepts starts and ends in the source."""

    def __init__(self, text: str, want: Callable[[str, dict[str, str | None]], bool]) -> None:
        super().__init__(convert_charrefs=False)
        self.text = text
        self.want = want
        self.spans: list[tuple[int, int]] = []
        self._lines = [0]
        for at, char in enumerate(text):
            if char == "\n":
                self._lines.append(at + 1)
        self._open: list[str] | None = None
        self._start = 0

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._lines[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open is not None:
            if tag not in _VOID:
                self._open.append(tag)
            return
        if not self.want(tag, dict(attrs)):
            return
        start = self._offset()
        if tag in _VOID:
            self.spans.append((start, start + len(self.get_starttag_text() or "")))
        else:
            self._open, self._start = [tag], start

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._open is None and self.want(tag, dict(attrs)):
            start = self._offset()
            self.spans.append((start, start + len(self.get_starttag_text() or "")))

    def handle_endtag(self, tag: str) -> None:
        if self._open is None or tag not in self._open:
            return
        del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]
        if not self._open:
            end = self.text.find(">", self._offset())
            self.spans.append((self._start, len(self.text) if end < 0 else end + 1))
            self._open = None


def _spans(text: str, want: Callable[[str, dict[str, str | None]], bool]) -> list[str]:
    parser = _SpanParser(text, want)
    parser.feed(text)
    parser.close()
    return [text[a:b] for a, b in parser.spans]


def clip_source(source: str, limit: int) -> str:
    """*source*, or its first *limit* characters and a note saying it was cut."""
    if len(source) <= limit:
        return source
    return source[:limit] + f"\n… (cut at {limit} characters; read a smaller element)"


def element_source(source: str | bytes, element_id: str) -> str | None:
    """The source of the element whose ``data-superchat-id`` is *element_id*, or ``None``.

    A chart element (one holding a ``<canvas>``) comes with the chart script that draws it, so
    its data is read with it. Cut to :data:`MAX_ELEMENT_CHARS` with a note.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    found = _spans(text, lambda _tag, attrs: attrs.get("data-superchat-id") == element_id)
    if not found:
        return None
    # A chart's data is in its slide's chart script, beside the canvas: read with the chart.
    canvases = _CANVAS_ID_RE.findall(found[0])
    scripts = [
        script
        for script in _spans(text, lambda tag, attrs: tag == "script" and _CHART in attrs)
        if any(f"'{c}'" in script or f'"{c}"' in script for c in canvases)
    ]
    return clip_source("\n".join([found[0], *scripts]), MAX_ELEMENT_CHARS)


def slide_sources(source: str | bytes) -> list[str]:
    """Every slide's source (``<section class="slide ...">``), in order."""
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    return _spans(
        text, lambda tag, attrs: tag == "section" and "slide" in (attrs.get("class") or "").split()
    )
