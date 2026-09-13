from __future__ import annotations

from enum import Enum
from pathlib import Path


class IngestionEligibility(str, Enum):
    """A capability/policy decision made from a path/extension alone,
    BEFORE any read or extraction attempt - never the result of an
    attempt that failed (that's FAILED, a different concept entirely,
    recorded via IngestionAttempt)."""

    ELIGIBLE = "eligible"
    # Deliberately out of scope by policy - matches D2's own established
    # finding that Cryptomator .c9r chunks are ciphertext, never
    # ingestable as documents. NOT a failure - nothing is attempted.
    EXCLUDED = "excluded"
    # No extractor exists for this format today - a fact about
    # capability, not about this specific file. NOT a failure either.
    UNSUPPORTED = "unsupported"


# Matches D2's established finding precisely - see
# AI_Brain_Architecture.md's T7 Provenance-Aware Duplicate Analysis
# section and the "Controlled T7 -> AI_Brain Ingestion Design"'s
# eligibility answer (Q1).
_EXCLUDED_SUFFIXES = frozenset({".c9r"})

# A small, explicit set for this implementation gate - not a claim that
# this is the full universe of unsupported formats, just enough to make
# UNSUPPORTED a real, testable, distinct outcome from FAILED. Extending
# this list as real extractors are added is expected and safe (it only
# ever makes MORE things eligible, never fewer).
_KNOWN_UNSUPPORTED_SUFFIXES = frozenset(
    {".bin", ".exe", ".dll", ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".mp4", ".mov"}
)


def classify_eligibility(path: Path) -> IngestionEligibility:
    """Decides eligibility from `path`'s suffix alone - never opens or
    reads the file. A corrupt-but-otherwise-supported file is NOT
    detected here; that surfaces later as a genuine FAILED attempt once
    extraction/normalization actually tries and fails."""
    suffix = path.suffix.lower()

    if suffix in _EXCLUDED_SUFFIXES:
        return IngestionEligibility.EXCLUDED
    if suffix in _KNOWN_UNSUPPORTED_SUFFIXES:
        return IngestionEligibility.UNSUPPORTED
    return IngestionEligibility.ELIGIBLE
