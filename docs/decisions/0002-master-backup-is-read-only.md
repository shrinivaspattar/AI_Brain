# 0002: Master backup is read-only

## Status
Accepted

## Context
The documents AI_Brain ingests come from an original archive of personal
files (old backups, exports, notes accumulated over time). This archive is
irreplaceable: if it's damaged or partially deleted, the source material is
gone for good, regardless of [[0001-file-first]] being able to rebuild the
*index* from files.

## Decision
The master backup is treated as strictly read-only by AI_Brain. Ingestion
(`SourceScanner`, `ArchiveExtractor`, `DocumentIngestor`) only ever reads
from it — it is never the ingestion *destination*, and no code path writes
to, moves, renames, or deletes anything inside it.

`ImportJob.source_path` may point into the master backup; `INGESTION_DIR`
(where archives are expanded and working copies are written) must always be
a separate location outside of it.

## Consequences
- Archive extraction (`ArchiveExtractor.extract`) always writes to a
  destination distinct from the source — extracting into the master backup
  itself is a bug, not a configuration choice.
- Any future feature that "cleans up," deduplicates, or reorganizes ingested
  content (see backlog: Perceptual Deduplication, Best Copy Arbitration)
  must operate on the derived/working copy, never on the master backup.
- The master backup can be mounted read-only at the OS/filesystem level as
  a defense-in-depth measure, since the application layer already assumes
  it never writes there.
- Recovery story: if the index or working copies are ever corrupted, the
  master backup is the one thing we can always re-ingest from.
