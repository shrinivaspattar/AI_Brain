"""Real-database tests for Implementation Milestone 2 (Batch Creation +
Deterministic Selection) of the Scaled Real-T7 Ingestion design. See
"Scaled Real-T7 Ingestion - Implementation Design Pass" (`2fab4b3`) for
the frozen design this milestone implements.

No T7 access of any kind: every candidate here is entirely synthetic -
`CandidateObservation` never reads a real D0/D1 report, and no code
path in this module or the service under test ever opens a real file.
Real-concurrency claims are proven against real Postgres
(`aibrain_test`), matching this project's non-negotiable standard.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.batch_creation_service import BatchCreationService
from app.classification.deterministic_selector import CandidateObservation, classify
from app.classification.policy_evaluator import (
    BatchClassPolicy,
    classify_source_category,
    classify_workload_category,
    is_extractable_archive_suffix,
    text_document_batch_policy,
)
from app.classification.selection_fingerprint import compute_selection_fingerprint
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_batch import IngestionBatch
from app.models.source_instance import RiskTierEstimated, SourceCategory, SourceInstance, WorkloadCategory


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - matches this
    project's established fixture pattern. BatchCreationService's own
    internal commit()/rollback() calls only affect the savepoint here,
    never the outer, test-isolating transaction (SQLAlchemy's
    join_transaction_mode="create_savepoint" behavior)."""
    engine = _engine()
    connection = engine.connect()
    outer_transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer_transaction.rollback()
    connection.close()
    engine.dispose()


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _d0_discovery_run(db: Session) -> DiscoveryRun:
    run = DiscoveryRun(
        run_kind=DiscoveryRunKind.D0_INVENTORY,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _candidate(path: str, size: int = 1000, d1_group: str | None = None) -> CandidateObservation:
    return CandidateObservation(
        root_t7_path=path, member_path=None, declared_size_bytes=size, d1_duplicate_group_id=d1_group
    )


_DOC_POLICY = text_document_batch_policy()

# Admits ANY physically-archive-shaped candidate, including .gz/.rar -
# used to prove source_category=ARCHIVE for unsupported archive types
# too (they are not relabeled merely because they can't be extracted).
_ANY_ARCHIVE_POLICY = BatchClassPolicy(
    selection_policy_version="test-any-archive-policy-v1",
    allowed_source_categories=frozenset({SourceCategory.ARCHIVE}),
    allowed_workload_categories=frozenset({WorkloadCategory.CONTAINER}),
    allowed_risk_tiers=frozenset(
        {RiskTierEstimated.LOW, RiskTierEstimated.MEDIUM, RiskTierEstimated.HIGH, RiskTierEstimated.EXTREME}
    ),
    require_extractable_archive=False,
)

# The real-world-shaped equivalent: only admits archives ArchiveExtractor
# can actually open today (.zip/.7z) - excludes .gz/.rar via the
# explicit, separate processing-capability check.
_ARCHIVE_POLICY = BatchClassPolicy(
    selection_policy_version="test-archive-policy-v1",
    allowed_source_categories=frozenset({SourceCategory.ARCHIVE}),
    allowed_workload_categories=frozenset({WorkloadCategory.CONTAINER}),
    allowed_risk_tiers=frozenset(
        {RiskTierEstimated.LOW, RiskTierEstimated.MEDIUM, RiskTierEstimated.HIGH, RiskTierEstimated.EXTREME}
    ),
    require_extractable_archive=True,
)


def _create_batch(
    db: Session,
    discovery_run: DiscoveryRun,
    candidates: list[CandidateObservation],
    *,
    policy: BatchClassPolicy = _DOC_POLICY,
    max_source_instances: int = 1000,
    max_source_bytes: int = 2_000_000_000,
    max_extracted_bytes: int | None = None,
    max_embeddings: int = 5000,
    max_runtime_seconds: int = 7200,
) -> IngestionBatch | None:
    return BatchCreationService(db).create_batch(
        discovery_run=discovery_run,
        candidates=candidates,
        policy=policy,
        max_source_instances=max_source_instances,
        max_source_bytes=max_source_bytes,
        max_extracted_bytes=max_extracted_bytes,
        max_embeddings=max_embeddings,
        max_runtime_seconds=max_runtime_seconds,
        classifier_version="test-batch-creation-v1",
    )


# -- Empty / single candidate --------------------------------------------


def test_empty_selection_creates_no_batch(db: Session) -> None:
    run = _d0_discovery_run(db)
    result = _create_batch(db, run, [])
    assert result is None


def test_no_policy_matching_candidates_creates_no_batch(db: Session) -> None:
    run = _d0_discovery_run(db)
    # A .jpg is MEDIA workload, never matched by the text-document policy.
    result = _create_batch(db, run, [_candidate("/synthetic/photo.jpg", 1000)])
    assert result is None


def test_single_candidate_creates_batch_with_one_instance(db: Session) -> None:
    run = _d0_discovery_run(db)
    batch = _create_batch(db, run, [_candidate("/synthetic/note.txt", 500)])
    assert batch is not None
    assert batch.source_instances_selected == 1
    assert batch.source_bytes_selected == 500

    instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .all()
    )
    assert len(instances) == 1
    assert instances[0].root_t7_path == "/synthetic/note.txt"
    # Dedicated, queryable columns - not evidence_snapshot.
    assert instances[0].source_category == SourceCategory.LOOSE_FILE
    assert instances[0].workload_category == WorkloadCategory.TEXT_DOCUMENT
    assert instances[0].risk_tier_estimated is not None
    # evidence_snapshot holds only supporting evidence, never a second
    # copy of the classification decision itself.
    assert "source_category" not in instances[0].evidence_snapshot
    assert "workload_category" not in instances[0].evidence_snapshot
    assert instances[0].evidence_snapshot["d0_declared_size_bytes"] == 500


# -- Envelope boundaries: stop, never skip --------------------------------


def test_max_source_instances_boundary_stops_at_exact_limit(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [_candidate(f"/synthetic/{i:03d}.txt", 100) for i in range(5)]
    batch = _create_batch(db, run, candidates, max_source_instances=3, max_source_bytes=10_000)
    assert batch.source_instances_selected == 3


def test_max_source_bytes_boundary_stops_at_exact_limit(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [_candidate(f"/synthetic/{i:03d}.txt", 400) for i in range(5)]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    # 400 + 400 = 800 fits; +400 = 1200 exceeds 1000 -> stop after 2.
    assert batch.source_instances_selected == 2
    assert batch.source_bytes_selected == 800


def test_exact_boundary_candidate_is_included(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [_candidate("/synthetic/a.txt", 500), _candidate("/synthetic/b.txt", 500)]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    # Exactly 1000 - must be included, not excluded by an off-by-one.
    assert batch.source_instances_selected == 2
    assert batch.source_bytes_selected == 1000


def test_first_over_limit_candidate_causes_stop_not_skip(db: Session) -> None:
    """The exact scenario the frozen design requires: a lexicographically-
    EARLY oversized candidate must stop selection entirely, even though
    a LATER, smaller candidate would have fit."""
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/a_huge.txt", 900),  # first in order, itself fits (900<=1000)
        _candidate("/synthetic/b_huge.txt", 900),  # 900+900=1800 > 1000 -> STOP here
        _candidate("/synthetic/c_small.txt", 50),  # would fit if skipping were allowed - must NOT be reached
    ]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    assert batch.source_instances_selected == 1
    instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .all()
    )
    paths = {i.root_t7_path for i in instances}
    assert paths == {"/synthetic/a_huge.txt"}
    assert "/synthetic/c_small.txt" not in paths  # never skipped-in


def test_over_limit_first_candidate_is_not_skipped_in_favor_of_a_smaller_later_one(
    db: Session,
) -> None:
    """An oversized FIRST candidate must stop selection immediately -
    the smaller candidate right after it must never be picked up
    instead. Nothing gets selected at all (matching the empty-selection
    disposition: no batch is created), which is itself the proof - a
    "skip" algorithm would instead have selected b_small.txt."""
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/a_toobig.txt", 5000),  # exceeds max_source_bytes alone
        _candidate("/synthetic/b_small.txt", 10),
    ]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    assert batch is None

    # Independently confirm at the pure-function level that b_small.txt
    # was never reached, not merely that no batch happened to be created.
    from app.classification.deterministic_selector import (
        SelectionEnvelope as _Envelope,
    )
    from app.classification.deterministic_selector import classify as _classify
    from app.classification.deterministic_selector import select as _select

    classified = [_classify(c) for c in candidates]
    result = _select(classified, _DOC_POLICY, _Envelope(max_source_instances=100, max_source_bytes=1000))
    assert result.selected == ()
    assert result.selectable_count == 1  # only b_small.txt individually fits


def test_zero_selected_but_policy_filtered_nonempty_still_creates_no_batch(db: Session) -> None:
    """If literally nothing is selected (even though something was
    policy_filtered/selectable), no batch is created at all."""
    run = _d0_discovery_run(db)
    candidates = [_candidate("/synthetic/a_toobig.txt", 5000)]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    assert batch is None


# -- Deterministic ordering / fingerprint ---------------------------------


def test_deterministic_ordering_is_lexicographic_by_path(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/zebra.txt", 100),
        _candidate("/synthetic/alpha.txt", 100),
        _candidate("/synthetic/mango.txt", 100),
    ]
    batch = _create_batch(db, run, candidates, max_source_instances=2, max_source_bytes=10_000)
    instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .order_by(SourceInstance.root_t7_path)
        .all()
    )
    assert [i.root_t7_path for i in instances] == ["/synthetic/alpha.txt", "/synthetic/mango.txt"]


def test_fingerprint_is_deterministic_for_identical_input() -> None:
    """Pure function test - proves recreating the same synthetic input
    produces the same fingerprint, independent of any database state."""
    kwargs = dict(
        d0_report_sha256=_unique_hash(),
        selection_policy_version="v1",
        ordering_version="lexicographic-path-v1",
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
    )
    paths_a = [("/synthetic/b.txt", None), ("/synthetic/a.txt", None)]
    paths_b = list(reversed(paths_a))  # different input ORDER, same set

    fp1 = compute_selection_fingerprint(selected_paths=paths_a, **kwargs)
    fp2 = compute_selection_fingerprint(selected_paths=paths_b, **kwargs)
    fp3 = compute_selection_fingerprint(selected_paths=paths_a, **kwargs)

    assert fp1 == fp2  # order-independent (re-sorted internally)
    assert fp1 == fp3  # deterministic across repeated calls
    assert len(fp1) == 64  # SHA-256 hex digest length


def test_fingerprint_differs_when_envelope_differs() -> None:
    kwargs = dict(
        d0_report_sha256=_unique_hash(),
        selection_policy_version="v1",
        ordering_version="lexicographic-path-v1",
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        selected_paths=[("/synthetic/a.txt", None)],
    )
    fp_a = compute_selection_fingerprint(max_source_instances=1000, **kwargs)
    fp_b = compute_selection_fingerprint(max_source_instances=2000, **kwargs)
    assert fp_a != fp_b


def test_batch_fingerprint_matches_recomputation_from_its_own_stored_inputs(db: Session) -> None:
    run = _d0_discovery_run(db)
    batch = _create_batch(db, run, [_candidate("/synthetic/note.txt", 500)])
    recomputed = compute_selection_fingerprint(
        d0_report_sha256=run.report_sha256,
        selection_policy_version=batch.selection_policy_version,
        ordering_version=batch.ordering_version,
        max_source_instances=batch.max_source_instances,
        max_source_bytes=batch.max_source_bytes,
        max_extracted_bytes=batch.max_extracted_bytes,
        max_embeddings=batch.max_embeddings,
        max_runtime_seconds=batch.max_runtime_seconds,
        selected_paths=[("/synthetic/note.txt", None)],
    )
    assert recomputed == batch.selection_fingerprint


# -- Policy filtering funnel -----------------------------------------------


def test_policy_filtered_source_excluded_from_selection(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/doc.txt", 100),
        _candidate("/synthetic/video.mp4", 100),  # MEDIA - never matches the text-document policy
    ]
    batch = _create_batch(db, run, candidates)
    assert batch.eligible_source_count == 2
    assert batch.policy_filtered_count == 1
    assert batch.source_instances_selected == 1

    instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .all()
    )
    assert {i.root_t7_path for i in instances} == {"/synthetic/doc.txt"}


def test_selectable_but_not_selected_source(db: Session) -> None:
    """A candidate that is individually small enough to fit
    (selectable) but is never reached because an earlier candidate in
    lexicographic order already triggered the STOP - selectable_count
    must exceed source_instances_selected in this scenario."""
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/a_first.txt", 900),
        _candidate("/synthetic/b_second.txt", 900),  # stops selection (900+900>1000)
        _candidate("/synthetic/c_third.txt", 10),  # individually selectable, never reached
    ]
    batch = _create_batch(db, run, candidates, max_source_instances=100, max_source_bytes=1000)
    assert batch.selectable_count == 3  # all three individually fit under 1000 bytes alone
    assert batch.source_instances_selected == 1  # but only the first was actually taken


# -- Archive container admission, without opening the archive ------------


def test_archive_container_admitted_without_opening_it(db: Session) -> None:
    run = _d0_discovery_run(db)
    batch = _create_batch(
        db, run, [_candidate("/synthetic/backup.zip", 5_000_000)], policy=_ARCHIVE_POLICY
    )
    assert batch is not None
    instance = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .one()
    )
    assert instance.source_category == SourceCategory.ARCHIVE
    assert instance.workload_category == WorkloadCategory.CONTAINER
    # No member SourceInstance is ever created at batch-creation time.
    assert instance.member_path is None


# -- Physical source category vs. processing capability (pure functions) --


@pytest.mark.parametrize(
    "path,expected_source_category,expected_workload_category",
    [
        ("/synthetic/vault_chunk.c9r", SourceCategory.LOOSE_FILE, WorkloadCategory.ENCRYPTED),
        ("/synthetic/backup.zip", SourceCategory.ARCHIVE, WorkloadCategory.CONTAINER),
        ("/synthetic/backup.7z", SourceCategory.ARCHIVE, WorkloadCategory.CONTAINER),
        ("/synthetic/backup.gz", SourceCategory.ARCHIVE, WorkloadCategory.CONTAINER),
        ("/synthetic/backup.rar", SourceCategory.ARCHIVE, WorkloadCategory.CONTAINER),
    ],
)
def test_suffix_classification_physical_vs_workload(
    path: str, expected_source_category: SourceCategory, expected_workload_category: WorkloadCategory
) -> None:
    """The precise correction under review: .gz/.rar remain physically
    ARCHIVE (never relabeled SPECIAL merely because they are not yet
    extractable), and .c9r remains a physically ordinary LOOSE_FILE
    whose workload is ENCRYPTED - source_category is never conflated
    with a processing-capability or workload judgment."""
    source_category = classify_source_category(path, None)
    workload_category = classify_workload_category(path, None, source_category)
    assert source_category == expected_source_category
    assert workload_category == expected_workload_category


@pytest.mark.parametrize(
    "path,expected_extractable",
    [
        ("/synthetic/backup.zip", True),
        ("/synthetic/backup.7z", True),
        ("/synthetic/backup.gz", False),
        ("/synthetic/backup.rar", False),
    ],
)
def test_is_extractable_archive_suffix_is_a_separate_capability_check(
    path: str, expected_extractable: bool
) -> None:
    """Confirms is_extractable_archive_suffix is genuinely independent
    of source_category - both .gz and .zip are ARCHIVE, but only .zip
    is extractable today."""
    assert classify_source_category(path, None) == SourceCategory.ARCHIVE
    assert is_extractable_archive_suffix(path, None) is expected_extractable


def test_unsupported_archive_type_is_excluded_by_require_extractable_archive(db: Session) -> None:
    """The actual admission-policy consequence: a policy requiring
    extractability excludes .gz/.rar even though they are genuinely
    ARCHIVE-category candidates; a policy that does not require it
    admits them, still correctly labeled ARCHIVE (never SPECIAL)."""
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/supported.zip", 1000),
        _candidate("/synthetic/unsupported.gz", 1000),
        _candidate("/synthetic/unsupported.rar", 1000),
    ]

    strict_batch = _create_batch(db, run, candidates, policy=_ARCHIVE_POLICY)
    strict_paths = {
        i.root_t7_path
        for i in db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == strict_batch.classification_run_id)
        .all()
    }
    assert strict_paths == {"/synthetic/supported.zip"}

    run2 = _d0_discovery_run(db)
    lenient_batch = _create_batch(db, run2, candidates, policy=_ANY_ARCHIVE_POLICY)
    lenient_instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == lenient_batch.classification_run_id)
        .all()
    )
    lenient_paths = {i.root_t7_path for i in lenient_instances}
    assert lenient_paths == {
        "/synthetic/supported.zip",
        "/synthetic/unsupported.gz",
        "/synthetic/unsupported.rar",
    }
    # Still correctly ARCHIVE, never relabeled SPECIAL, even though
    # unsupported - the physical fact is unaffected by admission policy.
    for instance in lenient_instances:
        assert instance.source_category == SourceCategory.ARCHIVE
        assert instance.workload_category == WorkloadCategory.CONTAINER


# -- D1 exact-duplicate archive representative rule -----------------------


def test_d1_duplicate_archive_representative_rule_admits_only_one(db: Session) -> None:
    run = _d0_discovery_run(db)
    candidates = [
        _candidate("/synthetic/copy_b.zip", 1000, d1_group="group-1"),
        _candidate("/synthetic/copy_a.zip", 1000, d1_group="group-1"),  # lexicographically first
        _candidate("/synthetic/unrelated.zip", 1000, d1_group="group-2"),
    ]
    batch = _create_batch(db, run, candidates, policy=_ARCHIVE_POLICY)
    instances = (
        db.query(SourceInstance)
        .filter(SourceInstance.classification_run_id == batch.classification_run_id)
        .all()
    )
    paths = {i.root_t7_path for i in instances}
    assert paths == {"/synthetic/copy_a.zip", "/synthetic/unrelated.zip"}
    assert "/synthetic/copy_b.zip" not in paths  # deferred, not deleted or quarantined


# -- Discovery-run-scoped eligibility --------------------------------------


def test_same_path_observed_under_two_different_discovery_runs_both_materialize(
    db: Session,
) -> None:
    run_a = _d0_discovery_run(db)
    run_b = _d0_discovery_run(db)

    batch_a = _create_batch(db, run_a, [_candidate("/synthetic/reobserved.txt", 500)])
    batch_b = _create_batch(db, run_b, [_candidate("/synthetic/reobserved.txt", 500)])

    assert batch_a is not None
    assert batch_b is not None
    assert batch_a.classification_run_id != batch_b.classification_run_id

    count = (
        db.query(func.count(SourceInstance.id))
        .filter(SourceInstance.root_t7_path == "/synthetic/reobserved.txt")
        .scalar()
    )
    assert count == 2  # a genuinely distinct physical occurrence per DiscoveryRun


def test_second_discovery_run_does_not_incorrectly_filter_a_globally_seen_path(
    db: Session,
) -> None:
    """The precise anti-pattern the frozen design forbids: eligibility
    must NEVER be computed as 'no SourceInstance exists for this path
    anywhere' - only scoped to the current DiscoveryRun."""
    run_a = _d0_discovery_run(db)
    _create_batch(db, run_a, [_candidate("/synthetic/seen_before.txt", 500)])

    run_b = _d0_discovery_run(db)
    batch_b = _create_batch(db, run_b, [_candidate("/synthetic/seen_before.txt", 500)])

    assert batch_b is not None
    assert batch_b.source_instances_selected == 1  # NOT filtered out


def test_first_batch_membership_unaffected_by_a_second_discovery_runs_batch(db: Session) -> None:
    run_a = _d0_discovery_run(db)
    batch_a = _create_batch(db, run_a, [_candidate("/synthetic/stable.txt", 500)])
    original_fingerprint = batch_a.selection_fingerprint
    original_run_id = batch_a.classification_run_id

    run_b = _d0_discovery_run(db)
    _create_batch(db, run_b, [_candidate("/synthetic/stable.txt", 500)])

    db.refresh(batch_a)
    assert batch_a.selection_fingerprint == original_fingerprint
    assert batch_a.classification_run_id == original_run_id
    instances_a = (
        db.query(SourceInstance).filter(SourceInstance.classification_run_id == original_run_id).all()
    )
    assert len(instances_a) == 1
    assert instances_a[0].root_t7_path == "/synthetic/stable.txt"


# -- ClassificationRun exclusivity, immutable membership -------------------


def test_classification_run_is_never_shared_between_batches(db: Session) -> None:
    run_a = _d0_discovery_run(db)
    run_b = _d0_discovery_run(db)
    batch_a = _create_batch(db, run_a, [_candidate("/synthetic/x.txt", 100)])
    batch_b = _create_batch(db, run_b, [_candidate("/synthetic/y.txt", 100)])
    assert batch_a.classification_run_id != batch_b.classification_run_id

    run_count = (
        db.query(func.count(ClassificationRun.id))
        .filter(ClassificationRun.id.in_([batch_a.classification_run_id, batch_b.classification_run_id]))
        .scalar()
    )
    assert run_count == 2


def test_membership_is_immutable_across_a_later_incompatible_policy_change(db: Session) -> None:
    """Selection is never re-run against an existing batch - a
    completely different policy/version used for a LATER call must not
    retroactively alter the first batch's already-materialized rows."""
    run = _d0_discovery_run(db)
    batch = _create_batch(db, run, [_candidate("/synthetic/frozen.txt", 500)], policy=_DOC_POLICY)
    original_policy_version = batch.selection_policy_version
    original_fingerprint = batch.selection_fingerprint

    different_policy = BatchClassPolicy(
        selection_policy_version="batch-class-1-text-document-v2-incompatible",
        allowed_source_categories=frozenset({SourceCategory.LOOSE_FILE}),
        allowed_workload_categories=frozenset({WorkloadCategory.MEDIA}),  # deliberately different
        allowed_risk_tiers=frozenset({RiskTierEstimated.LOW}),
    )
    # A second DiscoveryRun/attempt with a different policy - must not
    # touch the first batch at all.
    run2 = _d0_discovery_run(db)
    _create_batch(db, run2, [_candidate("/synthetic/other.mp4", 100)], policy=different_policy)

    db.refresh(batch)
    assert batch.selection_policy_version == original_policy_version
    assert batch.selection_fingerprint == original_fingerprint


# -- Atomicity: failed transaction leaves no partial membership ----------


def test_failed_transaction_leaves_no_partial_membership(db: Session) -> None:
    run = _d0_discovery_run(db)
    with pytest.raises(Exception):  # noqa: B017 - a real IntegrityError from the final commit
        _create_batch(
            db,
            run,
            [_candidate("/synthetic/should_not_persist.txt", 500)],
            max_embeddings=0,  # violates ck_ingestion_batches_max_embeddings_positive
        )

    # Zero SourceInstance rows and zero ClassificationRun rows survive -
    # the whole operation rolled back together, not just the final insert.
    instance_count = (
        db.query(func.count(SourceInstance.id))
        .filter(SourceInstance.root_t7_path == "/synthetic/should_not_persist.txt")
        .scalar()
    )
    assert instance_count == 0

    run_count = (
        db.query(func.count(ClassificationRun.id))
        .filter(ClassificationRun.d0_discovery_run_id == run.id)
        .scalar()
    )
    assert run_count == 0

    batch_count = (
        db.query(func.count(IngestionBatch.id))
        .join(ClassificationRun, IngestionBatch.classification_run_id == ClassificationRun.id)
        .filter(ClassificationRun.d0_discovery_run_id == run.id)
        .scalar()
    )
    assert batch_count == 0


# -- Real Postgres concurrency: same DiscoveryRun, concurrent creation ----


def test_concurrent_batch_creation_for_same_discovery_run_does_not_double_materialize() -> None:
    """Real-Postgres proof (never mocked, per this project's standard)
    that the per-DiscoveryRun advisory lock actually serializes
    concurrent batch-creation attempts: two threads, each with its OWN
    connection/session, race to create a batch from the SAME
    DiscoveryRun with the SAME candidate set. Exactly one must succeed
    with the full selection; the other must see the paths as already
    materialized (correctly excluded by discovery-run-scoped
    eligibility) and create nothing.

    Uses real commits (not the savepoint fixture) since true cross-
    connection locking behavior requires genuinely separate
    transactions - cleaned up manually at the end, matching this
    project's established pattern for this class of test.
    """
    engine = _engine()
    setup_db = Session(engine)
    discovery_run = DiscoveryRun(
        run_kind=DiscoveryRunKind.D0_INVENTORY,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    setup_db.add(discovery_run)
    setup_db.commit()
    setup_db.refresh(discovery_run)
    discovery_run_id = discovery_run.id
    report_sha256 = discovery_run.report_sha256
    setup_db.close()

    candidates = [_candidate(f"/synthetic/concurrent_{i:03d}.txt", 100) for i in range(5)]

    barrier = threading.Barrier(2)
    results: list[IngestionBatch | None] = [None, None]
    errors: list[Exception] = []

    def worker(index: int) -> None:
        thread_db = Session(engine)
        try:
            thread_run = thread_db.get(DiscoveryRun, discovery_run_id)
            barrier.wait()
            results[index] = BatchCreationService(thread_db).create_batch(
                discovery_run=thread_run,
                candidates=candidates,
                policy=_DOC_POLICY,
                max_source_instances=100,
                max_source_bytes=10_000,
                max_extracted_bytes=None,
                max_embeddings=5000,
                max_runtime_seconds=7200,
                classifier_version="test-concurrency-v1",
            )
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"unexpected errors: {errors}"
        succeeded = [r for r in results if r is not None]
        assert len(succeeded) == 1, "exactly one concurrent attempt must materialize the batch"

        cleanup_db = Session(engine)
        try:
            total_instances = (
                cleanup_db.query(func.count(SourceInstance.id))
                .filter(SourceInstance.root_t7_path.like("/synthetic/concurrent_%"))
                .scalar()
            )
            assert total_instances == 5, "no candidate may be double-materialized"
            assert report_sha256  # sanity: fixture data was real
        finally:
            cleanup_db.close()
    finally:
        # Manual cleanup - this test intentionally commits real rows.
        cleanup_db = Session(engine)
        cleanup_db.execute(
            text(
                "DELETE FROM ingestion_batches WHERE classification_run_id IN "
                "(SELECT id FROM classification_runs WHERE d0_discovery_run_id = :rid)"
            ),
            {"rid": discovery_run_id},
        )
        cleanup_db.execute(
            text("DELETE FROM provenance_links WHERE source_instance_id IN "
                 "(SELECT id FROM source_instances WHERE root_t7_path LIKE '/synthetic/concurrent_%')"),
        )
        cleanup_db.execute(
            text("DELETE FROM source_instances WHERE root_t7_path LIKE '/synthetic/concurrent_%'")
        )
        cleanup_db.execute(
            text("DELETE FROM classification_runs WHERE d0_discovery_run_id = :rid"),
            {"rid": discovery_run_id},
        )
        cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :rid"), {"rid": discovery_run_id})
        cleanup_db.commit()
        cleanup_db.close()
        engine.dispose()
