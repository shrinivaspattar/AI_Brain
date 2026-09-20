from __future__ import annotations

import re
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.document_chunk import DocumentChunk

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric/underscore tokens, no stemming - the same
    tokenizer Experiment 001 measured."""
    return _TOKEN_RE.findall(text.lower())


class Bm25Index:
    """In-memory Okapi BM25 over chunk contents (rank_bm25 defaults, as in
    Experiment 001). Built from every chunk that has an embedding, so it
    covers the same chunks dense search does."""

    def __init__(self, chunk_ids: list[int], contents: list[str]):
        self._ids = chunk_ids
        self._bm25 = None
        if contents:
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi([tokenize(content) for content in contents])

    def __len__(self) -> int:
        return len(self._ids)

    def search(self, query: str, limit: int) -> list[int]:
        """Chunk ids by descending BM25 score, ties by id, limited to chunks
        that actually contain at least one query word.

        Filtering on containment, not on score > 0: Okapi BM25 gives a zero
        or negative idf to a word found in half or more of the chunks, so on
        a small corpus a genuine match can score <= 0 and would otherwise be
        dropped. At Experiment 001's scale (1,000+ chunks) the two rules
        agree."""
        tokens = tokenize(query)
        if self._bm25 is None or not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        doc_freqs = self._bm25.doc_freqs
        matching = [i for i in range(len(self._ids)) if any(t in doc_freqs[i] for t in tokens)]
        matching.sort(key=lambda i: (-scores[i], self._ids[i]))
        return [self._ids[i] for i in matching[:limit]]


class Bm25IndexCache:
    """Builds the index once and rebuilds it only when the set of embedded
    chunks changes. A cheap `(database, count, max id)` query is the check:
    chunks are never edited in place (re-embedding deletes and recreates
    them with new ids), so a change in either number means new content.

    Personal-corpus scale: the index lives in process memory and a rebuild
    reads every chunk's text. That is fine for thousands of chunks and would
    need rethinking for millions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._signature: tuple | None = None
        self._index: Bm25Index | None = None

    def get(self, db: Session) -> Bm25Index:
        embedded = DocumentChunk.embedding.is_not(None)
        count, max_id = db.execute(select(func.count(DocumentChunk.id), func.max(DocumentChunk.id)).where(embedded)).one()
        signature = (db.get_bind().url.render_as_string(hide_password=True), count, max_id)

        with self._lock:
            if self._index is None or signature != self._signature:
                rows = db.execute(
                    select(DocumentChunk.id, DocumentChunk.content).where(embedded).order_by(DocumentChunk.id)
                ).all()
                self._index = Bm25Index([r[0] for r in rows], [r[1] for r in rows])
                self._signature = signature
            return self._index


default_bm25_cache = Bm25IndexCache()
