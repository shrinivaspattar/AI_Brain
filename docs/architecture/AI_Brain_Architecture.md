# AI_Brain Architecture

## Purpose
AI_Brain is a personal, offline, privacy-focused AI assistant. It ingests
your own documents and notes, builds a searchable knowledge base from them,
and lets you converse with a local LLM that can retrieve and cite that
knowledge (RAG) and accumulate long-term memory about you over time. See
[[0001-file-first]] and [[0002-master-backup-is-read-only]] for the
foundational data-handling decisions.

## Stack
- Ubuntu 24.04, Docker
- Backend: FastAPI (Python), SQLAlchemy + Alembic, PostgreSQL
- LLM runtime: Ollama, running local chat models (`qwen3:8b`, `qwen2.5-coder:14b`, ...) and `nomic-embed-text` for embeddings
- Vector store: pgvector (Postgres extension) — vectors live alongside `documents`/`document_chunks`, no separate vector service
- Cache/queue: Redis
- Frontend: plain HTML/CSS/vanilla JS, no build step, served directly by FastAPI (`frontend/`)

Everything runs locally; no data or requests leave the machine.

## Data flow (target end-to-end)

```
master backup / source files (read-only)
        │  SourceScanner
        ▼
   discovered files ──ArchiveExtractor──▶ expanded files (documents/imports/<job>/)
        │
        ▼
   DocumentIngestor ──▶ Document rows, tagged with import_job_id   [implemented]
        │
        ▼
   chunking + embeddings (app/embeddings), wired into execute_job   ──▶ DocumentChunk rows w/ pgvector embedding   [implemented]
        │
        ▼
   RAG retrieval (app/rag/retrieval_service.py, POST /rag/search) ──▶ ranked chunks + source Document   [implemented]
        │
        ▼
   chat (app/services/chat_service.py, POST /chat) ↔ Ollama (CHAT_MODEL)   [implemented]
        │  reads Memory rows (app/memory), injected as user-facts context
        ▼
   response, with citations back to source Document(s)   [implemented]
        │  chat_client.chat(messages, tools=...) ↔ tool_calls
        ▼
   tool loop (app/tools, up to 5 iterations) — search_knowledge_base,
   get_current_datetime, list_recent_documents, find_duplicate_documents,
   plan_duplicate_cleanup, read_file_content (all read-only/dry-run),
   remember (review-gated Memory proposal)   [implemented]
        │
        ▼
   dedup detection (app/dedup, GET /dedup/exact|near) — exact-hash and
   near-duplicate (pgvector) document groups, detection only   [implemented]
        │
        ▼
   dry-run cleanup planning (app/dedup, GET /dedup/exact/plan) — Best
   Copy Arbitration over exact-duplicate groups (keep oldest, propose
   deleting the rest); returns a plan only, never deletes anything
   [implemented]
        │
        ▼
   provenance trace (app/provenance, GET /documents/{id}/provenance) —
   walks ImportJob → Document → DocumentChunk → citing Message(s), read
   only   [implemented]
        │
        ▼
   browser (frontend/, served at "/") ↔ POST /chat, GET /chat/{id},
   GET /import-jobs, GET /memory + approve|reject — chat, read-only
   import job monitor, and human memory review queue, no build step
   [implemented]
```

## Backend module layout (`backend/app/`)

| Module | Status | Responsibility |
|---|---|---|
| `ingestion/` | Implemented | `SourceScanner` walks a source dir; `ArchiveExtractor` safely expands `.zip` and `.7z` archives (bomb/path-traversal/disk-space guarded — see below); `DocumentIngestor` orchestrates scan → extract → persist, and computes `Document.content_hash` (SHA-256, streamed) for exact-duplicate detection; `text_extractor.extract_text` pulls plain text out of `.pdf`/`.docx`/anything-UTF-8-decodable, used before embedding. |
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`, `Conversation`, `Message`, `Memory`, `ToolCallRecord`, `DuplicateReview`, `DuplicateReviewMember`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it. |
| `services/` | Implemented | `DocumentService` (persistence, `list_documents`), `ImportJobService` (job lifecycle, enforced state transitions, embeds documents synchronously during `execute_job`), `EmbeddingService` (chunk + embed + persist a document's text), `ChatService`/`ChatClient` (RAG-augmented chat over Ollama, conversation persistence, memory injection, tool-calling loop). |
| `api/` | Implemented | FastAPI routers: health, version, database, documents, import_jobs, rag, chat, memory, tools, dedup. |
| `db/` | Implemented | Session/engine setup, health checks. |
| `core/` | Implemented | `Settings` (env-driven config: `DATABASE_URL`, `INGESTION_DIR`, `OLLAMA_HOST`, `CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, ...). |
| `embeddings/` | Implemented | `chunker.chunk_text` (character-bounded, overlapping chunks); `client.EmbeddingClient` wraps Ollama's `embed` API for `nomic-embed-text`. |
| `rag/` | Implemented | `retrieval_service.RetrievalService.search(query, top_k)`: embeds the query, ranks `document_chunks` by pgvector cosine distance, joined to source `Document`. Exposed via `POST /rag/search`. No reranking/relevance filtering beyond raw distance yet. |
| `memory/` | Implemented | `service.MemoryService`: flat `content` + optional `confidence`/provenance store + `status` (pending/approved/rejected), no type taxonomy. `POST /memory`, `GET /memory?status=`, `POST /memory/{id}/approve`\|`/reject`, `DELETE /memory/{id}`. Review-gated write hook (`remember` tool) + read hook (approved-only) into `ChatService` — see Memory, below. |
| `tools/` | Implemented | `registry.Tool`/`ToolRegistry` (dispatch by name, `call()` never raises, returns a structured `ToolCallResult`). `builtin.build_default_registry`: `search_knowledge_base`, `get_current_datetime`, `list_recent_documents`, `find_duplicate_documents`, `plan_duplicate_cleanup`, `read_file_content` (all read-only/dry-run), plus `remember` (proposes a `Memory`, never writes one live — see Memory, below). Exposed via `GET /tools`; driven by `ChatService._run_tool_loop`, which persists a `ToolCallRecord` audit row per call. No write/move/delete or external-network tools exist. |
| `files/` | Implemented | `service.FileAccessService.read_file(path)`: the one tool with real filesystem access, scoped to paths under a COMPLETED `ImportJob.source_path` (path-traversal-safe via `Path.resolve()` + `is_relative_to`, same pattern as `ArchiveExtractor`). Reuses `ingestion.text_extractor.extract_text`; truncates at `MAX_FILE_READ_LENGTH`. Read-only — no write, move, or delete capability. Exposed via the `read_file_content` tool. |
| `dedup/` | Implemented (detection + dry-run plan); review model implemented, not yet exposed via API | `service.DeduplicationService`: `find_exact_duplicates` (groups by `Document.content_hash`), `find_near_duplicate_documents` (pgvector cosine similarity on first-chunk embeddings), `plan_exact_duplicate_cleanup` (Best Copy Arbitration + dry-run plan: keeps the oldest copy per exact-duplicate group by `created_at`/`id`, proposes deleting the rest — computing a plan never deletes, moves, or quarantines anything). Exposed via `GET /dedup/exact`\|`/near`\|`/exact/plan` and the `find_duplicate_documents`/`plan_duplicate_cleanup` tools. `review_service.DedupReviewService`: persists a detected finding as a `DuplicateReview` (see Deduplication, below, for the full design) and records human approve/reject decisions — no API endpoint yet. |
| `provenance/` | Implemented | `service.ProvenanceService.trace_document(document_id)`: walks the existing FK chain (`ImportJob` → `Document.import_job_id` → `DocumentChunk.document_id`, plus every `Message` whose denormalized `citations` names the document) into one queryable trace. Read-only, no new source of truth. Exposed via `GET /documents/{id}/provenance`. |
| `frontend/` (repo root, not under `backend/app/`) | Implemented (chat + import job monitor + memory review) | Plain `index.html`/`style.css`/`app.js`, no build step, no framework. Mounted by FastAPI at `"/"` via `StaticFiles(..., html=True)` (see Frontend, below), so the whole app is one process on one port. Three views toggled client-side: Chat, Import Jobs (read-only, polls `GET /import-jobs`), and Memory Review (human approve/reject queue over `GET /memory?status=`). |

## Archive extraction
`ArchiveExtractor.extract(files, destination)` (`app/ingestion/archive.py`)
dispatches by extension:
- `.zip` (`zipfile`, stdlib): per-member checks — path-traversal
  (`is_relative_to(destination)`), expansion-ratio bomb protection
  (`compress_size` vs. `file_size`, hard limit 1000×), then a disk-space
  check (sum of member sizes vs. a 10GB hard free-space reserve).
- `.7z` (`py7zr`): same three protections, with one structural difference.
  7z's default "solid" compression shares one compressed block across many
  files, so `FileInfo.compressed` is `None` for most members — only
  (typically) the first file in a block gets a real per-file value. The
  expansion-ratio check is therefore done at the **whole-archive** level
  (total uncompressed member size vs. the `.7z` file's actual size on
  disk) rather than per-member. `.7z` also gets a check ZIP never needed:
  `py7zr` recreates real OS symlinks on extraction (`zipfile` does not),
  so a symlink member could otherwise point outside the destination —
  symlink members are rejected outright, not validated.

Both extractors return the archive's extracted contents as `DiscoveredFile`
entries; `DocumentIngestor` skips the `.zip`/`.7z` container itself from
becoming a `Document` (only its extracted contents do).

Verified against two real files from the user's actual archive corpus
(metadata/listing only via `py7zr.is_7zfile` + `.list()`, not full
extraction — one is 13.6GB): both parsed correctly, confirmed solid
LZMA2 compression, no symlink members in either.

No other archive formats (`.tar`, `.tar.gz`, `.rar`, ...) are supported yet.

## Chunking and embeddings
`EmbeddingService.embed_document(document, content)` (`app/services/embedding_service.py`):
1. Splits `content` into overlapping chunks (`app/embeddings/chunker.py`, default 1000 chars / 200 overlap).
2. Deletes any existing `DocumentChunk` rows for that document (idempotent re-embedding).
3. Calls Ollama's embed API in one batched request (`app/embeddings/client.py`) for `EMBEDDING_MODEL` (`nomic-embed-text`, `EMBEDDING_DIMENSIONS=768`).
4. Persists one `DocumentChunk` row per chunk, `(document_id, chunk_index)` unique, `embedding` as a pgvector column.

`EmbeddingService` itself takes already-extracted text `content`, not a file
path. `ImportJobService._embed_documents` (called from `execute_job`, after
ingestion) bridges the gap: for each created `Document`, it calls
`extract_text(Path(document.source))` (`app/ingestion/text_extractor.py`)
and hands the result to `EmbeddingService`, synchronously, best-effort per
document — an unextractable file or failed embed is logged and skipped,
not fatal to the job.

`extract_text` dispatches via a `{extension: extractor}` table
(`_EXTRACTORS`): `.pdf` via `pypdf` (joins each page's `extract_text()`,
so a scanned/image-only PDF with no text layer yields `""` — no crash,
just no chunks), `.docx` via `python-docx` (joins paragraph text),
`.pptx` via `python-pptx` (joins each shape's `text_frame.text` across
all slides), `.xlsx` via `openpyxl` (`read_only`+`data_only`, joins each
row's non-empty cells tab-separated across all sheets — opened as a
binary file handle rather than a path string, since unlike the other
three libraries, `openpyxl` validates the *filename's* extension
internally and would otherwise refuse a conflict-suffix-renamed file).
Everything else falls back to a plain UTF-8 read — which is what makes
true binaries (images, video, unsupported formats) raise and get
skipped, same as before. All four known extensions are matched by
prefix, not exact equality (`.pdf_1768918262` still extracts as a PDF),
to tolerate files a conflict-resolution/dedup tool renamed by appending
`_<timestamp>` with no separating dot — found for real in the user's
archive corpus (a full extension survey found ~3,552 PDFs, 595 xlsx, and
379 pptx affected in various ways). No OCR, legacy `.doc`/`.ppt`/`.xls`,
or other formats yet. Revisit sync vs. a background worker (Redis) once
import volumes get large enough that embedding noticeably slows down
`execute_job`.

## Retrieval
`RetrievalService.search(query, top_k=5)` (`app/rag/retrieval_service.py`):
1. Embeds `query` via `EmbeddingClient` (same `nomic-embed-text` model as ingestion).
2. Orders `document_chunks` by pgvector cosine distance to that embedding (`DocumentChunk.embedding.cosine_distance(...)`), joined to their source `Document`.
3. Returns the nearest `top_k` as `RetrievedChunk(chunk, document, distance)`.

Exposed via `POST /rag/search` (`{query, top_k}` → chunk content + source
document + similarity `score = 1 - distance`, `top_k` bounded 1–50). No
reranking or relevance-threshold filtering yet — it's raw nearest-neighbor
search over whatever has been embedded.

## Chat
`ChatService.send_message(content, conversation_id=None, top_k=5)` (`app/services/chat_service.py`):
1. Gets or creates the `Conversation` (a `conversation_id` for an unknown conversation raises `ValueError` → 404 at the API layer); persists the user's `Message`.
2. Calls `RetrievalService.search(content, top_k)` for relevant `document_chunks`.
3. Loads up to 50 `Memory` rows (most-recent-first) and the conversation's message history (capped at the last 20 messages), then builds an Ollama prompt: a system message (base instructions + numbered `[n]` document-context block, or a note that nothing relevant was found + a "What you know about the user" block from memory, if any) followed by the history.
4. Runs `_run_tool_loop(prompt)` (see Tool calling, below) instead of a single `chat_client.chat()` call.
5. Persists and returns the assistant `Message`, with `citations` set to a denormalized snapshot of the retrieved chunks (`document_chunk_id`, `document_id`, `document_title`, `document_source`) — captured at reply time so a citation stays legible even if that chunk is later re-embedded or deleted.

Exposed via `POST /chat` (`{message, conversation_id?, top_k?}` → `{conversation_id, message}`) and `GET /chat/{conversation_id}` (full message history). Verified end-to-end against the real `qwen3:8b` model: it correctly cites `[n]` markers for document context, states memory facts plainly (no citation marker — the system prompt explicitly scopes `[n]` citations to the numbered document list only), and uses prior turns as context on follow-up questions. Single blocking response only — no streaming yet.

## Tool calling
`ChatClient.chat(messages, tools=None)` (`app/services/chat_client.py`) returns
the raw Ollama response message (`.content` and `.tool_calls`), not just text
— this is what lets `ChatService` drive a real loop rather than a single
call. `ChatUnavailableError` (Ollama unreachable/failed) still maps to 503
at the API layer.

`ChatService._run_tool_loop(messages)`: sends `messages` plus
`ToolRegistry.to_ollama_schema()` to `chat_client.chat()`. If the reply has
no `tool_calls`, its `.content` is the final answer. Otherwise: append the
assistant's tool-call message, execute each requested call via
`ToolRegistry.call(name, arguments)`, append one `role="tool"` message per
result, and loop — capped at `MAX_TOOL_ITERATIONS` (5), after which a
graceful fallback string is returned instead of looping forever.

`ToolRegistry` (`app/tools/registry.py`): `register`/`get`/`list_tools`,
`to_ollama_schema()` (the OpenAI-style `{"type": "function", "function":
{...}}` shape Ollama expects), and `call(name, arguments)` — which never
raises, returning a `ToolCallResult(content, is_error, error, duration_ms)`
instead. `content` is always what gets fed back to the model as the tool
message (on failure, a human-readable description); `is_error`/`error`
give callers (the audit trail, below) a structured status without parsing
the content string.

`app/tools/builtin.build_default_registry(db, conversation_id=None,
on_memory_proposed=None)` registers seven tools:
- `search_knowledge_base(query, top_k=5)` — read-only, explicit on-demand
  `RetrievalService` search, letting the model search multiple times
  with different queries within one turn, distinct from the always-on
  context already injected into the prompt.
- `get_current_datetime()` — read-only, UTC, ISO 8601.
- `list_recent_documents(limit=10)` — read-only, wraps
  `DocumentService.list_documents`.
- `find_duplicate_documents(scope="exact"|"near")` — read-only, wraps
  `DeduplicationService.find_exact_duplicates`/`find_near_duplicate_documents`
  (see Deduplication, below). Reports duplicates; never acts on them.
- `plan_duplicate_cleanup(limit=10)` — dry-run only, wraps
  `DeduplicationService.plan_exact_duplicate_cleanup` (Best Copy
  Arbitration — see Deduplication, below). Returns a proposed keep/delete
  plan as text; computing the plan has zero effect on any file or row.
- `read_file_content(path)` — the one tool with real filesystem access,
  wraps `FileAccessService.read_file` (see File operations, below).
  Read-only and scoped: refuses any path outside an already-ingested
  import source directory.
- `remember(content, confidence=None)` — the one write path, and even it
  never writes something live: see Memory, below, for the review-gate
  design (`Memory.status`) that makes this safe.

`conversation_id`/`on_memory_proposed` exist solely for `remember`'s
provenance — see Memory for how `ChatService` supplies them.

Exposed via `GET /tools`. Before building around `qwen3:8b`'s tool
support, confirmed via `ollama /api/show` that it actually advertises
the `tools` capability. Verified end-to-end: asked the real model for
the current date/time and got back a minute-precise answer only
obtainable by actually calling `get_current_datetime` (the model's
training data doesn't include 2026).

### File operations
`read_file_content` is the only tool with real filesystem access, and
was deliberately deferred until its own explicit scoping conversation
(everything else built so far only ever touches rows already inside
Postgres — a real file read is a materially bigger security surface).
That conversation settled three things before any code was written:
read-only only for this milestone; a path may only be read if it falls
under the `source_path` of a COMPLETED `ImportJob` (never an arbitrary
filesystem path, and never the wider personal-data corpus or a "master
backup" that hasn't been explicitly imported — see
`docs/decisions/0002-master-backup-is-read-only.md`); and if a
write/delete capability is ever added later, it must require a
separate human-confirmation step the model itself cannot trigger — a
`delete_file` tool the model can call directly is explicitly off the
table.

`FileAccessService.read_file(path)` (`app/files/service.py`)
implements exactly that: resolves the requested path
(`Path.resolve()`), checks it via `is_relative_to()` against every
COMPLETED import job's resolved `source_path` (the same path-traversal
pattern `ArchiveExtractor` already uses for `..`-escaping archive
members), reuses `text_extractor.extract_text()` so PDF/DOCX/etc. read
as text rather than raw bytes, and truncates at `MAX_FILE_READ_LENGTH`
(20,000 characters) so one file can't flood the model's context. Raises
`FileAccessError` — never a bare filesystem exception — for every
refusal case: outside every allowed root, missing, not a regular file,
or not decodable as text.

Verified end-to-end against the real model both ways: asked it to read
a real ingested file's exact content, and the `ToolCallRecord` audit
trail showed a `SUCCESS` call whose result matched the file byte-for-
byte; separately, asked it to read `/etc/hostname` (never imported),
and the audit trail showed an `ERROR` call with the `FileAccessError`
message, which the model then correctly explained back.

### Tool-call audit trail
`ToolCallRecord` (`app/models/tool_call.py`, migration `db6a38db9323`):
one row per tool call, written inside `_run_tool_loop` immediately as the
call happens — `conversation_id` (FK), `message_id` (FK, nullable),
`tool_name`, `iteration`/`call_index` (Ollama's `tool_calls` carry no
native call id, so these — 1-based loop pass, 0-based position within
that pass — are what identify a call's position within one turn),
`arguments` (JSONB), `status` (`success`/`error`), `result` (truncated
copy), `result_truncated`, `error_message`, `duration_ms`, `created_at`.
Indexed on `conversation_id`, `message_id`, `tool_name`.

`message_id` is nullable because the call happens mid-loop, before the
final assistant `Message` exists. `ChatService._link_tool_calls_to_message`
does one bulk `UPDATE` to backfill it once that `Message` is persisted —
so a record still exists (with `message_id=NULL`) even if something later
in `send_message` fails, rather than losing the fact that the call
happened.

Two safety properties, both directly tested:
- **Best-effort persistence**: a DB failure while writing a
  `ToolCallRecord` is logged and swallowed (`_record_tool_call`), never
  propagated — audit logging must never be why a user doesn't get an
  answer.
- **Bounded storage, full model context**: the persisted `result` is
  truncated to `MAX_TOOL_RESULT_LENGTH` (4000 chars, `result_truncated`
  flags when this happened); the *full*, untruncated result is still what
  gets fed back to the model — only the audit copy is capped.

This is explicitly an audit/debug record — never queried for chat context,
RAG, or memory. No tool takes secrets as arguments, but `read_file_content`
(see File operations, below) *can* return raw file contents from the
user's own ingested data, and that result is subject to the same
`MAX_TOOL_RESULT_LENGTH` truncation and no redaction beyond it — the
audit trail will happily store whatever a read file contains up to that
cap. No redaction logic exists yet since nothing sensitive has actually
surfaced this way in practice; a future write tool, or evidence that
ingested files commonly contain secrets, would be the trigger to revisit
this before trusting the trail not to store something sensitive long-term.

Verified end-to-end against the real model and directly against Postgres:
asked it to list recently ingested documents, it called
`list_recent_documents(limit=10)` on its own, and the resulting
`ToolCallRecord` showed the correct arguments, `SUCCESS` status,
`message_id` linkage, and a 2ms `duration_ms`.

## Deduplication
`DeduplicationService` (`app/dedup/service.py`) is detection (and
dry-run planning) only — nothing in this module, or anywhere else in the
codebase, deletes, moves, or modifies a file. Three methods:

- `find_exact_duplicates(limit=100)` — groups `Document` rows sharing an
  identical `content_hash` (SHA-256 over raw file bytes, computed during
  ingestion). Zero false positives; misses re-saved/re-compressed copies
  that changed on disk without changing content.
- `find_near_duplicate_documents(similarity_threshold=0.95, limit=100)` —
  cross-joins each document's first-chunk (`chunk_index=0`) pgvector
  embedding against every other's via cosine distance. A lightweight
  proxy for whole-document similarity (first ~1000 characters only, not
  a full-document comparison — that would be O(chunk_count²) and wasn't
  needed for a first pass).
- `plan_exact_duplicate_cleanup(limit=100)` — **Best Copy Arbitration**:
  for each exact-duplicate group, sorts by `(created_at, id)` and
  proposes keeping the oldest copy, with a `DryRunAction(action="delete",
  document, reason)` for each remaining copy. Deliberately scoped to
  exact duplicates only — "highly similar" isn't "safe to discard,"
  so near-duplicate arbitration isn't automated. The arbitration rule
  itself is a simple, deterministic first pass (oldest copy wins; no
  inspection of path depth, filename quality, or file location) —
  revisit if that assumption doesn't hold up in practice. A plan is
  pure computed data returned to the caller; producing one never
  writes to the database or touches a file.

Exposed via `GET /dedup/exact`, `GET /dedup/near`, `GET /dedup/exact/plan`,
and the `find_duplicate_documents`/`plan_duplicate_cleanup` chat tools
(see Tool calling, above). Verified end-to-end: ingested two real
byte-identical files, confirmed `GET /dedup/exact/plan` proposed keeping
the earlier-ingested one, confirmed via direct Postgres query that both
documents were untouched afterward, then asked the real model for a dry
run cleanup plan — it called `plan_duplicate_cleanup`, and its answer
matched the API's keep/delete decision exactly.

This is KRM's "Dry-run mode for destructive/derived operations" item,
narrowly scoped to exact-duplicate cleanup: there is still no code path
anywhere that can actually delete a file, so "dry run" here means
"compute and return a plan," not "simulate an execution that could
otherwise happen." Executing a plan is a separate, deliberately
unbuilt milestone.

### Dedup review decision model

A design-and-model milestone, stopped deliberately before any API or
frontend work: KRM deduplication is split into three explicitly
separate stages, and until now only the first existed as anything more
than an in-memory computation.

```
Detection            DeduplicationService.find_exact_duplicates /
                      find_near_duplicate_documents - stateless,
                      re-run on demand, persists nothing.
     │
     ▼
Decision              DuplicateReview + DuplicateReviewMember
(this milestone)      (app/models/dedup_review.py, migration
                      a7ad86ac80d3) + DedupReviewService
                      (app/dedup/review_service.py) - persisted,
                      human-reviewable findings. PENDING by default.
     │
     ▼
Execution             Does not exist. No code path anywhere in
(not built)           AI_Brain deletes, moves, quarantines, or
                      renames a file. Approving a review has zero
                      effect beyond the review row itself.
```

**Why a new persisted model, not an extension of the existing dry-run
plan**: `plan_exact_duplicate_cleanup` is recomputed fresh on every
call and never persists anything - there is no way to review a
*specific* finding over time, record who decided what, or leave a
paper trail. This mirrors exactly the gap Memory's `status` field
closed for model-proposed facts (Phase 3); `DuplicateReview` is the
same review-gate pattern applied to dedup findings, per KRM's own
long-standing "Confidence Review Queue" backlog item.

**What evidence actually exists to build this on** (inspected before
designing anything): `Document` stores `content_hash` (SHA-256),
`title`, `source` (full path), `source_type`, `import_job_id`, and
`created_at`/`updated_at` - the *ingestion* timestamp, not the file's
real filesystem mtime, which is never captured anywhere in the
ingestion pipeline. There is no file-size column, no filesystem
mtime/ctime, no version/revision metadata, and no image perceptual
hash - none of that data exists in this codebase today, so none of it
is used as evidence. `DocumentChunk` stores per-chunk text and a
pgvector embedding, which is what backs the near-duplicate similarity
score. No `_DUPLICATES_QUARANTINE` convention, quarantine directory,
or quarantine flag exists anywhere in the codebase - dedup findings
have never had anywhere to go but a computed API response, until now.

**`DuplicateReview`** (`duplicate_reviews` table):
`match_type` (`exact`/`near`), `content_hash` (exact only),
`similarity` (near only, 0-1), `confidence` (0-1 - confidence this
finding **is** a genuine match, not confidence in a canonical choice),
`recommendation_reason` (human-readable, always populated),
`evidence` (JSONB - a point-in-time snapshot of each member's
title/source/source_type/content_hash/import_job_id/created_at, plus
the arbitration rule or lack thereof; snapshotted like
`Message.citations` so a review still explains itself even if a member
`Document` is later changed), `status` (`pending`/`approved`/
`rejected`, mirroring `MemoryStatus`), `reviewer_decision` (optional
free-text human note, distinct from the system's own
`recommendation_reason`), `reviewed_at`, timestamps.

**`DuplicateReviewMember`** (`duplicate_review_members`): a child table
rather than fixed `document_a_id`/`document_b_id` columns, because an
exact-duplicate group is genuinely N-way (`find_exact_duplicates`
already supports more than two documents sharing a hash) - fixed
columns would silently truncate a real 3+-way group to a pair. Each row
is one `Document`'s `role` in a finding: `proposed_canonical` (0 or 1
per review) or `duplicate` (1 or more).

**The ambiguous-candidate guarantee**: `DedupReviewService.
create_review_from_exact_group` proposes a canonical using the same
safe, deterministic rule as `plan_exact_duplicate_cleanup` (oldest
`created_at`, tie-broken by `id`) - safe because byte-equality already
proves the match; only the *choice of which byte-identical copy to
keep* is a heuristic, always presented as an overridable proposal.
`create_review_from_near_pair` **never** creates a `proposed_canonical`
member, structurally, not as a fallback - similarity is evidence these
documents are related, not evidence of which one is more complete,
correct, or current, and guessing from recency/size/ingestion-order
would be exactly the unsafe assumption this design exists to avoid. A
near-duplicate review with no proposed canonical is not a missing
recommendation; it **is** the recommendation - "a human needs to look
at this."

**Review decisions are permissive, matching `MemoryService`**:
`approve_review`/`reject_review` place no guard on a review's current
`status` - either can be called from any state, and the last call wins
(no optimistic-locking/version column, same as `Memory`). Nothing
before this needed a stricter rule, and the same future frontend
constraint applies as for Memory: action buttons would only ever be
shown for a `pending` review, so this path isn't reachable through a
UI even though the service permits it.

**Safety, verified against real Postgres, not just asserted**: creating
a review and approving/rejecting one were both confirmed, via direct
query immediately after, to leave every referenced `Document` and
`ImportJob` row completely unmodified - content_hash, status, all
fields byte-for-byte unchanged. No filesystem operation of any kind
runs anywhere in `DedupReviewService`.

**Deliberately not built this milestone**: any API endpoint (`app/api/`
has no new router), any frontend view, and - as with the exact-only
dry-run plan before it - any execution mechanism. This was a design
checkpoint: the model and service exist and are tested, but nothing
exposes them to a client yet, pending review of the design itself.

## Provenance chain
The schema already links every derived fact back toward a source file
via foreign keys - `Document.import_job_id`, `DocumentChunk.document_id`,
`Message.conversation_id`, `ToolCallRecord`/`Memory` → `message_id` - but
until now that chain was only ever implicit, never something you could
actually ask the system to walk. `ProvenanceService.trace_document`
(`app/provenance/service.py`) formalizes it as one read-only query:

- `import_job`: the `ImportJob` a `Document` came from, via
  `import_job_id` (`None` for documents created directly via
  `POST /documents`, which never had one).
- `chunk_count`: how many `DocumentChunk` rows exist for the document.
- `cited_in`: every `Message` whose denormalized `citations` (see Chat,
  above) names this document - i.e. every reply the model has actually
  given that was grounded in it.

Exposed via `GET /documents/{id}/provenance` (404 if the document
doesn't exist). Scoped to `Document` as the trace root, since it's the
one entity everything else ultimately derives from; there's no symmetric
`GET /memory/{id}/provenance` or similar yet, though `Memory` and
`ToolCallRecord` already carry the `conversation_id`/`message_id` FKs
a trace in that direction would need.

Two real bugs surfaced while building and testing this against the real
database (mocked unit tests never exercise either, since they hand-craft
objects and skip the actual Postgres round-trip):
- A JSONB column storing Python `None` serializes to a JSON `null`, not
  a SQL `NULL` - so `Message.citations.is_not(None)` at the query level
  doesn't reliably exclude citation-less messages. The service treats
  that filter as a best-effort pre-filter only and re-checks
  `message.citations` truthiness in Python before scanning it.
- Most of `tests/integration/test_import_execution.py`'s tests had never
  cleaned up after themselves against `aibrain_test` (only the one added
  for the dedup milestone did). Across many suite runs this session that
  silently piled up 160 import jobs' worth of documents and chunks with
  repeated, deterministic embeddings (same input text → same fake
  embedding), which pushed a real test pair out of dedup's near-duplicate
  query past its result `limit` and made that test fail deterministically
  - not the flaky, occasional kind of failure, but every single run,
  until the pollution was cleaned up and the tests fixed. All eight
  tests in that file now clean up via a shared `_cleanup_import_job`
  helper.

Verified end-to-end: ingested a real file via the real import pipeline,
confirmed the trace showed its import job and one chunk with an empty
citation list, asked the real model a question it could only answer by
citing that document, and confirmed the same trace then included the
real message and conversation that cited it.

## Frontend
`frontend/` (`index.html`, `style.css`, `app.js`) is plain HTML/CSS/
vanilla JS — no framework, no build step, no Node/npm toolchain. FastAPI
serves it directly: `app.mount("/", StaticFiles(directory=BASE_DIR /
"frontend", html=True))` in `app/main.py`, registered *after* every
`app.include_router(...)` call so it never shadows an API route (a
`Mount` only gets a chance to match once every earlier, more specific
route has already missed - this was verified with a dedicated test,
not just assumed). This was an explicit stack decision made before
writing any frontend code: a single-user, offline-first, one-process
personal tool doesn't need a second toolchain or a second port for its
UI.

Two views live in the same single page, toggled client-side by
`showChatView()`/`showImportJobsView()` (no client-side router, no
separate HTML files) - Chat, and a read-only Import Jobs monitor.
Memory review and dedup review remain unbuilt, each its own later slice.

### Chat

- `app.js` posts to `POST /chat` and renders `GET /chat/{id}` history
  through the same `MessageResponse` shape the API already returns
  (role, content, citations) - no separate frontend-facing schema.
- Conversation continuity across a reload lives in `localStorage`
  (`ai_brain_conversation_id`) - a page load with a stored id replays
  history via `GET /chat/{id}`; a 404 (deleted/invalid conversation) is
  treated as "start fresh," clearing the stored id automatically rather
  than getting stuck in an error state. A "New conversation" button
  clears it explicitly.
- Message content is rendered via `textContent`, never `innerHTML` -
  a message containing markup (from the model, or something a user
  pasted) can't inject into the page. Verified directly: rendering a
  `<img src=x onerror=...>` payload produces literal escaped text and
  no `<img>` element, let alone an executed handler.
- No message-send timeout is imposed client-side; a "Thinking..." state
  disables the composer until the response (or an error) arrives,
  since a real `qwen3:8b` turn with tool calls has taken over a minute
  in practice.

Verified in a real browser against the real backend and a real
`qwen3:8b` call (not just unit-tested): ingested a real file, asked a
question through the UI, and confirmed the rendered answer and its
citation matched the ingested content end to end. Also exercised
directly in the browser: reload-persistence, "New conversation" reset,
submitting an empty message being a no-op, and recovery from a stale
`localStorage` conversation id.

### Import job monitoring

Read-only monitoring over the existing `GET /import-jobs` endpoint -
no new backend endpoint was added, and no `ImportJob` business logic
(status transitions, progress calculation) is duplicated in
JavaScript; every field a job card shows (name, source path/type,
status, progress, files discovered/processed, error message, all four
timestamps) is exactly what the API already returns, via the existing
`ImportJobResponse` schema. Each of the six `ImportStatus` values gets
its own colored badge (`.status-badge.status-<lowercase-status>`); a
`FAILED` job's `error_message` renders in a dedicated block.

Deliberately no create/start/execute/retry controls in the UI - this
is a monitor, not a management console. Creating and running import
jobs remains API/curl-only, consistent with the "monitoring must
remain read-only" scoping decision made before writing any code for
this slice.

Polling (`fetchImportJobs`): refetches every `IMPORT_JOBS_POLL_INTERVAL_MS`
(5s) only while at least one returned job is non-terminal
(`TERMINAL_IMPORT_STATUSES = {COMPLETED, FAILED, CANCELLED}`), and only
while the Import Jobs view is the one currently visible - switching to
Chat calls `stopImportJobsPolling()` immediately, and a fetch that
resolves after the user has already switched away checks
`importJobsViewEl.hidden` before scheduling its next tick, so a
poll in flight at the moment of switching can't leak into a background
loop. A manual "Refresh" button works regardless of polling state (it
cancels any pending timer and re-fetches immediately).

Verified end-to-end in a real browser against the real database: created
real jobs in PENDING, COMPLETED, and FAILED states and confirmed each
rendered correctly (including the FAILED job's error text); executed the
PENDING job via a separate `curl` call (simulating an external actor,
the way a background worker eventually would) and watched the UI update
itself on its next poll tick with no manual action - confirmed via the
browser's network log that request volume stayed flat while on the Chat
view and only resumed on switching back. Also reconfirmed chat
(including citations and reload-persistence) is unaffected by this
addition.

A real bug was found during this verification: `POST
/import-jobs/{id}/execute` returned a bare HTTP 500 when the source
path didn't exist, since the job row was always correctly `FAILED`
regardless (confirmed via `GET /import-jobs/{id}` immediately after),
so it never actually affected this read-only monitor - it never calls
`/execute`. Fixed as its own follow-up commit rather than folded into
this one; see "Import job execute error handling," under Import job
lifecycle, below.

## Memory
`Memory` (`app/models/memory.py`): `content` (the fact/preference itself),
optional `confidence` (0–1), `status` (`pending`/`approved`/`rejected`,
migration `bb3be4e51d72`), optional provenance (`conversation_id`/
`message_id` it was derived from — null for anything written directly via
the API). No `memory_type` taxonomy (episodic/semantic/preference/etc.) —
the original project brief explicitly said not to invent one until
something in the actual implementation needs it, and nothing does yet.
`status`, by contrast, is a lifecycle field the trust concern below
directly justified.

`MemoryService` (`app/memory/service.py`): `create_memory` (human-authored
via the API, always `APPROVED` — typing "remember this" is already the
confirmation step), `propose_memory` (model-authored via the `remember`
tool, always `PENDING`), `get_memory`, `list_memories` (most-recent-first,
capped, optional `status` filter), `approve_memory`/`reject_memory` (raise
`ValueError` → 404 if missing; a rejected row is kept, not deleted, as a
record of what was proposed and rejected), `delete_memory` (actually
removes a row). Exposed via `POST /memory`, `GET /memory?status=`,
`POST /memory/{id}/approve`, `POST /memory/{id}/reject`,
`DELETE /memory/{id}`.

**Read hook**: every chat turn loads up to 50 memories via
`list_memories(status=APPROVED)` and injects them into the prompt (see
Chat, above) — a `PENDING` proposal is structurally excluded, not just
by convention.

**Write hook**: the `remember` tool (`app/tools/builtin.py`) lets the
model propose a memory, but `propose_memory` always writes `PENDING` — a
model's own claim of confidence isn't independently verified, so nothing
it proposes takes effect until a human calls `/approve` or `/reject`. This
is the review-gate design the trust concern called for (echoing the KRM
backlog's "Confidence Review Queue"), not a default-on side effect of
chatting. A `remember` call also produces its own `ToolCallRecord` like
any other tool call.

Provenance for a proposed memory works the same way as tool calls: the
`remember` tool doesn't know the eventual assistant `Message`'s id (it
doesn't exist yet mid-loop), so `ChatService` rebuilds a fresh, per-turn
`ToolRegistry` scoped to the current `conversation_id` (via
`build_default_registry(db, conversation_id, on_memory_proposed)`) whenever
the caller hasn't supplied its own registry, and backfills `message_id`
via `_link_memories_to_message` once the `Message` is persisted — mirroring
`_link_tool_calls_to_message`. Persisting a proposal is best-effort, same
guarantee as the audit trail: a DB failure while writing a `Memory` row
can't break the chat turn (it surfaces as a failed tool call instead,
which the model sees and can react to).

Verified end-to-end against the real model through the full review cycle:
asked it to remember a fact → confirmed `GET /memory?status=pending`
showed it → confirmed a **genuinely separate** new conversation had no
knowledge of it (the actual property this design protects) → approved it
→ confirmed a new conversation afterward answered correctly using it. (A
same-conversation follow-up *did* reference the still-pending fact, but
that's the model reading ordinary conversation history — the user's own
prior message — not the memory system; the read-hook only governs what
gets injected as a remembered fact for turns/conversations that don't
already have it in their history.)

Memory retrieval is "most recent N", not similarity-ranked like document
retrieval; revisit if the store grows large enough that recency stops
being a good proxy for relevance.

`approve_memory`/`reject_memory` place no guard on the memory's current
`status` - either can be called regardless of what it currently is, and
whichever call lands last wins (there's no optimistic-locking/version
column). This is deliberate-by-omission rather than a bug: nothing in
the service or API layer has ever needed a stricter state machine, and
the one caller that matters - the frontend review queue, below - never
exposes a path to re-review something already decided (Approve/Reject
only render for a `pending` memory). Documented and tested at both the
service and real-HTTP level rather than changed.

### Memory Review view (frontend)
A third frontend view (`nav-memory-btn`, alongside Chat and Import
Jobs), reusing `GET /memory?status=`, `POST /memory/{id}/approve`, and
`POST /memory/{id}/reject` verbatim - no new backend endpoints, no
memory business logic (status transitions, scoring) duplicated in
JavaScript. Filter tabs (Pending/Approved/Rejected/All) map directly to
the existing `status` query parameter; Approve/Reject buttons render
only for `pending` memories, so the one-way candidate → review →
approved/rejected flow is a frontend-level restriction, not a backend
one (see above).

Provenance display: `conversation_id`/`message_id` are always shown as
plain references (free - no extra fetch). When a `message_id` exists,
an on-demand "View source message" toggle fetches the *existing*
`GET /chat/{conversation_id}` endpoint and finds the matching message
client-side, showing a truncated (300-char) snippet of its content plus
any citations - the same citation shape already used in Chat. This
reuses an existing endpoint rather than adding a new single-message
one; the trade-off (fetching a whole conversation's history to find one
message) is called out in the Roadmap as something to revisit only if
it becomes a real cost - personal-scale conversation lengths make it a
non-issue today, and it's fetched on demand, not eagerly for every
listed candidate.

Confirmation: Approve/Reject use a two-click in-page pattern (`createConfirmableActionButton`
in `app.js`) - first click arms the button ("Confirm Approve?"), a
second click within 4 seconds performs the action, anything else
disarms it. This replaced an initial native `window.confirm()` design:
browser verification for this milestone found that native JS dialogs
aren't reliably interactable through browser automation tooling, and
they're visually inconsistent with the rest of the app regardless - the
in-page pattern is more robust and no less explicit a confirmation.
There is no bulk-approve/bulk-reject control anywhere in the UI.

Verified end-to-end in a real browser against the real database and a
real chat turn: asked the real `qwen3:8b` model to remember a synthetic
test fact, confirmed the resulting `PENDING` memory appeared with
correct provenance - including its `message_id` starting `null` and
correctly backfilling once the turn fully completed, the same timing
behavior described above - approved one candidate and rejected another,
confirmed both moved to the correct filter tab afterward, confirmed via
direct query that the source `Message`/`Conversation` rows they were
derived from were completely unmodified by either action, and
reconfirmed Chat (with citations) and Import Job Monitoring both still
work unaffected by this addition.

## Import job lifecycle
`ImportJob.status` is a state machine (`app/models/import_job.py`):

```
PENDING ──execute_job──▶ RUNNING ──mark_completed──▶ COMPLETED
   ▲                        │
   └────mark_running────────┘ (retry after FAILED)
                             │
                             ▼
                           FAILED
```
`mark_running` only accepts jobs in `PENDING` or `FAILED` (retry). `mark_completed`
only accepts jobs in `RUNNING`. `execute_job` refuses jobs already `RUNNING`,
`PAUSED`, `COMPLETED`, or `CANCELLED`. Invalid transitions raise `ValueError`
and are surfaced as 409 Conflict at the API layer.

### Import job execute error handling
`ImportJobService.execute_job` wraps the actual ingestion attempt in a
broad `except Exception`: on any failure it persists the job as
`FAILED` with `error_message = str(exc)` and `finished_at` set, then
re-raises the *original* exception (not a `ValueError`) so a caller
using the service directly (e.g. a future background worker) sees the
real exception type and can log or retry appropriately.

The `POST /import-jobs/{id}/execute` API handler (`app/api/import_jobs.py`)
used to only catch `ValueError` (mapped to 404/409 via
`_raise_for_value_error`), so any other exception - most commonly
`FileNotFoundError`/`NotADirectoryError` from `SourceScanner` for a
missing or invalid `source_path` - fell through as a bare, unhandled
HTTP 500, even though the job row was already correctly `FAILED` in
the database by that point. A missing source path is an expected
outcome of attempting to execute a job, not an unexpected server
error, so the handler now also catches the general `Exception` case
(after the `ValueError` branch) and returns `422 Unprocessable
Content` with `detail=str(exc)` - the exact same string already stored
in the job's `error_message`, so the two are always consistent and
nothing beyond that message (no traceback, no internals) is exposed.
`ValueError`-based 404/409 responses and the 200 success path are
unchanged.

## On-disk layout
- `documents/imports/<job_id>/` — working copies produced by ingestion for a
  given import job. Derived, disposable, safe to delete and re-ingest.
- `knowledge/` — reserved for curated/derived knowledge artifacts (future).
- `postgres/`, `redis/` — data directories for the Dockerized services.
- `logs/`, `config/`, `prompts/`, `scripts/` — reserved, currently empty.

## Open/planned areas
- `docker-compose.yml` is currently empty — services (Postgres, Redis,
  Ollama) are expected to run some other way for now (e.g. local
  installs) until Compose is filled in.
- The `vector` Postgres extension (`postgresql-16-pgvector`) and the
  `document_chunks` migration are applied on this host, but neither step
  is automated yet — a fresh machine needs both done manually before
  `EmbeddingService` will work.
- Frontend covers chat, read-only import job monitoring, and memory
  review (see Frontend, above) — no dedup review UI yet, and no
  job-creation form (creating/running import jobs stays API/curl-only);
  those remain future frontend slices.
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
