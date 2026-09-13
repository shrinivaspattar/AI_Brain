from __future__ import annotations

from dataclasses import dataclass

from app.classification.policy_evaluator import (
    BatchClassPolicy,
    classify_risk_tier_estimated,
    classify_source_category,
    classify_workload_category,
)
from app.models.source_instance import RiskTierEstimated, SourceCategory, WorkloadCategory

# Identifies the ordering algorithm itself, separate from
# selection_policy_version - a future ordering change is distinguishable
# from a policy-predicate change (frozen implementation design, point 1).
ORDERING_VERSION = "lexicographic-path-v1"


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    """One path observed by a DiscoveryRun, with its declared D0
    metadata - evidence only, never verified against a live filesystem
    during selection (frozen invariant: selection never touches T7).

    `d1_duplicate_group_id` is set only for candidates D1 has already
    grouped as exact-byte duplicates (SHA-256 content hash) - the ONLY
    evidence the archive-representative scheduling rule is allowed to
    use, per the frozen policy (never filename/path similarity or size
    alone).

    Building the actual list of these from a real D0/D1 report is
    explicitly out of scope for this milestone - today's D0 report
    format (`inventory.json`) records aggregate statistics, not a full
    per-file enumeration, so no code here reads it directly. A future
    milestone integrates a real D0/D1 report reader; this milestone's
    tests supply synthetic lists directly.
    """

    root_t7_path: str
    member_path: str | None
    declared_size_bytes: int
    d1_duplicate_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class ClassifiedCandidate:
    observation: CandidateObservation
    source_category: SourceCategory
    workload_category: WorkloadCategory
    risk_tier_estimated: RiskTierEstimated


@dataclass(frozen=True, slots=True)
class SelectionEnvelope:
    """Selection-time limits only - max_extracted_bytes/max_embeddings/
    max_runtime_seconds are runtime-only limits this milestone does not
    enforce (frozen implementation design, point 5)."""

    max_source_instances: int
    max_source_bytes: int


@dataclass(frozen=True, slots=True)
class SelectionResult:
    eligible_count: int
    policy_filtered_count: int
    selectable_count: int
    selected: tuple[ClassifiedCandidate, ...]
    source_bytes_selected: int


def classify(observation: CandidateObservation) -> ClassifiedCandidate:
    source_category = classify_source_category(observation.root_t7_path, observation.member_path)
    workload_category = classify_workload_category(
        observation.root_t7_path, observation.member_path, source_category
    )
    risk_tier_estimated = classify_risk_tier_estimated(source_category, observation.declared_size_bytes)
    return ClassifiedCandidate(
        observation=observation,
        source_category=source_category,
        workload_category=workload_category,
        risk_tier_estimated=risk_tier_estimated,
    )


def _sort_key(observation: CandidateObservation) -> str:
    """Lexicographic by root_t7_path - deterministic, reproducible from
    frozen report content, never live filesystem enumeration order.
    Members are not selection inputs in this milestone (only container/
    loose rows are materialized at batch-creation time), so root_t7_path
    alone is a complete, unambiguous key here."""
    return observation.root_t7_path


def select(
    candidates: list[ClassifiedCandidate],
    policy: BatchClassPolicy,
    envelope: SelectionEnvelope,
) -> SelectionResult:
    """The frozen deterministic selection algorithm, exactly.

    `candidates` must already be ELIGIBLE (not yet materialized for the
    current DiscoveryRun) - eligibility itself is computed by the
    caller (`BatchCreationService`), since it requires a database read
    this pure function must not perform.

    STOP, NEVER SKIP: candidates are walked in lexicographic order;
    the FIRST one that would exceed EITHER envelope limit ends
    selection entirely. A later, smaller candidate is never
    substituted - this is load-bearing for selection_fingerprint
    reproducibility (frozen design, point 5), not an arbitrary
    tie-break.

    `selectable_count` is a DESCRIPTIVE, non-filtering metric only -
    the count of policy_filtered candidates that would individually
    fit within max_source_bytes in isolation. It is computed
    independently of ordering and is NEVER used to filter or reorder
    the walk below: doing so would silently skip an individually-
    oversized candidate instead of stopping AT it, which is exactly
    what STOP-NEVER-SKIP forbids. A "selectable but not selected"
    candidate is therefore possible and expected: one that could have
    fit on its own, but was never reached because an earlier candidate
    in lexicographic order already triggered the stop.
    """
    eligible_count = len(candidates)

    policy_filtered = [c for c in candidates if policy.matches(c)]
    policy_filtered_count = len(policy_filtered)

    selectable_count = sum(
        1 for c in policy_filtered if c.observation.declared_size_bytes <= envelope.max_source_bytes
    )

    ordered = sorted(policy_filtered, key=lambda c: _sort_key(c.observation))

    selected: list[ClassifiedCandidate] = []
    running_bytes = 0
    for candidate in ordered:
        if len(selected) + 1 > envelope.max_source_instances:
            break
        if running_bytes + candidate.observation.declared_size_bytes > envelope.max_source_bytes:
            break
        selected.append(candidate)
        running_bytes += candidate.observation.declared_size_bytes

    return SelectionResult(
        eligible_count=eligible_count,
        policy_filtered_count=policy_filtered_count,
        selectable_count=selectable_count,
        selected=tuple(selected),
        source_bytes_selected=running_bytes,
    )


def select_archive_duplicate_deferrals(candidates: list[ClassifiedCandidate]) -> frozenset[str]:
    """The frozen D1 exact-duplicate archive representative rule:
    admits at most one representative (the lexicographically-first
    root_t7_path) per D1 duplicate group, among ARCHIVE-category
    candidates only. Returns the set of root_t7_path values to DEFER
    (exclude from this selection) - never a deletion or mutation of
    anything; deferred candidates remain real, untouched, eligible
    observations for a future selection.

    The ONLY evidence used is `d1_duplicate_group_id` (D1's own SHA-256
    content-hash grouping) - never filename/path similarity, directory-
    naming convention, or compressed size alone, per the frozen policy.

    INPUT SOURCE, stated explicitly: this function consumes ALREADY-
    COMMITTED D1 evidence only, carried in `CandidateObservation.
    d1_duplicate_group_id` (itself supplied by the caller, ultimately
    derived from an existing D1 report - see `CandidateObservation`'s
    own docstring). It never rescans, rehashes, or otherwise
    rediscovers duplicate groups from the filesystem during batch
    creation - doing so would violate the established separation:
    D0 is the selection universe, D1/D2 are enrichment/scheduling
    evidence, and the filesystem is never consulted merely to
    reconstruct what D1 already determined. This function is pure and
    performs no I/O of any kind, by construction - the strongest
    possible guarantee that it cannot touch T7.
    """
    archive_candidates = [c for c in candidates if c.source_category == SourceCategory.ARCHIVE]
    chosen_representative: dict[str, str] = {}
    deferred: set[str] = set()

    for candidate in sorted(archive_candidates, key=lambda c: _sort_key(c.observation)):
        group_id = candidate.observation.d1_duplicate_group_id
        if group_id is None:
            continue
        if group_id not in chosen_representative:
            chosen_representative[group_id] = candidate.observation.root_t7_path
        else:
            deferred.add(candidate.observation.root_t7_path)

    return frozenset(deferred)
