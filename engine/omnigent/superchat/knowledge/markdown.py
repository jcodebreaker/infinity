"""A file as one Markdown document, with a marker where each page starts.

The same pages the passages are cut from (:mod:`omnigent.superchat.knowledge.parse`) are written
out as Markdown, so what a model reads whole (``files_get``, an attached file's text) and what a
search cites (``page 3``) agree. A PDF keeps its real pages; a text, Markdown or CSV file keeps
its virtual pages; a workbook has one page per sheet and its rows become Markdown tables. Pure:
pages in, text out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnigent.superchat.knowledge.chunking import Block, ParsedPage

_MARKER = "<!-- page {n} -->"
_MARKER_LINE = re.compile(r"^<!-- page (\d+) -->$", re.MULTILINE)
_TABLE_KINDS = frozenset({"csv", "sheet"})
_CELL_SEP = " | "


#: A source line that looks like a page marker (any spacing), which must not read as a real one.
_FORGED_MARKER = re.compile(r"^([ \t]*)(<!--[ \t]*page\b)", re.IGNORECASE | re.MULTILINE)


def _neutralise(text: str) -> str:
    """*text* with every marker-like line defanged (a backslash before it), so only the markers
    :func:`to_markdown` writes split a file into pages."""
    return _FORGED_MARKER.sub(r"\1\\\2", text)


def page_marker(page_no: int) -> str:
    """The line that starts page *page_no* in a file's Markdown."""
    return _MARKER.format(n=page_no)


def _cell(value: str) -> str:
    return value.strip().replace("|", "\\|").replace("\n", " ")


def _table(text: str) -> str:
    """A table slice (``a | b`` rows, header first) as a Markdown table."""
    rows = [line.split(_CELL_SEP) for line in text.split("\n") if line.strip()]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = [
        "| " + " | ".join(_cell(c) for c in [*row, *[""] * (width - len(row))]) + " |"
        for row in rows
    ]
    lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


def _block(block: Block, kind: str) -> str:
    text = _neutralise(block.text.strip())
    if not text:
        return ""
    if block.heading_level is not None:
        return f"{'#' * block.heading_level} {text}"
    if block.atomic and kind in _TABLE_KINDS:
        return _table(text)
    return text


def to_markdown(pages: Iterable[ParsedPage], *, kind: str = "text") -> str:
    """The Markdown of *pages*; each page starts with its marker, even when it has no text.

    :param kind: The chunk kind ``parse_file`` returned (``text``, ``csv``, ``sheet``).
    """
    parts: list[str] = []
    for page in pages:
        body = "\n\n".join(b for b in (_block(x, kind) for x in page.blocks) if b)
        parts.append(f"{page_marker(page.page_no)}\n\n{body}".rstrip())
    return "\n\n".join(parts) + "\n"


def split_pages(markdown: str) -> dict[int, str]:
    """``{page number: that page's Markdown}`` (marker line excluded)."""
    found = list(_MARKER_LINE.finditer(markdown))
    pages: dict[int, str] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(markdown)
        pages[int(match.group(1))] = markdown[match.end() : end].strip()
    return pages


def select_pages(markdown: str, wanted: Iterable[int]) -> str:
    """The Markdown of only the pages in *wanted*, markers kept, in page order."""
    pages = split_pages(markdown)
    chosen = sorted({n for n in wanted if n in pages})
    return "\n\n".join(f"{page_marker(n)}\n\n{pages[n]}".rstrip() for n in chosen) + (
        "\n" if chosen else ""
    )


def parse_page_spec(spec: object, page_count: int) -> list[int]:
    """Page numbers from ``3``, ``"2-4"``, ``"1,5,7-9"`` or a list of those, within the file.

    :raises ValueError: the spec is not page numbers.
    """
    if isinstance(spec, bool) or spec is None:
        raise ValueError("pages must be page numbers")
    items = spec if isinstance(spec, list) else [spec]
    numbers: set[int] = set()
    for item in items:
        if isinstance(item, bool):
            raise ValueError("pages must be page numbers")
        if isinstance(item, int):
            numbers.add(item)
            continue
        for part in str(item).split(","):
            part = part.strip()
            if not part:
                continue
            low, dash, high = part.partition("-")
            try:
                start, end = int(low), int(high) if dash else int(low)
            except ValueError:
                raise ValueError(f"'{part}' is not a page or a range of pages") from None
            numbers.update(range(start, min(end, start + 500) + 1))
    return sorted(n for n in numbers if 1 <= n <= page_count)
