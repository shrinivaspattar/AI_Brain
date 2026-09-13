from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.files.service import FileAccessError, FileAccessService
from app.models.import_job import ImportJob, ImportStatus


def test_read_file_allowed_only_under_a_completed_import_job(
    tmp_path: Path,
) -> None:
    """Proves the path-scoping decision actually holds against a real
    database: a file is only readable if it sits under the source_path
    of an ImportJob whose status is COMPLETED - not PENDING/RUNNING/
    FAILED, and not an arbitrary directory that was never imported at
    all.
    """
    completed_source = tmp_path / "completed-source"
    completed_source.mkdir()
    (completed_source / "notes.txt").write_text("real ingested content")

    pending_source = tmp_path / "pending-source"
    pending_source.mkdir()
    (pending_source / "notes.txt").write_text("not yet ingested content")

    never_imported = tmp_path / "never-imported"
    never_imported.mkdir()
    (never_imported / "notes.txt").write_text("never touched by AI_Brain")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        completed_job = ImportJob(
            name="File Access Integration Test - completed",
            source_path=str(completed_source),
            source_type="filesystem",
            status=ImportStatus.COMPLETED,
        )
        pending_job = ImportJob(
            name="File Access Integration Test - pending",
            source_path=str(pending_source),
            source_type="filesystem",
            status=ImportStatus.PENDING,
        )
        db.add_all([completed_job, pending_job])
        db.commit()

        try:
            service = FileAccessService(db)

            content = service.read_file(str(completed_source / "notes.txt"))
            assert content == "real ingested content"

            try:
                service.read_file(str(pending_source / "notes.txt"))
                raise AssertionError(
                    "Expected FileAccessError for a non-completed import job"
                )
            except FileAccessError:
                pass

            try:
                service.read_file(str(never_imported / "notes.txt"))
                raise AssertionError(
                    "Expected FileAccessError for a never-imported directory"
                )
            except FileAccessError:
                pass

        finally:
            db.query(ImportJob).filter(
                ImportJob.id.in_([completed_job.id, pending_job.id])
            ).delete(synchronize_session=False)
            db.commit()


def test_read_file_rejects_a_completed_non_filesystem_source_type(
    tmp_path: Path,
) -> None:
    """The Controlled Ingestion Implementation gate's FileAccessService
    hardening: a COMPLETED ImportJob whose source_type is NOT
    "filesystem" - the real, concrete shape a future T7-backed
    classification/import reference would take (e.g.
    "classification_run:<id>") - must never be treated as a readable
    root, even though it satisfies every other condition
    (`_allowed_roots()` used to trust status=COMPLETED alone). This is
    a synthetic stand-in only: no real T7 path or ImportJob is created
    here.
    """
    non_filesystem_source = tmp_path / "not-really-a-filesystem-import"
    non_filesystem_source.mkdir()
    (non_filesystem_source / "notes.txt").write_text("must not be readable")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        job = ImportJob(
            name="File Access Integration Test - non-filesystem source_type",
            source_path=str(non_filesystem_source),
            source_type="classification_run_reference",
            status=ImportStatus.COMPLETED,
        )
        db.add(job)
        db.commit()

        try:
            service = FileAccessService(db)

            try:
                service.read_file(str(non_filesystem_source / "notes.txt"))
                raise AssertionError(
                    "Expected FileAccessError for a non-filesystem source_type"
                )
            except FileAccessError:
                pass
        finally:
            db.query(ImportJob).filter(ImportJob.id == job.id).delete(
                synchronize_session=False
            )
            db.commit()
