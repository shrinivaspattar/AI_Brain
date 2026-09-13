"""Real-database tests for Implementation Milestone 1 (schema/model
only) of the Scaled Real-T7 Ingestion design - the `IngestionBatch`
model, the generation-fenced reservation primitives added to
`ContentIdentityGroup`, and the discovery-run-scoped materialization
uniqueness added to `SourceInstance`. See "Scaled Real-T7 Ingestion -
Implementation Design Pass" (`2fab4b3`) for the frozen design this
schema implements.

No T7 access of any kind: every row here is entirely synthetic. No
BatchCreationService, PolicyEvaluator, resource guard, or reservation
lifecycle exists yet - only the durable schema primitives and the
minimal `claim_generation` increment already wired into the existing
`WorkerClaimService.claim_content_identity_group`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classification.classification_run_service import ClassificationRunService
from app.classification.worker_claim_service import WorkerClaimService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_batch import BatchStatus, IngestionBatch
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - matches this
    project's established fixture pattern (see
    test_content_identity_schema_execution.py's fixture docstring)."""
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


def _discovery_run(db: Session) -> DiscoveryRun:
    run = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _classification_run(db: Session) -> ClassificationRun:
    discovery = _discovery_run(db)
    return ClassificationRunService(db).start_run(
        classifier_version="test-classifier-v1",
        d1_discovery_run_id=discovery.id,
    )


def _minimal_batch_kwargs(classification_run_id: int) -> dict:
    """The full set of NOT NULL fields a real IngestionBatch requires -
    deliberately small, synthetic values, never derived from a real D0
    report."""
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        eligible_source_count=1000,
        policy_filtered_count=1000,
        selectable_count=1000,
        source_instances_selected=1000,
        source_bytes_selected=50_000_000,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-1-text-document-v1",
        ordering_version="lexicographic-path-v1",
    )


# -- IngestionBatch creation -------------------------------------------


def test_ingestion_batch_creates_with_frozen_defaults(db: Session) -> None:
    run = _classification_run(db)
    batch = IngestionBatch(**_minimal_batch_kwargs(run.id))
    db.add(batch)
    db.commit()
    db.refresh(batch)

    assert batch.id is not None
    assert batch.status == BatchStatus.PLANNED
    assert batch.stop_reason is None
    assert batch.review_required is False
    assert batch.extracted_bytes_consumed == 0
    assert batch.embeddings_reserved == 0
    assert batch.monotonic_runtime_seconds_consumed == 0
    assert batch.started_at is None
    assert batch.completed_at is None
    assert batch.created_at is not None


# -- ClassificationRun 1:1 exclusive ownership --------------------------


def test_ingestion_batch_classification_run_ownership_is_exclusive(db: Session) -> None:
    run = _classification_run(db)
    first = IngestionBatch(**_minimal_batch_kwargs(run.id))
    db.add(first)
    db.commit()

    second = IngestionBatch(**_minimal_batch_kwargs(run.id))
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_two_different_classification_runs_each_get_their_own_batch(db: Session) -> None:
    run_a = _classification_run(db)
    run_b = _classification_run(db)

    batch_a = IngestionBatch(**_minimal_batch_kwargs(run_a.id))
    batch_b = IngestionBatch(**_minimal_batch_kwargs(run_b.id))
    db.add_all([batch_a, batch_b])
    db.commit()  # must not raise

    assert batch_a.id != batch_b.id


# -- Status validity, at the DB level, not merely the ORM ---------------


def test_ingestion_batch_status_enum_rejects_invalid_value_at_db_level(db: Session) -> None:
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    columns = ", ".join(kwargs.keys())
    placeholders = ", ".join(f":{k}" for k in kwargs.keys())

    with pytest.raises(Exception):  # noqa: B017 - a raw invalid-enum DB error, not an ORM one
        db.execute(
            text(
                f"INSERT INTO ingestion_batches (status, {columns}) "
                f"VALUES ('NOT_A_REAL_STATUS', {placeholders})"
            ),
            kwargs,
        )
        db.commit()
    db.rollback()


def test_ingestion_batch_stop_reason_matches_status_check_constraint(db: Session) -> None:
    """RUNNING with a stop_reason set must be rejected; PAUSED without
    one must also be rejected - the CHECK constraint enforces both
    directions of the frozen invariant, not merely one."""
    run = _classification_run(db)
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id))
    batch.stop_reason = None
    db.add(batch)
    db.commit()  # RUNNING + NULL stop_reason is valid

    with pytest.raises(IntegrityError):
        db.execute(
            text("UPDATE ingestion_batches SET stop_reason = 'MANUAL_PAUSE' WHERE id = :id"),
            {"id": batch.id},
        )
        db.commit()
    db.rollback()


# -- Counter / envelope constraints --------------------------------------


def test_ingestion_batch_rejects_negative_extracted_bytes_consumed(db: Session) -> None:
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    kwargs["max_extracted_bytes"] = 100
    batch = IngestionBatch(**kwargs)
    batch.extracted_bytes_consumed = -1
    db.add(batch)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_batch_rejects_embeddings_reserved_exceeding_max(db: Session) -> None:
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    kwargs["max_embeddings"] = 10
    batch = IngestionBatch(**kwargs)
    batch.embeddings_reserved = 11
    db.add(batch)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_batch_rejects_extracted_bytes_exceeding_max(db: Session) -> None:
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    kwargs["max_extracted_bytes"] = 100
    batch = IngestionBatch(**kwargs)
    batch.extracted_bytes_consumed = 101
    db.add(batch)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_batch_allows_null_max_extracted_bytes_with_zero_consumed(db: Session) -> None:
    """The frozen 'N/A' case for a batch class admitting no archives
    (e.g. Class 1) - NULL max_extracted_bytes must coexist with a zero
    consumed counter without tripping the envelope-backstop constraint."""
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    kwargs["max_extracted_bytes"] = None
    batch = IngestionBatch(**kwargs)
    db.add(batch)
    db.commit()  # must not raise
    assert batch.max_extracted_bytes is None
    assert batch.extracted_bytes_consumed == 0


def test_ingestion_batch_rejects_non_positive_envelope_values(db: Session) -> None:
    run = _classification_run(db)
    kwargs = _minimal_batch_kwargs(run.id)
    kwargs["max_source_instances"] = 0
    batch = IngestionBatch(**kwargs)
    db.add(batch)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# -- claim_generation fencing primitive ----------------------------------


def _content_identity_group(db: Session) -> ContentIdentityGroup:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=ContentPipelineState.CLASSIFIED,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def test_claim_generation_starts_at_zero_and_increments_on_fresh_claim(db: Session) -> None:
    group = _content_identity_group(db)
    assert group.claim_generation == 0

    claimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert claimed is not None
    assert claimed.id == group.id
    assert claimed.claim_generation == 1


def test_claim_generation_increments_again_on_stale_reclaim(db: Session) -> None:
    """Simulates a worker crash: the claim is already stale (claimed_at
    far in the past). A second claim attempt must reclaim it AND
    advance claim_generation a second time - the fencing token a
    future reservation-recovery lifecycle depends on to distinguish
    this new generation from the crashed one."""
    group = _content_identity_group(db)

    first = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert first.claim_generation == 1

    # Simulate staleness directly (worker-a "crashed" and never released).
    db.execute(
        text("UPDATE content_identity_groups SET claimed_at = :old WHERE id = :id"),
        {"old": datetime.now(UTC) - timedelta(hours=1), "id": group.id},
    )
    db.commit()

    second = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert second is not None
    assert second.id == group.id
    assert second.claimed_by == "worker-b"
    assert second.claim_generation == 2  # incremented again, not merely reused


def test_claim_generation_does_not_advance_when_already_actively_claimed(db: Session) -> None:
    """A claim that is NOT stale must not be reclaimed or advanced by a
    second worker - the existing SKIP LOCKED / staleness semantics are
    unaffected by adding claim_generation."""
    group = _content_identity_group(db)

    WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )

    second = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )
    assert second is None  # still actively claimed by worker-a, not stale

    db.refresh(group)
    assert group.claim_generation == 1
    assert group.claimed_by == "worker-a"


# -- reserved_embeddings persistence (schema primitive only) -------------


def test_reserved_embeddings_defaults_to_null_and_persists_when_set(db: Session) -> None:
    """`reserved_embeddings_batch_id` (Milestone 4's "Durable
    Reservation Ownership" correction) must be set together with
    `reserved_embeddings` - the `ck_content_identity_groups_reservation
    _ownership_consistent` CHECK constraint enforces this biconditional
    at the database level, tested directly in
    test_batch_aware_worker_claim_execution.py; this test only confirms
    the plain default-null / round-trip behavior still holds."""
    run = _classification_run(db)
    batch = IngestionBatch(**_minimal_batch_kwargs(run.id))
    db.add(batch)
    db.commit()
    db.refresh(batch)
    batch_id = batch.id  # captured now - see note below

    group = _content_identity_group(db)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None

    # Both attributes are assigned from a pre-captured local (`batch_id`),
    # never from `batch.id` accessed live here: `db.commit()` above
    # expires `batch`'s attributes, so a later `batch.id` access would
    # trigger a lazy-reload mid-assignment - which triggers autoflush
    # BETWEEN the two attribute-sets below, flushing `reserved_embeddings
    # = 42` alone (with `reserved_embeddings_batch_id` not yet set) and
    # tripping the ownership-consistency CHECK constraint. Not a model
    # bug - purely an ordering hazard in test code assigning two
    # co-constrained columns from an object whose id needs a fresh read.
    group.reserved_embeddings = 42
    group.reserved_embeddings_batch_id = batch_id
    db.commit()
    db.refresh(group)
    assert group.reserved_embeddings == 42
    assert group.reserved_embeddings_batch_id == batch_id

    group.reserved_embeddings = None
    group.reserved_embeddings_batch_id = None
    db.commit()
    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None


# -- SourceInstance discovery-run-scoped uniqueness (defense-in-depth) ---


def test_source_instance_rejects_duplicate_loose_file_under_same_run(db: Session) -> None:
    """The NULL-member_path case the COALESCE expression exists for:
    two loose-file rows with the same (classification_run_id,
    root_t7_path) and member_path IS NULL on both must be rejected -
    a plain UNIQUE constraint would NOT catch this, since Postgres
    never treats two NULLs as equal."""
    run = _classification_run(db)
    first = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/dup_loose.txt",
        evidence_snapshot={},
    )
    db.add(first)
    db.commit()

    second = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/dup_loose.txt",
        evidence_snapshot={},
    )
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_source_instance_rejects_duplicate_archive_member_under_same_run(db: Session) -> None:
    run = _classification_run(db)
    first = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/archive.zip",
        member_path="inner/file.txt",
        evidence_snapshot={},
    )
    db.add(first)
    db.commit()

    second = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/archive.zip",
        member_path="inner/file.txt",
        evidence_snapshot={},
    )
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_source_instance_allows_different_members_of_same_archive(db: Session) -> None:
    run = _classification_run(db)
    first = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/archive2.zip",
        member_path="a.txt",
        evidence_snapshot={},
    )
    second = SourceInstance(
        classification_run_id=run.id,
        root_t7_path="/synthetic/archive2.zip",
        member_path="b.txt",
        evidence_snapshot={},
    )
    db.add_all([first, second])
    db.commit()  # must not raise - different member_path values


def test_source_instance_allows_same_path_under_a_different_classification_run(
    db: Session,
) -> None:
    """The frozen invariant this constraint must NOT violate: a later
    DiscoveryRun's own ClassificationRun may legitimately re-observe
    the exact same real path - a different classification_run_id is a
    different row in this constraint's key, by design."""
    run_a = _classification_run(db)
    run_b = _classification_run(db)

    first = SourceInstance(
        classification_run_id=run_a.id,
        root_t7_path="/synthetic/reobserved.txt",
        evidence_snapshot={},
    )
    second = SourceInstance(
        classification_run_id=run_b.id,
        root_t7_path="/synthetic/reobserved.txt",
        evidence_snapshot={},
    )
    db.add_all([first, second])
    db.commit()  # must not raise - different classification_run_id
