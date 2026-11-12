"""Cut parsed pages into passages that keep their page number and heading path.

Pure: pages of blocks in, :class:`Chunk` out. A chunk never spans two pages, starts a new chunk at
a heading, and carries the headings above it (``heading_path``). A block marked ``atomic`` (a
table slice) is never merged with another block.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

#: Packing target and hard ceiling of a passage, in characters.
TARGET_CHARS = 1000
MAX_CHARS = 1600
HEADING_SEP = " > "

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Block:
    """One line or paragraph of a page.

    :param text: The block's text.
    :param heading_level: ``1`` is the top heading level; ``None`` for body text.
    :param atomic: Never merged with a neighbour (a table slice keeps its header row).
    """

    text: str
    heading_level: int | None = None
    atomic: bool = False


@dataclass(frozen=True)
class ParsedPage:
    """One page of a file (a real PDF page, or a virtual page of a text file)."""

    page_no: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        """Whether the page has any text at all."""
        return any(block.text.strip() for block in self.blocks)


@dataclass(frozen=True)
class Chunk:
    """One passage: where it is and what it says."""

    page_start: int
    page_end: int
    kind: str
    heading_path: str
    text: str


def _split_long(text: str) -> Iterable[str]:
    """Pieces of at most :data:`MAX_CHARS`, cut at sentence ends, else at spaces, else hard."""
    if len(text) <= MAX_CHARS:
        yield text
        return
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > MAX_CHARS:
            cut = sentence.rfind(" ", 0, MAX_CHARS)
            cut = cut if cut > MAX_CHARS // 2 else MAX_CHARS
            head, sentence = sentence[:cut], sentence[cut:].lstrip()
            if current:
                yield current
                current = ""
            yield head
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            yield current
            current = ""
        current = f"{current} {sentence}".strip() if current else sentence
    if current:
        yield current


def chunk_pages(pages: Iterable[ParsedPage], *, kind: str = "text") -> list[Chunk]:
    """Pack *pages* into chunks, in reading order.

    :param pages: Parsed pages, ascending.
    :param kind: The chunk kind stored with every passage (``text``, ``sheet``, ``csv``).
    """
    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []  # (level, text) of the headings above the cursor

    def path() -> str:
        return HEADING_SEP.join(text for _, text in stack)

    for page in pages:
        pending: list[str] = []
        pending_path = path()
        size = 0

        def flush(page_no: int = page.page_no) -> None:
            nonlocal pending, size, pending_path
            body = "\n".join(pending).strip()
            if body:
                chunks.append(Chunk(page_no, page_no, kind, pending_path, body))
            pending, size, pending_path = [], 0, path()

        for block in page.blocks:
            text = block.text.strip()
            if not text:
                continue
            if block.heading_level is not None:
                flush()
                while stack and stack[-1][0] >= block.heading_level:
                    stack.pop()
                stack.append((block.heading_level, text))
                pending_path = path()
                pending, size = [text], len(text)
                continue
            if block.atomic:
                if len(pending) == 1 and stack and pending[0] == stack[-1][1]:
                    pending, size = [], 0  # a lone heading is already in the heading path
                flush()
                for piece in _split_long(text):
                    pending_path = path()
                    chunks.append(Chunk(page.page_no, page.page_no, kind, pending_path, piece))
                continue
            for piece in _split_long(text):
                if size and size + 1 + len(piece) > TARGET_CHARS:
                    flush()
                pending.append(piece)
                size += len(piece) + 1
        flush()
    return chunks
