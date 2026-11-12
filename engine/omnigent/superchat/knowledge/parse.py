"""Turn a file's bytes into pages of blocks, by kind.

A PDF keeps its real pages. A text or Markdown file gets virtual pages of about
:data:`VIRTUAL_PAGE_CHARS` so a citation can still say "page 3". CSV and XLSX files are not paged
text: see :mod:`omnigent.superchat.knowledge.tabular`. Nothing here calls a model.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnigent.superchat.knowledge.chunking import Block, ParsedPage
from omnigent.superchat.knowledge.pdf import extract_pages

#: Artifact kinds this capability indexes.
INDEXABLE_KINDS = frozenset({"pdf", "txt", "md", "csv", "xlsx"})
#: A virtual page of a text file ends at the first block boundary past this many characters.
VIRTUAL_PAGE_CHARS = 3500

_FENCE = re.compile(r"^\s*(```|~~~)")
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BLANK_RUN = re.compile(r"\n\s*\n")


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM); Windows-1252 for a file that is not UTF-8."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def paginate(blocks: Iterable[Block], limit: int = VIRTUAL_PAGE_CHARS) -> list[ParsedPage]:
    """Group blocks into virtual pages, cutting at the first block boundary past *limit*."""
    pages: list[ParsedPage] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        current.append(block)
        size += len(block.text)
        if size >= limit:
            pages.append(ParsedPage(len(pages) + 1, current))
            current, size = [], 0
    if current or not pages:
        pages.append(ParsedPage(len(pages) + 1, current))
    return pages


def _text_blocks(text: str) -> list[Block]:
    return [Block(p.strip()) for p in _BLANK_RUN.split(text.replace("\r\n", "\n")) if p.strip()]


def _markdown_blocks(text: str) -> list[Block]:
    """Headings (``#``) become heading blocks; the rest is split on blank lines, fences intact."""
    blocks: list[Block] = []
    paragraph: list[str] = []
    fenced = False

    def end_paragraph() -> None:
        body = "\n".join(paragraph).strip()
        if body:
            blocks.append(Block(body))
        paragraph.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            paragraph.append(line)
            continue
        heading = None if fenced else _ATX_HEADING.match(line)
        if heading and heading.group(2):
            end_paragraph()
            blocks.append(Block(heading.group(2), heading_level=len(heading.group(1))))
        elif not fenced and not line.strip():
            end_paragraph()
        else:
            paragraph.append(line)
    end_paragraph()
    return blocks


def parse_file(kind: str, data: bytes) -> tuple[list[ParsedPage], str]:
    """Pages of blocks for a file, and the chunk kind to store (``text``).

    :raises ValueError: the bytes cannot be read as that kind.
    """
    if kind == "pdf":
        return extract_pages(data), "text"
    text = decode_text(data) if kind in ("md", "txt") else ""
    if kind == "md":
        return paginate(_markdown_blocks(text)), "text"
    if kind == "txt":
        return paginate(_text_blocks(text)), "text"
    raise ValueError(f"cannot read a {kind} file as pages (tables go through tabular)")
