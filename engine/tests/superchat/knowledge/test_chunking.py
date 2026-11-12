"""Chunking keeps page numbers and heading paths; parsing turns each kind into pages."""

from __future__ import annotations

import io

import pytest

from omnigent.superchat.knowledge.chunking import MAX_CHARS, Block, ParsedPage, chunk_pages
from omnigent.superchat.knowledge.parse import INDEXABLE_KINDS, parse_file
from omnigent.superchat.knowledge.pdf import extract_pages, render_page, render_thumbnails
from tests.superchat.knowledge._fixtures import make_pdf


def test_chunks_never_span_pages_and_keep_the_page_number() -> None:
    pages = [
        ParsedPage(1, [Block("alpha line one"), Block("alpha line two")]),
        ParsedPage(2, [Block("beta line")]),
    ]
    chunks = chunk_pages(pages)
    assert [(c.page_start, c.page_end) for c in chunks] == [(1, 1), (2, 2)]
    assert chunks[0].text == "alpha line one\nalpha line two"


def test_a_heading_starts_a_chunk_and_the_path_carries_over_pages() -> None:
    pages = [
        ParsedPage(
            1, [Block("Intro", 1), Block("hello"), Block("Details", 2), Block("deep text")]
        ),
        ParsedPage(2, [Block("still details")]),
    ]
    chunks = chunk_pages(pages)
    assert [c.heading_path for c in chunks] == ["Intro", "Intro > Details", "Intro > Details"]
    assert chunks[1].text.startswith("Details")
    assert chunks[2].page_start == 2


def test_a_sibling_heading_replaces_its_sibling_in_the_path() -> None:
    pages = [ParsedPage(1, [Block("A", 1), Block("x"), Block("B", 1), Block("y")])]
    assert [c.heading_path for c in chunk_pages(pages)] == ["A", "B"]


def test_long_paragraph_is_split_under_the_ceiling_at_sentences() -> None:
    sentence = "This is a sentence about revenue. "
    chunks = chunk_pages([ParsedPage(1, [Block(sentence * 200)])])
    assert len(chunks) > 3
    assert all(len(c.text) <= MAX_CHARS for c in chunks)
    assert all(c.text.endswith(".") for c in chunks)


def test_atomic_blocks_are_not_merged_and_a_lone_sheet_heading_is_not_a_chunk() -> None:
    pages = [
        ParsedPage(
            1,
            [Block("Sheet1", 1), Block("a | b\n1 | 2", atomic=True), Block("c | d", atomic=True)],
        )
    ]
    chunks = chunk_pages(pages, kind="sheet")
    assert [c.text for c in chunks] == ["a | b\n1 | 2", "c | d"]
    assert {c.heading_path for c in chunks} == {"Sheet1"}
    assert {c.kind for c in chunks} == {"sheet"}


def test_pdf_pages_headings_and_text_layer() -> None:
    pdf = make_pdf(
        [
            [("1. Revenue overview", 20), ("Revenue grew 12 percent in Montreal.", 11)],
            [("2. Risks", 20), ("Currency exposure is the main risk.", 11)],
        ]
    )
    pages = extract_pages(pdf)
    assert [p.page_no for p in pages] == [1, 2]
    chunks = chunk_pages(pages)
    assert [(c.page_start, c.heading_path) for c in chunks] == [
        (1, "1. Revenue overview"),
        (2, "2. Risks"),
    ]
    assert "Currency exposure" in chunks[1].text


def test_a_page_without_text_is_counted_but_has_no_chunks() -> None:
    pdf = make_pdf([[("Some words here", 11)], []])
    pages = extract_pages(pdf)
    assert [p.has_text for p in pages] == [True, False]
    assert len(chunk_pages(pages)) == 1


def test_thumbnails_are_256px_and_a_page_render_is_1024px_webp() -> None:
    from PIL import Image

    pdf = make_pdf([[("page one", 12)], [("page two", 12)]])
    thumbs = list(render_thumbnails(pdf))
    assert [n for n, _ in thumbs] == [1, 2]
    thumb = Image.open(io.BytesIO(thumbs[0][1]))
    page = Image.open(io.BytesIO(render_page(pdf, 2) or b""))
    assert (thumb.format, thumb.width) == ("WEBP", 256)
    assert (page.format, page.width) == ("WEBP", 1024)
    assert render_page(pdf, 3) is None


def test_unreadable_pdf_is_a_value_error() -> None:
    try:
        extract_pages(b"not a pdf")
    except ValueError as exc:
        assert "PDF" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_markdown_headings_and_fences() -> None:
    text = "# Plan\nintro\n\n## Steps\n```\n# not a heading\n```\nlast\n"
    pages, kind = parse_file("md", text.encode())
    chunks = chunk_pages(pages, kind=kind)
    assert [c.heading_path for c in chunks] == ["Plan", "Plan > Steps"]
    assert "# not a heading" in chunks[1].text


def test_text_file_gets_virtual_pages_for_citations() -> None:
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(20))
    pages, _ = parse_file("txt", body.encode())
    assert len(pages) > 2
    chunks = chunk_pages(pages)
    assert chunks[0].page_start == 1 and chunks[-1].page_start == len(pages)


def test_tables_are_not_read_as_pages_of_text() -> None:
    for kind in ("csv", "xlsx"):
        with pytest.raises(ValueError):
            parse_file(kind, b"a,b\n1,2\n")


def test_indexable_kinds() -> None:
    assert {"pdf", "txt", "md", "csv", "xlsx"} == INDEXABLE_KINDS
