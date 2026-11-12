"""Shared pieces of the runner knowledge tests: tiny PDFs, a fake embedder, a wired indexer."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.superchat.knowledge.db import TurnPassage
from omnigent.superchat.knowledge.embedder import (
    Embedded,
    Embedder,
    EmbeddingPlan,
    EmbeddingUnavailable,
)
from omnigent.superchat.knowledge.indexer import Indexer
from omnigent.superchat.knowledge.rerank import Reranker, RerankUnavailable

DIMS = 64
TAG = "openrouter:fake-embed:64"

#: Words the fake model treats as the same idea (a stand-in for meaning).
SYNONYMS = {
    "car": "vehicle",
    "automobile": "vehicle",
    "vehicle": "vehicle",
    "money": "currency",
    "cash": "currency",
    "currency": "currency",
}


def make_pdf(pages: list[list[tuple[str, int]]]) -> bytes:
    """A PDF with one page per list of ``(line, font size)``; no ML, no scans."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    for lines in pages:
        y = 780
        for text, size in lines:
            pdf.setFont("Helvetica-Bold" if size > 12 else "Helvetica", size)
            pdf.drawString(72, y, text)
            y -= size + 8
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def concept_vector(text: str) -> list[float]:
    """Deterministic bag of concepts: a word and its synonyms land in the same slot."""
    vector = [0.0] * DIMS
    for word in text.lower().replace("\n", " ").split():
        word = "".join(ch for ch in word if ch.isalnum())
        if word:
            concept = SYNONYMS.get(word, word)
            vector[int.from_bytes(concept.encode()[:4].ljust(4, b"\0"), "big") % DIMS] += 1.0
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


@dataclass
class FakeEmbedder(Embedder):
    """Stands in for the model proxy (no network, no keys)."""

    reason: str | None = None  # set: cannot embed, for that reason
    fail_with: str | None = None
    tag: str = TAG
    calls: list[list[str]] = field(default_factory=list)

    def plan(self) -> EmbeddingPlan:
        if self.reason:
            raise EmbeddingUnavailable(self.reason)
        return EmbeddingPlan("openrouter", "fake-embed", DIMS, self.tag)

    def embed(self, texts: list[str]) -> Embedded:
        self.plan()
        if self.fail_with:
            raise EmbeddingUnavailable(self.fail_with)
        self.calls.append(list(texts))
        return Embedded([concept_vector(t) for t in texts], self.tag)


def build_indexer(tmp_path: Path, embedder: Embedder | None = None) -> tuple[Indexer, Path]:
    workspace = tmp_path / "workspace"
    (workspace / "your_files").mkdir(parents=True)
    indexer = Indexer(
        tmp_path / "home" / ".superchat" / "knowledge", workspace, embedder or FakeEmbedder()
    )
    return indexer, workspace


def run_all(indexer: Indexer) -> None:
    """Run every due file to the end, as the runtime thread would."""
    while indexer.process_next():
        pass


@dataclass
class FakeReranker(Reranker):
    """Puts the passages containing ``prefer`` first (a stand-in for the model's judgment)."""

    prefer: str = ""
    reason: str | None = None  # set: cannot rerank, for that reason
    calls: list[tuple[str, int]] = field(default_factory=list)

    def rerank(self, query: str, passages: list[str]) -> list[int]:
        if self.reason:
            raise RerankUnavailable(self.reason)
        self.calls.append((query, len(passages)))
        order = range(len(passages))
        return sorted(order, key=lambda i: (self.prefer.lower() not in passages[i].lower(), i))


def put_turn(
    indexer: Indexer,
    chat_id: str,
    turn_item_id: str,
    date: str,
    text: str,
    *,
    origin: str = "main",
    project: str = "",
    refs: dict[str, list[str]] | None = None,
    last_item_id: str = "",
) -> None:
    """A conversation passage with its C1 metadata and C5 refs (ADR 0016), stored through the
    index's turn writer and embedded by its pass (no compaction needed)."""
    indexer.index_turns(
        [
            TurnPassage(
                chat_id=chat_id,
                turn_item_id=turn_item_id,
                date=date,
                origin=origin,
                project=project,
                person_text=text.split(".")[0],
                text=text,
                main_id=chat_id,
                refs=refs or {},
                last_item_id=last_item_id,
            )
        ]
    )
    run_all(indexer)
