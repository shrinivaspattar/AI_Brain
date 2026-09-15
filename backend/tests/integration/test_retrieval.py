from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.retrieval_service import RetrievalService
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _unit_vector(dimensions: int, index: int) -> list[float]:
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


def test_search_ranks_chunks_by_pgvector_cosine_distance() -> None:
    dimensions = settings.EMBEDDING_DIMENSIONS

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)

        doc_a = document_service.create_document(
            DocumentCreate(
                title="a.txt",
                source="/retrieval-test/a.txt",
                source_type="txt",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="b.txt",
                source="/retrieval-test/b.txt",
                source_type="txt",
            )
        )
        doc_c = document_service.create_document(
            DocumentCreate(
                title="c.txt",
                source="/retrieval-test/c.txt",
                source_type="txt",
            )
        )

        chunk_a = DocumentChunk(
            document_id=doc_a.id,
            chunk_index=0,
            content="chunk a",
            embedding=_unit_vector(dimensions, 0),
        )
        chunk_b = DocumentChunk(
            document_id=doc_b.id,
            chunk_index=0,
            content="chunk b",
            embedding=_unit_vector(dimensions, 1),
        )
        chunk_c = DocumentChunk(
            document_id=doc_c.id,
            chunk_index=0,
            content="chunk c",
            embedding=_unit_vector(dimensions, 2),
        )
        db.add_all([chunk_a, chunk_b, chunk_c])
        db.commit()
        db.refresh(chunk_b)
        db.refresh(chunk_c)

        # doc_b's and doc_c's chunks are orthogonal one-hot unit vectors
        # relative to the query - an EXACT (bit-for-bit, not approximate)
        # cosine-distance tie of 1.0, computed by pgvector itself: the
        # dot product of two disjoint one-hot vectors sums only zeros.
        # M15 froze `DocumentChunk.id` ASC as the deterministic secondary
        # sort key for such ties - read the actually-assigned ids rather
        # than assuming insertion order, so this test verifies the real
        # rule rather than an assumption about autoincrement behavior.
        tied_chunks_by_id = sorted([chunk_b, chunk_c], key=lambda c: c.id)
        expected_tie_order = [c.document_id for c in tied_chunks_by_id]

        try:
            fake_client = MagicMock()
            fake_client.embed.return_value = [_unit_vector(dimensions, 0)]

            service = RetrievalService(db, embedding_client=fake_client)

            def _own_results():
                # aibrain_test accumulates rows across test runs (no
                # per-test cleanup/isolation yet), so search broadly and
                # filter down to just this test's own documents rather
                # than assuming they're the global top-k.
                all_results = service.search("irrelevant query text", top_k=1000)
                own_document_ids = {doc_a.id, doc_b.id, doc_c.id}
                return [result for result in all_results if result.document.id in own_document_ids]

            results = _own_results()

            assert len(results) == 3
            assert results[0].document.id == doc_a.id
            assert results[0].distance == pytest.approx(0.0, abs=1e-6)
            assert results[1].distance == pytest.approx(1.0, abs=1e-6)
            assert results[2].distance == pytest.approx(1.0, abs=1e-6)
            # Exact order among the tied pair, not merely "both present" -
            # the frozen M15 rule (lower DocumentChunk.id first).
            assert [results[1].document.id, results[2].document.id] == expected_tie_order

            # Repeating the search must reproduce the identical ordering -
            # determinism, not merely a one-off correct result.
            repeated_results = _own_results()
            assert [r.document.id for r in repeated_results] == [r.document.id for r in results]

        finally:
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id.in_([doc_a.id, doc_b.id, doc_c.id])
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id, doc_c.id])
            ).delete(synchronize_session=False)
            db.commit()
