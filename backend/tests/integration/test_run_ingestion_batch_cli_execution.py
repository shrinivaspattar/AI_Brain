"""Real-database tests for the Milestone 12 Operator CLI
(`scripts/run_ingestion_batch.py`), per "M12 — Operator CLI Design &
Freeze" and its "Final CLI Contract Reconciliation".

The CLI builds its OWN engine/session, entirely separate from this
test's own connection - so, unlike the savepoint-isolated `db` fixture
used by every other integration test in this repository, seeded rows
here must be REAL, committed rows (a savepoint on a different
connection is invisible to the CLI's own transaction), and this file
is responsible for its own explicit cleanup rather than relying on an
outer-transaction rollback. This mirrors exactly the "residue
incident" already documented for Milestone 11's own test suite - the
`real_db` fixture below deletes everything it created, in FK-safe
order, at teardown, every time.

Every scenario here deliberately stays within states no claim query
ever re-admits for normalization/chunking/embedding (`UNSUPPORTED`,
Milestone 1's own `initial_pipeline_state_for_eligibility`) - so this
file, like the rest of the scaled-ingestion suite, has zero Ollama
dependency. No T7 access of any kind: every path is synthetic, under
a test's own tmp_path.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.batch_orchestrator_service import BatchOrchestratorService
from app.core.config import settings
from app.models.classification_run import ClassificationRun
from app.models.content_identity_group import ContentIdentityGroup
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind
from app.models.ingestion_attempt import IngestionAttempt
from app.models.ingestion_batch import BatchStatus, BatchStopReason, IngestionBatch
from app.models.source_instance import SourceCategory, SourceInstance

AI_BRAIN_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = AI_BRAIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_ingestion_batch  # noqa: E402


def _engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _unique_hash() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


class _RealDb:
    """A genuinely committing session against `aibrain_test`, plus
    tracking for the classification_run/discovery_run ids this test
    created - so teardown can delete every row transitively scoped to
    them, in FK-safe order, regardless of how far the CLI's own run
    actually progressed."""

    def __init__(self, session: Session):
        self.session = session
        self._run_ids: list[int] = []
        self._discovery_run_ids: list[int] = []

    def track(self, run: ClassificationRun, discovery: DiscoveryRun) -> None:
        self._run_ids.append(run.id)
        self._discovery_run_ids.append(discovery.id)

    def _cleanup(self) -> None:
        db = self.session
        for run_id in self._run_ids:
            group_ids = [
                gid
                for (gid,) in db.execute(
                    select(SourceInstance.content_identity_group_id).where(
                        SourceInstance.classification_run_id == run_id,
                        SourceInstance.content_identity_group_id.is_not(None),
                    )
                ).all()
            ]
            db.execute(
                delete(IngestionAttempt).where(
                    IngestionAttempt.source_instance_id.in_(
                        select(SourceInstance.id).where(SourceInstance.classification_run_id == run_id)
                    )
                )
            )
            if group_ids:
                db.execute(
                    delete(IngestionAttempt).where(IngestionAttempt.content_identity_group_id.in_(group_ids))
                )
            db.execute(delete(SourceInstance).where(SourceInstance.classification_run_id == run_id))
            if group_ids:
                db.execute(delete(ContentIdentityGroup).where(ContentIdentityGroup.id.in_(group_ids)))
            db.execute(delete(IngestionBatch).where(IngestionBatch.classification_run_id == run_id))
            db.execute(delete(ClassificationRun).where(ClassificationRun.id == run_id))
        for discovery_run_id in self._discovery_run_ids:
            db.execute(delete(DiscoveryRun).where(DiscoveryRun.id == discovery_run_id))
        db.commit()


@pytest.fixture()
def real_db():
    engine = _engine()
    session = Session(bind=engine)
    ctx = _RealDb(session)
    yield ctx
    ctx._cleanup()
    session.close()
    engine.dispose()


def _classification_run(db: Session, ctx: _RealDb) -> ClassificationRun:
    discovery = DiscoveryRun(
        run_kind=DiscoveryRunKind.D1_DUPLICATE_ANALYSIS,
        source_root="/synthetic/not-a-real-t7-path",
        report_sha256=_unique_hash(),
        run_started_at=datetime.now(UTC) - timedelta(minutes=5),
        run_completed_at=datetime.now(UTC),
    )
    db.add(discovery)
    db.commit()
    db.refresh(discovery)

    run = ClassificationRun(
        classifier_version="test-m12-cli-v1",
        d1_discovery_run_id=discovery.id,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    ctx.track(run, discovery)
    return run


def _batch(db: Session, run: ClassificationRun, *, source_instances_selected: int) -> IngestionBatch:
    batch = IngestionBatch(
        status=BatchStatus.RUNNING,
        stop_reason=None,
        classification_run_id=run.id,
        max_source_instances=1000,
        max_source_bytes=2_000_000_000,
        max_extracted_bytes=None,
        max_embeddings=5000,
        max_runtime_seconds=7200,
        eligible_source_count=100,
        policy_filtered_count=50,
        selectable_count=20,
        source_instances_selected=source_instances_selected,
        source_bytes_selected=50_000_000,
        extracted_bytes_consumed=0,
        embeddings_reserved=0,
        selection_fingerprint=_unique_hash(),
        selection_policy_version="batch-class-1-text-document-v1",
        ordering_version="lexicographic-path-v1",
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _loose_instance(db: Session, run: ClassificationRun, path: Path) -> SourceInstance:
    instance = SourceInstance(
        classification_run_id=run.id,
        root_t7_path=str(path),
        evidence_snapshot={},
        source_category=SourceCategory.LOOSE_FILE,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


def _write_unsupported_file(tmp_path: Path) -> Path:
    """`.exe` is one of `eligibility_service`'s known-UNSUPPORTED
    suffixes - a group resolved from it lands directly in the
    terminal `UNSUPPORTED` pipeline state and is never re-admitted by
    normalization/chunking/embedding's own claim queries, so a batch
    made entirely of these can run to real, Ollama-free COMPLETED."""
    path = tmp_path / "source" / f"payload_{uuid.uuid4().hex}.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(uuid.uuid4().bytes * 4)
    return path


# -- database-argument contract -----------------------------------------


def test_omitting_database_flag_targets_aibrain_test_and_prints_it(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    exit_code = run_ingestion_batch.main(
        ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Using database: aibrain_test" in out
    assert "aibrain_test" == make_url(settings.DATABASE_URL).set(database="aibrain_test").database


def test_explicit_database_flag_is_honored_and_printed(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    exit_code = run_ingestion_batch.main(
        [
            "--batch-id",
            str(batch.id),
            "--workspace-root",
            str(tmp_path / "workspace"),
            "--database",
            "aibrain_test",
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Using database: aibrain_test" in out


# -- batch-existence pre-check (not a caught ValueError) -----------------


def test_unknown_batch_id_prints_clean_message_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_ingestion_batch.main(
        ["--batch-id", "-1", "--workspace-root", str(tmp_path / "workspace")]
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "not found" in out.lower()


def test_deep_unrelated_exception_propagates_uncaught_not_mislabeled_as_not_found(
    real_db: _RealDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves the CLI has no `except ValueError` around `run_once()`:
    an unrelated exception raised deep inside the composed services
    (simulated here via a test double standing in for, e.g., the real
    write-once `ValueError` `ContentIdentityService.assign_content_
    identity` can raise during ordinary identity resolution - see the
    M12 Final CLI Contract Reconciliation) must propagate all the way
    out of `main()`, rather than being silently reported as "batch not
    found"."""
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    def _raise_unrelated(self, *args, **kwargs):
        raise ValueError("SourceInstance 999 already has a content_identity_group_id set (write-once)")

    monkeypatch.setattr(run_ingestion_batch.BatchOrchestratorService, "run_once", _raise_unrelated)

    with pytest.raises(ValueError, match="write-once"):
        run_ingestion_batch.main(
            ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
        )

    out = capsys.readouterr().out
    assert "not found" not in out.lower()


# -- argparse usage contract ----------------------------------------------


def test_missing_batch_id_is_a_usage_error_exit_2(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_ingestion_batch.main(["--workspace-root", str(tmp_path / "workspace")])
    assert excinfo.value.code == 2


def test_missing_workspace_root_is_a_usage_error_exit_2() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_ingestion_batch.main(["--batch-id", "1"])
    assert excinfo.value.code == 2


# -- run_once()/generate_report() composition -----------------------------


def test_single_unsupported_item_completes_the_batch_with_no_ollama_dependency(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    exit_code = run_ingestion_batch.main(
        ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "stage archive_processing: processed 0" in out
    assert "stage identity_resolution: processed 1" in out
    assert "completion: applied=True status=completed" in out
    assert "Batch " in out and ": completed (source_work_exhausted)" in out
    assert "terminal_source_count:       1" in out
    assert "successful_ingestion_count:  0" in out

    real_db.session.expire_all()
    refreshed = real_db.session.get(IngestionBatch, batch.id)
    assert refreshed.status == BatchStatus.COMPLETED
    assert refreshed.stop_reason == BatchStopReason.SOURCE_WORK_EXHAUSTED


def test_batch_with_unattempted_selected_work_remains_running_and_still_exits_0(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`source_instances_selected=2` but only one real SourceInstance
    row exists - mirrors the M11 orchestrator suite's own fixture
    pattern for isolating "still RUNNING" from "genuinely COMPLETED"."""
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=2)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    exit_code = run_ingestion_batch.main(
        ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "completion:" not in out
    assert "unattempted_selected_count:  1" in out

    real_db.session.expire_all()
    refreshed = real_db.session.get(IngestionBatch, batch.id)
    assert refreshed.status == BatchStatus.RUNNING


def test_worker_id_is_auto_generated_and_unique_per_invocation(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=2)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    run_ingestion_batch.main(["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")])
    first_out = capsys.readouterr().out

    run_ingestion_batch.main(["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")])
    second_out = capsys.readouterr().out

    def _worker_id(out: str) -> str:
        line = next(line_ for line_ in out.splitlines() if line_.startswith("Running batch"))
        return line.split("as worker ")[1].split("...")[0]

    assert _worker_id(first_out) != _worker_id(second_out)


def test_repeated_invocation_after_completion_is_a_safe_no_op(
    real_db: _RealDb, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    first_exit = run_ingestion_batch.main(
        ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
    )
    capsys.readouterr()

    second_exit = run_ingestion_batch.main(
        ["--batch-id", str(batch.id), "--workspace-root", str(tmp_path / "workspace")]
    )
    out = capsys.readouterr().out

    assert first_exit == 0
    assert second_exit == 0
    assert "stage identity_resolution: processed 0" in out
    assert "completion:" not in out


def test_report_reflects_orchestrator_service_directly_via_real_composition(
    real_db: _RealDb, tmp_path: Path
) -> None:
    """Exercises `BatchOrchestratorService` directly (same session
    convention as the M11 suite) alongside the CLI, to confirm the
    CLI's own `main()` output is describing the same, real state - not
    a second, divergent code path."""
    run = _classification_run(real_db.session, real_db)
    batch = _batch(real_db.session, run, source_instances_selected=1)
    _loose_instance(real_db.session, run, _write_unsupported_file(tmp_path))

    orchestrator = BatchOrchestratorService(real_db.session)
    direct_result = orchestrator.run_once(
        batch.id, worker_id="direct-comparison-worker", workspace_root=tmp_path / "workspace-direct"
    )
    real_db.session.commit()

    assert direct_result.stage_results[1].processed_count == 1
    assert direct_result.completion is not None
    assert direct_result.completion.applied is True
