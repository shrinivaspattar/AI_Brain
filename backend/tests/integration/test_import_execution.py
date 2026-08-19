from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document
from app.models.import_job import ImportJob, ImportStatus
from app.services.import_job_service import ImportJobService


def test_import_job_executes_against_test_database(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    (source / "notes.txt").write_text("hello AI_Brain")
    (source / "readme.md").write_text("# AI_Brain")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")

    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Integration Test Import",
            source_path=str(source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        result = service.execute_job(job.id)

        assert result.status == ImportStatus.COMPLETED
        assert result.progress == 100
        assert result.files_discovered == 2
        assert result.files_processed == 2

        documents = list(
            db.scalars(
                select(Document).where(
                    Document.source.in_(
                        [
                            str(source / "notes.txt"),
                            str(source / "readme.md"),
                        ]
                    )
                )
            )
        )

        assert len(documents) == 2

        assert {document.title for document in documents} == {
            "notes.txt",
            "readme.md",
        }


def test_import_job_executes_zip_against_test_database(
    tmp_path: Path,
) -> None:
    import zipfile

    source = tmp_path / "source"
    source.mkdir()

    archive = source / "knowledge.zip"

    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("notes.txt", "hello from ZIP")
        zip_file.writestr("docs/readme.md", "# AI_Brain")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="ZIP Integration Test",
            source_path=str(source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(db)

        result = service.execute_job(job.id)

        assert result.status == ImportStatus.COMPLETED
        assert result.progress == 100
        assert result.files_discovered == 3
        assert result.files_processed == 3

        documents = list(
            db.scalars(select(Document).where(Document.source.like(f"%{job.id}%")))
        )

        assert len(documents) == 2
        assert {document.title for document in documents} == {
            "notes.txt",
            "readme.md",
        }


def test_import_job_fails_when_source_is_missing(
    tmp_path: Path,
) -> None:
    missing_source = tmp_path / "does-not-exist"

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Missing Source Integration Test",
            source_path=str(missing_source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        try:
            service.execute_job(job.id)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Expected FileNotFoundError")

        db.refresh(job)

        assert job.status == ImportStatus.FAILED
        assert job.error_message
        assert job.finished_at is not None
        assert job.started_at is not None


def test_import_job_completes_when_source_is_empty(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty-source"
    source.mkdir()

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Empty Source Integration Test",
            source_path=str(source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        result = service.execute_job(job.id)

        assert result.status == ImportStatus.COMPLETED
        assert result.progress == 100
        assert result.files_discovered == 0
        assert result.files_processed == 0
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.error_message is None


def test_import_job_records_successful_lifecycle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("lifecycle test")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Lifecycle Integration Test",
            source_path=str(source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        result = service.execute_job(job.id)

        assert result.status == ImportStatus.COMPLETED
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.started_at <= result.finished_at


def test_import_job_records_failed_lifecycle(
    tmp_path: Path,
) -> None:
    missing_source = tmp_path / "missing-source"

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Failed Lifecycle Integration Test",
            source_path=str(missing_source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        try:
            service.execute_job(job.id)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Expected FileNotFoundError")

        db.refresh(job)

        assert job.status == ImportStatus.FAILED
        assert job.started_at is not None
        assert job.finished_at is not None
        assert job.started_at <= job.finished_at
        assert job.error_message


def test_completed_import_job_cannot_be_executed_again(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("duplicate execution test")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="Repeated Execution Integration Test",
            source_path=str(source),
            source_type="filesystem",
        )

        db.add(job)
        db.commit()
        db.refresh(job)

        service = ImportJobService(
            db,
            ingestion_dir=tmp_path / "imports",
        )

        result = service.execute_job(job.id)

        assert result.status == ImportStatus.COMPLETED

        try:
            service.execute_job(job.id)
        except ValueError as exc:
            assert str(exc) == f"Import job {job.id} cannot be executed"
        else:
            raise AssertionError(
                "Expected completed import job execution to be rejected"
            )
