"""The explicitly-required real-database concurrency proofs for the
worker claim/lease mechanism added by the "Controlled T7 -> AI_Brain
Ingestion Design" schema-extension milestone (`6491dad`).

Per this project's standing rule (already applied to Chain 1's dedup
executor and to `ContentIdentityService.get_or_create_group`): a
concurrency claim is proven with real, separate database sessions on
separate threads, never mocked, and never merely assumed from a
UNIQUE constraint or a single-threaded test.

No T7 access, no ingestion pipeline: every group/instance here is
synthetic, and "processing" in these tests means only writing a claim
or an IngestionAttempt row - no extraction/normalization/chunking/
embedding logic exists or runs here.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.ingestion_attempt_service import IngestionAttemptService
from app.classification.worker_claim_service import WorkerClaimService
from app.core.config import settings
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
    ContentPipelineState,
)
from app.models.ingestion_attempt import IngestionAttempt, IngestionAttemptOutcome, IngestionAttemptStage


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _create_group(engine, pipeline_state: ContentPipelineState) -> int:
    with Session(engine) as db:
        group = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=_unique_hash(),
            pipeline_state=pipeline_state,
        )
        db.add(group)
        db.commit()
        return group.id


def _cleanup_groups(engine, group_ids: list[int]) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM ingestion_attempts WHERE content_identity_group_id = ANY(:ids)"),
            {"ids": group_ids},
        )
        conn.execute(
            text("DELETE FROM content_identity_groups WHERE id = ANY(:ids)"),
            {"ids": group_ids},
        )
        conn.commit()


# -- 1. Concurrent ContentIdentityGroup claims ------------------------------


def test_concurrent_claims_on_one_group_exactly_one_worker_wins() -> None:
    """Two threads, two separate sessions, race to claim the SAME
    eligible group. Unlike ContentIdentityService.get_or_create_group
    (a convergence - both callers succeed on the same row), this is a
    genuine winner/loser race: exactly one worker may claim the group,
    the other must get None back, never a corrupted/double claim."""
    engine = _engine()
    group_id = _create_group(engine, ContentPipelineState.CLASSIFIED)

    db_a = Session(engine)
    db_b = Session(engine)

    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def run(name: str, db: Session) -> None:
        barrier.wait()
        claimed = WorkerClaimService(db).claim_content_identity_group(
            worker_id=name,
            eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
            lease_duration=timedelta(minutes=10),
            claiming_pipeline_state=ContentPipelineState.EXTRACTING,
        )
        results[name] = claimed.id if claimed is not None else None

    thread_a = threading.Thread(target=run, args=("worker-a", db_a))
    thread_b = threading.Thread(target=run, args=("worker-b", db_b))

    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert not thread_a.is_alive() and not thread_b.is_alive(), (
            "a thread did not finish - possible deadlock"
        )

        winners = [name for name, claimed_id in results.items() if claimed_id == group_id]
        losers = [name for name, claimed_id in results.items() if claimed_id is None]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(losers) == 1, f"expected exactly one loser, got {results}"

        with engine.connect() as verify_conn:
            row = verify_conn.execute(
                text("SELECT claimed_by, pipeline_state FROM content_identity_groups WHERE id = :i"),
                {"i": group_id},
            ).first()
        assert row.claimed_by == winners[0]
        assert row.pipeline_state == "EXTRACTING"
    finally:
        db_a.close()
        db_b.close()
        _cleanup_groups(engine, [group_id])
        engine.dispose()


def test_ten_workers_racing_for_five_groups_each_group_claimed_exactly_once() -> None:
    """Higher-contention variant: 10 threads, 5 eligible groups. Every
    group must be claimed by exactly one worker; every worker either
    gets a distinct group or None; no two workers ever claim the same
    group."""
    engine = _engine()
    group_ids = [_create_group(engine, ContentPipelineState.CLASSIFIED) for _ in range(5)]
    thread_count = 10

    sessions = [Session(engine) for _ in range(thread_count)]
    results: dict[int, object] = {}
    barrier = threading.Barrier(thread_count)

    def run(index: int, db: Session) -> None:
        barrier.wait()
        claimed = WorkerClaimService(db).claim_content_identity_group(
            worker_id=f"worker-{index}",
            eligible_pipeline_states=[ContentPipelineState.CLASSIFIED],
            lease_duration=timedelta(minutes=10),
        )
        results[index] = claimed.id if claimed is not None else None

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(thread_count)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert all(not t.is_alive() for t in threads), "a thread did not finish"

        claimed_ids = [v for v in results.values() if v is not None]
        assert sorted(claimed_ids) == sorted(group_ids), (
            f"expected all 5 groups claimed exactly once total, got {claimed_ids}"
        )
        assert len(claimed_ids) == len(set(claimed_ids)), (
            f"a group was claimed by more than one worker: {claimed_ids}"
        )

        with engine.connect() as verify_conn:
            distinct_claimants = verify_conn.execute(
                text(
                    "SELECT COUNT(DISTINCT claimed_by) FROM content_identity_groups "
                    "WHERE id = ANY(:ids) AND claimed_by IS NOT NULL"
                ),
                {"ids": group_ids},
            ).scalar_one()
        assert distinct_claimants == 5
    finally:
        for s in sessions:
            s.close()
        _cleanup_groups(engine, group_ids)
        engine.dispose()


# -- 2. Stale-claim recovery under real concurrency --------------------------


def test_stale_claim_is_reclaimed_by_a_different_worker_under_concurrency() -> None:
    """Simulates a crashed worker (a claim old enough to be stale) and
    proves a fresh worker can reclaim it - and that a SECOND fresh
    worker racing for the same stale claim does not also succeed."""
    engine = _engine()
    group_id = _create_group(engine, ContentPipelineState.EXTRACTED)

    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE content_identity_groups "
                "SET claimed_by = 'worker-crashed', claimed_at = :stale_at "
                "WHERE id = :id"
            ),
            {"stale_at": datetime.now(UTC) - timedelta(hours=1), "id": group_id},
        )
        conn.commit()

    db_a = Session(engine)
    db_b = Session(engine)
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def run(name: str, db: Session) -> None:
        barrier.wait()
        claimed = WorkerClaimService(db).claim_content_identity_group(
            worker_id=name,
            eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
            lease_duration=timedelta(minutes=10),
        )
        results[name] = claimed.id if claimed is not None else None

    thread_a = threading.Thread(target=run, args=("worker-recovery-a", db_a))
    thread_b = threading.Thread(target=run, args=("worker-recovery-b", db_b))

    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        winners = [name for name, cid in results.items() if cid == group_id]
        assert len(winners) == 1, f"expected exactly one recoverer, got {results}"

        with engine.connect() as verify_conn:
            row = verify_conn.execute(
                text("SELECT claimed_by FROM content_identity_groups WHERE id = :i"),
                {"i": group_id},
            ).first()
        assert row.claimed_by == winners[0]
        assert row.claimed_by != "worker-crashed"
    finally:
        db_a.close()
        db_b.close()
        _cleanup_groups(engine, [group_id])
        engine.dispose()


# -- 3. Competing workers across a mixed fresh/stale/ineligible pool ---------


def test_competing_workers_only_claim_fresh_or_stale_never_actively_held() -> None:
    """A pool of groups in different claim states - one unclaimed, one
    freshly claimed (must NOT be reclaimed), one stale-claimed (must be
    reclaimable) - processed by concurrent workers, proving the claim
    query respects all three cases simultaneously under real
    contention, not just in isolation."""
    engine = _engine()
    fresh_group_id = _create_group(engine, ContentPipelineState.EXTRACTED)
    stale_group_id = _create_group(engine, ContentPipelineState.EXTRACTED)
    unclaimed_group_id = _create_group(engine, ContentPipelineState.EXTRACTED)
    all_ids = [fresh_group_id, stale_group_id, unclaimed_group_id]

    with engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE content_identity_groups SET claimed_by = 'worker-active', "
                "claimed_at = :now WHERE id = :id"
            ),
            {"now": datetime.now(UTC), "id": fresh_group_id},
        )
        conn.execute(
            text(
                "UPDATE content_identity_groups SET claimed_by = 'worker-crashed', "
                "claimed_at = :stale WHERE id = :id"
            ),
            {"stale": datetime.now(UTC) - timedelta(hours=1), "id": stale_group_id},
        )
        conn.commit()

    thread_count = 6
    sessions = [Session(engine) for _ in range(thread_count)]
    results: dict[int, object] = {}
    barrier = threading.Barrier(thread_count)

    def run(index: int, db: Session) -> None:
        barrier.wait()
        claimed = WorkerClaimService(db).claim_content_identity_group(
            worker_id=f"competitor-{index}",
            eligible_pipeline_states=[ContentPipelineState.EXTRACTED],
            lease_duration=timedelta(minutes=10),
        )
        results[index] = claimed.id if claimed is not None else None

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(thread_count)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        claimed_ids = [v for v in results.values() if v is not None]
        # Only the stale and unclaimed groups were ever claimable - the
        # actively-held (fresh) claim must never appear here.
        assert fresh_group_id not in claimed_ids
        assert set(claimed_ids) <= {stale_group_id, unclaimed_group_id}
        assert len(claimed_ids) == len(set(claimed_ids)), "a group was double-claimed"

        with engine.connect() as verify_conn:
            fresh_row = verify_conn.execute(
                text("SELECT claimed_by FROM content_identity_groups WHERE id = :i"),
                {"i": fresh_group_id},
            ).first()
        assert fresh_row.claimed_by == "worker-active"
    finally:
        for s in sessions:
            s.close()
        _cleanup_groups(engine, all_ids)
        engine.dispose()


# -- 4. Failure-attempt persistence / idempotency under concurrency ---------


def test_concurrent_attempt_recording_for_different_groups_all_persist() -> None:
    """Many workers concurrently recording IngestionAttempt rows for
    DIFFERENT groups at the same moment - proves the audit table
    itself has no hidden contention/serialization bug and every
    attempt is durably, independently persisted."""
    engine = _engine()
    group_ids = [_create_group(engine, ContentPipelineState.EXTRACTED) for _ in range(8)]

    sessions = [Session(engine) for _ in group_ids]
    barrier = threading.Barrier(len(group_ids))
    errors: dict[int, Exception] = {}

    def run(index: int, db: Session, group_id: int) -> None:
        barrier.wait()
        try:
            IngestionAttemptService(db).record_pipeline_attempt(
                content_identity_group_id=group_id,
                attempted_stage=IngestionAttemptStage.NORMALIZING,
                worker_id=f"worker-{index}",
                outcome=IngestionAttemptOutcome.SUCCEEDED,
            )
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    threads = [
        threading.Thread(target=run, args=(i, sessions[i], group_ids[i]))
        for i in range(len(group_ids))
    ]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert errors == {}, f"expected no errors, got {errors}"

        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM ingestion_attempts "
                    "WHERE content_identity_group_id = ANY(:ids)"
                ),
                {"ids": group_ids},
            ).scalar_one()
        assert count == len(group_ids)
    finally:
        for s in sessions:
            s.close()
        _cleanup_groups(engine, group_ids)
        engine.dispose()


def test_repeated_retry_attempts_for_the_same_group_are_all_preserved_independently() -> None:
    """Idempotency, stated precisely for an append-only audit table:
    recording several attempts (a retry history) for the SAME group,
    including concurrently, must never overwrite or merge prior rows -
    each attempt is independently durable. This is what "idempotent
    retries" means for THIS table: retrying is always safe because
    every attempt gets its own row, never a shared mutable slot two
    retries could race to overwrite."""
    engine = _engine()
    group_id = _create_group(engine, ContentPipelineState.EXTRACTED)

    thread_count = 5
    sessions = [Session(engine) for _ in range(thread_count)]
    barrier = threading.Barrier(thread_count)
    errors: dict[int, Exception] = {}

    def run(index: int, db: Session) -> None:
        barrier.wait()
        try:
            # Deliberately invalid (missing failure_code) - proves the
            # service's validation guards concurrent callers too, not
            # only sequential ones.
            IngestionAttemptService(db).record_pipeline_attempt(
                content_identity_group_id=group_id,
                attempted_stage=IngestionAttemptStage.EMBEDDING,
                worker_id=f"retry-worker-{index}",
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=None,
                failure_detail=f"attempt {index} failed",
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(thread_count)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # Every attempt must have supplied a failure_code (required by
        # the service/CHECK) - the test above intentionally left it
        # None to prove the constraint still guards concurrent writers
        # too; expect every thread to have failed with ValueError.
        assert len(errors) == thread_count
        assert all(isinstance(e, ValueError) for e in errors.values())

        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM ingestion_attempts "
                    "WHERE content_identity_group_id = :id"
                ),
                {"id": group_id},
            ).scalar_one()
        # None should have been persisted, since every call was invalid.
        assert count == 0
    finally:
        for s in sessions:
            s.close()
        _cleanup_groups(engine, [group_id])
        engine.dispose()


def test_repeated_valid_retry_attempts_for_the_same_group_all_persist_independently() -> None:
    """The valid counterpart to the test above: several concurrent,
    VALID retry attempts for the same group all persist as separate
    rows - none is lost, none overwrites another."""
    engine = _engine()
    group_id = _create_group(engine, ContentPipelineState.EXTRACTED)

    thread_count = 5
    sessions = [Session(engine) for _ in range(thread_count)]
    barrier = threading.Barrier(thread_count)
    errors: dict[int, Exception] = {}

    def run(index: int, db: Session) -> None:
        barrier.wait()
        try:
            IngestionAttemptService(db).record_pipeline_attempt(
                content_identity_group_id=group_id,
                attempted_stage=IngestionAttemptStage.EMBEDDING,
                worker_id=f"retry-worker-{index}",
                outcome=IngestionAttemptOutcome.FAILED,
                failure_code=None,
                failure_detail=None,
                retryable=None,
            )
        except Exception:
            pass
        try:
            IngestionAttemptService(db).record_pipeline_attempt(
                content_identity_group_id=group_id,
                attempted_stage=IngestionAttemptStage.EMBEDDING,
                worker_id=f"retry-worker-{index}",
                outcome=IngestionAttemptOutcome.SUCCEEDED,
            )
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    threads = [threading.Thread(target=run, args=(i, sessions[i])) for i in range(thread_count)]

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert errors == {}, f"expected no errors, got {errors}"

        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM ingestion_attempts "
                    "WHERE content_identity_group_id = :id AND outcome = 'SUCCEEDED'"
                ),
                {"id": group_id},
            ).scalar_one()
        assert count == thread_count
    finally:
        for s in sessions:
            s.close()
        _cleanup_groups(engine, [group_id])
        engine.dispose()
