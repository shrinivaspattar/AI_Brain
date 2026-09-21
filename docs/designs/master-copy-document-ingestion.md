# Design: ingesting the documents on the cleaned master copy

Status: DRAFT for review (2026-09-22). Nothing in this note has been run.
Follows [[0003-clean-master-copy]] and [[0002-master-backup-is-read-only]].

## Goal
Make the text content of the cleaned master copy searchable and answerable in
AI_Brain, using the existing Chain 2 pipeline (discovery, classification,
batch creation, orchestrated ingestion), without changing the drive.

## What is on the drive (measured 2026-09-22, from the recorded checksum list)
The master copy holds 85,801 distinct files, 60.8 GB. Only documents matter for
this design:

| Group | Files | Size |
|---|---|---|
| Document-type files (md, txt, json, html, pdf, docx, xlsx, pptx, csv, ipynb, xml, mbox, epub, rtf) | 14,349 | 7.72 GB |
| of which inside vendor or cache folders (node_modules, site-packages, .git, caches, sync-version folders) | 947 | 0.04 GB |
| **Core documents (candidates)** | **13,402** | **7.68 GB** |

Core documents by type: json 4,569 (642 MB), txt 4,486 (1.1 GB), md 1,828
(195 MB), html 1,277 (4.3 GB), pdf 776 (685 MB), docx 138, ipynb 100, csv 99,
pptx 56. Median size is 4 KB; the 90th percentile is 171 KB; 28 files are over
50 MB (3.9 GB in total, mostly html).

Not in scope here: video, audio, images (need transcription or OCR first), the
encrypted vault (locked), programs and caches. They remain on the drive.

## Fit with the existing pipeline
- Chain 2 already has a batch class for exactly this input:
  `text_document_batch_policy` admits loose files only, categories text
  document and structured data, low and medium risk. That is now a full match,
  because the master copy has no archives left.
- Files are read read-only from the drive; content is copied into the
  workspace before extraction, so `Document.source` points at a working copy
  and the dedup executor can never act on the master copy.
- The batch runner (`run_ingestion_batch.py`) takes no path argument. Real
  paths only enter through batch creation, from a gitignored selection file,
  following the pattern of the earlier pilots. No real drive path may appear
  in committed code or docs.

## Proposed flow
1. **Selection builder (new script, reads only the checksum list).** Filters the
   list to core documents, applies the exclusion rules, groups them, and writes
   a gitignored selection file plus a printed summary. It touches neither the
   drive nor the database.
2. **Discovery record.** Chain 2 needs a discovery run to reference. Existing
   kinds (D0, D1, D2) describe the old, messy drive and are stale. Options:
   (a) add a new kind for the master checksum list, which needs an Alembic
   migration; (b) register the list as a new D0-kind run. **Decision needed.**
3. **Batch creation** through the unmodified `BatchCreationService`, first in
   the test database, then in production.
4. **Bounded runs** through the unmodified runner, attended, resumable.

## Grouping
- Group by top-level folder first (keeps provenance readable), then split into
  batches by size so no batch is dominated by a few huge files.
- Large-file class: files over 50 MB (28 files, 3.9 GB) go into their own
  later batches with their own limits, not into the first ones.
- Exclusions applied by rule (listed in the selection summary, never silent):
  vendor and cache folders; files that fail a read test; files whose current
  SHA-256 no longer matches the recorded one (the drive changed since the
  clean-up).
- Order of work: pilot first, then folders by value (notes and documents before
  exports and metadata).

## Safety and checks
- Every batch is created in `aibrain_test` first; reaching `aibrain` needs an
  explicit argument, as the runner already enforces.
- Before a file is ingested, the recorded checksum is compared with the
  content actually read; a mismatch is skipped and reported, not ingested.
- The existing master-backup guard (`MASTER_BACKUP_PATHS`) stays configured.
- Recommended, by the user: mount the drive read-only at the OS level while
  ingesting.

## Success measures (to be recorded, not assumed)
- Eligible files versus ingested files versus failed files, with failures
  grouped by cause (unreadable, empty text, too large, timeout).
- Chunks and embeddings created, and time per file, measured on the pilot.
- A fixed set of real questions answered from the ingested documents, with the
  source file shown, judged by the user.

## Unresolved (marked deliberately; no numbers are invented here)
- Batch envelope limits (max files, max bytes, max embeddings, max runtime):
  CALIBRATION_REQUIRED from pilot measurements on this CPU-only machine.
- How to treat 4,569 json files (likely export metadata): ingest as structured
  data, skip, or sample first. UNRESOLVED; look at a sample before deciding.
- How to treat html files (4.3 GB, some very large): extract visible text only
  and set a size ceiling. UNRESOLVED.
- Discovery-run kind (option a or b above). UNRESOLVED.
- Whether code files (7,578, 0.3 GB) join the same batches or a later one.
  UNRESOLVED.

## Gates (each is a separate step with its own approval)
1. Approve this design.
2. Build and review the selection builder and its summary (no database).
3. Create the pilot batch in the test database.
4. Run the pilot and review its report and the question checks.
5. Create and run further batches, first in test, then in production.
