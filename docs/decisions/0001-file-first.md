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

## Known gaps (found 2026-09-20, not yet resolved)

Checked against the running system, two places where "files are the source
of truth, the database can be rebuilt" does not fully hold:

1. **Conversations and memories exist only in Postgres.** The decision says
   knowledge extracted by the AI must never be the only copy of something.
   `Conversation`, `Message` and `Memory` rows have no on-disk counterpart,
   and there is no export code for them. A human-authored memory
   (`POST /memory`) is therefore lost if the database is dropped. At the
   time of writing the stakes are small (0 memories, 2 conversations,
   4 messages), but it grows with use. Fix options, not yet chosen: export
   approved memories and conversations to files, or state explicitly that
   they are database-only data alongside job history and audit state.

2. **Chain 2 documents can point at working copies that no longer exist.**
   Chain 2 stores content in a workspace directory and sets
   `Document.source` to that copy. The real-drive pilot used a workspace
   under `/tmp`, which a reboot removed: 6 of the 13 documents in the
   production database now have a `source` path missing on disk. Nothing is
   lost, since the originals remain on the source drive, provenance records
   their real paths, and search and chat use the stored chunks. But
   `Document.source` is misleading for these documents. Fix options, not yet
   chosen: run batches with a workspace on a persistent path, or treat
   `Document.source` as informational for Chain 2 and rely on provenance.
