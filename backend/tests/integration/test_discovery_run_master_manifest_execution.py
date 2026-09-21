"""Real-database test for the D3_MASTER_MANIFEST discovery kind (decision
0003). Entirely synthetic: a throwaway report file and a fake root; no T7
access. Runs against `aibrain_test`, rolled back per test."""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.classification.discovery_run_service import DiscoveryRunService
from app.core.config import settings
from app.models.discovery_run import DiscoveryRun, DiscoveryRunKind


@pytest.fixture()
def db():
    engine = create_engine(make_url(settings.DATABASE_URL).set(database="aibrain_test"))
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer.rollback()
    connection.close()
    engine.dispose()


def test_master_manifest_kind_can_be_recorded_and_read_back(db, tmp_path):
    report = tmp_path / "manifest.csv"
    report.write_text("file,bytes\n/synthetic/a.txt,3\n")
    now = datetime.now(UTC)

    run = DiscoveryRunService(db).record_run(
        run_kind=DiscoveryRunKind.D3_MASTER_MANIFEST,
        source_root="/synthetic/not-a-real-master-root",
        report_path=report,
        run_started_at=now,
        run_completed_at=now,
    )

    assert run.report_sha256 == hashlib.sha256(report.read_bytes()).hexdigest()
    stored = db.get(DiscoveryRun, run.id)
    assert stored.run_kind == DiscoveryRunKind.D3_MASTER_MANIFEST


def test_database_enum_lists_all_four_kinds(db):
    labels = [r[0] for r in db.execute(text("select unnest(enum_range(null::discovery_run_kind))::text")).fetchall()]
    assert set(labels) >= {"D0_INVENTORY", "D1_DUPLICATE_ANALYSIS", "D2_PROVENANCE_ANALYSIS", "D3_MASTER_MANIFEST"}
