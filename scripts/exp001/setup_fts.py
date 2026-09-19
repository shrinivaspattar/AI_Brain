#!/usr/bin/env python3
"""Experiment 001 step: build the experiment-only full-text side table in the
SCRATCH database. `document_chunks` itself is never altered - the sparse
index lives in `exp001_chunk_fts(chunk_id, tsv)` with a GIN index, created
here and nowhere in the app's models or migrations. If the experiment later
justifies a production FTS column, that is a separate milestone with its own
Alembic migration and authorization gate.

Idempotent: rebuilds the table's contents from the current chunks.

Usage:
    python scripts/exp001/setup_fts.py
"""

import argparse

import exp001_common as common

from sqlalchemy import create_engine, text  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=common.DEFAULT_SCRATCH_DB)
    args = parser.parse_args()

    engine = create_engine(common.scratch_url(args.database))
    with engine.begin() as conn:
        common.assert_connected_to_scratch(conn)
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS exp001_chunk_fts ("
                " chunk_id integer PRIMARY KEY REFERENCES document_chunks(id) ON DELETE CASCADE,"
                " tsv tsvector NOT NULL)"
            )
        )
        conn.execute(text("TRUNCATE exp001_chunk_fts"))
        conn.execute(
            text(
                "INSERT INTO exp001_chunk_fts (chunk_id, tsv) "
                "SELECT id, to_tsvector('english', content) FROM document_chunks"
            )
        )
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_exp001_chunk_fts_tsv ON exp001_chunk_fts USING GIN (tsv)")
        )
        chunks = conn.execute(text("SELECT count(*) FROM document_chunks")).scalar()
        indexed = conn.execute(text("SELECT count(*) FROM exp001_chunk_fts")).scalar()

    print(f"document_chunks={chunks} exp001_chunk_fts={indexed}")


if __name__ == "__main__":
    main()
