"""Unit tests for ContentIdentityService's error-scoping logic
(review round 6, point 2): `get_or_create_group` must catch ONLY the
specific UNIQUE-constraint violation for
`uq_content_identity_groups_identity` as "lost the identity race," and
must re-raise any other IntegrityError unchanged. Mocked, not against
real Postgres, because this is pure exception-dispatch logic - the
real-database race behavior itself is proven separately, against real
Postgres, in tests/integration/test_content_identity_concurrency_execution.py.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.classification.content_identity_service import ContentIdentityService
from app.models.content_identity_group import ContentIdentityAlgorithm, ContentIdentityKind


def _integrity_error(constraint_name: str | None) -> IntegrityError:
    orig = SimpleNamespace(diag=SimpleNamespace(constraint_name=constraint_name))
    return IntegrityError("insert ...", {}, orig)


def test_reraises_an_unrelated_integrity_error() -> None:
    db = MagicMock()
    db.scalars.return_value.first.return_value = None  # no existing row
    db.commit.side_effect = _integrity_error("some_unrelated_constraint")

    service = ContentIdentityService(db)
    with pytest.raises(IntegrityError):
        service.get_or_create_group(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash="a" * 64,
        )
    db.rollback.assert_called_once()


def test_reraises_when_the_driver_exposes_no_constraint_name_at_all() -> None:
    """A missing/None constraint_name must never be treated as a match -
    silently guessing "probably our constraint" is exactly the bug this
    review point exists to prevent."""
    db = MagicMock()
    db.scalars.return_value.first.return_value = None
    db.commit.side_effect = _integrity_error(None)

    service = ContentIdentityService(db)
    with pytest.raises(IntegrityError):
        service.get_or_create_group(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash="a" * 64,
        )


def test_swallows_only_the_expected_identity_unique_violation() -> None:
    winner = MagicMock()
    db = MagicMock()
    # First _find() (before the insert attempt) -> nothing found; second
    # _find() (after the IntegrityError, refetching the winner) -> winner.
    db.scalars.return_value.first.side_effect = [None, winner]
    db.commit.side_effect = _integrity_error("uq_content_identity_groups_identity")

    service = ContentIdentityService(db)
    result = service.get_or_create_group(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash="a" * 64,
    )

    assert result is winner
    db.rollback.assert_called_once()


def test_raises_if_the_expected_violation_occurs_but_no_winner_is_found() -> None:
    """Genuinely unexpected: the constraint name matched, but a refetch
    found nothing. Something other than a simple race caused this -
    surface it rather than returning None silently."""
    db = MagicMock()
    db.scalars.return_value.first.side_effect = [None, None]
    db.commit.side_effect = _integrity_error("uq_content_identity_groups_identity")

    service = ContentIdentityService(db)
    with pytest.raises(IntegrityError):
        service.get_or_create_group(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash="a" * 64,
        )
