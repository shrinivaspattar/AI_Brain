from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.bm25_index import Bm25IndexCache
from app.rag.retrieval_service import RetrievalService
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * settings.EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


@pytest.fixture
def corpus():
    """Three documents. The query's embedding equals doc_dense's, so dense
    search ranks doc_dense first; only doc_keyword contains the rare word
    that BM25 keys on, but its embedding is orthogonal to the query's, so
    dense search ranks it no better than a tie for last."""
    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain_test"))
    with Session(engine) as db:
        service = DocumentService(db)
        docs = {
            name: service.create_document(
                DocumentCreate(title=f"{name}.txt", source=f"/hybrid-test/{name}.txt", source_type="txt")
            )
            for name in ("dense", "keyword", "filler")
        }
        db.add_all(
            [
                DocumentChunk(document_id=docs["dense"].id, chunk_index=0,
                              content="general talk about search and retrieval", embedding=_unit_vector(0)),
                DocumentChunk(document_id=docs["keyword"].id, chunk_index=0,
                              content="calibrate the zyxquartz_9 module before use", embedding=_unit_vector(1)),
                DocumentChunk(document_id=docs["filler"].id, chunk_index=0,
                              content="unrelated filler text about weather", embedding=_unit_vector(2)),
            ]
        )
        db.commit()
        try:
            yield db, docs
        finally:
            ids = [d.id for d in docs.values()]
            db.query(DocumentChunk).filter(DocumentChunk.document_id.in_(ids)).delete(synchronize_session=False)
            db.query(Document).filter(Document.id.in_(ids)).delete(synchronize_session=False)
            db.commit()


def _service(db):
    client = MagicMock()
    client.embed.return_value = [_unit_vector(0)]
    return RetrievalService(db, embedding_client=client, bm25_cache=Bm25IndexCache())


def _own(results, docs):
    ids = {d.id for d in docs.values()}
    return [r for r in results if r.document.id in ids]


def test_hybrid_off_is_dense_only_and_misses_the_keyword_document_at_the_top(corpus, monkeypatch):
    db, docs = corpus
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", False)
    results = _own(_service(db).search("zyxquartz_9", top_k=1000), docs)
    assert results[0].document.id == docs["dense"].id


def test_hybrid_on_surfaces_the_keyword_match_first(corpus, monkeypatch):
    db, docs = corpus
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", True)
    results = _own(_service(db).search("zyxquartz_9", top_k=1000), docs)
    assert results[0].document.id == docs["keyword"].id


def test_hybrid_results_carry_a_real_cosine_distance_even_for_bm25_only_hits(corpus, monkeypatch):
    db, docs = corpus
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", True)
    results = _own(_service(db).search("zyxquartz_9", top_k=1000), docs)
    by_doc = {r.document.id: r for r in results}
    assert by_doc[docs["keyword"].id].distance == pytest.approx(1.0, abs=1e-6)  # orthogonal to the query
    assert by_doc[docs["dense"].id].distance == pytest.approx(0.0, abs=1e-6)


def test_hybrid_respects_top_k_and_is_deterministic(corpus, monkeypatch):
    db, docs = corpus
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", True)
    service = _service(db)
    first = service.search("zyxquartz_9", top_k=2)
    second = service.search("zyxquartz_9", top_k=2)
    assert len(first) == 2
    assert [r.chunk.id for r in first] == [r.chunk.id for r in second]


def test_hybrid_with_no_bm25_match_still_returns_dense_results(corpus, monkeypatch):
    db, docs = corpus
    monkeypatch.setattr(settings, "SEARCH_HYBRID_ENABLED", True)
    results = _own(_service(db).search("qqqqnomatchqqqq", top_k=1000), docs)
    assert results[0].document.id == docs["dense"].id
