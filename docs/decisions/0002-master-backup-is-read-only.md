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

## Known gap (found 2026-09-20, not yet resolved)

The third consequence above says cleanup and deduplication must operate on a
working copy, never on the master backup. That is not true for the original
(Chain 1) ingestion path:

- For loose, non-archive files, `DocumentIngestor` does not make a working
  copy. `Document.source` is the original file's own path
  (`app/ingestion/document_ingestor.py`); only archive members are extracted
  into `INGESTION_DIR`.
- The dedup plan then uses `Document.source` as each action's `source_path`
  (`app/dedup/execution_plan_service.py`), so an executed plan would
  quarantine-move the original file. The synthetic end-to-end demo
  (`scripts/dedup_pipeline_synthetic_demo.py`) showed exactly this: the file
  that moved was the one in the source folder, not a copy.
- The Chain 2 ingestion path is different: its `Document.source` is a
  workspace copy, so Chain 2 content is already on working copies.

**Why it is harmless today:** `DedupFilesystemExecutor` is not wired to any
API endpoint or CLI, and it takes `allowed_root` and `quarantine_root` from
its caller with no defaults. Nothing in the running application can trigger
it.

**Before anyone wires it up**, decide how the executor should honour this
decision. Options, not yet chosen:

- refuse any `allowed_root` inside a configured master-backup path;
- require plans to be built only from documents whose `source` lies under
  `INGESTION_DIR` (working copies);
- ingest loose files by copying them into `INGESTION_DIR` first.

Independently of that choice, mounting the master backup read-only at the OS
level (already suggested above) would make the gap unexploitable.

### Update (2026-09-20): guard added

The first option above is now implemented. `DedupFilesystemExecutor` refuses
to be constructed when its working area (`allowed_root`) is on, inside, or
above a protected path, or when its quarantine folder is inside one. Protected
paths come from the `MASTER_BACKUP_PATHS` setting (comma-separated absolute
paths) and a caller can add more through `protected_roots` but never remove
configured ones. Paths are resolved without needing to exist, so an unmounted
drive is still protected.

Limits, stated plainly: the guard protects only paths that are configured, so
`MASTER_BACKUP_PATHS` must be set wherever the executor could run. The
underlying fact that Chain 1 does not copy loose files is unchanged; the guard
prevents the executor from acting on the original location, it does not make a
working copy. Mounting the master backup read-only at the OS level remains the
stronger protection.

### Update (2026-09-22): the cleaned drive is the master copy

See [[0003-clean-master-copy]]. The drive was deduplicated and its archives
unpacked by hand; the read-only rule in this decision applies to the cleaned
content unchanged.
