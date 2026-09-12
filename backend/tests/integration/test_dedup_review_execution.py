"""Real-database tests for the KRM dedup review DECISION layer.

Proves three things against real Postgres that a mocked-db unit test
cannot: the JSONB evidence snapshot round-trips correctly, the
review/member rows and their foreign keys actually persist as designed,
and - the core safety property of this whole design - that creating or
reviewing a DuplicateReview never modifies the Document/ImportJob rows
it references. No dedup execution mechanism exists anywhere in this
codebase; these tests confirm that remains true at the data layer too.
"""

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dedup.review_service import DedupReviewService
from app.dedup.service import ExactDuplicateGroup, NearDuplicatePair
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMember,
    DuplicateReviewMemberRole,
    DuplicateReviewStatus,
)
from app.models.document import Document
from app.models.import_job import ImportJob, ImportStatus
from app.schemas.document import DocumentCreate
from app.services.document_service import DocumentService


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def test_create_review_from_exact_group_persists_against_real_database() -> None:
    engine = _engine()

    with Session(engine) as db:
        import_job = ImportJob(
            name="Dedup Review Test Import",
            source_path="/dedup-review-test/source",
            source_type="filesystem",
            status=ImportStatus.COMPLETED,
        )
        db.add(import_job)
        db.commit()
        db.refresh(import_job)

        document_service = DocumentService(db)
        older = document_service.create_document(
            DocumentCreate(
                title="report.txt",
                source="/dedup-review-test/report.txt",
                source_type="txt",
                content_hash="review-test-hash-1",
                import_job_id=import_job.id,
            )
        )
        newer = document_service.create_document(
            DocumentCreate(
                title="report-copy.txt",
                source="/dedup-review-test/report-copy.txt",
                source_type="txt",
                content_hash="review-test-hash-1",
            )
        )
        # Force deterministic created_at ordering, matching the pattern
        # used for the dry-run-plan tests (clock resolution isn't
        # reliable enough to trust across two sequential inserts).
        newer.created_at = older.created_at + timedelta(seconds=1)
        db.commit()

        try:
            service = DedupReviewService(db)
            group = ExactDuplicateGroup(
                content_hash="review-test-hash-1", documents=[newer, older]
            )
            review = service.create_review_from_exact_group(group)

            assert review.id is not None
            assert review.match_type == DuplicateMatchType.EXACT
            assert review.status == DuplicateReviewStatus.PENDING

            members = list(
                db.scalars(
                    select(DuplicateReviewMember).where(
                        DuplicateReviewMember.review_id == review.id
                    )
                )
            )
            assert len(members) == 2
            canonical = next(
                m
                for m in members
                if m.role == DuplicateReviewMemberRole.PROPOSED_CANONICAL
            )
            assert canonical.document_id == older.id

            # Provenance preserved: the evidence snapshot carries the
            # real import_job_id through untouched.
            older_evidence = next(
                m
                for m in review.evidence["members"]
                if m["document_id"] == older.id
            )
            assert older_evidence["import_job_id"] == import_job.id

            # Safety: creating a review must not modify the documents
            # or the import job it references.
            db.refresh(older)
            db.refresh(newer)
            db.refresh(import_job)
            assert older.content_hash == "review-test-hash-1"
            assert newer.content_hash == "review-test-hash-1"
            assert import_job.status == ImportStatus.COMPLETED

        finally:
            db.query(DuplicateReviewMember).filter(
                DuplicateReviewMember.document_id.in_([older.id, newer.id])
            ).delete(synchronize_session=False)
            db.query(DuplicateReview).filter(
                DuplicateReview.content_hash == "review-test-hash-1"
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([older.id, newer.id])
            ).delete(synchronize_session=False)
            db.query(ImportJob).filter(ImportJob.id == import_job.id).delete(
                synchronize_session=False
            )
            db.commit()


def test_create_review_from_near_pair_persists_with_no_canonical() -> None:
    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(
                title="draft-v1.txt", source="/dedup-review-test/draft-v1.txt",
                source_type="txt",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="draft-v2.txt", source="/dedup-review-test/draft-v2.txt",
                source_type="txt",
            )
        )

        try:
            service = DedupReviewService(db)
            pair = NearDuplicatePair(
                document_a=doc_a, document_b=doc_b, similarity=0.92
            )
            review = service.create_review_from_near_pair(pair)

            members = list(
                db.scalars(
                    select(DuplicateReviewMember).where(
                        DuplicateReviewMember.review_id == review.id
                    )
                )
            )
            assert len(members) == 2
            assert all(
                m.role == DuplicateReviewMemberRole.DUPLICATE for m in members
            )
            # The ambiguous-candidate property, proven against real
            # persisted rows: no PROPOSED_CANONICAL exists anywhere for
            # a near-duplicate review.
            assert not any(
                m.role == DuplicateReviewMemberRole.PROPOSED_CANONICAL
                for m in members
            )

        finally:
            db.query(DuplicateReviewMember).filter(
                DuplicateReviewMember.document_id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.query(DuplicateReview).filter(
                DuplicateReview.id == review.id
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_approve_and_reject_against_real_database_preserve_source_data() -> None:
    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(
                title="a.txt", source="/dedup-review-test/a.txt",
                source_type="txt", content_hash="review-test-hash-2",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="a-copy.txt", source="/dedup-review-test/a-copy.txt",
                source_type="txt", content_hash="review-test-hash-2",
            )
        )
        original_doc_a_hash = doc_a.content_hash
        original_doc_b_hash = doc_b.content_hash

        service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="review-test-hash-2", documents=[doc_a, doc_b]
        )
        review = service.create_review_from_exact_group(group)

        try:
            approved = service.approve_review(
                review.id, reviewer_decision="Confirmed identical, safe to keep older."
            )
            assert approved.status == DuplicateReviewStatus.APPROVED
            assert approved.reviewed_at is not None

            # Safety: approving never touches the referenced documents.
            db.refresh(doc_a)
            db.refresh(doc_b)
            assert doc_a.content_hash == original_doc_a_hash
            assert doc_b.content_hash == original_doc_b_hash
            assert db.get(Document, doc_a.id) is not None
            assert db.get(Document, doc_b.id) is not None

            # Reviewing again (reject after approve) is permitted -
            # documented last-write-wins behavior - and still touches
            # nothing but the review row.
            rejected = service.reject_review(
                review.id, reviewer_decision="Changed my mind on reflection."
            )
            assert rejected.status == DuplicateReviewStatus.REJECTED
            db.refresh(doc_a)
            assert doc_a.content_hash == original_doc_a_hash

        finally:
            db.query(DuplicateReviewMember).filter(
                DuplicateReviewMember.review_id == review.id
            ).delete(synchronize_session=False)
            db.query(DuplicateReview).filter(
                DuplicateReview.id == review.id
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_approve_review_raises_for_nonexistent_review_against_real_database() -> None:
    engine = _engine()

    with Session(engine) as db:
        service = DedupReviewService(db)

        try:
            service.approve_review(999999999)
            raise AssertionError("Expected ValueError for a nonexistent review")
        except ValueError as exc:
            assert "not found" in str(exc)


def test_list_reviews_filters_by_status_against_real_database() -> None:
    engine = _engine()

    with Session(engine) as db:
        document_service = DocumentService(db)
        doc_a = document_service.create_document(
            DocumentCreate(
                title="x.txt", source="/dedup-review-test/x.txt",
                source_type="txt", content_hash="review-test-hash-3",
            )
        )
        doc_b = document_service.create_document(
            DocumentCreate(
                title="x-copy.txt", source="/dedup-review-test/x-copy.txt",
                source_type="txt", content_hash="review-test-hash-3",
            )
        )

        service = DedupReviewService(db)
        group = ExactDuplicateGroup(
            content_hash="review-test-hash-3", documents=[doc_a, doc_b]
        )
        review = service.create_review_from_exact_group(group)

        try:
            pending = service.list_reviews(status=DuplicateReviewStatus.PENDING)
            assert any(r.id == review.id for r in pending)

            service.approve_review(review.id)

            pending_after = service.list_reviews(status=DuplicateReviewStatus.PENDING)
            assert not any(r.id == review.id for r in pending_after)

            approved = service.list_reviews(status=DuplicateReviewStatus.APPROVED)
            assert any(r.id == review.id for r in approved)

        finally:
            db.query(DuplicateReviewMember).filter(
                DuplicateReviewMember.review_id == review.id
            ).delete(synchronize_session=False)
            db.query(DuplicateReview).filter(
                DuplicateReview.id == review.id
            ).delete(synchronize_session=False)
            db.query(Document).filter(
                Document.id.in_([doc_a.id, doc_b.id])
            ).delete(synchronize_session=False)
            db.commit()
