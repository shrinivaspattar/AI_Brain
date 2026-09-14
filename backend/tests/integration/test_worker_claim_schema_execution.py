"""Real-database tests for the worker claim/lease fields and
IngestionAttempt audit table added by the "Controlled T7 -> AI_Brain
Ingestion Design" schema-extension milestone (`6491dad` round 1/2).

No T7 access, no ingestion pipeline: every ContentIdentityGroup/
SourceInstance here is entirely synthetic, and no extraction,
normalization, chunking, or embedding logic exists or is exercised -
only the claim/release/attempt-recording primitives themselves.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
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
from app.models.ingestion_attempt import (
    IngestionAttempt,
    IngestionAttemptKind,
    IngestionAttemptOutcome,
    IngestionAttemptStage,
    IngestionFailureCode,
)
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - see
    test_content_identity_schema_execution.py's fixture docstring for
    why this is needed (services under test commit internally)."""
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


def _classification_run(db: Session, discovery_run: DiscoveryRun) -> ClassificationRun:
    run = ClassificationRun(
        classifier_version="test-classifier-v1",
        d1_discovery_run_id=discovery_run.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _group(
    db: Session,
    pipeline_state: ContentPipelineState = ContentPipelineState.CLASSIFIED,
) -> ContentIdentityGroup:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=pipeline_state,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def _instance(db: Session, classification_run: ClassificationRun) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path="/synthetic/unhashed_loose_file.txt",
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


# -- ContentIdentityGroup claim fields --------------------------------------


def test_content_identity_group_claim_fields_default_to_null(db: Session) -> None:
    group = _group(db)
    assert group.claimed_by is None
    assert group.claimed_at is None


def test_claim_content_identity_group_claims_an_eligible_row(db: Session) -> None:
    group = _group(db, ContentPipelineState.CLASSIFIED)

    claimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
        claiming_pipeline_state=ContentPipelineState.EXTRACTING,
    )

    assert claimed is not None
    assert claimed.id == group.id
    assert claimed.claimed_by == "worker-a"
    assert claimed.claimed_at is not None
    assert claimed.pipeline_state == ContentPipelineState.EXTRACTING


def test_claim_content_identity_group_ignores_ineligible_pipeline_state(
    db: Session,
) -> None:
    _group(db, ContentPipelineState.INGESTED)

    claimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
    )

    assert claimed is None


def test_claim_content_identity_group_does_not_reclaim_a_fresh_claim(
    db: Session,
) -> None:
    group = _group(db, ContentPipelineState.EXTRACTED)
    WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
        lease_duration=timedelta(minutes=10),
    )

    second = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-b",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
        lease_duration=timedelta(minutes=10),
    )

    assert second is None
    db.refresh(group)
    assert group.claimed_by == "worker-a"


def test_claim_content_identity_group_reclaims_a_stale_claim(db: Session) -> None:
    group = _group(db, ContentPipelineState.EXTRACTED)
    db.query(ContentIdentityGroup).filter_by(id=group.id).update(
        {
            "claimed_by": "worker-crashed",
            "claimed_at": datetime.now(UTC) - timedelta(hours=1),
        }
    )
    db.commit()

    reclaimed = WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-recovery",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
        lease_duration=timedelta(minutes=10),
    )

    assert reclaimed is not None
    assert reclaimed.id == group.id
    assert reclaimed.claimed_by == "worker-recovery"


def test_release_content_identity_group_claim_clears_claim_and_advances_state(
    db: Session,
) -> None:
    group = _group(db, ContentPipelineState.CLASSIFIED)
    WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
        lease_duration=timedelta(minutes=10),
        claiming_pipeline_state=ContentPipelineState.EXTRACTING,
    )
    db.refresh(group)

    WorkerClaimService(db).release_content_identity_group_claim(
        group.id, claim_generation=group.claim_generation, new_pipeline_state=ContentPipelineState.EXTRACTED
    )

    db.refresh(group)
    assert group.claimed_by is None
    assert group.claimed_at is None
    assert group.pipeline_state == ContentPipelineState.EXTRACTED


def test_release_content_identity_group_claim_on_failure_leaves_failed_state(
    db: Session,
) -> None:
    group = _group(db, ContentPipelineState.EXTRACTED)
    WorkerClaimService(db).claim_content_identity_group(
        worker_id="worker-a",
        eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
        lease_duration=timedelta(minutes=10),
    )
    db.refresh(group)

    WorkerClaimService(db).release_content_identity_group_claim(
        group.id, claim_generation=group.claim_generation, new_pipeline_state=ContentPipelineState.FAILED
    )

    db.refresh(group)
    assert group.claimed_by is None
    assert group.pipeline_state == ContentPipelineState.FAILED


# -- SourceInstance identity-resolution claim fields -------------------------


def test_claim_source_instance_for_identity_resolution(db: Session) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )

    assert claimed is not None
    assert claimed.id == instance.id
    assert claimed.claimed_by == "worker-a"


def test_claim_source_instance_ignores_already_resolved_instances(
    db: Session,
) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)
    group = _group(db)
    db.query(SourceInstance).filter_by(id=instance.id).update(
        {"content_identity_group_id": group.id}
    )
    db.commit()

    claimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )

    assert claimed is None


def test_claim_source_instance_reclaims_a_stale_claim(db: Session) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)
    db.query(SourceInstance).filter_by(id=instance.id).update(
        {
            "claimed_by": "worker-crashed",
            "claimed_at": datetime.now(UTC) - timedelta(hours=1),
        }
    )
    db.commit()

    reclaimed = WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-recovery", lease_duration=timedelta(minutes=10)
    )

    assert reclaimed is not None
    assert reclaimed.id == instance.id


def test_release_source_instance_claim_clears_claim_only(db: Session) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)
    WorkerClaimService(db).claim_source_instance_for_identity_resolution(
        worker_id="worker-a", lease_duration=timedelta(minutes=10)
    )
    db.refresh(instance)

    WorkerClaimService(db).release_source_instance_claim(instance.id, claim_generation=instance.claim_generation)

    db.refresh(instance)
    assert instance.claimed_by is None
    assert instance.claimed_at is None
    assert instance.content_identity_group_id is None


# -- IngestionAttempt: constraints and service correctness -------------------


def test_ingestion_attempt_check_rejects_pipeline_advance_with_source_instance(
    db: Session,
) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)

    bad = IngestionAttempt(
        attempt_kind=IngestionAttemptKind.PIPELINE_ADVANCE,
        content_identity_group_id=None,
        source_instance_id=instance.id,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        outcome=IngestionAttemptOutcome.SUCCEEDED,
        worker_id="worker-a",
        attempted_at=datetime.now(UTC),
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_attempt_check_rejects_identity_resolution_with_group(
    db: Session,
) -> None:
    group = _group(db)

    bad = IngestionAttempt(
        attempt_kind=IngestionAttemptKind.IDENTITY_RESOLUTION,
        content_identity_group_id=group.id,
        source_instance_id=None,
        attempted_stage=IngestionAttemptStage.IDENTITY_RESOLUTION,
        outcome=IngestionAttemptOutcome.SUCCEEDED,
        worker_id="worker-a",
        attempted_at=datetime.now(UTC),
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_attempt_check_rejects_stage_mismatch(db: Session) -> None:
    group = _group(db)

    bad = IngestionAttempt(
        attempt_kind=IngestionAttemptKind.PIPELINE_ADVANCE,
        content_identity_group_id=group.id,
        source_instance_id=None,
        attempted_stage=IngestionAttemptStage.IDENTITY_RESOLUTION,
        outcome=IngestionAttemptOutcome.SUCCEEDED,
        worker_id="worker-a",
        attempted_at=datetime.now(UTC),
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_attempt_check_rejects_failed_without_detail(db: Session) -> None:
    group = _group(db)

    bad = IngestionAttempt(
        attempt_kind=IngestionAttemptKind.PIPELINE_ADVANCE,
        content_identity_group_id=group.id,
        source_instance_id=None,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        outcome=IngestionAttemptOutcome.FAILED,
        worker_id="worker-a",
        attempted_at=datetime.now(UTC),
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_ingestion_attempt_service_records_a_successful_pipeline_attempt(
    db: Session,
) -> None:
    group = _group(db)

    attempt = IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=group.id,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.SUCCEEDED,
    )

    assert attempt.id is not None
    assert attempt.content_identity_group_id == group.id
    assert attempt.failure_code is None


def test_ingestion_attempt_service_rejects_identity_resolution_stage_for_pipeline(
    db: Session,
) -> None:
    group = _group(db)

    with pytest.raises(ValueError, match="use record_identity_resolution_attempt"):
        IngestionAttemptService(db).record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.IDENTITY_RESOLUTION,
            worker_id="worker-a",
            outcome=IngestionAttemptOutcome.SUCCEEDED,
        )


def test_ingestion_attempt_service_requires_failure_fields_for_failed_outcome(
    db: Session,
) -> None:
    group = _group(db)

    with pytest.raises(ValueError, match="requires failure_code"):
        IngestionAttemptService(db).record_pipeline_attempt(
            content_identity_group_id=group.id,
            attempted_stage=IngestionAttemptStage.EXTRACTING,
            worker_id="worker-a",
            outcome=IngestionAttemptOutcome.FAILED,
        )


def test_ingestion_attempt_service_records_a_failed_pipeline_attempt(
    db: Session,
) -> None:
    group = _group(db)

    attempt = IngestionAttemptService(db).record_pipeline_attempt(
        content_identity_group_id=group.id,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.FAILED,
        failure_code=IngestionFailureCode.MALFORMED_ARCHIVE,
        failure_detail="zip header CRC mismatch",
        retryable=True,
    )

    assert attempt.failure_code == IngestionFailureCode.MALFORMED_ARCHIVE
    assert attempt.retryable is True


def test_ingestion_attempt_service_records_an_identity_resolution_attempt(
    db: Session,
) -> None:
    discovery = _discovery_run(db)
    classification = _classification_run(db, discovery)
    instance = _instance(db, classification)

    attempt = IngestionAttemptService(db).record_identity_resolution_attempt(
        source_instance_id=instance.id,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.FAILED,
        failure_code=IngestionFailureCode.T7_UNAVAILABLE,
        failure_detail="mount point not present",
        retryable=True,
    )

    assert attempt.source_instance_id == instance.id
    assert attempt.attempted_stage == IngestionAttemptStage.IDENTITY_RESOLUTION


def test_multiple_attempts_preserve_full_retry_history(db: Session) -> None:
    """The whole point of a per-attempt table rather than a mutable
    'last failure' column: every attempt for the same group is
    preserved, not overwritten."""
    group = _group(db)
    service = IngestionAttemptService(db)

    service.record_pipeline_attempt(
        content_identity_group_id=group.id,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        worker_id="worker-a",
        outcome=IngestionAttemptOutcome.FAILED,
        failure_code=IngestionFailureCode.EMBEDDING_UNAVAILABLE,
        failure_detail="ollama unreachable (attempt 1)",
        retryable=True,
    )
    service.record_pipeline_attempt(
        content_identity_group_id=group.id,
        attempted_stage=IngestionAttemptStage.EXTRACTING,
        worker_id="worker-b",
        outcome=IngestionAttemptOutcome.SUCCEEDED,
    )

    attempts = (
        db.query(IngestionAttempt)
        .filter(IngestionAttempt.content_identity_group_id == group.id)
        .order_by(IngestionAttempt.attempted_at)
        .all()
    )
    assert len(attempts) == 2
    assert attempts[0].outcome == IngestionAttemptOutcome.FAILED
    assert attempts[1].outcome == IngestionAttemptOutcome.SUCCEEDED
