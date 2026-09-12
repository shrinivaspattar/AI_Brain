from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.service import DeduplicationService
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _unit_vector(dimensions: int, index: int) -> list[float]:
    vector = [0.0] * dimensions
    vector[index] = 1.0
    return vector


def test_find_exact_duplicates_against_real_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)

        doc_a = document_service.create_document(
            DocumentCreate(
                title="a.txt",
                source="/dedup-test/a.txt",
                source_type="txt",
                content_hash="shared-hash-123",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="b.txt",
                source="/dedup-test/b.txt",
                source_type="txt",
                content_hash="shared-hash-123",
            )
        )
        doc_c = document_service.create_document(
            DocumentCreate(
                title="c.txt",
                source="/dedup-test/c.txt",
                source_type="txt",
                content_hash="unique-hash-456",
            )
        )

        try:
            service = DeduplicationService(db)
            groups = service.find_exact_duplicates()

            own_groups = [g for g in groups if g.content_hash == "shared-hash-123"]
            assert len(own_groups) == 1

            group_doc_ids = {d.id for d in own_groups[0].documents}
            assert group_doc_ids == {doc_a.id, doc_b.id}

            # the unique-hash document must not appear in any group
            all_grouped_ids = {d.id for g in groups for d in g.documents}
            assert doc_c.id not in all_grouped_ids

        finally:
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id, doc_c.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_plan_exact_duplicate_cleanup_against_real_database() -> None:
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)

        doc_a = document_service.create_document(
            DocumentCreate(
                title="original.txt",
                source="/dedup-test/original.txt",
                source_type="txt",
                content_hash="plan-test-hash-789",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="original-copy.txt",
                source="/dedup-test/original-copy.txt",
                source_type="txt",
                content_hash="plan-test-hash-789",
            )
        )

        # Force a deterministic ordering rather than relying on clock
        # resolution between the two create_document() calls above.
        doc_b.created_at = doc_a.created_at + timedelta(seconds=1)
        db.commit()

        try:
            service = DeduplicationService(db)
            plans = service.plan_exact_duplicate_cleanup()

            own_plans = [p for p in plans if p.content_hash == "plan-test-hash-789"]
            assert len(own_plans) == 1

            plan = own_plans[0]
            # doc_a was created first (earlier created_at), so it should
            # be the one kept; doc_b is the proposed deletion.
            assert plan.keep.id == doc_a.id
            assert len(plan.actions) == 1
            assert plan.actions[0].action == "delete"
            assert plan.actions[0].document.id == doc_b.id

            # nothing was actually deleted - both documents still exist
            still_present = db.scalars(
                select(Document).where(Document.id.in_([doc_a.id, doc_b.id]))
            ).all()
            assert len(still_present) == 2

        finally:
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_find_near_duplicate_documents_against_real_pgvector() -> None:
    """The important test: proves the pgvector chunk-to-chunk cosine
    distance cross-join actually works against a real database, not
    just that the SQLAlchemy statement builds without raising.
    """
    dimensions = settings.EMBEDDING_DIMENSIONS

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)

        doc_a = document_service.create_document(
            DocumentCreate(
                title="report-v1.pdf", source="/dedup-test/report-v1.pdf", source_type="pdf"
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="report-v2.pdf", source="/dedup-test/report-v2.pdf", source_type="pdf"
            )
        )
        doc_c = document_service.create_document(
            DocumentCreate(
                title="unrelated.pdf", source="/dedup-test/unrelated.pdf", source_type="pdf"
            )
        )

        # a and b: near-identical opening content (orthogonal vector +
        # a tiny nudge, so distance is small but not exactly zero)
        vector_a = _unit_vector(dimensions, 0)
        vector_b = _unit_vector(dimensions, 0)
        vector_b[1] = 0.05

        vector_c = _unit_vector(dimensions, 5)

        db.add_all(
            [
                DocumentChunk(
                    document_id=doc_a.id,
                    chunk_index=0,
                    content="chunk a",
                    embedding=vector_a,
                ),
                DocumentChunk(
                    document_id=doc_b.id,
                    chunk_index=0,
                    content="chunk b",
                    embedding=vector_b,
                ),
                DocumentChunk(
                    document_id=doc_c.id,
                    chunk_index=0,
                    content="chunk c",
                    embedding=vector_c,
                ),
            ]
        )
        db.commit()

        try:
            service = DeduplicationService(db)

            # broad threshold sweep to isolate this test's own docs from
            # whatever else exists in aibrain_test
            all_pairs = service.find_near_duplicate_documents(
                similarity_threshold=0.5,
                limit=1000,
            )
            own_ids = {doc_a.id, doc_b.id, doc_c.id}
            own_pairs = [
                p
                for p in all_pairs
                if p.document_a.id in own_ids and p.document_b.id in own_ids
            ]

            pair_id_sets = [{p.document_a.id, p.document_b.id} for p in own_pairs]
            assert {doc_a.id, doc_b.id} in pair_id_sets
            assert {doc_a.id, doc_c.id} not in pair_id_sets
            assert {doc_b.id, doc_c.id} not in pair_id_sets

            matched = next(
                p for p in own_pairs if {p.document_a.id, p.document_b.id} == {doc_a.id, doc_b.id}
            )
            assert matched.similarity > 0.9

        finally:
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id.in_([doc_a.id, doc_b.id, doc_c.id])
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id, doc_c.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_find_near_duplicate_documents_respects_threshold() -> None:
    dimensions = settings.EMBEDDING_DIMENSIONS

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        document_service = DocumentService(db)

        doc_a = document_service.create_document(
            DocumentCreate(
                title="x.txt", source="/dedup-test/x.txt", source_type="txt"
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="y.txt", source="/dedup-test/y.txt", source_type="txt"
            )
        )

        db.add_all(
            [
                DocumentChunk(
                    document_id=doc_a.id,
                    chunk_index=0,
                    content="chunk a",
                    embedding=_unit_vector(dimensions, 0),
                ),
                DocumentChunk(
                    document_id=doc_b.id,
                    chunk_index=0,
                    content="chunk b",
                    embedding=_unit_vector(dimensions, 1),
                ),
            ]
        )
        db.commit()

        try:
            service = DeduplicationService(db)

            # orthogonal vectors -> similarity 0.0, well below any
            # reasonable threshold
            pairs = service.find_near_duplicate_documents(
                similarity_threshold=0.5,
                limit=1000,
            )
            own_ids = {doc_a.id, doc_b.id}
            own_pairs = [
                p
                for p in pairs
                if p.document_a.id in own_ids and p.document_b.id in own_ids
            ]

            assert own_pairs == []

        finally:
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.commit()
