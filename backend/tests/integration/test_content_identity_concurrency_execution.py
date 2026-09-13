"""The critical, explicitly-required real-database concurrency proof
for this schema: two genuinely concurrent attempts to establish the
SAME content identity (identity_kind, identity_algorithm, identity_hash)
must converge on exactly ONE ContentIdentityGroup row, never create
two conflicting ones.

Per this project's standing rule (already applied to Chain 1's dedup
executor - see test_dedup_executor_execution.py's
test_concurrent_execute_calls_only_one_claims_and_mutates): a
concurrency claim is proven with real, separate database sessions on
separate threads, never mocked. This is explicitly not something the
UNIQUE constraint alone is trusted to guarantee without a live test -
the user's own stated requirement for this implementation gate.

No T7 access: identity_hash values here are synthetic (random hex),
never computed from a real file.
"""

import threading
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.content_identity_service import ContentIdentityService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import (
    ContentIdentityAlgorithm,
    ContentIdentityGroup,
    ContentIdentityKind,
)
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.source_instance import SourceInstance


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _cleanup(engine, identity_hash: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM content_identity_groups WHERE identity_hash = :h"),
            {"h": identity_hash},
        )
        conn.commit()


def test_concurrent_claims_for_the_same_identity_hash_converge_on_one_group() -> None:
    """Two threads, two SEPARATE database sessions/connections
    (simulating two separate classification processes), both call
    `get_or_create_group` for the IDENTICAL identity at nearly the same
    moment. Required outcome: both calls succeed (this is a
    convergence, not a winner/loser race like Chain 1's execute()
    lock), and both return the SAME ContentIdentityGroup.id. Exactly
    one row must exist afterward - the UNIQUE constraint prevents two
    rows from persisting, and the service's IntegrityError-then-refetch
    path is what turns "one of us loses the INSERT race" into "both of
    us end up with the correct answer" instead of one caller crashing.
    """
    identity_hash = uuid.uuid4().hex + uuid.uuid4().hex
    engine = _engine()

    db_a = Session(engine)
    db_b = Session(engine)

    results: dict[str, int] = {}
    errors: dict[str, Exception] = {}
    barrier = threading.Barrier(2)

    def claim(name: str, db: Session) -> None:
        barrier.wait()
        try:
            group = ContentIdentityService(db).get_or_create_group(
                identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
                identity_algorithm=ContentIdentityAlgorithm.SHA256,
                identity_hash=identity_hash,
            )
            results[name] = group.id
        except Exception as exc:  # noqa: BLE001 - capturing for assertion below
            errors[name] = exc

    thread_a = threading.Thread(target=claim, args=("A", db_a))
    thread_b = threading.Thread(target=claim, args=("B", db_b))

    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert not thread_a.is_alive() and not thread_b.is_alive(), (
            "a thread did not finish - possible deadlock"
        )

        # Neither side may raise - this is a convergence, not a race
        # with an acceptable loser.
        assert errors == {}, f"expected no errors, got {errors}"
        assert len(results) == 2, f"expected both callers to succeed, got {results}"

        # Both callers ended up with the SAME group id.
        assert results["A"] == results["B"]

        # And the database itself holds exactly one row for this
        # identity - not two rows that happen to coincide in Python.
        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM content_identity_groups "
                    "WHERE identity_hash = :h"
                ),
                {"h": identity_hash},
            ).scalar_one()
        assert count == 1
    finally:
        db_a.close()
        db_b.close()
        _cleanup(engine, identity_hash)
        engine.dispose()


def test_ten_concurrent_claims_for_the_same_identity_hash_all_converge() -> None:
    """A higher-contention variant of the same proof: ten threads, ten
    separate sessions, all racing for one identity. Still exactly one
    row, and every thread's result agrees."""
    identity_hash = uuid.uuid4().hex + uuid.uuid4().hex
    engine = _engine()
    thread_count = 10

    sessions = [Session(engine) for _ in range(thread_count)]
    results: dict[int, int] = {}
    errors: dict[int, Exception] = {}
    barrier = threading.Barrier(thread_count)

    def claim(index: int, db: Session) -> None:
        barrier.wait()
        try:
            group = ContentIdentityService(db).get_or_create_group(
                identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
                identity_algorithm=ContentIdentityAlgorithm.SHA256,
                identity_hash=identity_hash,
            )
            results[index] = group.id
        except Exception as exc:  # noqa: BLE001
            errors[index] = exc

    threads = [
        threading.Thread(target=claim, args=(i, sessions[i]))
        for i in range(thread_count)
    ]

    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert all(not t.is_alive() for t in threads), "a thread did not finish"
        assert errors == {}, f"expected no errors, got {errors}"
        assert len(results) == thread_count

        distinct_group_ids = set(results.values())
        assert len(distinct_group_ids) == 1, (
            f"expected all {thread_count} callers to converge on one group, "
            f"got {distinct_group_ids}"
        )

        with engine.connect() as verify_conn:
            count = verify_conn.execute(
                text(
                    "SELECT COUNT(*) FROM content_identity_groups "
                    "WHERE identity_hash = :h"
                ),
                {"h": identity_hash},
            ).scalar_one()
        assert count == 1
    finally:
        for session in sessions:
            session.close()
        _cleanup(engine, identity_hash)
        engine.dispose()


def _setup_source_instance_and_two_groups(engine) -> tuple[int, int, int]:
    """Returns (source_instance_id, group_a_id, group_b_id) - a
    synthetic, DEFERRED-identity SourceInstance (content_identity_
    group_id starts NULL, exactly the archive-member/uniquely-sized-
    loose-file case from the frozen design) plus two already-distinct
    ContentIdentityGroups for two threads to race to assign."""
    with Session(engine) as setup_db:
        discovery_run = DiscoveryRun(
            run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
            source_root="/synthetic/not-a-real-t7-path",
            report_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            run_started_at=datetime.now(UTC),
            run_completed_at=datetime.now(UTC),
        )
        setup_db.add(discovery_run)
        setup_db.commit()

        classification_run = ClassificationRun(
            classifier_version="test-classifier-v1",
            d1_discovery_run_id=discovery_run.id,
            started_at=datetime.now(UTC),
        )
        setup_db.add(classification_run)
        setup_db.commit()

        instance = SourceInstance(
            classification_run_id=classification_run.id,
            root_t7_path="/synthetic/write_once_race.txt",
            evidence_snapshot={},
        )
        setup_db.add(instance)
        setup_db.commit()

        group_a = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        )
        group_b = ContentIdentityGroup(
            identity_kind=ContentIdentityKind.EXTRACTED_CONTENT,
            identity_algorithm=ContentIdentityAlgorithm.SHA256,
            identity_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        )
        setup_db.add(group_a)
        setup_db.add(group_b)
        setup_db.commit()

        return instance.id, group_a.id, group_b.id


def _cleanup_source_instance_race(engine, instance_id: int, group_ids: list[int]) -> None:
    with engine.connect() as conn:
        # Fetch the classification/discovery run ids before deleting the
        # instance, so they can be cleaned up too.
        row = conn.execute(
            text("SELECT classification_run_id FROM source_instances WHERE id = :i"),
            {"i": instance_id},
        ).first()
        classification_run_id = row[0] if row else None

        discovery_run_id = None
        if classification_run_id is not None:
            dr_row = conn.execute(
                text(
                    "SELECT d1_discovery_run_id FROM classification_runs WHERE id = :c"
                ),
                {"c": classification_run_id},
            ).first()
            discovery_run_id = dr_row[0] if dr_row else None

        conn.execute(text("DELETE FROM source_instances WHERE id = :i"), {"i": instance_id})
        if classification_run_id is not None:
            conn.execute(
                text("DELETE FROM classification_runs WHERE id = :c"),
                {"c": classification_run_id},
            )
        if discovery_run_id is not None:
            conn.execute(
                text("DELETE FROM discovery_runs WHERE id = :d"),
                {"d": discovery_run_id},
            )
        conn.execute(
            text("DELETE FROM content_identity_groups WHERE id = ANY(:ids)"),
            {"ids": group_ids},
        )
        conn.commit()


def test_write_once_under_real_concurrent_sessions_exactly_one_assignment_wins() -> None:
    """The point-5 requirement: two SEPARATE sessions/threads
    simultaneously attempt to assign DIFFERENT ContentIdentityGroups to
    the SAME SourceInstance. Required outcome, unlike the get_or_create_
    group convergence test above: this is a genuine winner/loser race,
    not a convergence - exactly ONE assignment may succeed, the other
    must fail cleanly with the documented write-once ValueError, and
    the database must end up holding whichever group won, never a
    silently overwritten or ambiguous value.
    """
    engine = _engine()
    instance_id, group_a_id, group_b_id = _setup_source_instance_and_two_groups(engine)

    db_a = Session(engine)
    db_b = Session(engine)

    results: dict[str, str] = {}
    errors: dict[str, Exception] = {}
    barrier = threading.Barrier(2)

    def assign(name: str, db: Session, group_id: int) -> None:
        barrier.wait()
        try:
            group = db.get(ContentIdentityGroup, group_id)
            ContentIdentityService(db).assign_content_identity(instance_id, group)
            results[name] = "succeeded"
        except Exception as exc:  # noqa: BLE001 - capturing for assertion below
            errors[name] = exc

    thread_a = threading.Thread(target=assign, args=("A", db_a, group_a_id))
    thread_b = threading.Thread(target=assign, args=("B", db_b, group_b_id))

    try:
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert not thread_a.is_alive() and not thread_b.is_alive(), (
            "a thread did not finish - possible deadlock"
        )

        # Exactly one assignment succeeded, exactly one was refused.
        assert len(results) == 1, f"expected exactly one success, got {results}"
        assert len(errors) == 1, f"expected exactly one failure, got {errors}"

        (loser_exc,) = errors.values()
        assert isinstance(loser_exc, ValueError)
        assert "write-once" in str(loser_exc)

        # The database holds exactly one of the two groups - never both,
        # never neither, never silently overwritten.
        with engine.connect() as verify_conn:
            row = verify_conn.execute(
                text(
                    "SELECT content_identity_group_id FROM source_instances "
                    "WHERE id = :i"
                ),
                {"i": instance_id},
            ).first()
        final_group_id = row[0]
        assert final_group_id in (group_a_id, group_b_id)

        winner_name = "A" if final_group_id == group_a_id else "B"
        assert winner_name in results
    finally:
        db_a.close()
        db_b.close()
        _cleanup_source_instance_race(engine, instance_id, [group_a_id, group_b_id])
        engine.dispose()
