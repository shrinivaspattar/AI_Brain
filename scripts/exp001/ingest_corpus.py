#!/usr/bin/env python3
"""Experiment 001 step: ingest a corpus directory into the SCRATCH database
through the existing, unmodified Chain 1 pipeline (ImportJobService), so the
experiment measures retrieval over exactly the chunks/embeddings the real
system would produce (1000-char chunks, 200 overlap, nomic-embed-text).

Writes to the scratch database only. Requires: the scratch database exists
and has been migrated (`alembic upgrade head` with DATABASE_URL pointing at
it), and Ollama is running with the embedding model.

Usage:
    python scripts/exp001/ingest_corpus.py --source-dir <corpus dir>
"""

import argparse
import tempfile
from pathlib import Path

import exp001_common as common

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.schemas.import_job import ImportJobCreate  # noqa: E402
from app.services.import_job_service import ImportJobService  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--database", default=common.DEFAULT_SCRATCH_DB)
    parser.add_argument("--name", default="exp001-corpus")
    args = parser.parse_args()

    if not args.source_dir.is_dir():
        raise SystemExit(f"--source-dir {args.source_dir} is not a directory")

    common.import_all_models()
    engine = create_engine(common.scratch_url(args.database))

    with Session(engine) as db:
        common.assert_connected_to_scratch(db.connection())
        try:
            db.execute(text("SELECT 1 FROM alembic_version")).scalar()
        except Exception as exc:
            raise SystemExit(
                f"'{args.database}' is not migrated (no alembic_version table): {exc}"
            ) from exc

        with tempfile.TemporaryDirectory(prefix="exp001_ingest_") as tmp:
            service = ImportJobService(db, ingestion_dir=Path(tmp))
            job = service.create_job(
                ImportJobCreate(
                    name=args.name,
                    source_path=str(args.source_dir.resolve()),
                    source_type="filesystem",
                )
            )
            service.execute_job(job.id)

        documents = db.execute(text("SELECT count(*) FROM documents")).scalar()
        chunks = db.execute(text("SELECT count(*) FROM document_chunks")).scalar()
        unembedded = db.execute(
            text("SELECT count(*) FROM document_chunks WHERE embedding IS NULL")
        ).scalar()
        chunkless = db.execute(
            text(
                "SELECT count(*) FROM documents d WHERE NOT EXISTS "
                "(SELECT 1 FROM document_chunks c WHERE c.document_id = d.id)"
            )
        ).scalar()

    print(f"documents={documents} chunks={chunks} chunks_without_embedding={unembedded} "
          f"documents_without_chunks={chunkless}")
    if unembedded or chunkless:
        print("WARNING: some documents/chunks have no embedding (Ollama down or a file "
              "failed extraction). run_experiment.py will refuse to run until every "
              "chunk is embedded - fix and re-ingest into a fresh scratch database.")


if __name__ == "__main__":
    main()
