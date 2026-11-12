"""PDF text layer, thumbnails and page renders with pypdfium2 (Apache-2.0 / BSD-3 PDFium bindings).

No OCR and no ML: a page without a text layer yields no blocks (the deep-parse slice reads those).
Headings are lines set noticeably larger than the document's body text.
"""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Iterator
from typing import Any

from omnigent.superchat.knowledge.chunking import Block, ParsedPage

THUMB_WIDTH = 256
PAGE_WIDTH = 1024
_WEBP_QUALITY = 80
#: A line is a heading when its font is this much larger than the body font.
_HEADING_RATIO = 1.15
_HEADING_MAX_CHARS = 140


def _lines(textpage: Any, raw: Any) -> list[tuple[str, float]]:
    """``(text, font size of its first character)`` for each non-empty line of a page."""
    text = textpage.get_text_range()
    lines: list[tuple[str, float]] = []
    start = 0
    n = len(text)
    while start < n:
        end = start
        while end < n and text[end] not in "\r\n":
            end += 1
        line = text[start:end].strip()
        if line:
            first = start
            while first < end and text[first].isspace():
                first += 1
            lines.append((line, float(raw.FPDFText_GetFontSize(textpage.raw, first))))
        start = end
        while start < n and text[start] in "\r\n":
            start += 1
    return lines


def _is_heading(text: str, size: float, body: float) -> bool:
    return bool(body) and size >= body * _HEADING_RATIO and len(text) <= _HEADING_MAX_CHARS


def extract_pages(data: bytes) -> list[ParsedPage]:
    """The text layer of every page, as blocks with heading levels.

    :raises ValueError: the bytes are not a readable PDF.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"not a readable PDF ({exc})") from None
    try:
        per_page: list[list[tuple[str, float]]] = []
        weight: Counter[float] = Counter()
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                lines = _lines(textpage, raw)
            finally:
                textpage.close()
                page.close()
            per_page.append(lines)
            for line, size in lines:
                weight[round(size, 1)] += len(line)
        body = weight.most_common(1)[0][0] if weight else 0.0
        sizes = sorted(
            {
                round(size, 1)
                for lines in per_page
                for text, size in lines
                if _is_heading(text, size, body)
            },
            reverse=True,
        )
        level_of = {size: rank + 1 for rank, size in enumerate(sizes)}
        return [
            ParsedPage(
                index + 1,
                [
                    Block(
                        text, level_of[round(size, 1)] if _is_heading(text, size, body) else None
                    )
                    for text, size in lines
                ],
            )
            for index, lines in enumerate(per_page)
        ]
    finally:
        document.close()


def render_thumbnails(data: bytes) -> Iterator[tuple[int, bytes]]:
    """``(page number, WebP)`` of every page at thumbnail size, one page at a time."""
    import pypdfium2 as pdfium
    from PIL import Image

    document = pdfium.PdfDocument(data)
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                width_pt, _ = page.get_size()
                image = page.render(scale=THUMB_WIDTH / max(width_pt, 1.0)).to_pil().convert("RGB")
            finally:
                page.close()
            image.thumbnail((THUMB_WIDTH, THUMB_WIDTH * 4), Image.Resampling.LANCZOS)
            yield index + 1, _webp(image)
    finally:
        document.close()


def render_page(data: bytes, page_no: int) -> bytes | None:
    """One page (from 1) as a 1024 px-wide WebP, or ``None`` when the document has no such page."""
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return None
    try:
        if page_no < 1 or page_no > len(document):
            return None
        page = document[page_no - 1]
        try:
            width_pt, _ = page.get_size()
            image = page.render(scale=PAGE_WIDTH / max(width_pt, 1.0)).to_pil().convert("RGB")
        finally:
            page.close()
        return _webp(image)
    finally:
        document.close()


def _webp(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "WEBP", quality=_WEBP_QUALITY)
    return buffer.getvalue()
