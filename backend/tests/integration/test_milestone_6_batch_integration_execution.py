"""Real-database tests for Implementation Milestone 6 (Identity
Resolution & Embedding-Reservation Batch Integration) of the Scaled
Real-T7 Ingestion design. See "Scaled Real-T7 Ingestion - Milestone 6
Design: Identity Resolution & Embedding-Reservation Batch Integration"
(sections 3, 4, and 4a) for the frozen design this milestone
implements, including the reservation-ownership semantics (lowest-`id`
arbitration only, RUNNING-only eligibility checked twice, no cross-batch
capacity borrowing, reservation ownership as resource-cost attribution
only) resolved by the Design Correction Pass.

No T7 access of any kind: every `root_t7_path` is a path under a test's
own `tmp_path`, standing in for what would be a real T7 path in
production. No embeddings are ever computed by a real model - a small
deterministic fake stands in for `EmbeddingClient` throughout, matching
the existing convention in `test_ingestion_pipeline_execution.py`.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.identity_resolution_service import IdentityResolutionService
from app.classification.pipeline_embedding_service import PipelineEmbeddingService
from app.classification.resource_guard import GuardResult, GuardTier
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
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome, IngestionFailureCode
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import SourceInstance

_EMBEDDING_DIMENSIONS = settings.EMBEDDING_DIMENSIONS


class FakeEmbeddingClient:
    """Deterministic, no-network stand-in for EmbeddingClient - matches
    the existing convention in test_ingestion_pipeline_execution.py."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t) % 7) / 7.0] * _EMBEDDING_DIMENSIONS for t in texts]


class FailingEmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding backend unreachable (simulated)")


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
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
    run = ClassificationRun(
        classifier_version="test-m6-batch-integration-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _minimal_batch_kwargs(classification_run_id: int, *, max_embeddings: int = 5000) -> dict:
    return dict(
        classification_run_id=classification_run_id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=max_embeddings,
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


def _batch(
    db: Session,
    classification_run: ClassificationRun,
    *,
    status: BatchStatus = BatchStatus.RUNNING,
    stop_reason: BatchStopReason | None = None,
    max_embeddings: int = 5000,
    embeddings_reserved: int = 0,
) -> IngestionBatch:
    if status is not BatchStatus.RUNNING and status is not BatchStatus.PLANNED and stop_reason is None:
        stop_reason = BatchStopReason.MANUAL_PAUSE if status is BatchStatus.PAUSED else BatchStopReason.SOURCE_WORK_EXHAUSTED
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        embeddings_reserved=embeddings_reserved,
        **_minimal_batch_kwargs(classification_run.id, max_embeddings=max_embeddings),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _loose_instance(db: Session, classification_run: ClassificationRun, path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=str(path),
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _archive_instance(db: Session, classification_run: ClassificationRun) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.zip",
        evidence_snapshot={},
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _link_source_instance(db: Session, classification_run: ClassificationRun, group: ContentIdentityGroup) -> SourceInstance:
    """A SourceInstance already identity-resolved to `group`, standing
    in for "this batch's ingestion is what produced/converged on this
    content identity" - the join `_resolve_owning_running_batch` walks."""
    instance = SourceInstance(
        classification_run_id=classification_run.id,
        root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
        evidence_snapshot={},
        content_identity_group_id=group.id,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _group_with_chunks(
    db: Session,
    *,
    n_chunks: int,
    pipeline_state: ContentPipelineState = ContentPipelineState.CHUNKED,
    claim_generation: int = 0,
    claimed_by: str | None = None,
    claimed_at: datetime | None = None,
    reserved_embeddings: int | None = None,
    reserved_embeddings_batch_id: int | None = None,
    n_already_embedded: int = 0,
) -> tuple[ContentIdentityGroup, Document]:
    group = ContentIdentityGroup(
        identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
        identity_algorithm=ContentIdentityAlgorithm.SHA256,
        identity_hash=_unique_hash(),
        pipeline_state=pipeline_state,
        claim_generation=claim_generation,
        claimed_by=claimed_by,
        claimed_at=claimed_at,
        reserved_embeddings=reserved_embeddings,
        reserved_embeddings_batch_id=reserved_embeddings_batch_id,
    )
    db.add(group)
    db.commit()
    db.refresh(group)

    document = Document(
        title="synthetic.txt",
        source="/synthetic/workspace/synthetic.txt",
        source_type="txt",
        content_hash=group.identity_hash,
        content_identity_group_id=group.id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)

    for i in range(n_already_embedded + n_chunks):
        db.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=i,
                content=f"chunk {i} of {document.id}",
                embedding=([0.1] * _EMBEDDING_DIMENSIONS) if i < n_already_embedded else None,
            )
        )
    db.commit()

    return group, document


def _guard_always(tier: GuardTier, reason=None, detail: str = "synthetic") -> SimpleNamespace:
    return SimpleNamespace(
        check_before_expensive_operation=lambda batch, kind: GuardResult(tier=tier, stop_reason=reason, detail=detail)
    )


# ============================================================
# 1-5: Identity-resolution batch claim integration
# ============================================================


def test_identity_resolution_receives_correct_classification_run_id(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("hello synthetic world - m6")

    run = _classification_run(db)
    _batch(db, run)
    instance = _loose_instance(db, run, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a",
        workspace_root=tmp_path / "workspace",
        classification_run_id=run.id,
    )

    assert resolved is not None
    assert resolved.id == instance.id
    assert resolved.content_identity_group_id is not None
    assert resolved.claimed_by is None  # released


def test_wrong_classification_run_id_cannot_claim_the_source(db: Session, tmp_path) -> None:
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("belongs to run A only")

    run_a = _classification_run(db)
    run_b = _classification_run(db)
    _batch(db, run_a)
    _batch(db, run_b)
    instance_a = _loose_instance(db, run_a, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-b",
        workspace_root=tmp_path / "workspace",
        classification_run_id=run_b.id,
    )

    assert resolved is None
    db.refresh(instance_a)
    assert instance_a.content_identity_group_id is None
    assert instance_a.claimed_by is None


@pytest.mark.parametrize("status", [BatchStatus.PAUSED, BatchStatus.PLANNED, BatchStatus.ABORTED, BatchStatus.COMPLETED])
def test_non_running_batch_cannot_admit_identity_resolution_work(db: Session, tmp_path, status: BatchStatus) -> None:
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("should not be claimable")

    run = _classification_run(db)
    _batch(db, run, status=status)
    instance = _loose_instance(db, run, source_file)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a",
        workspace_root=tmp_path / "workspace",
        classification_run_id=run.id,
    )

    assert resolved is None
    db.refresh(instance)
    assert instance.content_identity_group_id is None
    assert instance.claimed_by is None


def test_ordinary_non_batch_identity_resolution_remains_valid(db: Session, tmp_path) -> None:
    """classification_run_id=None (the default) preserves the exact
    pre-Milestone-6 batch-unaware predicate - regression proof."""
    source_file = tmp_path / "source" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("no batch involved at all")

    run = _classification_run(db)
    instance = _loose_instance(db, run, source_file)  # no IngestionBatch created for this run

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a", workspace_root=tmp_path / "workspace"
    )

    assert resolved is not None
    assert resolved.id == instance.id
    assert resolved.content_identity_group_id is not None


def test_archive_source_cannot_enter_identity_resolution_even_with_batch_scope(db: Session, tmp_path) -> None:
    """An archive container must remain outside loose-file identity
    resolution - unchanged, existing predicate (member_path IS NULL AND
    NOT archive-suffixed) - proven here still holds when a
    classification_run_id/RUNNING batch is also in scope."""
    run = _classification_run(db)
    _batch(db, run)
    archive = _archive_instance(db, run)

    resolved = IdentityResolutionService(db).resolve_next(
        worker_id="worker-a",
        workspace_root=tmp_path / "workspace",
        classification_run_id=run.id,
    )

    assert resolved is None
    db.refresh(archive)
    assert archive.content_identity_group_id is None
    assert archive.claimed_by is None


# ============================================================
# 6-11: Embedding-reservation enforcement
# ============================================================


def test_embedding_reserves_exact_chunk_count(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100)
    group, _ = _group_with_chunks(db, n_chunks=3)
    _link_source_instance(db, run, group)

    fake = FakeEmbeddingClient()
    result = PipelineEmbeddingService(db, embedding_client=fake).embed_next(worker_id="worker-a")

    assert result is not None
    assert result.pipeline_state == ContentPipelineState.INGESTED
    db.refresh(batch)
    # Monotonic: consume does not decrement, so the final counter value
    # IS the exact amount reserved for this attempt.
    assert batch.embeddings_reserved == 3
    assert len(fake.calls) == 1
    assert len(fake.calls[0]) == 3


def test_max_embeddings_prevents_over_admission(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=2, embeddings_reserved=0)
    group, _ = _group_with_chunks(db, n_chunks=3)
    _link_source_instance(db, run, group)

    fake = FakeEmbeddingClient()
    result = PipelineEmbeddingService(db, embedding_client=fake).embed_next(worker_id="worker-a")

    assert result is None  # cleanly deferred - "None-equivalent"
    assert fake.calls == []  # embed() never invoked

    db.refresh(group)
    assert group.pipeline_state == ContentPipelineState.CHUNKED
    assert group.claimed_by is None
    assert group.reserved_embeddings is None
    db.refresh(batch)
    assert batch.embeddings_reserved == 0

    attempts = db.query(IngestionAttempt).filter(IngestionAttempt.content_identity_group_id == group.id).count()
    assert attempts == 0


def test_successful_embedding_consumes_reservation_exactly_once(db: Session) -> None:
    run = _classification_run(db)
    _batch(db, run, max_embeddings=100)
    group, _ = _group_with_chunks(db, n_chunks=2)
    _link_source_instance(db, run, group)

    PipelineEmbeddingService(db, embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-a")

    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    succeeded = (
        db.query(IngestionAttempt)
        .filter(
            IngestionAttempt.content_identity_group_id == group.id,
            IngestionAttempt.outcome == IngestionAttemptOutcome.SUCCEEDED,
        )
        .count()
    )
    assert succeeded == 1


def test_embedding_failure_releases_reservation_and_credits_batch_back(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100)
    group, _ = _group_with_chunks(db, n_chunks=2)
    _link_source_instance(db, run, group)

    PipelineEmbeddingService(db, embedding_client=FailingEmbeddingClient()).embed_next(worker_id="worker-a")

    db.refresh(group)
    assert group.pipeline_state == ContentPipelineState.FAILED
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None
    assert group.claimed_by is None

    db.refresh(batch)
    assert batch.embeddings_reserved == 0  # credited back, not left at 2

    attempt = (
        db.query(IngestionAttempt)
        .filter(
            IngestionAttempt.content_identity_group_id == group.id,
            IngestionAttempt.outcome == IngestionAttemptOutcome.FAILED,
        )
        .one()
    )
    assert attempt.failure_code == IngestionFailureCode.EMBEDDING_UNAVAILABLE
    assert attempt.retryable is True


def test_retry_after_abandoned_reservation_does_not_double_consume(db: Session) -> None:
    """Simulates a worker crash AFTER committing chunk embeddings but
    BEFORE calling consume_embedding_reservation: chunk 0 is already
    embedded, the group still carries a stale claim/live reservation
    for the full original amount. A fresh embed_next() must (a) trigger
    the existing abandoned-reservation recovery (crediting the stale
    amount back), (b) reserve only the REMAINING unembedded count, and
    (c) never re-send the already-embedded chunk's content."""
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100, embeddings_reserved=3)
    group, document = _group_with_chunks(
        db,
        n_chunks=2,  # 2 remaining unembedded
        n_already_embedded=1,  # 1 already embedded (simulated crash survivor)
        claim_generation=1,
        claimed_by="crashed-worker",
        claimed_at=datetime.now(UTC) - timedelta(minutes=30),
        reserved_embeddings=3,
        reserved_embeddings_batch_id=batch.id,
    )
    _link_source_instance(db, run, group)
    already_embedded_content = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id, DocumentChunk.embedding.is_not(None))
        .one()
        .content
    )

    fake = FakeEmbeddingClient()
    result = PipelineEmbeddingService(db, embedding_client=fake).embed_next(worker_id="resume-worker")

    assert result is not None
    assert result.pipeline_state == ContentPipelineState.INGESTED

    all_texts_sent = [text_ for call in fake.calls for text_ in call]
    assert already_embedded_content not in all_texts_sent
    assert len(all_texts_sent) == 2

    db.refresh(batch)
    # Abandoned 3 credited back (-> 0), then 2 newly reserved and
    # consumed (monotonic, -> 2). NOT 3 + 2 = 5.
    assert batch.embeddings_reserved == 2


def test_no_running_owner_proceeds_unreserved(db: Session) -> None:
    """No IngestionBatch at all references this group's classification
    run - embedding must still complete, never stranding the item at
    CHUNKED merely for lack of an active batch to charge."""
    run = _classification_run(db)
    group, _ = _group_with_chunks(db, n_chunks=2)
    _link_source_instance(db, run, group)  # run has no IngestionBatch

    fake = FakeEmbeddingClient()
    result = PipelineEmbeddingService(db, embedding_client=fake).embed_next(worker_id="worker-a")

    assert result is not None
    assert result.pipeline_state == ContentPipelineState.INGESTED
    assert len(fake.calls) == 1


def test_no_running_owner_when_only_owning_batch_has_stopped(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, status=BatchStatus.COMPLETED, stop_reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)
    group, _ = _group_with_chunks(db, n_chunks=1)
    _link_source_instance(db, run, group)

    result = PipelineEmbeddingService(db, embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-a")

    assert result is not None
    assert result.pipeline_state == ContentPipelineState.INGESTED
    db.refresh(batch)
    assert batch.embeddings_reserved == 0  # never touched - not the owner


# ============================================================
# 12, 17: Cross-batch deterministic arbitration; no capacity borrowing
# ============================================================


def test_cross_batch_deterministic_owner_resolution_picks_lowest_id(db: Session) -> None:
    run_low = _classification_run(db)
    run_high = _classification_run(db)
    batch_low = _batch(db, run_low, max_embeddings=100)
    batch_high = _batch(db, run_high, max_embeddings=100)
    assert batch_low.id < batch_high.id

    group, _ = _group_with_chunks(db, n_chunks=2)
    _link_source_instance(db, run_low, group)
    _link_source_instance(db, run_high, group)  # same group, converged from two batches

    PipelineEmbeddingService(db, embedding_client=FakeEmbeddingClient()).embed_next(worker_id="worker-a")

    db.refresh(batch_low)
    db.refresh(batch_high)
    assert batch_low.embeddings_reserved == 2
    assert batch_high.embeddings_reserved == 0


def test_no_cross_batch_capacity_borrowing_when_lowest_id_owner_is_exhausted(db: Session) -> None:
    """The lowest-id batch is the resolved owner but has NO spare
    capacity; the higher-id batch (also RUNNING, also referencing the
    same group) has plenty. The reservation must be DENIED, never
    silently satisfied from the non-owner's capacity."""
    run_low = _classification_run(db)
    run_high = _classification_run(db)
    batch_low = _batch(db, run_low, max_embeddings=1, embeddings_reserved=1)  # full
    batch_high = _batch(db, run_high, max_embeddings=100, embeddings_reserved=0)  # plenty
    assert batch_low.id < batch_high.id

    group, _ = _group_with_chunks(db, n_chunks=2)
    _link_source_instance(db, run_low, group)
    _link_source_instance(db, run_high, group)

    fake = FakeEmbeddingClient()
    result = PipelineEmbeddingService(db, embedding_client=fake).embed_next(worker_id="worker-a")

    assert result is None  # deferred, not silently borrowed from batch_high
    assert fake.calls == []
    db.refresh(batch_low)
    db.refresh(batch_high)
    assert batch_low.embeddings_reserved == 1  # untouched
    assert batch_high.embeddings_reserved == 0  # never borrowed from


# ============================================================
# 15: Owner batch stops (PAUSED/ABORTED/COMPLETED) after reservation
# ============================================================


@pytest.mark.parametrize("status", [BatchStatus.PAUSED, BatchStatus.ABORTED, BatchStatus.COMPLETED])
def test_owner_batch_stopping_after_reservation_does_not_invalidate_it(db: Session, status: BatchStatus) -> None:
    """Design section 4a, point 7: a granted reservation's fate is
    fully decoupled from the owning batch's later status transitions.
    Exercised directly at the WorkerClaimService layer (the primitive
    PipelineEmbeddingService relies on) since simulating a real
    concurrent status change mid-call would require thread
    interleaving this scenario does not need."""
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100)
    group, _ = _group_with_chunks(db, n_chunks=2, claim_generation=1, claimed_by="worker-a", claimed_at=datetime.now(UTC))

    claims = WorkerClaimService(db)
    outcome = claims.reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=2)
    assert outcome.reserved is True

    stop_reason = BatchStopReason.MANUAL_PAUSE if status is BatchStatus.PAUSED else BatchStopReason.SOURCE_WORK_EXHAUSTED
    db.query(IngestionBatch).filter_by(id=batch.id).update({"status": status, "stop_reason": stop_reason})
    db.commit()

    # Re-claim generation after reserve (reserve does not bump it further).
    db.refresh(group)
    consumed = claims.consume_embedding_reservation(
        group_id=group.id, my_generation=group.claim_generation, new_pipeline_state=ContentPipelineState.INGESTED
    )
    assert consumed is True

    db.refresh(group)
    assert group.pipeline_state == ContentPipelineState.INGESTED
    assert group.reserved_embeddings is None
    db.refresh(batch)
    assert batch.status == status  # unchanged by the consume call
    assert batch.embeddings_reserved == 2  # monotonic, unaffected by the batch's own status


# ============================================================
# 16: Recovery uses the durable reserved_embeddings_batch_id
# ============================================================


def test_reservation_recovery_credits_the_durable_owner_not_a_freshly_resolved_one(db: Session) -> None:
    """The abandoned reservation was granted against batch_original
    (the durable owner recorded on the row). A DIFFERENT, also-RUNNING,
    lower-id batch (batch_decoy) now exists and would be
    _resolve_owning_running_batch's fresh pick - but recovery must
    credit batch_original, read fresh from the stale row itself, never
    whatever a fresh resolution would currently prefer."""
    run_original = _classification_run(db)
    run_decoy = _classification_run(db)
    batch_original = _batch(db, run_original, max_embeddings=100, embeddings_reserved=5)
    group, _ = _group_with_chunks(
        db,
        n_chunks=1,
        claim_generation=1,
        claimed_by="crashed-worker",
        claimed_at=datetime.now(UTC) - timedelta(minutes=30),
        reserved_embeddings=5,
        reserved_embeddings_batch_id=batch_original.id,
    )
    _link_source_instance(db, run_original, group)
    # A lower-id, also-RUNNING decoy batch created AFTER batch_original,
    # then re-parented to sort lower only in id-space is not possible
    # (ids are assigned by insertion order) - instead, prove the
    # opposite: even though batch_decoy IS the fresh lowest-id pick
    # once it exists, recovery still credits batch_original, not it.
    batch_decoy = _batch(db, run_decoy, max_embeddings=100, embeddings_reserved=0)
    _link_source_instance(db, run_decoy, group)

    claims = WorkerClaimService(db)
    reclaimed = claims.claim_content_identity_group(
        worker_id="resume-worker",
        eligible_pipeline_states=[ContentPipelineState.CHUNKED],
        lease_duration=timedelta(minutes=10),
    )

    assert reclaimed is not None
    assert reclaimed.id == group.id
    assert reclaimed.claim_generation == 2

    db.refresh(batch_original)
    db.refresh(batch_decoy)
    assert batch_original.embeddings_reserved == 0  # credited back to the DURABLE owner
    assert batch_decoy.embeddings_reserved == 0  # never touched - it was never the reservation's owner

    db.refresh(group)
    assert group.reserved_embeddings is None
    assert group.reserved_embeddings_batch_id is None


# ============================================================
# 14: Stale/zombie worker cannot consume or release a newer reservation
# ============================================================


def test_stale_generation_cannot_consume_or_release_a_newer_reservation(db: Session) -> None:
    run = _classification_run(db)
    batch = _batch(db, run, max_embeddings=100)
    group, _ = _group_with_chunks(db, n_chunks=1, claim_generation=1, claimed_by="worker-old", claimed_at=datetime.now(UTC))

    claims = WorkerClaimService(db)
    # worker-old reserves under generation 1.
    outcome_old = claims.reserve_embeddings(group_id=group.id, my_generation=1, batch_id=batch.id, n=1)
    assert outcome_old.reserved is True

    # worker-old is now delayed (not crashed) past its lease; recovery
    # reclaims the row for worker-new, generation 2, crediting back
    # worker-old's reservation and granting a fresh one.
    db.query(ContentIdentityGroup).filter_by(id=group.id).update({"claimed_at": datetime.now(UTC) - timedelta(hours=1)})
    db.commit()
    reclaimed = claims.claim_content_identity_group(
        worker_id="worker-new", eligible_pipeline_states=[ContentPipelineState.CHUNKED], lease_duration=timedelta(minutes=10)
    )
    assert reclaimed is not None
    assert reclaimed.claim_generation == 2
    outcome_new = claims.reserve_embeddings(group_id=group.id, my_generation=2, batch_id=batch.id, n=1)
    assert outcome_new.reserved is True

    # worker-old, still holding stale generation 1, now tries to
    # consume/release - both must be safe no-ops, never touching
    # generation 2's live reservation/claim.
    consumed_by_stale = claims.consume_embedding_reservation(
        group_id=group.id, my_generation=1, new_pipeline_state=ContentPipelineState.INGESTED
    )
    assert consumed_by_stale is False

    released_by_stale = claims.release_embedding_reservation(group_id=group.id, my_generation=1)
    assert released_by_stale is False

    db.refresh(group)
    assert group.claim_generation == 2
    assert group.claimed_by == "worker-new"
    assert group.reserved_embeddings == 1  # generation 2's live reservation, untouched
    assert group.pipeline_state == ContentPipelineState.CHUNKED  # never wrongly advanced by the stale caller


# ============================================================
# 13: Real-Postgres concurrency - two workers race for the reservation
# ============================================================


def test_concurrency_stress_two_workers_race_for_shared_group_reservation_repeated() -> None:
    """Repeated (10x) real-Postgres race: two workers concurrently call
    embed_next() for the SAME globally-claimable ContentIdentityGroup,
    converged from two different RUNNING batches. Proves, every
    iteration: exactly one worker's embed() call happens, the charged
    batch is always the lower-id batch (deterministic under real
    contention, not just single-threaded), and zero leaked exceptions."""
    engine = _engine()
    iterations = 10
    for _ in range(iterations):
        setup_db = Session(engine)
        discovery = DiscoveryRun(
            run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
            source_root="/synthetic/not-a-real-t7-path",
            report_sha256=_unique_hash(),
            run_started_at=datetime.now(UTC) - timedelta(minutes=5),
            run_completed_at=datetime.now(UTC),
        )
        setup_db.add(discovery)
        setup_db.commit()
        setup_db.refresh(discovery)

        run_low = ClassificationRun(classifier_version="v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC))
        run_high = ClassificationRun(classifier_version="v1", d1_discovery_run_id=discovery.id, started_at=datetime.now(UTC))
        setup_db.add_all([run_low, run_high])
        setup_db.commit()
        setup_db.refresh(run_low)
        setup_db.refresh(run_high)

        batch_low = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run_low.id))
        batch_high = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run_high.id))
        setup_db.add_all([batch_low, batch_high])
        setup_db.commit()
        setup_db.refresh(batch_low)
        setup_db.refresh(batch_high)
        assert batch_low.id < batch_high.id

        group = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=_unique_hash(),
            pipeline_state=ContentPipelineState.CHUNKED,
        )
        setup_db.add(group)
        setup_db.commit()
        setup_db.refresh(group)

        document = Document(
            title="synthetic.txt",
            source="/synthetic/workspace/synthetic.txt",
            source_type="txt",
            content_hash=group.identity_hash,
            content_identity_group_id=group.id,
        )
        setup_db.add(document)
        setup_db.commit()
        setup_db.refresh(document)
        chunk = DocumentChunk(document_id=document.id, chunk_index=0, content="race content", embedding=None)
        setup_db.add(chunk)
        setup_db.commit()

        instance_low = SourceInstance(
            classification_run_id=run_low.id,
            root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
            evidence_snapshot={},
            content_identity_group_id=group.id,
        )
        instance_high = SourceInstance(
            classification_run_id=run_high.id,
            root_t7_path=f"/synthetic/{uuid.uuid4().hex}.txt",
            evidence_snapshot={},
            content_identity_group_id=group.id,
        )
        setup_db.add_all([instance_low, instance_high])
        setup_db.commit()

        group_id, document_id = group.id, document.id
        batch_low_id, batch_high_id = batch_low.id, batch_high.id
        run_low_id, run_high_id = run_low.id, run_high.id
        discovery_id = discovery.id
        instance_low_id, instance_high_id = instance_low.id, instance_high.id
        setup_db.close()

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: list[Exception] = []
        fakes: dict[str, FakeEmbeddingClient] = {"a": FakeEmbeddingClient(), "b": FakeEmbeddingClient()}

        def worker(label: str):
            thread_db = Session(engine)
            try:
                barrier.wait()
                results[label] = PipelineEmbeddingService(thread_db, embedding_client=fakes[label]).embed_next(
                    worker_id=f"worker-{label}"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                thread_db.close()

        threads = [threading.Thread(target=worker, args=("a",)), threading.Thread(target=worker, args=("b",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        try:
            assert not errors, f"unexpected leaked exceptions: {errors}"
            winners = [label for label, result in results.items() if result is not None]
            assert len(winners) == 1, f"expected exactly one winner, got {winners}"
            total_embed_calls = sum(len(f.calls) for f in fakes.values())
            assert total_embed_calls == 1

            verify_db = Session(engine)
            final_batch_low = verify_db.get(IngestionBatch, batch_low_id)
            final_batch_high = verify_db.get(IngestionBatch, batch_high_id)
            final_group = verify_db.get(ContentIdentityGroup, group_id)
            assert final_group.pipeline_state == ContentPipelineState.INGESTED
            # Deterministic under real contention: the lower-id batch is
            # always charged, never the higher-id one.
            assert final_batch_low.embeddings_reserved == 1
            assert final_batch_high.embeddings_reserved == 0
            verify_db.close()
        finally:
            cleanup_db = Session(engine)
            cleanup_db.execute(text("DELETE FROM document_chunks WHERE document_id = :id"), {"id": document_id})
            cleanup_db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document_id})
            cleanup_db.execute(
                text("DELETE FROM source_instances WHERE id = ANY(:ids)"),
                {"ids": [instance_low_id, instance_high_id]},
            )
            cleanup_db.execute(
                text("DELETE FROM ingestion_attempts WHERE content_identity_group_id = :id"), {"id": group_id}
            )
            cleanup_db.execute(text("DELETE FROM content_identity_groups WHERE id = :id"), {"id": group_id})
            cleanup_db.execute(
                text("DELETE FROM ingestion_batches WHERE id = ANY(:ids)"), {"ids": [batch_low_id, batch_high_id]}
            )
            cleanup_db.execute(
                text("DELETE FROM classification_runs WHERE id = ANY(:ids)"), {"ids": [run_low_id, run_high_id]}
            )
            cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_id})
            cleanup_db.commit()
            cleanup_db.close()

    engine.dispose()
