from pathlib import Path

from app.classification.eligibility_service import IngestionEligibility, classify_eligibility


def test_c9r_is_excluded() -> None:
    assert classify_eligibility(Path("/synthetic/vault/d/AB/somefile.c9r")) == (
        IngestionEligibility.EXCLUDED
    )


def test_known_unsupported_extension_is_unsupported() -> None:
    assert classify_eligibility(Path("/synthetic/photo.jpg")) == IngestionEligibility.UNSUPPORTED
    assert classify_eligibility(Path("/synthetic/video.mp4")) == IngestionEligibility.UNSUPPORTED


def test_ordinary_document_is_eligible() -> None:
    assert classify_eligibility(Path("/synthetic/report.pdf")) == IngestionEligibility.ELIGIBLE
    assert classify_eligibility(Path("/synthetic/notes.txt")) == IngestionEligibility.ELIGIBLE


def test_unknown_extension_defaults_to_eligible_not_unsupported() -> None:
    """Only a small, EXPLICIT denylist marks something UNSUPPORTED -
    an extension nobody has classified yet is ELIGIBLE (an attempt will
    be made; if it genuinely can't be decoded, that surfaces later as a
    real FAILED attempt, never silently pre-judged as UNSUPPORTED)."""
    assert classify_eligibility(Path("/synthetic/mystery.xyz123")) == (
        IngestionEligibility.ELIGIBLE
    )


def test_classification_never_opens_the_file() -> None:
    """Proves this is purely suffix-based: a path that doesn't even
    exist on disk still classifies correctly, since no read is ever
    attempted here."""
    assert classify_eligibility(Path("/this/path/does/not/exist.c9r")) == (
        IngestionEligibility.EXCLUDED
    )
