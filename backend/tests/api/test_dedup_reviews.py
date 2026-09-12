from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.models.dedup_review import (
    DuplicateMatchType,
    DuplicateReview,
    DuplicateReviewMember,
    DuplicateReviewMemberRole,
    DuplicateReviewStatus,
)
from app.models.document import Document


def _document(doc_id: str, title: str, content_hash: str | None = None) -> Document:
    return Document(
        id=doc_id,
        title=title,
        source=f"/documents/{title}",
        source_type="txt",
        content_hash=content_hash,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def _member(
    member_id: int, review_id: int, document_id: str, role: DuplicateReviewMemberRole
) -> DuplicateReviewMember:
    return DuplicateReviewMember(
        id=member_id, review_id=review_id, document_id=document_id, role=role
    )


def _exact_review(review_id: int = 1) -> DuplicateReview:
    return DuplicateReview(
        id=review_id,
        match_type=DuplicateMatchType.EXACT,
        content_hash="hash-a",
        similarity=None,
        confidence=1.0,
        recommendation_reason="Identical content.",
        evidence={"match_type": "exact"},
        status=DuplicateReviewStatus.PENDING,
        reviewer_decision=None,
        human_selected_canonical_document_id=None,
        reviewed_at=None,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


# --- list / filter ---------------------------------------------------------


def test_list_duplicate_reviews_exact() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            review = _exact_review()
            doc_a = _document("doc-1", "a.txt", "hash-a")
            doc_b = _document("doc-2", "a-copy.txt", "hash-a")
            members = [
                _member(1, 1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL),
                _member(2, 1, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
            ]

            service_class.return_value.list_reviews.return_value = [review]
            service_class.return_value.get_review_members_with_documents.return_value = [
                (members[0], doc_a),
                (members[1], doc_b),
            ]

            client = TestClient(app)
            response = client.get("/dedup/reviews")

            assert response.status_code == 200
            body = response.json()
            assert len(body) == 1
            assert body[0]["match_type"] == "exact"
            assert body[0]["status"] == "pending"
            assert body[0]["recommended_canonical_document_id"] == "doc-1"
            assert body[0]["human_selected_canonical_document_id"] is None
            assert len(body[0]["members"]) == 2
            assert body[0]["members"][0]["document"]["title"] == "a.txt"

    finally:
        app.dependency_overrides.clear()


def test_list_duplicate_reviews_near() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            review = DuplicateReview(
                id=2,
                match_type=DuplicateMatchType.NEAR,
                content_hash=None,
                similarity=0.93,
                confidence=0.93,
                recommendation_reason="Similar but not identical.",
                evidence={"match_type": "near"},
                status=DuplicateReviewStatus.PENDING,
                reviewer_decision=None,
                human_selected_canonical_document_id=None,
                reviewed_at=None,
                created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
                updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
            )
            doc_a = _document("doc-1", "v1.pdf")
            doc_b = _document("doc-2", "v2.pdf")
            members = [
                _member(3, 2, "doc-1", DuplicateReviewMemberRole.DUPLICATE),
                _member(4, 2, "doc-2", DuplicateReviewMemberRole.DUPLICATE),
            ]

            service_class.return_value.list_reviews.return_value = [review]
            service_class.return_value.get_review_members_with_documents.return_value = [
                (members[0], doc_a),
                (members[1], doc_b),
            ]

            client = TestClient(app)
            response = client.get("/dedup/reviews", params={"status": "pending"})

            assert response.status_code == 200
            body = response.json()
            assert body[0]["match_type"] == "near"
            assert body[0]["similarity"] == 0.93
            # The ambiguous-candidate property, visible in the API shape.
            assert body[0]["recommended_canonical_document_id"] is None
            assert body[0]["human_selected_canonical_document_id"] is None

            service_class.return_value.list_reviews.assert_called_once_with(
                status=DuplicateReviewStatus.PENDING
            )

    finally:
        app.dependency_overrides.clear()


def test_list_duplicate_reviews_rejects_invalid_status_filter() -> None:
    client = TestClient(app)

    response = client.get("/dedup/reviews", params={"status": "bogus"})

    assert response.status_code == 422


def test_list_duplicate_reviews_returns_empty_list() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.list_reviews.return_value = []

            client = TestClient(app)
            response = client.get("/dedup/reviews")

            assert response.status_code == 200
            assert response.json() == []

    finally:
        app.dependency_overrides.clear()


# --- get single review ------------------------------------------------


def test_get_duplicate_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            review = _exact_review()
            doc_a = _document("doc-1", "a.txt", "hash-a")
            service_class.return_value.get_review.return_value = review
            service_class.return_value.get_review_members_with_documents.return_value = [
                (_member(1, 1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL), doc_a),
            ]

            client = TestClient(app)
            response = client.get("/dedup/reviews/1")

            assert response.status_code == 200
            assert response.json()["id"] == 1

    finally:
        app.dependency_overrides.clear()


def test_get_duplicate_review_returns_404_for_missing_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.get_review.return_value = None

            client = TestClient(app)
            response = client.get("/dedup/reviews/999")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


# --- approve -----------------------------------------------------------


def test_approve_duplicate_review_with_canonical() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            approved_review = _exact_review()
            approved_review.status = DuplicateReviewStatus.APPROVED
            approved_review.human_selected_canonical_document_id = "doc-1"
            approved_review.reviewed_at = datetime(2026, 8, 12, 11, 0, tzinfo=UTC)

            doc_a = _document("doc-1", "a.txt", "hash-a")
            service_class.return_value.approve_review.return_value = approved_review
            service_class.return_value.get_review_members_with_documents.return_value = [
                (_member(1, 1, "doc-1", DuplicateReviewMemberRole.RECOMMENDED_CANONICAL), doc_a),
            ]

            client = TestClient(app)
            response = client.post(
                "/dedup/reviews/1/approve",
                json={"canonical_document_id": "doc-1", "reviewer_decision": "Agreed."},
            )

            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "approved"
            assert body["human_selected_canonical_document_id"] == "doc-1"

            service_class.return_value.approve_review.assert_called_once_with(
                1, canonical_document_id="doc-1", reviewer_decision="Agreed."
            )

    finally:
        app.dependency_overrides.clear()


def test_approve_duplicate_review_without_body() -> None:
    """Approving a NEAR review with no canonical choice - an empty
    request body must be accepted, not required to omit the endpoint."""
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            review = DuplicateReview(
                id=2,
                match_type=DuplicateMatchType.NEAR,
                similarity=0.9,
                confidence=0.9,
                recommendation_reason="test",
                evidence={},
                status=DuplicateReviewStatus.APPROVED,
                human_selected_canonical_document_id=None,
                reviewed_at=datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
                created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
                updated_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
            )
            service_class.return_value.approve_review.return_value = review
            service_class.return_value.get_review_members_with_documents.return_value = []

            client = TestClient(app)
            response = client.post("/dedup/reviews/2/approve")

            assert response.status_code == 200
            service_class.return_value.approve_review.assert_called_once_with(
                2, canonical_document_id=None, reviewer_decision=None
            )

    finally:
        app.dependency_overrides.clear()


def test_approve_duplicate_review_returns_404_for_missing_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.approve_review.side_effect = ValueError(
                "Duplicate review 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/999/approve")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_approve_duplicate_review_returns_409_for_already_reviewed() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.approve_review.side_effect = ValueError(
                "Duplicate review 1 has already been reviewed (status=approved) - "
                "it cannot be reviewed again"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/1/approve")

            assert response.status_code == 409

    finally:
        app.dependency_overrides.clear()


def test_approve_duplicate_review_returns_422_for_missing_canonical() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.approve_review.side_effect = ValueError(
                "Duplicate review 1 is an exact-duplicate finding and requires "
                "an explicit canonical_document_id to approve"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/1/approve")

            assert response.status_code == 422

    finally:
        app.dependency_overrides.clear()


def test_approve_duplicate_review_returns_422_for_invalid_canonical() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.approve_review.side_effect = ValueError(
                "'doc-999' is not a member of duplicate review 1 - cannot "
                "select it as the canonical copy"
            )

            client = TestClient(app)
            response = client.post(
                "/dedup/reviews/1/approve",
                json={"canonical_document_id": "doc-999"},
            )

            assert response.status_code == 422

    finally:
        app.dependency_overrides.clear()


# --- reject --------------------------------------------------------------


def test_reject_duplicate_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            rejected_review = _exact_review()
            rejected_review.status = DuplicateReviewStatus.REJECTED
            rejected_review.reviewer_decision = "Not actually duplicates."
            rejected_review.reviewed_at = datetime(2026, 8, 12, 11, 0, tzinfo=UTC)

            service_class.return_value.reject_review.return_value = rejected_review
            service_class.return_value.get_review_members_with_documents.return_value = []

            client = TestClient(app)
            response = client.post(
                "/dedup/reviews/1/reject",
                json={"reviewer_decision": "Not actually duplicates."},
            )

            assert response.status_code == 200
            assert response.json()["status"] == "rejected"

            service_class.return_value.reject_review.assert_called_once_with(
                1, reviewer_decision="Not actually duplicates."
            )

    finally:
        app.dependency_overrides.clear()


def test_reject_duplicate_review_returns_404_for_missing_review() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.reject_review.side_effect = ValueError(
                "Duplicate review 999 not found"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/999/reject")

            assert response.status_code == 404

    finally:
        app.dependency_overrides.clear()


def test_reject_duplicate_review_returns_409_for_already_reviewed() -> None:
    db = MagicMock()

    app.dependency_overrides[get_db] = lambda: db

    try:
        with patch("app.api.dedup_reviews.DedupReviewService") as service_class:
            service_class.return_value.reject_review.side_effect = ValueError(
                "Duplicate review 1 has already been reviewed (status=rejected) - "
                "it cannot be reviewed again"
            )

            client = TestClient(app)
            response = client.post("/dedup/reviews/1/reject")

            assert response.status_code == 409

    finally:
        app.dependency_overrides.clear()
