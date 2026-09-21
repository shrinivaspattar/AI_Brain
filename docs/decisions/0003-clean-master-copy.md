# 0003: The cleaned external drive is the master copy

## Status
Accepted (2026-09-22)

## Context
The original archive of personal and professional files sat on an external
SSD as raw files and as zip/7z/rar archives, with the same content repeated
many times: loose copies, copies inside archives, archives inside archives,
and folder-level copies. It held 672 GB in 624,628 files, but only about 61 GB
of that was distinct content.

Decision [[0002-master-backup-is-read-only]] says the master backup is
irreplaceable and read-only for the application. Keeping that decision true
for a drive full of repeats would mean ingesting the same content many times.

## Decision
The external drive, after the deduplication and clean-up described below, is
the master copy of the source material. It is still read-only for AI_Brain
(0002 applies unchanged). What changed is its content: one copy of each
distinct file, no archives left to unpack, and every remaining file verified.

How the clean-up was done (all scripts are in `scripts/`, run by a person, and
every deletion was carried out by the user, never by the application):

1. Metadata scan of the whole tree, then table-of-contents and SHA-256 of
   every file, including files inside archives and nested archives
   (`scan_tree_metadata.py`, `scan_archive_contents.py`,
   `hash_archive_members.py`, `hash_all_files.py`).
2. A unique-content manifest and a plan: one preferred copy per content
   (`build_unique_manifest.py`, `build_removal_plan_from_hashes.py`).
3. Extraction of only the files that existed nowhere else, each verified by
   SHA-256 before its archive was retired
   (`extract_unique_and_retire_archives.py`).
4. A final full hash of the remaining files against the recorded checksums
   (`verify_kept_files.py`): 85,801 files, none missing, none changed.

The files, hashes and plans behind this live in the gitignored `knowledge/`
directory. Real drive paths are never written into committed source or
docs; batch scripts read them from local, gitignored configuration.

## Consequences
- The master copy is a set of raw files. AI_Brain fetches from them directly;
  nothing has to be unpacked first. The existing text extractor already reads
  PDF, Word, PowerPoint and Excel, and treats other extensions as plain text.
- Integrity is checkable at any time: re-hash the drive and compare with the
  recorded SHA-256 list (`verify_kept_files.py`). Any difference means the
  drive changed since the clean-up.
- Content that is not text yet (video, audio, images, encrypted vaults) is
  kept in the master copy but is not searchable until a transcription, OCR or
  unlock step exists. That is a later, separate decision per file type.
- Because there are no archives left, the archive-versus-loose identity rule
  in the ingestion design no longer has to be exercised for this source. The
  code stays, since other sources may still contain archives.
- Nothing in the application may write to, move, rename or delete files on
  the master copy. Mounting it read-only at the operating-system level is
  still the stronger protection and is recommended.
- New material must not be added to the master copy by the application. If
  the user adds files by hand, the recorded checksum list must be regenerated
  before it is used for verification again.

## Known limits
- Files that were password-protected inside the old archives (a handful, of no
  value to the user) were not readable and were removed on the user's
  decision, along with three folders of program installers and the Coursera
  course archives, which the user holds elsewhere.
- Four tutorial videos disappeared from the drive before the clean-up; they
  are not in the recorded list and are not part of the master copy.
