from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from app.models.source_instance import RiskTierEstimated, SourceCategory, WorkloadCategory

if TYPE_CHECKING:
    # Deferred import to avoid a circular dependency - deterministic_selector
    # imports FROM this module. Only needed for the type hint below.
    from app.classification.deterministic_selector import ClassifiedCandidate

# ALL archive-shaped suffixes D0 recognizes as "known archive types" -
# .zip/.7z ARE extractable by ArchiveExtractor today; .gz/.rar are NOT,
# but remain physically ARCHIVE regardless (source_category is a
# physical fact, never a processing-capability judgment - see
# `is_extractable_archive_suffix` for the separate capability check).
_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".gz", ".rar"})
_EXTRACTABLE_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z"})

# Matches eligibility_service._EXCLUDED_SUFFIXES exactly - Cryptomator
# ciphertext is a normal, single-stream loose file physically; its
# workload is what's genuinely special (encrypted), not its physical shape.
_ENCRYPTED_WORKLOAD_SUFFIXES = frozenset({".c9r"})

_TEXT_DOCUMENT_SUFFIXES = frozenset({".txt", ".md", ".pdf", ".docx", ".pptx"})
_STRUCTURED_DATA_SUFFIXES = frozenset({".json", ".csv"})
_MEDIA_SUFFIXES = frozenset(
    {".mp4", ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".mov", ".wav", ".m4a"}
)
_SOFTWARE_SUFFIXES = frozenset({".exe", ".dll", ".msi", ".apk", ".bin"})

# Syncthing appends "_<digits>" to a colliding filename (e.g.
# ".pdf_1768918262") - the numeric/policy pass's analytical normalization
# is adopted here as the REAL classification rule too (frozen
# implementation design, point 4): stripping it is required, not optional,
# or 8,000+ real files would misclassify as UNKNOWN.
_CONFLICT_SUFFIX_RE = re.compile(r"^(\.[a-zA-Z0-9]+)_\d+$")

# Archive source-size tiers, per the frozen numeric/policy pass (point 3) -
# declared compressed bytes only, never an expansion-risk claim.
_ARCHIVE_SMALL_MAX_BYTES = 10_000_000  # <10MB
_ARCHIVE_MEDIUM_MAX_BYTES = 1_000_000_000  # 10MB-1GB
_ARCHIVE_LARGE_MAX_BYTES = 5_000_000_000  # 1-5GB, else EXTREME

# Loose-file risk boundaries, per the frozen numeric/policy pass (point 4).
_LOOSE_MEDIUM_MAX_BYTES = 1_000_000  # <1MB is LOW
_LOOSE_HIGH_MAX_BYTES = 50_000_000  # 1-50MB is MEDIUM
_LOOSE_EXTREME_MAX_BYTES = 500_000_000  # 50-500MB is HIGH, else EXTREME


def _normalize_suffix(path: str) -> str:
    """Pure, suffix-only, no file ever opened - extends
    eligibility_service.classify_eligibility's existing pattern.
    Strips a Syncthing conflict-rename suffix if present."""
    suffix = PurePosixPath(path).suffix.lower()
    match = _CONFLICT_SUFFIX_RE.match(suffix)
    return match.group(1) if match else suffix


def classify_source_category(root_t7_path: str, member_path: str | None) -> SourceCategory:
    """Pure, suffix-only - never opens a file, and never conflates a
    physical fact with a processing-capability one: `.gz`/`.rar` are
    physically ARCHIVE (D0's own "known archive type" grouping includes
    them) even though `ArchiveExtractor` cannot open them today - see
    `is_extractable_archive_suffix` for that separate, explicit check.
    `member_path`, when given, is what's actually classified (a
    member's own extension is what matters, once one exists);
    `root_t7_path` otherwise. `SPECIAL` is reserved for a genuinely
    non-standard physical shape not yet concretely encountered - no
    suffix mapping here returns it."""
    suffix = _normalize_suffix(member_path if member_path is not None else root_t7_path)
    if suffix in _ARCHIVE_SUFFIXES:
        return SourceCategory.ARCHIVE
    return SourceCategory.LOOSE_FILE


def is_extractable_archive_suffix(root_t7_path: str, member_path: str | None) -> bool:
    """A PROCESSING-CAPABILITY fact, deliberately separate from
    `source_category` (a physical fact): whether `ArchiveExtractor` can
    actually open this archive-shaped source today. `.zip`/`.7z` yes;
    `.gz`/`.rar` not yet - those remain source_category=ARCHIVE
    (physically correct) but are excluded from admission by any batch
    policy that requires extractability (see
    `BatchClassPolicy.require_extractable_archive`)."""
    suffix = _normalize_suffix(member_path if member_path is not None else root_t7_path)
    return suffix in _EXTRACTABLE_ARCHIVE_SUFFIXES


def classify_workload_category(
    root_t7_path: str,
    member_path: str | None,
    source_category: SourceCategory,
) -> WorkloadCategory:
    """Pure, suffix-only - never opens a file. An ARCHIVE's own row
    always gets CONTAINER (frozen invariant: an archive is never itself
    content), REGARDLESS of whether it is extractable - its workload is
    only knowable per-member, once members exist, a later archive-
    extraction milestone's concern; whether it can be opened AT ALL is
    the separate `is_extractable_archive_suffix` capability check."""
    if source_category == SourceCategory.ARCHIVE:
        return WorkloadCategory.CONTAINER

    suffix = _normalize_suffix(member_path if member_path is not None else root_t7_path)
    if suffix in _ENCRYPTED_WORKLOAD_SUFFIXES:
        return WorkloadCategory.ENCRYPTED
    if suffix in _TEXT_DOCUMENT_SUFFIXES:
        return WorkloadCategory.TEXT_DOCUMENT
    if suffix in _STRUCTURED_DATA_SUFFIXES:
        return WorkloadCategory.STRUCTURED_DATA
    if suffix in _MEDIA_SUFFIXES:
        return WorkloadCategory.MEDIA
    if suffix in _SOFTWARE_SUFFIXES:
        return WorkloadCategory.SOFTWARE
    return WorkloadCategory.UNKNOWN


def classify_risk_tier_estimated(
    source_category: SourceCategory,
    declared_size_bytes: int,
) -> RiskTierEstimated:
    """Selection-time estimate only - see the frozen two-phase model
    (risk_tier_actual is a later, post-extraction concern this
    milestone does not implement). For archives, informed by the
    source-size tier alone (the only pre-extraction signal available
    today) - a distinct judgment from that tier, not a relabeling of
    it, per the frozen architecture's own caution. Applies identically
    to extractable and not-yet-extractable archives alike - risk
    estimation is about size, not about today's processing capability."""
    if source_category == SourceCategory.ARCHIVE:
        if declared_size_bytes < _ARCHIVE_SMALL_MAX_BYTES:
            return RiskTierEstimated.LOW
        if declared_size_bytes < _ARCHIVE_MEDIUM_MAX_BYTES:
            return RiskTierEstimated.MEDIUM
        if declared_size_bytes < _ARCHIVE_LARGE_MAX_BYTES:
            return RiskTierEstimated.HIGH
        return RiskTierEstimated.EXTREME

    if declared_size_bytes >= _LOOSE_EXTREME_MAX_BYTES:
        return RiskTierEstimated.EXTREME
    if declared_size_bytes >= _LOOSE_HIGH_MAX_BYTES:
        return RiskTierEstimated.HIGH
    if declared_size_bytes >= _LOOSE_MEDIUM_MAX_BYTES:
        return RiskTierEstimated.MEDIUM
    return RiskTierEstimated.LOW


def classify_backup_sync_context(root_t7_path: str) -> str | None:
    """STILL DEFERRED, per the frozen architecture (Round 4) and the
    numeric/policy pass: no path-pattern list is invented here. Always
    returns None. A future, separately-authorized gate defines this
    signal's structure and versioned pattern list."""
    return None


@dataclass(frozen=True, slots=True)
class BatchClassPolicy:
    """A batch class's admission predicate - pure, deterministic. Keeps
    analytical normalization (this module's suffix logic) separate from
    the actual per-`SourceInstance` classification decision recorded at
    materialization time (see `batch_creation_service`).

    `require_extractable_archive`, when True, additionally requires any
    ARCHIVE-category candidate to pass `is_extractable_archive_suffix`
    - the explicit, separate way an archive-admitting policy excludes
    not-yet-supported archive types (.gz/.rar) WITHOUT relabeling their
    physical source_category. Ignored for non-ARCHIVE candidates.
    """

    selection_policy_version: str
    allowed_source_categories: frozenset[SourceCategory]
    allowed_workload_categories: frozenset[WorkloadCategory]
    allowed_risk_tiers: frozenset[RiskTierEstimated]
    require_extractable_archive: bool = False

    def matches(self, candidate: "ClassifiedCandidate") -> bool:
        if (
            candidate.source_category == SourceCategory.ARCHIVE
            and self.require_extractable_archive
            and not is_extractable_archive_suffix(
                candidate.observation.root_t7_path, candidate.observation.member_path
            )
        ):
            return False
        return (
            candidate.source_category in self.allowed_source_categories
            and candidate.workload_category in self.allowed_workload_categories
            and candidate.risk_tier_estimated in self.allowed_risk_tiers
        )


def text_document_batch_policy(version: str = "batch-class-1-text-document-v1") -> BatchClassPolicy:
    """Class 1 (Text/Document) per the frozen numeric/policy baseline -
    LOOSE_FILE only, no archives admitted."""
    return BatchClassPolicy(
        selection_policy_version=version,
        allowed_source_categories=frozenset({SourceCategory.LOOSE_FILE}),
        allowed_workload_categories=frozenset(
            {WorkloadCategory.TEXT_DOCUMENT, WorkloadCategory.STRUCTURED_DATA}
        ),
        allowed_risk_tiers=frozenset({RiskTierEstimated.LOW, RiskTierEstimated.MEDIUM}),
    )
