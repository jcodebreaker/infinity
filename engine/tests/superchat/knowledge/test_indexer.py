"""The host's indexer: PDF pages and thumbnails, hybrid ranking, reconcile, delete."""

from __future__ import annotations

import os
from pathlib import Path

from omnigent.superchat.knowledge.db import file_id_for
from omnigent.superchat.knowledge.indexer import MAX_ATTEMPTS
from tests.superchat.knowledge._fixtures import FakeEmbedder, build_indexer, make_pdf, run_all

PDF = make_pdf(
    [
        [("1. Overview", 20), ("This report covers the quarterly results.", 11)],
        [("2. Risks", 20), ("Currency exposure is the main risk for the car division.", 11)],
    ]
)


def _put(workspace: Path, rel: str, data: bytes | str) -> Path:
    path = workspace / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def test_pdf_is_indexed_with_pages_headings_and_thumbnails(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/uploads/2026-10-09/report.pdf", PDF)
    assert indexer.scan() == {"added": 1, "changed": 0, "removed": 0}
    run_all(indexer)
    status = indexer.status()
    (file,) = status["files"]
    assert (file["state"], file["pages"], file["search"]) == ("searchable", 2, "hybrid")
    assert file["path"] == "your_files/uploads/2026-10-09/report.pdf"
    assert status["embeddings"] == {"available": True, "reason": None}
    fid = file["file_id"]
    assert fid == file_id_for(file["path"])
    assert indexer.thumbnail(fid, 1)[:4] == b"RIFF" and indexer.thumbnail(fid, 2)
    assert indexer.thumbnail(fid, 3) is None
    page = indexer.page(indexer.db.get_file(fid), 2)
    assert "Currency exposure" in page["text"] and page["image_mime"] == "image/webp"


def test_hybrid_search_finds_a_synonym_that_keywords_miss(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/report.pdf", PDF)
    indexer.scan()
    run_all(indexer)
    hybrid = indexer.search("automobile")
    assert hybrid["mode"] == "hybrid"
    top = hybrid["results"][0]
    assert (top["page"], top["file_name"]) == (2, "report.pdf")
    assert top["thumbnail_url"] and top["abs_path"].endswith("your_files/report.pdf")
    # Keyword-only cannot connect "automobile" to "car".
    words = indexer.db.search("automobile", limit=5)
    assert words == []


def test_text_files_and_file_filter(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/a.md", "# Plan\nBuy a vehicle next year.\n")
    _put(ws, "your_files/b.txt", "The vehicle budget is small.\n")
    _put(ws, "your_files/ignored.png", b"\x89PNG")
    indexer.scan()
    run_all(indexer)
    assert len(indexer.status()["files"]) == 2
    only_b = indexer.search("vehicle", file_ids=[file_id_for("your_files/b.txt")])
    assert {r["file_name"] for r in only_b["results"]} == {"b.txt"}
    assert indexer.search("vehicle", file_ids=[])["results"] == []


def test_keyword_only_without_an_embedding_connection_says_why(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    _put(ws, "your_files/notes.txt", "Currency exposure is the main risk.\n")
    indexer.scan()
    run_all(indexer)
    (file,) = indexer.status()["files"]
    assert (file["search"], file["keyword_only_reason"]) == ("keyword", "no_connection")
    assert indexer.status()["embeddings"] == {"available": False, "reason": "no_connection"}
    found = indexer.search("currency exposure")
    assert (found["mode"], found["keyword_only_reason"]) == ("keyword", "no_connection")
    assert found["results"][0]["file_name"] == "notes.txt"


def test_a_key_added_later_is_embedded_by_the_one_time_reindex(tmp_path: Path) -> None:
    embedder = FakeEmbedder(reason="no_connection")
    indexer, ws = build_indexer(tmp_path, embedder)
    _put(ws, "your_files/notes.txt", "The automobile market is changing.\n")
    indexer.scan()
    run_all(indexer)
    assert indexer.maybe_reindex() == 0
    embedder.reason = None
    assert indexer.maybe_reindex() == 1
    assert indexer.maybe_reindex() == 0  # once per model tag
    run_all(indexer)
    assert indexer.status()["files"][0]["search"] == "hybrid"
    assert indexer.search("car")["results"][0]["file_name"] == "notes.txt"


def test_a_provider_hiccup_retries_then_settles_for_keywords(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path, FakeEmbedder(fail_with="error"))
    clock = [1000.0]
    indexer._clock = lambda: clock[0]
    _put(ws, "your_files/notes.txt", "alpha beta\n")
    indexer.scan()
    for _ in range(MAX_ATTEMPTS):
        assert indexer.process_next()
        clock[0] += 10_000
    run_all(indexer)
    (file,) = indexer.status()["files"]
    assert file["state"] == "searchable" and file["search"] == "keyword"
    assert file["keyword_only_reason"] == "error"


def test_reconcile_picks_up_changes_and_deletes(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    note = _put(ws, "your_files/note.txt", "first version about apples\n")
    _put(ws, "goals/launch/files/plan.md", "# Launch\nship oranges\n")
    _put(ws, "goals/launch/hidden_files/state.md", "secret pears\n")
    indexer.scan()
    run_all(indexer)
    assert {f["path"] for f in indexer.status()["files"]} == {
        "your_files/note.txt",
        "goals/launch/files/plan.md",
    }
    assert indexer.scan() == {"added": 0, "changed": 0, "removed": 0}
    note.write_text("second version about bananas\n")
    os.utime(note, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    assert indexer.scan()["changed"] == 1
    run_all(indexer)
    assert indexer.search("apples")["results"] == []
    assert indexer.search("bananas")["results"][0]["file_name"] == "note.txt"
    note.unlink()
    assert indexer.scan()["removed"] == 1
    assert indexer.search("bananas")["results"] == []
    assert indexer.search("oranges")["results"]


def test_delete_removes_passages_vectors_and_thumbnails(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)  # hybrid: the vectors go too
    pdf = _put(ws, "your_files/report.pdf", PDF)
    indexer.scan()
    run_all(indexer)
    fid = file_id_for("your_files/report.pdf")
    thumbs = tmp_path / "home" / ".superchat" / "knowledge" / "thumbs" / fid
    assert thumbs.is_dir()
    pdf.unlink()
    indexer.scan()
    assert not thumbs.exists()
    assert indexer.db.chunks_for(fid) == []
    assert indexer.search("currency")["results"] == []


def test_a_saved_artifact_outside_the_files_area_stays_indexed(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "projects/q3/summary.md", "# Summary\nRevenue grew.\n")
    assert indexer.add(ws / "projects/q3/summary.md", explicit=True)
    assert not indexer.add(ws / "projects/q3/missing.md", explicit=True)
    assert not indexer.add("/etc/passwd")
    run_all(indexer)
    indexer.scan()  # reconcile must not drop an explicit file
    assert indexer.search("revenue")["results"][0]["path"] == "projects/q3/summary.md"


def test_unreadable_pdf_ends_failed_with_a_reason(tmp_path: Path) -> None:
    indexer, ws = build_indexer(tmp_path)
    _put(ws, "your_files/broken.pdf", b"not a pdf")
    indexer.scan()
    clock = [1000.0]
    indexer._clock = lambda: clock[0]
    for _ in range(MAX_ATTEMPTS):
        indexer.process_next()
        clock[0] += 10_000
    (file,) = indexer.status()["files"]
    assert file["state"] == "failed" and "PDF" in file["error"]


def test_changed_embedding_model_drops_old_vectors(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    indexer, ws = build_indexer(tmp_path, embedder)
    _put(ws, "your_files/a.txt", "automobile news\n")
    _put(ws, "your_files/b.txt", "currency news\n")
    indexer.scan()
    run_all(indexer)
    embedder.tag = "openrouter:other-model:64"
    assert indexer.reindex() == 2
    run_all(indexer)
    assert indexer.db.vector_tag() == "openrouter:other-model:64"
    assert indexer.search("car")["results"]


AURORA = make_pdf(
    [
        [("Search check", 20), ("This document is used to check that file search works.", 11)],
        [("Team", 20), ("The team meets for the weekly review and the planning of the year.", 11)],
        [
            ("Project Aurora", 20),
            ("Project Aurora: the approved budget is 4.7 million dollars,", 11),
            ("delivery planned for March 2027.", 11),
        ],
        [("Appendix", 20), ("Notes on the file and the page layout, and the cite style.", 11)],
    ]
)


def _first_page(tmp_path: Path, query: str) -> tuple[int, dict]:
    indexer, ws = build_indexer(tmp_path, FakeEmbedder(reason="no_connection"))
    _put(ws, "your_files/uploads/search-check.pdf", AURORA)
    indexer.scan()
    run_all(indexer)
    found = indexer.search(query)
    assert found["mode"] == "keyword" and found["results"]
    return found["results"][0]["page"], found


def test_natural_language_query_finds_the_page_without_vectors(tmp_path: Path) -> None:
    page, _ = _first_page(
        tmp_path,
        "use files_search on my files to find the approved budget for Project Aurora, "
        "and cite the file and page",
    )
    assert page == 3


def test_more_natural_questions_rank_the_right_page(tmp_path: Path) -> None:
    for query, expected in [
        ("What is the budget of Aurora?", 3),
        ("when is delivery planned?", 3),
        ("how much money, in dollars, was approved", 3),
        ("budgets", 3),  # prefix of a longer word
        ("weekly team review", 2),
    ]:
        page, _ = _first_page(tmp_path / str(abs(hash(query))), query)
        assert page == expected, query


def test_query_syntax_is_never_executed(tmp_path: Path) -> None:
    for query in [
        'budget" OR *',
        "NEAR(budget aurora)",
        "budget AND NOT",
        "-budget ^aurora:",
        "a:b*",
    ]:
        _first_page(tmp_path / str(abs(hash(query))), query + " aurora")
