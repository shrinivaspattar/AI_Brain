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


def test_read_file_rejects_a_literal_classification_run_reference_source_path(
    tmp_path: Path,
) -> None:
    """The precise scenario `6491dad` named: a COMPLETED ImportJob whose
    `source_path` is the literal non-path reference string
    "classification_run:<id>" (never a real T7 directory), with the
    correct non-filesystem `source_type`. Proves the recommended
    convention actually works end to end, not just that an arbitrary
    non-filesystem source_type is excluded (the previous test already
    covers that with a real directory as source_path - this one uses
    the exact reference-string shape a future T7-backed ImportJob would
    use).
    """
    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    real_file_elsewhere = tmp_path / "real_secret.txt"
    real_file_elsewhere.write_text("must not be readable via the reference job")

    with Session(engine) as db:
        job = ImportJob(
            name="File Access Integration Test - literal reference source_path",
            source_path="classification_run:999999",
            source_type="classification_run_reference",
            status=ImportStatus.COMPLETED,
        )
        db.add(job)
        db.commit()

        try:
            service = FileAccessService(db)

            # The reference string itself is not readable as a file.
            try:
                service.read_file("classification_run:999999")
                raise AssertionError("Expected FileAccessError for the reference string itself")
            except FileAccessError:
                pass

            # And it grants no access to anything else either.
            try:
                service.read_file(str(real_file_elsewhere))
                raise AssertionError("Expected FileAccessError for an unrelated real file")
            except FileAccessError:
                pass
        finally:
            db.query(ImportJob).filter(ImportJob.id == job.id).delete(
                synchronize_session=False
            )
            db.commit()


def test_reference_string_path_resolution_stays_confined_even_if_mislabeled(
    tmp_path: Path, monkeypatch
) -> None:
    """Defense-in-depth check, not a claim about the actual built
    system (which never does this): if a "classification_run:<id>"
    reference string were ever mistakenly given `source_type =
    "filesystem"` (the one thing the allow-list is supposed to
    prevent), verify what `Path(...).resolve()` actually does with it -
    it resolves RELATIVE TO THE PROCESS'S CURRENT WORKING DIRECTORY
    (no leading slash), producing a path under the application's own
    cwd, never a path outside it. This is proven with entirely
    synthetic paths under `tmp_path` (chdir'd here so the "cwd-
    relative" resolution is fully controlled) - deliberately NEVER
    with a string resembling either real T7 mount point, even as a
    "should be rejected" input, since even a `Path.resolve()` call
    against such a string would perform real stat/readlink syscalls
    against that real, currently-mounted filesystem to normalize it -
    exactly the kind of "verification that itself touches the real
    path" this project's own D2 safety-follow-up incident already
    established must never happen, rejected or not.
    """
    monkeypatch.chdir(tmp_path)

    unrelated_dir = tmp_path / "completely_unrelated_directory"
    unrelated_dir.mkdir()
    (unrelated_dir / "secret.txt").write_text("must not be readable via the reference job")

    database_url = make_url(settings.DATABASE_URL).set(database="aibrain_test")
    engine = create_engine(database_url)

    with Session(engine) as db:
        # Deliberately mislabeled - source_type says "filesystem" even
        # though source_path is a reference string, simulating the
        # exact mistake the allow-list exists to prevent if it were
        # ever bypassed or misconfigured elsewhere.
        job = ImportJob(
            name="File Access Integration Test - mislabeled reference (defense in depth)",
            source_path="classification_run:42",
            source_type="filesystem",
            status=ImportStatus.COMPLETED,
        )
        db.add(job)
        db.commit()

        try:
            service = FileAccessService(db)

            # Even with the mistaken "filesystem" label, the allowed
            # root is confined to <cwd>/classification_run:42 - a
            # sibling directory under the same cwd must still never be
            # readable through it.
            try:
                service.read_file(str(unrelated_dir / "secret.txt"))
                raise AssertionError(
                    "A sibling directory must never resolve as relative to "
                    "a cwd-relative reference-string root"
                )
            except FileAccessError:
                pass
        finally:
            db.query(ImportJob).filter(ImportJob.id == job.id).delete(
                synchronize_session=False
            )
            db.commit()
