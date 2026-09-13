"""Real-database tests for Implementation Milestone 3 (Resource Guard +
Batch Runtime/State Control) of the Scaled Real-T7 Ingestion design.
See "Scaled Real-T7 Ingestion - Implementation Design Pass" (`2fab4b3`),
section "### 12. Batch state machine - exact transitions", and the
numeric pass's "### 9. Runtime accounting - honest guarantees" for the
frozen behavior under test.

No T7 access of any kind: every `IngestionBatch` here is synthetic,
built directly (never via `BatchCreationService`, which is out of this
milestone's scope). Real-concurrency claims are proven against real
Postgres (`aibrain_test`), matching this project's non-negotiable
standard (see `test_batch_creation_execution.py`'s own concurrency
test for the established pattern this file mirrors).
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

from app.classification.batch_control_service import BatchControlService
from app.classification.classification_run_service import ClassificationRunService
from app.classification.resource_guard import GuardResult, GuardTier
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


@pytest.fixture()
def db():
    """Savepoint-isolated real Postgres session - matches this
    project's established fixture pattern. `BatchControlService`'s own
    internal `commit()` calls only affect the savepoint here, never the
    outer, test-isolating transaction."""
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
        classifier_version="test-batch-control-v1",
        d1_discovery_run_id=discovery.id,
    )


def _minimal_batch_kwargs(classification_run_id: int) -> dict:
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


def _batch(
    db: Session,
    *,
    status: BatchStatus = BatchStatus.PLANNED,
    stop_reason: BatchStopReason | None = None,
    stop_reason_detail: str | None = None,
) -> IngestionBatch:
    run = _classification_run(db)
    batch = IngestionBatch(
        status=status,
        stop_reason=stop_reason,
        stop_reason_detail=stop_reason_detail,
        **_minimal_batch_kwargs(run.id),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


class _FakeClock:
    """Deterministic stand-in for `time.monotonic` - real wall-clock
    sleeps would make runtime-accounting assertions flaky and slow."""

    def __init__(self, start: float = 1_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr("app.classification.batch_control_service.time.monotonic", fake)
    return fake


def _guard_always(tier: GuardTier, reason: BatchStopReason | None = None, detail: str = "synthetic") -> SimpleNamespace:
    """A minimal duck-typed guard - `BatchControlService.resume()` only
    ever calls `check_before_claim`, so nothing more needs faking."""
    return SimpleNamespace(check_before_claim=lambda batch: GuardResult(tier=tier, stop_reason=reason, detail=detail))


# ============================================================
# STATE: legal transitions
# ============================================================


def test_start_transitions_planned_to_running_and_sets_started_at(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    result = BatchControlService(db).start(batch.id)
    assert result.applied is True
    assert result.batch.status is BatchStatus.RUNNING
    assert result.batch.started_at is not None
    assert result.batch.stop_reason is None


@pytest.mark.parametrize(
    "reason",
    [BatchStopReason.WORKSPACE_SOFT_STOP, BatchStopReason.POSTGRES_SOFT_STOP, BatchStopReason.MANUAL_PAUSE],
)
def test_pause_transitions_running_to_paused_with_each_valid_reason(db: Session, reason: BatchStopReason) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    result = BatchControlService(db).pause(batch.id, reason=reason, detail="d")
    assert result.applied is True
    assert result.batch.status is BatchStatus.PAUSED
    assert result.batch.stop_reason is reason
    assert result.batch.stop_reason_detail == "d"


def test_resume_transitions_paused_to_running_when_guard_is_normal(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    result = BatchControlService(db).resume(batch.id, guard=_guard_always(GuardTier.NORMAL))
    assert result.applied is True
    assert result.batch.status is BatchStatus.RUNNING


@pytest.mark.parametrize(
    "reason",
    [
        BatchStopReason.WORKSPACE_HARD_STOP,
        BatchStopReason.POSTGRES_HARD_STOP,
        BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE,
        BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED,
    ],
)
def test_abort_transitions_running_to_aborted_with_each_valid_reason(db: Session, reason: BatchStopReason) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    result = BatchControlService(db).abort(batch.id, reason=reason, detail="d")
    assert result.applied is True
    assert result.batch.status is BatchStatus.ABORTED
    assert result.batch.stop_reason is reason
    assert result.batch.completed_at is not None


def test_abort_transitions_paused_to_aborted(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    result = BatchControlService(db).abort(batch.id, reason=BatchStopReason.WORKSPACE_HARD_STOP)
    assert result.applied is True
    assert result.batch.status is BatchStatus.ABORTED


# -- MANUAL_ABORT: the explicit operator-abort path (Milestone 3 final correction) --


def test_paused_to_manual_abort_applies(db: Session) -> None:
    """The one edge the frozen state machine documents as
    'operator aborts explicitly': PAUSED -> ABORTED."""
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    result = BatchControlService(db).abort(batch.id, reason=BatchStopReason.MANUAL_ABORT, detail="operator decision")
    assert result.applied is True
    assert result.batch.status is BatchStatus.ABORTED
    assert result.batch.stop_reason is BatchStopReason.MANUAL_ABORT
    assert result.batch.stop_reason_detail == "operator decision"
    assert result.batch.completed_at is not None


def test_running_to_manual_abort_does_not_apply(db: Session) -> None:
    """Verified against the frozen Architecture doc rather than assumed:
    RUNNING -> ABORTED's only documented causes are hard-stop /
    persistent-Ollama-failure / safety-invariant-violation - no
    operator-explicit cause is listed for that edge. MANUAL_ABORT is a
    structurally valid reason value, but illegal from RUNNING - the
    call must cleanly report `applied=False`, never silently abort the
    still-RUNNING batch and never raise."""
    batch = _batch(db, status=BatchStatus.RUNNING)
    result = BatchControlService(db).abort(batch.id, reason=BatchStopReason.MANUAL_ABORT)
    assert result.applied is False
    assert result.batch.status is BatchStatus.RUNNING
    assert result.batch.stop_reason is None


def test_manual_abort_persists_stop_reason_together_with_status(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.WORKSPACE_SOFT_STOP)
    BatchControlService(db).abort(batch.id, reason=BatchStopReason.MANUAL_ABORT, detail="d")
    row = db.execute(
        text("SELECT status, stop_reason, stop_reason_detail FROM ingestion_batches WHERE id = :id"),
        {"id": batch.id},
    ).one()
    assert row.status == "ABORTED"
    assert row.stop_reason == "MANUAL_ABORT"
    assert row.stop_reason_detail == "d"


def test_rejected_manual_abort_from_running_does_not_corrupt_a_live_runtime_session(
    db: Session, clock: _FakeClock
) -> None:
    """Regression test for a bug caught while wiring MANUAL_ABORT in:
    an abort() call with a structurally-valid reason but the wrong
    current status must NOT discard this instance's in-memory session
    bookkeeping just because the attempt was made - the batch is still
    genuinely RUNNING, and a later, legitimate transition must still
    account for the FULL elapsed time from the original start()."""
    batch = _batch(db, status=BatchStatus.PLANNED)
    service = BatchControlService(db)
    service.start(batch.id)
    clock.advance(40.0)

    rejected = service.abort(batch.id, reason=BatchStopReason.MANUAL_ABORT)
    assert rejected.applied is False

    clock.advance(10.0)
    result = service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)
    assert result.applied is True
    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(50.0)


@pytest.mark.parametrize(
    "reason",
    [
        BatchStopReason.SOURCE_WORK_EXHAUSTED,
        BatchStopReason.EXTRACTED_BYTES_ENVELOPE_EXHAUSTED,
        BatchStopReason.EMBEDDINGS_ENVELOPE_EXHAUSTED,
        BatchStopReason.RUNTIME_BUDGET_EXCEEDED,
    ],
)
def test_complete_transitions_running_to_completed_with_each_valid_reason(db: Session, reason: BatchStopReason) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    result = BatchControlService(db).complete(batch.id, reason=reason)
    assert result.applied is True
    assert result.batch.status is BatchStatus.COMPLETED
    assert result.batch.stop_reason is reason
    assert result.batch.completed_at is not None


def test_exactly_six_legal_transitions_exist() -> None:
    """Meta-test pinning the frozen state machine's exact shape: the
    union of every reason set this service accepts, keyed by its
    (expected, new_status) transition pair, must be exactly the six
    frozen transitions - no more, no fewer. `MANUAL_ABORT` adds a new
    REASON, not a new transition EDGE - RUNNING->ABORTED and
    PAUSED->ABORTED remain the same two edges the state machine already
    had; only PAUSED->ABORTED's legal reason set gained a member."""
    from app.classification.batch_control_service import (
        _ABORT_REASON_LEGAL_SOURCES,
        _ABORT_REASONS,
        _COMPLETE_REASONS,
        _PAUSE_REASONS,
    )

    transitions = {
        (BatchStatus.PLANNED, BatchStatus.RUNNING),  # start()
        (BatchStatus.RUNNING, BatchStatus.PAUSED),  # pause()
        (BatchStatus.RUNNING, BatchStatus.ABORTED),  # abort() from RUNNING
        (BatchStatus.RUNNING, BatchStatus.COMPLETED),  # complete()
        (BatchStatus.PAUSED, BatchStatus.RUNNING),  # resume()
        (BatchStatus.PAUSED, BatchStatus.ABORTED),  # abort() from PAUSED
    }
    assert len(transitions) == 6
    assert _PAUSE_REASONS == {
        BatchStopReason.WORKSPACE_SOFT_STOP,
        BatchStopReason.POSTGRES_SOFT_STOP,
        BatchStopReason.MANUAL_PAUSE,
    }
    assert _ABORT_REASONS == {
        BatchStopReason.WORKSPACE_HARD_STOP,
        BatchStopReason.POSTGRES_HARD_STOP,
        BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE,
        BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED,
        BatchStopReason.MANUAL_ABORT,
    }
    # The four resource/safety reasons are legal from either RUNNING or
    # PAUSED; MANUAL_ABORT is legal ONLY from PAUSED - verified against
    # the frozen Architecture doc's exact transition-cause wording.
    for reason in (
        BatchStopReason.WORKSPACE_HARD_STOP,
        BatchStopReason.POSTGRES_HARD_STOP,
        BatchStopReason.OLLAMA_PERSISTENTLY_UNREACHABLE,
        BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED,
    ):
        assert _ABORT_REASON_LEGAL_SOURCES[reason] == (BatchStatus.RUNNING, BatchStatus.PAUSED)
    assert _ABORT_REASON_LEGAL_SOURCES[BatchStopReason.MANUAL_ABORT] == (BatchStatus.PAUSED,)
    assert _COMPLETE_REASONS == {
        BatchStopReason.SOURCE_WORK_EXHAUSTED,
        BatchStopReason.EXTRACTED_BYTES_ENVELOPE_EXHAUSTED,
        BatchStopReason.EMBEDDINGS_ENVELOPE_EXHAUSTED,
        BatchStopReason.RUNTIME_BUDGET_EXCEEDED,
    }


# ============================================================
# STATE: illegal transitions / reason validation
# ============================================================


def test_pause_rejects_a_completion_reason(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    with pytest.raises(ValueError):
        BatchControlService(db).pause(batch.id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)


def test_abort_rejects_a_pause_reason(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    with pytest.raises(ValueError):
        BatchControlService(db).abort(batch.id, reason=BatchStopReason.MANUAL_PAUSE)


def test_complete_rejects_a_hard_stop_reason(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    with pytest.raises(ValueError):
        BatchControlService(db).complete(batch.id, reason=BatchStopReason.WORKSPACE_HARD_STOP)


def test_start_on_already_running_batch_does_not_apply(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    result = BatchControlService(db).start(batch.id)
    assert result.applied is False
    assert result.batch.status is BatchStatus.RUNNING


def test_pause_on_planned_batch_does_not_apply(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    result = BatchControlService(db).pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)
    assert result.applied is False
    assert result.batch.status is BatchStatus.PLANNED


def test_complete_on_paused_batch_does_not_apply(db: Session) -> None:
    """No PAUSED -> COMPLETED transition exists in the frozen state
    machine."""
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.MANUAL_PAUSE)
    result = BatchControlService(db).complete(batch.id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)
    assert result.applied is False
    assert result.batch.status is BatchStatus.PAUSED


def test_aborted_is_always_terminal_resume_never_applies(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.ABORTED, stop_reason=BatchStopReason.WORKSPACE_HARD_STOP)
    result = BatchControlService(db).resume(batch.id, guard=_guard_always(GuardTier.NORMAL))
    assert result.applied is False
    assert result.batch.status is BatchStatus.ABORTED


def test_aborted_is_always_terminal_abort_again_does_not_reapply(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.ABORTED, stop_reason=BatchStopReason.WORKSPACE_HARD_STOP)
    result = BatchControlService(db).abort(batch.id, reason=BatchStopReason.POSTGRES_HARD_STOP)
    assert result.applied is False
    assert result.batch.status is BatchStatus.ABORTED
    # The original reason is preserved - a lost/rejected call must never
    # overwrite already-persisted terminal state.
    assert result.batch.stop_reason is BatchStopReason.WORKSPACE_HARD_STOP


def test_completed_is_terminal_every_further_call_reports_not_applied(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.COMPLETED, stop_reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)
    service = BatchControlService(db)
    assert service.start(batch.id).applied is False
    assert service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE).applied is False
    assert service.resume(batch.id, guard=_guard_always(GuardTier.NORMAL)).applied is False
    assert service.abort(batch.id, reason=BatchStopReason.WORKSPACE_HARD_STOP).applied is False
    assert service.complete(batch.id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED).applied is False


# ============================================================
# RESOURCE: resume() re-checks the guard
# ============================================================


def test_resume_soft_stop_refuses_to_resume(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.WORKSPACE_SOFT_STOP)
    result = BatchControlService(db).resume(
        batch.id, guard=_guard_always(GuardTier.SOFT_STOP, BatchStopReason.WORKSPACE_SOFT_STOP)
    )
    assert result.applied is False
    assert result.batch.status is BatchStatus.PAUSED
    assert result.batch.stop_reason is BatchStopReason.WORKSPACE_SOFT_STOP


def test_resume_hard_stop_aborts_instead_of_resuming(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.PAUSED, stop_reason=BatchStopReason.WORKSPACE_SOFT_STOP)
    result = BatchControlService(db).resume(
        batch.id, guard=_guard_always(GuardTier.HARD_STOP, BatchStopReason.WORKSPACE_HARD_STOP, "disk fell further")
    )
    assert result.applied is True
    assert result.batch.status is BatchStatus.ABORTED
    assert result.batch.stop_reason is BatchStopReason.WORKSPACE_HARD_STOP
    assert result.batch.stop_reason_detail == "disk fell further"


# ============================================================
# RUNTIME: monotonic accounting
# ============================================================


def test_pause_after_start_accumulates_the_session_delta(db: Session, clock: _FakeClock) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    service = BatchControlService(db)
    service.start(batch.id)
    clock.advance(120.0)
    result = service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)
    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(120.0)


def test_runtime_accumulates_cumulatively_across_pause_resume_cycles(db: Session, clock: _FakeClock) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    service = BatchControlService(db)
    service.start(batch.id)
    clock.advance(50.0)
    service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)

    clock.advance(9_999.0)  # wall-clock gap while paused must NOT count
    service.resume(batch.id, guard=_guard_always(GuardTier.NORMAL))
    clock.advance(30.0)
    result = service.complete(batch.id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED)

    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(80.0)


def test_a_fresh_service_instance_with_no_observed_session_contributes_zero_delta(db: Session, clock: _FakeClock) -> None:
    """Honest crash semantics: a `BatchControlService` that never itself
    called `start()`/`resume()` for this batch (e.g. a new process after
    a crash) must add exactly 0.0 - never fabricate a guess at the lost
    interval."""
    batch = _batch(db, status=BatchStatus.RUNNING)
    clock.advance(500.0)  # time "passed" but this instance never observed a checkpoint
    fresh_service = BatchControlService(db)
    result = fresh_service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)
    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(0.0)


def test_abort_from_running_also_accumulates_the_session_delta(db: Session, clock: _FakeClock) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    service = BatchControlService(db)
    service.start(batch.id)
    clock.advance(15.0)
    result = service.abort(batch.id, reason=BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED)
    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(15.0)


def test_abort_from_paused_does_not_double_count_the_already_checkpointed_session(
    db: Session, clock: _FakeClock
) -> None:
    batch = _batch(db, status=BatchStatus.PLANNED)
    service = BatchControlService(db)
    service.start(batch.id)
    clock.advance(10.0)
    service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE)
    clock.advance(999.0)  # elapsed while paused - never counted
    result = service.abort(batch.id, reason=BatchStopReason.WORKSPACE_HARD_STOP)
    assert result.batch.monotonic_runtime_seconds_consumed == pytest.approx(10.0)


# ============================================================
# SAFETY: status + stop_reason persisted together
# ============================================================


def test_status_and_stop_reason_are_persisted_together_in_one_update(db: Session) -> None:
    batch = _batch(db, status=BatchStatus.RUNNING)
    BatchControlService(db).abort(batch.id, reason=BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED, detail="d")
    row = db.execute(
        text("SELECT status, stop_reason, stop_reason_detail FROM ingestion_batches WHERE id = :id"),
        {"id": batch.id},
    ).one()
    assert row.status == "ABORTED"
    assert row.stop_reason == "SAFETY_INVARIANT_VIOLATION_DETECTED"
    assert row.stop_reason_detail == "d"


def test_terminal_aborted_batch_blocks_every_further_transition_attempt(db: Session) -> None:
    """A proxy, at the state-transition level, for 'prevents new work
    admission' (point 11 of the Milestone 3 authorization) - actual
    claim-admission blocking is a future worker-integration milestone's
    job; this milestone's own responsibility is that the terminal state,
    once persisted, can never be transitioned away from again."""
    batch = _batch(db, status=BatchStatus.RUNNING)
    service = BatchControlService(db)
    service.abort(batch.id, reason=BatchStopReason.WORKSPACE_HARD_STOP)
    for attempt in (
        lambda: service.pause(batch.id, reason=BatchStopReason.MANUAL_PAUSE),
        lambda: service.resume(batch.id, guard=_guard_always(GuardTier.NORMAL)),
        lambda: service.complete(batch.id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED),
    ):
        result = attempt()
        assert result.applied is False
        assert result.batch.status is BatchStatus.ABORTED


# ============================================================
# CONCURRENCY: real Postgres, separate connections, real races
# ============================================================


def _real_batch(engine) -> int:
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
    run = ClassificationRunService(setup_db).start_run(
        classifier_version="test-batch-control-concurrency-v1",
        d1_discovery_run_id=discovery.id,
    )
    batch = IngestionBatch(status=BatchStatus.RUNNING, **_minimal_batch_kwargs(run.id))
    setup_db.add(batch)
    setup_db.commit()
    batch_id = batch.id
    setup_db.close()
    return batch_id


def _cleanup_real_batch(engine, batch_id: int) -> None:
    """No cascade is defined on either FK - children must be deleted
    before parents: ingestion_batches -> classification_runs ->
    discovery_runs."""
    cleanup_db = Session(engine)
    classification_run_id = cleanup_db.execute(
        text("SELECT classification_run_id FROM ingestion_batches WHERE id = :id"), {"id": batch_id}
    ).scalar_one()
    discovery_run_id = cleanup_db.execute(
        text(
            "SELECT COALESCE(d0_discovery_run_id, d1_discovery_run_id, d2_discovery_run_id) "
            "FROM classification_runs WHERE id = :id"
        ),
        {"id": classification_run_id},
    ).scalar_one()
    cleanup_db.execute(text("DELETE FROM ingestion_batches WHERE id = :id"), {"id": batch_id})
    cleanup_db.execute(text("DELETE FROM classification_runs WHERE id = :id"), {"id": classification_run_id})
    cleanup_db.execute(text("DELETE FROM discovery_runs WHERE id = :id"), {"id": discovery_run_id})
    cleanup_db.commit()
    cleanup_db.close()


def _race(engine, batch_id: int, actions: list) -> tuple[list, list[Exception]]:
    """Runs each callable in `actions` in its own thread, on its own
    Session, released simultaneously via a Barrier - mirrors
    `test_batch_creation_execution.py`'s established concurrency-test
    shape exactly."""
    barrier = threading.Barrier(len(actions))
    results: list = [None] * len(actions)
    errors: list[Exception] = []

    def worker(index: int, action) -> None:
        thread_db = Session(engine)
        try:
            service = BatchControlService(thread_db)
            barrier.wait()
            results[index] = action(service)
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)
        finally:
            thread_db.close()

    threads = [threading.Thread(target=worker, args=(i, action)) for i, action in enumerate(actions)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_pause_racing_abort_exactly_one_applies() -> None:
    engine = _engine()
    batch_id = _real_batch(engine)
    try:
        results, errors = _race(
            engine,
            batch_id,
            [
                lambda service: service.pause(batch_id, reason=BatchStopReason.MANUAL_PAUSE),
                lambda service: service.abort(batch_id, reason=BatchStopReason.SAFETY_INVARIANT_VIOLATION_DETECTED),
            ],
        )
        assert not errors, f"unexpected errors leaked from a race: {errors}"
        applied = [r for r in results if r.applied]
        assert len(applied) == 1, "exactly one of two racing transitions may apply"
        final_status = applied[0].batch.status
        assert final_status in (BatchStatus.PAUSED, BatchStatus.ABORTED)

        verify_db = Session(engine)
        try:
            persisted = verify_db.get(IngestionBatch, batch_id)
            assert persisted.status is final_status
            assert persisted.stop_reason is not None
        finally:
            verify_db.close()
    finally:
        _cleanup_real_batch(engine, batch_id)
        engine.dispose()


def test_concurrent_complete_racing_abort_exactly_one_applies() -> None:
    engine = _engine()
    batch_id = _real_batch(engine)
    try:
        results, errors = _race(
            engine,
            batch_id,
            [
                lambda service: service.complete(batch_id, reason=BatchStopReason.SOURCE_WORK_EXHAUSTED),
                lambda service: service.abort(batch_id, reason=BatchStopReason.WORKSPACE_HARD_STOP),
            ],
        )
        assert not errors, f"unexpected errors leaked from a race: {errors}"
        applied = [r for r in results if r.applied]
        assert len(applied) == 1, "exactly one of two racing transitions may apply"
        assert applied[0].batch.status in (BatchStatus.COMPLETED, BatchStatus.ABORTED)
    finally:
        _cleanup_real_batch(engine, batch_id)
        engine.dispose()


def test_concurrent_resume_racing_abort_exactly_one_applies() -> None:
    engine = _engine()
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
    run = ClassificationRunService(setup_db).start_run(
        classifier_version="test-batch-control-concurrency-v1",
        d1_discovery_run_id=discovery.id,
    )
    batch = IngestionBatch(
        status=BatchStatus.PAUSED,
        stop_reason=BatchStopReason.MANUAL_PAUSE,
        **_minimal_batch_kwargs(run.id),
    )
    setup_db.add(batch)
    setup_db.commit()
    batch_id = batch.id
    setup_db.close()

    try:
        normal_guard = _guard_always(GuardTier.NORMAL)
        results, errors = _race(
            engine,
            batch_id,
            [
                lambda service: service.resume(batch_id, guard=normal_guard),
                lambda service: service.abort(batch_id, reason=BatchStopReason.WORKSPACE_HARD_STOP),
            ],
        )
        assert not errors, f"unexpected errors leaked from a race: {errors}"
        applied = [r for r in results if r.applied]
        assert len(applied) == 1, "exactly one of two racing transitions may apply"
        assert applied[0].batch.status in (BatchStatus.RUNNING, BatchStatus.ABORTED)
    finally:
        _cleanup_real_batch(engine, batch_id)
        engine.dispose()
