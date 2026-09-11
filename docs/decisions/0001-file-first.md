# 0001: File-first architecture

## Status
Accepted

## Context
AI_Brain ingests personal documents, notes, and archives from disk. We need
to decide what the *source of truth* is: the filesystem, or Postgres, which
indexes it (including vector embeddings, via pgvector).

## Decision
Files on disk are the source of truth. `documents/` (and later `knowledge/`)
hold the real data. Postgres rows (`Document`, `DocumentChunk`, `ImportJob`,
...) — embeddings included — are derived indexes: metadata and vectors
computed *from* the files, not a replacement for them.

Practically, this means:
- Ingestion never deletes or rewrites the original source files it reads.
- The database can be dropped and rebuilt from the files on disk without
  losing data (aside from job history/audit state).
- Any "memory" or knowledge extracted by the AI is stored as files or as
  clearly-derived records that point back to a source file, never as the
  only copy of something.

## Consequences
- Rebuilding the index (Postgres, including its pgvector embeddings) from
  scratch is always possible and should be kept cheap, since it's the
  recovery path of last resort.
- Import jobs track *what was discovered and processed*, not the content
  itself — content stays on disk, referenced by `Document.source`.
- We need a consistent on-disk layout and stable paths, since paths (not
  database IDs) are the durable identity of a piece of knowledge.
- Costs: no single database transaction covers "file + index" consistency;
  ingestion code has to handle partial states (file present, index stale or
  missing) as the normal case, not an edge case.
