"""Full-stack regression tests for POST /import-jobs/{id}/execute.

Unlike the rest of tests/integration/ (which call ImportJobService
directly against a real database), these go through the real FastAPI
HTTP layer via TestClient, with `get_db` overridden to a real
`aibrain_test` session. That's deliberate: the bug being regression-
tested here (a nonexistent source path becoming a bare HTTP 500) lived
entirely in the API layer's exception handling, not in the service -
a service-level test alone would not have caught it.
"""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportStatus


def _test_engine():
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    return create_engine(database_url)


def _override_get_db(engine):
    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    return override


def _create_job(engine, *, name: str, source_path: str) -> int:
    with Session(engine) as db:
        job = ImportJob(
            name=name,
            source_path=source_path,
            source_type="filesystem",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id


def _cleanup_job(engine, job_id: int) -> None:
    with Session(engine) as db:
        db.query(ImportJob).filter(ImportJob.id == job_id).delete(
            synchronize_session=False
        )
        db.commit()


def test_execute_import_job_returns_clean_422_for_missing_source_path(
    tmp_path: Path,
) -> None:
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    missing_source = tmp_path / "does-not-exist"
    job_id = _create_job(
        engine,
        name="API Execute Regression - missing path",
        source_path=str(missing_source),
    )

    try:
        client = TestClient(app)
        response = client.post(f"/import-jobs/{job_id}/execute")

        assert response.status_code == 422
        body = response.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)
        # No stack trace or internal implementation details - just the
        # same message that ends up in the job's own error_message.
        assert "Traceback" not in body["detail"]
        assert "raise" not in body["detail"]

        with Session(engine) as db:
            job = db.get(ImportJob, job_id)
            assert job.status == ImportStatus.FAILED
            assert job.error_message == body["detail"]
            assert job.finished_at is not None
            assert job.started_at is not None

    finally:
        app.dependency_overrides.clear()
        _cleanup_job(engine, job_id)


def test_execute_import_job_returns_clean_422_when_source_is_a_file(
    tmp_path: Path,
) -> None:
    """NotADirectoryError - source_path exists but isn't a directory.

    Documented as an expected SourceScanner failure mode (see
    app/ingestion/scanner.py), so it should get the same clean-error
    treatment as a missing path, not a 500.
    """
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    not_a_directory = tmp_path / "actually-a-file.txt"
    not_a_directory.write_text("this is a file, not a directory")

    job_id = _create_job(
        engine,
        name="API Execute Regression - not a directory",
        source_path=str(not_a_directory),
    )

    try:
        client = TestClient(app)
        response = client.post(f"/import-jobs/{job_id}/execute")

        assert response.status_code == 422
        body = response.json()
        assert "Traceback" not in body["detail"]

        with Session(engine) as db:
            job = db.get(ImportJob, job_id)
            assert job.status == ImportStatus.FAILED
            assert job.error_message == body["detail"]

    finally:
        app.dependency_overrides.clear()
        _cleanup_job(engine, job_id)


def test_execute_import_job_still_succeeds_for_a_valid_source_path(
    tmp_path: Path,
) -> None:
    """Regression guard: the fix must not change behavior for the
    success path - still 200, still COMPLETED, through the same real
    HTTP-to-database stack used by the failure-path tests above."""
    engine = _test_engine()
    app.dependency_overrides[get_db] = _override_get_db(engine)

    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("a real file to ingest")

    job_id = _create_job(
        engine,
        name="API Execute Regression - valid path",
        source_path=str(source),
    )

    try:
        client = TestClient(app)
        response = client.post(f"/import-jobs/{job_id}/execute")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["error_message"] is None

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            document_ids = list(
                db.scalars(
                    select(Document.id).where(Document.import_job_id == job_id)
                )
            )
            if document_ids:
                db.query(DocumentChunk).filter(
                    DocumentChunk.document_id.in_(document_ids)
                ).delete(synchronize_session=False)
            db.query(Document).filter(Document.import_job_id == job_id).delete(
                synchronize_session=False
            )
            db.commit()
        _cleanup_job(engine, job_id)
