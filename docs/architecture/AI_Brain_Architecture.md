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
   GET /import-jobs, GET /memory + approve|reject, GET /dedup/reviews +
   approve|reject — chat, read-only import job monitor, human memory
   review queue, and human dedup review queue, no build step
   [implemented]
```

## Backend module layout (`backend/app/`)

| Module | Status | Responsibility |
|---|---|---|
| `ingestion/` | Implemented | `SourceScanner` walks a source dir; `ArchiveExtractor` safely expands `.zip` and `.7z` archives (bomb/path-traversal/disk-space guarded — see below); `DocumentIngestor` orchestrates scan → extract → persist, and computes `Document.content_hash` (SHA-256, streamed) for exact-duplicate detection; `text_extractor.extract_text` pulls plain text out of `.pdf`/`.docx`/anything-UTF-8-decodable, used before embedding. |
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`, `Conversation`, `Message`, `Memory`, `ToolCallRecord`, `DuplicateReview`, `DuplicateReviewMember`, `DedupExecutionPlan`, `DedupExecutionPlanAction`, `DiscoveryRun`, `ClassificationRun`, `ContentIdentityGroup`, `SourceInstance`, `ProvenanceLink`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it (Chain 1 only); `Document.content_identity_group_id` links it to its `ContentIdentityGroup` (Chain 2 only — see `classification/`, below). Both columns are independently nullable; a given `Document` row is populated by exactly one of the two chains, never both, but nothing in the schema enforces this — see "Scaled Real-T7 Ingestion — Milestones 7–12" for the currently-undecided relationship between them. |
| `classification/` | Implemented — full scaled batch ingestion pipeline (Implementation Milestones 1–12), reachable today only via `scripts/run_ingestion_batch.py` (no API endpoint, no scheduler) | `DiscoveryRunService`, `ClassificationRunService`, `ContentIdentityService` (`get_or_create_group` — the database-concurrency-safe claim for a `ContentIdentityGroup`; `assign_content_identity` — write-once `SourceInstance.content_identity_group_id`), `SourceInstanceService`, `CanonicalDecisionService`, `BatchCreationService`/`deterministic_selector`/`policy_evaluator`, `BatchResourceGuard`/`BatchControlService`, `WorkerClaimService`, `ArchiveProcessingService`, `IdentityResolutionService`, `NormalizationService`, `ChunkingService`, `PipelineEmbeddingService`, `BatchReportService`, `BatchCompletionReconciliationService`, and `BatchOrchestratorService` (`run_once()` — the pipeline's own coordinator). `NormalizationService`/`ChunkingService`/`PipelineEmbeddingService` create and populate `Document`/`DocumentChunk` rows directly — this is a second, independent ingestion path into those same tables, alongside `ImportJobService` (Chain 1). See "Scaled Real-T7 Ingestion — Milestones 7–12," below, for the full picture, including the currently-undecided relationship between the two paths. No T7 access anywhere in this module beyond what a separately-authorized real-T7 batch explicitly grants. |
| `services/` | Implemented | `DocumentService` (persistence, `list_documents`), `ImportJobService` (job lifecycle, enforced state transitions, embeds documents synchronously during `execute_job`), `EmbeddingService` (chunk + embed + persist a document's text), `ChatService`/`ChatClient` (RAG-augmented chat over Ollama, conversation persistence, memory injection, tool-calling loop). |
| `api/` | Implemented | FastAPI routers: health, version, database, documents, import_jobs, rag, chat, memory, tools, dedup. |
| `db/` | Implemented | Session/engine setup, health checks. |
| `core/` | Implemented | `Settings` (env-driven config: `DATABASE_URL`, `INGESTION_DIR`, `OLLAMA_HOST`, `CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, ...). |
| `embeddings/` | Implemented | `chunker.chunk_text` (character-bounded, overlapping chunks); `client.EmbeddingClient` wraps Ollama's `embed` API for `nomic-embed-text`. |
| `rag/` | Implemented | `retrieval_service.RetrievalService.search(query, top_k)`: embeds the query, ranks `document_chunks` by pgvector cosine distance, joined to source `Document`. Exposed via `POST /rag/search`. No reranking/relevance filtering beyond raw distance yet. |
| `memory/` | Implemented | `service.MemoryService`: flat `content` + optional `confidence`/provenance store + `status` (pending/approved/rejected), no type taxonomy. `POST /memory`, `GET /memory?status=`, `POST /memory/{id}/approve`\|`/reject`, `DELETE /memory/{id}`. Review-gated write hook (`remember` tool) + read hook (approved-only) into `ChatService` — see Memory, below. |
| `tools/` | Implemented | `registry.Tool`/`ToolRegistry` (dispatch by name, `call()` never raises, returns a structured `ToolCallResult`). `builtin.build_default_registry`: `search_knowledge_base`, `get_current_datetime`, `list_recent_documents`, `find_duplicate_documents`, `plan_duplicate_cleanup`, `read_file_content` (all read-only/dry-run), plus `remember` (proposes a `Memory`, never writes one live — see Memory, below). Exposed via `GET /tools`; driven by `ChatService._run_tool_loop`, which persists a `ToolCallRecord` audit row per call. No write/move/delete or external-network tools exist. |
| `files/` | Implemented | `service.FileAccessService.read_file(path)`: the one tool with real filesystem access, scoped to paths under a COMPLETED `ImportJob.source_path` (path-traversal-safe via `Path.resolve()` + `is_relative_to`, same pattern as `ArchiveExtractor`). Reuses `ingestion.text_extractor.extract_text`; truncates at `MAX_FILE_READ_LENGTH`. Read-only — no write, move, or delete capability. Exposed via the `read_file_content` tool. |
| `dedup/` | Implemented (detection + dry-run recommendation plan + review decision layer, with API and frontend + dry-run execution planning layer, API only) | `service.DeduplicationService`: `find_exact_duplicates` (groups by `Document.content_hash`), `find_near_duplicate_documents` (pgvector cosine similarity on first-chunk embeddings), `plan_exact_duplicate_cleanup` (Best Copy Arbitration + dry-run plan: keeps the oldest copy per exact-duplicate group by `created_at`/`id`, proposes deleting the rest — computing a plan never deletes, moves, or quarantines anything). Exposed via `GET /dedup/exact`\|`/near`\|`/exact/plan` and the `find_duplicate_documents`/`plan_duplicate_cleanup` tools. `review_service.DedupReviewService`: persists a detected finding as a `DuplicateReview` (see Deduplication, below) and records human approve/reject decisions. Exposed via `GET /dedup/reviews`\|`/{id}` and `POST /dedup/reviews/{id}/approve`\|`/reject`, and the Dedup Review frontend view. `execution_plan_service.DedupExecutionPlanService`: turns an approved review into an immutable `DedupExecutionPlan` snapshot (real file hash/size observed at generation time) and re-verifies it against the live filesystem/Document rows on demand (see "Dry-run execution planning layer," below). Exposed via `POST /dedup/reviews/{id}/plans`, `GET /dedup/plans`\|`/{id}`\|`/{id}/validity` (`app/api/dedup_execution_plans.py`) — no frontend yet, no execution endpoint anywhere. |
| `provenance/` | Implemented | `service.ProvenanceService.trace_document(document_id)`: walks the existing FK chain (`ImportJob` → `Document.import_job_id` → `DocumentChunk.document_id`, plus every `Message` whose denormalized `citations` names the document) into one queryable trace. Read-only, no new source of truth. Exposed via `GET /documents/{id}/provenance`. |
| `frontend/` (repo root, not under `backend/app/`) | Implemented (chat + import job monitor + memory review + dedup review) | Plain `index.html`/`style.css`/`app.js`, no build step, no framework. Mounted by FastAPI at `"/"` via `StaticFiles(..., html=True)` (see Frontend, below), so the whole app is one process on one port. Four views toggled client-side: Chat, Import Jobs (read-only, polls `GET /import-jobs`), Memory Review (human approve/reject queue over `GET /memory?status=`), and Dedup Review (human approve/reject queue over `GET /dedup/reviews?status=`). |

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

KRM deduplication is split into three explicitly separate stages.
Detection existed before this pair of milestones as a stateless
computation; these two milestones added the DECISION stage (data model
first, then its API and frontend) - EXECUTION still does not exist.

```
Detection            DeduplicationService.find_exact_duplicates /
                      find_near_duplicate_documents - stateless,
                      re-run on demand, persists nothing.
     │
     ▼
Decision              DuplicateReview + DuplicateReviewMember
(these milestones)    (app/models/dedup_review.py, migrations
                      a7ad86ac80d3 + 6af80a472ca2) + DedupReviewService
                      (app/dedup/review_service.py) + GET/POST
                      /dedup/reviews... + the Dedup Review frontend view.
                      PENDING by default; a human decides via the API
                      or UI.
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
`Document` is later changed - deliberately metadata only, never the
document's actual content, so a review stays auditable without
duplicating source text unnecessarily), `status` (`pending`/`approved`/
`rejected`, mirroring `MemoryStatus`), `reviewer_decision` (optional
free-text human note, distinct from the system's own
`recommendation_reason`), `human_selected_canonical_document_id`
(FK to `documents`, nullable - see below), `reviewed_at`, timestamps.

**`DuplicateReviewMember`** (`duplicate_review_members`): a child table
rather than fixed `document_a_id`/`document_b_id` columns, because an
exact-duplicate group is genuinely N-way (`find_exact_duplicates`
already supports more than two documents sharing a hash) - fixed
columns would silently truncate a real 3+-way group to a pair. Each row
is one `Document`'s `role` in a finding: `recommended_canonical` (0 or
1 per review; renamed from `proposed_canonical` via migration
`6af80a472ca2` for clarity - a Postgres enum-label rename, not a new
column, via `ALTER TYPE ... RENAME VALUE`) or `duplicate` (1 or more).

**The ambiguous-candidate guarantee**: `DedupReviewService.
create_review_from_exact_group` recommends a canonical using the same
safe, deterministic rule as `plan_exact_duplicate_cleanup` (oldest
`created_at`, tie-broken by `id`) - safe because byte-equality already
proves the match; only the *choice of which byte-identical copy to
keep* is a heuristic, always presented as an overridable recommendation.
`create_review_from_near_pair` **never** creates a `recommended_canonical`
member, structurally, not as a fallback - similarity is evidence these
documents are related, not evidence of which one is more complete,
correct, or current, and guessing from recency/size/ingestion-order
would be exactly the unsafe assumption this design exists to avoid. A
near-duplicate review with no recommended canonical is not a missing
recommendation; it **is** the recommendation - "a human needs to look
at this."

**Recommended canonical vs. human-approved canonical - kept explicit
everywhere, never conflated**: a `RECOMMENDED_CANONICAL` member is only
ever the system's suggestion, computed once at review-creation time and
never updated afterward.
`DuplicateReview.human_selected_canonical_document_id` is the *only*
field representing an actual decision, and `approve_review` never
copies the recommendation into it automatically - not even when a
human simply agrees. For an EXACT review, `canonical_document_id` is a
**required** argument to `approve_review` (validated as an actual
member of the review, or rejected with a clear error); omitting it
raises rather than silently defaulting to the recommendation. For a
NEAR review it's **optional** - a human may explicitly name a canonical
(still validated against membership), or approve with none at all,
which is itself a valid, deliberate outcome ("confirmed as related, no
canonical chosen") rather than an incomplete one. The API surfaces both
concepts side by side as `recommended_canonical_document_id` and
`human_selected_canonical_document_id` on every `DuplicateReviewResponse`,
so the distinction is visible in the data itself, not just in code
comments.

**Review decisions are stricter than `MemoryService`'s precedent, on
purpose**: `approve_review`/`reject_review` require the review to be
`PENDING` - reviewing an already-decided review raises `ValueError`
("...has already been reviewed..."), mapped to `409 Conflict` at the
API layer, rather than silently overwriting the prior decision the way
`Memory.status` permits. This is a deliberate divergence: Memory's
permissive last-write-wins design was fine because nothing downstream
depends on a single, stable, non-overwritable decision, but dedup
review is the stage that will eventually gate a real filesystem
action, and "do not silently allow inconsistent decisions" was an
explicit requirement here. `approve_review` also validates that the
review actually has members before proceeding (defensive - the service
always creates them atomically, so this should be unreachable in
practice, but a review can never be "approved" into an inconsistent
state either way).

**API** (`app/api/dedup_reviews.py`, prefix `/dedup/reviews`):
`GET /dedup/reviews` (optional `?status=`, same enum-typed query-param
pattern as `GET /memory`), `GET /dedup/reviews/{id}`, `POST
/dedup/reviews/{id}/approve` (body: optional `canonical_document_id`,
optional `reviewer_decision`), `POST /dedup/reviews/{id}/reject` (body:
optional `reviewer_decision`). Every response embeds each member's full
`Document` via `DedupReviewService.get_review_members_with_documents` -
a separate explicit query joining `DuplicateReviewMember` to `Document`
(matching this codebase's established convention of joining in the
service layer, as `ProvenanceService.trace_document` already does,
rather than relying on an ORM relationship for that join). Error
mapping extends the existing "not found → 404, else → 409" convention
(`app/api/import_jobs.py`, `app/api/memory.py`) with a third case:
an invalid or missing canonical selection is a client input problem,
not a state conflict, so it gets `422` instead of `409`. No create/sync
endpoint exists - a review only ever comes from
`DedupReviewService.create_review_from_exact_group`/
`create_review_from_near_pair` being called directly (e.g. from a
future scheduled detection pass); the API only ever lists, views, and
records decisions on reviews that already exist.

**Frontend** (`nav-dedup-review-btn`, the fourth view alongside Chat,
Import Jobs, and Memory Review): same filter-tab/card pattern as Memory
Review (`Pending`/`Approved`/`Rejected`/`All` mapped straight to the
`status` query param), plus a persistent safety banner - "Reviewing
this finding does not modify your files" - and no delete/move/
quarantine control anywhere in the view or the API it calls. EXACT and
NEAR findings are visually distinguished by a colored match-type badge
(`.match-type-exact` purple, `.match-type-near` orange) in addition to
the differing recommendation text. Approve/Reject use the same in-page
two-click confirmation pattern introduced for Memory Review
(`createConfirmableActionButton`). A canonical-choice radio group is
built per review: for EXACT it has no default selection and the
Approve button starts disabled, only enabling once a real member is
chosen (mirroring the backend's required-canonical validation exactly
in the UI); for NEAR it defaults to a pre-selected "No canonical
(undecided)" option, since approving without one is the expected,
common case for an ambiguous finding.

**Safety, verified against real Postgres and a real browser, not just
asserted**: creating a review, and both approving (with an explicit
canonical) and rejecting one, were confirmed via direct query
immediately after to leave every referenced `Document` and `ImportJob`
row completely unmodified - content_hash, status, all fields
byte-for-byte unchanged. The same was reconfirmed in a live browser
session against controlled synthetic findings. No filesystem operation
of any kind runs anywhere in `DedupReviewService` or
`app/api/dedup_reviews.py`.

**Still deliberately not built (at the point above)**: any execution
mechanism. There was still no code path anywhere in AI_Brain that
deletes, moves, renames, quarantines, or overwrites a file - approving
a review only ever recorded that a human made a decision. The next
milestone (below) adds the DRY-RUN EXECUTION PLAN stage strictly
between approval and any future execution - it still does not add an
execution mechanism.

### Dry-run execution planning layer

The full pipeline, with this milestone's addition in context:

```
Detection -> Recommendation -> Human Review -> Approval
    -> Dry-run Execution Plan
    -> Explicit Execution Authorization
    -> Filesystem Execution   (not built)
    -> Verification   (not built)
```

**Why a new service, not an extension of `plan_exact_duplicate_cleanup`
or `DedupReviewService`**: `DeduplicationService.plan_exact_duplicate_cleanup`
operates on raw, un-reviewed detection output - the Recommendation
stage, stateless, with no concept of a human decision at all. This
milestone's input is categorically different: an already-**APPROVED**
`DuplicateReview` carrying an explicit `human_selected_canonical_document_id`
- the Approval stage. Extending the former to also read post-approval
state would collapse two stages this architecture deliberately keeps
apart (a detection heuristic and a human decision are not the same
kind of fact). `DedupReviewService` was reused as-is, unmodified, as
the boundary for reading review/member state - no dedup-review logic
was duplicated.

**`DedupExecutionPlan`** (`dedup_execution_plans` table): `review_id`
(FK), `canonical_document_id` (a frozen copy of the authorizing
review's `human_selected_canonical_document_id` at generation time -
copied rather than only reachable via the review, so a plan is
self-describing even though, by construction, that field can never
change on an already-approved review anyway), the canonical's own
observed state (`canonical_source_path`, `canonical_observed_exists`,
`canonical_observed_content_hash`, `canonical_observed_file_size`) all
stored directly on the plan since a plan has exactly one canonical,
`status` (an enum with exactly one reachable value, `generated` -
`EXECUTED`/`ABORTED` would only mean something once an executor
exists, and inventing them now would document a lifecycle this
codebase can't enter; extending the enum later is an ordinary
migration), `created_at`. Deliberately no `updated_at`: a plan row is
never mutated after insert - every regeneration is a brand-new row,
the same non-overwriting convention `ToolCallRecord`/`Memory` already
follow.

**`DedupExecutionPlanAction`** (`dedup_execution_plan_actions`): one
row per non-canonical member - `document_id`, `action` (an enum with
exactly one value, `delete` - the only action type this system can
ever propose, because no move/rename/quarantine capability exists
anywhere in AI_Brain; adding the enum value without the capability
would imply a promise this codebase doesn't keep), `source_path`,
`target_document_id`/`target_path` (the canonical, duplicated onto
every action row for the same self-description reason as above),
`observed_exists`/`observed_content_hash`/`observed_file_size` (this
row's own snapshot - deliberately never the whole file, just enough
metadata to re-identify and re-verify it later), and `reason` (why
*this specific file*, distinct from `DuplicateReview.recommendation_reason`,
which explains the finding as a whole).

**Generation reads files, but never writes anything to disk**:
`generate_plan_for_review` computes each member's current SHA-256 and
size via a plain streamed read (a small local `_observe_file` helper -
deliberately not a reuse of `DocumentIngestor`'s private `_hash_file`,
which swallows every failure into a bare `None` and never reports size
or existence separately; staleness comparison needs all three
distinguished). It raises `ValueError` for every case where a plan
cannot honestly be produced: review missing (404), not `APPROVED` (409
- a genuine state conflict, same class as an already-reviewed review),
approved with no `human_selected_canonical_document_id` (422 - **the
core near-duplicate safety rule**: a near-duplicate review approved
without picking a canonical does not carry a decision sufficient to
plan from, and a canonical is never inferred to fill the gap), or a
canonical that somehow isn't one of the review's own members (422,
defensive - refuses to plan against inconsistent data rather than
guessing). A missing/unreadable file at generation time is not itself
an error, though: **updated by the "Execution Recovery &
Partial-Replanning Design" milestone** - a non-canonical member whose
file is already gone at generation time is now EXCLUDED from the plan
entirely (no action created for it at all), rather than included with
`observed_exists=false` as originally designed. See that section below
for why: an action generated against an already-missing file could
never pass `check_plan_validity`, which permanently invalidated any
*regenerated* plan that still included even one already-resolved
member. If every non-canonical member's file is already gone, no plan
can be produced at all (422) - there is nothing left to plan.

**Regeneration is a first-class, expected operation, not a singleton
per review**: every call to `generate_plan_for_review` inserts a new
row rather than updating or replacing an existing plan for the same
review - `DedupExecutionPlanService.list_plans` returns all of them,
newest first. This mirrors the audit-trail convention used everywhere
else in this codebase (never overwrite history) and sidesteps an
entire class of cache-invalidation bugs: there is no "the plan" to
keep in sync, only an append-only sequence of point-in-time snapshots.
Verified deterministic against real files: two plans generated back to
back against an unchanged file record identical hashes/sizes/paths,
while differing (correctly) in `id`.

**`check_plan_validity` re-reads everything, every time - nothing here
is ever cached or trusted from an earlier call.** Per file (the
canonical, and each action), it independently checks: the `Document`
row still exists (`document_exists` - a deleted document must not be
silently skipped from the report the way a display-only join
reasonably would be); the live `Document.source` still equals the
plan's frozen path (`path_changed` - the plan only ever reads its own
snapshot, so a document re-pointed elsewhere since generation must be
flagged, not silently followed *or* silently ignored); the file still
exists at that path (`exists_now`); its extension still matches
`Document.source_type` (`type_matches` - a near-free extra safety
dimension: an astronomically unlikely hash/size collision aside, this
catches the very real case of a path being reused for an unrelated
file); and its hash/size still match what was observed at generation
(`hash_matches`/`size_matches`). All of these collapse into one
`is_valid` a future executor must check - `false` means **abort: do
not guess, do not substitute another file, do not continue partially**,
per the design's own explicit safety requirement. Verified against
real files: editing a "duplicate" file after plan generation flips
`hash_matches`/`size_matches`/`is_valid` to false while the untouched
canonical still reads valid; deleting it flips `exists_now`/`is_valid`
to false the same way - and in both cases the check itself, confirmed
by byte-for-byte comparison before and after, touches nothing.

**API** (`app/api/dedup_execution_plans.py`): `POST
/dedup/reviews/{review_id}/plans` (generate, 201), `GET /dedup/plans`
(optional `?review_id=`), `GET /dedup/plans/{id}`, `GET
/dedup/plans/{id}/validity`. Error mapping extends the same convention
introduced for dedup reviews (not found -> 404, state conflict -> 409)
with the same third case (insufficient/inconsistent decision -> 422).
**No execution endpoint exists anywhere in this API.**

**Auditability**: a plan answers "why was this proposed" via its own
`reason` field per action, and "which review authorized this" via
`review_id`/`canonical_document_id` directly on the plan row, with no
join required back to a review whose `human_selected_canonical_document_id`
is guaranteed frozen once approved. `evidence`/`recommendation_reason`
on the parent `DuplicateReview` remain reachable via `review_id` for
the fuller "why was this a duplicate at all" story. No new persisted
audit-log table was added for verification *attempts* specifically
(calling `check_plan_validity` is a pure, stateless read) - nothing
consumes such a log yet, and adding one now would be exactly the kind
of speculative infrastructure this project avoids; it's a natural
addition once a real executor exists to require it.

**Deliberately not built this milestone**: any endpoint or mechanism
that turns a plan into a real filesystem action, any frontend view (the
spec that produced this milestone made frontend exposure explicitly
conditional - "if a plan is exposed in the UI" - not a requirement),
and any persisted execution-authorization or execution-audit record,
since none of those have anything to attach to yet. No real file was
read for anything other than computing a hash/size, and none was
written, moved, renamed, deleted, or overwritten anywhere in this
milestone.

### Explicit execution authorization layer

**Approval is not execution authorization. Authorization is not
execution.** Neither `DuplicateReview.status == APPROVED` nor
`DedupExecutionPlan.status == GENERATED` is ever treated anywhere in
this codebase as permission to touch a file. This milestone adds the
one thing that *is* such permission - and even it is not a filesystem
action: creating, listing, reading, or revoking a
`DedupPlanAuthorization` performs zero filesystem writes. There is
still no filesystem executor anywhere in AI_Brain; this layer exists to
be the thing a future executor will be required to check before acting,
not to act on anything itself.

**`DedupPlanAuthorization`** (`dedup_plan_authorizations` table):
`plan_id` (FK to `dedup_execution_plans.id`, NOT NULL - binds an
authorization to exactly one immutable plan, permanently; no method
anywhere reassigns it), `status` (an enum with exactly two reachable
values, `authorized` and `revoked` - deliberately no `executed`/`failed`
value, since no filesystem executor exists anywhere in this codebase to
ever produce that event; inventing it now would document a lifecycle
stage this system cannot enter, the same reasoning already applied to
`DedupPlanStatus` and `DedupPlanActionType`), `validity_snapshot`
(JSONB - a frozen copy of the `PlanValidity` result the pre-check
computed at the moment authorization was granted, via
`dataclasses.asdict()`; proof of what was true then, deliberately not a
duplicate of the plan or its actions, which stay reachable via
`plan_id` and never change), `authorized_by` (optional free text, this
being a single-user system with no account model - mirrors
`DuplicateReview.reviewer_decision`), `authorized_at`, `revoked_at`/
`revocation_reason` (nullable - populated only on revocation),
`created_at`, and - unlike the fully immutable `DedupExecutionPlan` -
an `updated_at`, since status *can* change (`authorized` ->
`revoked`).

**No persisted "requested"/"pending" authorization state.** The
lifecycle is: `GENERATED PLAN -> AUTHORIZATION REQUESTED (ephemeral
API call, not a row) -> AUTHORIZED -> [future: EXECUTED/FAILED, out of
scope] / REVOKED`. If any pre-check fails, `authorize_plan` raises
before ever calling `db.add` - no half-authorized row is ever written,
matching how `DedupReviewService`/`DedupExecutionPlanService` already
raise `ValueError` before persisting anything for a failed attempt.

**At most one *active* (non-revoked) authorization per plan**,
enforced at the application/service level in `authorize_plan` (a
`SELECT` for an existing `authorized`-status row for the same
`plan_id`), not as a database constraint - the same precedent already
set by `DuplicateReview`'s PENDING-only guard and `ImportJob`'s state
transitions. Attempting to authorize an already-actively-authorized
plan raises (409); revoking first and re-authorizing is the only path
to a second authorization for the same plan.

**Authorization is a single synchronous call requiring explicit
confirmation**: `AuthorizePlanRequest.confirm` must be literally `true`
in the request body - there is no default that authorizes anything from
an empty or omitted body. This satisfies "require explicit
confirmation" without a two-phase request/confirm HTTP exchange.

**Pre-check order in `DedupPlanAuthorizationService.authorize_plan`**
(every step re-fetched fresh, nothing cached or trusted from an
earlier call in this same session):
1. Fetch the plan fresh (`DedupExecutionPlanService.get_plan`) - 404 if
   missing.
2. Fetch its review fresh via `plan.review_id` - 404 if somehow missing
   (defensive; FK integrity should make this unreachable).
3. Confirm `review.status == APPROVED` - 409 if not (pending or
   rejected are both refused identically here, exactly as
   `generate_plan_for_review` already refuses them).
4. Confirm no other *active* authorization already exists for this
   plan - 409 if one does.
5. Run `DedupExecutionPlanService.check_plan_validity(plan_id)` fresh -
   the **existing** dry-run staleness check, reused verbatim, not
   duplicated - and refuse (422) unless `validity.is_valid`.

Only after all five pass is a `DedupPlanAuthorization` row created,
carrying the passing `PlanValidity` as `validity_snapshot`.

**No automatic regeneration, ever.** A stale plan is refused outright;
`authorize_plan` never regenerates, updates, or silently patches a
plan on the caller's behalf. A human must deliberately call `POST
/dedup/reviews/{review_id}/plans` again and have the new plan
independently pass its own fresh validity check.

**TOCTOU: authorization is not a permanent guarantee.** *Filesystem
state is revalidated immediately before execution* - not merely
"was valid a moment ago." `validity_snapshot` is historical proof of
what `check_plan_validity` found at authorization time; it is
explicitly **not** a promise that stays true afterward, since the
filesystem can change the instant after the call returns. The
`check_currency` method (exposed as `GET
/dedup/authorizations/{id}/currency`) makes this boundary explicit by
re-running `check_plan_validity` fresh on every call and combining it
with the authorization's current (non-frozen) `status` into one
`is_still_actionable` boolean - deliberately separate from, and able to
disagree with, the frozen snapshot. Verified against real files: an
authorization created while a file is unchanged reports
`is_still_actionable: true`; editing that file afterward flips it to
`false` on the very next `check_currency` call, while
`validity_snapshot` on the authorization itself remains exactly what it
was at authorization time. **Whoever eventually builds a filesystem
executor must still perform its own fresh `check_plan_validity`
immediately before every mutation, every time** - `check_currency`
narrows the TOCTOU window for callers who ask, but does not eliminate
it, since the filesystem can change in the instant between that
question and the next action.

**API** (`app/api/dedup_plan_authorizations.py`): `POST
/dedup/plans/{plan_id}/authorize` (create, 201, body:
`{confirm: true, authorized_by?}`), `GET /dedup/authorizations`
(optional `?plan_id=`, `?status=`), `GET /dedup/authorizations/{id}`,
`GET /dedup/authorizations/{id}/currency` (the TOCTOU check above),
`POST /dedup/authorizations/{id}/revoke` (optional body `{reason?}`).
Error mapping extends the existing convention: not found -> 404;
review not approved, an active authorization already exists, or an
authorization is already revoked -> 409; a missing/false `confirm` or a
plan that failed its fresh validity re-check -> 422. **No execution
endpoint exists anywhere in this API, and no endpoint anywhere accepts
a generic `execute=true` flag.**

**Frontend: none built this milestone**, a deliberate scope decision
consistent with the prior dry-run-planning milestone. This layer
introduces the highest-stakes concept yet exposed by this system - an
explicit, named "authorize this for future execution" action - and
building its UI without a concrete filesystem executor to authorize
*for* would be speculative. If a frontend is added once an executor
exists, it must remain read-only with respect to files, must present
authorization as "Authorize this exact plan for future execution," and
must never label it "Delete," "Clean up," or "Execute."

**Auditability**: the chain `Review -> Plan -> Authorization` is fully
reconstructable - a `DedupPlanAuthorization` reaches its plan via
`plan_id`, and the plan reaches its review via `review_id`, with
`validity_snapshot` proving what was independently re-verified at each
authorization attempt (not merely what the plan looked like at
*generation* time). `Future Execution -> Future Verification ->
Execution Audit` are explicitly out of scope: no execution-audit table
exists yet, since nothing produces execution events to audit.

**Real-filesystem verification performed**: the full 8-step protocol -
authorize a valid synthetic plan (succeeds, no file touched); modify a
planned file; authorize the now-stale plan (refused, 422); generate a
fresh plan; confirm the fresh plan captures the new file state;
authorize the fresh, valid plan (succeeds); confirm authorization
itself never modified any file - was run against the real running API
and the real `aibrain` database (via a temporary synthetic directory,
never the personal corpus), then fully cleaned up with zero leftover
rows.

**Deliberately not built this milestone**: any filesystem executor,
any endpoint that consumes an authorization to perform a real file
action, any execution-audit record (nothing exists yet to audit), and
any frontend. **Remaining work before a filesystem executor can safely
be implemented**: the executor itself (the only genuinely new
component), a per-mutation immediate revalidation call into
`check_plan_validity` (the existing method already does everything
needed; the executor must simply call it, and abort on `is_valid ==
False`, immediately before *each* file operation rather than once for
the whole plan), a persisted execution-audit table recording per-action
outcomes (this is a natural, ordinary extension once real execution
events exist to record), and a decision on what a partially-completed
execution (plan has N actions, action K fails or the process is
interrupted) means for the remaining unexecuted actions - out of scope
until an executor exists to actually raise the question.

### Execution audit model

Still no filesystem executor anywhere in AI_Brain. This milestone
prepares the architecture for one - the execution audit model, an
explicit execution state machine, and documented failure/safety
semantics - without introducing any capability that creates, deletes,
moves, renames, or modifies a real file. Every model, service method,
and API endpoint below performs zero filesystem I/O; they exist purely
to record and query facts a caller (eventually, a real executor)
reports.

```
Detection -> Recommendation -> Human Review -> Approval
    -> Dry-run Execution Plan
    -> Explicit Execution Authorization
    -> Filesystem Execution   (still not built)
    -> Verification           (still not built)
    -> Execution Audit        (this milestone - the RECORDING side)
```

**Review approval != plan generation != execution authorization !=
execution.** Each is checked fresh and independently, never inferred
from another: `DedupExecutionService.start_execution` does not trust
that an authorization's `AUTHORIZED` status still implies its plan is
valid - it re-runs `check_plan_validity` itself, the same discipline
`authorize_plan` already applies to review approval. An authorization
never automatically becomes an execution record; `start_execution`
must be called explicitly, and can still fail for a perfectly valid,
un-revoked authorization if the underlying files changed in the
meantime.

**`DedupExecution`** (`dedup_executions` table) - one row per
authorized *attempt*: `authorization_id` (FK, **UNIQUE** - see below),
`plan_id` (duplicated from the authorization for the same
self-description reason `DedupExecutionPlanAction.target_document_id`
is duplicated), `status`, `executor_identity` (optional free text -
no real executor exists to define a version convention yet),
`started_at`, `ended_at` (null while running), `failure_reason`
(derived, never caller-supplied - see below), `updated_at` (this row
*is* mutated exactly once, RUNNING -> a terminal status, unlike the
fully immutable `DedupExecutionPlan`).

**`DedupExecutionActionAudit`** (`dedup_execution_action_audits`
table) - one row per planned action *per execution*, unique on
`(execution_id, plan_action_id)`. This is where **plan vs. actual** is
made structural, not just a naming convention: `planned_action`/
`source_path`/`target_path`/`expected_content_hash`/
`expected_file_size`/`document_id` are frozen copies of the plan
action's own fields, copied by `DedupExecutionService.
record_action_result` directly from the plan action row - never
accepted as request/caller input, so nothing can describe "what was
intended" any differently than the plan itself says. `result`/
`observed_content_hash`/`observed_file_size`/
`filesystem_mutation_occurred`/`error_message`/`started_at`/`ended_at`
describe what actually happened, entirely caller-reported. Example
from the spec, represented exactly as designed: a DELETE was planned
with `expected_content_hash=ABC`; the file changed before mutation;
the row records `observed_content_hash=XYZ`, `result=
precondition_failed`, `filesystem_mutation_occurred=false` - proof
nothing was touched, sitting right next to proof of *why*. Rows are
immutable once inserted (no `updated_at`, no update method anywhere) -
a recorded outcome is a permanent historical fact, matching
`DedupExecutionPlanAction`.

**Execution state machine** (`DedupExecutionStatus`): `RUNNING` ->
one of `COMPLETED` / `FAILED` / `PARTIALLY_COMPLETED`. No persisted
"requested"/"pending" state exists before `RUNNING`: exactly like
`authorize_plan`, `start_execution` raises before ever creating a row
if any precondition fails, so a row only ever comes into existence
already running. No separate `EXECUTION_STARTED` state either - the
moment a row is created *is* the moment execution starts; there is no
real, observable interval between "started" and "running" for this
system's synchronous, one-action-at-a-time model, so persisting both
would document a distinction that can never actually be seen.
`COMPLETED`/`FAILED`/`PARTIALLY_COMPLETED` are never accepted as
caller input - `complete_execution` *derives* the outcome from the
execution's own action-audit rows: all `SUCCESS` -> `COMPLETED`; zero
`SUCCESS` among attempted/recorded actions -> `FAILED` (the very first
action already failed or hit a precondition failure - zero progress);
at least one `SUCCESS` and at least one non-`SUCCESS` -> `PARTIALLY_
COMPLETED` (progress was made, then execution stopped). This split is
introduced because it is genuinely distinguishable in the data, not
speculatively - `PARTIALLY_COMPLETED` answers a question `FAILED`
alone cannot: "did we make zero progress, or some?"

**Failure semantics: STOP on first unexpected failure, always.** The
worked example from the spec - 10 planned actions, 2 succeed, action 3
fails, actions 4-10 are never attempted - is represented as exactly 10
`DedupExecutionActionAudit` rows: 2 `SUCCESS`, 1 `FAILED`, 7
`NOT_ATTEMPTED`. `NOT_ATTEMPTED` is itself a recorded, explicit fact -
never the mere *absence* of a row - specifically so "not attempted"
can never be confused with "the audit system forgot to record this."
`complete_execution` refuses to finalize at all (422) unless every
`DedupExecutionPlanAction` belonging to the plan has exactly one
corresponding audit row: "do not claim the whole plan completed" is
enforced structurally, not just documented. Once finalized, an
execution is permanently terminal - `start_execution` refuses a second
attempt under the same authorization (see below) and
`record_action_result`/`complete_execution` both refuse to act on a
non-`RUNNING` execution.

**`DedupExecutionActionResult`** - four values, each a plausible real
outcome, none invented for a capability that doesn't exist: `SUCCESS`
(mutation occurred - today's only action type, DELETE, has no
successful no-op form, enforced by `record_action_result`), `PRECONDITION_
FAILED` (the mandatory immediately-before-mutation revalidation didn't
match - by definition no mutation was attempted, enforced the same
way), `FAILED` (the operation was attempted and the OS/filesystem
itself failed - permissions, I/O error, unrelated to staleness),
`NOT_ATTEMPTED` (execution stopped before reaching this action -
`started_at`/`ended_at` are always null, since there is no real
attempt interval to time).

**Duplicate execution prevention**: an authorization backs **at most
one execution, ever** - enforced by a `UNIQUE` constraint on
`dedup_executions.authorization_id` (verified directly against real
Postgres: a second insert for the same `authorization_id` raises an
`IntegrityError`) plus a service-level pre-check for a clean 409
before ever touching the database. This is deliberately *stricter*
than `DedupPlanAuthorization`'s own "at most one **active**
authorization per plan" rule (which permits revoke-then-reauthorize,
each pass re-validating the plan fresh): there is no retry path
through the *same* authorization for execution. A genuine second
attempt requires a brand-new authorization - which itself requires
today's authorization to be revoked first, and re-passes a fresh
`check_plan_validity`. "Do not retry indefinitely" is structural here,
not a policy note.

**`start_execution` pre-check order** (every step fresh, nothing
cached): authorization exists (404) -> authorization status is
`AUTHORIZED` right now (409) -> no `DedupExecution` already exists for
this authorization (409) -> `check_plan_validity` passes right now
(422, the **TOCTOU** check - being `AUTHORIZED` is necessary but not
sufficient, since the filesystem may have changed since authorization
was granted). No automatic regeneration: a stale plan blocks execution
outright, exactly as it already blocks authorization.

**Filesystem safety semantics for the future executor** (documented
here, not implemented): for *every* action, in order - load the
authorization and confirm it is still `AUTHORIZED`; load the immutable
plan; revalidate the relevant plan state; **revalidate the source file
immediately before mutation** (not once for the whole plan); verify
expected hash/size/path/type; perform exactly the planned operation;
verify the result; persist the action audit via `record_action_result`;
continue or stop per the failure policy above. The critical point,
repeated because it is the single most important safety property this
architecture depends on: **validating once for the entire plan and
assuming all actions remain valid is not safe** - two actions in the
same plan can be seconds apart in wall-clock time, and the filesystem
can change in that window. `check_plan_validity` (existing, reused
verbatim) already does everything a per-action revalidation needs; a
future executor's only remaining job is to call it immediately before
*each* mutation, not once at the start.

**Reversibility decision - unresolved, deliberately not implemented
this milestone**: should a future executor's DELETE mean (A) permanent
deletion, or (B) move to a controlled quarantine/staging area first?
**Recommendation: (B)** for AI_Brain's first filesystem executor -
reversibility is the safer default for a system that has, until this
point, never modified a real file, and a staging area turns an
executor bug from data loss into an inconvenience. Note explicitly:
**no `_DUPLICATES_QUARANTINE` constant, directory convention, or
quarantine mechanism exists anywhere in this codebase today** (grepped
and confirmed) - every prior mention of "quarantine" in this codebase
is a docstring/comment explicitly listing it as a capability that does
*not* exist. This decision must be made explicitly, before writing the
first line of executor code - implementing DELETE-as-permanent by
default, then trying to retrofit reversibility later, is the wrong
order.

**API** (`app/api/dedup_executions.py`): `POST
/dedup/authorizations/{id}/executions` (start, 201, body:
`{confirm: true, executor_identity?}`), `GET /dedup/executions`
(optional `?plan_id=`, `?authorization_id=`, `?status=`), `GET
/dedup/executions/{id}`, `GET /dedup/executions/{id}/actions` (list
that execution's action audits), `POST
/dedup/executions/{id}/actions` (record one action's actual outcome -
body: `plan_action_id`, `result`, and only the *observed* fields;
never the planned ones), `POST /dedup/executions/{id}/complete`
(finalize - takes no status input, derives it). Error mapping extends
the existing convention: not found -> 404; authorization not active,
an execution already exists for it, an execution already finalized, or
an action result already recorded -> 409; a stale plan, an incomplete
audit trail, a plan-action/execution plan mismatch, or a definitional
consistency violation (e.g. `SUCCESS` without a mutation) -> 422. **No
endpoint anywhere performs a filesystem action, and none accepts a
generic `execute=true` flag.**

**Frontend: none built this milestone** - same deliberate scope
decision as the two prior milestones in this pipeline.

**Auditability**: the full chain - Review -> Plan -> Authorization ->
Execution -> individual action audits - is reconstructable via foreign
keys alone (`DedupExecution.authorization_id`/`plan_id`,
`DedupExecutionActionAudit.execution_id`/`plan_action_id`), verified
directly against real Postgres. `Verification`/`Execution Audit`'s
*consuming* side (a future executor calling `check_plan_validity`
immediately before each mutation, then calling
`record_action_result` to report what it found) remains genuinely
out of scope - there is nothing yet to consume these APIs, since no
executor exists.

**Real-database verification performed**: synthetic records created
and fully exercised against real Postgres (both `aibrain_test` and, via
the real running API, `aibrain` itself) - successful/failed/partially-
completed execution representation, a precondition failure recorded
against a file deliberately changed outside any AI_Brain code path
(proving the plan-vs-actual distinction end-to-end), the
`authorization_id` and `(execution_id, plan_action_id)` unique
constraints both verified to raise real `IntegrityError`s when bypassed
at the ORM level, and confirmation - via byte-for-byte file comparison
before and after every scenario - that no file was ever created,
deleted, moved, renamed, or modified. All `aibrain` test data was
fully cleaned up afterward with zero leftover rows.

**Deliberately not built this milestone**: the filesystem executor
itself, any endpoint that performs a real file action, and any
frontend. **Remaining decisions/work before a filesystem executor can
safely be implemented**: the executor itself (the only genuinely new
component - everything it needs to call already exists:
`check_plan_validity`, `start_execution`, `record_action_result`,
`complete_execution`); the DELETE-as-permanent-vs-quarantine decision
above, made explicitly before any executor code is written; a
concrete implementation of "revalidate immediately before every
mutation" inside the executor's per-action loop (the mechanism exists;
nothing calls it in a loop yet). Crash-recovery *representation* is no
longer unmodeled - see "Executor Safety & Recovery Design" immediately
below, and "Execution Recovery & Partial-Replanning Design" further
below for the actual recovery mechanism and the re-planning-exclusion
fix. Automatic crash *detection* (something that notices a stuck
`RUNNING` execution on its own, with no human involved) remains out of
scope by deliberate choice, not merely because nothing exists yet to
crash - see that later section's point 1.

### Executor Safety & Recovery Design

A dedicated design pass, deliberately separate from building the
executor itself, addressing the single hardest correctness boundary in
this architecture: what a future executor must do (and how its outcome
must be represented) at the exact moment of a filesystem mutation,
including when that moment is interrupted by a crash. **No filesystem
mutation capability was added.** No quarantine mechanism was
implemented. No executor code (`DELETE`, move, rename, or equivalent)
exists anywhere in this codebase after this milestone, same as before
it. Two schema additions were made - `DedupExecutionActionResult.UNKNOWN`
and `DedupExecutionStatus.NEEDS_REVIEW` - specifically because without
them, the audit model built in the prior milestone could not honestly
represent "a mutation may have happened, and we cannot currently say
for certain" without either lying (claiming SUCCESS or FAILED) or
losing the fact entirely (recording nothing). Everything else in this
section is documentation of a *future* executor's required behavior,
not new capability.

#### 1. Reversible DELETE semantics

**Decision: quarantine/staging (move), not permanent deletion**, for
AI_Brain's first filesystem executor. A move gives every mistake - an
executor bug, a bad plan, a human approving the wrong canonical - a
recovery path; permanent deletion gives none. Reversibility is the
safer default for a system whose entire design, up to this point, has
never modified a real file, and should only be abandoned later for a
concrete, demonstrated reason (e.g. disk space pressure from
accumulated quarantined files), not by default.

**No such mechanism exists in this codebase today.** Grepped and
confirmed: `_DUPLICATES_QUARANTINE` does not appear anywhere in
AI_Brain. Every existing mention of "quarantine" in this codebase (in
`app/api/dedup_reviews.py`, `app/api/dedup_execution_plans.py`,
`app/dedup/execution_plan_service.py`, `app/dedup/service.py`) is a
docstring/comment explicitly listing it as a capability that does
*not* exist - this milestone does not change that.

**Destination convention (design only, not implemented)**: a
quarantine root outside the corpus tree (e.g. a sibling of
`INGESTION_DIR`, not inside `/mnt/t7ssd/vscode/data` itself - keeping
quarantined files off the path anything else in this codebase scans or
reads), with each quarantined file placed at
`<quarantine_root>/<execution_id>/<plan_action_id>__<original_basename>`.

**Collision handling**: namespacing the destination by
`execution_id`/`plan_action_id` - both already unique, already
present on every `DedupExecutionActionAudit` row - makes a filename
collision structurally impossible without any additional uniqueness
logic (no timestamp suffixes, no retry-on-collision loops, nothing
speculative). Two different plans, or two actions within the same
plan, can never target the same quarantine path.

**Atomicity**: a `rename()`-based move is atomic on POSIX *only* when
source and destination are on the same filesystem/volume; across
volumes it degrades to a non-atomic copy-then-delete, which
reintroduces exactly the partial-mutation risk this whole design
avoids (a crash mid-copy leaves two partial states, not zero or one).
**Required behavior for a future executor**: the quarantine root MUST
live on the same filesystem/volume as the corpus it quarantines from,
verified before first use, not discovered by accident. If a move ever
raises `OSError` with `errno.EXDEV` (cross-device), the executor MUST
treat that as a `FAILED` result and stop - never silently fall back to
copy-then-delete.

#### 2. Crash recovery

**What happens to a `RUNNING` execution if the process dies**:
nothing, automatically. The `DedupExecution` row simply stays
`status=RUNNING` forever - there is no timeout, no heartbeat, no
background sweep that notices a stuck execution and reclassifies it.
This is deliberate, not an oversight: a time-based "this has been
RUNNING too long, assume it failed" heuristic would be exactly the
kind of guess this architecture exists to refuse to make. Detecting a
stuck `RUNNING` execution (e.g. by `started_at` age, or by checking
whether a matching process is still alive) is an operational/
monitoring concern for whoever eventually builds the executor, not a
data-model concern - no such detection is implemented or assumed here.

**How an interrupted action is represented - three distinguishable
cases**:
- **Mutation definitely did not happen**: the action was never
  attempted, or a precondition check correctly stopped it first. No
  new representation needed - `NOT_ATTEMPTED` and `PRECONDITION_FAILED`
  (both already `filesystem_mutation_occurred=False`) already say this.
- **Mutation definitely happened**: the executor observed a definite
  outcome and reported it. Already covered by `SUCCESS`
  (`filesystem_mutation_occurred=True`) or `FAILED` (either value,
  since a live executor's own definite report is what's trusted).
- **Mutation outcome is unknown**: the process crashed between
  attempting (or performing) the mutation and persisting its outcome -
  *this* is the case with no prior representation, and the reason
  `DedupExecutionActionResult.UNKNOWN` was added this milestone. See
  its docstring (`app/models/dedup_execution.py`) for the exact
  contract.

**`UNKNOWN` is never written by a live in-progress executor** - by the
time a process crashes, there is no live caller left to write
anything. An `UNKNOWN` row can only ever be written *after the fact*,
by a future recovery step that has independently concluded an
action's fate cannot be confirmed (or has chosen not to spend the
effort re-verifying and would rather force human review). That
recovery step does not exist in this codebase - only the
representation it would use does.

**"Avoid falsely marking an action successful merely because the
executor crashed after mutation"** is enforced structurally, not just
by convention: `record_action_result` refuses to accept
`SUCCESS`/`FAILED`/`PRECONDITION_FAILED`/`NOT_ATTEMPTED` paired with a
`None` mutation flag, and refuses `UNKNOWN` paired with anything
*but* `None` (see `app/dedup/execution_service.py`). There is no way
to construct an audit row that claims certainty about a mutation
outcome without a caller explicitly asserting a definite `True`/`False`.

#### 3. Audit ordering

**The mutation happens, then the audit is persisted - never the
reverse, and never speculatively before the attempt.** This ordering
is now the explicit contract documented on
`DedupExecutionActionResult` itself. One direct consequence: if a
process dies between those two steps, the affected action has **no
audit row at all** - and that absence is genuinely ambiguous, from the
database alone, between "never reached this action yet" (a live
execution still has work left) and "attempted it, then crashed before
recording" (needs recovery). Resolving that ambiguity requires
out-of-band evidence (typically: re-observing the filesystem to see
whether the file is now gone) that only a future recovery step can
gather - it is not something `record_action_result` or
`complete_execution` can infer from the database alone, and neither
method tries to.

**The meaning of `filesystem_mutation_occurred` (tri-state, on
`DedupExecutionActionAudit`)**:
- **`True`** - the mutation demonstrably occurred, either directly
  observed by the executor immediately after the operation or
  independently reconfirmed later.
- **`False`** - the mutation demonstrably did NOT occur: never
  attempted, or attempted and the filesystem confirmed unchanged (a
  precondition check correctly refusing before any OS call; an
  attempted operation failing before any write took effect).
- **`None`/NULL** - genuinely indeterminate. Reserved exclusively for
  `result=UNKNOWN`. Must never be treated as equivalent to `False` by
  any code, anywhere, ever - "we don't know" and "it definitely didn't
  happen" are different facts with different consequences for what a
  future recovery step should do next.

A plain non-nullable boolean cannot represent this third case, which
is why `filesystem_mutation_occurred` was changed from `NOT NULL` to
nullable this milestone (migration `6189c90d31a8`) - a real, if small,
schema change, made only because the tri-state distinction is
genuinely required, not spec­ulative.

**An important implementation pitfall found and fixed while building
this**: SQLAlchemy's `mapped_column(..., default=False)` silently
coerces an explicit `None` into `False` at flush time (a scalar column
default fires whenever the value being flushed is `None`, regardless
of whether that `None` was a deliberate assignment or simply never
set - it has no way to distinguish the two). The column-level default
was removed entirely; `record_action_result`'s own Python-level
default (`False`, applied only when a caller omits the parameter, not
when they explicitly pass `None`) is what the earlier code should have
relied on. This was caught by the real-Postgres integration test for
`UNKNOWN` - assertions against an in-memory unit-test mock would never
have surfaced it, which is exactly why this project insists on real-
database verification, not mocked verification alone, for anything
this load-bearing.

#### 4. Idempotency / restart behavior

**What happens if the executor is restarted**: it cannot resume a
previously-`RUNNING` execution by calling `start_execution` again for
the same authorization - that call is refused (see "duplicate
execution prevention" in the prior milestone), backed by both a
service-level check and a real `UNIQUE` constraint on
`dedup_executions.authorization_id`. This existing property doubles as
restart protection: a naive "just retry with the same authorization"
approach is already structurally impossible, not merely discouraged.
**A genuinely new attempt requires a brand-new authorization** - which
itself requires the old one to be explicitly revoked first (see next
point) and re-passes a fresh `check_plan_validity`.

**A subtle, real interaction surfaced and verified this milestone**:
finalizing an execution (`complete_execution`) never changes its
authorization's own `status` - that field only ever moves
`AUTHORIZED` → `REVOKED`, and only via `revoke_authorization`, called
explicitly. This means a **consumed** authorization (one already bound
to a terminal execution, which per the unique constraint above can
never execute again) still reads as "active" to `authorize_plan`'s own
duplicate-authorization check, and a fresh authorization attempt for
the same plan is refused (409) until the consumed one is explicitly
revoked. This is intentional: "this plan is available for a fresh
attempt" is a deliberate, auditable, human-initiated step (an explicit
revoke, with an optional reason) rather than something that happens
silently the instant an execution finalizes - fully consistent with
"an authorization should not automatically become an execution
record" running in the other direction too: an execution finishing
does not automatically free its authorization.

**A genuine gap this design pass surfaced, since fixed** by the later
"Execution Recovery & Partial-Replanning Design" section: re-planning
a *partially*-executed review's remaining work was not actually
possible at the time this section was originally written. Once a
duplicate had really been removed by an earlier execution,
`generate_plan_for_review` had no concept of "already resolved" at the
review-member level - it would include that member in any new plan
regardless. Because a plan action's own `observed_exists` is frozen at
*that* plan's generation time, and `check_plan_validity`'s `is_valid`
requires the file to exist *now* for that exact action, an action
generated against an already-missing file could never be valid - not
now, not ever. This blocked authorization of the **entire** new plan,
not just the still-outstanding part of it, even though the outstanding
member's own file was completely untouched and its own action would
otherwise have been perfectly valid on its own. Resolved by excluding
a non-canonical member from a regenerated plan's actions entirely
whenever its file no longer exists at generation time (a live
re-check, not a stored "resolved" flag) - see that section for the
full design and the worked example proving it.

#### 5. Per-action locking/concurrency

**Decision: rely on the existing database-level unique constraints;
do not build additional locking infrastructure.** AI_Brain is a
single-process, single-user, local system - there is no task queue, no
worker pool, and no distributed-execution architecture anywhere else
in this codebase, and none is planned. Building lease tokens,
heartbeats, or distributed locks for a multi-worker scenario that
cannot occur given how this system is actually deployed would be
exactly the kind of speculative infrastructure this project avoids.

**What actually prevents concurrent double-execution today**: the
`UNIQUE` constraint on `dedup_executions.authorization_id` (an
authorization can never back two executions, even if two callers
somehow both pass `start_execution`'s pre-check before either commits
- the constraint, not the pre-check, is what's truly load-bearing
under a race) and the `UNIQUE` constraint on
`dedup_execution_action_audits.(execution_id, plan_action_id)` (the
same reasoning, per action). Both were verified directly against real
Postgres to raise a real `IntegrityError` when the service-level
pre-check is deliberately bypassed. The service-level checks exist to
turn that raw `IntegrityError` into a clean `ValueError`/409 in the
common (non-contended) case - they are a usability nicety, not the
actual safety mechanism.

**If AI_Brain ever gains a genuine multi-process or distributed
executor architecture**, this decision should be revisited - but not
before there is a concrete reason to.

#### Testing and verification for this design pass

15 new tests total: unit (8, covering `UNKNOWN`'s validation rules and
`NEEDS_REVIEW` derivation/priority), API (3, the same behaviors
through the HTTP layer), and real-database integration (4): the full
crash-recovery representation against
real Postgres (including a real NULL round-trip check via
`db.expire_all()` + re-fetch, which is exactly what caught the
SQLAlchemy default-coercion bug above), the consumed-authorization
interaction, and the re-planning gap. Full suite: 466 tests, run three
consecutive times with zero flakiness. Verified against the real
running API and the real `aibrain` database: a synthetic three-document
review executed with one `SUCCESS` and one `UNKNOWN` action, correctly
finalized to `NEEDS_REVIEW`, with the `null` mutation flag confirmed
round-tripping through real Postgres in the raw API response; the
consumed-authorization/explicit-revoke interaction reproduced exactly
as documented. Cleaned up afterward with zero leftover rows. No real
corpus file was read, created, deleted, moved, renamed, or modified at
any point.

### Execution Recovery & Partial-Replanning Design

A follow-up design pass resolving the two gaps the prior milestone
explicitly surfaced and deferred - crash detection/closure, and
re-planning after partial execution - kept, again, deliberately
separate from writing the executor. **Still no filesystem mutation
capability anywhere in this codebase.** The code added here either (a)
reads the filesystem, never writes to it (recovery re-observation,
exactly like `check_plan_validity` already does), or (b) changes what
gets *proposed* in a plan, never what happens to a real file.

#### 1. Detecting a stale/stuck `RUNNING` execution

No automatic detection - AI_Brain has no process supervision anywhere,
so it cannot know whether a `RUNNING` execution's process is actually
dead versus just slow. `list_executions` gained an advisory
`started_before` filter (`GET /dedup/executions?status=running&
started_before=<ISO 8601 timestamp>`) so a human can narrow candidates
worth investigating - this is a convenience for finding *candidates*,
never a judgment that a matching execution is actually stuck. No
threshold is hard-coded or defaulted; inventing one now, with no real
executor yet to calibrate against, would be speculative.

#### 2 & 3. Generating `UNKNOWN` during recovery, and the transition to `NEEDS_REVIEW`

`DedupExecutionService.recover_stale_execution(execution_id)` is an
explicit, human-invoked action (gated by `confirm: true` at the API
layer, the same convention as `authorize_plan`/`start_execution` - not
a parameter on the service method itself, matching how neither of
those accepts one either) that closes out a `RUNNING` execution. For
every planned action with no existing audit row, it re-observes that
file right now (reusing `_observe_file`, never duplicating the
file-reading logic) and classifies it into exactly one of two
outcomes - deliberately not three, and never a guess at `SUCCESS`:

- **File confirmed unchanged** (still exists, hash AND size exactly
  match the plan's pre-mutation expectation): `NOT_ATTEMPTED`,
  `filesystem_mutation_occurred=False`. Not a guess - if the delete
  had happened, the file could not still be sitting there with its
  original content. Recovery cannot (and does not try to) distinguish
  "never reached" from "attempted and failed leaving the file intact"
  - the safe next step is identical either way, so the distinction
  doesn't matter.
- **Anything else** (file now missing, or present with different
  content): `UNKNOWN`, `filesystem_mutation_occurred=None`, with
  whatever was actually observed attached
  (`observed_content_hash`/`observed_file_size`/`error_message`) for a
  human's later benefit. This is the direct, tested answer to "what
  happens if the filesystem state differs during recovery" (point 9):
  a missing file is *consistent with* a successful delete, but
  recovery never promotes that consistency into a claimed `SUCCESS` -
  it cannot rule out the file having vanished for an unrelated reason
  (a human manually deleting it, external interference), and asserting
  success without proof is precisely what this entire design refuses
  to do.

`recover_stale_execution` then calls the EXISTING `complete_execution`
- no new finalization logic was needed, since any `UNKNOWN` row already
forces `NEEDS_REVIEW` (built in the prior milestone). Raises
`ValueError` if the execution isn't `RUNNING`, or if every planned
action already has a recorded outcome (nothing to recover - call
`complete_execution` directly).

#### 4. What a human does after `NEEDS_REVIEW`

Inspect the execution's action audits (`GET
/dedup/executions/{id}/actions`) and independently verify each
`UNKNOWN` row's real-world outcome - AI_Brain builds no tooling for
this investigation itself. **A genuinely unresolved question,
deliberately not answered by inventing a mechanism for it**: once a
human has manually confirmed a document's file really is gone, there
is currently no supported way to convert that confirmed finding into a
recorded `SUCCESS` fact after the fact. Audit rows are immutable by
design (no update method exists anywhere in `DedupExecutionService`),
and building a one-off "correct this `UNKNOWN` into a `SUCCESS`"
mutation path felt like exactly the kind of ad-hoc workaround this
design pass exists to avoid rather than introduce. The practical
consequence, and why it's still safe: that document remains
"unresolved" by point 5's exclusion rule below, so a fresh plan for
its review will still propose it - but since the file really is gone,
`check_plan_validity`'s existing `exists_now` check will (correctly)
flag it, refusing authorization rather than acting on stale
information. Point 8 below explains why the LIVE-file-existence
exclusion added in point 5 actually makes this safer than it sounds:
that document's file being physically absent means the exclusion
mechanism drops it from a regenerated plan's *actions* regardless of
whether any `SUCCESS` audit exists for it. A human is only left with
manual work in the narrower case of an `UNKNOWN` finding they have
NOT yet personally verified - not a lingering safety gap, but real
remaining toil this milestone does not remove.

#### 5, 6 & 8. Excluding already-resolved work from a regenerated plan

**The prior milestone's documented gap is now fixed.**
`generate_plan_for_review` (`app/dedup/execution_plan_service.py`) now
excludes a non-canonical member from the plan's *actions* entirely if
its file does not currently exist - a plain, live re-read at
generation time, using the exact same `_observe_file` call the method
already made for every member (previously used only to populate
`observed_exists` on an action that got created regardless; now it
gates whether that action gets created at all). If EVERY non-canonical
member's file is already gone, `generate_plan_for_review` raises
(422) - there is nothing left to plan.

This is deliberately based on **live file existence, not on a stored
"resolved" flag or a `SUCCESS`-audit lookup** - a smaller, more general
fix than adding review-member-level resolution tracking would have
been, and it closes both gaps at once:

- **Point 6 (failed/never-attempted actions)**: nothing changes for
  these - a member with a `FAILED`/`NOT_ATTEMPTED`/`UNKNOWN` result (or
  no result at all yet) has its file still present, so it is *not*
  excluded and is re-proposed in the new plan exactly as before, freshly
  re-observed.
- **Point 8 (preventing accidental re-execution of completed work)**:
  solved for BOTH the clean-completion case (a `SUCCESS`-recorded
  deletion leaves the file gone, live-check excludes it) AND the
  crash-recovery case (an `UNKNOWN` finding where the file also happens
  to be missing is excluded too, even though no `SUCCESS` audit exists
  for it - see the worked example below). Two independent layers now
  protect against ever re-targeting a gone file: exclusion at
  generation time (this milestone), and `check_plan_validity`'s
  existing `exists_now` requirement at authorization/execution time (a
  prior milestone) as a backstop if exclusion were ever somehow
  bypassed.

A plan's API response gained a computed (never stored)
`excluded_document_ids` field - the set difference between the
review's own members and the plan's actual actions - so a human
looking at a smaller-than-expected plan can see *which* members were
left out, without the response claiming to know *why* (a missing file
is equally consistent with "an earlier execution succeeded" and "a
human deleted it separately" - the field doesn't guess between them).

**Worked example, verified against real Postgres and the real running
API**: a review with two duplicates, one execution partially
completes - one duplicate's file is deleted for real (test-simulated,
recorded `SUCCESS`) and the other is left unresolved with its file
still present. `recover_stale_execution` correctly records the
unresolved one as `NOT_ATTEMPTED` (file unchanged). Regenerating the
plan produces exactly one action - the still-outstanding, untouched
duplicate - with `excluded_document_ids` correctly naming the resolved
one. That new plan is immediately, fully valid and authorizes
successfully in one call. A second scenario proves the general-purpose
nature of the exclusion rule: when BOTH duplicates' files are made to
disappear (one via a recorded `SUCCESS`, one via an `UNKNOWN` recovery
finding), regenerating the plan excludes both and correctly refuses
with "no plannable work left," rather than silently producing an empty
or partially-doomed plan.

#### 7. Whether a new authorization is required after recovery/re-planning

Yes, unconditionally, and unchanged from the existing architecture -
zero new code was needed for this. `DedupPlanAuthorization` is bound
1:1 to a specific `plan_id`; a regenerated plan is a brand-new row
with a brand-new `plan_id`, so it always requires its own fresh
`authorize_plan` call, which itself re-runs `check_plan_validity`
against current reality. Recovery and re-planning do not, and could
not, retroactively authorize anything.

#### 9. Filesystem state differing during recovery

Answered directly in points 2 & 3 above: anything other than an exact
match to the plan's pre-mutation expectation - a missing file, or a
present file with different content - is recorded `UNKNOWN`, never
interpreted further. Recovery does not attempt to distinguish *why*
the state differs (successful deletion vs. external interference vs.
something else) - only a human, investigating outside this system, can
do that.

#### 10. Testing and verification for this design pass

19 new tests: unit (8, covering `recover_stale_execution`'s
classification logic, the `started_before` filter, and the plan
generation exclusion/raise behavior), API (6, the recovery endpoint
and the `started_before` query parameter through the HTTP layer), and
real-database integration (5): the two recovery classification
outcomes against real Postgres, `recover_stale_execution` refusing a
non-`RUNNING` execution, the `started_before` filter against real data,
and the full recovery-then-replan flow proving the exclusion mechanism
handles a mixed `SUCCESS`+`UNKNOWN` scenario correctly. One existing
test (`test_generate_plan_records_missing_source_honestly`) was
rewritten, and one existing integration test
(originally documenting the now-fixed gap) was rewritten to prove the
fix instead of the gap - both intentional, understood consequences of
this milestone's behavior change, not incidental breakage. Full suite:
487 tests, run three consecutive times with zero flakiness. Verified
against the real running API and the real `aibrain` database: a
three-document review's plan generated with `excluded_document_ids`
correctly empty, one action recorded `SUCCESS` (with the file really
deleted), the execution recovered via `POST .../recover` correctly
classifying the untouched second action as `NOT_ATTEMPTED`, a
regenerated plan correctly excluding only the resolved document and
authorizing successfully in one call - then fully cleaned up with zero
leftover rows. No real corpus file was read, created, deleted, moved,
renamed, or modified at any point.

**Deliberately not built this milestone**: the filesystem executor
itself; any automatic stale-execution detection/trigger (point 1 is
advisory only); any mechanism to convert a human-confirmed `UNKNOWN`
finding into a `SUCCESS` fact (point 4's documented, deliberately
unresolved question); any frontend.

### Filesystem Executor Design (no implementation)

**Zero code changed in this milestone.** No model, migration, service
method, or API endpoint was added, and none of the operations named
below - `unlink`, `remove`, `rename`, `move`, quarantine placement, or
any other filesystem mutation - exists anywhere in this codebase after
it. This is a settled design for code that does not yet exist, written
so the actual implementation milestone has nothing left to improvise
under pressure. Everything here is a decision or a specification;
where a decision requires a new concept (recovery/reconciliation,
below) that concept is designed in full but built later, explicitly
gated on this design being reviewed first.

#### 1. Quarantine/staging semantics

**The operation is a same-filesystem `rename()` (move), never a copy,
never an unlink.** The duplicate's bytes are relocated, not destroyed
- reversibility comes from the fact that the data still exists
somewhere findable, not from any undo mechanism.

**Destination path**: `<quarantine_root>/<execution_id>/
<plan_action_id>__<original_basename>`. Namespacing by
`execution_id`/`plan_action_id` - both already unique, already present
on every `DedupExecutionActionAudit` row - makes a destination
collision structurally impossible without any additional uniqueness
logic (no timestamp suffixes, no retry-on-collision loops). The
original basename is carried through unmodified for human
readability; it plays no role in the uniqueness guarantee and is never
parsed or interpreted.

**`quarantine_root` must live on the same filesystem/device as the
corpus it quarantines from - verified, not assumed.** This was checked
directly against this actual deployment while writing this design:
`BASE_DIR` (the AI_Brain checkout, under `/home`) resolves to device
`nvme0n1p8`; the real corpus at `/mnt/t7ssd/vscode/data` resolves to
device `sdc1` - **entirely different filesystems.** The natural,
naive choice - a quarantine directory under `BASE_DIR`, alongside
`INGESTION_DIR` - would be wrong: every quarantine move would be a
cross-device rename, which is never atomic and would force exactly the
copy-then-delete fallback this design exists to forbid. The quarantine
root must instead live on the SAME device as the corpus (e.g. a
sibling of `/mnt/t7ssd/vscode/data`, such as
`/mnt/t7ssd/vscode/.dedup_quarantine`) - outside the ingested/scanned
subtree, so a quarantined file can never be re-ingested or
re-flagged as a duplicate of itself. **Required executor startup
check**: compare `os.stat(quarantine_root).st_dev` against
`os.stat(Path(source_path).parent).st_dev` for the corpus root(s) in
use, and refuse to start at all if they differ - this must be caught
at startup, not discovered as a per-action `EXDEV` surprise partway
through a plan. No `DUPLICATES_QUARANTINE_DIR`-style setting was added
to `app/core/config.py` this milestone (that's implementation); the
recommendation is to add exactly one such path setting, defaulting to
a location verified to share a device with wherever documents are
actually ingested from.

**Retention is explicitly out of scope for the executor.** Quarantined
files persist indefinitely; nothing in this design ever purges them.
A future "empty the quarantine" operation is a distinct, later,
separately-approved capability - not something the executor decides
on its own.

#### 2. Operation semantics: what `DELETE` actually means

`DedupPlanActionType.DELETE` is - and remains - the plan's vocabulary
for user intent ("remove this duplicate"), not a literal instruction
to unlink. The executor fulfills a `DELETE`-typed action by performing
the quarantine move described above. This is a deliberate, permanent
mapping, not a temporary stand-in for "real" deletion: if AI_Brain
ever adds a genuinely irreversible permanent-delete capability, it
must be a **new, separately-named action type** (e.g. `PURGE`) with
its own explicit authorization/audit story, never a silent
reinterpretation of what `DELETE` does. Nothing here proposes adding
`PURGE` - it is named only to make clear that `DELETE`-means-quarantine
is not expected to quietly become `DELETE`-means-unlink later.

#### 3. Per-action precondition checks

The mandatory, immediately-before-mutation check reuses
`check_plan_validity`'s existing six dimensions verbatim
(`document_exists`, `path_changed`, `exists_now`, `type_matches`,
`hash_matches`, `size_matches`) plus three new ones this design adds
(security/link checks, below). **Not implemented this milestone, but
specified**: a single-action variant,
`check_action_validity(plan_id, plan_action_id)`, reusing the exact
same comparison logic `check_plan_validity` already proves correct,
so the executor never re-validates every OTHER action in the plan just
to check one - `check_plan_validity` as it exists today is
whole-plan-scoped and would make per-action revalidation
needlessly quadratic across an n-action plan.

#### 4. TOCTOU strategy

The window between the final precondition check and the actual
`rename()` call cannot be reduced to zero without a locking mechanism
this design deliberately does not adopt (flock-style locks do not
prevent an external `rm`, are not portable across every filesystem
AI_Brain might run on, and would mean this system holding a lock on a
file it does not exclusively own). **The strategy is minimize, detect,
report - never assume.** The window is minimized by checking
immediately before acting (not once for the whole plan, as
established in the prior milestone); if the file changes in that
narrow window anyway, the `rename()` call itself will typically
surface it as an `OSError` (source vanished under us) - recorded as
`FAILED`, a definite OS-reported outcome - and post-move verification
(point 9) catches the remaining sliver where `rename()` succeeds but
something is still inconsistent, recorded `UNKNOWN` rather than
trusted blindly.

#### 5. Action locking/concurrency

Unchanged from the prior milestone's decision: the existing
`UNIQUE` constraints (`dedup_executions.authorization_id`,
`dedup_execution_action_audits.(execution_id, plan_action_id)`,
both already verified against real Postgres) remain the sole
concurrency guarantee - no distributed locking is introduced for
AI_Brain's single-process, single-user architecture. Added by this
design: **within one execution, actions are processed strictly
sequentially, one at a time, never in parallel** - the stop-on-first-
failure policy is inherently a linear, ordered concept, and
parallelizing actions would require an entirely different (and
unbuilt) failure semantics. Actions are processed in ascending
`DedupExecutionPlanAction.id` order (their creation order at plan
generation time) - arbitrary but deterministic, which is all that is
actually required since no action depends on another.

#### 6. Execution/audit ordering, and the complete crash-window inventory

The ordering contract from the prior milestone stands: mutation
happens, THEN the audit is persisted, never the reverse. This design
adds the full enumeration of where a crash can land relative to that
ordering, and what recovery sees in each case:

| Window | Where the crash lands | What recovery finds | Handling |
|---|---|---|---|
| W1 | Before the per-action precondition re-check | No audit row; file untouched | `recover_stale_execution`: file matches plan exactly → `NOT_ATTEMPTED` |
| W2 | After the precondition check passes, before `rename()` | No audit row; file untouched | Same as W1 - indistinguishable from it, and doesn't need to be distinguished |
| W3 | During the `rename()` call itself | No audit row; POSIX rename is atomic on a shared device, so the file is either still at the source or fully at the destination - no partial state is possible for the file itself | Recovery sees either "unchanged" (→ `NOT_ATTEMPTED`) or "missing" (→ `UNKNOWN`, point 9's post-move verification never ran) |
| W4 | After `rename()` returns success, before post-move verification | No audit row; file is at the destination, gone from source | Recovery sees "missing" from the source path → `UNKNOWN` (exactly the scenario in the user's own worked example: mutation occurred, crash, audit says `UNKNOWN`) |
| W5 | After verification passes, before `record_action_result` is called or its transaction commits | Same as W4 | Same as W4 - `UNKNOWN` |
| W6 | After one action's audit is fully committed, before the next action starts | That action's audit is correct and complete; execution is otherwise clean | `recover_stale_execution` resumes exactly where the crash left off - already-audited actions are never touched again |
| W7 | During `complete_execution` itself, after every action already has a correct audit row | Execution stuck at `RUNNING`, but `recover_stale_execution` would find nothing unresolved | **Already handled correctly today**: `recover_stale_execution` raises "nothing to recover" in exactly this case - the correct operator action is `POST /dedup/executions/{id}/complete` directly, not `.../recover` |

W4/W5 - mutation definitely happened, audit doesn't know it yet - are
precisely why `UNKNOWN` exists and why recovery refuses to guess
`SUCCESS` from a missing file alone: recovery cannot distinguish W4/W5
from an entirely different story (a human deleted the file separately,
unrelated to this execution).

#### 7. Recovery/reconciliation - designed, not implemented

The immutability of `DedupExecutionActionAudit` is a property worth
keeping, not a limitation to route around - so a human's later,
independently-verified finding about an `UNKNOWN` action (e.g.
"I checked - `foo.txt` is gone, and the quarantine copy is present
with a matching hash") must be recorded as a **new fact layered on
top of history**, never as an edit to the original row. Designed
shape for when this becomes part of an actual executor milestone:

```
DedupExecutionActionReconciliation
    id
    audit_id            -- FK to DedupExecutionActionAudit, UNIQUE
                         --   (at most one reconciliation per audit -
                         --    once verified, it's verified once)
    verified_result      -- SUCCESS or FAILED only - NOT_ATTEMPTED
                          --   never needed reconciliation (already
                          --   confident) and reconciling UNKNOWN to
                          --   UNKNOWN is a no-op not worth a row
    verified_by           -- free text, mirrors authorized_by/
                           --   reviewer_decision's existing convention
    verification_method   -- free text/JSONB: HOW it was verified
                           --   (e.g. "inspected quarantine path,
                           --   confirmed present with matching hash")
    verified_at
    created_at
```

Reconciliation would be purely additive and purely forensic - it
answers "what do we now believe happened," it does not change what
`DedupExecution.status` was already finalized to (that stays whatever
`complete_execution` derived at the time, `NEEDS_REVIEW` included -
finalized status is itself a historical fact, not something a later
reconciliation retroactively rewrites either). It also does NOT change
re-planning behavior: the live-file-existence exclusion added in the
prior milestone already correctly drops a genuinely-gone file from a
future plan regardless of whether a reconciliation record exists for
it - reconciliation exists for auditability and human confidence, not
because anything is unsafe without it. **Not built until an actual
executor exists to make `UNKNOWN` findings worth reconciling in
practice.**

#### 8. Executor idempotency

The executor itself needs no idempotent-retry logic, because the
architecture already forecloses the scenario that would require it:
one execution per authorization, permanently (prior milestone), means
there is no code path through which the SAME intended mutation could
ever be attempted twice under the same permission. A human who wants
to address remaining work after a crash goes through
recover → (optionally) reconcile → a fresh plan (benefiting from
exclusion) → a fresh authorization → a fresh execution - each step an
explicit new decision, never a blind retry of the old one.

#### 9. Verification after mutation

Before ever calling `record_action_result` with `SUCCESS`, the
executor must independently confirm, via a plain non-mutating read:
the source path no longer exists, the destination path exists, and
the destination's hash/size match what was expected pre-move. If
`rename()` raised no error but this verification fails - a
corroboration mismatch, not an OS-reported failure - the correct
result is `UNKNOWN`, not `FAILED`: `FAILED` is reserved for a
definite, OS-reported failure, and a rename that "succeeded" but left
something unexplained at the destination is precisely the kind of
finding that must not be resolved by guessing.

#### 10. Result semantics, refined for this specific operation

The four (five, including `UNKNOWN`) `DedupExecutionActionResult`
values already exist; this design refines what each one means
specifically for a same-filesystem quarantine-move executor:

- **`SUCCESS`**: precondition check passed, `rename()` succeeded, AND
  post-move verification (point 9) corroborated it.
  `filesystem_mutation_occurred=True`.
- **`PRECONDITION_FAILED`**: the immediately-before-mutation re-check
  found a mismatch (hash/size/path/type/existence, or one of this
  design's new checks - multiple hard links, a symlink, not a regular
  file, or outside an allowed mutation root) and refused before ever
  calling `rename()`. `filesystem_mutation_occurred=False`.
- **`FAILED`**: `rename()` itself raised an `OSError` (permission
  denied, `EXDEV`, or any other OS-level error). Refinement over the
  original generic model: because this executor's ONLY operation is a
  same-filesystem `rename()`, and POSIX same-device rename is atomic,
  **`FAILED` for this executor always means
  `filesystem_mutation_occurred=False`** - an atomic rename either
  fully succeeds or has no effect, so a raised error proves nothing
  moved. (The original model's "`FAILED` may be True or False" language
  stays technically accurate for a hypothetical future non-atomic
  operation type; it does not apply to this one.)
- **`NOT_ATTEMPTED`**: stop-on-first-failure skipped this action
  because an earlier one in the same execution failed or hit a
  precondition failure. Unchanged from the existing model.
- **`UNKNOWN`**: a crash left the action's fate unconfirmed (points 6
  and 9), or the post-move verification corroboration mismatched.
  `filesystem_mutation_occurred=None` always.

#### 11. Authorization consumption

Unchanged, restated for completeness: the executor calls
`start_execution` exactly once at the beginning of a run, consuming
the authorization (enforced by the existing unique constraint). It
never attempts to reuse an authorization, and never itself creates a
new one.

#### 12. Behavior after partial execution

Unchanged, restated for completeness: stop on the first `FAILED` or
`PRECONDITION_FAILED`; every remaining planned action is explicitly
recorded `NOT_ATTEMPTED` (never silently skipped); the execution
finalizes `PARTIALLY_COMPLETED`. Addressing the remainder requires a
fresh plan (benefiting from the prior milestone's exclusion fix), a
fresh authorization, and a fresh execution.

#### 13. Security and path safety

Not previously addressed, and load-bearing before any real mutation
capability exists:

- **Allowed mutation roots**: the executor must validate that every
  `source_path` it is asked to touch falls within a configured
  allow-list of roots (recommended: a new setting alongside
  `INGESTION_DIR`) before doing anything else with it - refusing
  outright (not `PRECONDITION_FAILED`, since this is a
  request/data-integrity problem, not staleness) if a `Document` row
  somehow points outside the expected corpus.
- **Canonicalization before comparison**: every path must be resolved
  (`Path.resolve()`) before any comparison or use, to defeat
  `..`-based traversal.
- **The quarantine root must never overlap with any allowed mutation
  root's ingested/scanned subtree** (point 1) - both to prevent
  re-ingestion of quarantined files and to keep the executor's own
  output out of the set of paths it might later be asked to act on
  again.

#### 14. Symlinks and hard links

- **Symlinks are refused, never followed.** If `source_path` is a
  symlink (`Path.is_symlink()`), or resolves through one, the
  precondition check refuses (`PRECONDITION_FAILED`) rather than
  silently operating on the link or its target.
- **Hard links are refused.** If `os.stat(source_path).st_nlink > 1`,
  quarantining this one name would not make the underlying data go
  away (other links still reference the same inode) - exactly the
  kind of misleading "it's gone" that would surprise a human relying
  on the duplicate having actually been removed. Refused
  (`PRECONDITION_FAILED`) rather than proceeding.

#### 15. Directories vs. regular files

The precondition check must explicitly verify `Path(source_path).
is_file()` (a regular file - not a directory, device file, FIFO, or
socket) before any operation, refusing (`PRECONDITION_FAILED`)
otherwise. Documents are files by definition, but this guards against
a corrupted or unexpected `Document.source` value rather than trusting
that invariant blindly.

#### 16. Permission errors, disk-full, and other OS failures

All fall under the existing `FAILED` bucket (point 10) with the raw
`OSError` message captured in `error_message` - no special-casing
needed. Worth noting: because the move is same-filesystem, a
disk-full failure during the rename itself should be exceptionally
rare (a same-device rename is a metadata operation, not a data copy,
so it does not need free space proportional to file size) - the more
realistic failure modes are permission errors and `EXDEV` (point 1).

#### 17. Executor identity/version

`DedupExecution.executor_identity` already exists (free text, added
two milestones ago) and needs no schema change. Recommended
convention for when a real value is ever written:
`"<component-name>/<version>"` (e.g. `"dedup-executor/0.1.0"`) - not
enforced, but consistent and greppable.

#### 18. What the executor is absolutely forbidden to mutate

- The plan's own canonical document's file - never a mutation target;
  only non-canonical members ever appear as plan actions.
- Any file outside that specific execution's own plan's `source_path`
  list.
- Any file outside the configured allowed mutation roots (point 13).
- Anything already sitting in the quarantine directory from an earlier
  run - an executor run touches only the destinations it creates
  itself, never a prior run's output.
- Any database row beyond exactly: the one `DedupExecution` it
  creates, one `DedupExecutionActionAudit` per action, and the two
  fields `complete_execution` sets on the execution row. **No
  `Document` row is ever deleted** - a document whose file has been
  quarantined keeps its full database record and provenance chain; it
  simply no longer resolves to a live file at its original path.
  Filesystem state and database record are deliberately decoupled.

#### Verification for this design pass

This is a design-only milestone: no model, service, API, test, or
migration changed, so there is nothing new to run against real
Postgres or the real API. The one concrete finding produced by
actually checking this deployment (`stat`, read-only, no mutation) -
that `BASE_DIR` and the real corpus live on different filesystem
devices - is recorded in point 1 above precisely because it would
otherwise have been an easy, plausible-looking mistake to carry
into the first real implementation attempt.

**Deliberately not built this milestone**: the filesystem executor
itself; `DedupExecutionActionReconciliation` (point 7 - fully
designed, explicitly deferred); any new configuration setting for
quarantine root or allowed mutation roots (recommended, not added);
any frontend.

### Filesystem executor implementation (synthetic-filesystem-only)

**This is the first code in AI_Brain that can perform a real
filesystem mutation.** It exists as one class,
`DedupFilesystemExecutor` (`app/dedup/executor.py`), implementing
exactly the reviewed design above and nothing more - the reversible
`DELETE`-means-quarantine-move operation, never permanent deletion.
It is **not wired into any API endpoint, router, or `app/main.py`** -
the only way to construct or call it is from trusted Python code
(today: its own test suite). Exposing it via HTTP, or to
arbitrary user-supplied roots, remains explicitly out of scope.

**Fail closed by construction, not by convention**: `__init__(self,
db, allowed_root, quarantine_root)` has no default value for either
root anywhere in this module - omitting one raises `TypeError` before
any other code runs. There is no config setting, environment variable,
or fallback path referencing a real location anywhere in this file;
`test_executor_source_never_references_real_corpus_path` asserts the
module's own source contains no string reference to `/mnt`, `t7ssd`,
or `vscode/data` at all, and
`test_executor_requires_explicit_roots_no_defaults` proves the missing-
argument `TypeError` directly. The constructor also refuses (all
`ValueError`, before touching any file): either root missing or not a
directory, the two roots being the same or nested inside each other,
and - the central safety property carried over from the design
milestone - the two roots being on different filesystem devices
(`os.stat().st_dev` comparison; verified with a real sibling-directory
check under `tmp_path` to confirm the assumption these tests rely on
actually holds in this environment).

**`execute(execution_id, *, confirm)`** - `confirm` has no default and
is required as a keyword argument, since no API layer wraps this
method yet to provide that gate itself. Refuses (`ValueError`, no
mutation) unless the execution is currently `RUNNING` with **zero**
existing action audits - this executor runs exactly once, start to
(stop-on-first-failure) finish, from a fully fresh execution. It is
deliberately **not resumable**: if `execute()` itself were somehow
interrupted, leaving an execution `RUNNING` with partial audits, a
second `execute()` call refuses outright rather than guessing how to
continue - `DedupExecutionService.recover_stale_execution` (previous
milestone) is the only supported path for closing out that situation,
since it makes a conservative, never-guessed determination rather than
this method silently attempting to pick up an unknown prior attempt.

**Per-action pipeline** (`_attempt_action`, run independently, fresh,
for every single action - never once for the whole plan): reload
authorization and plan validity via the existing
`DedupPlanAuthorizationService.check_currency` and re-derive
actionability **scoped to this one action** (not the whole plan's
aggregate `is_valid`, which would incorrectly block an otherwise-fine
action just because some unrelated action in the same plan had gone
stale) → refuse symlinks on the un-resolved path, then canonicalize
(`resolve(strict=True)`) and verify containment inside `allowed_root`
on the *resolved* path (defeating a symlinked intermediate directory,
not just a symlinked final component) → verify a regular file → refuse
multiple hard links (quarantining one name would not remove the
underlying data) → a **second**, later hash/size re-observation via
the existing `_observe_file`, deliberately redundant with what
`check_plan_validity` already confirmed moments earlier - each
re-check closer to the mutation shrinks the TOCTOU window, which is
the entire point → verify the source and quarantine share a device →
verify the destination path (`<quarantine_root>/<execution_id>/
<plan_action_id>__<basename>`) does not already exist (**load-bearing,
not defensive theater**: `os.rename()` silently overwrites an existing
destination on POSIX with no error at all - this check is the only
thing standing between "nothing there yet" and silent data
destruction) → exactly one `os.rename()` → **verify the resulting
quarantine object** before ever recording `SUCCESS`, checking each
condition independently and by name (so a failing test - or a future
reader - can see exactly which one broke, not one opaque combined
boolean): the source path no longer exists; the destination exists as
a genuine regular file; the destination is *not* a symlink (checked
explicitly, never merely implied by a hash comparison); and the
destination's hash and size match what the plan expected. "No
unexpected second filesystem operation occurred" holds by
construction, not by a runtime check - `_attempt_action` contains
exactly one `os.rename()` call site, and nothing in this class ever
calls copy, chmod, or a second move. Any one of these conditions
failing - a `rename()` that raised no error but left something the
verification cannot corroborate - is recorded `UNKNOWN`, with the
specific failed check(s) named in `error_message`, never trusted into
a false `SUCCESS`.

**Failure policy, exactly as specified**: the first action that does
not cleanly succeed stops the run; every action already recorded
keeps its result; every action after the stopping point is explicitly
recorded `NOT_ATTEMPTED`. A same-device `rename()` failing (permission
error, or any other `OSError`) is always recorded with
`filesystem_mutation_occurred=False`, per the design's atomicity
refinement. `execute()` also wraps each action attempt in a last-resort
exception handler: anything genuinely unanticipated is recorded
`UNKNOWN` (never leaving the execution stuck at `RUNNING` from an
unhandled exception) and the run stops. `execute()` always finalizes
via the existing `complete_execution`, which - already, from an
earlier milestone - can never report `COMPLETED` unless every planned
action's audit is `SUCCESS`.

**Real, verified device check**: `test_device_of_matches_for_siblings_
under_tmp_path` confirms two sibling directories under the same
`tmp_path` genuinely share a device on this environment, validating
the assumption every other test in the suite depends on for its
`allowed_root`/`quarantine_root` pair to be meaningful (rather than
accidentally passing only because the device check never actually
distinguished them).

**Tests**: 30 real-database (`aibrain_test`) + real-synthetic-
filesystem (`tmp_path`) tests, covering: successful quarantine
(including byte-for-byte destination content verification), hash
mismatch, size mismatch, missing source, a symlinked source, a
multi-hard-linked source, a source outside `allowed_root`,
authorization revoked mid-run, a live `Document.source_type` change
(staleness `check_plan_validity` alone would catch, distinct from a
raw file change), destination collision, a real permission failure
(skipped when running as root, where the check is meaningless), stop-
on-first-failure with the middle of three actions corrupted, an
all-actions-fail case, a mocked post-move hash/size mismatch
(`UNKNOWN`), a mocked destination-is-a-symlink mismatch (`UNKNOWN`,
exercising that specific named check on its own), a mocked unexpected
exception (`UNKNOWN`, and the run still reaches a terminal state),
calling `execute()` twice after completion, calling it on an execution
with pre-existing partial audits, a nonexistent execution id, and the
full set of constructor fail-closed checks.

**Test isolation, proven the same way the production code's isolation
is proven**: every test's `allowed_root`/`quarantine_root` is a
disposable `tmp_path` subdirectory, created fresh and torn down
automatically by pytest - no test in this suite references, could
reach, or was ever pointed at the real personal corpus, the user's
home directory, or any other existing personal-data directory, and
none discovers a filesystem root dynamically from the host environment
(no `os.environ`, no `BASE_DIR`/`INGESTION_DIR`, no config setting).
`test_executor_tests_never_reference_real_paths_or_discover_roots_
dynamically` scans the test file's own source for exactly these
forbidden references and asserts none exist - the same source-scanning
technique already used to prove the production code's own isolation,
now applied to the tests themselves. (Writing that test surfaced its
own small lesson: a source-scanning test that reads its *entire own
file* must not spell out the literal forbidden strings in its own
assertions or docstring, or it fails against itself - the forbidden
literals are built via string concatenation specifically so the
banned substrings never appear contiguously in this file's own
source.)

One real, unrelated finding surfaced while verifying zero leftover
rows for this milestone: `aibrain_test` had two stray `dedup_plan_
authorizations` rows (plus their parent review/plan/documents) left
over from an *earlier* milestone's now-fixed test cleanup bug (a first,
failing attempt at `test_reauthorizing_a_plan_after_execution_
requires_explicit_revoke` left data behind before its cleanup
argument list was corrected) - never touched by any later successful
run, since each test tracks only the row IDs it itself creates. Found
and removed directly; `aibrain_test`'s dedup-related tables are
confirmed empty as of this milestone.

**Deliberately not built this milestone**: any API endpoint or router
wiring for the executor; any default or configured production
`allowed_root`/`quarantine_root`; permanent deletion; any resumption
of a partially-run `execute()` call (that remains `recover_stale_
execution`'s job); `DedupExecutionActionReconciliation` (still
deferred); any frontend. **The real corpus was not read, mutated, or
referenced by any code or test added in this milestone.**

### Executor hardening: concurrency, freshness, symlink roots, and destination-device checks

A hostile code-level security review of the executor above (adversarial,
no behavior changes) surfaced findings that this milestone closes, one
at a time, exactly as the review scoped them.

**Concurrency exclusivity, made real rather than assumed**: a new
`DedupExecution.claimed_at` column (nullable `DateTime`, migration
`02467162321c`) is set exactly once, by a new `_claim_execution`
method, under `SELECT ... FOR UPDATE` on the target execution row -
Postgres blocks a second concurrent transaction's own `FOR UPDATE` on
that same row until the first commits or rolls back, so two callers for
the same `execution_id` can never both enter the action loop. The
second caller does not see a raw `IntegrityError` or an uncaught
`ValueError` from `complete_execution` as its "normal" concurrency
outcome - it blocks until the first transaction finishes, then finds
`claimed_at` already set (or the status no longer `RUNNING`) and raises
a clean, deliberate `ValueError` naming the prior claim. `execute()` now
calls `_claim_execution` in place of the old inline
get-and-check-status block; nothing about the existing one-execution-
per-authorization semantics changed. Proven under genuine contention -
not merely reasoned about - by `test_concurrent_execute_calls_only_
one_claims_and_mutates`, which drives two real threads against two
separate `Session`/connection objects on real Postgres, synchronized
with a `threading.Barrier` so both attempt `execute()` as close to
simultaneously as possible; run 5 times in isolation with zero
flakiness in addition to its place in the full suite.

**Authorization/plan freshness, made explicit rather than inherited
from a framework default**: `_attempt_action` now opens with
`self.db.expire_all()`, an explicit statement that every action must
observe current authorization and plan state, independent of whatever
`expire_on_commit` happens to default to. `test_authorization_revoked_
from_another_session_is_observed_even_with_expire_on_commit_disabled`
proves the property doesn't secretly depend on that default at all: it
opens the executor's own session with `expire_on_commit=False` -
deliberately disabling the framework behavior this safety property used
to lean on - revokes the authorization from a second, independent
session mid-run, and confirms the next action still refuses.

**Quarantine-root and allowed-root symlinks now rejected, not silently
resolved**: the constructor checks `Path(...).is_symlink()` on both
roots *before* any `resolve()` call runs (`resolve()` would otherwise
follow the link transparently), raising `ValueError` for either root.
Threat model: a symlinked root is a single point where "the directory
you asked for" and "the directory actually used" can silently diverge
between calls, and unlike an intermediate path component inside a
plan action (already defeated by resolving the full path and checking
containment), a symlinked *root itself* has nothing above it to contain
it against. Fail-closed was chosen deliberately for the production-
facing design over silent resolution. Covered by
`test_executor_rejects_allowed_root_as_symlink` and `test_executor_
rejects_quarantine_root_as_symlink`.

**Directory/FIFO/socket sources - confirmed rejected two layers above
the executor, with the executor's own check proven independently
correct anyway**: all three source types share `Path.is_file()` via
`_observe_file`, the exact primitive used by `generate_plan_for_review`'s
existing exclusion logic (see "Execution Recovery & Partial-Replanning
Design"), by `check_plan_validity`, and by the executor's own explicit
`is_file()` guard. Tracing the real code path shows a directory, FIFO,
or socket source is excluded from the plan *before a plan can even be
generated* - `generate_plan_for_review` raises "no plannable work left"
long before the executor is reached, making the executor's own
`is_file()` check unreachable through the normal pipeline today.
`test_execute_directory_source_is_excluded_at_plan_generation`,
`test_execute_fifo_source_is_excluded_at_plan_generation`, and
`test_execute_socket_source_is_excluded_at_plan_generation` assert the
real, reachable rejection point directly. Because "unreachable today"
is not the same as "provably correct on its own,"
`test_execute_own_regular_file_check_independently_rejects_a_directory`
uses `dataclasses.replace` to construct a doctored `PlanValidity` that
forces `check_plan_validity`'s upstream verdict to say "still valid,"
bypassing that layer specifically so the executor's own redundant
`is_file()` check can be exercised and confirmed correct in genuine
isolation - real defense-in-depth, not two checks that merely happen to
agree because they share one primitive.

**`'..'` traversal and a symlinked intermediate source directory -
confirmed already defeated, no code change required**:
`test_execute_dotdot_traversal_in_source_path_is_precondition_failed`
and `test_execute_symlinked_intermediate_directory_escape_is_
precondition_failed` prove the existing `resolve(strict=True)` +
containment check (already part of the reviewed design) correctly
rejects both, since resolving the full path both normalizes `..`
segments and follows every symlink in the chain, not just a symlinked
final component.

**`EXDEV` on the real `os.rename()` call - confirmed to land in the
existing generic failure bucket, no copy-fallback added**:
`test_execute_exdev_on_rename_is_failed` mocks `os.rename` to raise
`OSError(errno.EXDEV, ...)` and confirms it is recorded exactly like
any other same-device rename failure: `FAILED`,
`filesystem_mutation_occurred=False`, run stopped. No fallback to a
copy-then-delete was added - the constructor's own same-device check
between `allowed_root` and `quarantine_root` should make a legitimate
`EXDEV` here unreachable in normal operation, but the handling exists
for the case it somehow still fires (e.g. a destination-parent
subdirectory unexpectedly on a different device).

**New destination-parent device check**: after creating the per-
execution quarantine subdirectory and before calling `os.rename()`, the
executor now separately verifies the destination's *parent directory*
is on the same device as the quarantine root (`_device_of(destination.
parent) != self._quarantine_device`), refusing with `FAILED`/
`PRECONDITION_FAILED` if not. This was previously only an assumption;
it is now an explicit, tested check
(`test_execute_destination_parent_wrong_device_is_failed`, via a mocked
`_device_of`).

**TOCTOU: explicitly not eliminated, and stated as a future gate before
real-corpus use.** The class docstring now documents, in full: the
exact window between the last `_observe_file` re-check and the
`os.rename()` call; what an attacker could substitute into that window
(a different file with the same name at the source path); why post-move
verification prevents that substitution from ever being reported as a
false `SUCCESS` (the verification re-hashes the *destination* against
the *plan's expected hash*, so a substituted file with different
content is caught and recorded `UNKNOWN`, never `SUCCESS`); the one
residual case verification cannot catch - a substituted file with the
identical hash and size to the original - which is accepted as
out-of-scope for a same-content substitution; and an explicit statement
that no advisory locking or file-descriptor pinning was implemented for
this milestone, because the existing architecture does not yet require
it and none of section 5's instructions asked for it - but that a
stronger primitive (fd-pinning across the check-then-rename step, or an
OS-level lock) is a **required gate before this executor may ever run
against the real corpus**, not an optional future nicety.

**Verify-failure semantics, restated as an explicit table** (unchanged
behavior, now made non-ambiguous in the docstring):

| Condition | Result | `filesystem_mutation_occurred` |
|---|---|---|
| Precondition failure (symlink, hard link, containment, wrong device, missing/changed source, etc.) | `FAILED` / `PRECONDITION_FAILED` | `False` |
| Rename failure, including `EXDEV` | `FAILED` | `False` |
| Post-move verification failure (hash/size mismatch, destination is a symlink, destination missing) | `UNKNOWN` | `None` |
| Any other ambiguous filesystem outcome | `UNKNOWN` | `None` |
| Successful, fully-verified rename | `SUCCESS` | `True` |
| Any action after the run has stopped | `NOT_ATTEMPTED` | (unset) |

A known mutation is never reported as `FAILED`/`mutation=False` - that
combination is reserved exclusively for failures that occurred strictly
*before* `os.rename()` was ever called.

**No production exposure added**, exactly as scoped: no API execution
endpoint, no UI execution control, no automatic execution, no default
root discovery, no real-corpus execution, no permanent deletion, no
`PURGE`, no quarantine implementation reachable outside this synthetic
test boundary.

**Tests**: 12 new tests in `test_dedup_executor_execution.py` (30 → 42),
for a full-suite total of 529 (up from 517). Full suite run 3 times
consecutively with zero flakiness (529 passed each time); the new
concurrency test additionally run 5 times in isolation with zero
flakiness. `aibrain_test`'s dedup-related tables confirmed empty after
the final run.

**Deliberately not built this milestone**: fd-pinning or advisory
locking to close the TOCTOU window (explicitly deferred as a required
gate, not implemented here); any API/UI exposure; any change to the
quarantine-vs-permanent-delete semantics. **The real corpus was not
read, mutated, or referenced by any code or test added in this
milestone.**

### Executor Reconciliation & TOCTOU Strategy — design only, zero code changed

A design pass settling every open question the hardening milestone left
explicitly deferred, before any of it becomes an implementation
milestone. **Zero code, model, migration, service, or test changed** -
this section documents decisions only.

**1. Is the current TOCTOU containment sufficient for AI_Brain's threat
model? No - not for real-corpus use, though it is sufficient for the
system's current synthetic-only, human-supervised stage.** AI_Brain
runs as a single process with no API/UI exposure to the executor and
no untrusted multi-tenant access, so the realistic risk is not a live
adversary racing `os.rename()` - it is a low-probability but nonzero
window in which some other legitimate actor (the user's own editor, an
external backup/sync job, antivirus scanning) touches a source file in
the microseconds between the executor's final re-observation and the
move. Post-move verification already turns a *content-different*
substitution into `UNKNOWN` rather than a false `SUCCESS` (see
"Executor hardening," above) - the residual gap is narrow and specific:
a same-hash, same-size substitution in that exact window would be
recorded as `SUCCESS` even though the file the executor actually moved
was not the one it last observed. For a mutation system acting on
irreplaceable personal data, that residual is real enough to gate
real-corpus use on closing it, even though it does not block continued
synthetic-only development.

**2. What primitive should close it: inode-identity pinning via file
descriptor, not advisory locking.** The concrete design: open the
source path with `os.open()` to get a file descriptor, `os.fstat(fd)`
that descriptor to capture its `(st_dev, st_ino)` - the kernel's
ground-truth identity for "this exact inode," immune to any later
rename or replacement of the *name* - then hash the file's content by
reading through that same fd (not by re-opening the path), so the hash
that gates the decision to mutate is guaranteed to describe the literal
bytes behind that inode. After `os.rename()` completes, `os.stat()` the
destination path and compare its `(st_dev, st_ino)` against the fd's
captured identity. A match proves, independent of content, that the
object moved to quarantine is the exact inode last observed and hashed
- no substitution in the intervening window could produce a match,
because a substitution necessarily involves a different inode even
when its content happens to be byte-identical. Advisory locking was
considered and rejected: it only constrains cooperating processes, and
the actors capable of touching a file in this window (the user, an
editor, a backup tool) have no reason to participate in a lock this
system invents. Inode-pinning needs no other actor's cooperation at
all - it is a verification the executor performs entirely on its own
after the fact.

**Refinement to this primitive, made explicit before any
implementation**: the security-relevant invariant is that the object
the eventual `os.rename()` actually operates on is *proven* to be the
identical filesystem object last observed and hashed through the
pinned descriptor - not merely that some earlier `open()` succeeded.
Having a valid fd and fstat result at hash-time says nothing on its own
about what `os.rename()` moves later; the proof only exists once the
*destination's* post-rename identity is compared against that pinned
`(st_dev, st_ino)` and found to match. Equally important is the
negative case, which must be defined now rather than left implicit:
if that identity cannot be established at all - `fstat` on the pinned
fd fails, `os.rename()` itself raises, or `os.stat()` on the
destination fails or cannot be performed - or if the identities are
established but do not match, the result is always `UNKNOWN`
(`filesystem_mutation_occurred=None`), never a guessed `SUCCESS` and
never silently treated as equivalent to today's hash/size verification
alone. This is not a new result category - it is the identical
post-move-verification semantics the hardening milestone already
established (see "Executor hardening," above), with identity added as
one more independently-named condition alongside "destination exists,"
"destination is a regular file," "destination is not a symlink," and
"hash/size match" - a failure here must be reported as its own named
condition, not folded silently into the hash/size check.

**3. How `UNKNOWN` reconciles without mutating historical audit
rows: the existing designed shape, unchanged, finalized here.**
`DedupExecutionActionReconciliation` (proposed in the earlier
Executor Safety & Recovery Design, "designed, not implemented"):

```
DedupExecutionActionReconciliation
    id
    audit_id              -- FK to DedupExecutionActionAudit, UNIQUE
                           --   (at most one reconciliation per audit)
    verified_result        -- SUCCESS or FAILED only
    verified_by             -- free text, mirrors authorized_by
    verification_method     -- free text: HOW it was verified
    observed_content_hash   -- the reconciler's OWN re-observation,
                             --   captured independently of the human's
                             --   claim (see point 4)
    observed_file_size
    verified_at
    created_at
```

Purely additive: the original `DedupExecutionActionAudit` row is never
edited, and `DedupExecution.status` (`NEEDS_REVIEW` included) is never
retroactively rewritten - a reconciliation answers "what do we now
believe happened," not "what should history have said."

**4. How a human-verified outcome becomes a reconciliation record: a
new `reconcile_action(audit_id, verified_result, verified_by,
verification_method)` service method that corroborates the human's
claim against the system's own fresh, independent re-observation rather
than trusting it blindly.** Preconditions: the audit must exist, its
`result` must currently be `UNKNOWN` (a definite `SUCCESS`/`FAILED`/
`PRECONDITION_FAILED`/`NOT_ATTEMPTED` never needs reconciling), and no
reconciliation may already exist for it (mirroring
`record_action_result`'s own one-outcome-per-action pattern). The
method then calls the existing, non-mutating `_observe_file` on the
audit's own `source_path` - exactly the same read `recover_stale_
execution` already performs - and requires the live observation to be
*consistent* with the claimed `verified_result` before recording it:
`SUCCESS` requires the source path to currently not exist (a delete
genuinely cannot have happened if the file is still there); `FAILED`
requires the source path to currently exist, matching the audit's own
`expected_content_hash`/`expected_file_size` (nothing happened to the
original). A claim inconsistent with what the system can itself
observe right now is refused (`ValueError`), not silently recorded -
the same "never trust an unverifiable claim" posture the whole executor
is built on, now applied to the human's own investigation rather than
only to the executor's. The independent observation is stored on the
reconciliation row alongside the human's `verification_method`, so a
later reader sees both what the human reported and what the system
itself confirmed at that moment.

**5. Effect on subsequent planning: none, by design - already true
today, unchanged by reconciliation's existence.** `generate_plan_for_
review`'s live-file-existence exclusion (see "Execution Recovery &
Partial-Replanning Design") re-observes the *actual* filesystem at
plan-generation time regardless of whether any reconciliation row
exists - a member whose file is gone is excluded whether that fact is
reconciled or not. Reconciliation exists purely for human auditability
and confidence in the historical record; the live check remains the
sole authority for what a new plan may safely propose, and stays that
way with this design.

**6. Can reconciliation ever auto-authorize another execution: no.**
`reconcile_action` performs no filesystem operation, creates no plan,
authorization, or execution, and does not change `DedupExecution.
status`. A new execution against the same review always requires the
full explicit chain unchanged by this design - fresh plan generation,
fresh authorization, fresh `start_execution` - regardless of whether
the earlier `UNKNOWN` was ever reconciled. This is the same principle
underlying every gate built so far: **authorization grants permission
to execute a specific immutable plan; it does not grant permission to
reinterpret the plan or discover new filesystem targets** - and
reconciliation, being purely forensic, must never become a path around
that.

**7. Can an executor crash be recovered without manual database
surgery: yes today for the common case, with one genuine gap this
design pass surfaced.** `recover_stale_execution` already closes out a
crashed `RUNNING` execution with no manual SQL - it re-observes every
unresolved planned action and finalizes via `complete_execution`,
requiring no knowledge of the new `claimed_at` field at all: once
`complete_execution` moves the execution out of `RUNNING`,
`_claim_execution`'s own first check (`status != RUNNING`) permanently
forecloses any future claim on that execution, exactly matching "one
execution per authorization, permanently." **The gap**: `recover_
stale_execution` does not itself take a `SELECT ... FOR UPDATE` claim
before reading and writing - if the "crashed" executor were actually
still alive (a false assumption, not a truly dead process; AI_Brain has
no process supervision to rule this out) and resumed writing action
results at the same moment a human called `recover_stale_execution`,
both could race to record a result for the same plan action, which
today would surface as a raw `IntegrityError` from the audit table's
own unique constraint rather than the clean, deliberate refusal the
executor's own `_claim_execution` now provides. **Recommendation for
the next implementation milestone**: `recover_stale_execution` should
take the same `SELECT ... FOR UPDATE` claim the executor now does
before proceeding, for exactly the same reason - recovery is a second
way to reach the RUNNING execution's action-writing path, and it
deserves the same exclusivity guarantee the hardening milestone gave
the primary path.

**Deliberately not built this pass**: `DedupExecutionActionReconciliation`
itself (model, migration, service, API, tests - this is decisions only);
fd-pinning implementation; the `recover_stale_execution` locking fix
identified in point 7; any API/UI exposure; any real-corpus execution.

### Reconciliation & TOCTOU implementation

Implements exactly the six-point scope of the design pass above -
`DedupExecutionActionReconciliation`, its service, inode/device identity
pinning inside the executor, `recover_stale_execution` locking parity,
adversarial/concurrency tests, and this documentation. **Zero unrelated
change**: no API/UI exposure, no automatic authorization, no permanent
deletion, and the real corpus was not read, mutated, or referenced by
any code or test added.

**1. `DedupExecutionActionReconciliation`** (`app/models/dedup_
execution.py`, migration `ef65da409302`) - exactly the shape the design
pass settled: `audit_id` (FK, `UNIQUE` via `uq_dedup_execution_action_
reconciliations_audit_id` - one reconciliation per audit, permanently),
`verified_result` (reuses the existing `dedup_execution_action_result`
Postgres enum rather than inventing a near-duplicate two-value type;
restricted to `SUCCESS`/`FAILED` at the service layer only, matching
this codebase's established convention of enforcing definitional
invariants in Python rather than via database `CHECK` constraints - see
`record_action_result`'s own tri-state `filesystem_mutation_occurred`
handling), `verified_by` (required, unlike the nullable `authorized_by`
it otherwise resembles - a reconciliation exists specifically to record
who made the judgment call, so leaving that optional would undermine
the record's own purpose), `verification_method` (free text, never
parsed), and the service's own `observed_content_hash`/
`observed_file_size` (captured independently of the human's claim - see
point 2). The same migration adds `DedupExecution.recovery_claimed_at`
for point 4.

**2. `DedupReconciliationService`** (`app/dedup/reconciliation_
service.py`) - `reconcile_action(audit_id, verified_result, verified_by,
verification_method)`. Preconditions, `ValueError` on every failure, no
row created: `verified_result` must be exactly `SUCCESS` or `FAILED`;
the audit must exist and currently be `UNKNOWN`; no reconciliation may
already exist for it. The corroboration step the design pass specified
is real, not decorative: the service calls the same non-mutating
`_observe_file` the executor and `recover_stale_execution` already use
on the audit's own `source_path`, and requires that fresh observation
to be CONSISTENT with the claim before recording anything - `SUCCESS`
requires the source to currently not exist; `FAILED` requires it to
exist with a hash/size still matching `audit.expected_content_hash`/
`expected_file_size`, the plan's original pre-mutation values. A claim
inconsistent with what the system can itself observe right now is
refused outright. The observation's own hash/size - not anything parsed
from `verification_method` - is what gets stored on the row. Not
guarded by `SELECT ... FOR UPDATE`: reconciliation is a rare, single-
human action, not a systemic concurrency surface, and a genuine race
would surface as a raw `IntegrityError` from the `audit_id` unique
constraint rather than a clean refusal - a known, documented residual,
not silently unhandled, and out of this milestone's scope to close.

**3. TOCTOU closure via inode/device identity pinning**
(`app/dedup/executor.py`) - `_pin_and_hash(path)` opens the source with
`os.open()`, captures `(st_dev, st_ino)` via `os.fstat()` on that
descriptor, and hashes the content by reading through that SAME
descriptor (never a second, separate `open()`) - replacing the prior
second `_observe_file` re-observation in the per-action pipeline. The
descriptor's own `fstat` mode is checked for `S_ISREG` at the tightest
possible point, rather than trusting the earlier, separate path-based
`is_file()` check from Step 7. The source/quarantine device check
(Step 12) now uses this pinned identity's device rather than the
earlier path-based `stat()`, the freshest information available at
that point. After `os.rename()`, `os.stat(destination, follow_symlinks=
False)` reads the destination's own identity (never following a
symlink at the destination itself) and compares it against the pinned
one - a match proves, independent of content, that the object moved is
the exact inode pinned and hashed immediately before the call. This is
added as its own independently-named post-move check, alongside the
pre-existing destination-exists/regular-file/not-symlink/hash-match
checks (see "Executor hardening," above) - any failure to establish or
match identity is `UNKNOWN`, never folded silently into the hash/size
check and never guessed as `SUCCESS`. The class docstring's TOCTOU
section is rewritten to describe this as CLOSED, not merely minimized -
the residual same-hash/same-size substitution case the earlier design
explicitly accepted is now always caught by identity, proven for real
(not merely reasoned about) by `test_execute_identity_substitution_
with_identical_content_is_unknown`, which performs a genuine
`os.replace()` substitution with byte-for-byte identical content inside
the real race window and confirms the result is `UNKNOWN`.

**4. `recover_stale_execution` locking parity**
(`app/dedup/execution_service.py`) - a new `_claim_for_recovery` helper,
structurally identical to the executor's own `_claim_execution`:
`SELECT ... FOR UPDATE` on the `DedupExecution` row, refusing
(`ValueError`, never a raw `IntegrityError`) if the execution is not
`RUNNING` or if `recovery_claimed_at` is already set, otherwise setting
it and committing. Deliberately a SEPARATE field from `claimed_at`: an
execution eligible for recovery may never have been claimed for
execution at all (it can crash between `start_execution` and the
executor's first `_claim_execution` call), so gating recovery on
`claimed_at` would wrongly block recovering that case. Documented
residual, unchanged from the design pass: this protects against two
concurrent `recover_stale_execution` calls, not against a "stale"
execution whose original executor is actually still alive and calling
`record_action_result` directly outside this method - that remaining
race is bounded only by the audit table's own unique constraint, and
fully closing it would require restructuring `record_action_result`'s
per-call commit behavior, out of scope here.

**5. Tests** - 18 new: 4 direct unit tests of `_pin_and_hash` itself
(hash/identity correctness, missing-file and directory rejection, two
files with identical content proven to have different inodes); 3
full-pipeline adversarial/edge-case tests in the executor suite (the
genuine substitution attack described above; destination identity
becoming entirely unstat-able after a real rename, proven to still
report `UNKNOWN` with identity named among the reasons even though
every other destination-based check fails for the same underlying
reason - pathlib's own `is_symlink()`/`is_file()` share the identical
`os.stat` call this identity check uses, so true isolation of "only
identity fails" is not a representable real scenario; and the source
vanishing immediately before `_pin_and_hash` itself, correctly
producing `PRECONDITION_FAILED` rather than an uncaught exception); 10
new `DedupReconciliationService` integration tests (both happy paths,
missing/non-`UNKNOWN`/duplicate audit rejection, `verified_result`
restricted to `SUCCESS`/`FAILED`, and - the corroboration requirement's
actual teeth - both contradictory-claim directions refused: `SUCCESS`
claimed while the source still exists, and `FAILED` claimed while the
source is gone OR has changed content); 1 new concurrent-recovery test
in the audit suite, structurally identical to the executor's own
concurrency test (two real threads, two separate database sessions, a
`threading.Barrier`, a slowed commit to force genuine contention),
proving exactly one of two simultaneous `recover_stale_execution` calls
on the same execution wins the claim and the other refuses cleanly.
Full suite: 547 tests (up from 529), run three times consecutively with
zero flakiness; the new concurrent-recovery and identity-substitution
tests additionally run five times each in isolation with zero
flakiness. `aibrain_test`'s dedup-related tables, including the new
`dedup_execution_action_reconciliations`, confirmed empty after the
final run. **No test in this suite references, reads, or could reach
the real personal corpus** - every mutation happened inside disposable
`tmp_path` pairs.

**Deliberately not built this milestone**: any API endpoint or router
wiring for reconciliation or the executor; any default or configured
production root; permanent deletion; `SELECT ... FOR UPDATE` locking
for reconciliation itself (documented residual, see point 2); any
frontend. **The real corpus was not read, mutated, or referenced by any
code or test added in this milestone.**

### execute() vs recover_stale_execution() race - safe convergence, not mutual exclusion

**The invariant this fix establishes, stated precisely (get this
wording right, since it matters for future architecture reviews): at
most one action-result audit is ever persisted for a given plan action,
and any concurrent loser converges to the existing terminal state
without leaking a raw database exception. This is NOT the same claim
as "execution and recovery can never overlap" - they still can, and
this fix does not change that. What changed is what happens next when
they do.**

Immediately after the reconciliation/TOCTOU milestone above, review of
its own documented residual ("`recover_stale_execution` does not itself
take a `SELECT ... FOR UPDATE` claim... unlike a still-alive original
executor") surfaced a sharper question: `_claim_execution` checks/sets
`claimed_at`; `_claim_for_recovery` checks/sets the entirely
independent `recovery_claimed_at`. Each is individually protected by
its own row lock, but **nothing checks the other column** - so a
genuinely still-running `execute()` call and a `recover_stale_
execution()` call can both be legitimately granted their claim for the
SAME still-`RUNNING` execution, and both can then reach `record_
action_result` for the SAME unresolved plan action. That overlap
itself is NOT prevented by anything below - only its consequence is
made safe.

**Proven, not just reasoned about**: a synchronized two-thread probe
(both callers' `record_action_result` calls gated behind a shared
`threading.Barrier` so their underlying INSERTs genuinely race at the
database level, not merely close in wall-clock time) reproduced a raw
`psycopg2.errors.UniqueViolation` `IntegrityError` propagating
**uncaught** out of `recover_stale_execution` in roughly 2 of 5 runs
before this fix - confirming the gap was real, exactly the "raw
IntegrityError as the normal concurrency outcome" failure mode the
entire hardening effort exists to eliminate.

**Why this can't be closed by a stronger claim predicate**: making
`_claim_for_recovery` require `claimed_at IS NOT NULL` does not help -
that is true in essentially every realistic call to `recover_stale_
execution` (the whole point is recovering an execution that already
started), so it would not prevent the dangerous case, only the
harmless one. Genuinely preventing the overlap would require knowing
whether the original `execute()` caller is still alive - process
supervision or a lease/heartbeat mechanism this codebase deliberately
does not have, and introducing one would be a real architecture
expansion, not a "smallest correction."

**The fix actually applied**: make the CONSEQUENCE of the overlap
always safe, rather than trying to make the overlap itself impossible.
Two new exception types in `execution_service.py`, both still
`ValueError` subclasses (every existing `except ValueError`/
`pytest.raises(ValueError, ...)` call site keeps working unchanged):

- `DuplicateActionResultError` - `record_action_result` now catches the
  database's own `IntegrityError` around its commit and converts it
  into this SAME exception its pre-insert check already raises for the
  "checked and found existing" case, so every caller sees one
  consistent, well-typed failure regardless of which layer caught the
  collision.
- `AlreadyFinalizedError` - `complete_execution`'s existing "not
  RUNNING" precondition now raises this specific subclass.

`recover_stale_execution`'s per-action loop now tolerates `Duplicate
ActionResultError` on any single action (a concurrent caller already
recorded that one first - not this call's problem to solve, so it
moves on to the next unresolved action rather than aborting the whole
attempt), and its final `complete_execution` call tolerates `Already
FinalizedError` (a concurrent `execute()` already finalized this
execution first - a success from recovery's own perspective too, since
the execution DID reach a terminal state; it returns that
already-finalized execution rather than raising). `DedupFilesystem
Executor.execute`'s own final `complete_execution` call is made
symmetric for the same reason. Neither caller can be left permanently
stuck `RUNNING` with its claim column set and no legal way to retry.

**Verified**: the exact synchronized-barrier probe that reproduced the
raw `IntegrityError` before the fix now completes cleanly on every
run - zero exceptions from either caller, exactly one audit row for
the one plan action, and both callers' returned `DedupExecution`
objects agreeing on the same final status - across 10 consecutive
isolated runs plus every run of the full suite. This is now a
permanent regression test
(`test_execute_vs_recover_stale_execution_race_never_corrupts_state`).

**Corpus hygiene guard updated**: the source-scan tests now also check
for the two real, confirmed T7 paths (the canonical path the user
named as protected, and the drive that happens to be actually mounted
with real personal data - one substring check covers both, since the
mounted variant's name is the canonical one plus a trailing "1"),
alongside the earlier `/mnt/t7ssd`-based reference this project's
memory once incorrectly protected. Neither T7 path was scanned,
listed, or accessed to verify this - the checks are pure string
containment against this project's own source files.

**Deliberately not built this pass**: any process-supervision or
lease/heartbeat mechanism (the only way to prevent the underlying claim
overlap itself, as opposed to making its consequences safe - explicitly
out of scope, not silently deferred); any API/UI exposure; any
real-corpus execution.

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

Four views live in the same single page, toggled client-side via a
shared `showView(name)` helper and a `VIEWS` registry (no client-side
router, no separate HTML files) - Chat, a read-only Import Jobs
monitor, a Memory Review queue, and a Dedup Review queue.

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

### Dedup Review view (frontend)
The fourth view (`nav-dedup-review-btn`), added alongside the backend
API in the same milestone as the `DuplicateReview` decision model's API
(see "Dedup review decision model," under Deduplication). Reuses
`GET /dedup/reviews?status=`, `POST /dedup/reviews/{id}/approve`, and
`POST /dedup/reviews/{id}/reject` verbatim - no dedup arbitration logic
lives in JavaScript, only display and the explicit approve/reject
action.

Same filter-tab/card pattern as Memory Review, plus two things unique
to this view: a persistent safety banner ("Reviewing this finding does
not modify your files") always visible at the top, and no delete/move/
quarantine control anywhere - matching that no such capability exists
in the API either. EXACT and NEAR findings get a distinct match-type
badge (`.match-type-exact` purple, `.match-type-near` orange) in
addition to differing recommendation text, so the two are visually
unmistakable at a glance, not just distinguishable by reading the card.

The canonical-choice UI is built to mirror the backend's validation
exactly, not just describe it: a radio group lists every member
document by name; for an EXACT review there is no default selection
and the Approve button starts `disabled`, only enabling once a real
member is chosen (`change` listeners on the radios toggle it); for a
NEAR review a "No canonical (undecided)" option is pre-selected by
default, since approving without one is the expected, common outcome
for an ambiguous finding, not an edge case to route around. The
"System recommendation" and "Human-approved canonical" fields are
rendered as two separate `<dt>/<dd>` rows side by side, always both
visible, so a reviewer can never mistake one for the other - text like
"a suggestion only, not a decision" is attached directly to the
recommendation, not left implicit.

Approve/Reject use the same in-page two-click confirmation
(`createConfirmableActionButton`) introduced for Memory Review - see
that section for why a native `window.confirm()` was rejected.

Verified end-to-end in a real browser: created a controlled synthetic
EXACT pair and NEAR pair directly via `DedupReviewService` (not the
real 720GB corpus), confirmed both rendered with the correct badges,
recommendation text, and canonical-choice defaults; confirmed the EXACT
review's Approve button was disabled until a canonical was picked;
approved the EXACT review with an explicit canonical and rejected the
NEAR review; confirmed both moved to the correct filter tab and that
the underlying `Document`/`ImportJob` rows were completely unmodified
afterward; and reconfirmed Chat, Import Job Monitoring, and Memory
Review all still work unaffected by this addition.

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

## T7 Corpus Discovery / Inventory

A deliberately separate track from both document ingestion and the
dedup filesystem executor: read-only understanding of what is actually
on the user's real T7 backup drive, gated and authorized independently
of any mutation-capable milestone. Authorized only after the dedup
execution/recovery milestone chain (`1868273` → `52e991a` → `1505f40`
→ `462510b` → `e5f5350` → `0ef1d92`) was explicitly closed - this is a
genuinely new, distinct gate, not a continuation of that chain.

**`app/discovery/corpus_inventory.py`** - the only code this milestone
adds. `scan_corpus(root, *, top_n=20) -> CorpusInventory` walks `root`
via `os.walk(topdown=False, followlinks=False)`, reading only
`os.lstat` metadata for each entry - **no file is ever opened, read,
hashed, or extracted**, and `lstat` (not `stat`) means a symlink is
measured by its own size, never the size of whatever it points to;
`followlinks=False` means a symlinked directory is never descended
into, closing off any possibility of a symlink loop or an escape
outside the scanned root. This module is NOT wired into
`SourceScanner`, `DocumentIngestor`, or any other part of the existing
ingestion pipeline, and never touches the database - a clean, standalone
module with zero coupling to anything that could mutate a file.

Resilient by design for an external, foreign drive of unknown health:
a directory that can't be listed (`os.walk`'s own `onerror` hook) or a
file whose `lstat` fails (permission denied, a vanished entry, a
device hiccup) is recorded in the returned inventory's `errors` list
and skipped - the scan itself never raises over one bad entry, since
aborting a multi-hundred-gigabyte inventory over a single unreadable
file would defeat the entire point.

Memory-bounded by construction: the full per-file list is never held
in memory. Aggregate counts (`total_files`, `total_size_bytes`,
`extension_counts`, `extension_size_bytes`, `archive_counts`) are
plain running totals; the two rankings (`largest_files`,
`largest_directories`) are each backed by a bounded min-heap
(`_TopNTracker`) holding only the `top_n` largest entries seen so far -
safe regardless of how many files the corpus actually contains.
Directory sizes are computed bottom-up in the SAME single pass:
`os.walk(topdown=False)` yields every directory only after all of its
subdirectories, so each directory's total is simply its own files'
sizes plus the already-computed totals of its immediate children,
recorded in a `dict[str, int]` keyed by path - no second pass over the
tree is needed.

**Archive classification is extension-based only, deliberately not
signature-verified, for this first pass** - `KNOWN_ARCHIVE_SUFFIXES`
(`.zip`, `.7z`, matching `app.ingestion.document_ingestor.
ARCHIVE_CONTAINER_SUFFIXES` - duplicated as a constant rather than
imported, to keep this read-only module fully decoupled from the
extraction-capable ingestion module) vs. `OTHER_ARCHIVE_SUFFIXES`
(`.rar`, `.tar`, `.gz`, `.tar.gz`, `.iso`, and other common archive
extensions this personal backup corpus might contain that AI_Brain
cannot yet extract) - reported separately so a human can see the gap
between "what is actually on the drive" and "what this project can
currently act on." Verifying an archive's actual signature (`is_
zipfile`, `py7zr.is_7zfile`) would require opening each candidate file
- more than a "cheap first inventory" calls for; a natural candidate
for a later, explicitly-authorized deeper-inventory phase, not
something this milestone does silently.

**Reports never committed to version control**: `write_inventory_
report(inventory, destination)` writes plain JSON, and the
conventional destination - `knowledge/t7_discovery/` - is listed in
`.gitignore`, since a real inventory's `largest_files`/`largest_
directories` rankings necessarily contain real personal file and
directory names from the user's own backup drive.

**Runner**: `scripts/t7_discovery.py <root> <output.json> [--top-n N]`
- a thin CLI wrapper with no logic of its own beyond argument parsing,
timing, and calling `scan_corpus`/`write_inventory_report` directly.

**Tests**: 16 new, all synthetic-`tmp_path`-only (no real T7 access in
any test) - counting, extension/archive classification (including the
two-part `.tar.gz`-style extensions), bottom-up directory totals,
`top_n` bounding (including `top_n=0` disabling the rankings without
affecting aggregate counts), a mocked `lstat` failure proving the scan
doesn't abort over one unreadable file, a real `chmod 000` directory
proving the same for an unlistable directory, a real symlink proving
`lstat`-not-`stat` semantics, a real symlinked directory proving
`followlinks=False`, and JSON report round-tripping.

**Deliberately not built this milestone**: any content hashing,
archive-signature verification, or archive extraction; any wiring into
`DocumentIngestor`/`SourceScanner`/the database; any deduplication
analysis of what the scan finds; any mutation of any kind. **The T7
was scanned read-only, exactly as authorized - no file on it was
opened, moved, renamed, deleted, or otherwise modified.**

## T7 Deduplication Analysis (Phase D1) — read-only

A separate, explicitly-authorized follow-on gate to T7 Corpus Discovery
above, opened only after that milestone was independently, critically
re-verified and committed (`ee043c6`). Authorization scope: reading
file metadata AND content for hashing, reading directory structure,
comparing files/directories, producing analysis reports - explicitly
NOT authorizing delete, move, rename, extract, quarantine, or any
deduplication execution.

**Read this before acting on any D1 finding - seven facts that must
stay explicit, not merely implied by the code:**

1. **D1 was performed against a LIVE corpus.** The real T7 run
   executed for 76.6 minutes while Syncthing was independently,
   actively modifying the corpus throughout (confirmed running before,
   during, and after the run) - see "Live-corpus semantics" below for
   exactly what that does and does not affect.
2. **D1 produces read-only forensic evidence, not a deletion
   recommendation.** It establishes that specific files are
   byte-for-byte identical (or that specific directory trees match
   structurally); it does not, and cannot, determine which copy - if
   any - is safe to remove. A file's backup/provenance significance is
   outside what a hash comparison can ever tell you.
3. **"Reclaimable bytes" is a formula, not a promise.** It is
   `size_bytes * (copies - 1)` per group - "the space freed if every
   group kept exactly one copy" - never "space confirmed safe to
   recover." Treat it as an upper-bound estimate under an assumption
   (keep-one-arbitrary-copy) that D1 itself never validates.
4. **The exact-file and directory-structural reclaimable totals must
   never be added together.** They are two different lenses over
   overlapping data (see `DuplicateAnalysis`'s own docstring) - summing
   them overstates genuinely reclaimable space, likely substantially.
5. **`.c9r` Cryptomator-chunk duplicates remain semantically separate**
   from ordinary plaintext/document duplicates, always - a same-hash
   `.c9r` pair means ciphertext-identical, not confirmed-identical
   plaintext, and a group is only classified this way if EVERY member
   ends in `.c9r`.
6. **The `size_collision_candidate_files` vs `files_hashed` gap (624,847
   vs 581,414 in the real run) is entirely, exclusively explained by
   zero-byte files** sharing a size with at least one other zero-byte
   file - they count as "candidates" but are never hashed (hashing an
   empty file is pointless; it reclaims zero bytes). No other code path
   causes a candidate to skip hashing - proven with a dedicated
   synthetic test, not merely asserted.
7. **Memory usage scales linearly with corpus size, intentionally.**
   Unlike `corpus_inventory.py`'s bounded top-N rankings, D1's size
   index and directory signatures hold one entry per file/directory -
   an accepted, necessary tradeoff (genuine duplicate detection cannot
   be done from a bounded sample), observed practical on the real
   ~628,000-file corpus (peak memory in the low hundreds of MB).

**`app/discovery/duplicate_analysis.py`** - the only new production
code. `analyze_duplicates(root)` runs two passes:

1. **Metadata-only** (comparable cost to `corpus_inventory.scan_
   corpus`): one `os.walk(topdown=False)` traversal builds a full
   size index (every file's path, grouped by `os.lstat` size) AND,
   bottom-up in the SAME pass, a structural signature per directory
   (a SHA-256 digest over each directory's own sorted `(filename,
   size)` pairs plus its immediate subdirectories' already-computed
   signatures - so a signature match implies identical names and
   sizes recursively throughout the entire subtree). **`corpus_
   inventory.py`'s own `inventory.json` does not retain a full
   per-file listing** (only a bounded top-N via `_TopNTracker`), so
   this could not literally reuse that report as a full index the way
   the recommended plan first assumed - this module performs its own
   independent full-corpus metadata pass instead, noted here rather
   than silently deviating from the stated plan.
2. **Content-reading, the expensive part**: only files that share
   their size with at least one other file (a real "collision
   candidate") are opened read-only and SHA-256 hashed - a file with a
   globally unique size can never be an exact duplicate of anything
   and is never opened. Hashing mirrors `document_ingestor._hash_file`
   exactly (1MB chunks, `"rb"` mode, `None` on `OSError` rather than
   raising).

**Three distinct kinds of findings, kept separate rather than merged**:
- `exact_duplicate_groups` - 2+ files with identical size AND SHA-256
  hash. Zero-byte files are excluded (trivially "identical," reclaim
  nothing, would otherwise dump every empty file in the corpus into
  one meaningless group).
- `cryptomator_chunk_duplicate_groups` - the SAME exact-hash-match
  logic, but reported separately whenever EVERY member of a group ends
  in `.c9r` (Cryptomator's encrypted-chunk extension). A same-hash
  `.c9r` pair is ciphertext-identical, not necessarily "the same user
  document appears twice" the way a repeated `.jpg` is - explicitly
  not folded into the ordinary category so a human reviewer doesn't
  mistake vault-internal chunk duplication for an obvious deletion
  candidate. A group containing even one non-`.c9r` member is
  classified as an ordinary exact duplicate instead, never silently
  absorbed into this category.
- `directory_duplicate_groups` - 2+ directories whose entire subtree
  matches by name and size, recursively - a STRUCTURAL signal
  corroborating, but not a substitute for, the per-file hash evidence
  above. Empty directories are excluded from this category (same
  reasoning as zero-byte files). When an entire tree is duplicated
  (both a parent and its children all match), only the TOPMOST match
  is reported - every nested sub-match is redundant noise from the
  same underlying duplicate tree, though the parent-level group's own
  `total_size_bytes`/`file_count` already include everything beneath
  it.

**Archives treated as plain files for this pass**, exactly as scoped:
a `.zip`/`.7z`/etc. participates in the same size→hash comparison as
any other file (two identical archives are found as an ordinary exact
duplicate); no archive is opened, inspected, or extracted internally.

**`write_duplicate_report`** reuses the SAME destination-safety
invariant as the discovery module - factored out into a new shared
`app/discovery/safety.py` (`reject_destination_inside_root`) rather
than duplicating that safety-critical check a second time.
`corpus_inventory.write_inventory_report` was refactored to use this
same shared helper; its own existing test suite was re-run and passed
unchanged, confirming the refactor didn't regress already-committed,
already-hardened behavior.

**Runner**: `scripts/t7_duplicate_analysis.py <root> <output.json>`.

**Tests**: 22 new (17 for `duplicate_analysis.py`, 5 for the extracted
`safety.py` helper) - all synthetic `tmp_path`, no real T7 access.
Covering: exact-duplicate detection, the size-collision-only hashing
optimization (a uniquely-sized file is never opened), zero-byte
exclusion, Cryptomator-chunk classification (including the mixed-group
non-absorption case), directory-tree structural matching (including
the require-matching-sizes-not-just-names case and the topmost-only
nested-match reporting), empty-directory exclusion, resilience to an
unreadable file (mocked) and an unlistable directory (real `chmod
000`), and the destination-safety invariant (including a sibling
directory sharing a name PREFIX, proving the check is genuinely
path-resolution-based, not a naive string-prefix comparison).

**Deliberately not built this milestone**: any deletion, move, rename,
extraction, quarantine, or deduplication execution of any kind; any
archive-internal inspection; any wiring into the database or the
existing dedup executor pipeline; any automatic decision-making from
these findings. **The T7 was only ever opened read-only (`"rb"` mode)
for hashing - no file on it was written to, truncated, moved, renamed,
deleted, or otherwise modified.**

**Review pass, before committing - one real correctness fix, two
documentation clarifications.** Requested explicitly before treating
this milestone as done, rather than accepting the real-run numbers at
face value:

- **Fix**: `_hash_file` opens a path in `"rb"` mode, which unavoidably
  follows a symlink to its target. A symlink's `os.lstat` size (its own
  tiny size) is unrelated to its target's actual size - if that tiny
  size happened to collide with an unrelated regular file's size, this
  module would have silently hashed an arbitrary amount of the
  symlink's TARGET content while reporting a `size_bytes` reflecting
  only the symlink's own size, a real data-integrity mismatch. Fixed
  by excluding symlinks from the hash-candidate size index entirely
  (still counted in `total_files_considered`, still included in their
  directory's structural signature by their own lstat size, exactly
  like `corpus_inventory.py` treats them) - proven with a synthetic
  reproduction before the fix (showing `_hash_file` on a symlink path
  really does return the target's content hash) and two new tests
  after. **The completed real-corpus run was never affected by this
  bug**: exFAT (the T7's filesystem) cannot represent symlinks at all,
  so the triggering condition was structurally impossible on the
  actual data analyzed - confirmed, not assumed.
- **The two reclaimable totals must never be summed**: `exact_
  duplicate_reclaimable_bytes` and `directory_duplicate_reclaimable_
  bytes` are two different lenses over overlapping data (a file inside
  a duplicated directory tree is very likely also counted in the
  file-level total), not two independent pools of unique bytes - now
  stated explicitly in `DuplicateAnalysis`'s own docstring, since
  nothing in the code previously stated this even though nothing in
  the code ever summed them either.
- **Memory usage is not bounded** the way `corpus_inventory.py`'s top-N
  rankings are - the size index and directory signatures scale
  linearly with the corpus's file/directory count, an accepted,
  necessary tradeoff for genuine duplicate detection (a bounded sample
  cannot find all duplicates), observed practical on the real
  ~628,000-file corpus (peak memory in the low hundreds of MB) - now
  stated explicitly rather than left implicit.
- **Live-corpus semantics documented explicitly**: the real T7 run
  (76.6 minutes) executed while Syncthing was independently, actively
  modifying the corpus throughout - each file's size (pass 1) and
  content hash (pass 2, run strictly after pass 1 completes for the
  entire tree) can reflect two different points in real time, not one
  atomic snapshot. Every individual hash comparison remains internally
  valid; the overall analysis should be read as "assembled from reads
  spread across the run's duration," not "the corpus at one instant."
  Directory-level structural signatures are internally consistent with
  each other (one single metadata pass) but are not independently
  content-hash-verified for every file within.
- **`Archive 2/vscode/data` does not match as one single top-level
  tree** - its own bottom-up signature diverges from `vscode/data`'s
  somewhere in the full recursive listing, so instead of one giant
  match, 361 separate directory-duplicate groups have at least one
  path under that subtree (`Obsidian`, `media library`, `Syncthing`,
  `Documents`, and numerous `takeout-*` folders each matching their
  counterparts elsewhere individually) - a materially more nuanced
  finding than "the whole 211.83GB tree is duplicated," reported here
  rather than the simpler but less accurate claim.
- 3 new tests added by this review pass (the exact `size_collision_
  candidate_files` vs `files_hashed` counting-gap mechanism, and two
  proving the symlink fix), full suite re-run and passed unchanged.

## T7 Provenance-Aware Duplicate Analysis (Phase D2) — read-only

A third, separate T7 gate, opened only after Phase D1 (`64887ba`) was
committed. Purpose: understand WHY the duplicates D1 found exist -
which look like distinct backup/export events, which show a naming
asymmetry consistent with (but not proof of) a current-vs-historical
relationship, which look intentionally independent, which are simply
ambiguous - never to decide what should be deleted.

**`app/discovery/provenance_analysis.py`** - the only new production
code. Deliberately split into two independent stages so the expensive,
T7-touching part never needs repeating just because the classification
logic changes: `collect_path_signals(d1_report_path)` (the ONLY stage
that touches the filesystem - a targeted, read-only `os.lstat` on
exactly the paths D1 already named, never a fresh directory walk) and
`classify(raw_data)` (a PURE function over already-collected data).
`analyze_provenance()` runs both back to back for a fresh D1 report;
`load_raw_from_previous_report()` + `classify()` re-derives a corrected
analysis from an ALREADY-COMPLETED D2 report's own raw per-path data,
with ZERO filesystem access - exactly the capability this milestone's
own review pass needed and used (see below).

**Review pass, before committing - a real naming/epistemics defect
found and corrected, not merely a style pass.** The first version's
classification codes doubled as both "which evidence pattern fired"
AND "what that means" - e.g. `current_plus_historical` for "one
copy's path carries a date/keyword, the other doesn't." That name
itself asserts a temporal claim (something IS current, something IS
historical) that the evidence does not support: a path lacking a
dating convention is not proof a file is in active use - it may simply
never have been placed in a dated folder. Corrected by separating the
concerns completely:

- **`inference_code`** now names only the EVIDENCE PATTERN, never the
  interpretation: `DIVERGENT_DATE_SIGNAL`, `PARTIAL_DATE_OR_KEYWORD_
  SIGNAL` (was `current_plus_historical`), `UNIFORM_KEYWORD_NO_
  FURTHER_SIGNAL` (was `same_backup_generation`), `NO_PROVENANCE_
  SIGNAL` (was `possibly_intentionally_independent`), `STALE_
  REFERENCE_UNVERIFIABLE` (was `insufficient_current_evidence` - itself
  renamed again after a first review pass fix still contained the word
  "current"). A test (`test_inference_codes_do_not_assert_current_or_
  historical_in_their_name`) asserts none of these codes contain
  `current`, `historical`, or unqualified `backup`.
- **Confidence downgraded where the evidence genuinely doesn't support
  more**: `PARTIAL_DATE_OR_KEYWORD_SIGNAL` (the exact pattern the
  review targeted) moved from `medium` to `low`, with `requires_human_
  review` now `True` for it - a single asymmetric naming signal is
  meaningfully weaker than `DIVERGENT_DATE_SIGNAL`'s two independently-
  plausible, differing calendar dates, which remains the only
  `medium`-confidence code. On the real corpus this moved 50,970
  groups from "medium, not flagged" to "low, flagged for review."
- **Prose is rendered separately, never stored per group**:
  `INFERENCE_PROSE` is a module-level lookup from `inference_code` to
  hedged human-readable text (e.g. `PARTIAL_DATE_OR_KEYWORD_SIGNAL`'s
  prose explicitly states "the absence of a date or keyword... is NOT
  proof that a file is currently in active use" and "does not
  establish which copy, if any, is 'current'"). Report generators
  render prose on demand; the compact JSON stores only the short code.

**The required distinction stays three explicit fields, never
collapsed**: `structured_facts` (OBSERVED FACT - a `StructuredFacts`
dataclass: counts of dated/undated/keyword-bearing paths, the actual
distinct date strings found, depth range - aggregated rather than
repeated per path), `inference_code` + its `INFERENCE_PROSE` rendering
(INFERENCE), and `confidence` (`medium`/`low`, reflecting how many
independent signals agree, never certainty).

**Signals used** (all path-text or metadata, nothing content-based):
date-like tokens (DDMMYYYY, this corpus's own convention, tried first;
YYYYMMDD fallback for Google-Takeout-style names; both range-validated
before acceptance), historical/backup keywords (`archive`, `backup`,
`as a copy of`, `old`, `_duplicates_quarantine`, `.trash`, `sync-
conflict`, `takeout-`) and current-sounding keywords (`current`,
`latest`, `working`), and path depth (component count).

**Classification rules, in priority order**, each an explicit
INFERENCE:
1. **`DIVERGENT_DATE_SIGNAL`** (medium): 2+ *different* date-like
   tokens across the group's copies.
2. **`PARTIAL_DATE_OR_KEYWORD_SIGNAL`** (low, review required): copies
   share at most one distinct date token but at least one copy has
   none - OR a historical keyword appears on a deeper path while a
   shallower sibling carries none. States only that an ASYMMETRY
   exists, never which copy is current.
3. **`UNIFORM_KEYWORD_NO_FURTHER_SIGNAL`** (low, review required): a
   historical keyword appears somewhere, nothing else distinguishes
   the copies.
4. **`NO_PROVENANCE_SIGNAL`** (low, review required): no date token and
   no keyword anywhere - absence of a signal is weak evidence, never a
   safety conclusion.
5. **`STALE_REFERENCE_UNVERIFIABLE`** (low, review required): fewer
   than two of the group's D1-named paths still existed when D2
   checked - D1's finding can't be corroborated and no interpretation
   is offered.

**Category preserved, provenance meaning never implied to be
identical across categories**: `group_kind` (`exact_duplicate` /
`cryptomator_chunk_duplicate` / `directory_duplicate`) is carried on
every `GroupProvenance` record, and the report's own JSON carries a
`cryptomator_chunk_provenance_note` stating explicitly that `.c9r`
matches are CIPHERTEXT chunk matches, not plaintext document matches,
and must never be reasoned about the same way as `exact_duplicate_
provenance` entries even when they share an `inference_code`.

**Question 8 (overlap)**: `overlaps_directory_group` names the D1
directory-group signature a file-level group's paths sit entirely
inside, if any - connecting the file-level and directory-level
evidence for the same underlying duplication rather than leaving them
disconnected.

**Live-corpus semantics, stated precisely, not overstated**: D1 ran
while Syncthing was actively modifying the corpus; D2's own reads
happened in a SEPARATE pass, roughly two hours later, while Syncthing
remained continuously active throughout both. On the real run, all
582,951 D1-named paths were still observable when D2 checked them
(`paths_no_longer_existing=0`) - **this states only that path
existence was confirmed, and must not be read as proof the corpus, or
any specific file's content, was unchanged between D1 and D2**. A
file's bytes could change without its mere existence changing, and
D2's own hash-free design (see below) cannot detect that. The report's
own `_read_this_first` field states this distinction verbatim, not
merely as prose elsewhere.

**Human review framing, stated precisely**: every `low`-confidence
finding's role is "requires independent human provenance review,"
never "likely safe to remove" - the report disclaimer says so
explicitly, and no prose anywhere in `INFERENCE_PROSE` uses "safe to
remove"/"safe to delete" language (enforced by a dedicated test).

**Report size reduced 62.6%** (498.4MB → 186.6MB on the real corpus) by
removing what the review identified as unnecessary duplication:
per-path `mtime`/`ctime` (collected but never actually used by any
classification rule - Syncthing's rewriting of timestamps on sync
makes cross-copy mtime comparison unreliable on this corpus anyway),
and the repeated inference PROSE paragraph previously stored on every
one of 67,727 groups (now a single shared lookup table, rendered on
demand). The remaining size is dominated by the corpus's own scale -
582,951 individual path strings are the actual evidence and cannot be
reduced without losing it.

**Runner**: `scripts/t7_provenance_analysis.py collect <d1_report.json>
<output.json> <summary.txt>` for a fresh run, or `... reclassify
<previous_report.json> <d1_report.json> <output.json> <summary.txt>`
to re-derive a corrected report from an already-completed one with
zero filesystem access - used for real to produce this milestone's
final corrected report from the original (pre-review) real run's raw
data, without a second ~14-minute `os.lstat` pass over the T7.

**Tests**: 34 (up from 24 after the review pass), entirely synthetic -
date-token extraction, the naming-neutrality invariant on every
inference code, every classification rule including the corrected
low-confidence/review-required framing for the asymmetric-signal case,
the "does not establish which copy is current" prose assertion, live-
corpus divergence, Cryptomator-chunk separation (including the report-
level ciphertext-scoping note), directory-group provenance, file/
directory overlap detection, the compact-schema assertions (no `mtime`/
`ctime` serialized, codes not prose stored per group), the two-stage
pipeline's pure-function property (`classify()` needs no filesystem
access), and `load_raw_from_previous_report`'s reconstruction (plus its
explicit refusal when pointed at an already-compact report with
nothing left to reconstruct).

**Deliberately not built this milestone**: any deletion, move, rename,
quarantine, or archive extraction; any invocation of `DedupFilesystem
Executor`; any authorization creation; any automatic choice of a
canonical/keeper file; any automatic deletion marking. **The T7 was
only ever read via `os.lstat` for this milestone** - no file was
opened, written to, moved, renamed, deleted, or otherwise modified. The
real corpus was NOT re-scanned to correct the classification defect
above - the fix was re-derived entirely from already-collected data.

### D2 Safety Follow-up - a procedural incident, disclosed precisely

**What actually happened, stated precisely rather than smoothed over**:
during D2's final pre-commit safety verification, a test intended to
check whether `write_provenance_report`'s destination guard would
reject a write into the OTHER named T7 path (not the one that
particular analysis had scanned) was written as a REAL write attempt
against `/media/personal/Seenu_T7SSD` (the canonical, currently-
unmounted T7 path) - `write_provenance_report(analysis, Path("/media/
personal/Seenu_T7SSD/sneaky.json"))`. **No T7 file was successfully
mutated** - the attempt was denied by OS-level permissions (`EACCES`,
that directory being root-owned) before any bytes were written, and
`find`-based re-verification afterward confirmed the directory's
contents were unchanged. But **the verification procedure itself
violated this project's own standing rule of never directing a
mutating operation at a real protected path, even as a test** - a
permission denial is not an application-level safety guarantee, and
this test should not have targeted a real path at all, synthetic or
otherwise substitutable. The mounted real corpus, `/media/personal/
Seenu_T7SSD1`, was not targeted by this specific test and was not
mutated. This distinction - "did D2 mutate the T7" (no) versus "did the
verification procedure obey the never-touch-the-real-corpus-for-
testing rule" (no, it did not) - is deliberately kept separate here,
per explicit correction, rather than collapsed into a single
reassurance.

**What this revealed about `reject_destination_inside_root`'s actual
scope**: the helper protects only the SPECIFIC `root` it is given
(`analysis.d1_root`, always caller-supplied, never hardcoded) - it has
no concept of "other locations the caller separately considers
sensitive." A destination inside a different, unrelated directory (a
second T7 mount point, for instance) is not rejected, because the
helper was never told that directory mattered. This is not a defect in
the helper's own logic (its single-root guarantee holds exactly as
designed and tested); it is a genuine gap in what this project's tools
currently protect BEYOND the one root a given analysis actually
scanned.

**Protected-root policy - a decision, not an implementation this
follow-up builds**: a broader "never write here regardless of what's
being analyzed" policy belongs in a SEPARATE, EXPLICIT layer -
deliberately NOT folded into `reject_destination_inside_root` itself,
and deliberately NOT a hardcoded list of real paths inside `app/
discovery/*.py` (this project has never hardcoded the real corpus
location into production code, and a "protected roots" constant would
be exactly that). The intended shape, decided now but built later, only
when separately authorized:

```
operation
   ↓
destination safety policy
   ├── resolve destination
   ├── reject protected roots       <- NEW: an explicit, caller/config-
   │                                     supplied list, never hardcoded
   │                                     literals in library code
   ├── reject inside active root    <- EXISTING: reject_destination_
   │                                     inside_root, unchanged
   └── reject unsafe/symlinked      <- EXISTING: already handled via
       destinations                     .resolve(strict=False)
```

A caller wanting multiple locations protected today must call
`reject_destination_inside_root` once per protected root explicitly
(proven safe and correct via `test_caller_can_protect_multiple_roots_
by_checking_each_explicitly`) - a real but manual pattern, acceptable
for the current single-analysis-at-a-time scripts, but not something
that would scale cleanly to a future milestone with more entry points.
Building the actual policy layer (where the protected-root list lives -
an explicit runner argument, an environment variable, a small config
file never containing defaults) is explicitly deferred to its own,
separately-authorized milestone.

**Test added, entirely synthetic**: `test_does_not_protect_a_second_
root_it_was_never_told_about` reproduces the exact scope gap using two
disposable `tmp_path` directories, never referencing or touching any
real filesystem location - proof of the boundary, not a repeat of the
incident.

**Deliberately not built this follow-up**: the protected-root policy
layer itself; any change to `reject_destination_inside_root`'s
single-root behavior; any T7 access of any kind, including read-only
verification (already confirmed clean by D2's own final checks and not
repeated here). This follow-up is documentation and a synthetic
regression test only.

## T7 → AI_Brain Ingestion & Representation Design (design pass, APPROVED and frozen — no implementation yet)

A fourth T7 gate, opened after D2 (`62e943f`) and its safety follow-up
(`48fb4a7`) were both formally closed. Scope, stated explicitly by the
user at authorization: **design only**. No T7 access of any kind
(read-only included), no schema migration, no code change, no
ingestion. This section proposes answers to nine user-posed questions,
mapped onto the codebase as it exists today rather than a parallel
design disconnected from it, and went through three review rounds
(conflation corrections, then two targeted clarifications) before
being approved. **Status: the direction is frozen** — the next gate
is a schema/model design milestone (not yet authorized), settling the
exact representation of `SourceInstance → ProvenanceLink →
ContentIdentityGroup → Document → DocumentChunk` while still leaving
logical-document/version semantics (identity layers 3/4, below)
deferred.

### Grounding: what already exists

Ingestion machinery already exists, built for the personal-corpus
import path (not yet run against the T7):

- `SourceScanner.scan()` (`app/ingestion/scanner.py`) — recursive,
  non-recursive-into-archives discovery, returns `DiscoveredFile(path,
  relative_path, size)`.
- `ArchiveExtractor.extract()` (`app/ingestion/archive.py`) — handles
  `.zip`/`.7z` only, one level deep (does not recurse into an archive
  extracted from another archive), with real safety limits already in
  place: path-traversal rejection, disk-space reserve, expansion-ratio
  zip-bomb guard, and (7z only) outright symlink-member rejection.
- `DocumentIngestor.ingest()` (`app/ingestion/document_ingestor.py`) —
  orchestrates scan → extract → persist; skips archive container files
  themselves, hashes every other file's content (`_hash_file`, SHA-256)
  and creates one `Document` row per file. **Gap worth naming now**: a
  single file that fails to extract or hash aborts the whole batch —
  there is no per-file catch-and-continue.
- `Document` (`app/models/document.py`) — flat: `id`, `title`, `source`
  (the on-disk path *after* extraction, not the original archive-
  relative location), `source_type`, `content_hash` (nullable,
  deliberately non-unique — duplicates are exactly what it's for),
  `import_job_id`. No archive-membership field, no version/grouping
  concept.
- `DocumentChunk` (`app/models/document_chunk.py`) — `document_id`,
  `chunk_index`, `content`, `embedding` (pgvector). Chunking itself is
  `chunk_text()` (`app/embeddings/chunker.py`), a plain character
  sliding window (1000/200 overlap).
- `ImportJob` (`app/models/import_job.py`) — `status`, `progress`,
  `files_discovered`, `files_processed`, `error_message`. Counters
  only: nothing records *which* files were processed, so there is no
  safe resume point below "the whole job" today.
- `ProvenanceService.trace_document()` (`app/provenance/service.py`) —
  already walks `ImportJob → Document → DocumentChunk → Message`
  (citations are a denormalized JSONB snapshot on `Message`, matched by
  scanning, not a foreign key) — read-only, adds no new source of
  truth. This is the hop this design extends one step further back.
- `FileAccessService` (`app/files/service.py`) — the only file-write-
  capable-adjacent surface in the whole codebase is deliberately a
  read; it will only read a path that falls under the `source_path` of
  a **COMPLETED** `ImportJob` — the master backup (and, by the same
  reasoning, the T7) stays out of reach of every AI-facing tool unless
  it was explicitly imported first. This is an existing enforcement
  point this design relies on, not a new one.
- [0001-file-first](../decisions/0001-file-first.md) — files on disk
  are the source of truth; Postgres is a derived, rebuildable index.
- [0002-master-backup-is-read-only](../decisions/0002-master-backup-is-read-only.md)
  — ingestion only ever *reads* from the master backup; `INGESTION_DIR`
  (where archives are expanded) is always a separate location. **The T7
  is architecturally just another instance of "master backup" under
  this existing decision** — no new read-only rule is being invented
  here, this design treats 0002 as already covering it.

### Revision note (review round 1)

The first draft of this section conflated several distinct concepts —
treating a D1 directory-structural match as equivalent evidence to a
D1 exact-content match, inferring canonical authority from the mere
absence of a review flag, and describing `archive_chain` and
`Document.content_hash` more casually than their actual semantics
support. A review pass caught this before commit (nothing below was
ever implemented or committed — this remains a design-only document)
and the design was restructured around four explicit **identity
layers**, from which the answers to the nine questions are re-derived.
This is the same discipline as D2's own pre-commit review: the
correction is written into the design itself, not patched around it.

### Identity layers — what this design distinguishes, and what it defers

Four different questions get asked about the same piece of matter, and
this design keeps them separate rather than letting one silently stand
in for another:

1. **Physical source occurrence** — "a specific file or archive member
   was observed at a specific path at a specific point in time."
   Represented by `SourceInstance` (below). **Modeled in this pass.**
2. **Content identity** — "these bytes are identical to those bytes,"
   an equivalence class established purely by a matching hash, no
   interpretation involved. **Modeled in this pass**, but only where a
   hash actually proves it (see Hash semantics) — never inferred from
   directory-level structural similarity.
3. **Logical document identity** — "this represents the same
   intellectual document," which can span multiple *different*
   content-identity classes (a re-saved PDF, an edited draft). This is
   exactly what D2's provenance analysis gathered evidence toward and
   explicitly declined to assert (`inference_code`, not a fact). **Not
   modeled in this pass** — D2's evidence is available as future input,
   but no schema or algorithm for this layer is proposed now.
4. **Document / version** — "which version of a logical document is
   this, and where does it sit in time relative to the others,"
   requiring the layer above to exist first plus temporal ordering.
   **Not modeled in this pass**, for the same reason as layer 3.

Everything below builds only on layers 1 and 2. Nothing in this design
requires layers 3 or 4 to exist, and nothing here should be read as a
disguised attempt to build them under a different name.

### Hash semantics — which hash, at which stage

Four distinct hashes exist or could exist in this pipeline; the first
draft blurred them into one. None of this is implemented yet — it is
the vocabulary the eventual implementation must keep straight:

| Name | What it hashes | Where it's computed today |
|---|---|---|
| **source file hash** | Raw bytes of a file exactly as it sits in the T7 (or any pre-extraction source). For an archive *itself* (e.g. a `.zip`), this hashes the archive's own bytes — not anything inside it. | D1 (`duplicate_analysis.py`, `_hash_file`), for size-collision candidates only. Never touches archive interiors — D1 never opens an archive. |
| **archive member hash** | Raw bytes of one member's *decompressed* content, after extraction. Distinct from the source file hash of the archive that contained it. | Not computed anywhere today. Would be computed post-extraction, during ingestion. |
| **extracted document content hash** | Raw bytes of the on-disk working copy at ingestion time. For a loose file this happens to equal its source file hash. For an archive member, this is the archive member hash — never the enclosing archive's own hash. | `DocumentIngestor._hash_file`, stored as `Document.content_hash` today. |
| **normalized-content hash** | A hash of *extracted text* (post `extract_text()`), which could match content across different container formats or after normalization. | Does not exist anywhere. Explicitly a future idea, not proposed for implementation now. |

The corrected statement of Q7 (duplicate work) follows directly from
this table: **`Document.content_hash` cannot serve as a pre-extraction
dedup check for anything that lives inside an archive**, because no
hash exists for an archive member until after it has been extracted —
D1 only ever hashed things `os.walk` could reach directly. A
loose file's already-known D1 source file hash *can* gate extraction
before it happens; an archive member cannot. The design does not
paper over this with one unified "content_hash, checked early"
gate — it names the two cases separately (see Q7, revised, below).

### Archive provenance — a structural chain, not an opaque string list

The first draft's `archive_chain: list[str]` was flagged as too
opaque to support ancestry queries later ("which documents came from
this specific archive," "how deeply nested is this member"). Revised
to a linked structure instead of a flat list:

```
ProvenanceLink
  kind: "t7_file" | "archive_member"
  path: path at this level (T7 path for a t7_file link; member path
        within its immediate parent for an archive_member link)
  parent: the ProvenanceLink this one was found inside (null for the
          root t7_file link)
```

A loose T7 file is a single `t7_file` link with no parent. A file two
archives deep is a three-link chain: `t7_file` (the outer archive as
found on the T7) → `archive_member` (the inner archive, as a member of
the outer one) → `archive_member` (the actual file, as a member of the
inner one). This is a schema *shape*, not a migration — the exact
table design (a self-referential table vs. a materialized path column
vs. something else) is left to the implementation gate, but the shape
is chosen now specifically so "all documents that passed through
archive X" or "nesting depth of this instance" are answerable by
walking or querying `parent`, not by parsing a string.

### `SourceInstance` — physical occurrence only

Corrected scope, per review: `SourceInstance` represents **one
physical observed occurrence** and nothing more. It carries:
- the T7 original path (or, for an archive member, resolved via its
  `ProvenanceLink` chain rather than a flat path string) — recorded as
  inert metadata, never dereferenced again as a live filesystem path
  after classification runs;
- its `ProvenanceLink` chain (above);
- whatever D0/D1/D2 evidence already exists about it (size, source
  file hash if D1 computed one, D2 `inference_code`/`confidence` if it
  was part of a D2 group);
- a reference to a content-identity value (a hash — see table above)
  it shares with zero or more *other* `SourceInstance`s.

It does **not** carry canonical status, does **not** imply a `Document`
exists yet, and does **not** imply anything about logical-document or
version identity (layers 3/4, explicitly deferred). Multiple
`SourceInstance`s referencing the same content-identity hash are
simply that — physically-separate occurrences of identical bytes —
until something else (see Canonical status, below) decides what, if
anything, gets ingested from that group.

### Canonical status — a relationship, not a property of a file

Round 2 correction: round 1 left two things unstated that a reviewer
correctly flagged as ambiguity waiting to happen. First, *which object
owns `canonical_status`*. Second, what `NON_CANONICAL` actually means.
Both are settled now.

**Where it lives.** Introduce `ContentIdentityGroup` as a first-class
object: the set of all `SourceInstance`s that share one proven content-
identity hash (layer 2). `canonical_status` is not an intrinsic
property of a `SourceInstance` or of a file — it is a property of the
**relationship between a `SourceInstance` and the specific
`ContentIdentityGroup` it belongs to**:

```
ContentIdentityGroup
    ├── SourceInstance A → UNRESOLVED
    ├── SourceInstance B → CANONICAL
    └── SourceInstance C → NON_CANONICAL
```

Because every `SourceInstance` belongs to exactly one
`ContentIdentityGroup` (its content-identity hash is single-valued),
this can still be implemented as one field on `SourceInstance` without
a separate join table — but it must always be *documented and reasoned
about* as scoped to that instance's group, never as "is this file
canonical," full stop. "Canonical for which content-identity group" is
the only question that has a real answer; "canonical everywhere" is
not a question this design lets get asked.

**The three states, corrected:**
- `UNRESOLVED` — the default and starting state for every
  `SourceInstance` in every group, including a group where every
  instance agrees byte-for-byte and D2 raised no concerns at all.
  Unresolved is a **valid, expected, and common end state**, not an
  error or a TODO.
- `CANONICAL` — this specific `SourceInstance` has been explicitly
  marked as the authoritative physical occurrence within its group, by
  a human decision or a future, separately-justified, narrowly-scoped
  automated policy. No such policy is defined here.
- `NON_CANONICAL` — corrected: this is **not** "whichever instances
  weren't picked" and does **not** follow automatically from another
  instance in the same group being marked `CANONICAL`. It requires its
  own explicit evidence or decision (e.g. "this copy is truncated,"
  "this copy is a known-stale backup fragment," a human said so) —
  exactly the same evidentiary bar as `CANONICAL` itself, just pointed
  the other way. Marking B `CANONICAL` leaves every other instance in
  the group at `UNRESOLVED` unless something *separately* justifies
  moving one of them to `NON_CANONICAL`. A group can therefore
  legitimately sit at "one `CANONICAL`, N `UNRESOLVED`" forever, with
  zero `NON_CANONICAL` instances — that is a normal, not a partial,
  state.

Critically, **ingestion (extraction/chunking/embedding) does not wait
for `CANONICAL` status and does not operate on `SourceInstance`s or
`canonical_status` at all — it operates on `ContentIdentityGroup`s**
(see Ingestion idempotency key, below). Canonical status governs which
`SourceInstance`'s path is *displayed* as the reference location for a
piece of already-ingested knowledge — it never governs *whether* that
knowledge gets ingested. This is what lets a `ContentIdentityGroup`
sit at all-`UNRESOLVED` forever without that ever being confused for a
disposition decision D2 was careful never to make.

### D1 directory-structural matches — context, not content proof

Corrected per review point 1: a D1 directory-structural match is **not
of the same evidential weight as a D1 exact-content match**, and does
not drive any content-identity decision on its own. D1's own report
already keeps `exact_duplicate_reclaimable_bytes` and
`directory_duplicate_reclaimable_bytes` separate for exactly this
reason (documented in D1's docstring: never sum them). This design
carries that same separation forward: every individual file inside a
directory-structural match still goes through its *own* content-
identity determination independently (it may land in a D1 exact-
duplicate group of its own, or be unique, or have no source file hash
computed at all, depending on what D1 actually hashed). The directory-
level match itself is preserved only as **contextual provenance**
attached to the `SourceInstance`s inside both trees — e.g. "this
occurrence sits inside a directory that structurally matches another
directory at path Y" — informative to a human or to layer-3/4 work
later, but never itself the reason a `Document` gets created or a
canonical selection gets made.

### Ingestion idempotency key

Round 2 addition, making explicit what round 1 only implied. The unit
that triggers the extraction/normalization/chunking pipeline is
`ContentIdentityGroup` (above) — not `SourceInstance`:

```
physical SourceInstance
        |
        v
content identity  (ContentIdentityGroup)
        |
        v
one extraction / normalization / chunking pipeline run
        |
        v
multiple provenance references (every SourceInstance in the group
points at the one resulting Document)
```

Stated plainly: **once a `ContentIdentityGroup`'s pipeline has run,
additional physical occurrences discovered later that join the same
group do not independently re-trigger extraction, normalization, or
chunking** — they attach as new `SourceInstance`s (new provenance
references) against the `Document` that already exists for that group.

The archive caveat from Hash semantics applies here without exception,
and is worth restating as its own chain because it is the one place
this idempotency key cannot short-circuit work, only downstream cost:

```
T7 archive
   |
   v
archive member
   |
   v
member hash becomes known only AFTER reading/extracting the member
```

A member's content identity is unknowable before it is read. So
archive-aware deduplication can prevent *downstream* work once the
member's hash is known (skip chunk/embed if that hash already has a
`Document`) — it cannot prevent the extraction/read of a not-yet-
identified member. The idempotency key is content identity, and
content identity for an archive member does not exist until after the
one unavoidable read that produces it.

### Ingestion state — its own lifecycle, not Chain 1's

Corrected per review point 6: this design reuses the *durability
principle* behind Chain 1's dedup executor (one durable, idempotent
row per processed item is the resume checkpoint — see Q8) but
explicitly does **not** reuse Chain 1's actual state machine
(`DedupExecutionAction`'s `PLANNED/AUTHORIZED/EXECUTING/SUCCEEDED/
FAILED/RECONCILED` lifecycle, its inode-pinning TOCTOU closure, its
post-move verification). That machine exists to make a specific
promise about *authorized mutation of real files on the T7* — a
promise ingestion, which never mutates the T7 and never even mutates
existing extracted copies once written, does not need and should not
inherit undigested. Ingestion needs its own lifecycle, sketched (not
implemented) as: `DISCOVERED → CLASSIFIED → EXTRACTING → EXTRACTED →
NORMALIZED → CHUNKED → EMBEDDED → INGESTED`, with terminal
`QUARANTINED` (defined precisely below) and `NEEDS_REVIEW` (D2
flagged, unresolved) states reachable from `CLASSIFIED`. Retry
semantics (what "extracting" failing mid-way means, whether it's safe
to retry from `CLASSIFIED` or must resume from a partial-extraction
cleanup step) are **not** designed in this pass — named as a real
question for the implementation gate, not answered here by assumption.

**`QUARANTINED`, defined precisely — a different word for a different
thing.** This project already has a "quarantine" with a specific,
load-bearing meaning: Chain 1's dedup executor quarantine, which
*reversibly relocates a real file the T7-adjacent filesystem holds*, as
one step of an authorized, auditable mutation plan. Ingestion's
`QUARANTINED` state means something categorically different and must
never be read as a synonym: **an ingestion artifact — the working copy
in the extraction workspace, or the classification record for a file
that could not be processed — is rejected or set aside within
`documents/imports/<job_id>/`,** because its archive was corrupt, its
format is unsupported, or it exceeded a size/expansion limit. It is not
a T7 source file being moved anywhere; the T7 item that produced a
`QUARANTINED` ingestion record has not been touched, renamed, or
relocated, and no such capability is proposed here. Future
implementers must keep these two "quarantine" concepts named clearly
enough (e.g. `IngestionState.QUARANTINED` vs. the dedup executor's
`QuarantineAction`/whatever it is currently called) that neither is
ever mistaken for the other in code, logs, or documentation.

### The pipeline, mapped onto that machinery

```
T7 source (read-only, forever)
   |
   v
discovery        <- ALREADY DONE: D0 corpus_inventory.py output
   |                 (619,087 files, ~669GB, knowledge/t7_discovery/inventory.json)
   v
classification   <- NEW STAGE. Reads D0 + D1 + D2 JSON reports only.
   |                 Establishes content-identity groupings (layer 2)
   |                 and creates SourceInstance + ProvenanceLink records
   |                 (layer 1). Does NOT decide canonical status (stays
   |                 UNRESOLVED) and does NOT collapse a directory-
   |                 structural match into one instance. Zero T7 access.
   v
archive handling <- ArchiveExtractor, extended: recursive (archive-
   |                 inside-archive, depth-limited, threading the
   |                 ProvenanceLink chain through each hop), per-item
   |                 errors caught and quarantined rather than aborting
   |                 the job.
   v
document extraction <- DocumentIngestor + text_extractor.py, unchanged
   |                     in kind, run once per content-identity group
   |                     (not once per SourceInstance).
   v
normalization    <- existing extract_text() dispatch; no change proposed.
   v
provenance       <- SourceInstance + ProvenanceLink records already
   |                 created at classification are attached to the
   |                 resulting Document; never re-derived from the T7.
   v
chunking         <- chunk_text(), unchanged.
   v
embeddings       <- EmbeddingClient, unchanged.
   v
AI_Brain knowledge (Document + DocumentChunk, queried by RetrievalService)
```

The load-bearing property of this shape is unchanged from the first
draft: classification is the only stage that has to think about the
T7's scale, and it does so entirely off already-collected JSON,
without touching the T7 itself. What changed is *what classification
is allowed to decide* — content-identity grouping and provenance
recording, yes; canonical authority or directory-level content
equivalence, no.

### Answering the nine questions (revised)

**1. What gets ingested?** One `Document` per **content-identity**
group (layer 2) — proven by a matching hash, per the table above — not
per D1 duplicate group indiscriminately. A D1 exact-duplicate group
*is* a content-identity group (D1 proved it via hash) and ingests as
one `Document`. A D1 directory-structural match is not, by itself — the
files inside it are ingested (or not) individually based on their own
content identity, with the directory relationship preserved only as
context (see above). A content-identity group where D2 flagged
`requires_human_review=True` still gets ingested as ONE `Document`
(ingestion doesn't wait on canonical resolution — see Canonical
status) but its `SourceInstance`s stay `UNRESOLVED` and the review flag
carries forward for a human to look at later.

**2. What stays archived (cataloged, never content-ingested)?** `.c9r`
Cryptomator chunks (ciphertext, established by D2); any file
classification quarantines (corrupted archive, unsupported format,
over a size/expansion limit); a `SourceInstance` that duplicates a
content-identity group already ingested (it is recorded, but its bytes
are not re-extracted, re-chunked, or re-embedded a second time).

**3. How are archives represented?** Via the `ProvenanceLink` chain
(above), not a flat string list — `t7_file → archive_member →
archive_member → ...`, queryable, not just displayable. `Document.
source` keeps meaning the on-disk working-copy path (unchanged, per
0001/0002); the `ProvenanceLink` chain is separate metadata, never a
live filesystem path once classification has run.

**4. How are versions represented?** Not represented in this pass —
this is layer 4 (document/version), explicitly deferred (see Identity
layers). What *is* represented: `SourceInstance` records every
physical occurrence with whatever date/keyword signal D2 already
recorded for it, which is the raw material a future layer-4 design
would consume. The target capability the user named — "I had three
copies of this document, which versions existed over time" — is not
answered by this design pass; it requires layer 3/4 work not yet begun,
and this document does not claim otherwise.

**5. How is provenance preserved / 9. how does an answer trace back to
the T7?** Extends `ProvenanceService.trace_document()`'s existing walk
(`ImportJob → Document → DocumentChunk → Message`) one hop earlier via
`SourceInstance` and its `ProvenanceLink` chain, giving the full
`answer → chunk → document → archive member(s) → source file → T7
path` trace as a read-only query over existing + proposed tables, not
a new subsystem.

**6. Where do extracted artifacts live?** Unchanged from the first
draft: a workspace outside both T7 mount points and outside
`knowledge/t7_discovery/`, consistent with the existing `documents/
imports/<job_id>/` layout, e.g. `documents/imports/<job_id>/extracted/`
mirroring each item's `ProvenanceLink` chain + relative path. Retention
policy for non-canonical or non-ingested working copies remains an
**open question**, deliberately unresolved — a Track 2 concern once
real disk-usage numbers exist.

**7. How do we prevent duplicate work?** Revised per the hash-semantics
correction above — this is now two genuinely different cases, not one
gate:
- **Loose T7 files**: D1's already-computed source file hash (where it
  exists — D1 only hashed size-collision candidates) can gate
  extraction *before* it happens, exactly as the first draft described.
- **Archive members**: no hash exists before extraction (D1 never
  opened archives). These must be extracted first; the resulting
  archive-member hash is then checked against already-ingested content
  identities, and a match means the *extraction was necessary but the
  chunk/embed step is skipped* — the redundant work saved is chunking
  and embedding, not extraction, and this design says so plainly rather
  than implying extraction itself is always avoidable.
- At the corpus level, in both cases: classification consumes D0/D1/D2's
  already-computed JSON instead of re-scanning or re-hashing the T7 —
  the ~104 minutes of D0+D1+D2 runtime is spent exactly once regardless.

**8. How does ingestion resume after interruption?** `ImportJob`'s
counters cannot answer "which ones" today. Proposal unchanged in
principle: each classified item gets its own durable `SourceInstance`
row at classification time, before extraction; resume is "the
classification manifest minus items with a durable row," combined with
whatever `NEEDS_REVIEW`/`QUARANTINED`/lifecycle state (above) that row
already carries. This reuses Chain 1's *durability principle* only —
not its state machine (see Ingestion state, above).

### What this design pass explicitly does NOT decide or build
- The exact `SourceInstance` / `ProvenanceLink` schema (column types,
  indexes, migration, whether `ProvenanceLink` is a real table or a
  materialized-path column) — sketched at the concept/shape level only.
- Logical document identity or document/version modeling (layers 3/4)
  — named and deliberately deferred, not sketched even at concept
  level beyond noting D2's evidence as future input.
- Any canonical-copy *selection policy* — canonical status stays a
  three-state field with no algorithm proposed to populate it beyond
  "a human decided." The pre-existing "Best Copy Arbitration" backlog
  item remains where that eventually lives.
- Retention policy for working copies (Q6's open question).
- Resource limits, backpressure, job scheduling, or a worker queue —
  Track 2, out of scope here.
- Ingestion's retry/partial-failure semantics beyond naming the states
  involved (see Ingestion state, above).
- Recursive-archive depth limit, per-item quarantine error taxonomy,
  and the classification stage's actual code — all implementation,
  deferred to whatever gate follows review of this document.
- Any T7 access. This entire section, including this revision, was
  written from the D0/D1/D2 reports already on disk and the existing
  ingestion codebase; no new scan, hash, or read of either T7 path
  occurred to produce it or this correction.

## Schema/Model Design: SourceInstance → ProvenanceLink → ContentIdentityGroup → Document → DocumentChunk (APPROVED and frozen — no implementation yet)

A fifth T7 gate, opened after the Ingestion & Representation Design
froze at `29d6864`. Scope, stated explicitly at authorization:
**schema/model design only**. No T7 scanning, no extraction, no
real-corpus ingestion, no embeddings, no production implementation, no
database migrations, no model implementation, no dedup execution, no
deletion/quarantine, no logical-document/version implementation. The
deliverable is a precise proposed schema and relationship model — code
sketches below are illustrative, not files to be created. Went through
five review rounds (the fourth review's six corrections, then a fifth,
pre-freeze cross-table invariant check that itself found and fixed one
real mutability-classification error and added two missing CHECK
constraints) before being approved. **Status: frozen** — the next gate
is implementation (an actual migration, model, and service code) of
this design, not yet authorized.

### Grounding: precedent already in this codebase

Before inventing new patterns, two existing tables were read closely
because they already solve a structurally similar problem and set the
house style this design follows:

- **`DuplicateReview` + `DuplicateReviewMember`**
  (`app/models/dedup_review.py`) — a parent "finding" row plus an
  N-way child table of members with a role, specifically because "a
  group is genuinely N-way... fixed columns would silently truncate a
  real 3+-way group." `ContentIdentityGroup` + `SourceInstance` is the
  same shape for the same reason.
- **`DedupExecutionPlan` + `DedupExecutionPlanAction`**
  (`app/models/dedup_execution_plan.py`) — immutable, snapshotted audit
  rows ("every regeneration is a new row, not an edit"), with evidence
  duplicated onto child rows for self-description without a join. This
  design's `SourceInstance` and `ProvenanceLink` rows follow the same
  immutability convention.

Both use: `Integer` surrogate primary keys, `SQLEnum(Enum, name=
"snake_case")` for status columns with a matching `server_default`,
UTC-lambda `created_at`/`updated_at`, `JSONB` for point-in-time evidence
snapshots (`DuplicateReview.evidence`), and `UniqueConstraint` for
N-way relationship integrity. Every table below follows these
conventions rather than inventing new ones.

### The five new/extended tables

**`DiscoveryRun`** — one row per already-completed D0/D1/D2 run. This
is deliberately **not** the heavier, atomic `CorpusSnapshot` concept
from the dormant backlog — D0/D1/D2 are three separate, sequential,
non-atomic runs against a live corpus (D2 ran ~2 hours after D1, with
Syncthing active throughout both, as D2's own documentation already
states). This table does not pretend otherwise; it just gives each
already-real run a durable, referenceable identity.

**Invariant, stated explicitly per review (round 4)**: *a `DiscoveryRun`
identifies an observed analysis/report, not a transactionally
consistent filesystem snapshot.* `report_sha256` proves which exact
report artifact a later `ClassificationRun` consumed — it proves
nothing about whether the T7 filesystem itself was unchanging while
that report was being produced, and no code or documentation may ever
read it as such. This sentence belongs verbatim in the eventual
model's docstring, not just in this design document.

```
DiscoveryRun
  id                  Integer, PK
  run_kind            Enum: D0_INVENTORY | D1_DUPLICATE_ANALYSIS
                            | D2_PROVENANCE_ANALYSIS
  source_root         Text        -- T7 root as recorded (inert metadata)
  report_sha256       String(64)  -- hash of the report JSON file itself -
                                      proves WHICH REPORT was consumed,
                                      never that the T7 was quiescent
                                      while producing it (see invariant above)
  run_started_at      DateTime
  run_completed_at    DateTime
  created_at          DateTime
```

**`ClassificationRun`** — one row per execution of the classification
stage (from the `29d6864` pipeline). Re-classification is expected and
first-class, mirroring D2's own `load_raw_from_previous_report`
philosophy of reprocessing without re-touching the T7.

**Addition per review (round 4)**: a `classifier_version` column
identifies *which classification behavior* produced this run's
`SourceInstance` rows — a lightweight, immutable implementation
identifier (e.g. a semantic version or short code identifier the
classification module reports about itself), not a full
`Processor`/`ProcessorVersion` architecture (that remains the dormant
"pipeline versioning" backlog item). Without it, two
`ClassificationRun`s over the identical D2 report could disagree in
their resulting classification and nothing durable would explain why.

```
ClassificationRun
  id                     Integer, PK
  classifier_version     String, NOT NULL   -- immutable identifier for
                          the classification logic that ran; the
                          durable answer to "why did this run classify
                          differently from that one over the same report"
  d0_discovery_run_id    Integer, FK -> discovery_runs.id, nullable
  d1_discovery_run_id    Integer, FK -> discovery_runs.id, nullable
  d2_discovery_run_id    Integer, FK -> discovery_runs.id, nullable
  started_at             DateTime
  completed_at           DateTime, nullable
  created_at             DateTime

  CHECK (d0_discovery_run_id IS NOT NULL OR d1_discovery_run_id IS NOT NULL
         OR d2_discovery_run_id IS NOT NULL)   -- added in round 5: a
         ClassificationRun consuming zero DiscoveryRuns is meaningless
```

**`SourceInstance`** — one physical observed occurrence, per the
`29d6864` design, now given concrete columns.

```
SourceInstance
  id                            Integer, PK
  classification_run_id         Integer, FK -> classification_runs.id, NOT NULL, index
  content_identity_group_id     Integer, FK -> content_identity_groups.id,
                                 NULLABLE, WRITE-ONCE  -- null until this
                                 occurrence's content identity is known.
                                 NOT archive-member-exclusive (round 5
                                 correction): ANY path D1 never hashed -
                                 including a uniquely-sized LOOSE file,
                                 which D1's size-collision filter also
                                 skips - starts here too. Set exactly
                                 once, by whichever step first computes
                                 this instance's hash (classification
                                 itself, if a D1 hash already exists;
                                 otherwise a later extraction/hashing
                                 step), then never changed again. May
                                 resolve to an ALREADY-EXISTING group
                                 (another instance got there first - the
                                 idempotency key working as designed) or
                                 a brand new one.
  root_t7_path                  Text, NOT NULL   -- the outermost t7_file
                                 path (denormalized from the root
                                 ProvenanceLink for query convenience,
                                 matching DedupExecutionPlanAction's own
                                 denormalization convention)
  member_path                   Text, nullable   -- path within the
                                 innermost archive; null for a loose file
                                 (root_t7_path already names it fully)
  evidence_snapshot             JSONB, NOT NULL  -- IMMUTABLE, copied
                                 verbatim at classification time: D1's
                                 group_kind/group_key/category if this
                                 path was part of a D1 group, D2's
                                 inference_code/confidence/evidence_codes/
                                 structured_facts/requires_human_review
                                 if D2 covered it, source-file hash if D1
                                 computed one. Never rewritten after
                                 creation - a correction is a new
                                 ClassificationRun, not an edit here,
                                 matching this project's existing
                                 evidence-snapshot convention
                                 (DuplicateReview.evidence, Message.citations).
  canonical_status               Enum: UNRESOLVED | CANONICAL | NON_CANONICAL
                                 default UNRESOLVED, MUTABLE (the one
                                 mutable evidentiary field on this row)
  canonical_status_reason        Text, nullable   -- REQUIRED (see CHECK
                                 constraint below) whenever status is not
                                 UNRESOLVED - the explicit evidence/
                                 decision, never inferred
  canonical_status_decided_by    Text, nullable   -- "human" today; a
                                 future narrowly-scoped automated policy
                                 would name itself here explicitly,
                                 never silently
  canonical_status_decided_at    DateTime, nullable
  created_at                     DateTime, NOT NULL   -- immutable once written
```

**`evidence_snapshot` semantics, frozen per review (round 4)**:
*`evidence_snapshot` is historical evidence captured from the
discovery/classification inputs that existed at `classification_run_id`'s
`started_at` — it is never a live filesystem view, and no code may
ever re-read or re-validate it against the T7's current state as if it
were current truth.* If the T7 changes after classification, this row
does not know and must not claim to; a fresh observation is a new
`SourceInstance` from a new `ClassificationRun`, never a reason to
mutate this JSON. Symmetrically, **`canonical_status_reason` is a
decision annotation, not source evidence** — it explains a human's (or
future policy's) choice about this row, and must never be read, copied
into, or treated as if it were part of `evidence_snapshot`. The two are
kept in genuinely different columns (see checklist item 13) specifically
so this distinction cannot be blurred by a future developer reaching
for "the JSON blob" without noticing which one they meant.

Constraint sketch (illustrative, not final SQL): `CHECK
(canonical_status = 'UNRESOLVED' OR (canonical_status_reason IS NOT
NULL AND canonical_status_decided_by IS NOT NULL AND
canonical_status_decided_at IS NOT NULL))` — makes the review-round-3
requirement ("`NON_CANONICAL` requires its own explicit evidence,
exactly like `CANONICAL`") a schema-level guarantee, not just a
documented convention a future developer could accidentally violate.

**`ProvenanceLink`** — the structural archive-ancestry chain from
`29d6864`, now with concrete columns supporting real ancestry queries
(the review-round-2 requirement: "not an opaque free-form
`archive_chain`").

```
ProvenanceLink
  id                   Integer, PK
  source_instance_id   Integer, FK -> source_instances.id, NOT NULL, index
  parent_link_id       Integer, FK -> provenance_links.id, nullable
                        (null only for the root t7_file link)
  sequence_index       Integer, NOT NULL  -- 0 for the root link,
                        increasing per nesting level; makes "immediate
                        container" / "nesting depth" queries index-
                        friendly without a recursive CTE every time
  kind                 Enum: T7_FILE | ARCHIVE_MEMBER
  path                 Text, NOT NULL   -- T7 path for a t7_file link;
                        member-internal path for an archive_member link
  created_at           DateTime  -- immutable

  UniqueConstraint(source_instance_id, sequence_index)
  CHECK ((sequence_index = 0) = (kind = 'T7_FILE' AND parent_link_id IS NULL))
    -- added in round 5: position 0 in a chain must be exactly the
    -- t7_file root; every other position must be an archive_member
    -- with a parent. Without this, the UNIQUE constraint alone
    -- prevents two roots but not a semantically-wrong root.
```

A query like "every `SourceInstance` whose chain passed through this
specific archive path" is now `SELECT DISTINCT source_instance_id FROM
provenance_links WHERE path = :archive_path` — a plain indexed lookup,
not a JSONB containment scan.

**`ContentIdentityGroup`** — the first-class object introduced in
review round 3, now with concrete columns.

**Identity domain, made explicit per review (round 4)**: `identity_hash`
alone, unqualified, does not say which of the four hash layers (source
file / archive member / extracted document content / normalized-content)
it lives in — and asserting equivalence across layers by accident is
exactly the kind of silent conflation this whole design exists to
prevent. Two columns make the namespace unambiguous, even though this
milestone populates only one combination of them:

```
ContentIdentityGroup
  id                  Integer, PK
  identity_kind       Enum: SOURCE_BYTES | EXTRACTED_CONTENT | NORMALIZED_CONTENT
                       -- this milestone populates EXTRACTED_CONTENT
                       exclusively (matching the "always the extracted
                       document content hash, never an enclosing
                       archive's source-file hash" rule below); the
                       other two values exist in the enum now so the
                       namespace is unambiguous if a future milestone
                       ever needs them, NOT because this design
                       proposes populating them
  identity_algorithm  Enum: SHA256   -- named explicitly rather than
                       assumed forever; today's D1/ingestion hashing is
                       exclusively SHA-256, but the algorithm is a
                       stated fact of this row, not an implicit default
  identity_hash       String(64), NOT NULL
  pipeline_state      Enum: DISCOVERED | CLASSIFIED | EXTRACTING | EXTRACTED
                       | NORMALIZED | CHUNKED | EMBEDDED | INGESTED
                       | NEEDS_REVIEW | UNSUPPORTED | EXCLUDED | FAILED
                       default DISCOVERED, MUTABLE - see the dedicated
                       discussion below; this is THE ingestion
                       idempotency boundary: the pipeline runs once per
                       group, never per SourceInstance (see
                       Q9/idempotency below)
  created_at          DateTime  -- immutable
  updated_at          DateTime  -- pipeline_state transitions touch this

  UniqueConstraint(identity_kind, identity_algorithm, identity_hash)
```

The `UNIQUE` constraint moved from `identity_hash` alone to the
`(identity_kind, identity_algorithm, identity_hash)` triple specifically
so that — even though only one combination is used today — the schema
itself, not just a convention, prevents two different identity spaces
from ever being compared as if they were the same one.

**`pipeline_state`'s non-processing outcomes, corrected per review
(round 4)**: the original four-state catch-all `QUARANTINED` conflated
three genuinely different outcomes and forced them all through language
that implied failure. Retired in favor of four distinct terminal or
review-pending states, none of which is a euphemism for another:
- `NEEDS_REVIEW` — a D2-flagged group blocked pending a human decision;
  may later transition onward once resolved. Unchanged from `29d6864`.
- `UNSUPPORTED` — classification determined, ahead of any extraction
  attempt, that this content's format/type is not currently handled
  (e.g. no extractor exists for it yet). A durable, known fact about
  the format, not an error.
- `EXCLUDED` — classification deliberately decided, by policy, that
  this content identity is out of scope for ingestion (e.g. `.c9r`
  Cryptomator ciphertext, per D2's own established finding). **`EXCLUDED`
  is not `FAILED`** — nothing was attempted and nothing broke; a
  decision was made.
- `FAILED` — an attempted step (`EXTRACTING`/`NORMALIZED`/`CHUNKED`/
  `EMBEDDED`) genuinely errored (corrupt archive member, decode error,
  embedding service unavailable). Retry semantics remain deferred, per
  `29d6864`, but the state itself is now distinct from the other three
  rather than sharing a bucket with them.

Retiring the shared `QUARANTINED` name also has a second, smaller
benefit: it removes any remaining possibility of confusion with the
dedup executor's quarantine (already addressed once in `29d6864`) by
simply not sharing a name with it at all anymore, on either side.

**`Document`** (existing table, one new column) —

```
Document (existing columns unchanged: id, title, source, source_type,
           content_hash, import_job_id, created_at, updated_at)
  + content_identity_group_id   Integer, FK -> content_identity_groups.id,
                                 UNIQUE, nullable during migration (see
                                 Migration strategy)
```

`content_hash` is kept, not removed - its value must always equal the
owning group's `identity_hash` (a documented invariant, denormalized
for self-description exactly like `DedupExecutionPlanAction` duplicates
`target_path`, not enforced by a DB trigger in this design pass).

**`DocumentChunk`** — **no schema change proposed.** Provenance is
preserved transitively through the existing `document_id` FK, now
extended one hop further upstream by `Document.content_identity_group_id
→ ContentIdentityGroup ← SourceInstance → ProvenanceLink`. Adding
columns here would duplicate information already reachable by a join;
this design deliberately does not do that.

### Cardinality, stated explicitly (checklist item 1)

```
DiscoveryRun (D0/D1/D2, up to 3)  1..3 ── consumed by ──> 1  ClassificationRun
ClassificationRun                        1 ── creates ──> N  SourceInstance
SourceInstance                           N ── links ────> 1  ProvenanceLink chain (1..N links each)
SourceInstance                           N ── may share ─> 0..1  ContentIdentityGroup  (nullable - see Hash semantics)
ContentIdentityGroup                     1 ── produces ──> 0..1  Document  (nullable until pipeline runs)
Document                                 1 ── chunks into ─> N  DocumentChunk  (unchanged)
```

The two **nullable** relationships in this diagram are the two places
review rounds 2-3 specifically corrected: `SourceInstance.
content_identity_group_id` is null exactly when an archive member's
identity is not yet known (Hash semantics), and
`ContentIdentityGroup.document_id`-via-`Document.
content_identity_group_id` is null exactly when the group's pipeline
hasn't produced a `Document` yet (a group sitting at `NEEDS_REVIEW` or
simply not yet processed). Neither nullability is an oversight; both
are the schema's way of refusing to assert an identity or a decision
that evidence doesn't yet support.

### Answering the checklist, in the order it was given

**1. Exact cardinality and ownership** — see diagram above.

**2. Immutable identifiers** — corrected in the round-5 consistency
check (below): this design actually has **three** mutability classes,
not two. Fully immutable, set at creation and never touched again:
`ContentIdentityGroup.identity_kind`/`identity_algorithm`/
`identity_hash`, every `ProvenanceLink` column, every `DiscoveryRun`/
`ClassificationRun` column, and every `SourceInstance` column except
`content_identity_group_id` and `canonical_status*`. **Write-once**
(starts `NULL`, set exactly once when the value first becomes knowable,
fixed forever after): `SourceInstance.content_identity_group_id` — see
the round-5 correction below for why this is not simply immutable.
**Freely mutable**: `SourceInstance.canonical_status` (+ its
reason/decided_by/decided_at) and `ContentIdentityGroup.pipeline_state`
(+ `updated_at`). A later observation of the same physical thing is
always a **new** `SourceInstance` row from a new `ClassificationRun`,
never an edit to an old one (matching `DedupExecutionPlan`'s "every
regeneration is a new row").

**3. Source/archive/content hash semantics** — unchanged in substance
from the `29d6864` table (source file hash / archive member hash /
extracted document content hash / normalized-content hash); this round
adds an explicit `identity_kind` + `identity_algorithm` domain (above)
so `ContentIdentityGroup.identity_hash` is always specifically the
*extracted document content hash* (`identity_kind = EXTRACTED_CONTENT`),
never a source-file hash of an enclosing archive, as a schema-enforced
fact rather than a naming convention alone.

**4. Archive-member provenance** — the `ProvenanceLink` chain, above.

**5. Multiple physical occurrences → one content identity** —
`SourceInstance.content_identity_group_id`, many-to-one, nullable.

**6. `Document` ↔ `ContentIdentityGroup` relationship** — one-to-one,
via a `UNIQUE` FK on `Document`, nullable until the group's pipeline
actually produces a document (a group can legitimately have zero
`Document`s — e.g. still `NEEDS_REVIEW`, `UNSUPPORTED`, `EXCLUDED`, or
`FAILED`). **Invariant, stated explicitly per review (round 4)**: *a
`ContentIdentityGroup` represents one ingestible content identity, and
at most one derived `Document` currently represents that identity.*
The `UNIQUE` constraint is a statement about how many `Document`s exist
*today* for a given identity — one, at most — not a claim that two
semantically different representations can never relate to each other.
If a future logical-document layer (identity layers 3/4, still
deferred) ever needs to merge multiple `ContentIdentityGroup`s under
one logical document, that almost certainly means a *new* mapping
table above this one — not a weakening of this `UNIQUE` constraint,
which stays exactly as strict as it is today.

**7. `DocumentChunk` provenance** — preserved transitively via the
existing, unchanged `document_id` FK; no new columns on `DocumentChunk`.

**8. Canonical-status ownership and evidence requirements** — lives on
`SourceInstance` (scoped to its one `ContentIdentityGroup`, per review
round 3), with a `CHECK` constraint sketch making "`NON_CANONICAL`
requires the same evidentiary bar as `CANONICAL`" a schema guarantee.

**9. Ingestion idempotency boundary** — `ContentIdentityGroup.
pipeline_state` is the single per-group progress record; `SourceInstance`
never carries its own copy of pipeline progress, which is what makes
"one pipeline run, multiple provenance references" (the `29d6864`
idempotency key) structurally true rather than merely a convention
someone could violate by writing to the wrong row.

**10. Lifecycle/state and uniqueness constraints** —
`ContentIdentityGroup.pipeline_state` refines the `29d6864` lifecycle
by replacing the earlier catch-all `QUARANTINED` with four distinct
states (`NEEDS_REVIEW`/`UNSUPPORTED`/`EXCLUDED`/`FAILED`, above) so a
non-processing outcome is never forced into language that implies a
failure it isn't. Uniqueness: `content_identity_groups
(identity_kind, identity_algorithm, identity_hash)` UNIQUE (not
`identity_hash` alone — see the identity-domain discussion above);
`documents.content_identity_group_id` UNIQUE; `provenance_links
(source_instance_id, sequence_index)` UNIQUE.

**11. Migration strategy from the existing `Document`/`DocumentChunk`
model** — purely additive. Five new tables
(`discovery_runs`, `classification_runs`, `source_instances`,
`provenance_links`, `content_identity_groups`); exactly one new
nullable column on `Document` (`content_identity_group_id`); zero
changes to `DocumentChunk`; zero columns dropped anywhere.
Any existing `Document` rows (from prior personal-corpus import
testing, if any exist) start with `content_identity_group_id = NULL` —
backfilling them (one `ContentIdentityGroup` per distinct existing
`content_hash`) is real data-transformation logic, explicitly **not**
designed in this pass, deferred to whenever implementation is
authorized. New ingestion code, once built, always populates the
column going forward. This keeps rebuildability intact at the schema
level: nothing about this migration requires destroying or re-deriving
existing rows to apply.

**12. Snapshot/reference semantics for reproducibility** —
`DiscoveryRun` + `ClassificationRun`, above — deliberately scoped down
from the heavier, still-dormant `CorpusSnapshot` vision (item 9 /
prior-art item 3 in the dormant backlog): this gives each already-real
D0/D1/D2 run a durable, hash-verifiable identity without claiming an
atomicity the live, Syncthing-active corpus never actually had.

**13. Distinction between filesystem/source facts, inferred
provenance, and human decisions** — structurally, not just
conventionally, separated: `SourceInstance.evidence_snapshot` (JSONB,
immutable, filesystem facts + D1/D2 machine inference only, snapshotted
verbatim at classification time) versus `SourceInstance.canonical_status*`
(real typed columns, mutable, human decisions only). A query for "what
did a human decide" can never accidentally return inference JSON, and
vice versa, because they are different column types in different parts
of the row, not merely different keys inside one blob.

### Cross-table invariant verification (round 5, pre-freeze consistency check)

Requested explicitly before freezing: verify every edge as a single
table (cardinality, optional/required, immutability, unique
constraints, creating event, allowed-to-change event), not only as
scattered prose, and check specifically for contradictions between
immutable `evidence_snapshot`, mutable `canonical_status`, the
`ContentIdentityGroup` lifecycle, nullable identity pre-hash, and
nullable `Document` pre-processing. One real correction and two real
constraint additions came out of doing this properly (below the table).

**Note on the requested diagram's shape**: the edge drawn as
"`ProvenanceLink` → leads to → `ContentIdentityGroup`" is a conceptual
dependency, not a literal foreign key — there is no FK from
`ProvenanceLink` to `ContentIdentityGroup` anywhere in this schema. The
actual FK is `SourceInstance.content_identity_group_id`;
`ProvenanceLink` only ever describes one `SourceInstance`'s own
ancestry and has no relationship to `ContentIdentityGroup` at all. The
table below uses the real FK graph.

| Edge | Cardinality | Optional/Required | Immutability | Unique constraint | Created by | Allowed to change |
|---|---|---|---|---|---|---|
| `DiscoveryRun` → `ClassificationRun` | 1 `DiscoveryRun` : N `ClassificationRun` (via 3 separate per-kind FKs) | each of the 3 FKs individually nullable; **CHECK added (round 5)**: at least one must be non-null | both sides fully immutable | none (reuse across runs is expected and normal) | classification stage, at `ClassificationRun` creation | never |
| `ClassificationRun` → `SourceInstance` | 1 : N | `SourceInstance.classification_run_id` NOT NULL | fully immutable | none | classification stage, per discovered path | never |
| `SourceInstance` → `ProvenanceLink` | 1 : 1..N (chain length = nesting depth + 1) | every instance *should* have ≥1 link — an app-level invariant, not DB-enforced (same accepted limitation class as `DuplicateReview`'s un-enforced "≥1 member") | fully immutable | `(source_instance_id, sequence_index)`; **CHECK added (round 5)**: `sequence_index = 0 ⟺ kind = T7_FILE AND parent_link_id IS NULL` | classification stage, atomically with the owning `SourceInstance` | never |
| `SourceInstance` → `ContentIdentityGroup` | N : 0..1 | nullable | **write-once** (round 5 correction, not fully immutable — see below) | none on this FK (many instances per group is the point) | whichever step first computes this instance's hash — classification itself if D1 already hashed it, otherwise a later extraction/hashing step | exactly one `NULL → value` transition, then fixed forever |
| `ContentIdentityGroup` → `Document` | 1 : 0..1 | `Document.content_identity_group_id` required at creation for any new `Document` (nullable column exists only for pre-migration legacy rows) | immutable from `Document`'s creation moment onward | `documents.content_identity_group_id` UNIQUE | the pipeline step that first needs a `Document` row to exist — no later than immediately before the first `DocumentChunk` is written, since `DocumentChunk.document_id` requires a parent | never after creation |
| `Document` → `DocumentChunk` | 1 : N (existing, unchanged) | `DocumentChunk.document_id` NOT NULL (existing) | `DocumentChunk` rows immutable (no `updated_at` exists on this table today) | `(document_id, chunk_index)` UNIQUE (existing) | chunking step | never |

**The one real correction this check surfaced**: `SourceInstance.
content_identity_group_id` cannot be "fully immutable" as round 4's
prose implied — it is **write-once**, a third mutability class distinct
from both "fully immutable" and "freely mutable." This also corrected
a framing error carried since round 2: the deferred-identity case is
**not archive-member-exclusive**. D1 only hashed files that shared a
size with at least one other file (its size-collision filter) — a
uniquely-sized *loose* T7 file was never hashed by D1 either, and its
`SourceInstance` starts with `content_identity_group_id = NULL` for
exactly the same reason an archive member does. Checklist item 2 above
is corrected to reflect three mutability classes, not two.

**Precise `Document`-existence rule, pinned down by this check** (round
4 left this implicit): `Document` existence is **not** synonymous with
`pipeline_state = INGESTED`. Since `DocumentChunk.document_id` requires
a parent row, `Document` must exist no later than the transition that
first produces chunks — i.e. by `CHUNKED`, at the latest.
`pipeline_state` keeps advancing on the owning `ContentIdentityGroup`
afterward (`EMBEDDED` → `INGESTED`) independent of the `Document` row
already existing: "has a `Document`" means "reached at least `CHUNKED`";
`INGESTED` means the whole pipeline — embeddings included — finished.
These are related, not identical, facts, and no code should treat them
as interchangeable.

**Verifying the specific scenario the pre-freeze request named**: does
a `ContentIdentityGroup` already at `INGESTED` (with a `Document`
already produced) create any contradiction when a *second*,
later-discovered `SourceInstance` (say, an archive member extracted
weeks after the first loose-file copy was ingested) resolves to the
same `identity_hash`? No — this is the idempotency key working exactly
as designed: the second instance's write-once
`content_identity_group_id` simply resolves to the **already-existing**
group, gains a new `SourceInstance` row as a new provenance reference
against the already-existing `Document`, and triggers no re-extraction,
re-chunking, or re-embedding. No contradiction between the immutable
`evidence_snapshot` (this new instance's own, separately recorded),
mutable `canonical_status` (this new instance starts `UNRESOLVED` in
its group, independent of any other instance's status), the group's
`pipeline_state` (already `INGESTED`, untouched), and the group's
existing `Document` (untouched). One genuine open question this
scenario surfaces, **explicitly deferred to implementation, not a
schema contradiction**: how concurrent classification/extraction races
over the *same* `identity_hash` are serialized (e.g. two instances
resolving their hash at nearly the same moment, both about to create a
group) — ordinary database-level concurrency control (a unique
constraint plus a retry-on-conflict, matching how this codebase already
handles similar races elsewhere) is expected to be sufficient, but the
exact mechanism is implementation detail, not schema design.

**Result: no contradiction found between immutable `evidence_snapshot`,
mutable `canonical_status`, the `ContentIdentityGroup` lifecycle,
nullable identity pre-hash, and nullable `Document` pre-processing.**
Two constraints were added (`ClassificationRun`'s "at least one input"
CHECK, `ProvenanceLink`'s root-shape CHECK) and one mutability
classification was corrected (`SourceInstance.content_identity_group_id`
is write-once, not immutable, and its deferred-identity case is not
archive-member-exclusive) as a direct result of doing this check
properly rather than restating round 4's prose.

### What this design pass explicitly does NOT decide or build
- Any actual Alembic migration file, model file, or service code — the
  sketches above are illustrative column lists, not files to create.
- The `Document`/`ContentIdentityGroup` backfill logic for any existing
  rows (checklist item 11's open sub-question).
- The heavier, atomic `CorpusSnapshot` concept from the dormant
  backlog — `DiscoveryRun`/`ClassificationRun` are a deliberately
  smaller, honestly non-atomic answer to reproducibility, not that.
- Any canonical-status *selection policy* — still just "a human
  decided," per `29d6864`.
- Logical document identity or document/version modeling (identity
  layers 3/4) — still deliberately deferred, unchanged from `29d6864`.
- Any T7 access, extraction, embeddings, or ingestion of any kind.
  This section was written entirely from the existing codebase (models,
  services, decisions 0001/0002) and the frozen `29d6864` design; no
  new scan, hash, or read of either T7 path occurred to produce it.

## Schema/Model Implementation: SourceInstance / ProvenanceLink / ContentIdentityGroup / Document / DocumentChunk

A sixth T7 gate, opened after the `14b8063` schema design froze.
Implements exactly that design - no T7 access, no extraction, no
real-corpus ingestion, no embeddings, no dedup execution, no
deletion/quarantine, no canonical-copy selection beyond the frozen
schema semantics. Every synthetic-fixture test uses `aibrain_test`
only; the main `aibrain` database's Alembic head was left untouched at
`ef65da409302` - the new migration was applied and verified against
`aibrain_test` alone, and is ready to apply to `aibrain` whenever a
future gate calls for it.

**Files added**: `app/models/discovery_run.py`, `classification_run.py`,
`content_identity_group.py`, `provenance_link.py`, `source_instance.py`
(one model/enum module each, matching this codebase's one-model-per-file
convention); `app/classification/` (new module: `discovery_run_service.py`,
`classification_run_service.py`, `content_identity_service.py`,
`source_instance_service.py`, `canonical_decision_service.py`);
`alembic/versions/b9a82d073399_*.py`;
`tests/integration/test_content_identity_schema_execution.py` (17
tests) and `test_content_identity_concurrency_execution.py` (2 tests).
**Files modified**: `app/models/document.py` (one new nullable, UNIQUE
`content_identity_group_id` column) and `app/models/__init__.py`
(registers the five new models).

**Migration `b9a82d073399`** (`ef65da409302` → `b9a82d073399`): five new
tables (`discovery_runs`, `classification_runs`, `content_identity_groups`,
`source_instances`, `provenance_links`) plus one new nullable column on
`documents`, with every constraint from the frozen design present:
`uq_content_identity_groups_identity` (the `identity_kind`/
`identity_algorithm`/`identity_hash` triple), `uq_provenance_links_
source_instance_id_sequence_index`, `uq_documents_content_identity_
group_id`, `ck_classification_runs_at_least_one_discovery_run`,
`ck_provenance_links_root_shape`, `ck_source_instances_canonical_
status_requires_evidence`. Verified against real `aibrain_test`:
`upgrade` → `downgrade` → `upgrade` all ran cleanly with no manual
intervention; `\d` on every new table confirmed the constraints landed
exactly as designed.

**Model relationships/cardinality**: implemented exactly as the
`14b8063` round-5 invariant table specifies - see that section for the
full table. One clarification surfaced during implementation, not a
change to the design: SQLAlchemy's `Enum` type persists a Python
`str, Enum` member by its `.name` (e.g. `'T7_FILE'`), matching this
codebase's existing convention (`DedupPlanStatus.GENERATED.name` as
`server_default`, etc.) - every `CHECK` constraint's string literals
(e.g. `kind = 'T7_FILE'`) were written against that convention and
verified to match what Postgres actually stores.

**How write-once `content_identity_group_id` is enforced**:
`ContentIdentityService.assign_content_identity` issues `UPDATE
source_instances SET content_identity_group_id = :group_id WHERE id =
:id AND content_identity_group_id IS NULL`, then checks the row count.
Under Postgres's default READ COMMITTED isolation, the `WHERE` clause
is re-evaluated against the latest committed data at `UPDATE` time, so
a second attempt against an already-assigned instance affects zero
rows; the service treats `rowcount == 0` as a hard `ValueError` rather
than a silent no-op, so a caller can never mistake "already set" for
"successfully set." `test_assign_content_identity_is_write_once`
proves the second attempt is refused and the first value is preserved.

**How same-`identity_hash` concurrent claims are serialized**:
`ContentIdentityService.get_or_create_group` is a check-then-insert
that relies on the database's own `UNIQUE` constraint as the actual
race-safety mechanism, not a Python-side lock (which cannot protect
against two separate connections/processes). It queries first; if
nothing exists, it inserts and commits. If a concurrent transaction won
the race, the `INSERT` raises `IntegrityError` on commit; the loser
rolls back, re-queries, and returns the winner's row instead of
raising. **Both callers succeed** - this is a convergence, not a
winner/loser race like Chain 1's `execute()` lock (which deliberately
lets exactly one caller win and the other fail cleanly). The
distinction matters: two classification passes discovering the same
content identity from different physical occurrences should both walk
away with a usable, correct answer, never a spurious error.

**The concurrency test itself** (the user's explicit, non-negotiable
requirement for this gate - "proven by an actual database concurrency
test, not merely assumed from the unique constraint"): two, then ten,
genuinely separate `Session`/connection pairs, synchronized with a
`threading.Barrier` so every caller starts its own `get_or_create_group`
call at (as close as threads allow) the same moment - the same pattern
already used for Chain 1's `test_concurrent_execute_calls_only_one_
claims_and_mutates`. Both tests assert: no thread raises; every thread
returns the identical `ContentIdentityGroup.id`; exactly one row exists
in the database afterward (checked via a fresh connection, not the
ORM's local identity map, so a bug that created two Python objects
sharing an id by accident could not hide a real second row). A
separate, ad-hoc, non-committed verification script forced genuine
contention (slowing one thread's commit to guarantee the other's
`INSERT` raced against an uncommitted row) and confirmed the
`IntegrityError`-then-refetch path is actually exercised, not merely
untested good luck from fast sequential execution - both threads still
converged on one row under forced contention. The two permanent tests
passed 15/15 consecutive runs with no flakiness.

**Test isolation**: every new test binds its `Session` to a connection
held in an explicit outer transaction, using SQLAlchemy 2.0's
`join_transaction_mode="create_savepoint"` - the services under test
correctly call `session.commit()` internally (real production
behavior), so each such commit only releases a `SAVEPOINT`; the outer
transaction is rolled back at teardown, so no synthetic row from any
test in this milestone was ever left behind in `aibrain_test`.
Verified directly via `psql` before and after the full run: zero rows
in all five new tables both times.

**Regression results**: full suite run three times, `652 passed, 1
skipped` every time (633 pre-existing + 17 schema/service tests + 2
concurrency tests), zero flakiness. The main `aibrain` database's
tables and Alembic head were confirmed unchanged throughout.

**What this implementation pass does NOT include** (unchanged scope
boundary from the authorization): no API routes or Pydantic schemas
for these new tables (not requested); no backfill logic for existing
`Document` rows lacking `content_identity_group_id` (explicitly
deferred in `14b8063`); no logical-document/version modeling (identity
layers 3/4, still deferred); no canonical-selection policy beyond a
human explicitly calling `CanonicalDecisionService.decide`; the
migration was not applied to the main `aibrain` database; no T7 access,
extraction, embeddings, or real-corpus ingestion of any kind.

### Review round 6 (pre-commit code/schema audit) — two real bugs found and fixed

Requested before committing `b9a82d073399`: verify six specific
invariants against the actual code and running Postgres schema, not
just against the design document. Two of the six surfaced real defects,
fixed below; the other four were verified correct as implemented.

**1. `ClassificationRun` → `DiscoveryRun` cardinality** — verified, no
defect. One `ClassificationRun` references 0–1 `DiscoveryRun` per kind
(D0/D1/D2) via three separate nullable FK columns; `\d
classification_runs` confirms `ck_classification_runs_at_least_one_
discovery_run` is present, and a direct-insert test bypassing the
service (`test_classification_run_requires_at_least_one_discovery_
run_db_level`) confirms the database itself - not merely the service -
refuses a row with all three null.

**2. `identity_hash` race handling — real defect, fixed.** The original
`except IntegrityError` caught ANY integrity failure on commit, not
specifically the `uq_content_identity_groups_identity` violation -
meaning an unrelated bug (a stray FK violation, a different constraint)
could have been silently reinterpreted as "lost the identity race" and
masked. Fixed: the handler now inspects `exc.orig.diag.constraint_name`
and re-raises anything that isn't exactly
`uq_content_identity_groups_identity`, including the case where the
driver exposes no constraint name at all. Proven by four new mocked
unit tests in `tests/classification/test_content_identity_service.py`
(real Postgres isn't needed for this - it's pure exception-dispatch
logic, verified separately from the real-database race behavior).

**3. Transaction scope — verified, documented, not redesigned.**
`get_or_create_group` and `assign_content_identity` both call
`session.commit()`/`rollback()` directly, matching every other service
in this codebase (`DocumentService.create_document`,
`SourceInstanceService.create_instance`, etc.) - each service call
owns its entire transaction. A `rollback()` here would discard ANY
other uncommitted work sharing that session. No current caller composes
these methods inside a larger shared transaction, so per the explicit
instruction not to redesign absent an actual contract requiring it,
this was documented precisely in both methods' docstrings (including
the fact that a future composable caller would need its own
`session.begin_nested()` SAVEPOINT, which nothing here provides) rather
than restructured.

**4. `ProvenanceLink` structural validation — real defect, fixed.** The
original CHECK constraint (`(sequence_index = 0) = (kind = 'T7_FILE'
AND parent_link_id IS NULL)`) correctly forbade a non-root row from
looking like a root, but did NOT require a non-root row to actually
HAVE a parent - an `ARCHIVE_MEMBER` row at `sequence_index = 1` with
`parent_link_id = NULL` would have passed. Fixed by tightening the
constraint to `(sequence_index = 0 AND kind = 'T7_FILE' AND
parent_link_id IS NULL) OR (sequence_index > 0 AND kind =
'ARCHIVE_MEMBER' AND parent_link_id IS NOT NULL)`, verified against
real Postgres (the orphan case is now rejected;
`test_provenance_link_root_shape_check_constraint_accepts_a_valid_non_
root_link` confirms the fix doesn't also reject legitimate chains).
Two cross-row invariants remain explicitly NOT database-enforced
(a single-row CHECK cannot see sibling rows) and are named precisely in
the model's docstring as accepted, service-enforced-only limitations:
that `parent_link_id` points to a link in the SAME `source_instance`
at a lower `sequence_index`, and that `sequence_index` values are
contiguous - both are guaranteed only because
`SourceInstanceService.create_instance` is the sole creation path and
always builds a chain in order.

**5. `SourceInstance` write-once concurrency — real test gap, closed.**
The original test suite only proved write-once under sequential
same-thread double-calls, not real concurrent contention. Added
`test_write_once_under_real_concurrent_sessions_exactly_one_assignment_
wins`: two separate sessions/threads, barrier-synchronized, attempt to
assign TWO DIFFERENT `ContentIdentityGroup`s to the SAME
`SourceInstance` simultaneously. Confirmed: exactly one assignment
succeeds, the other fails with the documented write-once `ValueError`,
and the database ends up holding exactly one of the two groups - never
both, never neither, never silently overwritten. 15/15 consecutive
runs, no flakiness. Mechanism: Postgres's row-level write lock
serializes the two `UPDATE`s on the same row; the second one to reach
it blocks until the first commits, then re-evaluates its `WHERE
content_identity_group_id IS NULL` clause against the now-non-null
value and affects zero rows.

**6. `evidence_snapshot` immutability — stated precisely, as requested,
no defect found.** NOT a database CHECK constraint or trigger (neither
exists, and a plain CHECK cannot compare old vs. new values). Enforced
by (a) documented contract and (b) the structural absence of any
mutating code path - `SourceInstanceService.create_instance` is
verified (`grep`) to be the only place in the codebase that ever
assigns this column, and it does so exactly once, at construction. A
direct `UPDATE` bypassing the ORM, or a future careless service
method, could still mutate it - nothing at the database level would
stop that. This is stated explicitly now in the model's docstring
rather than left merely implied, matching this design's existing,
accepted precedent for `Document.content_hash`'s consistency invariant.

**Final numbers after this review round**: focused tests
(`tests/classification/` + the two `test_content_identity_*_execution.py`
files) - 26/26 passing. Full suite - `659 passed, 1 skipped`, run three
times, zero flakiness (652 from the pre-review implementation + 7 new:
4 mocked exception-scoping tests, 2 `ProvenanceLink` CHECK tests, 1
write-once concurrency test). `aibrain_test` confirmed empty of
synthetic rows after every run; the main `aibrain` database's schema
and Alembic head (`ef65da409302`) remain untouched. No T7 access of
any kind occurred during this review round.

## Controlled T7 → AI_Brain Ingestion Design (APPROVED and frozen — no implementation yet)

A seventh T7 gate, opened after the schema/model implementation closed
at `1253a2e`. Scope, stated explicitly at authorization: **design
only**. No T7 access of any kind, no extraction, no hashing on the T7,
no embeddings, no database schema changes, no dedup executor changes,
no mutation of any kind. The deliverable is this document; nothing
here is implemented. Went through two review rounds (round 2's
worker/queue-concurrency, `FileAccessService`, and failure-semantics
corrections below) before being approved. **Status: frozen** — the
next gate is implementation of this design (still requiring its own
schema-extension gate first, per the concrete recommendations below),
not yet authorized.

### Grounding

In addition to everything already grounded in the `29d6864`/`14b8063`
sections above, this pass inspected `ImportJobService` and
`EmbeddingService` (not read closely before now):

- `ImportJobService.execute_job()` wraps
  `SourceScanner`/`ArchiveExtractor`/`DocumentIngestor`/embedding in a
  single `try`/`except` — **one failing file currently fails the whole
  job**, matching the gap already named in `29d6864`. `_embed_documents`
  is the one place that already isolates per-item failures (catches
  and logs, never aborts the batch) — a real, existing precedent this
  design reuses rather than inventing a new pattern.
- `EmbeddingService.embed_document()` is already fully idempotent by
  construction: it `DELETE`s a document's existing chunks and
  re-inserts fresh ones, every call. Re-running it twice on the same
  content produces the same end state. This is the load-bearing
  existing precedent for Q6/idempotency below.
- `FileAccessService._allowed_roots()` trusts **any** `ImportJob` with
  `status == COMPLETED`, resolving its raw `source_path` string as a
  live filesystem root chat tools may read from. This interacts
  directly with Q17 below and is addressed explicitly, not glossed
  over.

### The nine-question answer set (deliverables 1–9), then explicit deferrals (deliverable 10)

#### 1. What is ingested — eligibility categories

Six outcomes, not three: **discovered** (a path D0/D1/D2 named or
extraction revealed) → **eligible** (classification decided extraction
should be attempted) → one of **excluded** (`.c9r` ciphertext, or a
future policy exclusion — a decision, not a failure), **unsupported**
(format/type has no extractor today — a fact about capability, not
this file), **failed** (an attempt was made and it errored), or
**successfully ingested**. This maps directly onto
`ContentPipelineState` as already frozen in `14b8063` — no new states
proposed. Ordinary loose files and archives are eligible by default;
`.c9r` is excluded by policy (per D2's own established finding);
media/other-unsupported-format files are unsupported until an
extractor exists; corrupt/unreadable files are only discoverable as
`failed` *after* an extraction attempt, never pre-judged as such.

#### 2. The ingestion unit — three layers, never collapsed

- **Discovery/classification granularity**: `SourceInstance` — one row
  per physical occurrence, as already frozen.
- **Extraction granularity**: an archive member, discovered live
  during extraction (see Q3) — becomes its own `SourceInstance`, never
  folded into its parent's.
- **Pipeline granularity — the actual unit of work**:
  `ContentIdentityGroup`. This was already established in `29d6864`'s
  idempotency key and is restated precisely here because this gate's
  Q6 depends on it: **the extraction-onward pipeline (normalize →
  chunk → embed) runs per `ContentIdentityGroup`, never per
  `SourceInstance` and never per archive member.** No new "ingestion
  work item" table is proposed — `ContentIdentityGroup.pipeline_state`
  already *is* the queue state, and no schema change is needed to make
  it one. Concretely, there are two independent queues, both already
  expressible in the frozen schema with zero new columns:
  1. **Extraction queue**: `SourceInstance` rows with
     `content_identity_group_id IS NULL` — physical occurrences whose
     identity is not yet known and must be read to discover it.
  2. **Pipeline queue**: `ContentIdentityGroup` rows whose
     `pipeline_state` is not yet terminal — content identities needing
     normalization/chunking/embedding.

#### 3. `SourceInstance` creation — corrects an implicit assumption from `14b8063`

`14b8063`'s design implicitly assumed classification could create every
`SourceInstance` up front, including archive members. **This is wrong,
and is corrected here**: D1/D2 never opened a single archive — an
archive's internal member list is genuinely unknown until extraction
actually reads it. So:
- **Classification** creates exactly one `SourceInstance` per
  T7-visible path named in D0/D1/D2 (loose files and top-level
  archives, each starting with a one-link `T7_FILE`-root
  `ProvenanceLink` chain), with `evidence_snapshot` populated from
  actual D0/D1/D2 report data.
- **Extraction** creates every deeper `SourceInstance` (one per archive
  member, at whatever nesting depth), the moment an archive is opened
  and its members enumerated — never at classification time. These
  rows still reference the SAME `classification_run_id` as their
  parent archive's instance (the classification run that decided to
  extract the enclosing archive is "responsible for" what extraction
  produces from it — no schema change needed for this, since
  `classification_run_id` is already a plain FK, not scoped to
  "things D0/D1/D2 actually named"). Their `evidence_snapshot` is
  honest about this: it records `{"discovered_during_extraction":
  true, "parent_source_instance_id": ...}` rather than fabricating
  D1/D2 evidence that was never collected for a path D1/D2 never saw.

The live filesystem is never treated as authoritative for anything
already recorded: a `SourceInstance`'s `evidence_snapshot` is fixed at
creation (per `14b8063`'s already-frozen immutability contract) whether
that creation happened at classification time or extraction time.

#### 4. Content identity — when each identity kind is known

Restating `14b8063` precisely for the ingestion case: `ContentIdentityGroup.
identity_kind` stays `EXTRACTED_CONTENT`-only in this design too —
`SOURCE_BYTES`/`NORMALIZED_CONTENT` remain reserved, unpopulated, exactly
as frozen. When identity becomes knowable differs by case:
- A loose T7 file **D1 already hashed** (a size-collision candidate):
  identity is known at **classification** time — no extraction read is
  needed at all; `get_or_create_group` can be called immediately using
  D1's already-computed hash.
- A loose T7 file **D1 never hashed** (unique size — D1's own
  size-collision filter skipped it, exactly as `14b8063`'s round-5
  correction already established): identity is unknown until the
  ingestion pipeline itself reads and hashes the file, at the
  `EXTRACTING`→`EXTRACTED` transition.
- **Any archive member**: identity is never known before the member is
  actually read during extraction — D1 never opened archives, full
  stop. This is the one case Q5 exists to describe precisely.

#### 5. Archive processing flow, precisely

```
T7 archive file (one SourceInstance, one-link T7_FILE-root chain,
                 created at classification)
   |
   v
EXTRACTING: ArchiveExtractor opens it (reusing the EXISTING safety
   |         guards: path-traversal rejection, disk-space reserve,
   |         expansion-ratio bomb guard, symlink-member rejection -
   |         all already implemented in app/ingestion/archive.py,
   |         reused, not reinvented), enumerates members
   v
for each member:
   |
   +-- create a new SourceInstance (chain = parent's chain + this
   |   member's ARCHIVE_MEMBER link, sequence_index = parent depth + 1)
   |
   +-- member's bytes are now in hand (extraction just read them) ->
   |   compute its EXTRACTED_CONTENT/SHA256 hash immediately
   |
   +-- get_or_create_group(EXTRACTED_CONTENT, SHA256, hash)
   |     |
   |     +-- if the returned group's pipeline_state is already
   |     |   INGESTED: STOP HERE for this member. assign_content_
   |     |   identity() links this new SourceInstance to the existing
   |     |   group and its existing Document - no normalize/chunk/
   |     |   embed work repeats. This is the idempotency key from
   |     |   29d6864, made concrete: downstream work is skippable,
   |     |   the READ itself never was.
   |     |
   |     +-- if the member is ITSELF an archive: recurse (same
   |         extraction logic, depth + 1, the SAME safety guards
   |         re-applied at this level too - path-traversal and
   |         expansion-ratio checks must run at EVERY recursion
   |         level, not only the outermost one), plus a NEW
   |         recursion-depth limit (not designed to an exact number
   |         here - a bounded constant, analogous to
   |         ArchiveExtractor.HARD_EXPANSION_RATIO, to be set at
   |         implementation time) to prevent an archive-bomb-via-
   |         nesting distinct from the existing single-level
   |         expansion-ratio bomb guard.
   |
   +-- otherwise (new identity): proceed to NORMALIZED -> CHUNKED ->
       EMBEDDED -> INGESTED for this ContentIdentityGroup, exactly
       once.
```

#### 6. Idempotency — the durable boundary, and how it survives every failure mode named

The unit is `ContentIdentityGroup` (Q2). Restating and extending
`29d6864`'s idempotency key against each specific scenario the
authorization asked about:
- **Retries**: `pipeline_state` is the durable resume checkpoint - a
  crash mid-`EXTRACTING` leaves the group at `EXTRACTING` (or, more
  precisely, still `CLASSIFIED` if the state transition to `EXTRACTING`
  itself is written *before* the extraction attempt begins, so a crash
  never leaves a group claiming to be "in progress" on work that never
  actually started - this ordering is a real implementation detail to
  get right, named here, not solved in code).
- **Crashes**: same durability principle as Chain 1 (one durable row =
  resume checkpoint), explicitly **not** Chain 1's state machine
  (already established in `29d6864`, unchanged here). A resumer's
  query is trivial and needs no new schema: `WHERE pipeline_state NOT
  IN (INGESTED, EXCLUDED, UNSUPPORTED)` (whether `FAILED` and
  `NEEDS_REVIEW` are included in a resume sweep is a retry-policy
  choice, not a schema question - see Failure/retry matrix below).
- **Duplicate jobs / concurrent claims**: already solved, already
  tested, in `1253a2e` - `ContentIdentityService.get_or_create_group`'s
  real-database concurrency proof applies unchanged here. This gate
  does not need to design new concurrency handling; it needs to *call*
  what already exists.
- **Process restarts**: identical to "crashes" above. The one
  additional requirement this surfaces: **each pipeline step's own
  operation must itself be idempotent, not merely resumable-once** -
  `EmbeddingService.embed_document`'s existing delete-then-recreate
  pattern is the model to follow for normalization and chunking too
  (re-running a step against the same input must produce the same
  output state, not accumulate duplicates).

#### 7. Ingestion state machine — transitions, retryability, terminality, reversibility

Reusing `ContentPipelineState` exactly as frozen in `14b8063` - no new
states, but **one real correction to how `NEEDS_REVIEW` is used**,
found while answering Q18 below and stated here because it changes the
transition table: `NEEDS_REVIEW` is reachable only from `CLASSIFIED`
when **classification itself cannot decide eligibility** (a genuine
ambiguity about whether this content category should be attempted at
all) - it is **not** the same thing as D2's per-instance
`requires_human_review` evidence flag, which lives inside
`SourceInstance.evidence_snapshot` and drives `canonical_status`
resolution only. `29d6864` already established that content ingestion
must never wait on canonical resolution; a `ContentPipelineState.
NEEDS_REVIEW` that triggered on every D2-flagged group would silently
contradict that. This is a real inconsistency between `29d6864` and
`14b8063`, caught here, resolved by narrowing `NEEDS_REVIEW`'s scope to
eligibility-level ambiguity only.

```
DISCOVERED --(classification)--> CLASSIFIED
CLASSIFIED --(eligible)--------> EXTRACTING
CLASSIFIED --(no extractor)----> UNSUPPORTED         [terminal-for-now]
CLASSIFIED --(policy exclusion)-> EXCLUDED           [terminal-for-now]
CLASSIFIED --(eligibility ambiguous)-> NEEDS_REVIEW  [blocks further
                                                       progress until a
                                                       human/policy call]
EXTRACTING --(success)---------> EXTRACTED
EXTRACTING --(error)-----------> FAILED              [retryable -> EXTRACTING]
EXTRACTED  --(success)---------> NORMALIZED
EXTRACTED  --(error)-----------> FAILED              [retryable -> EXTRACTED's predecessor step]
NORMALIZED --(success)---------> CHUNKED
NORMALIZED --(error)-----------> FAILED              [retryable]
CHUNKED    --(success)---------> EMBEDDED
CHUNKED    --(error)-----------> FAILED              [retryable]
EMBEDDED   --(success)---------> INGESTED            [TERMINAL - success]
EMBEDDED   --(error)-----------> FAILED              [retryable]
```

- **Terminal, success**: `INGESTED` only.
- **Terminal-for-now, not permanently frozen by construction**:
  `UNSUPPORTED` and `EXCLUDED` - a *future* `ClassificationRun` (a new
  row, per the existing "reclassification is a new run, never an edit"
  convention) could revisit and potentially move such a group forward
  once capability changes (a new extractor, a policy change) - this is
  a reclassification EVENT, not a direct state-machine transition, and
  is named as an open question for the next schema-extension gate:
  today's schema has no `superseded_by_classification_run_id` field to
  record such a supersession, and none is added here.
  `NEEDS_REVIEW` is not retryable automatically; it requires an
  explicit human/policy decision before any forward transition.
- **Retryable**: `FAILED` only, and only back to the step that failed -
  never a full rollback to `DISCOVERED`. Retry policy (backoff, max
  attempts, automatic vs. manual) is explicitly implementation detail,
  not designed here.
- **Reversibility**: none of these transitions are reversible in the
  sense of undoing a completed step - there is no `INGESTED →
  EXTRACTED` transition. The only "reversal" concept in this design is
  retrying `FAILED` back to its own attempted step, or a future
  reclassification superseding `UNSUPPORTED`/`EXCLUDED`/`NEEDS_REVIEW`.

#### 8. Extraction/working-space design

Unchanged in principle from `0002`/`29d6864`: T7 (immutable, forever
read-only) → temporary extraction workspace (`documents/imports/
<job_id>/extracted/`, ephemeral) → durable AI_Brain derived artifacts
(the `Document`/`DocumentChunk` **rows**, not the on-disk working
copy - the working copy's own retention policy remains the still-open
question named in `29d6864`, unchanged here). Specifics for this gate:
- **Cleanup**: still an open policy question (unchanged from `29d6864`)
  - not resolved here either.
- **Disk-space checks**: reuse `ArchiveExtractor`'s existing
  `HARD_FREE_SPACE_BYTES`/`PREFERRED_FREE_SPACE_RATIO` mechanism as-is;
  these already check the *workspace's* disk, which is exactly right -
  the T7's own free space is irrelevant since it is never written to.
- **Partial extraction recovery**: a crash mid-extraction may leave a
  partially-written directory in the workspace. Because extraction is
  deterministic given the same archive bytes, the correct recovery is
  to **delete the partial directory and re-extract from scratch** on
  retry - never attempt to resume a byte-partial extraction.
- **Nested archive safety**: `ArchiveExtractor`'s existing
  path-traversal and expansion-ratio guards must be re-applied at
  **every** recursion level, not only the outermost call - easy to
  miss when generalizing a single-level extractor into a recursive
  one, named explicitly here so it isn't missed at implementation time.
- **Path traversal protection**: same point as above, stated for
  emphasis - a member path validated safe relative to its immediate
  archive's extraction directory must be re-validated at each nesting
  level, since a safe-looking relative path can still escape when
  composed across levels if not checked per-level.

#### 9. Snapshot / live-corpus semantics for ingestion

`DiscoveryRun` (already implemented) already carries the correct,
honest, non-atomic semantics - restated, not re-designed. The one gap
this gate surfaces: `SourceInstance` rows discovered **during
extraction** (Q3) have no corresponding D0/D1/D2 `DiscoveryRun` at all
- they were never named in any report, because D1/D2 never opened
archives. Their `evidence_snapshot`'s `discovered_during_extraction:
true` marker (Q3) is the honest record of this - it does **not**
retroactively claim these paths were part of any `DiscoveryRun`'s
observation. A future query asking "what fraction of the corpus has
been observed" (Q12) must account for this: paths discovered only via
extraction were never part of any D0/D1/D2 denominator to begin with.

### Deliverable 4: data-flow / provenance query graph (answers Q15)

Confirmed fully queryable with the schema exactly as already
implemented in `1253a2e` - zero gaps, zero new columns needed:

```
DocumentChunk.document_id
   -> Document.content_identity_group_id
      -> ContentIdentityGroup.id
         <- SourceInstance.content_identity_group_id (reverse, 1:N -
            every physical occurrence that ever resolved to this
            identity, across every ClassificationRun that ever
            observed one)
            -> SourceInstance.id
               <- ProvenanceLink.source_instance_id (reverse, 1:N)
                  -> walk parent_link_id to the root (sequence_index=0,
                     kind=T7_FILE)
                     -> root_t7_path (or, for the field itself,
                        SourceInstance.root_t7_path directly - already
                        denormalized for exactly this convenience)
```

This is `ProvenanceService.trace_document()`'s existing walk
(`ImportJob → Document → DocumentChunk → Message`), extended one hop
earlier exactly as `29d6864` already specified - no new subsystem, a
service-layer addition to an already-existing, already-tested query
pattern.

### Deliverable 5: failure/retry matrix (answers Q11, Q13)

| Failure | State reached | Retryable? | Notes |
|---|---|---|---|
| Corrupt PDF/DOCX/etc. | `FAILED` (from `NORMALIZED`'s predecessor) | Yes, manually/on-schedule | Not `UNSUPPORTED` - the *format* is supported, this *file* is bad |
| Malformed archive | `FAILED` (from `EXTRACTING`) | Yes | `ArchiveExtractor` already raises a typed error for this |
| Encrypted archive (no password available) | `UNSUPPORTED` | No (until a future capability exists) | A capability gap, not a per-file failure |
| Unsupported file type | `UNSUPPORTED` | No (until an extractor exists) | Decided at `CLASSIFIED`, before any attempt |
| Oversized file / exceeds expansion ratio | `FAILED` | Policy-dependent | `ArchiveExtractor`'s existing hard limits already produce a typed error |
| Extraction failure (other) | `FAILED` (from `EXTRACTING`) | Yes | |
| Normalization failure | `FAILED` (from `EXTRACTED`) | Yes | |
| Chunking failure | `FAILED` (from `NORMALIZED`) | Yes | |
| Embedding failure (e.g. Ollama unreachable) | `FAILED` (from `CHUNKED`) | Yes, likely transient | Matches `_embed_documents`'s existing per-item catch-and-skip precedent |
| T7 unavailable mid-`EXTRACTING` | `FAILED` | Yes, once T7 returns | Not data loss - the source file is presumed still there, just unreachable now |
| Insufficient workspace disk space | `FAILED` | Yes, once space is freed | Detected by the existing `ArchiveExtractor` disk-space check |

**Named schema gap, not fixed here** (database schema changes are out
of scope for this design pass): `ContentIdentityGroup` has no
`failure_reason`/error-detail column today, unlike `ImportJob.
error_message` or `DedupExecutionActionAudit.error_message`. A `FAILED`
group currently has no durable, structured place to record *why* -
this is a genuine, concrete recommendation for whatever schema-
extension gate comes before this design is implemented.

### Deliverable 6: idempotency model — see Q6 above (kept together with its own question rather than duplicated here)

### Deliverable 7: snapshot/live-corpus semantics — see Q9 above

### Deliverable 8: extraction workspace design — see Q8 above

### Deliverable 9: processing lineage design (sketch only, per explicit instruction not to build the full architecture yet)

The concept, not the schema: a future minimal lineage layer would need,
at minimum, an analogous pattern to `ClassificationRun.classifier_
version` generalized across every pipeline step - e.g. an
`extractor_version`/`normalizer_version`/`chunker_version`/`embedding_
model_version` recorded per `ContentIdentityGroup` (or per `Document`)
at the point each step runs, so a future answer can name "which parser
produced this text, which model produced this vector." The fuller
`Processor`/`ProcessorVersion`/`ProcessingRun`/`InputArtifact`/
`OutputArtifact` shape from the dormant research backlog (see
[[project-ai-brain-prior-art-research-priorities]]-equivalent doc
section) remains the eventual destination, but this gate does not
propose its schema - only confirms that the frozen `14b8063` design's
one-`ContentIdentityGroup`-per-pipeline-run shape is compatible with
attaching such fields later without restructuring anything already
built.

### Answering the remaining questions not yet covered above

**Q12 (partial corpus ingestion)**: no new "completeness" flag or table
proposed. Completeness is a **derived**, not stored, fact - a query
comparing paths named across `DiscoveryRun`-backed reports against
`SourceInstance`s with a terminal `pipeline_state` gives a live
completeness view. This is an observability/reporting concern (dormant
backlog item), not a schema concern, and is not designed further here.

**Q13 (T7 unavailability, continued)**: a "previously observed source
disappears" (a later D1 report no longer lists a path an earlier
`SourceInstance` named) does **not** mutate or delete the earlier
`SourceInstance` or anything derived from it - `SourceInstance` rows
are immutable historical facts (per `14b8063`); disappearance is
represented purely by *absence* from a later `DiscoveryRun`'s
underlying report, never as an edit to old rows. A dedicated
`OBSERVED`/`MISSING_FROM_LATEST_SNAPSHOT` status (dormant backlog item
11) would need a new schema field to track current-presence-per-
snapshot explicitly - not designed or added here.

**Q14 (path vs. identity)**: fully answered by mechanisms already
built, requiring no new design. A renamed/moved file with unchanged
content produces a *new* `SourceInstance` (new physical occurrence)
that resolves, via `get_or_create_group`, to the *same*
`ContentIdentityGroup` - "same content, new occurrence." A file at a
*stable* path whose content changes produces a new `SourceInstance`
whose hash differs, resolving to a *different* `ContentIdentityGroup` -
"same physical occurrence, new content" - while the old `SourceInstance`
(and anything already derived from its old content identity) remains
untouched, exactly the historical-record behavior `14b8063` already
guarantees.

**Q16 (rebuildability)**: in principle, `T7 (immutable) +
DiscoveryRun.report_sha256-verified reports + ClassificationRun.
classifier_version + SourceInstance/ProvenanceLink (durable) +
ContentIdentityGroup (durable)` is sufficient to re-derive `Document`/
`DocumentChunk` by re-running extraction/normalization/chunking/
embedding. **One real operational gap surfaced by checking this
carefully**: the actual D0/D1/D2 JSON report files
(`knowledge/t7_discovery/*.json`) are gitignored and exist only on this
one machine - if this machine's disk were lost, `report_sha256`
verification and full reconstruction would be lost even though the T7
itself might survive. This is a genuine rebuildability risk worth a
future gate's attention (e.g. a durable off-machine backup of discovery
reports), not something this design pass fixes. Also worth naming: an
embedding model change means "rebuilt" vectors are not bit-identical to
lost ones, only functionally similar - full rebuildability of
*embeddings specifically* also depends on pinning/retaining the exact
model version (Q10's territory).

**Q17 (privacy boundaries)**: two distinct decisions, both named
explicitly rather than defaulted silently:
1. **Storage vs. semantic indexing**: `SourceInstance.root_t7_path`/
   `member_path` and `ProvenanceLink.path` necessarily store real
   filenames/paths as structured provenance metadata - unavoidable,
   that's the whole point of provenance. This design's position: these
   fields are **not** independently embedded/semantically indexed as
   searchable text content (i.e. a filename is never fed into the
   embedding pipeline as if it were document text) - only actual
   extracted document content is embedded.
2. **Display/exposure**: whether a future chat answer's citation shows
   the raw T7 path verbatim, or a redacted/generic label, is a
   UI/prompt-design decision explicitly **not** made here (matches the
   dormant "privacy boundaries" backlog item) - named as a real
   decision a future RAG/UI gate must make deliberately, not one that
   should default silently to "show everything."
3. **A concrete, existing risk this gate identifies and mitigates at
   the design level**: `FileAccessService._allowed_roots()` trusts any
   `ImportJob.source_path` where `status == COMPLETED` as a live,
   chat-tool-readable filesystem root. If a future T7 ingestion run
   were represented as an `ImportJob` with `source_path = "/media/
   personal/Seenu_T7SSD1"`, completing it would silently grant every
   chat tool read access to the **entire T7**, far beyond anything the
   ingestion/identity layer actually vetted - a serious, unintended
   widening of exposure. **Recommended mitigation, a value convention
   requiring zero schema change**: a T7 ingestion run's `ImportJob.
   source_path` should be set to a non-path reference string (e.g.
   `"classification_run:<id>"`), never a real T7 directory.
   Verified precisely, not just asserted: `Path("classification_run:
   42").resolve()` resolves relative to the process's current working
   directory (no leading slash), producing a harmless, almost
   certainly nonexistent path - it can never satisfy `resolved.
   is_relative_to(root)` for a real T7 path a chat tool might request,
   so `FileAccessService` correctly, structurally fails closed. This
   is a recommended convention for the eventual implementation gate,
   not a code change made now.

**Q18 (human review)**: reuses `CanonicalDecisionService` exactly as
already built in `1253a2e` - no new mechanism. D2's `requires_human_
review` evidence (copied into `SourceInstance.evidence_snapshot` at
classification time, per `29d6864`) is what a human consults before
ever calling `decide()`; it is never auto-converted into a `CANONICAL`/
`NON_CANONICAL` decision by any code path, matching the standing rule
that inference must never be silently promoted to disposition. As
resolved under Q7 above, this human-review path is entirely orthogonal
to `ContentPipelineState.NEEDS_REVIEW`, which now means eligibility
ambiguity only, not "D2 flagged something."

### Review round 2 — worker/queue semantics, security hardening, failure-detail direction

Requested before freezing: tighten eight specific points. The review's
opening instruction was explicit - "do not assume the unique identity
constraint solves pipeline concurrency" - and that assumption is
exactly what round 1 left implicit. Corrected below, still design-only:
no code, schema, or migration changes are made in this round either.

#### 1. `ContentIdentityGroup` as work unit — the identity `UNIQUE`
constraint solves a *different* problem than pipeline concurrency

`1253a2e`'s `get_or_create_group` concurrency proof establishes that
two workers racing to **establish** the same identity converge on one
group. It says **nothing** about two workers racing to **advance** an
*already-existing* group's `pipeline_state` - that is a second,
separate concurrency problem, unaddressed until now.

**The concrete gap**: today's `ContentIdentityGroup` schema
(`pipeline_state`, `created_at`, `updated_at`) has no field recording
*who is currently working on advancing this group*, or *when they
started*. Without one, two workers could both see `pipeline_state =
'EXTRACTED'`, both start normalizing, and both write conflicting
`DocumentChunk` rows.

**Recommended design** (schema fields named precisely, not added in
this pass - a concrete requirement for the next schema-extension gate):
add `claimed_by` (text, nullable) and `claimed_at` (timestamp,
nullable) to `ContentIdentityGroup`. A worker claims a group with a
single atomic statement - the standard Postgres claim pattern already
proven safe by this codebase's own `get_or_create_group` and Chain 1's
`SELECT ... FOR UPDATE` claiming, generalized:

```sql
UPDATE content_identity_groups
SET claimed_by = :worker_id, claimed_at = now()
WHERE id = (
    SELECT id FROM content_identity_groups
    WHERE pipeline_state IN (<the state this worker advances FROM>)
      AND (claimed_by IS NULL OR claimed_at < now() - :lease_duration)
    ORDER BY created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING *;
```

`FOR UPDATE SKIP LOCKED` is what makes this safe under real concurrency
- a second worker's identical query simply skips a row another worker
already has locked, rather than blocking on it or double-claiming it.
This is the same category of primitive Chain 1 already relies on for
execution claiming, generalized here for a queue rather than a
single-shot execution - not a new invention, not Chain 1's specific
state machine either.

**Answering each sub-question explicitly**:
- **Claim**: the atomic `UPDATE ... WHERE ... FOR UPDATE SKIP LOCKED`
  above.
- **Preventing double-processing**: the same statement - a group with
  a fresh (non-expired) `claimed_at` is excluded from every other
  worker's claim query.
- **Worker crashes after claiming**: `claimed_by`/`claimed_at` remain
  set, `pipeline_state` remains at whatever it was before the crash
  (the crash happened *during* an attempt to advance it, so it never
  advanced). The group looks claimed but is making no progress.
- **Stale-claim recovery**: a lease duration (a bounded constant, value
  not chosen here) makes a claim eligible for reclaiming once
  `claimed_at` is older than the lease - the same `WHERE (claimed_by IS
  NULL OR claimed_at < now() - lease_duration)` clause already shown
  above *is* the recovery mechanism, not a separate one - no distinct
  "recovery service" is needed the way Chain 1 needed
  `recover_stale_execution`, because claim expiry alone is sufficient
  here (no filesystem mutation is at stake, only which worker gets to
  attempt a database write).
- **Retry semantics**: reclaiming a stale or `FAILED` group and
  re-attempting its current step is safe *specifically because* each
  step's operation is idempotent (Q6's `EmbeddingService.
  embed_document` precedent) - re-running normalize/chunk/embed against
  the same input produces the same result, never a duplicate.
- **Terminal failure**: not decided in this pass. Whether `FAILED`
  remains indefinitely retryable or accumulates an attempt count that
  eventually reaches a genuinely terminal "gave up" state depends on
  the attempt-metadata design in point 5 below - named as an open
  question there, not answered twice.
- **Idempotent retries**: as above - the correctness argument is "the
  operation is idempotent," not "the claim mechanism prevents
  duplication" (the claim mechanism prevents *concurrent* duplication;
  idempotency is what makes a *sequential* retry after a crash safe).

#### 2. `SourceInstance` identity-resolution queue — an even more acute version of the same gap

`SourceInstance` has **no state or claim field of any kind** today -
not even the single `pipeline_state` `ContentIdentityGroup` has. Two
different cases need two different answers, not one:

- **Archive members**: no per-member claim field is needed at all,
  because the natural unit of claiming is the **parent archive's own
  `SourceInstance`**, not each member individually. One worker claims
  the archive-file `SourceInstance` (via the same claim pattern as
  point 1, applied to whatever record represents "this archive needs
  extracting" - concretely, its owning `ContentIdentityGroup` if one
  exists for the archive container, or the `SourceInstance` itself if
  extraction is modeled as a pre-identity step - see below), opens it
  once, and within that single claimed unit of work: enumerates every
  member, creates every member `SourceInstance`, computes every
  member's `EXTRACTED_CONTENT` hash, and calls `get_or_create_group`/
  `assign_content_identity` for each - all inside one worker's
  transaction. No second worker can ever be mid-way through the SAME
  archive at the same time, because only the archive-level claim was
  ever contended for.
- **Uniquely-sized loose files** (no parent archive to claim instead):
  this case has no coarser unit to borrow a claim from - it genuinely
  needs its own claim mechanism. **Recommended design, not added in
  this pass**: the same `claimed_by`/`claimed_at` pattern from point 1,
  added to `SourceInstance` itself, used only for instances where
  `content_identity_group_id IS NULL` and no parent link exists (a
  root-level, unhashed loose file).

**Lifecycle, stated precisely**: `queue` (`content_identity_group_id
IS NULL`, root-level `SourceInstance`) → `claim` (per above) → `read
and hash the file` → `get_or_create_group` + `assign_content_identity`
(both already concurrency-proven in `1253a2e` - reused, not
redesigned) → on failure, see point 5 (this is exactly the case named
there where a failure has no home in today's schema, since
`SourceInstance` carries no failure field at all).

#### 3. `FileAccessService` — the full call chain, and a structural fix instead of a naming convention alone

Round 1's proposed mitigation was a *string convention*
(`"classification_run:<id>"` never resolving as a real path). This
review correctly identifies that a convention alone is one careless
future caller away from being violated - a structural check is
stronger. The full call chain was re-read end to end:
`read_file(path)` → `_allowed_roots()` (queries every `COMPLETED`
`ImportJob`, resolves each raw `source_path` to a `Path`) →
`resolved.is_relative_to(root)` for the *requested* path against every
allowed root → `resolved.is_file()` → `extract_text(resolved)`. The
only place a T7-backed reference could ever leak into "read access" is
`_allowed_roots()` - nowhere else in the chain even sees `source_type`.

**Structural fix, not a naming convention**: `_allowed_roots()` must
filter by `ImportJob.source_type` against an explicit **allow-list**,
not merely trust every `COMPLETED` job's `source_path` regardless of
kind. Checked directly against this codebase's actual usage (`grep`
across `tests/` and `app/`): every real caller today sets
`ImportJob.source_type = "filesystem"` - already, unanimously, before
this design pass touched anything. The recommended allow-list is
therefore exactly `{"filesystem"}` today, requiring **zero change to
any existing caller's behavior** - only an added filter in
`_allowed_roots()`'s query (`WHERE status == COMPLETED AND source_type
== 'filesystem'`, or an explicit small allow-list if more filesystem-
backed types are added later).

**Why an allow-list, not a deny-list, and why that choice matters**:
a deny-list (block known-bad `source_type`s) trusts every *new* type by
default - a future T7-reference type, or any other non-filesystem
concept nobody thought to block yet, would be silently trusted the
moment it's introduced. An allow-list trusts nothing by default - a
new `source_type` is unreadable until someone deliberately adds it to
the allow-list, which forces a conscious decision at the moment it
matters. This is the same fail-closed posture already established as
a house principle after the D2 safety-follow-up incident
("application safety must not depend on the OS happening to deny an
unsafe operation") - applied here to a different mechanism, the same
underlying discipline.

**Explicit answer to "why can a T7-backed classification/import object
never become a chat-readable filesystem root"**: because (a) its
`source_path` never contains a real filesystem path in the first place
(point 7 below states precisely which objects may hold one), and (b)
even if it somehow did, its `source_type` would not be in `{"filesystem"}`
and `_allowed_roots()` would exclude it by construction, not by
convention. Two independent reasons, not one - a violation of (a)
alone is still caught by (b).

#### 4. `EmbeddingService` crash semantics — verified by re-reading the code, not assumed

`embed_document()`'s `DELETE` and subsequent `INSERT`s are issued on
the **same session, with no intermediate commit** between them - the
only `db.commit()` for the non-empty-chunks path is the single call at
the very end, after both the delete and the adds. Confirmed against
`app/db/session.py`'s `SessionLocal` configuration and SQLAlchemy 2.0's
"autobegin" transaction semantics: a transaction begins implicitly on
first use and persists until an explicit `commit()` or `rollback()` -
there is no intermediate auto-commit point hiding inside this method.

**Stated precisely, per stage**:
- **Crash after the `DELETE` statement is issued, before `commit()`**:
  the `DELETE` is part of an uncommitted transaction. Postgres rolls
  back the entire transaction when the connection drops - the delete
  never takes effect. **Old chunks are preserved, unchanged.** Not a
  partial-delete state; a full no-op from the database's perspective.
- **Crash during `self.embedding_client.embed(chunks)`** (the Ollama
  network call): still before any add or commit - same outcome as
  above. Old chunks preserved.
- **Crash during `db.add_all(records)` or before the final
  `db.commit()`**: still uncommitted - same outcome. Old chunks
  preserved.
- **The one residual ambiguity, common to any transactional system, not
  specific to this code**: a crash *during* the `commit()` call itself,
  where the command reached Postgres and was applied, but the
  acknowledgment never reached the client. This is not resolvable by
  inspecting this method in isolation - **it is resolved by
  idempotency**: calling `embed_document()` again is always safe
  regardless of which outcome actually occurred, because it starts
  with the same `DELETE` and produces the same end state either way.

**Conclusion, stated as the review required - not silently assumed**:
this existing implementation *is* effectively atomic for the delete+
recreate pair (single uncommitted transaction, all-or-nothing), and the
one edge case a database transaction cannot resolve on its own is
covered by the operation's own idempotency, not by a false claim of
distributed-transaction atomicity. No change to `EmbeddingService` is
proposed - the existing behavior is correct and is now documented as
such, with the reasoning shown rather than asserted.

#### 5. Failure semantics — a recommended direction, not implemented

**Recommended shape** for the next schema-extension gate, chosen by
weighing the two options the review named against this codebase's own
established pattern:

A **per-attempt audit row** (one new table, e.g. `ContentPipelineAttempt`
- name illustrative, not final), recording `content_identity_group_id`,
`attempted_stage` (which forward transition was being tried),
`failure_code` (a small controlled vocabulary - `corrupt_archive`,
`extraction_error`, `embedding_unavailable`, etc.), `failure_detail`
(free text - the actual exception message, for debugging), `attempted_
at`, `worker_id`. **This is the recommended direction**, not the
simpler alternative (a handful of mutable columns directly on
`ContentIdentityGroup` - `last_failed_stage`/`failure_code`/
`failure_detail`/`attempt_count`), because this codebase already has a
strong, repeated precedent for exactly this shape: `DedupExecutionActionAudit`
and `DiscoveryRun`/`ClassificationRun` themselves are all one-row-per-
event, append-only audit records, not mutable "latest status" columns
bolted onto a parent row. An audit table preserves the *full retry
history* ("why does this keep failing, across every attempt") rather
than only the most recent failure - directly useful for the debugging
`FAILED` groups will need. The simpler column-only alternative remains
available as a lighter fallback if the schema-extension gate weighs the
trade-off differently; this design pass states a recommendation, not a
mandate.

**Distinguishability, stated explicitly since this is exactly what the
review is probing for**: `failure_code`/`failure_detail`/`attempted_
stage` (wherever they end up living) are populated **only** when
`pipeline_state = 'FAILED'`. They are never populated for, and never
used to distinguish, `EXCLUDED` (whose reasoning already lives in
`SourceInstance.evidence_snapshot`/documented policy, not a failure),
`UNSUPPORTED` (a capability fact - "no extractor exists," not a
per-attempt detail), or `NEEDS_REVIEW` (whose reasoning is the
eligibility-ambiguity finding itself, already the state's whole
meaning). Four states, four different kinds of "why," never
conflated into one shared detail field.

#### 6. Discovery report rebuildability — gitignored is not disposable

**The design decision, stated explicitly**: D0/D1/D2 report files
(`knowledge/t7_discovery/*.json`) are **durable, high-value, hard-to-
reproduce-identically artifacts**, not disposable/regenerable-on-demand
junk. Gitignoring them protects against committing real personal
filenames into shared git history (a privacy control) - it says
nothing about backup, and this design pass corrects the conflation.
Re-scanning the T7 to "regenerate" a lost report would **not** reliably
reproduce it: the T7 is a live, Syncthing-managed corpus (established
since D0/D1/D2 themselves), so a new scan reflects a different point in
time, not a byte-identical replay of an old one - a lost report is a
genuine, non-trivial loss, not an inconvenience fixed by re-running a
script.

**Options considered and the recommendation**:
- Backing the reports up *onto the T7 itself* is explicitly **rejected**
  - it would violate the standing "T7 remains immutable, forever
    read-only" principle just to solve a backup problem for something
    else entirely.
- Committing them to git (even a private repository) is explicitly
  **rejected** - the reason they were gitignored (real personal
  filenames/paths) doesn't stop being true just because a repository is
  private.
- **Recommended**: treat this as an *operational* backup responsibility
  outside AI_Brain's own application code - the same way any personal
  backup routine already exists for a home machine - copy `knowledge/
  t7_discovery/*.json` to at least one location independent of this
  one machine's disk (an external drive, a personal cloud backup,
  etc.). AI_Brain's own responsibility, for a future gate, is limited
  to making the *need* visible rather than solving backup itself -
  e.g., a future health-check confirming a `DiscoveryRun.report_sha256`
  still matches a reachable file, surfacing drift or loss rather than
  silently trusting a stale reference.
- **Also named as a real alternative for a future schema-extension
  gate to weigh, not decided here**: storing report content (or a
  compacted, critical subset of it) directly in Postgres as a `DiscoveryRun`
  column, so the reports inherit whatever backup discipline already
  covers the database itself, rather than needing a wholly separate
  file-backup policy. Not proposed as a schema change in this pass.

#### 7. Real/derived source boundary — an explicit allow/forbid list

Stated once, precisely, tying points 3 and 7 together as one coherent
rule:

**MAY hold a real T7 path, as inert metadata only, never as a
live-dereferenceable filesystem root**: `DiscoveryRun.source_root`,
`SourceInstance.root_t7_path`/`member_path`, `ProvenanceLink.path`.
Already documented (in `29d6864`/`14b8063`) as never re-dereferenced as
a live path after classification runs - reaffirmed here, unchanged.

**MUST NEVER hold a real, live-dereferenceable T7 path**: `ImportJob.
source_path` for any T7-sourced ingestion run (must hold a non-path
reference such as `"classification_run:<id>"`, per point 3) - and
`Document.source`, which already means the on-disk **working-copy**
path under `documents/imports/<job_id>/`, never the T7 path itself,
for T7-sourced documents exactly as it already means for personal-
corpus documents today - no new exception introduced.

**The actual authority boundary, stated as one rule**: a real T7 path
existing anywhere in the database, as provenance evidence, never by
itself grants filesystem access. Access is gated **exclusively** through
`FileAccessService._allowed_roots()`'s `source_type` allow-list (point
3) - and every object that legitimately carries a real T7 path
(`DiscoveryRun`, `SourceInstance`, `ProvenanceLink`) has no `source_type`
field and is never consulted by `_allowed_roots()` at all, structurally,
not by omission that could later be "fixed" into a leak.

### Deliverable 10: explicit list of decisions still deferred (updated after review round 2)

**Now a decided *direction*, not an open menu** (round 2 committed to
these, even though none is implemented in this pass):
- Worker claiming for `ContentIdentityGroup`: `claimed_by`/`claimed_at`
  columns + `SELECT ... FOR UPDATE SKIP LOCKED`, lease-based stale
  recovery (round 2, point 1).
- Identity-resolution claiming for root-level, unhashed
  `SourceInstance`s: the same `claimed_by`/`claimed_at` pattern, added
  to `SourceInstance` itself; archive members need no such field
  because their unit of claiming is their parent archive (round 2,
  point 2).
- `FileAccessService._allowed_roots()` must filter by an explicit
  `source_type` **allow-list** (`{"filesystem"}` today), not merely
  trust every `COMPLETED` `ImportJob` (round 2, point 3) - a code
  change, not a schema change, for the implementation gate.
- Failure detail: a per-attempt audit table (recommended,
  `ContentPipelineAttempt`-shaped) over mutable columns directly on
  `ContentIdentityGroup`, matching this codebase's existing audit-row
  precedent (round 2, point 5).
- Discovery reports are durable artifacts requiring an operational,
  off-machine backup routine outside AI_Brain's own code - never
  backed up onto the T7, never committed to git (round 2, point 6).
- The real/derived source boundary is now an explicit allow/forbid list
  (round 2, point 7), not a single convention.

**Still genuinely open, exact values/schemas not chosen**:
- The exact lease duration for stale-claim recovery (point 1).
- The exact resume-sweep policy for `FAILED` (auto-retry vs. manual,
  backoff, max attempts) and whether `FAILED` ever becomes a distinct,
  bounded-retry terminal state (points 1 and 5).
- The retention policy for the extraction workspace (still open since
  `29d6864`).
- The exact recursion-depth limit for nested-archive extraction (a
  bounded constant, value not chosen here).
- A `superseded_by_classification_run_id`-style field (or equivalent)
  for `UNSUPPORTED`/`EXCLUDED`/`NEEDS_REVIEW` groups a future
  reclassification might move forward - not designed, not added to the
  schema here.
- The exact schema for `ContentPipelineAttempt` (or the lighter
  column-only alternative, if a future gate weighs the trade-off
  differently) - a direction is recommended, not a final schema.
- The full `Processor`/`ProcessorVersion`/`ProcessingRun` architecture
  (Q10) - only the minimal per-step version-field concept is sketched.
- Citation/UI display policy for real T7 paths (Q17.2).
- Whether discovery-report content should eventually move into
  Postgres itself rather than staying as external files (round 2,
  point 6's named alternative).
- Corpus-completeness reporting/observability (Q12) - derivable from
  existing data, but no view or endpoint is designed here.
- Everything already deferred by `29d6864`/`14b8063` and unchanged by
  this pass: logical document/version identity (identity layers 3/4),
  any canonical-selection algorithm beyond an explicit human decision,
  resource limits/backpressure/scheduling (Track 2), and the exact
  `SourceInstance` backfill logic for any pre-existing `Document` rows.

### What this design pass explicitly does NOT decide or build
- Any code, migration, or schema change of any kind.
- The `ImportJob.source_type`/`source_path` convention change for T7
  runs (Q17.3) - recommended, not applied.
- Any T7 access, read, hash, extraction, or embedding. This section was
  written entirely from the existing, already-implemented codebase
  (`app/ingestion/`, `app/classification/`, `app/services/`,
  `app/provenance/`, `app/files/`) and the frozen `29d6864`/`14b8063`
  designs; no new scan, hash, or read of either T7 path occurred to
  produce it.

## Ingestion Schema-Extension Implementation: worker claims + IngestionAttempt

An eighth T7 gate, opened after the Controlled T7 → AI_Brain Ingestion
Design froze at `6491dad`. Implements exactly the two schema
extensions that design recommended - worker claim/lease fields and a
durable per-attempt failure record - and nothing else. No T7 access,
no extraction, no embeddings, no real ingestion jobs, no ingestion
pipeline logic, no logical document/version semantics, no API/UI.

### What was built

**`ContentIdentityGroup`** gains `claimed_by` (nullable text) and
`claimed_at` (nullable timestamptz) - a transient, in-flight marker,
cleared on every attempt's completion (success or failure), never a
permanent record. Because the frozen `pipeline_state` enum has an
explicit in-progress marker for exactly one step (`EXTRACTING`) and
none for normalization/chunking/embedding, `claimed_by`/`claimed_at`
serve as the uniform in-progress signal across every step: claiming
for extraction also advances `pipeline_state` to `EXTRACTING` in the
same atomic statement (reusing the existing enum value as intended);
every other step leaves `pipeline_state` at its previous completed
value for the duration of a claim.

**`SourceInstance`** gains the same two columns, used only for
root-level, unhashed instances (`content_identity_group_id IS NULL`,
no parent link) - a uniquely-sized loose T7 file D1 never hashed.
Archive-member instances never use these fields: per the frozen
design, the claiming unit for an archive is its own parent
`SourceInstance`, not each member individually.

**`IngestionAttempt`** (new table) - one durable, immutable audit row
per attempt, matching this codebase's existing one-row-per-event
precedent (`DedupExecutionActionAudit`, `DiscoveryRun`,
`ClassificationRun`) rather than mutable "latest failure" columns.
Recorded for every attempt, success or failure, so full retry history
is always reconstructable. Correct parent relationship: exactly one of
`content_identity_group_id` (`PIPELINE_ADVANCE` attempts) or
`source_instance_id` (`IDENTITY_RESOLUTION` attempts) is set, enforced
by `ck_ingestion_attempts_parent_matches_kind` - mirroring
`DuplicateReview`'s existing "exactly one of content_hash/similarity,
depending on match_type" pattern rather than a generic polymorphic
association table. `failure_code` (a small controlled vocabulary
matching the failure/retry matrix from `6491dad` exactly - corrupt
input, malformed archive, oversized/expansion-limit, extraction/
normalization/chunking error, embedding unavailable, T7 unavailable,
insufficient disk space, permission denied, other read error),
`failure_detail`, and `retryable` are populated if and only if
`outcome = FAILED`, enforced by `ck_ingestion_attempts_failure_
requires_detail` - mirroring `SourceInstance.canonical_status`'s own
evidence-required constraint. Never populated for, and never confused
with, `EXCLUDED`/`UNSUPPORTED`/`NEEDS_REVIEW` - those `ContentPipelineState`
values carry their own, different reasoning elsewhere.

**`app/classification/worker_claim_service.py`** (`WorkerClaimService`)
- the concurrency-safe claiming primitive: `SELECT ... FOR UPDATE SKIP
LOCKED` (a row a concurrent caller has already locked is skipped, never
blocked on and never double-claimed) followed by an `UPDATE` in the
same transaction, generalizing Chain 1's own execution-claiming
primitive to a queue rather than a single-shot claim - not a reuse of
Chain 1's state machine. A claim is stale, and reclaimable by any
worker, once `claimed_at` is older than a caller-supplied
`lease_duration` - the same query that grants fresh work also
reclaims stale work; no separate recovery service exists or is needed.
`claim_content_identity_group`/`release_content_identity_group_claim`
and `claim_source_instance_for_identity_resolution`/
`release_source_instance_claim` cover both queues.

**`app/classification/ingestion_attempt_service.py`**
(`IngestionAttemptService`) - records outcomes a caller already
determined; performs no extraction, normalization, chunking, or
embedding itself. `record_pipeline_attempt`/`record_identity_
resolution_attempt`, both validating that a `FAILED` outcome always
carries `failure_code`/`failure_detail`/`retryable` - enforced twice,
once in Python (a clear `ValueError` before ever reaching the
database) and once by the DB `CHECK` constraint (so a direct-insert
caller bypassing this service is still refused).

### Migration `fd9f81672e59` (`b9a82d073399` → `fd9f81672e59`)

Purely additive: two new nullable columns each on
`content_identity_groups`/`source_instances`, one new table. Applied
and verified against `aibrain_test` only - `upgrade` → `downgrade` →
`upgrade` all ran cleanly; `\d` on every affected table confirmed every
column and constraint landed exactly as designed. The main `aibrain`
database's Alembic head remains untouched.

### Concurrency proof (the explicitly non-negotiable requirement for this gate)

Real Postgres, real separate sessions/threads, never mocked - matching
the same standard already applied to Chain 1's `execute()` race test
and to `ContentIdentityService.get_or_create_group`:

- **Concurrent claims on one group**: two threads race for the same
  eligible `ContentIdentityGroup` - exactly one wins, the other gets
  `None` back cleanly, never a corrupted or double claim.
- **Ten workers racing for five groups**: every group claimed exactly
  once total; no two workers ever claim the same group; verified via a
  fresh connection's `COUNT(DISTINCT claimed_by)`, not just the ORM's
  local view.
- **Stale-claim recovery under concurrency**: a claim aged past its
  lease is reclaimed by exactly one of two competing fresh workers -
  the crashed worker's identity never persists as the final claimant.
- **Competing workers across a mixed pool**: one unclaimed, one
  freshly (actively) claimed, one stale-claimed group, processed by
  six concurrent workers simultaneously - the actively-claimed group is
  never touched, the other two are claimed exactly once between them,
  proving the claim query's three-way `WHERE` logic holds under real
  contention, not only in isolated single-case tests.
- **Failure-attempt persistence under concurrency**: eight workers
  concurrently recording attempts for eight different groups - zero
  errors, all eight rows durably persisted.
- **Retry-history idempotency**: five concurrent, valid retry attempts
  for the SAME group all persist as independent rows (none lost, none
  merged); a parallel test proves the service's failure-field
  validation still guards every concurrent caller, not only sequential
  ones - five concurrent invalid calls all correctly raise `ValueError`
  and persist zero rows.

All 7 concurrency tests passed 15/15 consecutive runs with no
flakiness. `aibrain_test` confirmed empty of synthetic rows after every
run (this milestone's tests use direct `psql`-style verification via a
fresh connection, plus the existing `join_transaction_mode=
"create_savepoint"` isolation pattern for the non-concurrency tests).

### Full suite

**687 passed, 1 skipped**, run three times, zero flakiness (659
pre-existing + 28 new: 21 schema/service tests, 7 concurrency tests).
The main `aibrain` database's schema and Alembic head (`ef65da409302`)
remain untouched throughout implementation and verification. No T7
access of any kind occurred - `/media/personal/Seenu_T7SSD` and
`/media/personal/Seenu_T7SSD1` were neither read from nor written to.

### What this implementation pass explicitly does NOT include
- Any ingestion pipeline logic (extraction, normalization, chunking,
  embedding) - the claim/attempt primitives exist for a future,
  separately-authorized pipeline gate to call, not to be called by
  anything built in this milestone.
- Any real ingestion job, real `ImportJob` row for a T7 source, or the
  `source_type` allow-list hardening `6491dad` recommended for
  `FileAccessService` (a code change to an existing service, not a
  schema extension - deferred to the implementation gate that actually
  needs it).
- A `superseded_by_classification_run_id`-style field for future
  reclassification of `UNSUPPORTED`/`EXCLUDED`/`NEEDS_REVIEW` groups -
  still named, still not added.
- Any exact lease-duration value beyond what individual call sites
  pass as a parameter - no default policy is baked into the schema or
  services; a future pipeline gate chooses its own lease durations per
  call.
- Logical document/version identity (identity layers 3/4) - unchanged,
  still deferred.
- Any API routes, Pydantic schemas, or UI for any of this - not
  requested, not built.

## Controlled Ingestion Implementation: the pipeline itself

A ninth T7 gate, opened after the ingestion schema-extension closed at
`ce50875`. Implements the pipeline `6491dad` designed, using the
schema and claim/attempt primitives `ce50875` built, entirely against
synthetic fixtures - no T7 access, no real embeddings, no production
ingestion UI/API, no logical document/version semantics, no dedup
executor involvement, no delete/move/rename/quarantine of anything.
**Zero database schema changes were needed for this gate** - a
deliberate, notable outcome: archive-completion tracking reuses the
existing `IngestionAttempt` audit table (see below) rather than adding
a new column, exactly the kind of schema-avoidance this project favors
when an existing mechanism already fits.

### Modules built

- **`app/classification/eligibility_service.py`** -
  `classify_eligibility(path)`, a pure, suffix-only decision
  (`ELIGIBLE`/`EXCLUDED`/`UNSUPPORTED`) made *before* any read is
  attempted - `.c9r` is `EXCLUDED` (matching D2's established finding),
  a small explicit denylist of binary/media extensions is
  `UNSUPPORTED`, everything else is `ELIGIBLE`. Never opens a file;
  works even on paths that don't exist.
- **`app/classification/workspace.py`** - shared layout/helpers for
  where a `ContentIdentityGroup`'s raw bytes live once available
  (`documents/imports/.../group_<id>/content<suffix>`), and the
  eligibility-to-initial-`pipeline_state` mapping, used identically by
  identity resolution and archive processing.
- **`app/classification/identity_resolution_service.py`** -
  `IdentityResolutionService.resolve_next()`: claims one unresolved
  root-level `SourceInstance`, reads it (the ONE legitimate read of
  `root_t7_path` this design always intended - see the class docstring
  for why this doesn't contradict "never dereferenced again"), hashes
  it, classifies eligibility, calls the already-concurrency-proven
  `ContentIdentityService.get_or_create_group`/`assign_content_identity`
  from `1253a2e`, writes workspace content for eligible groups, records
  an `IngestionAttempt`, releases the claim. T7-unavailable/permission-
  denied/other-read-error are each their own `IngestionFailureCode`.
- **`app/classification/archive_processing_service.py`** -
  `ArchiveProcessingService.process_next_archive()`: claims one
  top-level, not-yet-processed archive `SourceInstance` (via a new
  `WorkerClaimService` query - see below), recursively extracts it
  (reusing `ArchiveExtractor` unmodified, including its existing
  path-traversal/expansion-ratio/disk-space/symlink guards, re-applied
  at every nesting level), creates a `SourceInstance` for every member
  including nested archives, and resolves every leaf member's identity
  inline. **Idempotent by construction, not by wrapping the whole walk
  in one database transaction**: before creating a member's
  `SourceInstance`, it checks whether one already exists for the exact
  `(classification_run_id, root_t7_path, member_path)` triple - a
  crash-and-resume finds already-created members and completes only
  what's still missing, never duplicating. An archive's own
  `SourceInstance` never receives a `content_identity_group_id` (its
  raw container bytes aren't "content"); "already fully processed" is
  tracked by checking for a prior `SUCCEEDED` `IngestionAttempt`
  against it - a legitimate reuse of the existing audit table for
  exactly its intended purpose, needing no new schema.
- **`app/classification/normalization_service.py`** -
  `NormalizationService.normalize_next()`: claims a group at
  `EXTRACTED`, runs the existing, unmodified `extract_text()`, creates
  the `Document` row (the first point one exists for this identity),
  advances to `NORMALIZED`. Idempotent: checks for an existing Document
  before creating one (a retry after a state-reset finds and reuses
  it, never creating a second - which the `UNIQUE` constraint would
  reject anyway, but the service handles it cleanly rather than
  crashing).
- **`app/classification/chunking_service.py`** -
  `ChunkingService.chunk_next()`: claims a group at `NORMALIZED`,
  creates `DocumentChunk` rows with `embedding = NULL` - deliberately
  NOT reusing the existing `EmbeddingService.embed_document()` (which
  atomically chunks AND embeds together), because the frozen
  `NORMALIZED -> CHUNKED -> EMBEDDED` state machine requires these as
  two independently resumable steps; `DocumentChunk.embedding`'s
  existing nullability already anticipated exactly this intermediate
  state. Idempotent: if chunks already exist for the Document, this is
  a no-op (never delete-and-recreate, which would discard any
  embeddings a partially-completed embedding pass already computed).
- **`app/classification/pipeline_embedding_service.py`** -
  `PipelineEmbeddingService.embed_next()`: claims a group at `CHUNKED`
  (or `EMBEDDED`, for a partially-embedded group needing to finish),
  embeds only chunks with `embedding IS NULL`, and - once none remain -
  advances through `EMBEDDED` straight to `INGESTED`. Resumability is
  structural, not special-cased: querying `WHERE embedding IS NULL`
  fresh on every claim means a crash after embedding some chunks simply
  leaves the rest for the next claim to finish, with zero risk of
  re-embedding (or re-billing an external call for) already-completed
  work.

### `WorkerClaimService` addition

`claim_source_instance_for_archive_processing()` - a new claim query
scoped to top-level (`member_path IS NULL`) archive-suffixed
`SourceInstance`s not yet successfully processed (see the
`IngestionAttempt`-based "already done" check above). Nested archives
are never claimed here - per the frozen design, they're discovered and
resolved entirely within their parent's single claimed unit of work.

### `ContentIdentityService.get_or_create_group` extension

Gained an optional `initial_pipeline_state` parameter (default
`DISCOVERED`, preserving every existing caller's behavior exactly).
The ingestion pipeline passes `EXTRACTED` when creating a group from
identity resolution or archive extraction: by the time a hash is
computed, the underlying bytes have necessarily already been read -
there is no separate `EXTRACTING` claim left to perform for that case,
so the group starts life past that step rather than needing redundant
work claimed for something already done.

### `Document` creation wiring

`DocumentCreate` and `DocumentService.create_document` gained
`content_identity_group_id` (nullable, defaults to `None` - every
existing personal-corpus-import caller unaffected). This was the one
piece of `1253a2e`'s explicitly-deferred scope ("no API routes or
Pydantic schemas... not requested") this gate actually needed, since
creating a pipeline `Document` requires setting this field.

### `FileAccessService` hardening (the item named at `6491dad`, built here)

`_allowed_roots()` now filters by an explicit `source_type` allow-list
(`{"filesystem"}`), not merely `status == COMPLETED`. Verified via
`grep` that every real caller in this codebase already uses
`"filesystem"`, so this costs zero behavior change for anything that
exists today; proven by a new integration test constructing a
`COMPLETED` `ImportJob` with a synthetic non-filesystem `source_type`
and confirming it is now correctly excluded. An allow-list, not a
deny-list, per `6491dad`'s stated reasoning: it fails closed for any
future `source_type` nobody thought to add yet.

### Verified end to end, then formally tested

Before writing the formal test suite, the full chain was run as a
manual smoke test and confirmed to reach `INGESTED` exactly as
designed: `SourceInstance(loose file) → IdentityResolutionService →
ContentIdentityGroup(EXTRACTED) → NormalizationService →
Document + NORMALIZED → ChunkingService → DocumentChunk(embedding=NULL)
+ CHUNKED → PipelineEmbeddingService → embeddings filled + INGESTED`.
A second smoke test confirmed nested-archive recursion end to end
(`outer.zip → top.txt` + `outer.zip → nested.zip → deep.txt`, with the
correct `ProvenanceLink` chain at each depth), and a third confirmed
crash-resume idempotency by pre-creating one member's `SourceInstance`
(simulating a crash between member-creation and identity-resolution)
and verifying re-processing completed it without creating a duplicate.

### The nine required test scenarios - all proven, real Postgres where concurrency/crash behavior is at stake

1. **Same identity → one `ContentIdentityGroup`** - two different
   loose files with identical bytes converge on the same group
   (reusing `1253a2e`'s already-proven mechanism, exercised here at the
   pipeline level).
2. **Concurrent workers → one owner per work item** - proven at the
   PIPELINE level (not just the low-level claim primitives `ce50875`
   already proved): 10 concurrent `IdentityResolutionService` workers
   against 10 distinct files, 6 concurrent `ArchiveProcessingService`
   workers against 6 distinct archives, and 8 concurrent
   `NormalizationService` workers against 8 distinct groups - every
   item was claimed and completed by exactly one worker in these runs,
   verified via a fresh connection, not the ORM's local view.
   **The invariant this proves, stated precisely rather than
   overclaimed**: each work item is claimed by at most one ACTIVE
   worker at a time, and retries converge idempotently to the existing
   durable state - this is NOT a promise of global "exactly-once
   execution." A crash can occur after an external side effect (e.g. a
   file already written to the workspace, or a future real embedding
   API call already billed) but before the corresponding state is
   recorded; what supplies safety here is claim ownership plus
   idempotency (every operation is safe to repeat and converges to the
   same end state), never an assumption that a side effect and its
   durable record happen atomically together.
3. **Worker crash → recoverable claim** - inherited directly from
   `ce50875`'s already-proven stale-claim recovery, unchanged and
   reused by every new service here (all built on `WorkerClaimService`).
4. **Retry → no duplicated Document/Chunk/embedding state** - a group
   forced back to `EXTRACTED` after a successful normalize and
   re-normalized reuses the existing `Document`, never creates a
   second one.
5. **Archive crash halfway → resumable without duplicate members** -
   proven by pre-creating one member's row (simulating the crash) and
   confirming re-processing completes it without duplication. **The
   invariant this test stands for, worth preserving explicitly since
   archive nesting is where future ingestion complexity will
   concentrate**: given an archive where some `SourceInstance` members
   already exist after a crash, a retry must deterministically
   identify those existing members and create only the missing ones -
   never duplicating their `ProvenanceLink` rows, and never mutating
   the evidence already recorded on the pre-existing member rows
   (`evidence_snapshot` stays exactly as first written, per its own
   immutability contract). The one synthetic scenario tested here (a
   single flat archive, one member pre-created) demonstrates this for
   the simplest case; a future gate that touches nested-archive resume
   more deeply should re-verify this same invariant one level down
   before real-T7 authorization, rather than assuming it generalizes
   untested.
6. **Unsupported/corrupt input → durable `FAILED`/`UNSUPPORTED`** -
   proven for both: a known-binary extension durably lands at
   `UNSUPPORTED` at identity-resolution time (never attempted); a
   `.pdf`-named file containing garbage bytes durably lands at `FAILED`
   with `CORRUPT_INPUT`, via a broadened exception handler around
   `extract_text()` (a real bug found and fixed while writing this
   test: `extract_text()` can raise library-specific exceptions from
   `pypdf`/`docx`/`pptx`/`openpyxl`, not only `UnicodeDecodeError`/
   `OSError` - the original narrower handler would have let such an
   exception escape uncaught out of a claimed worker instead of
   recording a durable `FAILED` attempt).
7. **Excluded input → `EXCLUDED`, not `FAILED`** - a `.c9r` file lands
   at `EXCLUDED` directly, with zero attempt ever made and no
   `IngestionAttempt` recorded (matching `29d6864`'s stated rule that
   `EXCLUDED`/`UNSUPPORTED` reasoning never lives in the same place as
   `FAILED`'s).
8. **Embedding crash → resumable/idempotent** - a group with one
   chunk already embedded (simulating a prior partial attempt) and the
   rest `NULL`: re-running embeds only the `NULL` ones (proven via the
   fake embedding client's call log, confirming the already-embedded
   chunk's content was never re-sent) and correctly reaches `INGESTED`.
   A parallel test confirms a genuine embedding-backend failure lands
   the group at `FAILED` with zero chunks marked embedded - never a
   partial, ambiguous state.
9. **T7-unavailable simulation → source remains represented, no
   destructive behavior** - a `SourceInstance` pointing at a path that
   never exists: the row is never deleted or mutated destructively,
   stays unresolved, and gets a durable `FAILED`/`T7_UNAVAILABLE`
   attempt explaining why.

### Full suite

**711 passed, 1 skipped**, run three times, zero flakiness (687
pre-existing + 24 new: 15 pipeline execution tests, 5 eligibility unit
tests, 3 pipeline-level concurrency tests, 1 `FileAccessService`
hardening test). The 3 concurrency tests were additionally run 10
consecutive times with no flakiness. `aibrain_test` confirmed empty of
synthetic rows after every run (one real leftover-data bug was found
and fixed while writing the concurrency tests: synthetic file content
that wasn't unique per test invocation caused `ContentIdentityGroup`
rows to be legitimately shared across separate test runs - correct
production behavior, but it made a blind delete-by-captured-id cleanup
fail with a foreign-key violation when an earlier run's data hadn't
been cleaned up yet; fixed by making test content unique per invocation
and by making cleanup delete a group only if no `SourceInstance`
anywhere still references it). The main `aibrain` database's schema and
Alembic head (`ef65da409302`) remain completely untouched - this gate
added no migration at all. No T7 access of any kind occurred at any
point in implementation, smoke-testing, or formal verification.

### What this implementation pass explicitly does NOT include
- Any T7 access, real extraction, real embeddings, or real ingestion
  jobs - every fixture in every test is synthetic, under a test's own
  `tmp_path`.
- Any API routes or UI for triggering or monitoring ingestion - not
  requested, not built.
- The `ImportJob`-as-outer-container convention `6491dad` sketched for
  a future T7 ingestion run (e.g. `source_type = "classification_run:
  <id>"`) - this gate's services are called directly, with no
  `ImportJob` wrapper; wiring one in is deferred to whenever real T7
  ingestion is authorized.
- A bounded retry/backoff policy for `FAILED` groups, or an explicit
  "give up permanently" terminal state distinct from `FAILED` -
  `FAILED` remains indefinitely retryable by construction; policy
  around when to stop retrying is not decided here.
- Resource limits, concurrency caps, or scheduling/orchestration for
  running many workers at once - each service exposes a single
  `claim-one-and-process-it` method; anything coordinating multiple
  workers across multiple stages is a future concern.
- Logical document identity or document/version modeling (identity
  layers 3/4) - unchanged, still deferred.
- Any change to the dedup executor or to `EmbeddingService` (the
  existing personal-corpus-import path) - both remain exactly as they
  were.

## Synthetic-only pre-real-T7 review gate

Closed at `a0523b7`, test-only: extended nested-archive crash-resume
coverage to three levels (`A.zip → B.zip → {file 1, file 2, C.zip →
file 3}`) and closed the `FileAccessService` ↔ `ImportJob` boundary
gap, including the defense-in-depth case of a mislabeled
`source_type`. No production code changed - the implementation already
matched the frozen design, so no documentation update was needed at
the time. 714 passed, 1 skipped, run three times. Neither T7 path was
accessed or referenced anywhere in this gate, including in "should be
rejected" test inputs.

## Real T7 Read-Only Ingestion Pilot

Authorized explicitly ("AUTHORIZE: REAL T7 READ-ONLY INGESTION
MILESTONE", checkpoint `a0523b7`) as this project's first-ever
authorization to read actual file content from the mounted real T7
corpus (`/media/personal/Seenu_T7SSD1`), strictly read-only, with an
explicit first-pass limit to a small, controlled pilot batch rather
than broad corpus ingestion.

### Pre-flight
- Mount confirmed present and matching D1's own recorded report root
  (`report["root"] == "/media/personal/Seenu_T7SSD1"`) before any
  candidate selection.
- The Alembic migrations already applied to `aibrain_test` many times
  (`b9a82d073399`, `fd9f81672e59`) were applied to the main `aibrain`
  database **for the first time** in this milestone, advancing it from
  `ef65da409302` to `fd9f81672e59` (head). Row counts on every
  pre-existing table were confirmed identical before and after.
- Ollama confirmed NOT running on this machine (`curl` connection
  refused, no matching process) - meaning real embedding calls were
  expected to fail with `EMBEDDING_UNAVAILABLE`, the already-designed-
  for failure path, not a gap in this pilot.

### Pilot batch (`scripts/t7_ingestion_pilot.py`)
Four real T7 items, selected programmatically from the already-
committed D1 `duplicate_analysis.json` report (never re-scanned) by
picking the smallest exact-duplicate group under a small size cap for
each of `.txt`/`.md`/`.pdf`/`.zip`: three small loose files and one
small archive. Every source path's `(size, mtime)` was snapshotted
before any read and re-verified identical after - including one final
re-check after all diagnosis/cleanup activity below - confirming the
real T7 corpus was never altered in any way at any point.

Result: all three loose files resolved identity correctly; the
archive was opened and found, correctly, to contain zero real file
members (four empty directory entries only, confirmed independently
via `unzip -l` against the real file) - the first real proof that
`ArchiveProcessingService` correctly handles an archive with nothing
to extract. One loose-file group reached `INGESTED` (trivially, having
produced zero chunks to embed); the other two correctly failed at the
embedding stage with `EMBEDDING_UNAVAILABLE`, proving the designed
failure path rather than a success.

### Real bug found: identity-resolution claim query had no archive exclusion

The pilot script's idempotency-check section (re-running each claim
method once more, expecting `None`) instead found and wrongly claimed
the archive's own `SourceInstance` row. Root cause:
`WorkerClaimService.claim_source_instance_for_identity_resolution`
filtered only on `content_identity_group_id IS NULL` - with no
exclusion for either (a) a root-level archive-suffixed path (which
legitimately and permanently keeps `content_identity_group_id IS NULL`
even after being fully, successfully processed - an archive's raw
container bytes are never "content"), or (b) an archive-member row
(`member_path IS NOT NULL`), whose actual content lives at
`member_path` inside the archive, not at the shared `root_t7_path`
value duplicated across every one of its siblings. Category (b) was
never actually hit by this pilot (its one archive had no real
members), but is the same latent hazard, structurally: had the archive
contained a nested archive member, that member's own row would have
been just as wrongly claimable, and would have caused
`root_t7_path` (the ARCHIVE's path) to be re-hashed as if it were that
member's content.

Concretely: the second, unnecessary claim call hashed the archive's
raw compressed bytes, created a bogus `ContentIdentityGroup`
(`identity_kind=EXTRACTED_CONTENT`), attempted normalization against
those bytes, and correctly failed there with `CORRUPT_INPUT` (`'utf-8'
codec can't decode byte 0xb3...` - the zip bytes have no extractor
mapping and fall through to the plain-text decode path). No
`Document`/`DocumentChunk` rows were ever created from it. The real T7
file was confirmed completely unchanged (`stat` size/mtime identical
before, during, and after) - this bug corrupted only this
application's own database bookkeeping, never the source corpus.

**Fix**: `claim_source_instance_for_identity_resolution` now also
requires `member_path IS NULL` and excludes any `root_t7_path` ending
in `.zip`/`.7z` (the same `_ARCHIVE_SUFFIXES` already used by
`claim_source_instance_for_archive_processing`), with the docstring
corrected to describe what the query now actually enforces rather than
what earlier prose merely assumed. Two regression tests were added
(`test_identity_resolution_never_claims_a_fully_processed_root_archive`,
`test_identity_resolution_never_claims_a_nested_archives_own_instance`)
- both independently verified to fail against the pre-fix query and
pass against the fixed one, not merely written to look plausible.

**Cleanup performed against the main `aibrain` database** (corrective,
not a new capability): the bogus `ContentIdentityGroup` row and its two
associated `IngestionAttempt` rows were deleted, and the archive's
`SourceInstance.content_identity_group_id` was reset to `NULL` (its
correct, permanent value); the stray workspace file
(`documents/imports/t7_pilot/group_4/content.zip`) was removed. The
three legitimate resolved groups and the archive's own correct
(zero-member) processing attempt were left untouched. Full synthetic
suite re-run three times after the fix: **716 passed, 1 skipped**, zero
flakiness (714 pre-existing + 2 new regression tests).

### Minor semantic imprecision noted, not changed
`ArchiveProcessingService` records its own archive-level attempts via
`IngestionAttemptService.record_identity_resolution_attempt` (labeling
them `attempt_kind=IDENTITY_RESOLUTION`), since that is the only method
available for `SourceInstance`-scoped attempts - even though what it
represents is "recursive archive extraction," not literally identity
resolution of the container itself. Not a functional bug (the CHECK
constraint and every consumer treat it correctly); left as a candidate
for a future, separately-authorized naming cleanup rather than folded
into this corrective gate.

### `.gitignore` extended
`documents/` (the ingestion extraction workspace, now containing real
personal file content read from T7 for the first time) was added to
`.gitignore`, mirroring the existing `knowledge/t7_discovery/` entry's
reasoning - real personal content must never be committed to version
control, whether it is file names/paths or file bytes.

### What this pilot explicitly does NOT include
- Any expansion beyond the four-item pilot batch - per the
  authorization's explicit first-pass limit, no broader ingestion was
  started, and none is authorized by this gate.
- Any mutation, rename, delete, quarantine, permission/ownership/
  timestamp change, or dedup disposition against T7 - none of this
  pipeline has ever had such a capability; the fix and cleanup above
  touched only this application's own Postgres database and its own
  extraction workspace, never the T7 mount.
- A fix for the `IDENTITY_RESOLUTION`-labeled archive-attempt naming
  imprecision noted above.
- Running Ollama or proving a real end-to-end embedding success against
  real T7 content - Ollama was confirmed not running; the correct
  `EMBEDDING_UNAVAILABLE` failure path was proven instead.

## Controlled Real-T7 Batch Ingestion

Authorized explicitly ("AUTHORIZE: CONTROLLED REAL-T7 BATCH INGESTION",
checkpoint `5d5d1c7`) as the second real-T7 read-only ingestion gate,
opened once the embedding-infrastructure prerequisite named after the
first pilot was verified: Ollama running (`localhost:11434`, v0.31.2),
`nomic-embed-text` pulled (768 dimensions, matching
`settings.EMBEDDING_DIMENSIONS`), the raw `EmbeddingClient` verified
with a real call, and the full `PipelineEmbeddingService.embed_next()`
path verified synthetically end-to-end (`CHUNKED -> INGESTED` with a
real embedded vector, rolled back afterward). Ollama was already
running continuously before this batch started and was not restarted
by it.

### Batch composition - seven representative real cases

Selected via a systematic, read-only survey of the existing D1
`duplicate_analysis.json` report (never re-scanned) plus two direct,
read-only T7 lookups: (1) an ordinary loose file with real, substantive
text; (2)/(3) two real paths with byte-identical content from a
1,127-copy D1 duplicate group; (4) a small real archive with genuine
(non-directory) file members; (5) a real file under a known-unsupported
suffix; (6) a real file that is genuinely invalid relative to its own
extension - confirmed via direct inspection (`zipfile.ZipFile()`
raising) before selection, not fabricated - a real Office "owner/lock"
file under a `.pptx` extension; (7) the exact same real path used as
`SourceInstance` id=1 in the first pilot, to prove reuse of a
pre-existing PRODUCTION `ContentIdentityGroup`, not just same-run
convergence.

**Deliberately not included**: a naturally-occurring nested archive.
42 real `.zip`/`.7z` files up to 50MB were checked (every member of
every one, `unzip -l`/`7z l`, filtering out each listing's own
self-referential header line) and none contained an archive member.
Asked explicitly, the decision was to rely on the existing synthetic
3-level nested-archive proof (`a0523b7`) rather than expand the search
into larger archives, which would work against "small, controlled
batch."

### Result: full chain proven on real, diverse content

`SourceInstance -> identity resolution -> ContentIdentityGroup -> claim
-> extraction -> normalization -> Document -> DocumentChunk -> real
Ollama embedding -> INGESTED`, all seven cases behaving exactly as
designed:
- **Already-known identity reuse**: the reused path resolved to the
  SAME pre-existing `ContentIdentityGroup` (id=1) created by the first
  pilot - a real production-identity reuse, not merely same-run
  convergence.
- **Duplicate convergence**: two distinct real paths with identical
  bytes converged onto one new group.
- **Archive with real members**: two real CSV members extracted,
  correct `T7_FILE -> ARCHIVE_MEMBER` two-link provenance chains.
- **Unsupported input**: durable `UNSUPPORTED`, zero attempt made.
- **Corrupt input**: durable `FAILED`/`CORRUPT_INPUT` at normalization
  (python-pptx correctly rejects the invalid package).
- **Real embedding**: 8 real chunks across 4 groups embedded via
  genuine Ollama calls (not skipped, not faked), all 4 reaching
  `INGESTED`.
- **Idempotency and no-duplicate-creation on retry**: every claim
  method's second AND third call returned `None`; `SourceInstance`/
  `ProvenanceLink` counts identical before/after the repeated no-op
  calls - the bug fixed in the first pilot (`5d5d1c7`) did not recur
  under this second, more diverse real-data run.

No new defect was found in this batch - a meaningful confirmation of
the prior fix, not just an absence of new findings.

### Source integrity

Every real path's filesystem metadata (size, mtime) was captured
before any read and re-verified unchanged at four checkpoints during
the run, plus once more independently afterward, outside the script.
Stated precisely, per review: this is strong corroborating evidence of
no mutation, not a cryptographic proof of byte-for-byte identity - the
stronger safety property is that this pipeline has never had any
write/rename/delete/quarantine capability against a source path at
all, at any point in its history.

### Database changes (main `aibrain`)

`ContentIdentityGroup` 3→9, `SourceInstance` 4→13, `ProvenanceLink`
4→15, `Document` 3→7. No migration; schema unchanged since `5d5d1c7`.
`aibrain_test` confirmed untouched (0 rows) - this batch used only the
main database. This is now genuine real-ingestion production data, not
a synthetic validation artifact.

### Full suite

**717 passed, 0 skipped**, run three times, zero flakiness (no backend
code changed this gate). The previously-always-skipped
`test_import_job_execution_embeds_documents_via_real_ollama` (a
pre-existing test on the older, unrelated personal-corpus `ImportJob`
path, `skipif(not _ollama_reachable())`) now runs for real with Ollama
up and passes - incidental confirmation that the original
`EmbeddingService.embed_document()` path also works against the real
backend, not itself part of this gate's scope.

### Script sensitivity - no real paths committed to version control

The batch's real T7 paths are never hardcoded in
`scripts/t7_batch_ingestion.py` - they live in a sibling
`scripts/t7_batch_selection.json`, gitignored via a new
`scripts/*_selection.json` rule (same reasoning as
`knowledge/t7_discovery/` and `documents/`: real personal file/
directory names must never be committed). The committed script only
knows generic case labels (`already_known_identity`, `loose_ordinary`,
`duplicate_a`/`duplicate_b`, `archive`, `unsupported`, `corrupt`) and
loads the actual paths from that file at runtime, erroring clearly if
it is absent. Verified via direct `grep` of the committed file for any
T7 path fragment or personal filename before commit.

### What this batch explicitly does NOT include
- Any expansion beyond this seven-case batch, or toward the remaining
  ~727 GB corpus - this is a successful controlled-batch milestone,
  not permission for broader ingestion. The next gate must be a
  separate, deliberate decision about the size and rules of the next
  real-T7 batch.
- A naturally-occurring nested-archive proof against real data (see
  above) - synthetic coverage remains authoritative for that scenario.
- Any mutation, rename, delete, quarantine, permission/ownership/
  timestamp change, or dedup disposition against T7.
- Any schema or migration change - none was needed.

## Scaled Real-T7 Ingestion Design (design pass, APPROVED and frozen — no implementation yet)

Design-only, following the same freeze-before-implement discipline as
`6491dad`. No T7 access, no schema migration, no code written for this
gate yet. Grounded directly in the current codebase (verified, not
assumed): there is currently no batch/envelope/pause/resource-monitoring
concept anywhere in `app/` - `ClassificationRun` has `started_at`/
`completed_at` but no status enum and no notion of a bounded subset of
instances; the only disk-space check anywhere is `ArchiveExtractor`'s
hardcoded per-archive `HARD_FREE_SPACE_BYTES`/`HARD_EXPANSION_RATIO`,
with no batch-level cumulative tracking; `SourceInstance.id` is a plain
autoincrement integer assigned only at creation, so it cannot serve as
a pre-selection ordering key.

### Terminology: D0 is a manifest, not a snapshot

The D0 report is an immutable *artifact* describing what D0 observed -
it is not proof the filesystem itself is unchanging. Precisely:
```
D0 report  = frozen description of what D0 observed
filesystem = may have changed since D0 ran (it was not an atomic snapshot)
```
"D0 discovery manifest" / "observed corpus inventory" replaces "frozen
snapshot" as the term of art throughout this design.

### 1. Discovery-run-scoped eligibility

The eligibility rule is **not** "no `SourceInstance` exists for this
path anywhere, ever." That would wrongly prevent a later `DiscoveryRun`
from ever re-observing a path whose content may have changed. The
correct invariant:

> A source path is eligible only when the *current* `DiscoveryRun`
> contains an observation for that path that has not already been
> materialized into a `SourceInstance` for that same observation/run.

`path != physical occurrence != content identity` remains explicit: a
later `DiscoveryRun` may create a new `SourceInstance` for the exact
same real path, and that new instance may converge on an existing
`ContentIdentityGroup` (bytes unchanged) or a new one (bytes changed),
resolved independently at read time exactly as `get_or_create_group`
already does today - no new mechanism, just a corrected scope on the
eligibility query itself (scoped by the current batch's `DiscoveryRun`
lineage, not global across all time).

D1/D2 remain enrichment signals feeding classification/analysis, never
the selection universe - this separation is unchanged and correct.

### 2. Deterministic selection, batch ownership, immutable membership

Selection sorts D0's flat path list lexicographically by relative path
- fully reproducible from frozen report content, never live filesystem
enumeration order.

**Frozen invariant**: every `IngestionBatch` owns exactly one dedicated
`ClassificationRun`; a `ClassificationRun` is never shared by another
`IngestionBatch` (the existing 1:1 `IngestionBatch.classification_run_id`
relationship enforces this). Because of this exclusivity, **batch
membership is defined solely as "the `SourceInstance` rows materialized
under that batch-owned `ClassificationRun`"** - never re-derived by
re-running the selection algorithm, even if eligibility rules or code
change later. Selection executes exactly once, synchronously, at batch
creation. A "resume" reopens processing of the already-created rows; it
never re-selects. This is the scaling analogue of Chain 1's immutable
execution-plan snapshot, using existing structure rather than a new
membership table.

A deterministic **selection fingerprint** - a hash of `(D0 report
sha256, selection_policy_version, ordering_version, envelope values,
sorted selected source references)` - is stored on the `IngestionBatch`
row as durable proof of exactly what was authorized/executed.

### 3. Classification dimensions: source, workload, and deferred context

Two independent, per-`SourceInstance` dimensions, computed once at row
creation (pure suffix/path evidence, no file opened - extending the
existing `classify_eligibility()` pattern rather than inventing a
parallel classifier):

```
source_category:   LOOSE_FILE | ARCHIVE | SPECIAL          (physical - what IS it)
workload_category: TEXT_DOCUMENT | STRUCTURED_DATA | MEDIA | CODE |
                    SOFTWARE | ENCRYPTED | CONTAINER | UNKNOWN  (what will processing involve)
```
`source_category` stays strictly physical - a file under Syncthing,
Takeout, "Archive 2", or a dated snapshot directory is still just a
`LOOSE_FILE` or `ARCHIVE`; backup/sync/snapshot-tree naming is a
**separate, explicitly deferred contextual signal** (its own field,
its own versioned path-pattern list), not a peer of `source_category`.
No literal path patterns are decided in this design pass.

`CONTAINER` is the workload value for an archive's own row - it is
never itself content (an already-established invariant), so it gets an
explicit "this is a container" value rather than a guessed blend of
its unopened members' eventual types.

### 4. Two-stage archive policy

```
SOURCE ADMISSION POLICY  - decides whether the archive CONTAINER may
                            enter a batch, using only pre-extraction
                            evidence (declared size, source_category,
                            risk_tier_estimated, compression type).
                            Evaluated during selection, alongside loose
                            files.
ARCHIVE MEMBER POLICY    - applies AFTER extraction creates real member
                            SourceInstance rows with real workload_category
                            values. Evaluated at extraction time, inside
                            the existing member-creation loop.
```
Unopened archive members never participate in the initial batch-selection
predicate - batch envelope sizing at selection time is based solely on
container-level rows, since members do not exist as `SourceInstance`
rows until extraction (an execution-time event, not a selection-time
one). This is consistent with, and reinforces, the extraction-staging
model below.

### 5. Risk tier - two-phase, honestly nullable

```
risk_tier_estimated  - selection-time. LOOSE_FILE/SPECIAL: fully knowable
                        immediately from size + workload_category.
                        ARCHIVE: necessarily conservative - only size and
                        compression type are knowable pre-extraction.
risk_tier_actual     - NULLABLE. Populated only once real evidence
                        (member count, nesting depth, measured expansion
                        ratio) actually exists. LOOSE_FILE/SPECIAL: set
                        immediately, equal to the estimate. ARCHIVE: set
                        only after extraction completes. If extraction
                        fails BEFORE that evidence exists (malformed
                        archive, envelope abort, T7 unavailable), 
                        `risk_tier_actual` stays NULL - never fabricated
                        as a stand-in value.
```
Values: `LOW | MEDIUM | HIGH | EXTREME`. Numeric boundaries are
deliberately not decided here (see below).

### 6. Resource envelope, extraction staging, embedding reservation, runtime accounting

```
batch_limits (stored on IngestionBatch, immutable once set):
    max_source_instances, max_source_bytes   - enforceable at selection time
    max_extracted_bytes, max_embeddings      - enforceable only as running
                                                 counters, checked before
                                                 every claim during execution
    max_runtime_seconds
```
**Extraction** ("the first scaling limit should be the smallest limit
reached", adopted verbatim): archives extract into an isolated
per-item staging directory (`workspace_root/_staging/<source_instance_id>/`),
never directly into the final location. Extracted bytes are monitored
during extraction against the batch's remaining budget; if exceeded,
the staging directory is deleted wholesale (zero partial artifacts
survive), the claim is released **without** recording an
`IngestionAttempt` (nothing was decided about the item - it wasn't
corrupt, just deferred to a future batch), and the stop is recorded
only on `IngestionBatch` (`stop_reason=EXTRACTED_BYTES_ENVELOPE_EXCEEDED`).

**Embedding capacity** uses the same atomic-claim discipline already
proven in `WorkerClaimService` - a single conditional `UPDATE`, not
read-then-act:
```sql
UPDATE ingestion_batches
SET embeddings_reserved = embeddings_reserved + :n
WHERE id = :batch_id AND embeddings_reserved + :n <= max_embeddings
RETURNING embeddings_reserved
```
No returned row = reservation denied = release the claim without
embedding (same disposition as extraction overflow). A reservation is
released back symmetrically if the underlying embedding call then
fails anyway.

**Runtime** budget is measured via `time.monotonic()` for stop
decisions (immune to NTP/clock adjustment); durable wall-clock
`started_at`/`completed_at` remain for audit only. Because a batch can
pause and resume across process restarts, consumed runtime accumulates
durably in `monotonic_runtime_seconds_consumed`, updated at each
pause/stop from that session's monotonic delta.

### 7. Batch state and work-item state are independent axes

`IngestionBatch.status` (`PLANNED | RUNNING | PAUSED | COMPLETED |
ABORTED`) plus `stop_reason`/`stop_reason_detail` are entirely
independent of any `ContentIdentityGroup.pipeline_state` or
`SourceInstance` state. A batch can pause on a resource limit while the
item in flight is perfectly valid; a batch can keep running while an
unrelated item genuinely fails. A batch report must always show both.

### 8. Completion reporting - four denominators, not one percentage

```
eligible_source_count        - D0-derived universe size under this batch
                                class's policy filter, before any take
attempted_source_count       - subset of batch membership that received
                                >=1 real processing claim (can be less
                                than membership if the batch stopped early)
terminal_source_count        - subset whose ContentIdentityGroup reached
                                a terminal pipeline_state
successful_ingestion_count   - subset of terminal specifically INGESTED
```
`FAILED` counts toward "processed at least once," never toward
"successfully ingested" - it remains indefinitely retryable by
construction, unchanged from the existing design.

### 9. Resource monitoring - required checks vs. optional extension

Required, using existing primitives, no new dependency: workspace disk
(`shutil.disk_usage`, already used by `ArchiveExtractor`), PostgreSQL
size (`pg_database_size()`), Ollama reachability (existing
`EmbeddingClient`). Host CPU/RAM monitoring is defined as an **optional,
pluggable** `BatchResourceGuard` extension point - `psutil` is
deliberately **not** added as a dependency for this design; it may be
adopted later as an optional implementation, never a prerequisite for
freezing this architecture.

### 10. Live corpus drift - three-way outcome, source bytes always authoritative

```
OBSERVED_AT_SELECTION           - D0's declared (path, size) - evidence only
SOURCE_PRESENT_AT_EXECUTION     - exists, metadata matches D0's declaration
SOURCE_CHANGED_AFTER_SELECTION  - exists, metadata differs - NOT an error;
                                    read and hash proceed normally, the
                                    mismatch is noted informationally in
                                    evidence_snapshot
SOURCE_MISSING_AT_EXECUTION     - the existing T7_UNAVAILABLE path, unchanged
```
Frozen invariant: **the source bytes actually read at ingestion time
are authoritative for content identity; D0 metadata is selection
evidence, not content authority.** A genuinely changed file is properly
re-ingested as a new observation under a later `DiscoveryRun` (per
point 1), not merely flagged as a same-batch anomaly.

### Resulting new schema: one table, `ingestion_batches`

```
id, classification_run_id (FK, 1:1, exclusive per point 2)
status                          # PLANNED / RUNNING / PAUSED / COMPLETED / ABORTED
stop_reason (nullable), stop_reason_detail (nullable)
max_source_instances, max_source_bytes, max_extracted_bytes,
  max_embeddings, max_runtime_seconds        # immutable once set
source_instances_selected, source_bytes_selected   # set at creation, immutable
extracted_bytes_consumed, embeddings_reserved       # atomically-updated counters
monotonic_runtime_seconds_consumed                   # durable cross-session accumulation
selection_fingerprint, selection_policy_version, ordering_version
created_at, started_at, completed_at        # wall-clock, audit-only
```
Plus four new columns on `SourceInstance`: `source_category`,
`workload_category`, `risk_tier_estimated`, `risk_tier_actual`
(nullable per point 5). No other schema changes.

### Frozen architectural rules (final, consolidated)

```
D0 is the batch-selection universe; D1/D2 are enrichment signals, never the universe.
Eligibility is scoped to the current DiscoveryRun's observations, not "ever touched, globally."
path != physical occurrence != content identity.
Selection is deterministic and reproducible from frozen report content.
Every IngestionBatch owns exactly one dedicated, never-shared ClassificationRun.
Batch membership is immutable once created; resume never re-runs selection.
Source category is strictly physical (LOOSE_FILE | ARCHIVE | SPECIAL); backup/sync/snapshot
  context is a separate, explicitly deferred signal, not a source_category peer.
Workload category is attached per-SourceInstance at creation time; an unopened archive's own
  row gets CONTAINER, never a guessed blend of its members.
Archive admission (container-level) and archive member policy (post-extraction) are two
  distinct evaluation stages; unopened members never affect initial batch-selection sizing.
Risk tier is two-phase: an estimate at selection, an honestly nullable actual value populated
  only once real post-extraction evidence exists - never fabricated on early failure.
Archive source size does not equal downstream workload size.
max_extracted_bytes is enforced via isolated per-item staging, discarded wholesale on overflow -
  never a partial, durable extraction artifact.
max_embeddings is enforced via the same atomic-claim discipline as WorkerClaimService, not
  read-then-act.
max_runtime uses a monotonic clock for decisions; wall-clock timestamps are audit-only.
Batch state (IngestionBatch.status) and work-item state (pipeline_state) are independent axes.
Completion is reported as four denominators, never one percentage.
The source bytes read at ingestion time are always authoritative for content identity over
  any D0 metadata.
Host resource monitoring beyond disk/Postgres/Ollama is optional and pluggable; psutil is not
  a dependency of this design.
```

### What this design pass does NOT decide

- Numeric envelope defaults (batch size, extracted-bytes ceiling,
  embedding count, runtime budget) for any specific batch.
- Numeric risk-tier boundaries (to be derived from D0's actual size
  distribution once decided, not invented).
- The backup/sync/snapshot context field's exact structure and its
  versioned path-pattern list.
- Per-batch-class (the earlier "Batch A-G" sketch) policy filter
  definitions - each is its own small design decision layered on top
  of this frozen architecture, e.g. the sketch's "Batch F - backup/
  snapshot collections" now composes `source_category` with the
  (still-deferred) backup/sync context signal rather than being its
  own `source_category` value.
- Any implementation - this architecture is approved and frozen at the
  conceptual/design level, but no code, schema migration, or T7 access
  is authorized by this freeze. **Next gate, not yet authorized**: a
  separate numeric/policy-definition pass (batch-class inclusion/
  exclusion rules, size/risk thresholds, archive admission thresholds,
  resource envelopes, promotion criteria) must be completed and
  explicitly approved before an implementation design, and the real T7
  remains outside the ingestion gate until then.

## Scaled Real-T7 Ingestion — Numeric + Policy Definition Pass (design pass, APPROVED and frozen — no implementation yet)

Docs-only, sitting on top of the frozen architecture (`f2b9815`). No T7
access, no scan, no schema/code change. Every number below is derived
from the two reports already on disk (`inventory.json`,
`duplicate_analysis.json`) plus direct, local, non-T7 measurements of
this machine's current disk/database state — never invented, never a
fresh corpus traversal. Anything the existing evidence can't support is
marked **UNRESOLVED**, not guessed.

### Evidence base (computed just now, from existing reports only)

D0 (`inventory.json`, scanned 2026-09-12): 619,087 files, 669.1 GB.

**Important scope limit on the normalization below**: Syncthing appends
`_<digits>` to a colliding filename (e.g. `.pdf_1768918262`), and
8,000+ files carry such a suffix. For *this pass's numeric estimation
only*, those raw observed extensions are merged into their base
extension (`.pdf_1768918262` → `.pdf`) to produce a meaningful
aggregate size/count table — otherwise the same real content-type
would be undercounted across many synthetic-looking "extensions."
**This is an analytical bucketing step for sizing envelopes, not a
decision about how the real `SourceInstance.workload_category`
classification function should behave.** Whether the actual
implementation strips conflict-rename suffixes when computing
`workload_category` is a separate, still-open implementation-design
question, not settled by this document:
```
raw observed extension/path  →  normalized analytical bucket (THIS PASS, for sizing only)
raw observed extension/path  →  SourceInstance.workload_category (a DIFFERENT, not-yet-decided function)
```
Normalized workload rollup (analytical only):

| Rollup | Files | Bytes | % of corpus |
|---|---|---|---|
| ARCHIVE (`.zip`/`.7z`/`.gz`/`.rar`) | 787 | 368.6 GB | 55.1% |
| MEDIA (`.mp4`/`.jpg`/`.png`/`.wav`/`.m4a`/…) | 37,287 | 163.8 GB | 24.5% |
| SOFTWARE (`.exe`/`.dll`/`.msi`/`.apk`/…) | 16,549 | 52.8 GB | 7.9% |
| UNACCOUNTED (`.html` 40.1GB dominant, `.dat`/`.db`/no-ext/`.bin`) | ~42,700 | 55.0 GB | 8.2% |
| TEXT_DOCUMENT (`.txt`/`.md`/`.pdf`/`.docx`/`.pptx`/`.json`) | 377,943 | 17.6 GB | 2.6% |
| ANKI (`.apkg`/`.colpkg`) | 288 | 6.8 GB | 1.0% |
| ENCRYPTED (`.c9r`) | 79,314 | 4.5 GB | 0.7% |

Verified directly against the running code (`eligibility_service.py`,
`text_extractor.py`), not assumed: `_EXCLUDED_SUFFIXES = {.c9r}`,
`_KNOWN_UNSUPPORTED_SUFFIXES = {.bin, .exe, .dll, .jpg, .jpeg, .png,
.gif, .mp3, .mp4, .mov}` — meaning most of MEDIA and part of SOFTWARE
are *already* routed to a durable `UNSUPPORTED` with zero wasted
attempt. `.wav`/`.m4a` (MEDIA) and `.msi`/`.apk`/`.dat`/`.db`/no-
extension/`.html` are **not** in either list today — they would be
attempted, and most would fail the plain-text-decode fallback as
`CORRUPT_INPUT` (a safe but wasted attempt), except `.html`, which
would *succeed* as raw markup treated as plain text — a quality
problem, not a safety one, since `text_extractor.py` only has explicit
extractors for `.pdf`/`.docx`/`.pptx`/`.xlsx`; `.txt`/`.md`/`.json`/
`.csv` work today purely via the plain-decode fallback succeeding.

D1 (`duplicate_analysis.json`) `.zip`/`.7z` group-size distribution
(the two pipeline-extractable archive types — `.gz`/`.rar` are outside
`ArchiveExtractor`'s current scope entirely, so are `SPECIAL`, not
`ARCHIVE`, for this pipeline): covers 412 of D0's 421 known `.zip`/`.7z`
files (97.9%; the remaining 2.1% are unique, non-duplicated archives D1
can't see).

| Size bucket | Distinct groups | Physical copies | Group bytes |
|---|---|---|---|
| <1MB | 16 | 74 | 0.003 GB |
| 1–10MB | 19 | 138 | 0.061 GB |
| 10–50MB | 8 | 28 | 0.166 GB |
| 50–200MB | 10 | 39 | 1.278 GB |
| 200MB–1GB | 14 | 51 | 7.478 GB |
| 1–5GB | 31 | 68 | 60.237 GB |
| 5–20GB | 2 | 12 | 23.773 GB |
| 20GB+ | 1 | 2 | 21.271 GB |

**Real finding, worded precisely to keep the architectural invariant
and the scaling optimization distinct**: archive containers are not
deduplicated through `ContentIdentityGroup` (an already-frozen,
correct invariant — a container is never itself content; this is a
fact about what the identity system does, not a claim about which
files are "the same"). Therefore, identical archive copies may
otherwise be independently, redundantly extracted — the 1–5GB bucket
alone has 31 distinct archives appearing as 68 physical copies (~2.2×
average duplication) per D1. The batch-selection policy **may**
explicitly exclude/defer redundant archive copies, but only on
evidence of the same kind D1 itself already establishes content
identity with: **membership in the same D1 exact-duplicate group
(SHA-256 content hash, already computed)**. Filename similarity, path
similarity, directory-naming convention (e.g. "Archive 2", dated
snapshots), or compressed size alone are **never** sufficient evidence
that two archive files are redundant copies of each other — those are
exactly the signals D2's provenance work already established as
suggestive, never authoritative. See policy filters below for the
precise predicate.

Local environment, measured directly (no T7 involved): workspace/
extraction volume (`/home`, where `documents/imports/` lives) has 25GB
free of 127GB (80% used). The PostgreSQL data volume (`/`) has **13GB
free of 110GB (88% used) — the tighter of the two**, worth naming
explicitly since it's easy to assume the workspace disk is the binding
constraint when it currently isn't. The `aibrain` database is
currently 10MB. Ollama v0.31.2 / `nomic-embed-text` (768-dim) is
running and verified.

**`max_extracted_bytes` must not be read as permission to consume that
much space merely because the workspace happens to have it.** Each
physical resource is governed independently by the same inequality,
with an *explicit, visible* safety reserve — not folded silently into
a single "floor" number:
```
projected_consumption_by_this_batch + projected_background_growth + safety_reserve
    <= currently_available_capacity   (re-checked live, not assumed fixed)
```
**The 10GB/5GB reserve values are configured policy; the 25GB/13GB
free-space figures are runtime measurements, not each other.** This
distinction matters: the reserves are decisions this design makes and
that persist until deliberately revised; the free-space figures are
observations of this machine's state *right now* and may be different
by the time any batch actually executes — a future implementation must
re-measure free space live before and during a batch, never treat
today's 13GB as a permanently known fact. Applied to each volume, using
today's measurement purely as a worked example:
```
Workspace (/home, safety_reserve_workspace = 10GB, POLICY):
    max_extracted_bytes (this batch's extraction) + safety_reserve_workspace
        <= currently_measured_free_space (25GB free TODAY, RUNTIME MEASUREMENT)
        →  extraction budget ceiling = 15GB AT TODAY'S HEADROOM, re-measured live

PostgreSQL (/, safety_reserve_pg = 5GB, POLICY — the binding constraint
for embedding-heavy classes):
    projected_db_growth (embeddings + pgvector indexes + WAL + temp work)
        + safety_reserve_pg
        <= currently_measured_free_space (13GB free TODAY, RUNTIME MEASUREMENT)
        →  db-growth budget ceiling = 8GB AT TODAY'S HEADROOM, re-measured live
```
**No DB-growth projection algorithm is defined here** (per-embedding
storage cost depends on row overhead, pgvector index structure, WAL
churn, and TOAST behavior — none of which have been measured against
this schema at any real scale; inventing a bytes-per-embedding formula
now would be exactly the kind of fabricated number this design pass is
required to avoid). Instead, the **governing invariant** is:

> Hard-stop is the last-resort tier, never the first threshold that
> matters. Using exactly the three tiers already defined per-guard in
> point 9 (soft stop, review-required, hard stop) — not inventing a new
> ordering beyond what's there: as a resource depletes, the **soft-stop
> threshold** (the less severe of the two live free-space levels —
> workspace <15GB, PostgreSQL <8GB) fires *before* the **hard-stop
> threshold** (workspace <10GB, PostgreSQL <5GB), triggering a
> controlled pause — no further work is admitted/claimed, though
> already-in-flight work may finish — well before an emergency stop is
> ever needed. The **review-required condition** is a separate,
> projection-based check evaluated at batch admission/planning time
> (e.g. "this batch is projected to consume/grow beyond the stated
> amount"), orthogonal to the live depletion sequence rather than a
> third point along it — it can fire independently of exactly where
> current headroom sits. Across all of this: **every required guard
> (workspace disk, PostgreSQL disk, and any others later added) must
> remain above its own hard-stop threshold throughout execution, and
> the tightest remaining resource budget among them governs continued
> execution** — a batch continues only as long as *every* guard clears,
> not merely the one its own workload happens to stress most. For
> Class 1 (no archives), the workspace guard is essentially inert; the
> PostgreSQL guard is the one actually exercised, and by how much is
> itself one of the empirical outputs Class 1 must produce (point 7's
> calibration list: "DB growth per embedded chunk"), not an input
> assumed in advance.

Both budget ceilings above (15GB workspace, 8GB PostgreSQL) are
snapshots of *today's* headroom, re-verified live before each batch
starts and continuously during execution (per the frozen architecture's
disk/DB checks) — never assumed to still hold at some future execution
time, and never a substitute for measuring actual consumption once
Class 1 runs.

**Explicitly UNRESOLVED — no calibration data exists yet**: archive
expansion ratio (compressed → extracted bytes) at any real size tier —
both real pilot archives so far had zero actual members, contributing
no expansion evidence at all. Embedding throughput/latency per chunk
against the local Ollama instance — no benchmark has been run.
Per-document chunk-count distribution beyond the two pilots' small
sample (2–4 chunks for files in the 2.6–2.8KB range). These are named
as calibration targets for the first scaled batch's own report to
supply, not invented now.

### 1. Batch classes (replaces the illustrative A–G sketch)

| Class | Purpose | `source_category` | `workload_category` | Archive admission | Member policy | `risk_tier_estimated` ceiling | Aggressiveness | Initial rollout? |
|---|---|---|---|---|---|---|---|---|
| **1 — Text/Document** | Ordinary loose documents | `LOOSE_FILE` only | `TEXT_DOCUMENT`, `STRUCTURED_DATA` | none (no archives) | n/a | LOW–MEDIUM | Lowest | **Yes — primary candidate** |
| **2 — Small Archive** | Prove archive path at scale, low blast radius | `LOOSE_FILE` + `ARCHIVE` | any (discovered post-extraction) | `.zip`/`.7z`, source-size tier SMALL (<10MB) | applies once members exist | LOW–MEDIUM | Low | **Yes — second candidate** |
| **3 — Medium Archive** | Larger but still bounded archives | `LOOSE_FILE` + `ARCHIVE` | any | `.zip`/`.7z`, source-size tier MEDIUM (10MB–1GB) | applies once members exist | MEDIUM | Moderate | After 1 & 2 prove clean |
| **4 — Large Archive** | The corpus's single biggest byte contributor | `ARCHIVE` | any | `.zip`/`.7z`, source-size tier LARGE (1–5GB) | applies once members exist | HIGH | High | Not initial — needs 2/3 evidence |
| **5 — Extreme Archive** | A handful (3 groups, 14 copies, ~45GB) of monster archives | `ARCHIVE` | any | `.zip`/`.7z`, source-size tier EXTREME (5GB+) | applies once members exist | EXTREME | Highest | No — own dedicated future gate, one-at-a-time |
| **6 — Media (deferred/completeness-only)** | Confirm `UNSUPPORTED` handling at scale — **not** an ingestion-value class | `LOOSE_FILE` | `MEDIA` | none | n/a | n/a — deferred while unsupported, not risk-rated | n/a | No — completeness tracking only, not a rollout candidate |
| **7 — Software/Executable (deferred/completeness-only)** | Same as Class 6, for `.exe`/`.dll`/etc. | `LOOSE_FILE` | `SOFTWARE` | none | n/a | n/a — deferred while unsupported, not risk-rated | n/a | No — completeness tracking only, not a rollout candidate |
| **8 — Special/Deferred** | `.c9r`, `.html`, `.dat`/`.db`/no-ext, Anki, backup/sync context | `LOOSE_FILE`/`SPECIAL` | `ENCRYPTED`/`UNKNOWN`/`CONTAINER` | none | n/a | n/a — deliberately not processed | n/a | No — deliberate exclusion, revisit later |

Note on the "source-size tier" labels used for archive admission (2–5):
these name **declared compressed source size only** — a physical fact
about the file on disk before any extraction. They are deliberately
**not** a restatement of `risk_tier_estimated`; see point 4 below for
why the two are kept distinct even though size is, today, the primary
signal informing the estimate.

Classes 6/7 are reclassified as **deferred, completeness-only** per
review — not "LOW-risk ingestion classes." Their content is not
processable today (no extractor exists), so there is no ingestion
value being deferred, only a denominator-completeness need: the
*completeness denominator* (point 8) should still account for the
~32% of the corpus (MEDIA+SOFTWARE) that terminates at `UNSUPPORTED`
immediately, but this is bookkeeping, not a rollout candidate at any
priority level.

### 2. Numeric envelopes

Distinguishing selection-time limits (enforceable from D0 alone),
runtime consumption limits (enforceable only as running counters), and
physical safety limits (independent of any single batch, checked
continuously), per the frozen architecture:

| Parameter | Kind | Class 1 (first batch) candidate | Rationale / evidence |
|---|---|---|---|
| `max_source_instances` | selection-time | **1,000 (policy choice, not derived)** | This is a deliberate policy choice for the first scaled batch — a conservative step beyond the two pilots' single-digit item counts — not a number the D0/D1 evidence itself dictates, and not validated by any rate/scaling formula. 1,000 remains available as a concrete anchor for the authorizing gate to accept or adjust. |
| `max_source_bytes` | selection-time (safety cap) | **2 GB** | Selection order is lexicographic by *path*, not by size (frozen) — so nothing guarantees the first 1,000 alphabetical documents are small ones. This cap guards against an outlier (e.g. an unusually large PDF) dominating the batch, without constraining the expected case (1,000 × 46KB ≈ 46MB expected). |
| `max_extracted_bytes` | runtime consumption | **N/A for Class 1** (no archives admitted) | Becomes meaningful starting at Class 2. For Class 2 specifically: UNRESOLVED precise value — expansion ratio for this corpus's archives has never been measured (both real pilots' archives had zero members). Recommend a conservative placeholder pending Class 2's own measurement, not a confident number. |
| `max_embeddings` | runtime consumption (atomic reservation) | **CALIBRATION_REQUIRED — no defensible number today** | The only real evidence is 5 items across both pilots: 2,640B→4 chunks, 2,821B→2 chunks, 140B→1 chunk, 114B→1 chunk, 3B→0 chunks. Applying that per-byte rate (~1 chunk/700–1,400 bytes) to the document category's actual ~46KB average implies **~33–66 chunks/file**, not the ~5 previously stated — that earlier figure was an arithmetic error, not a smaller, more conservative estimate. Five tiny (<2.9KB) items cannot be responsibly extrapolated to a 46KB-average population that includes ~342KB-average PDFs. The atomic reservation mechanism (point 6) is unaffected by this — it enforces whatever ceiling is set, atomically; the *ceiling itself* must be set by the authorizing gate with this gap explicitly acknowledged, not invented here. |
| `max_runtime_seconds` | runtime consumption (monotonic) | **7,200 (2 hours) — provisional first-batch policy choice; calibration required for later promotion** | Same status as `max_source_instances`: a concrete operational number chosen for this first batch, not a measured limit. No embedding-throughput benchmark exists yet against the local Ollama instance for this workload — Class 1's own measured throughput is what should set a calibrated figure for later classes. |
| Workspace safety reserve (`/home`) | physical, continuous | **10GB, explicit** — extraction budget = 25GB current free − 10GB reserve = 15GB ceiling today | A standing invariant, not batch-specific; extraction envelope must never be sized as "however much free space exists," only against this reserved-down budget. |
| PostgreSQL safety reserve (`/`) | physical, continuous | **5GB, explicit** — db-growth budget = 13GB current free − 5GB reserve = 8GB ceiling today | The *binding* constraint for embedding-heavy classes. Class 1's projected DB growth (~15MB of vectors + overhead) is trivial against this 8GB budget, but later archive classes must estimate their own `db_growth` against it explicitly — this is not merely "the floor," it's the reserve *plus* the growth this specific batch is projected to cause. |

**What would cause these to be reduced**: any hard-stop firing during
Batch 1 (see resource-guard thresholds), an error rate materially above
what the two pilots showed (both were 100%-explained, zero unexplained
failures), or workspace/Postgres headroom shrinking further before
Batch 1 runs (these are today's numbers, not guaranteed at execution
time). **What would cause these to be increased**: Batch 1 completing
with zero safety incidents, all four denominators reconciling cleanly,
and measured resource consumption well under the physical floors above
— feeding directly into the promotion ladder (point 8).

### 3. Archive SOURCE-SIZE tiers (declared compressed bytes — not a risk claim)

Adopting the D1-grounded buckets directly (not round numbers). These
name **declared, pre-extraction, compressed source size only** — a
physical fact read from D0/D1, nothing about expected expansion or
risk:

| Source-size tier | Declared source size | Groups | Physical copies | Total group bytes |
|---|---|---|---|---|
| SMALL | <10MB | 35 | 212 | 0.064 GB |
| MEDIUM | 10MB–1GB | 32 | 118 | 8.92 GB |
| LARGE | 1–5GB | 31 | 68 | 60.24 GB |
| EXTREME | 5GB+ | 3 | 14 | 45.04 GB |

**Explicitly not inferred**: decompressed/expansion size, and
therefore **not expansion-risk tiers**. D0/D1 record only compressed
source bytes; no extraction has occurred at any scale larger than
"zero real members" (both real pilots). Using a compressed-to-extracted
multiplier here would be inventing a compression ratio — instead,
Class 2 (SMALL source-size tier) is deliberately the *first* archive
class precisely so its extraction produces the first real
expansion-ratio evidence this corpus has ever generated, calibrating
Class 3+. The separate `risk_tier_estimated`/`risk_tier_actual` model
(point 4) is what actually carries risk judgments; the size tiers above
are one *input* to `risk_tier_estimated`, not a relabeling of it.

### 4. Risk-tier thresholds

```
LOOSE FILES:
  LOW      - TEXT_DOCUMENT/STRUCTURED_DATA workload, <1MB (the large
             majority, given the corpus's 46KB average)
  MEDIUM   - TEXT_DOCUMENT/STRUCTURED_DATA, 1-50MB (PDF-sized outliers)
  HIGH     - >50MB, any workload
  EXTREME  - >500MB as a single loose file (anomalous - would warrant
             review regardless of workload)

ARCHIVES AT ADMISSION (risk_tier_estimated):
  Informed primarily by the source-size tier above, since declared size
  is the main pre-extraction signal currently available - but this is
  a DISTINCT judgment from the size tier itself, not merely its label
  restated. Today, in the absence of other pre-extraction signals
  (compression-type-specific expansion history, corpus-wide duplication
  patterns), the estimate tracks size directly:
  LOW      - source-size tier SMALL
  MEDIUM   - source-size tier MEDIUM
  HIGH     - source-size tier LARGE
  EXTREME  - source-size tier EXTREME
  Future evidence (once Class 2+ produce real expansion/compression-type
  data) may inform risk_tier_estimated independently of size - this
  design leaves that door open rather than hard-wiring size==risk.

ARCHIVE ACTUAL RISK (risk_tier_actual - post-extraction evidence):
  UNRESOLVED precise numeric boundaries - no measured expansion-ratio/
  member-count/nesting-depth data exists yet for this corpus at any
  scale. Provisional structure only (multiplier boundaries NOT
  evidence-derived, explicitly flagged for calibration from Class 2's
  own results):
    LOW      - low expansion, low member count, shallow nesting
    MEDIUM   - moderate on any one of those axes
    HIGH     - high on any one axis
    EXTREME  - extreme on any axis
  NULL (insufficient evidence) - extraction failed before member
  enumeration completed (malformed archive, envelope abort, T7
  unavailable) - never fabricated as LOW or any other value.
```

### 5. Batch policy filters (selection predicates)

Respecting the frozen `SOURCE ADMISSION → extraction → MEMBER POLICY`
separation strictly — member `workload_category` is never available at,
and never used in, pre-extraction archive selection:

```
Class 1 (Text/Document):
  eligible AND source_category == LOOSE_FILE
          AND workload_category IN (TEXT_DOCUMENT, STRUCTURED_DATA)
          AND risk_tier_estimated IN (LOW, MEDIUM)

Class 2 (Small Archive):
  [Class 1 predicate, admitting loose documents alongside archives]
  OR (eligible AND source_category == ARCHIVE
      AND suffix IN (.zip, .7z)
      AND declared_source_bytes < 10MB
      AND NOT redundant_archive_copy)

  redundant_archive_copy is TRUE only when this candidate path is a
  member of a D1 exact-duplicate group (SHA-256 content hash, already
  computed by D1) that ALSO contains another archive path already
  selected in this or an earlier batch. This is the ONLY evidence
  allowed to establish redundancy - never filename similarity, path
  similarity, directory-naming convention (e.g. "Archive 2", dated
  snapshots), or compressed size alone. When exactly one candidate per
  D1 group is needed, selection takes the lexicographically-first path
  within that group (consistent with the frozen deterministic-ordering
  rule).

  **This is a selection/extraction-scheduling decision only, never a
  deduplication mutation.** Precisely: for archive processing, at most
  one representative of an exact-byte D1 duplicate group is admitted
  for extraction by the relevant batch policy. The other physical
  occurrences remain exactly what they already are — real, distinct
  `SourceInstance` rows and provenance observations on real T7 paths —
  and are NOT treated as deleted, discarded, merged, or globally
  equivalent merely because their own extraction was skipped. Nothing
  about their `SourceInstance` row changes; they simply remain
  eligible-but-unselected for archive-container extraction specifically,
  available to a future batch if this exclusion policy is ever
  revisited. No file is touched, moved, or removed on T7 or anywhere
  else as a result of this policy.

Class 3/4/5: identical shape to Class 2, substituting the size-tier
  bound (10MB-1GB / 1-5GB / 5GB+) and requiring the preceding class(es)
  to have already been promoted per the ladder (point 8).

Class 6/7 (Media/Software - completeness only):
  eligible AND source_category == LOOSE_FILE
          AND workload_category IN (MEDIA, SOFTWARE)

Class 8 (Special/Deferred): NEVER auto-selected by any class above -
  explicit exclusion, not merely "not matched": suffix IN
  (_EXCLUDED_SUFFIXES ∪ {.html, .dat, .db, no-extension, .apkg,
  .colpkg}) OR source_category == SPECIAL OR the (still-undesigned)
  backup/sync context signal matches.
```
The archive-duplicate exclusion clause in Class 2+ operationalizes the
"real finding" from the evidence section above — without it, the 2.2×
average duplication in the 1–5GB tier alone would roughly double actual
extraction work for no additional content-identity value.

### 6. Special / deferred content — explicit policy

| Content | Current code state (verified) | Batch policy |
|---|---|---|
| `.c9r` | Already `EXCLUDED` (`_EXCLUDED_SUFFIXES`) | Stays excluded — no batch ever selects it. |
| `.jpg`/`.jpeg`/`.png`/`.gif`/`.mp3`/`.mp4`/`.mov`/`.exe`/`.dll`/`.bin` | Already `UNSUPPORTED` | Selectable only under Class 6/7 (completeness), never Class 1–5. |
| `.wav`/`.m4a` | **Not** in either list — would attempt and fail `CORRUPT_INPUT` | Recommend adding to the unsupported list in a future implementation pass — not done here (docs-only). Meanwhile, excluded from every batch class above by not appearing in any class's workload set. |
| `.msi`/`.apk`/`.dat`/`.db`/no-extension | **Not** in either list — would attempt and likely fail `CORRUPT_INPUT` | Same recommendation as above; excluded from all classes here via Class 8. |
| `.html` (40.1GB, 7,896 files — the single largest "unaccounted" contributor) | **Not** in either list — would *succeed* via plain-decode as raw-markup "text," a quality problem | Deliberately routed to Class 8 (deferred), not Class 1, until a real HTML-to-text extractor exists — ingesting 40GB of raw tag soup as "text documents" would be a real, avoidable quality regression. |
| `.apkg`/`.colpkg` (Anki, 6.8GB) | Not in either list; likely fails plain-decode (binary SQLite-based format) | Class 8 — structured but niche; revisit with a dedicated extractor later. |
| Backup/sync/snapshot context (Syncthing, Takeout, "Archive 2", dated snapshots) | No code exists for this signal at all (frozen architecture defers it) | **Unresolved policy input** — no path patterns invented here, per instruction. Composes with `source_category` once designed, per the frozen architecture's own note on Batch F. |

### 7. First scaled real-T7 batch — exact criteria

```
Class:               1 (Text/Document)
Eligible workload:   LOOSE_FILE, workload_category IN (TEXT_DOCUMENT, STRUCTURED_DATA)
Admission policy:    risk_tier_estimated IN (LOW, MEDIUM); no archives
max_source_instances: 1,000 (policy choice for this first scaled batch, not
                      derived from a validated rate/scaling formula)
max_source_bytes:     2 GB (safety cap)
max_extracted_bytes:  N/A (no archives)
max_embeddings:       CALIBRATION_REQUIRED - the only real evidence (5 items,
                      3-2,821 bytes each) cannot be responsibly extrapolated to
                      this category's ~46KB average; this gap must be closed by
                      the gate authorizing Class 1's actual execution, not
                      guessed here. The atomic-reservation mechanism (point 6)
                      applies to whatever ceiling is ultimately set.
max_runtime_seconds:  7,200 (2 hours) - provisional first-batch policy choice;
                      calibration required for later promotion, same status as
                      max_source_instances
Risk ceiling:         MEDIUM
Stop conditions:      standard hard/soft/review set (point 9) - the tightest
                      remaining resource budget governs continued execution
                      (workspace and PostgreSQL guards both apply; PostgreSQL
                      is expected to be the one actually exercised) - plus
                      review-required on ANY unexpected failure-code class not
                      already seen in the two prior pilots
Expected evidence:    real embedding throughput at 100-250x pilot scale; real
                      chunk-count distribution across ~1,000 real documents; a
                      real failure-code histogram at scale (previously only ever
                      observed in single digits); confirmation the discovery-run-
                      scoped eligibility and immutable-membership invariants hold
                      under real, larger data
Promotion criteria:   zero safety incidents; zero unexplained claim/recovery
                      anomalies; error rate consistent with or better than the
                      two pilots (both had 100% explained outcomes); resource
                      consumption held within the physical budgets in point 2;
                      all four completion denominators reconcile with no gaps;
                      PLUS the calibration evidence below, since these are the
                      empirical inputs required to set max_embeddings/runtime/
                      resource limits for every later class:
                        - chunks per source distribution
                        - embeddings per source distribution
                        - embedding throughput/latency
                        - DB growth per embedded chunk
                        - workspace consumption
                        - processing time
                        - retry/error distribution
```
This is intentionally the *smallest* meaningful step up from the two
pilots (4 → 7 → 1,000 items) that still produces genuinely new evidence
(throughput and chunk-distribution data at real scale), not "as large
as possible." Note that `max_embeddings` being unresolved means Class 1
cannot actually be *executed* until that gap is closed by its own
authorizing gate — this numeric/policy pass defines the shape and the
other envelope values, not a claim that every number is ready to run.

### 8. Promotion ladder

Not a fixed size-doubling rule — each step requires specific evidence,
per the frozen architecture's evidence-based-promotion principle:

```
Class 1 (Text/Document, 1,000 files)
   ↓  requires: zero safety incidents; error rate ≤ pilot baseline;
      resource headroom maintained; all denominators reconcile
Class 1, scaled up (e.g. 5,000-10,000 files - exact number deferred to
   that gate's own authorization, grounded in Class 1's actual measured
   throughput/error rate, not chosen now)
   ↓  requires the above, PLUS: Class 1's measured embeddings-per-file
      and runtime-per-file actuals now replace the estimates in point 2
Class 2 (Small Archive, <10MB, first real archive-at-scale evidence)
   ↓  requires the above, PLUS: risk_tier_estimated vs risk_tier_actual
      agreement within an acceptable margin (UNRESOLVED numeric margin -
      no data exists to set one yet; Class 2's own report establishes
      the first such comparison) - "archive risk-estimation accuracy"
      only becomes measurable once Class 2 runs
Class 3 (Medium Archive, 10MB-1GB)
   ↓  requires the above, PLUS acceptable expansion-ratio variance
      (now measured twice, at Class 2 and Class 3)
Class 4 (Large Archive, 1-5GB) - the single biggest byte contributor
   ↓  requires materially more evidence given this class alone is
      ~60GB/31 distinct archives - a dedicated review checkpoint,
      not a routine promotion
Class 5 (Extreme Archive, 5GB+, 3 groups/14 copies/~45GB)
      own dedicated future gate - explicitly NOT part of the routine
      ladder; each of these 3 archives is individually reviewed
```
Class 6/7/8 do not sit on this ladder at all — they are either
low-priority completeness work (6/7) or deliberately excluded (8).

### 9. Resource-guard thresholds

Using the frozen architecture's required checks (disk, Postgres size,
Ollama reachability) with real, currently-measured baselines. Per the
governing invariant stated in point 2: all of these must remain above
their hard-stop threshold simultaneously, and **the tightest remaining
budget among them governs continued execution** — a batch does not
continue merely because the check its own workload happens to stress
is fine; every guard must clear.

| Check | Hard stop | Soft stop | Review-required | Calibration status |
|---|---|---|---|---|
| Workspace disk (`/home`, currently 25GB free) | free < 10GB | free < 15GB | any batch projected to consume >5GB | Set directly from measured headroom — no calibration needed. |
| PostgreSQL disk (`/`, currently 13GB free — the tighter constraint) | free < 5GB | free < 8GB | any batch projected to grow the DB by >2GB | Set directly from measured headroom. |
| Ollama reachability | unreachable on 3 consecutive checks | n/a | any single embedding call latency spike | Reachability check itself is ready now; a latency *ceiling* is **UNRESOLVED** — no baseline call-latency has ever been measured against this Ollama instance. Batch 1's own embedding calls should establish it. |
| CPU/RAM | not defined | not defined | not defined | Deferred entirely per the frozen architecture — optional, pluggable, `psutil` not adopted. |

### 10. Policy versioning

`selection_policy_version` identifies the batch *policy/class*, not an
opaque counter — e.g. `"batch-class-1-text-document-v1"`,
`"batch-class-2-small-archive-v1"`. A version bump is reserved for
changes to the *selection predicate itself* (which files a class
includes/excludes) — e.g. `v2` if `.html` were later promoted out of
Class 8 once a real extractor exists. Changing a **numeric envelope**
value (e.g. raising Class 1's `max_source_instances` from 1,000 to
5,000) does **not** require a new policy version, because envelope
values are already a separate, direct input to the selection fingerprint
(frozen at `f2b9815`) — so the fingerprint changes automatically on any
envelope change, without needing the policy version itself to change.
Policy version and envelope values are therefore both fingerprint
inputs, but for different reasons: policy version captures "what rule
decided inclusion," envelope values capture "how much was allowed."

### 11. Reporting — design-level specification only (not implemented)

The eventual `BatchReportService` (frozen architecture, not built yet)
must expose at minimum:
```
eligible_source_count          policy_filtered_count       selectable_count
source_instances_selected      unattempted_selected_count  attempted_source_count
terminal_source_count          successful_ingestion_count
source_bytes_selected          actual_source_bytes_read
extracted_bytes_consumed       embeddings_reserved         runtime_consumed_seconds
risk_tier_estimated vs risk_tier_actual distribution (including NULL/insufficient-evidence count)
drift outcomes (OBSERVED_AT_SELECTION / SOURCE_PRESENT_AT_EXECUTION /
  SOURCE_CHANGED_AFTER_SELECTION / SOURCE_MISSING_AT_EXECUTION counts)
stop_reason (and stop_reason_detail)
```
No implementation of this report is written in this pass.

### 12. Final review tables

**Policy table**

| Class | Source admission (source-size tier) | Member policy | `risk_tier_estimated` ceiling | `max_source_instances` | `max_source_bytes` | `max_extracted_bytes` | `max_embeddings` | `max_runtime` | Primary stop conditions | Promotion evidence |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `LOOSE_FILE`, TEXT/STRUCTURED | n/a | MEDIUM | 1,000 (policy choice) | 2GB | N/A | **CALIBRATION_REQUIRED** | 2h (policy choice, calibration required) | standard + tightest-resource-governs + new failure-code review | zero incidents, denominators reconcile, + full calibration list (point 7) |
| 2 | + `ARCHIVE` SMALL (<10MB), D1-dedup-aware admission | post-extraction | MEDIUM | TBD at gate | TBD at gate | UNRESOLVED | TBD at gate | TBD at gate | + extraction-envelope overflow | + expansion-ratio evidence produced |
| 3 | `ARCHIVE` MEDIUM (10MB–1GB) | post-extraction | MEDIUM | TBD | TBD | UNRESOLVED (calibrated from Class 2) | TBD | TBD | + risk-estimation accuracy | + accuracy within margin (TBD) |
| 4 | `ARCHIVE` LARGE (1–5GB) | post-extraction | HIGH | TBD | TBD | UNRESOLVED | TBD | TBD | dedicated review checkpoint | materially more evidence required |
| 5 | `ARCHIVE` EXTREME (5GB+) | post-extraction | EXTREME | 1 (one archive at a time) | n/a | UNRESOLVED | TBD | TBD | own dedicated gate | individual review per archive |
| 6/7 | `LOOSE_FILE` MEDIA/SOFTWARE — deferred, completeness-only | n/a | n/a — not risk-rated, unsupported | n/a, low priority | n/a | N/A | ~0 (mostly zero-attempt `UNSUPPORTED`) | short | standard | completeness accounting only, never a scaling step |
| 8 | none — deliberate exclusion | n/a | n/a | 0 | 0 | 0 | 0 | n/a | n/a | revisit only with dedicated future design |

**Decided now vs. requires empirical calibration**

| Decided now | Requires empirical calibration |
|---|---|
| Batch class taxonomy (1–8) and their admission predicates | Exact `max_source_instances`/`max_runtime` for Class 2 onward |
| `max_source_instances`=1,000 for Class 1, as an explicit **policy choice** (not derived) | `max_embeddings` for Class 1 itself — the only real evidence (5 tiny items) cannot be responsibly extrapolated to the category's real size mix |
| Archive SOURCE-SIZE tiers (SMALL/MEDIUM/LARGE/EXTREME), grounded in D1 — a physical-size fact only | `max_extracted_bytes` for any archive class (no expansion-ratio evidence exists) |
| Risk-tier *structure* (estimated/actual split, loose-file boundaries) | `risk_tier_estimated` boundaries for archives beyond "informed by size tier" (no other pre-extraction signal yet exists); all `risk_tier_actual` numeric boundaries (no member-count/nesting/expansion data exists) |
| Special/deferred content list (this pass's recommendations); Media/Software reclassified as deferred/completeness-only, not risk-rated | The backup/sync/snapshot contextual signal's pattern list (explicitly out of scope) |
| Resource-guard hard/soft/review thresholds for disk (measured directly); the tightest-remaining-budget-governs invariant | A DB-growth-per-embedding projection (deliberately not modeled — measured empirically instead, point 7); Ollama embedding-latency ceiling (no baseline exists) |
| Policy-versioning scheme and its relationship to the selection fingerprint | Class 1's own scaled-up size (deferred to that gate, informed by Class 1's actuals) |
| Archive-duplicate-copy exclusion as a selection/scheduling policy (never a dedup mutation — the other copies remain untouched, real `SourceInstance` rows) | Acceptable expansion-ratio/risk-estimation-accuracy margins for promotion |

### What this pass does NOT decide

- No implementation, schema, or migration - docs only. This freeze
  approves the numeric/policy baseline above; it does not authorize
  code, schema/migration work, or real-T7 access.
- No T7 access of any kind occurred in producing this pass - every
  number above is derived from `inventory.json`/`duplicate_analysis.json`
  (already on disk) and local, non-T7 disk/database measurements.
- The backup/sync/snapshot context field's structure and pattern list.
- Any UNRESOLVED/CALIBRATION_REQUIRED value marked above (`max_embeddings`
  for Class 1, all archive-class envelopes, `risk_tier_actual` numeric
  boundaries, the Ollama latency ceiling) - intentionally deferred to
  the relevant batch's own empirical calibration, never disguised as a
  settled number.

**Next gate, not yet authorized**: a separate implementation design
pass, translating this frozen policy layer into `IngestionBatch`,
selection, resource-guard, reservation, reporting, and worker-control
behavior while preserving every architectural contract already frozen
at `f2b9815`. Implementation itself remains a further, later gate
beyond that.

## Scaled Real-T7 Ingestion — Implementation Design Pass (design pass, APPROVED and frozen — no implementation yet)

Docs-only, sitting on top of the frozen architecture (`f2b9815`) and
frozen numeric/policy layer (`3d37ec0`). No T7 access, no code, no
schema/migration. Grounded in direct re-verification of the current
codebase (not assumption) — two real findings surfaced by that
re-verification are called out explicitly below because they change
what "translate the frozen design into code" actually requires.

### Two real findings from re-reading the current code

1. **`EmbeddingClient.embed()` is confirmed all-or-nothing** (verified
   by reading `app/embeddings/client.py`, not assumed per instruction):
   it makes exactly one `ollama.Client.embed(model=..., input=texts)`
   call for the *entire* list of texts and returns all vectors, or the
   call raises and none are produced. There is no partial-success mode
   to design around.
2. **`ArchiveExtractor.extract()` is atomic per archive, not
   incremental** (verified by reading `app/ingestion/archive.py`): it
   validates members/expansion/disk-space *once*, then calls
   `archive.extractall(destination)` in a single library call. There is
   no hook to abort partway through writing members. This means the
   frozen "monitor extracted bytes *during* extraction, abort if
   exceeded" language cannot be implemented as literal mid-extraction
   monitoring — the actually-achievable mechanism is a **pre-flight
   check**: sum `member.file_size` across the archive's central
   directory (the same computation `_validate_disk_space` already
   does) *before* calling `extract()` at all, and refuse to start if
   the projected total would exceed the batch's remaining budget. This
   achieves the same invariant (never a durable partial artifact) via
   prevention rather than interruption — a clarification of *how*, not
   a weakening of *what was frozen*. Also worth noting: `ArchiveExtractor`
   already has its own hardcoded `HARD_FREE_SPACE_BYTES = 10GiB` — the
   same order of magnitude as the new batch-level workspace reserve;
   the two checks are complementary layers, not a replacement for each
   other, and both remain in effect.

### 1. `IngestionBatch` model — exact fields

```
id                       Integer, PK, autoincrement
classification_run_id    Integer, FK -> classification_runs.id, UNIQUE, NOT NULL
                          (UNIQUE enforces the frozen 1:1 exclusive ownership
                          at the DB level, not merely by service convention)
status                   Enum(BatchStatus), NOT NULL, default PLANNED
stop_reason              Enum(BatchStopReason), NULLABLE
stop_reason_detail       Text, NULLABLE
review_required          Boolean, NOT NULL, default False
                          (set once at creation from the projection check -
                          orthogonal to status, per the frozen three-tier model;
                          NOT itself a status transition)

-- envelope (immutable once set; NOT NULL once a batch actually exists -
-- CALIBRATION_REQUIRED is a statement that batch CREATION must be refused
-- until a human supplies a real number, never a stored NULL sentinel)
max_source_instances     Integer, NOT NULL
max_source_bytes         BigInteger, NOT NULL
max_extracted_bytes      BigInteger, NULLABLE (NULL = "not applicable", e.g.
                          Class 1 admits no archives - a real, meaningful NULL,
                          distinct from CALIBRATION_REQUIRED's "not yet decided")
max_embeddings           Integer, NOT NULL
max_runtime_seconds      Integer, NOT NULL

-- selection-time facts (immutable once set)
eligible_source_count    Integer, NOT NULL
policy_filtered_count    Integer, NOT NULL
selectable_count         Integer, NOT NULL
source_instances_selected Integer, NOT NULL
source_bytes_selected    BigInteger, NOT NULL
selection_fingerprint    String(64), NOT NULL   (SHA-256 hex)
selection_policy_version String, NOT NULL
ordering_version         String, NOT NULL

-- runtime counters (mutable ONLY via narrow atomic service methods,
-- never raw ORM attribute sets from calling code)
extracted_bytes_consumed BigInteger, NOT NULL, default 0
embeddings_reserved      Integer, NOT NULL, default 0
monotonic_runtime_seconds_consumed  Float, NOT NULL, default 0

-- timestamps (wall-clock, audit-only - NEVER used for runtime-budget
-- decisions; monotonic_runtime_seconds_consumed is authoritative for that)
created_at               DateTime(timezone=True), NOT NULL, default now
started_at               DateTime(timezone=True), NULLABLE
completed_at             DateTime(timezone=True), NULLABLE
```

`BatchStatus`: `PLANNED | RUNNING | PAUSED | COMPLETED | ABORTED`
(exact transitions in point 12).

`BatchStopReason` (set only when leaving `RUNNING`; distinguishes
*designed* envelope exhaustion, which is a normal path to `COMPLETED`,
from *unplanned* interruption, which is `PAUSED`/`ABORTED` — see point
12's mapping table):
```
SOURCE_WORK_EXHAUSTED            (no eligible work remains - normal COMPLETED path)
EXTRACTED_BYTES_ENVELOPE_EXHAUSTED   (normal COMPLETED path)
EMBEDDINGS_ENVELOPE_EXHAUSTED        (normal COMPLETED path)
RUNTIME_BUDGET_EXCEEDED              (normal COMPLETED path)
WORKSPACE_SOFT_STOP               -> PAUSED (resumable)
POSTGRES_SOFT_STOP                -> PAUSED (resumable)
MANUAL_PAUSE                      -> PAUSED (resumable)
WORKSPACE_HARD_STOP               -> ABORTED (terminal)
POSTGRES_HARD_STOP                -> ABORTED (terminal)
OLLAMA_PERSISTENTLY_UNREACHABLE   -> ABORTED (terminal)
SAFETY_INVARIANT_VIOLATION_DETECTED  -> ABORTED (terminal)
```

**Immutability rule**: `classification_run_id`, every `max_*` field,
`eligible_source_count`/`policy_filtered_count`/`selectable_count`/
`source_instances_selected`/`source_bytes_selected`,
`selection_fingerprint`, `selection_policy_version`, `ordering_version`
are written exactly once, at creation, inside the batch-creation
transaction (point 2), and never updated by any later code path — no
service method exposes updating them. Only `status`, `stop_reason`,
`stop_reason_detail`, the three runtime counters, and the three
timestamps are ever mutated post-creation, each through a specific,
narrow method (mirroring `WorkerClaimService`'s existing pattern of
purpose-built methods rather than generic setters).

**`selection_fingerprint` semantics**: SHA-256 hex digest of a
canonical serialization of `(D0 report_sha256, selection_policy_version,
ordering_version, envelope values in a fixed field order, the sorted
list of selected (root_t7_path, member_path) pairs)` — computed once,
after selection completes and before commit.

**`selection_policy_version` semantics**: identifies which selection
*predicate* was used (e.g. `"batch-class-1-text-document-v1"`) —
bumped only when the predicate logic changes, never for a numeric
envelope change (per the frozen numeric pass).

**`ordering_version` semantics**: identifies the deterministic ordering
algorithm (e.g. `"lexicographic-path-v1"`) — versioned separately so a
future ordering change is distinguishable from a policy change.

### 2. Batch creation transaction

**Two phases, not one**: a read-only *computation* phase (D0 report
read, eligibility, policy filtering, ordering, envelope-aware
selection — pure functions over already-committed data and the D0 JSON
file, no writes) followed by one **atomic write phase**:

```
BEGIN
  pg_advisory_xact_lock(lock_key)   -- see exact key derivation below
  re-verify eligibility under the lock (a second, authoritative read -
     the computation phase's read was necessarily unlocked and could be stale)
  IF selected is empty: ROLLBACK, return None (no ClassificationRun or
     IngestionBatch is created for a vacuous batch)
  INSERT ClassificationRun (this batch's dedicated, never-shared run)
  INSERT SourceInstance rows for every selected path (member rows are
     NOT created here - only container/loose rows; members are created
     later, during archive extraction, per point 11)
  compute selection_fingerprint from the now-known IDs/paths
  INSERT IngestionBatch (status=PLANNED)
COMMIT
```

**Advisory lock precision, stated exactly (per review)**:
- **Scope**: one lock per `DiscoveryRun` — two batch-creation attempts
  against *different* `DiscoveryRun`s never contend; only two attempts
  against the *same* `DiscoveryRun` serialize.
- **Key derivation**: `lock_key = deterministic 64-bit hash of the
  namespaced string "ingestion_batch_creation:discovery_run:{discovery_run_id}"`
  — namespaced (not the raw `discovery_run_id` integer used directly)
  so a future, unrelated use of advisory locks elsewhere in this
  codebase cannot collide with this one merely by sharing a small
  integer id space. Deterministic so repeated calls for the same
  `DiscoveryRun` always target the same lock.
- **Transaction lifetime**: `pg_advisory_xact_lock` specifically (the
  transaction-scoped variant), never the session-scoped
  `pg_advisory_lock`. It is released automatically at `COMMIT` or
  `ROLLBACK` of the enclosing transaction — no explicit unlock call
  exists anywhere in this design, and no lock can leak past the
  transaction's own lifetime.
- **What it serializes**: only the "read current eligibility, then
  materialize `SourceInstance` rows" sequence *during batch creation*
  for one `DiscoveryRun`. It has no effect on and does not serialize
  batch *execution* (claiming, embedding, extraction) — those keep
  their own, already-existing, unrelated concurrency primitives.
- **What still provides correctness independently of the lock — per
  review, the lock is not a substitute for a DB constraint**: today,
  verified directly, `SourceInstance` has no `UNIQUE` constraint
  preventing two rows from ever representing the same
  `(classification_run_id, root_t7_path, member_path)` triple. The
  advisory lock provides practical, non-blocking serialization for the
  *normal* code path; a `UNIQUE` constraint on exactly that triple is
  recommended as implementation-time hardening — the invariant of last
  resort if some future code path ever fails to take the lock
  correctly, exactly the same defense-in-depth relationship
  `IngestionBatch.classification_run_id`'s own `UNIQUE` constraint
  already has to service-level discipline elsewhere in this design.
- **Crash/rollback behavior**: if the creating transaction crashes or
  rolls back after acquiring the lock, Postgres releases it the instant
  the transaction ends — automatically, with no manual cleanup and no
  orphaned-lock recovery logic needed. A rollback leaves zero
  `ClassificationRun`/`SourceInstance`/`IngestionBatch` rows, per the
  transaction-atomicity design below.

**Why the advisory lock, not optimistic retry**: this project's
established pattern for "prevent two things from claiming the same
work" is a DB-native primitive (`SELECT ... FOR UPDATE SKIP LOCKED` for
per-row claims elsewhere in this codebase) — an advisory lock is the
same *class* of primitive applied to "no row exists yet to lock,"
serializing the eligibility-check-then-materialize sequence rather than
inventing a new optimistic-concurrency scheme — but, per the point
above, it is deliberately paired with a recommended DB constraint, not
relied upon alone.

**Failure handling, without inventing cross-transaction guarantees**:
- No sources qualify: refuse to create a batch at all (return `None`),
  matching the existing pattern where `resolve_next()`/
  `process_next_archive()` return `None` rather than create an empty
  placeholder attempt.
- Envelope boundary reached during selection: not an error — normal
  termination of the selection loop (point 5); the batch is created
  with whatever was selected.
- Concurrent operation on the same `DiscoveryRun`: serialized by the
  advisory lock; the second caller blocks, then re-evaluates eligibility
  fresh once the first transaction commits or rolls back.
- `SourceInstance` materialization race: prevented structurally by the
  lock; as defense-in-depth, any DB-level constraint violation rolls
  back the whole transaction — fails safe, retriable, no partial rows.
- Fingerprint construction fails, or anything fails before `COMMIT`:
  standard ACID rollback — zero side effects. Nothing beyond that single
  transaction's atomicity is promised (no claim about coordinating
  across a crash *after* `COMMIT` but before the caller observes
  success — the data is durably correct either way; only the caller's
  knowledge of the outcome could be delayed, never the data's
  integrity).

### 3. Immutable membership — implementation constraints

- **Membership query**: `SELECT * FROM source_instances WHERE
  classification_run_id = :batch.classification_run_id` — this *is*
  membership, in full, given exclusive 1:1 ownership (point 1's UNIQUE
  constraint).
- **Resume avoids reselection structurally**: the worker loop for an
  existing `IngestionBatch` never calls the selection algorithm again —
  it only ever calls `WorkerClaimService` methods, which (per the
  required change below) are scoped by `classification_run_id`.
  Selection is invoked exactly once, inside batch creation (point 2).
- **Required change surfaced by this design**: today's
  `WorkerClaimService` claim queries (`claim_source_instance_for_
  identity_resolution`, `claim_source_instance_for_archive_processing`)
  filter *globally* across all `SourceInstance` rows — verified by
  reading their exact `WHERE` clauses — with **no
  `classification_run_id` filter at all**. Without adding one, a worker
  processing batch N could claim a `SourceInstance` belonging to a
  *different* batch. This must become a required parameter on both
  methods (point 10 covers the trickier case of
  `claim_content_identity_group`, which is reached only indirectly).
- **Code/policy changes cannot retroactively alter membership**:
  membership is a materialized fact (already-committed rows), not a
  recomputed query result — no later code change can affect which rows
  already exist. The only way membership could appear to change would
  be something inserting a new `SourceInstance` under an *existing*
  batch's `classification_run_id` from outside its creation transaction
  — explicitly forbidden as an invariant; nothing in this design does
  that.
- **A later `DiscoveryRun` legitimately re-observing the same path**:
  eligibility for `DiscoveryRun_2`'s batch is scoped to `DiscoveryRun_2`'s
  own observations (per the frozen architecture) — a path already
  materialized under `DiscoveryRun_1`'s batch is simply a *different*
  observation/run scope and remains eligible for `DiscoveryRun_2`,
  producing a genuinely new `SourceInstance` row. `get_or_create_group`
  independently determines convergence or divergence from the bytes
  actually read this time — unchanged, already-proven mechanism.

### 4. Policy engine

Small, pure functions extending `eligibility_service.py`'s existing
suffix-only pattern — deliberately not a heavyweight "engine" class:

```
classify_source_category(path) -> SourceCategory        # pure, suffix/path only
classify_workload_category(path) -> WorkloadCategory     # pure, suffix only
classify_risk_tier_estimated(source_category, workload_category,
                              declared_size_bytes) -> RiskTier   # pure
classify_backup_sync_context(path) -> BackupSyncContext | None
    # STILL DEFERRED - always returns None until a future gate defines
    # the pattern list; an explicit stub, never a guess
```

**Normalization decision, closing the numeric pass's open question**:
`classify_workload_category` **does** strip Syncthing's `_<digits>`
conflict-rename suffix before mapping to a workload (e.g.
`.pdf_1768918262` → `.pdf` → `TEXT_DOCUMENT`) — the numeric pass's
analytical normalization is hereby adopted as the real classification
rule too, since treating 8,000+ real files as `UNKNOWN` merely because
of a sync tool's rename convention would be a needless, avoidable
misclassification. This is a concrete decision, not left open.

**Archive source-admission policy**: `admits_archive(source_size_tier,
batch_class_policy) -> bool`, evaluated only against container-level
facts (declared size, suffix) — never opens the file, never touches
member data (point 11 enforces this is impossible at this stage since
members don't exist yet).

**Archive member policy**: applied *after* extraction, to each
newly-created member `SourceInstance`, using the *same*
`classify_workload_category`/eligibility functions already used for
loose files — no member-specific logic beyond running the same
classifiers on the member's own relative path.

**D1 exact-duplicate archive representative scheduling**: a selection-
time function grouping candidate archive paths by their D1
duplicate-group id (already computed by D1), admitting only the
lexicographically-first path per group — per the frozen policy, this
is scheduling only, never a mutation of the other copies' rows.

**Deferred/special content**: a static, explicit frozenset (matching
`_EXCLUDED_SUFFIXES`/`_KNOWN_UNSUPPORTED_SUFFIXES`'s existing pattern),
never auto-inferred.

**Explainability**: each classifier's inputs/version are recorded in
the *existing* `SourceInstance.evidence_snapshot` JSONB field — no new
column needed for "why," reusing the field that already exists for
exactly this purpose. The `eligible → policy_filtered → selectable →
selected` funnel counts (point 1's new fields) are captured once,
during selection, since `eligible`/`policy_filtered`/`selectable` are
properties of a specific point in time during selection and cannot be
reconstructed later purely from the final `SourceInstance` rows.

### 5. Selection algorithm — exact behavior

```
eligible = compute_eligible(discovery_run)   # D0 paths, scoped per point 1 above
eligible_source_count = len(eligible)
ordered = sorted(eligible, key=relative_path_string)   # ordering_version="lexicographic-path-v1"
policy_filtered = [p for p in ordered if batch_class_policy.matches(p)]  # order preserved
policy_filtered_count = len(policy_filtered)

selected, running_instances, running_bytes = [], 0, 0
for path in policy_filtered:                 # single forward pass, in order
    if running_instances + 1 > max_source_instances: break
    declared_size = d0_declared_size(path)
    if running_bytes + declared_size > max_source_bytes: break
    selected.append(path)
    running_instances += 1
    running_bytes += declared_size
selectable_count = index reached before the break (or len(policy_filtered) if none)
source_instances_selected = len(selected)
source_bytes_selected = running_bytes
```

**Exact tie-breaking decision, per instruction not to invent a
different algorithm implicitly**: the *first* item that would exceed
*either* limit **stops selection entirely (`break`)** — it is never
skipped in favor of continuing to scan for smaller items later in
order. This keeps the algorithm simple, reproducible, and non-
reordering, at the cost that one oversized item early in lexicographic
order can truncate a batch well short of `max_source_instances`. A
"skip instead of stop" variant is a genuinely different algorithm and
would need its own `ordering_version`/policy, not a silent substitution
here.

**Preserved explicitly, per review, as load-bearing for reproducibility
— not an arbitrary tie-break**: because selection stops rather than
skips, the exact same input state (D0 report, already-materialized
`SourceInstance` rows, policy, envelope) always produces the exact same
selected set, and therefore the exact same `selection_fingerprint`
(point 1). A hypothetical skip-and-continue variant could still be made
deterministic in principle, but changing between the two behaviors is a
change to the selection *algorithm itself* and requires a new
`ordering_version`/`selection_policy_version` — this stop-not-skip rule
is treated as part of what "reproducible" means for this design, not a
detail that could be silently swapped later without changing those
version identifiers.

**What happens when**:
- A source is no longer present, or its metadata differs from D0:
  **not detectable at selection time** — selection never touches T7,
  only D0's declared metadata. These surface later, at actual read
  time, via the already-frozen `T7_UNAVAILABLE` /
  `SOURCE_CHANGED_AFTER_SELECTION` outcomes — no presence check is
  added to selection.
- Policy evaluation cannot confidently classify a source: it falls
  through to `UNKNOWN`/`SPECIAL` and simply fails to match any Class
  1–7 policy predicate — it remains technically eligible for a future,
  differently-classed batch, just not selected now. No forced
  classification.

### 6. `BatchResourceGuard`

```
class BatchResourceGuard:
    def check_before_claim(batch) -> GuardResult          # workspace + Postgres free space
    def check_before_expensive_operation(batch, kind) -> GuardResult  # e.g. before
                                                            # starting archive extraction,
                                                            # or immediately before an
                                                            # embedding call specifically
    def check_ollama_reachable() -> bool
```
- **Before claiming work**: workspace + Postgres free space (cheap,
  fast) — every claim-attempt cycle, before calling any
  `WorkerClaimService` method.
- **During work**: the extracted-bytes running counter (point 7) and
  monotonic elapsed runtime (point 9), checked at the same per-cycle
  cadence.
- **Before expensive operations**: Ollama reachability checked
  immediately before an actual embedding call (not merely once per
  cycle); workspace free space re-checked immediately before starting
  archive extraction specifically, since that is the operation that
  could consume a large chunk of it.
- **`PAUSED`**: a soft-stop threshold fires (workspace <15GB, Postgres
  <8GB free — the reserve-vs-measurement distinction from the numeric
  pass applies identically here) or an operator issues a manual pause —
  resumable.
- **`ABORTED`**: a hard-stop threshold fires (workspace <10GB, Postgres
  <5GB free), Ollama unreachability persists past its threshold, or a
  safety-invariant violation is detected — terminal, never resumable.
- **Interaction with isolated staging**: a guard failure *during*
  archive extraction (e.g. disk fills further from something else
  entirely on the machine) receives the identical disposition as an
  extracted-bytes envelope overflow (point 7): staging discarded
  wholesale, claim released, no `IngestionAttempt` recorded.
- **Persistence**: `status` and `stop_reason`/`stop_reason_detail` are
  set together in one `UPDATE`, never as two separate writes that could
  leave them inconsistent.
- **`review_required`** (point 1's new field) is computed once, at
  batch creation, from the projection check — purely advisory,
  orthogonal to `status`, never itself pausing or aborting anything.
- No DB-growth projection formula is introduced here either — per the
  frozen numeric pass, `BatchReportService` measures actual DB-size
  delta (before/after `pg_database_size()`) post hoc, never predicts it.

### 7. Extracted-bytes accounting

**No wording implies `ArchiveExtractor` interrupts extraction member-
by-member** — finding 2 (verified: `extractall()` is one atomic call)
governs the entire design below. **Three distinct quantities, never
conflated**:
```
declared_member_bytes           - sum(member.file_size for non-dir members),
                                   read from the archive's own central directory
                                   via metadata inspection ALONE (zipfile.ZipFile(path)
                                   .infolist() / the py7zr equivalent) - zero bytes
                                   extracted yet, this is an ESTIMATE from metadata,
                                   never proof of what will actually be written
actual_durable_extracted_bytes  - sum(DiscoveredFile.size for each REAL extracted
                                   file, via ArchiveExtractor's own existing
                                   _discover_extracted_files, which Path.stat()s
                                   each real file after extraction) - MEASURED,
                                   not estimated, and only known after extract()
                                   returns successfully
peak/transient staging usage    - actual disk consumption while files sit in
                                   _staging/ before promotion or deletion - a
                                   live-disk-pressure concern, protected by the
                                   resource guard's shutil.disk_usage check
                                   (point 6), NOT by extracted_bytes_consumed at all
```

Exact sequence:
```
1. archive metadata inspection (no extraction): open the archive's central
   directory only
2. declared_member_bytes = sum(member.file_size for non-dir members)   [ESTIMATE]
3. compare against remaining envelope - atomic conditional admission,
   using the ESTIMATE (same discipline as embedding reservation):
     UPDATE ingestion_batches SET extracted_bytes_consumed = extracted_bytes_consumed + :declared_member_bytes
     WHERE id = :batch_id AND extracted_bytes_consumed + :declared_member_bytes <= max_extracted_bytes
     RETURNING extracted_bytes_consumed
   IF no row returned: REFUSE BEFORE EXTRACTION - extract() is never called;
     delete any pre-existing staging dir for this source_instance_id (idempotent
     cleanup, see crash/retry below); release claim; no IngestionAttempt
     recorded; item deferred, worker moves on
4. IF admitted: extract atomically into workspace_root/_staging/<source_instance_id>/
   (extractall(), all members or an exception - never partial per finding 2)
5. ON SUCCESS: measure actual_durable_extracted_bytes from the real extracted
   files (step above) - this MEASURED value, not the step-2 estimate, is what
   the durable counter is reconciled to:
     delta = actual_durable_extracted_bytes - declared_member_bytes   (can be
             positive, negative, or zero - declared size is NEVER assumed to
             equal actual bytes written)
     UPDATE ingestion_batches SET extracted_bytes_consumed = extracted_bytes_consumed + :delta
     WHERE id = :batch_id   (a plain, unconditional correction - the admission
     decision already happened in step 3; this only makes the reported total
     accurate to what was REALLY written, never re-litigates admission)
   promote staging -> archive_<id>/ (the final, non-staging location)
6. ON EXTRACTION FAILURE (extract() raises): delete staging wholesale; release
   the ORIGINAL declared_member_bytes reservation exactly (symmetric to step 3,
   since no actual bytes durably exist to reconcile against):
     UPDATE ingestion_batches SET extracted_bytes_consumed = extracted_bytes_consumed - :declared_member_bytes
     WHERE id = :batch_id
```
**Declared size is never treated as proof of actual bytes written** —
step 5's reconciliation exists specifically because a real archive's
central-directory metadata could in principle diverge from what is
actually written to disk; the durable counter always converges to the
*measured* value, with the declared estimate serving only the
admission decision in step 3.

The step-3 conditional `UPDATE` is the actual safety net (closing a
real race between two workers reading a stale counter); the pre-flight
metadata inspection is an efficiency optimization avoiding wasted
extraction work on something that was never going to fit, not itself
the safety mechanism.

- **Durable extracted bytes** = the reconciled, measured value from
  step 5 — never counted while still in staging, and never simply
  equated to the step-2 estimate.
- **Transient staging bytes** are never added to
  `extracted_bytes_consumed` at all; discarding staging (overflow,
  guard failure, or extraction error) never touches the durable
  counter beyond the symmetric release in step 6, satisfying "discarded
  staging does not count as durable extracted consumption."
- **Peak physical workspace usage vs. cumulative extracted bytes**:
  kept fully distinct — the resource guard's live `shutil.disk_usage`
  check (point 6) is what protects against peak physical usage
  (measures actual free space on disk, regardless of any counter); the
  `extracted_bytes_consumed` counter tracks the batch's *cumulative
  durable allowance*, a policy-level accounting concept that does not
  by itself reflect current disk pressure from other sources.
- **Crash/restart**: before any (re)extraction attempt for a given
  `source_instance_id`, delete any pre-existing staging directory for
  it first (idempotent cleanup) — a genuinely new requirement, since no
  staging concept exists in the current implementation to clean up
  after.
- **Retry**: identical to crash/restart — always starts from a clean
  staging directory.

### 8. Embedding reservation — fenced lifecycle, including crash recovery

**Round 2's design had a real hole, correctly identified**: a
presence-only predicate (`reserved_embeddings IS NOT NULL`) cannot
distinguish "no one holds a reservation" from "*someone else, later,*
holds a reservation." A worker delayed rather than dead (a classic
zombie-worker scenario, not merely a crash) can wake after recovery has
already reclaimed its row and a *new* claimant has reserved fresh
capacity — the old worker's release would match the new reservation
purely because both happen to leave the column non-NULL. Presence
alone is not ownership.

**Frozen invariant, strengthened**: a reservation may be consumed or
released only by the exact claim-generation identity that created it,
or by an explicitly authorized recovery operation acting on that exact
generation — never by a state-only predicate that a later, unrelated
generation could accidentally satisfy.

**Fencing mechanism — one new integer alongside the existing markers,
still no new table**: `ContentIdentityGroup.claim_generation: Integer,
NOT NULL, default 0`, incremented by exactly 1 every time the row is
claimed — whether that claim is fresh (`claimed_by`/`claimed_at` were
NULL) or a stale-reclaim (they were set, but expired). Every claim
returns its own `claim_generation` value to the caller, who holds it
for the lifetime of that attempt. `reserved_embeddings` (unchanged from
Round 2: `Integer | NULL`) now co-exists with this fence: every
mutating operation on the reservation — reserve, consume, release,
recover — includes `AND claim_generation = :my_generation` in its
`WHERE` clause. A delayed worker's `:my_generation` is, by definition,
*older* than whatever generation the row has moved to once it has been
reclaimed — so its operation matches zero rows and is a safe no-op,
**structurally**, not by luck of timing.

**Exact lifecycle**:
```
claim (generation N):
    UPDATE content_identity_groups
    SET claimed_by = :worker_id, claimed_at = now(), claim_generation = claim_generation + 1
    WHERE id = :group_id AND (claimed_by IS NULL OR claimed_at < stale_before)
    RETURNING claim_generation
    -- the caller remembers :my_generation = the returned value for this attempt's lifetime

reserve (generation N):
    UPDATE content_identity_groups SET reserved_embeddings = :n
    WHERE id = :group_id AND claim_generation = :my_generation
    -- fenced: if the row has since moved to generation N+1 (this worker is stale),
    -- this matches zero rows - the worker MUST check for this and abort rather than
    -- proceed to call embed() at all
    AND (same logical operation):
    UPDATE ingestion_batches SET embeddings_reserved = embeddings_reserved + :n
    WHERE id = :batch_id AND embeddings_reserved + :n <= max_embeddings
    RETURNING embeddings_reserved
    -- IF either UPDATE fails to match/return: roll back whichever part succeeded;
    -- release claim; no embed() call; no IngestionAttempt recorded; item deferred

embed (generation N):
    EmbeddingClient.embed(texts) - confirmed all-or-nothing (finding 1):
    either all :n chunks receive vectors, or an exception is raised and none do

consume (generation N, success):
    - chunk embeddings persisted (existing DocumentChunk.embedding update)
    - clear the reservation, FENCED to this generation (same idempotent
      operation as release/recover below, invoked from the success path)
    - embeddings_reserved on the batch is NOT decremented (monotonic - unchanged)
    - release claim normally, ALSO fenced: only generation N's own worker may
      clear claimed_by/claimed_at for generation N

release (generation N, failure - embed() raises) OR
recover (authorized recovery of a stale generation N, performed by
whichever worker's claim attempt observes staleness):
    BOTH paths use the exact SAME idempotent, OWNERSHIP-CHECKED operation:
      UPDATE content_identity_groups
      SET reserved_embeddings = NULL
      WHERE id = :group_id AND claim_generation = :my_generation
        AND reserved_embeddings IS NOT NULL
      RETURNING reserved_embeddings
    IF a row is returned (won the fenced NOT NULL -> NULL transition FOR
      THIS EXACT GENERATION): UPDATE ingestion_batches SET embeddings_reserved
      = embeddings_reserved - :released_n WHERE id = :batch_id
    IF no row returned - EITHER already NULL (released by a concurrent/prior
      operation for the SAME generation), OR the generation has already moved
      on (this caller is stale): do nothing further - both cases are
      indistinguishable from the caller's point of view, and both correctly
      require no action from a stale caller
```

**Recovery is authorized precisely because it reads under the row
lock, not because it is a special-cased caller**: the *same*
`SELECT ... FOR UPDATE SKIP LOCKED` claim query that reclaims a stale
row (by construction — no separate recovery service, unchanged from
this codebase's existing design) reads the row's *current*
`claim_generation`/`reserved_embeddings` fresh, under lock, immediately
before incrementing the generation. It therefore always acts on the
*true current* generation, never a remembered, potentially-stale one —
this is the structural difference between "recovery" (always
current-generation, lock-authorized) and "a stale worker's own late
release" (always a remembered, possibly-outdated generation, fenced
out the moment it no longer matches).

**Exact recovery sequence**:
```
generation N becomes stale (claimed_at < stale_before, existing mechanism)
  -> the reclaiming SELECT ... FOR UPDATE reads generation N's current
     reserved_embeddings under lock
  -> releases generation N's reservation via the fenced operation above
     (matches, because this read is fresh/current, not remembered)
  -> claim_generation increments to N+1; claimed_by/claimed_at set fresh
  -> generation N is now invalidated - any operation later presented with
     :my_generation = N will match zero rows, permanently, for this row
  -> generation N+1 may claim and reserve independently, exactly as if
     starting fresh
```

**Why cross-batch identity convergence cannot create ownership
ambiguity**: `ContentIdentityGroup.claimed_by`/`claimed_at`/
`claim_generation` are a *single* set of columns on *one* row — at most
one `(worker, generation)` pair can hold them at any instant, guaranteed
by the existing `SELECT ... FOR UPDATE SKIP LOCKED` claim mechanism,
regardless of how many different batches' `SourceInstance` rows happen
to reference that group. If `Batch A → SourceInstance A → Group X` and
`Batch B → SourceInstance B → Group X`, only one of A's or B's workers
can hold Group X's claim at a given moment; the fencing token scopes
the reservation to *that* specific claim attempt, not to "batch A" or
"batch B" as a label — there is structurally never a moment where two
different batches' work on the same group could hold simultaneous,
ambiguous reservations. A reservation is therefore always attributable
to the exact claim-generation lifecycle that created it, independent of
and never relying solely on which `ContentIdentityGroup` is involved.

**Why no new table is needed — explicit justification across all four
axes named in review**:
```
content identity  - the row IS the ContentIdentityGroup; one row per identity,
                     no ambiguity possible
work item          - at this pipeline stage, the "work item" IS the
                     ContentIdentityGroup itself (normalize/chunk/embed
                     operate on groups, not on SourceInstances) - work-item
                     identity and content identity coincide exactly
batch              - never needs encoding here: the claim is globally
                     exclusive (one worker at a time, regardless of which
                     batch's SourceInstance made the group eligible), so
                     batch-level ambiguity cannot arise structurally (see above)
claim generation   - the new claim_generation integer, incremented on every
                     claim/reclaim - the exact, and only, dimension needed to
                     distinguish "this attempt" from "a later attempt on the
                     same row"
```
Co-locating `reserved_embeddings` and `claim_generation` alongside the
pre-existing `claimed_by`/`claimed_at` on `ContentIdentityGroup` is
therefore sufficient; a separate reservation table would add a join and
a second source of truth for no additional safety.

- **Concurrency**: all conditional `UPDATE`s serialize via Postgres
  row-level locking on the single row they target — correct, though a
  known point of contention under very high concurrency (acceptable at
  this project's scale; not a scenario this design needs to optimize
  for).
- **Batch pause/stop with reservations outstanding**: reservations are
  *not* released merely because the batch pauses — they represent real,
  potentially still-in-flight work; only the stale-claim-triggered
  recovery path above ever clears one, always fenced to the generation
  it actually belongs to.

**New schema surfaced by this correction**: two columns on
`ContentIdentityGroup` — `reserved_embeddings: Integer | NULL` (Round 2)
and `claim_generation: Integer, NOT NULL, default 0` (this round) —
noted in the design gap register (point 19) as `DECIDED/FROZEN`, not
deferred; the mechanism above is a complete, closed design, not an open
question.

### 9. Runtime accounting — honest guarantees

```
process start / resume:  session_start = time.monotonic()  (a NEW timer each
                          session - the prior session's monotonic value is
                          meaningless across a process restart)
pause / graceful stop / completion:
    session_delta = time.monotonic() - session_start
    UPDATE ingestion_batches SET monotonic_runtime_seconds_consumed =
        monotonic_runtime_seconds_consumed + :session_delta WHERE id = :batch_id
crash (no graceful exit):
    the delta since the LAST checkpoint is LOST - never recorded.
```
**Explicit, honest limitation, per instruction not to overclaim**:
exact runtime accounting across an *unclosed* (crashed) process is
**not guaranteed** — only graceful pause/stop/completion checkpoints
are accounted for; `monotonic_runtime_seconds_consumed` is therefore a
lower bound on true wall-clock time whenever a crash occurs without an
intervening checkpoint, never a promised-exact figure.

**Recommended, not frozen**: a periodic heartbeat checkpoint (e.g.
every N claim-cycles) would bound the worst-case undercounting from a
crash. This is a design recommendation for the implementation to
consider, not a requirement the frozen architecture mandated.

### 10. Claim / batch interaction

**Frozen implementation invariant**: a batch worker may claim only
`SourceInstance` rows belonging to that batch's dedicated
`ClassificationRun`.

**Exact claim predicate** (extending, not replacing, the existing
`SELECT ... FOR UPDATE SKIP LOCKED` mechanism — verified directly that
neither method filters by it today):
```
claim_source_instance_for_identity_resolution(
    *, worker_id: str, lease_duration: timedelta, classification_run_id: int
) -> SourceInstance | None:
    WHERE classification_run_id = :classification_run_id
      AND content_identity_group_id IS NULL AND member_path IS NULL
      AND NOT (root_t7_path archive-suffixed)
      AND (claimed_by IS NULL OR claimed_at < stale_before)

claim_source_instance_for_archive_processing(
    *, worker_id: str, lease_duration: timedelta, classification_run_id: int
) -> SourceInstance | None:
    WHERE classification_run_id = :classification_run_id
      AND member_path IS NULL AND (root_t7_path archive-suffixed)
      AND content_identity_group_id IS NULL AND NOT already_processed
      AND (claimed_by IS NULL OR claimed_at < stale_before)
```
Both simply gain `classification_run_id = :classification_run_id` as
one more `AND` term — the row-lock/claim mechanism itself is unchanged.

**How batch identity reaches the claim**: explicit parameter passing
only — the orchestrating worker loop holds its `IngestionBatch` (and
therefore `batch.classification_run_id`) as its own state and passes it
into every `WorkerClaimService` call it makes, exactly matching this
codebase's existing style (every service here takes explicit keyword
arguments; nothing uses global or thread-local implicit context).

**Adversarial concurrency test, required**: Batch A and Batch B (two
real `IngestionBatch` rows, each with its own dedicated
`ClassificationRun` and disjoint `SourceInstance` sets) run
concurrently against the same worker infrastructure, using
`threading.Barrier`-synchronized real-Postgres workers (this project's
established, non-negotiable concurrency-test standard — never mocked).
Assert, over many interleaved claim attempts, that **every claimed
row's `classification_run_id` exactly matches the `classification_run_id`
argument used to claim it** — zero cross-batch claims, proven, not
merely argued. Added to the test matrix (point 16).

**A genuinely tricky case, decided explicitly rather than left open —
and clarified further per review (see also point 6 below)**:
`claim_content_identity_group` claims a `ContentIdentityGroup`, which
has no direct `classification_run_id` — and, per the already-proven
duplicate-convergence behavior, a single group can legitimately be
pointed at by `SourceInstance` rows from *different* batches. This does
**not** conflict with the frozen invariant above, because it is a
different claim entirely: the invariant governs `SourceInstance`-level
claims specifically (the ones that represent "this batch's selected
work" and are the only claims that ever read T7 bytes); `claim_content_
identity_group` operates on already-resolved, purely-database-internal
content identity, a later pipeline stage that never touches T7 again.
**Decision, unchanged from Round 1**: `claim_content_identity_group`
remains a *global* claim (no batch filter) — whichever batch's worker
claims an eligible shared group first legitimately advances it, and
`embeddings_reserved` is charged to *that* batch. This is a known,
accepted imprecision in cross-batch budget attribution (the underlying
work itself is still correctly deduplicated and only processed once;
only which batch's ledger records the cost is approximate in this rare
case), not a correctness bug, and not a violation of batch isolation —
named here rather than silently assumed.

**Three distinct concepts, stated explicitly so they are never
conflated**:
```
batch membership              - which SourceInstance rows were selected/
                                 materialized under THIS batch's ClassificationRun.
                                 Structural, immutable, per-batch (points 1/3).
content identity convergence  - which ContentIdentityGroup a SourceInstance's
                                 bytes resolve to. A property of BYTES, global,
                                 and MAY legitimately span multiple batches.
worker ownership               - which specific claim (claimed_by/claimed_at)
                                 currently owns a row for active processing.
                                 Transient, per-attempt.
```
Different batches may legitimately contain `SourceInstance`s that later
converge on the same `ContentIdentityGroup` — this is not an error, it
is the same duplicate-content-reuse behavior already proven twice on
real data. What must never happen, and is what the frozen predicate
above actually prevents: **identity convergence must never allow a
worker to claim a `SourceInstance` belonging to another batch.**
`SourceInstance`-level claims stay strictly batch-scoped, full stop;
only the later, T7-independent `ContentIdentityGroup`-level claim
(a different claim, on a different kind of row, representing already-
resolved shared truth about bytes) is intentionally global — and that
distinction is precisely why the two are different methods with
different scoping rules, not an inconsistency between them.

- **Claim ownership recovery**: unchanged existing `claimed_at <
  stale_before` mechanism; now additionally scoped by
  `classification_run_id` for the two `SourceInstance`-level methods.
- **Batch pauses while an item is claimed**: the claim is left as-is;
  it naturally goes stale after its lease and becomes reclaimable on
  resume (by the same or a different worker) — no special "batch
  pause" handling needed at the claim level, this falls out of the
  existing lease mechanism.
- **Hard-stop cleanup**: on `ABORTED`, a worker releases any claim it
  currently holds immediately, rather than leaving it to expire
  naturally — avoiding an unnecessary wait through the full
  `lease_duration` before a future retry batch can pick the item up.
- **Avoiding false terminality**: `COMPLETED` is set only after a
  reconciliation query confirms zero claimable/in-progress work remains
  for this batch's rows (every `SourceInstance`/`ContentIdentityGroup`
  has reached a terminal state, or is provably unreachable within
  remaining envelope) — never merely because one claim attempt
  returned `None`, which could just mean a transient lack of
  currently-claimable work while other items are mid-flight elsewhere.

Existing execute-vs-recovery race semantics and domain-error
convergence (Chain 1's dedup-executor precedent) are unaffected —
Chain 2's claim discipline remains its own, separate lazy-reclaim
mechanism, not retrofitted with Chain 1's `recover_stale_execution`
pattern.

### 11. Archive processing integration

```
source admission (selection time, point 5)
    -> container-level SourceInstance created, source_category=ARCHIVE,
       workload_category=CONTAINER, risk_tier_estimated set from declared
       size (point 4) - all set AT CREATION, before any read
    -> archive source claim (claim_source_instance_for_archive_processing,
       now classification_run_id-scoped)
    -> pre-flight extracted-bytes reservation (point 7)
    -> isolated staging extraction (point 7)
    -> member SourceInstance creation (existing _extract_recursive loop,
       unchanged structurally) - EACH member gets source_category=
       LOOSE_FILE or ARCHIVE (if itself a nested archive), workload_category
       via classify_workload_category(member.relative_path), risk_tier_estimated
       from its own size - all set HERE, at member-creation time, since this
       is the first point a member's own path/extension is known
    -> member policy (point 4) - evaluated per member, same classifiers as
       loose files
    -> identity resolution proceeds exactly as today (unchanged) for
       eligible members
```
**Explicit non-negotiable**: archives are never opened during initial
source selection (point 5) — `source_category=ARCHIVE`/
`workload_category=CONTAINER`/`risk_tier_estimated` for the container
are set from D0's declared metadata alone; only extraction (a later,
execution-time step, per a claimed batch) ever reads archive bytes.

### 12. Batch state machine — exact transitions

```
PLANNED   --(first claim-cycle begins)-->  RUNNING
RUNNING   --(soft-stop OR manual pause)-->  PAUSED           [stop_reason set]
RUNNING   --(hard-stop OR persistent Ollama failure
             OR safety-invariant violation)-->  ABORTED      [stop_reason set, TERMINAL]
RUNNING   --(reconciliation confirms zero remaining
             claimable/in-progress work)-->  COMPLETED       [stop_reason =
             SOURCE_WORK_EXHAUSTED / *_ENVELOPE_EXHAUSTED / RUNTIME_BUDGET_EXCEEDED]
PAUSED    --(operator resumes)-->  RUNNING
PAUSED    --(operator aborts explicitly)-->  ABORTED
```
- **`PLANNED`**: the batch exists (created, immutable membership fixed)
  but no worker has yet begun a claim cycle against it.
- **`RUNNING`**: actively being processed.
- **`PAUSED`**: resumable — same batch, same `IngestionBatch` row,
  membership and all counters preserved exactly as they stood.
- **`COMPLETED`**: reached when envelope exhaustion or genuine work
  exhaustion is the reason execution stopped — a *designed*, successful
  end to this batch's scope, not a failure. Terminal.
- **`ABORTED`**: reached only via a genuine resource-guard hard-stop or
  safety violation — an *unplanned* interruption. **Terminal — retrying
  requires a brand-new `IngestionBatch` (new `ClassificationRun`, new
  selection, new fingerprint)**, never resuming an aborted one, per the
  frozen architecture.
- **Human review points**: `review_required=True` (set at creation) is
  advisory and does not gate any transition by itself; a `stop_reason`
  in the "review-required" category (per point 9 of the numeric pass:
  first scaled batch of a class, unusually large archive encountered, a
  new failure-code class) is a *process* signal for the human overseeing
  the batch, not a code-enforced gate — consistent with how every other
  gate in this project has worked.

### 13. `BatchReportService` — design specification

Computes, for a given `IngestionBatch`, all fields the frozen
architecture requires:
```
eligible_source_count, policy_filtered_count, selectable_count   <- read directly
                                                    from the immutable IngestionBatch row
source_instances_selected, source_bytes_selected   <- read directly (immutable)
unattempted_selected_count  = source_instances_selected - attempted_source_count
attempted_source_count      = COUNT(DISTINCT SourceInstance) under this batch's
                               classification_run_id with >=1 IngestionAttempt
terminal_source_count       = COUNT of those whose resolved ContentIdentityGroup.pipeline_state
                               IN (INGESTED, EXCLUDED, UNSUPPORTED, FAILED, NEEDS_REVIEW)
successful_ingestion_count  = COUNT with pipeline_state == INGESTED specifically
actual_source_bytes_read    = SUM of real bytes read at identity-resolution/extraction
                               time (from evidence_snapshot, not D0's declared estimate)
extracted_bytes_consumed, embeddings_reserved, runtime_consumed
                             <- read directly from IngestionBatch's counters
risk_tier_estimated vs risk_tier_actual distribution
                             <- GROUP BY on SourceInstance, including the NULL
                               ("insufficient evidence") bucket explicitly
drift outcomes               <- classified per SourceInstance at read time
                               (OBSERVED_AT_SELECTION / SOURCE_PRESENT_AT_EXECUTION /
                               SOURCE_CHANGED_AFTER_SELECTION / SOURCE_MISSING_AT_EXECUTION)
stop_reason, stop_reason_detail   <- read directly
```
**Denominator reconciliation rule**: `source_instances_selected ==
attempted_source_count + unattempted_selected_count` must hold exactly
at all times (a query-level invariant to assert in the report, not
merely hope for) — any discrepancy indicates a bug (e.g. a claim
released without either completing or remaining cleanly unattempted).
`terminal_source_count <= attempted_source_count <=
source_instances_selected` must also hold. No implementation of this
service is written in this pass.

### 14. Failure / retry semantics — batch-level vs. item-level

| Condition | Level | Retryable? | Terminal for batch? |
|---|---|---|---|
| Envelope exceeded (extracted bytes / embeddings) for one item | item (deferred, no attempt recorded) | item eligible again in a *future* batch | no — batch continues with other work |
| Resource soft stop | batch | batch resumable (`PAUSED`) | no |
| Resource hard stop | batch | no — new batch required | yes (`ABORTED`) |
| T7 unavailable | item (`IngestionAttempt` FAILED, existing) | yes, retryable | no |
| Corrupt input | item (`IngestionAttempt` FAILED, existing) | yes (indefinitely, by construction) | no |
| Unsupported content | item (`EXCLUDED`/`UNSUPPORTED`, no attempt) | n/a — durable terminal state | no |
| Embedding unavailable | item (`IngestionAttempt` FAILED, existing) | yes | no |
| Worker crash | item (claim goes stale, existing) | yes, via existing reclaim | no |
| Stale claim recovery | item (existing mechanism) | n/a — recovery IS the retry path | no |
| Database transaction failure | batch-creation-time: whole creation rolls back; mid-batch: item-level, existing session/transaction handling | yes, depends on where it occurred | no, unless it repeats and trips a resource guard |

No new semantics conflict with the existing `IngestionAttempt` model —
every item-level row above uses outcomes/failure codes already frozen
in `ce50875`; this design only adds the *batch*-level layer on top,
which the existing model never had a concept of before.

### 15. Concurrency / transaction design — where read-then-act is unsafe

| Operation | Unsafe pattern | Required atomic operation |
|---|---|---|
| Batch selection ownership | reading eligibility then materializing without a lock | `pg_advisory_xact_lock` for the whole creation transaction (point 2) |
| Embedding reservation | check `embeddings_reserved < max` then `UPDATE` | single conditional `UPDATE ... WHERE ... <= max_embeddings RETURNING` (point 8) |
| Extracted-bytes reservation | same shape as embeddings | same conditional `UPDATE` pattern (point 7) |
| Claim ownership | already solved (`SELECT ... FOR UPDATE SKIP LOCKED` + `UPDATE`, existing) | unchanged, extended with `classification_run_id` filter |
| Stale claim recovery | already solved (same claim query reclaims, by construction) | unchanged |
| Batch terminalization | checking "no claimable work" without confirming no in-progress work elsewhere | reconciliation query (point 10), not a single claim-attempt result |

**DB constraints vs. service-level invariants**: the `UNIQUE` constraint
on `IngestionBatch.classification_run_id` is a DB-enforced guarantee
(cannot be violated even by a bug). Envelope immutability, the
`selection_fingerprint`'s correctness, and the funnel-count semantics
are service-level invariants (enforced by narrow method design, not a
DB constraint) — worth naming as a real limitation: a sufficiently
wrong direct-SQL write could still violate them; the design relies on
disciplined service boundaries, matching every other write-once field
in this codebase (e.g. `SourceInstance.content_identity_group_id`'s
"write-once" nature is also service-enforced, not DB-enforced, and
that has held up through five real-data milestones already).

### 16. Test design — the matrix implementation must satisfy

All synthetic unless a future gate explicitly authorizes a real-T7
test — matching every prior milestone's discipline:
```
- deterministic selection reproduces identical results for identical inputs
- immutable membership: re-running "selection" logic against an existing
  batch's ClassificationRun never adds/removes rows
- a later DiscoveryRun creates a genuinely new SourceInstance for a path
  already observed under an earlier DiscoveryRun's batch
- envelope boundaries: exactly the item that would exceed a limit stops
  selection (not skipped-and-continued)
- concurrent embedding reservations cannot together exceed max_embeddings
  (real Postgres concurrency test, this project's non-negotiable standard)
- archive staging cleanup: an aborted/overflowing extraction leaves zero
  files outside the deleted staging directory
- resource soft stop -> PAUSED -> resume continues without reselection
- resource hard stop -> ABORTED -> retry requires a new IngestionBatch
- review_required is set correctly at creation, never blocks execution by itself
- pause/resume: monotonic runtime accumulates correctly across a
  simulated process restart
- crash/recovery: a stale claim (including an orphaned embedding
  reservation) is correctly reclaimed and released, and reclaiming it
  concurrently from two workers never double-releases the reservation
  (point 8's idempotency proof, exercised for real)
- **adversarial cross-batch claim isolation (required, point 10)**:
  Batch A and Batch B, disjoint `SourceInstance` sets, run concurrently
  via `threading.Barrier`-synchronized real-Postgres workers; assert
  every claimed row's `classification_run_id` matches the id used to
  claim it, over many interleaved attempts
- **adversarial late-worker reservation safety (required, point 8,
  real Postgres, not mocked)**:
  ```
  1. Worker A claims a ContentIdentityGroup at generation N and reserves
     embedding capacity for N.
  2. A's claim goes stale (simulate via an already-expired claimed_at,
     the existing test pattern for stale-claim scenarios).
  3. Recovery reclaims generation N: releases N's reservation, advances
     to generation N+1.
  4. Worker B claims at generation N+1 and reserves NEW capacity.
  5. Worker A (unaware of any of the above) now executes its own,
     remembered generation-N release operation.
  Assert:
    - A's release matches zero rows (fenced out by claim_generation)
    - B's reservation is untouched and intact
    - ingestion_batches.embeddings_reserved reflects ONLY B's reservation
      (N's was already correctly released at step 3, exactly once)
    - B's subsequent successful embed()/consume completes with no leaked
      or double-counted capacity
  ```
  Also test two concurrent recovery attempts for the SAME stale
  generation racing each other — assert exactly one succeeds in
  releasing/advancing, the other's `UPDATE` matches zero rows, and the
  batch counter is decremented exactly once, not twice.
- batch/item state independence: a PAUSED batch can coexist with a
  FAILED, unrelated ContentIdentityGroup
- archive admission never has access to member workload data (a
  regression test analogous to the identity-resolution/archive-
  exclusion tests already in this codebase)
- D1 duplicate-archive scheduling admits exactly one representative per
  group, deterministically
- all three drift outcomes (present-unchanged / changed / missing)
  produce their frozen, distinct dispositions
- report denominator reconciliation holds across every scenario above
- no test in this suite references any real T7 path, ever
```

### 17. Security / safety boundary — implementation-level invariants

- **No write path to T7 exists anywhere in this design** — every new
  component (selection, `BatchResourceGuard`, `IngestionBatch`
  lifecycle) only ever reads D0's JSON file and already-committed
  database rows; extraction writes exclusively under
  `workspace_root`/`_staging`, never under `/media/personal/Seenu_T7SSD*`.
- **Accidental extraction into the source corpus is prevented
  structurally**: `destination` is always computed from
  `WORKSPACE_ROOT`, never derived from `SourceInstance.root_t7_path`'s
  own directory — the existing `ArchiveProcessingService` already does
  this; the new staging layer adds a subdirectory under the same
  workspace root, never a new root.
- **No mutation of source files**: unchanged from every prior
  milestone — nothing in this design adds a write/rename/delete
  capability against a source path.
- **No accidental dedup deletion/quarantine**: `DedupFilesystemExecutor`
  and dedup execution/authorization are never invoked anywhere in this
  design — the D1-duplicate-archive scheduling rule (point 4) only
  affects which archive is *extracted first*, never anything about
  Chain 1's dedup disposition machinery.
- **`ImportJob.source_path` boundary preserved**: `FileAccessService`'s
  existing allow-list (`_FILESYSTEM_BACKED_SOURCE_TYPES = {"filesystem"}`,
  verified directly) is untouched by this design; nothing here creates
  an `ImportJob` referencing a classification-derived T7 path, and the
  existing hardening (a non-`"filesystem"` `source_type` can never
  become a readable root) remains the only path by which a T7-adjacent
  reference could ever reach `FileAccessService`.
- **Policy changes cannot silently alter an existing batch**: per point
  1's immutability rule, an `IngestionBatch`'s envelope, selection
  policy version, and materialized membership are fixed permanently at
  creation — a later change to `classify_workload_category` or a batch
  class's predicate affects only *future* batch creations, never a
  batch already `PLANNED`/`RUNNING`/`PAUSED`.

### 18. Implementation order — independently reviewable milestones

```
1. Schema/model design review (IngestionBatch + 4 new SourceInstance columns) -
   NOT the migration itself, a focused review of the exact DDL
2. Migration (schema only, applied to aibrain_test first, per this project's
   established pattern)
3. Batch creation service (selection algorithm + advisory-lock transaction)
4. Policy evaluator (classify_source_category/workload_category/risk_tier_estimated,
   the D1-duplicate-representative rule)
5. WorkerClaimService extension (classification_run_id-scoped claim methods)
6. BatchResourceGuard (disk/Postgres/Ollama checks, three-tier stop logic)
7. Extracted-bytes + embedding reservation (atomic counters, staging integration)
8. Runtime accounting (monotonic + durable accumulation)
9. Worker/claim integration (the loop that ties 3-8 together per batch)
10. Archive-processing integration (staging, member classification at creation)
11. BatchReportService (reporting, denominator reconciliation)
12. Adversarial/concurrency tests (real Postgres, per this project's
    non-negotiable standard - the LAST milestone, proving everything above,
    not a substitute for testing incrementally within each prior milestone)
```
Each milestone is independently reviewable and, per this project's
established discipline, independently authorized and committed — not
assumed to land in one commit.

### 19. Design gap register

**DECIDED / FROZEN** (this pass, on top of `f2b9815`/`3d37ec0` —
Round 2 corrections included, not left as open questions):
```
IngestionBatch exact schema (point 1), including review_required and the
three selection-time funnel counts.

Advisory-lock batch-creation transaction, with EXACT scope (per DiscoveryRun),
key derivation (namespaced string hash, never a raw id), transaction-scoped
lifetime (pg_advisory_xact_lock, auto-released on commit/rollback), and the
explicit pairing with a recommended DB UNIQUE constraint as independent
defense-in-depth - the lock is not a substitute for a constraint (point 2).

classification_run_id required on SourceInstance-level claims, with the
EXACT predicate for both claim methods and the mechanism (explicit parameter
passing) by which batch identity reaches them (point 10) - required, not
optional hardening.

The embedding-reservation FENCED crash-recovery lifecycle IN FULL: two new
ContentIdentityGroup columns (reserved_embeddings, claim_generation), every
reserve/consume/release/recover operation checking BOTH reservation presence
AND generation ownership, and the proof that a delayed (not merely crashed)
worker from an old generation can never touch a newer generation's
reservation, nor can concurrent recovery double-release (point 8) - this is a
CLOSED design, not a named-but-unsolved gap. (A presence-only version of this
was proposed in Round 2 and correctly identified as insufficient against a
zombie-worker race in Round 3 - the fencing token above is the fix, not a
patch on top of the old design.)

classify_workload_category strips conflict-rename suffixes for real (point 4,
closing the numeric pass's open question).

Selection stops (never skips-and-continues) at the first envelope-exceeding
item, explicitly tied to selection_fingerprint reproducibility (point 5).

The three-way declared/actual/peak distinction for extracted-bytes accounting,
including the post-extraction reconciliation step - declared size is never
treated as proof of actual bytes written (point 7).

claim_content_identity_group stays global; cross-batch convergence charges
whichever batch claims first - and is explicitly NOT a batch-isolation
violation, since SourceInstance-level claims (the ones that touch T7) remain
strictly batch-scoped regardless (point 10, with the three-concept
membership/convergence/ownership clarification).

COMPLETED vs PAUSED vs ABORTED mapping by stop_reason category (point 12);
denominator reconciliation invariant (point 13).
```

**REQUIRES IMPLEMENTATION-TIME VERIFICATION** (genuinely open questions
only - the two Round-1 gaps above are no longer listed here, per
review, since they are now fully decided):
```
Exact SQLAlchemy column types/DDL syntax for the new IngestionBatch table,
the new SourceInstance columns, and ContentIdentityGroup.reserved_embeddings
(this pass specifies semantics, not DDL syntax).

The exact DDL for the recommended SourceInstance UNIQUE constraint
(classification_run_id, root_t7_path, member_path) - whether to add it in
the same migration as IngestionBatch or as a follow-up hardening change.

The exact reconciliation query shape for point 10/13's "zero claimable/
in-progress work remains" check (logically specified, not yet written as SQL).

Whether a periodic runtime heartbeat is worth its complexity (recommended,
not required, point 9).
```

**DELIBERATELY DEFERRED** (unchanged from earlier gates, not
reinterpreted here):
```
Numeric envelope values for Class 2-5 (archive classes); max_embeddings
for Class 1 itself (CALIBRATION_REQUIRED, per 3d37ec0); risk_tier_actual
numeric boundaries; the backup/sync/snapshot context signal's structure
and pattern list; Ollama embedding-latency ceiling; any actual
implementation code, schema migration, or real-T7 access - all remain
gated behind their own, separate future authorizations.
```

**This freeze approves the implementation design in full — it
authorizes no code, no schema/migration, and no real-T7 access.**
**Next gate, not yet authorized**: implementation itself, beginning
with the first independently reviewable milestone from point 18's
sequence (schema/model design review) rather than jumping directly to
real-T7 execution — each subsequent milestone in that sequence remains
its own, separately authorized step.

## Scaled Real-T7 Ingestion — Milestone 5 Design: Archive Processing / Extraction (DESIGN ONLY — no code, no migration, no real-T7 access authorized by this section)

Design pass for Implementation Milestone 5, opened after Milestones
1-4 (`cc8dbec`/`4709ffd`/`ca3ab4e`/`6513948`) were committed and
reviewed separately. This section freezes the archive-processing
lifecycle, staging/workspace contract, extraction-envelope
reconciliation, crash/resume semantics, nested-archive rules, and
failure taxonomy that a later, separately-authorized implementation
pass must be reviewed against. **No implementation code, migration, or
real-T7 access is authorized by this section** — see "Implementation
acceptance criteria" at the end.

**Design Correction Pass applied**: the initial pass correctly
identified `SourceInstance`'s lack of claim-generation fencing as an
open gap but proposed resolving it with either a schema addition OR a
conservative lease as alternatives; review rejected the lease as a
substitute correctness mechanism and required the fencing design
itself. Sections marked "CORRECTED"/"RESOLVED"/"UPDATED" below reflect
that correction round, applied directly to this same document rather
than left as a separate patch, since this design has not yet been
frozen or committed. Section 21 re-reviews all fifteen mandatory
invariants against the corrected design; section 19 gives the final
disposition of all eight original gaps.

### 0. What already exists — read before designing anything new

This is not a green-field design. A real, already-committed,
already-tested archive pipeline exists from the earlier "Controlled T7
-> AI_Brain Ingestion Design" chain (`6491dad`, `ce50875`, `4e6f405`),
built and proven **before** the Scaled Real-T7 Ingestion batch system
existed. Milestone 5's job is to make THIS existing pipeline
batch-aware and envelope-honest — exactly the same shape of task
Milestone 4 already completed for `WorkerClaimService` — never to
redesign archive extraction from scratch. Files inspected in full for
this pass:

```
app/ingestion/archive.py                        - ArchiveExtractor (the atomic extractall() wrapper)
app/classification/archive_processing_service.py - ArchiveProcessingService (the recursive walker)
app/models/provenance_link.py                    - ProvenanceLink (the ancestry chain)
app/classification/source_instance_service.py    - SourceInstanceService.create_instance
app/classification/content_identity_service.py   - ContentIdentityService (get_or_create_group / assign_content_identity)
app/classification/workspace.py                  - write_workspace_content / find_existing_workspace_content
app/classification/eligibility_service.py        - classify_eligibility (EXCLUDED/UNSUPPORTED/ELIGIBLE)
app/models/ingestion_attempt.py                  - IngestionFailureCode vocabulary
app/classification/worker_claim_service.py       - claim_source_instance_for_archive_processing (Milestone 4)
app/models/source_instance.py, ingestion_batch.py, content_identity_group.py - already-frozen schema
```

**Exact existing mechanics this design builds on, unchanged unless
stated otherwise:**

- `ArchiveExtractor.extract(files, destination)` wraps `ZipFile.
  extractall()`/`py7zr.SevenZipFile.extractall()` — each is confirmed,
  by reading the code directly, to be a single atomic call per format:
  no per-file interruption point exists between "extraction started"
  and "extraction finished," matching the design's own explicit
  instruction to treat this as ground truth, not an assumption.
  Existing safety checks, unmodified: path-traversal rejection
  (`target.resolve().is_relative_to(destination)` per member), a hard
  expansion-ratio bomb guard (1000x, `HARD_EXPANSION_RATIO`), a hard
  workspace-free-space reserve check (10GB, `HARD_FREE_SPACE_BYTES`,
  computed from the archive's own declared member sizes), and (7z
  only) explicit symlink-member rejection.
- `ArchiveProcessingService._extract_recursive` already recursively
  walks nested archives (`max_depth=10`), already builds the full
  `ProvenanceLink` ancestry chain per member via `SourceInstanceService
  .create_instance(..., chain=full_chain)`, already resolves each leaf
  member's content identity via `ContentIdentityService.
  get_or_create_group`/`assign_content_identity`, and already achieves
  crash-resumability through **idempotency**, not transactional
  wrapping: `_find_existing_member` checks for an existing
  `SourceInstance` (keyed on `classification_run_id, root_t7_path,
  member_path`) before creating one, so a re-run after a crash
  completes or skips each member exactly once, never duplicating a row.
- `claim_source_instance_for_archive_processing` already gained an
  optional `classification_run_id` parameter in Milestone 4 (batch
  scoping + RUNNING-only admission, re-checked in the claim's own
  `UPDATE`) — but `ArchiveProcessingService.process_next_archive`
  **does not yet pass it**. Wiring this one call site is Milestone 5's
  first, smallest task.

**What does NOT yet exist and is this milestone's actual scope**:
batch-envelope integration (`max_extracted_bytes`/
`extracted_bytes_consumed`), the frozen staging/promotion model from
the numeric pass (the current code extracts directly into
`workspace_root/archive_<id>/depth_<n>_<stem>`, not into an isolated,
cleaned-up `_staging/` tree), member-level classification
(`source_category`/`workload_category`/`risk_tier_estimated`, required
by "### 11. Archive processing integration" above but not present in
`_extract_recursive` today), and `SourceInstance.risk_tier_actual`
(named in "### 5. Risk tier" above as this milestone's own future
column — does not exist in the schema yet).

### 1. Lifecycle / state model

```
IngestionBatch RUNNING
    |
    v
claim_source_instance_for_archive_processing(classification_run_id=batch's run)
    -- gains the classification_run_id argument this milestone adds to
       the actual call site; RUNNING-admission already enforced inside
       the claim's own UPDATE (Milestone 4, unchanged)
    |
    v
[idempotent staging cleanup]  -- delete any pre-existing staging_root
                                  for this SourceInstance's id (crash-
                                  orphan cleanup, see section 6)
    |
    v
PRE-FLIGHT: extracted-bytes envelope admission (section 6)
    -- conditional UPDATE on IngestionBatch.extracted_bytes_consumed
       using the ROOT archive's own declared (D0) size as the estimate
    -- FAILS -> staging deleted (nothing written yet), claim released,
       NO IngestionAttempt recorded (deferred, not failed - matches
       the frozen numeric pass's EXTRACTED_BYTES_ENVELOPE_EXCEEDED
       disposition exactly)
    |
    v  (admitted)
RECURSIVE EXTRACTION  (existing _extract_recursive, unchanged shape)
    for each level (root, then every nested archive found):
        extractor.extract([this level's file], this level's staging dir)
        for each member:
            find-or-create SourceInstance + ProvenanceLink (idempotent)
            classify member (source_category/workload_category/
              risk_tier_estimated) - NEW this milestone, at creation time
            if member is itself ARCHIVE: recurse (depth+1)
            else: resolve content identity (existing mechanism, unchanged)
    -- raises at any point -> caught by the OUTER _process_claimed_archive
       try/except (existing shape, unchanged) -> classified via
       _classify_extraction_failure -> durable FAILED IngestionAttempt
       on the ROOT SourceInstance
    |
    v
POST-EXTRACTION RECONCILIATION (section 6)
    -- sum every real file's measured size across every recursion level
       actually reached; reconcile IngestionBatch.extracted_bytes_consumed
       from the declared estimate to the measured total (delta can be
       +/-/0, never re-litigates the pre-flight admission decision)
    |
    v
risk_tier_actual set on the ROOT SourceInstance from real evidence
(member count, max depth reached, measured expansion ratio) - NULL if
extraction failed before this evidence existed (section 5, unchanged
from the frozen numeric pass)
    |
    v
[unconditional staging cleanup]  -- delete staging_root regardless of
                                     success or failure (section 6)
    |
    v
durable IngestionAttempt (SUCCEEDED or FAILED) recorded on the ROOT
SourceInstance  (existing mechanism, unchanged)
    |
    v
release_source_instance_claim(instance.id)  (existing `finally` block,
                                              unchanged - see the
                                              fencing gap in section 17
                                              below, which this pass
                                              does NOT close)
```

**Terminal vs. recoverable states, stated explicitly** (mandatory
invariant: "stop means STOP, never skip-and-continue" applies to
*admission* decisions, never to an already-granted, atomic
`extractall()` call already in flight — see section 16):

- **Recoverable**: claim staleness (`claimed_at < stale_before` on the
  ROOT `SourceInstance`), envelope-exhausted deferral (no attempt
  recorded — the item is simply not yet processed, exactly like a
  batch-selection-time deferral), any `FAILED` `IngestionAttempt`
  marked `retryable=True` (a fresh claim attempt, same or later batch,
  starts over from the idempotent-resume state).
- **Terminal** (for THIS `ClassificationRun`'s archive claim): the
  ROOT archive's `IngestionAttempt` outcome is `SUCCEEDED` — no further
  claim of this root `SourceInstance` for archive processing is ever
  needed again (matches the existing "already processed" exclusion via
  `IngestionAttempt.outcome == SUCCEEDED` in the claim query,
  unchanged). A `FAILED` attempt with `retryable=False` (none of the
  current `_classify_extraction_failure` mappings set this today — all
  existing archive failures are recorded `retryable=True` in the
  current code) would also be terminal if introduced later; this
  design does not change that default.

### 2. Claim and generation rules — CORRECTED (Design Correction Pass)

**Original position, superseded**: this section originally stated that
`SourceInstance` has no `claim_generation` column and that archive
claim safety rests on `claimed_by`/`claimed_at` staleness alone,
recording the consequence as an accepted, open gap (former section 17).
**Review correctly rejected this as a blocking gap, not an acceptable
residual risk** — a shorter lease narrows the race window but does not
remove the underlying correctness dependency on unconditional claim
release. This section now freezes the closing design in full,
generalizing Milestone 4's `ContentIdentityGroup.claim_generation`
pattern to `SourceInstance`.

**Durable generation field**:
```
SourceInstance.claim_generation: Integer, NOT NULL, default 0, server_default '0'
```
Identical shape to `ContentIdentityGroup.claim_generation` (Milestone
1) — no new concept, the same fencing token generalized to a second
model. A migration adding this column is required before Milestone 5
implementation proceeds (out of scope for this design-only pass, per
its strict boundaries — see the gap register's final disposition).

**Claim acquisition** — both `claim_source_instance_for_identity_
resolution` and `claim_source_instance_for_archive_processing` gain the
fencing uniformly (the column lives on the model, not on one use case;
identity-resolution claims share the exact same "large file, long
hash time, plausible zombie window" hazard archive processing does,
so fencing only the archive path would leave an inconsistent,
partially-hardened model):
```
UPDATE source_instances
SET claimed_by = :worker_id,
    claimed_at = now(),
    claim_generation = claim_generation + 1
WHERE id = :candidate_id
  AND (claimed_by IS NULL OR claimed_at < :stale_before)
  [AND classification_run_id = :run_id
   AND EXISTS (running-batch check)]   -- Milestone 4 additions, unchanged, composed unmodified
RETURNING claim_generation
-- the caller retains this returned value as :my_generation for the
-- lifetime of its attempt, exactly matching ContentIdentityGroup's
-- existing contract
```
This is the SAME statement Milestone 4 already added the batch-scoping
`AND` terms to (section 2's admission/RUNNING-requirement bullets,
retained below) — the generation increment is one more value in the
same `SET` clause, not a separate statement, preserving the existing
single-atomic-UPDATE claim shape exactly.

**Release fencing** — `release_source_instance_claim` gains a REQUIRED
`claim_generation` parameter, mirroring `release_content_identity_
group_claim`'s Milestone-4 hardening exactly:
```
UPDATE source_instances
SET claimed_by = NULL, claimed_at = NULL
WHERE id = :instance_id AND claim_generation = :my_generation
RETURNING id
-- applied = a row was returned; False = safe no-op (stale generation),
-- never a raised exception, never a wrongful clear of a newer owner's claim
```
Both existing call sites (`identity_resolution_service.py`'s `finally`
block, `archive_processing_service.py`'s `finally` block) must pass
`claim_generation=instance.claim_generation` — the exact same
mechanical, zero-behavior-change-for-the-honest-case update Milestone
4 already made for every `release_content_identity_group_claim` call
site. This is an implementation-time call-site change; the CONTRACT is
frozen here.

**Stale-claim recovery** — no separate recovery service, exactly as
already established for `ContentIdentityGroup`: the SAME claim query
above that grants a fresh claim also reclaims a stale one, incrementing
`claim_generation` in the identical statement. Recovery is authorized
because it reads the row fresh under the claim's own `WHERE`
re-evaluation at `UPDATE`-execution time (Postgres READ COMMITTED,
unchanged reasoning from every other fenced claim in this codebase) —
never because of a special-cased caller identity.

**Zombie-worker behavior, the exact scenario from the review now
closed**: worker A claims (generation N); A is delayed, not crashed;
recovery reclaims to generation N+1 for worker B; A eventually finishes
and calls `release_source_instance_claim(instance.id, claim_generation
=N)` — this now matches ZERO rows (the row is at generation N+1), so
A's release is a safe no-op. B's `claimed_by`/`claimed_at` are
untouched. A third worker C cannot be admitted into B's in-progress
work via A's stale release, because A's release no longer has any
effect on the row at all. The three-way-concurrent-write scenario
section 17 originally described as a real, load-bearing consequence of
the unfenced release is now structurally prevented — not merely made
less likely by a shorter lease.

**Admission** (unchanged from the original pass): `claim_source_
instance_for_archive_processing` must be called with `classification_
run_id` set to the claiming worker's batch's `ClassificationRun.id` —
the smallest concrete code change this section requires at the call
site (`ArchiveProcessingService.process_next_archive`). `None` (the
pre-Milestone-4 default) must never be used for a batch-driven archive
worker.

**Batch-RUNNING requirement** (unchanged): enforced entirely inside the
claim's own `UPDATE` (Milestone 4, unchanged) — no separate check is
added or needed in `ArchiveProcessingService` itself.

**Lease policy — frozen mechanism, per-class calibration explicitly
tied to an existing gap, never invented here** (closes review point 7):
staleness remains exactly `claimed_at < now() - lease_duration` (the
existing formula, unchanged) — `lease_duration` remains an explicit
parameter to `process_next_archive` (already its shape today), never a
hardcoded constant inside `ArchiveProcessingService`. The existing
default, `timedelta(minutes=10)` — the same default every other claim
method in this codebase already uses — remains the correct value ONLY
for the smallest extractable archive class (Class 2, "SMALL <10MB" per
the frozen numeric pass's policy table). For Classes 3-5 (MEDIUM/
LARGE/EXTREME), a materially larger `lease_duration` is required
before real-T7 execution — this is **not a new, separately-invented
gap**: it is the SAME `CALIBRATION_REQUIRED`/`TBD at gate` numeric
envelope the frozen numeric pass already named for those classes'
`max_runtime`/`max_extracted_bytes`/`max_embeddings` fields, now
explicitly recognized as covering claim `lease_duration` too, governed
by the same future calibration gate — never a number fabricated by
this design pass.

### 3. Staging/workspace contract

**Frozen naming, reconciling the numeric pass's `_staging/
<source_instance_id>/` model with the recursion the current code
already performs:**

```
staging_root(root_source_instance_id) =
    workspace_root / "_staging" / f"archive_{root_source_instance_id}"

nested_staging_dir(root_source_instance_id, full_member_path) =
    staging_root(root_source_instance_id) / sha256(full_member_path).hexdigest()[:16]
```

- **Per-source isolation**: every staging path is namespaced under the
  ROOT archive's own durable `SourceInstance.id` — stable across
  retries/resumes (same DB row, reclaimed), never derived from a
  worker id, timestamp, or attempt counter.
- **Deterministic naming, collision-safe**: the current code's
  `depth_{depth}_{archive_path.stem}` naming can collide (two sibling
  nested archives sharing a stem at the same depth); this design
  replaces it with a **stable hash of the member's own full internal
  path** (`full_member_path`, already tracked by the existing
  recursion) — deterministic across resumes, collision-free by
  construction, and avoids passing an attacker/data-controlled string
  (an archive member's own name) directly into a real filesystem path
  component.
- **No writes anywhere under the source corpus**: `staging_root` is
  always rooted under `workspace_root` (an `AI_Brain`-owned directory,
  never a T7 path) — `ArchiveExtractor.extract()` itself never receives
  a T7 path as its `destination` argument, and nothing in this design
  changes that.
- **"Promotion," precisely defined** (refining, not contradicting, the
  numeric pass's "staging -> promoted" phrasing, which was written
  before the recursive, per-member-incremental persistence model was
  fully elaborated): there is no single, all-or-nothing filesystem
  "promotion" step. Each ELIGIBLE leaf member's raw bytes are copied to
  their **permanent, content-identity-addressed** location
  (`workspace_content_path(workspace_root, group_id, suffix)` — the
  same, already-established convention loose files already use) at the
  moment that member's identity is resolved, exactly as the existing
  code already does via `write_workspace_content`. The **staging tree
  itself** (the raw, as-extracted archive structure) is always
  transient: it is never a place `Document`/`DocumentChunk` content is
  read from later, and it is deleted unconditionally at the end of
  every claim attempt (section 6) — "promotion" means "durable content
  already lives at its permanent, content-addressed home before
  staging is discarded," never "the staging directory becomes the
  permanent home."

### 4. Extraction safety — pre-flight algorithm

**Pre-flight envelope calculation, exactly once per root claim attempt
(not recomputed per nested level):**

```
1. estimated_bytes = the ROOT archive SourceInstance's own D0-declared
   size (evidence_snapshot["d0_declared_size_bytes"], already captured
   at selection time per Milestone 2's BatchCreationService - never a
   fresh filesystem stat, since D0 evidence is what selection already
   committed to)
2. UPDATE ingestion_batches
   SET extracted_bytes_consumed = extracted_bytes_consumed + :estimated_bytes
   WHERE id = :batch_id
     AND max_extracted_bytes IS NOT NULL
     AND extracted_bytes_consumed + :estimated_bytes <= max_extracted_bytes
   RETURNING extracted_bytes_consumed
3. IF no row returned:
     - delete staging_root(root_instance_id) if it exists (idempotent,
       matches crash-orphan cleanup - see section 6)
     - release_source_instance_claim(root_instance_id)
     - record NOTHING on IngestionAttempt (deferred, not failed)
     - set IngestionBatch.stop_reason = EXTRACTED_BYTES_ENVELOPE_EXHAUSTED
       via BatchControlService.complete()/pause() per the batch's own
       state-machine rules (Milestone 3, unchanged) - this design does
       not add a new stop reason, it reuses the already-frozen one
     - return (no extraction attempted)
4. IF max_extracted_bytes IS NULL (a batch class admitting no archives,
   or one that has deliberately opted out of the envelope check):
     - skip step 2/3 entirely; proceed directly to extraction. A NULL
       envelope is a real, meaningful "not applicable," never treated
       as "unlimited" by accident (matches IngestionBatch's own frozen
       column semantics).
```

**Declared vs. actual vs. peak staging usage — the three distinct
quantities, per the frozen numeric pass, restated precisely for the
recursive case:**

```
declared_member_bytes (per-level)  - ArchiveExtractor's OWN existing
    _validate_disk_space computation (sum of member.file_size across
    THAT level's central directory) - a per-level, workspace-disk-only
    check, already implemented, UNCHANGED by this design. Distinct
    from the BATCH-envelope estimate above, which uses the ROOT
    archive's own compressed size, not a recursive sum of every
    nested level's declared content (unknowable without opening every
    nested archive first - not attempted).
actual_durable_extracted_bytes (whole-claim-attempt total) - the sum,
    across EVERY recursion level actually reached, of every real
    extracted file's measured size (Path.stat(), exactly as
    _discover_extracted_files/_discover_extracted_7z_files already
    measure per level) - accumulated via a running total threaded
    through _extract_recursive's existing recursion, reconciled to
    IngestionBatch.extracted_bytes_consumed exactly ONCE, at the end
    of the whole claim attempt (not per nested level - see below).
peak staging usage - transient, workspace-disk-only, protected by
    ArchiveExtractor's own existing HARD_FREE_SPACE_BYTES check at
    EVERY level's extractall() call (unchanged) - never counted
    against extracted_bytes_consumed, matching the frozen numeric
    pass's "peak/transient staging usage... a live-disk-pressure
    concern... NOT by extracted_bytes_consumed at all."
```

### 4a. Archive-member safety — resolved via direct verification (Design Correction Pass)

The original pass left ZIP-symlink safety as an unverified stdlib
assumption and py7zr hardlink/special-file behavior as fully unknown.
Review required this be resolved before implementation, not carried
forward as belief. Both were investigated directly against the actual
installed libraries this codebase uses (`zipfile` stdlib, Python 3.12;
`py7zr` 1.1.3) — read-only introspection and a disposable, non-T7,
non-committed synthetic reproduction, never touching application code,
test code, or real archives.

**ZIP symlink-mode entries — CONFIRMED EMPIRICALLY, not merely
believed**: a ZIP entry crafted with `external_attr` marking it as a
symlink (`stat.S_IFLNK`) was extracted via `zipfile.ZipFile.
extractall()` on this project's actual Python 3.12 runtime. Result: a
plain REGULAR FILE was written, containing the intended symlink
TARGET STRING as its literal byte content — `zipfile.extractall()`
never recreates a real symlink from ANY entry, regardless of its
Unix-mode `external_attr` bits, because the stdlib implementation does
not interpret those bits at all for extraction purposes. This is now a
confirmed fact about this project's actual runtime, not an assumption
about "well-known stdlib behavior" — the exact reproduction (craft a
`ZipInfo` with `external_attr = (stat.S_IFLNK | 0o777) << 16`, write it
via `writestr`, extract, assert `stat.S_ISREG` on the result) is
specified here precisely so it becomes a real, committed test at
implementation time (see the corrected test matrix, section 18).

**py7zr `FileInfo` — read directly from `py7zr/py7zr.py` (installed
1.1.3), not guessed**: the public shape-flag surface is exactly five
properties: `is_directory`, `is_file`, `is_symlink`, `is_junction`,
`is_socket`. There is **no separate `is_hardlink` property**. Three
concrete findings follow directly from reading the extraction dispatch
code (`SevenZipFile.extractall()`'s internal branch on these flags):

```
is_socket   - py7zr's OWN extraction code explicitly skips writing
              anything for a socket entry ("ignore special files" /
              "pass  # TODO: implement me" in the actual source) - no
              file is ever created for it. SAFE by the library's own
              construction, BUT a real, previously-undiscovered bug
              follows from this: `_discover_extracted_7z_files` (in
              THIS codebase's ArchiveExtractor) does NOT skip
              is_socket members - it unconditionally calls
              `path.stat()` on every non-directory member, which would
              raise an UNCAUGHT FileNotFoundError for a socket entry
              py7zr silently declined to write. Concrete fix required:
              `_validate_7z_members` must reject `is_socket` members
              BEFORE calling extractall() (fail closed, matching the
              existing is_symlink rejection precedent), closing the
              crash at its root rather than letting discovery hit it.

is_junction - Windows-junction recreation in py7zr's own extraction
              code is explicitly gated to `sys.platform == "win32"`.
              This project runs on Linux exclusively (confirmed: every
              path in this codebase is a Linux filesystem path) - but
              `_validate_7z_members` today checks ONLY `is_symlink`,
              never `is_junction`. Concrete fix required: reject
              `is_junction` members alongside `is_symlink` in the
              SAME existing check, on the SAME defense-in-depth
              reasoning (never trust a third-party library's exact
              cross-platform fallback behavior for an archive-
              controlled entry type, when explicitly rejecting it
              costs nothing).

hardlinks   - no distinct `is_hardlink` flag exists on the public
              FileInfo API; a 7z-internal "hardlink to another archive
              member" concept is referenced only inside a private
              method (`_find_link_target`, docstring: "Find the target
              member of a symlink OR hardlink member") whose naming
              strongly suggests hardlink-shaped entries are modeled
              via the SAME `is_symlink` flag already rejected -
              substantially reducing, but not fully eliminating
              (short of constructing and extracting a real
              hardlink-bearing .7z, which this design-only pass does
              not do), the original uncertainty.
```

**Fail-closed policy, per the review's explicit instruction** ("if
behavior cannot be established confidently, specify an explicit
fail-closed policy"): rather than trying to enumerate every unsafe
MEMBER type pre-extraction across every library version (a moving
target), this design adds a **post-extraction file-type audit** as the
actual backstop, on top of (never instead of) the tightened
pre-extraction member checks above:

```
after extractall() returns, for every path under the destination tree
(os.walk, or equivalently iterating the already-known member list's
resolved paths):
    st = path.lstat()   # lstat, never stat - must not follow a
                         # symlink that should never have existed
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        raise ValueError(f"unsafe extracted entry type at {path}")
        # -> caught by the existing outer try/except, classified as
        #    MALFORMED_ARCHIVE (an archive that, despite passing
        #    format validation, produced something never legitimate
        #    to write) - staging deleted per section 12, claim
        #    released, FAILED IngestionAttempt recorded
```

This verifies the ACTUAL on-disk result, independent of which library
produced it or which version is installed - it closes the residual
uncertainty from both `is_junction`'s Linux fallback behavior and the
hardlink question directly, without depending on either being fully
traced through third-party source. Combined with the tightened
pre-extraction checks, this closes review point 3 completely: nothing
is left as an unverified assumption, and anything genuinely unresolved
(a truly novel entry type no one has considered) still fails closed
rather than silently succeeding.

### 5. Extraction algorithm

```
def extract_one_level(file, staging_dir):
    # existing ArchiveExtractor.extract([file], staging_dir) - UNCHANGED.
    # Atomic per format (finding already verified by reading the code
    # directly, per this milestone's explicit instruction). Internal
    # safety checks (traversal, expansion ratio, disk space, 7z-symlink
    # rejection) already run BEFORE extractall() is called, inside this
    # existing method, unmodified.
    return extractor.extract([file], staging_dir)

def extract_recursive(file, depth, ...):
    if depth >= max_depth: raise (-> OVERSIZED_OR_EXPANSION_LIMIT, see
                                    section 13's failure-taxonomy fix)
    staging_dir = nested_staging_dir(root_id, full_member_path) if depth > 0
                  else staging_root(root_id)
    extracted = extract_one_level(file, staging_dir)          # atomic
    running_total += sum(m.size for m in extracted if m is a real file)
    for member in extracted (excluding the archive file itself):
        # existing idempotent find-or-create + classification + recursion
        # + identity-resolution shape, EXTENDED this milestone to also
        # call classify_source_category/classify_workload_category/
        # classify_risk_tier_estimated (already-existing functions from
        # policy_evaluator.py, Milestone 2) at member-creation time -
        # per "### 11. Archive processing integration" above, which
        # already specifies this and is not yet wired into the code.
        ...
```

No per-file interruption exists inside `extract_one_level` — a crash
during the atomic `extractall()` call leaves the staging directory in
an arbitrary, possibly-partial state, discovered and discarded (never
trusted) by the idempotent-cleanup-then-fresh-extract step the NEXT
claim attempt performs (section 6).

### 6. Post-extraction reconciliation algorithm

```
1. actual_total = the running_total accumulated across the ENTIRE
   recursive walk (every level actually reached, summed once)
2. delta = actual_total - estimated_bytes   (from section 4's pre-flight;
   can be positive, negative, or zero - the declared D0 size is NEVER
   assumed equal to the true recursive extracted total, especially
   once nested archives are involved, since D0 only ever observed the
   ROOT archive's own compressed size)
3. UPDATE ingestion_batches
   SET extracted_bytes_consumed = extracted_bytes_consumed + :delta
   WHERE id = :batch_id
   -- a plain, UNCONDITIONAL correction (the admission decision already
   -- happened in section 4's step 2; this only makes the reported
   -- total accurate to what was REALLY written, exactly matching the
   -- frozen "### 7. Extracted-bytes accounting" pattern, generalized
   -- from one archive to a whole recursive tree)
4. Mismatch outcome, made explicit (closing design area #7's "define
   mismatch outcomes" requirement): reconciliation NEVER converts an
   incomplete extraction into a reported success. If extract_recursive
   raised at ANY point (steps 1-2 above never execute for that claim
   attempt), the pre-flight reservation from section 4 remains at its
   estimated value UNTIL the failure path (section 13) explicitly
   releases it - reconciliation and failure-release are mutually
   exclusive paths, never both run for the same attempt.
```

### 7. Member materialization / resume algorithm — CORRECTED (Design Correction Pass, exact wiring)

Unchanged from the existing, already-proven `_extract_recursive` shape
(see section 0). The original pass named this wiring in principle but
did not cite exact signatures - the review required the exact
integration to be resolved, not left as "call the existing
classifiers." Verified directly against `policy_evaluator.py`'s actual
function signatures:

```
member_source_category = classify_source_category(
    root_t7_path=root_t7_path, member_path=full_member_path)
    # physical shape ONLY (ARCHIVE if the member's own suffix is
    # .zip/.7z/.gz/.rar, LOOSE_FILE otherwise) - never a processing-
    # capability judgment, matching classify_source_category's own
    # frozen docstring exactly

member_workload_category = classify_workload_category(
    root_t7_path=root_t7_path, member_path=full_member_path,
    source_category=member_source_category)
    # CONTAINER if member_source_category is ARCHIVE (a nested
    # archive is never itself content); otherwise the member's own
    # content-type nature from its suffix (TEXT_DOCUMENT/
    # STRUCTURED_DATA/MEDIA/SOFTWARE/ENCRYPTED/UNKNOWN)

member_risk_tier_estimated = classify_risk_tier_estimated(
    source_category=member_source_category,
    declared_size_bytes=member.size)
    # NAMING NOTE, stated explicitly rather than silently glossed
    # over: this function's parameter is named `declared_size_bytes`
    # for its ORIGINAL (selection-time, pre-extraction) loose-file use
    # case, where the size truly is an unverified declaration. For an
    # archive MEMBER, by the time this call happens the member has
    # ALREADY been extracted - `member.size` (from the existing
    # DiscoveredFile, itself from Path.stat() inside
    # _discover_extracted_files/_discover_extracted_7z_files) is a
    # REAL, MEASURED value, not a declaration. This is naming friction
    # only, never a semantic error: risk_tier_ESTIMATED remains "what
    # is known before full pipeline processing" regardless of whether
    # the size input happened to come from a declaration or a real
    # measurement for this particular row.
```

**Physical category vs. workload category, preserved exactly as
already frozen** (review point 5's explicit instruction): the
distinction is not re-litigated here - `source_category` answers "what
IS this member physically" (never opened further than a suffix check),
`workload_category` answers "what kind of content is this," and
neither is inferred from, or conflated with, the other. Both are set
once, at member-creation time, on their own dedicated columns -
`evidence_snapshot` remains exactly `{"discovered_during_extraction":
True, "parent_source_instance_id": root_instance_id}`, unchanged, per
the already-frozen "physical category vs JSONB" correction from
Milestone 2.

**`classify_backup_sync_context` is explicitly NOT called for archive
members** (closing review point 5's second instruction: "do not infer
backup/sync/snapshot semantics from archive membership"). That
classifier remains the existing, project-wide stub (`always None`,
Round 4 of the earlier design chain) regardless of whether a
`SourceInstance` is a loose file or an archive member - being
physically located inside an archive is never treated as a
backup/sync/snapshot signal in this design, and this milestone adds no
new logic that would make it one.

**`SourceInstance.risk_tier_actual` — exact specification, resolving
review point 4** (the original pass correctly identified the column
does not exist yet, but the review required this be resolved against
the frozen architecture, not left as a bare "implementation must add
it"):

```
Column:  SourceInstance.risk_tier_actual: RiskTierEstimated | None
         (reuses the EXISTING RiskTierEstimated enum - no new enum
         type, no new Postgres type to create)
Nullable: yes - NULL is a real, meaningful "no evidence yet," never
         fabricated as a stand-in value (frozen numeric pass, section
         5, unchanged)
Written: EXACTLY ONCE, on the ROOT archive SourceInstance, at the end
         of a SUCCESSFUL whole-claim-attempt extraction (the same
         moment section 6's reconciliation completes) - write-once,
         matching every other immutable-fact column convention already
         established (evidence_snapshot, the three classification
         columns, etc.)
Inputs (real evidence, per the frozen numeric pass's own naming -
"member count, nesting depth, measured expansion ratio"):
    total_member_count   = count of every SourceInstance created or
                            reused across the ENTIRE recursive walk
    max_depth_reached     = the highest `depth` value actually recursed
                            into during this claim attempt
    measured_expansion_ratio = actual_durable_extracted_bytes (section
                            6's reconciled total) / the root archive's
                            own on-disk compressed size
Mapping: NOT decided by this pass, matching the frozen numeric pass's
         OWN explicit deferral ("Values: LOW | MEDIUM | HIGH | EXTREME.
         Numeric boundaries are deliberately not decided here") - this
         design resolves WHAT feeds the computation and WHEN it runs,
         without inventing the thresholds risk_tier_estimated's own
         frozen spec already left as a future calibration decision.
         Extraction failing before this evidence exists (any FAILED
         outcome, including the section 4a fail-closed audit and the
         section 13 max_depth fix) leaves risk_tier_actual NULL,
         exactly as the frozen numeric pass requires.
```

A migration adding this column is required before Milestone 5
implementation proceeds (out of scope for this design-only pass - see
the gap register's final disposition, same status as `SourceInstance.
claim_generation` above).

**Resume, restated precisely as the required invariant proof:**
`_find_existing_member`'s lookup key (`classification_run_id,
root_t7_path, member_path`) is unaffected by staging-directory
cleanup/recreation (section 3) - an existing member row is found and
its `evidence_snapshot`/classification columns are never touched
(write-once, existing convention), regardless of how many times the
underlying staging tree has been wiped and rebuilt by intervening
crashed attempts. Re-extraction of already-resolved members' raw bytes
on a resumed attempt is an accepted, existing inefficiency (full
`extractall()` is unconditional per level), never a correctness
concern - explicitly named here rather than left implicit.

### 8. Nested archive rules

- **Depth limit**: `max_depth` (existing parameter, default 10),
  unchanged.
- **Resource accounting**: batch-envelope admission/reconciliation
  happens ONCE per root claim attempt (sections 4/6), summing every
  level's measured bytes into one running total - never a separate
  pre-flight/reconciliation pair per nested level (the nested archive's
  own declared size is unknowable without opening it, and is already
  implicitly included in its own DECLARED_MEMBER_BYTES the moment its
  PARENT level's `extractall()` runs, since the nested `.zip`/`.7z`
  file itself is one of the parent's real extracted files).
- **Provenance construction**: unchanged (`chain_prefix`/`full_chain`,
  existing).
- **Crash recovery**: proven by induction, not special-cased per depth
  - the SAME idempotent find-or-create/staging-cleanup/re-extract
  mechanism applies uniformly at every recursion level, since
  `extract_recursive` is structurally identical regardless of depth.
- **Duplicate nested members**: generalizes cleanly from section 13
  below - no additional mechanism needed.
- **Archive-member classification**: identical classifiers applied at
  every depth (a nested-archive member gets `source_category=ARCHIVE`
  if it is itself `.zip`/`.7z`, `LOOSE_FILE` otherwise - exactly the
  depth-0 rule, unconditionally generalized).

### 9. Provenance rules

Frozen relationship (already correct in the existing schema and
`SourceInstanceService`, restated as the explicit contract this
milestone must not disturb):

```
archive SourceInstance (root_t7_path=archive path, member_path=NULL)
   |
   +-- ProvenanceLink[0] = T7_FILE, path=root_t7_path
   |
   +-- ArchiveProcessingService._extract_recursive
          |
          +-- member SourceInstance (root_t7_path=SAME archive path,
          |     member_path=full internal path)
          |      |
          |      +-- ProvenanceLink[0] = T7_FILE (same root path)
          |      +-- ProvenanceLink[1] = ARCHIVE_MEMBER (this member's
          |            own relative path within its immediate container)
          |
          +-- nested archive member (source_category=ARCHIVE)
                 |
                 +-- recurse: its OWN members get ProvenanceLink[2],
                       [3], ... - one link per nesting level, root
                       first, exactly matching ProvenanceLink's own
                       frozen "T7 file -> archive -> nested archive ->
                       member -> ..." docstring
```

Every physical occurrence (every `SourceInstance` row, at every depth)
retains its own, complete, independently-queryable ancestry chain -
`ProvenanceLink` rows are never shared or merged across
`SourceInstance` rows, even when two members converge to the same
`ContentIdentityGroup` (section 13).

### 10. Resource / accounting rules

Integration points with the already-frozen `IngestionBatch` envelope,
stated exhaustively (design area #15):

```
max_source_bytes        - enforced at SELECTION time only (Milestone 2,
                           unchanged) - the archive CONTAINER's declared
                           size counts once, at admission; extraction
                           never re-checks or re-charges this envelope.
max_extracted_bytes     - THIS milestone's own envelope (sections 4/6).
max_runtime_seconds     - unaffected by this design; BatchControlService's
                           existing monotonic accounting (Milestone 3)
                           is not extended or duplicated here.
workspace free space    - ArchiveExtractor's OWN existing per-level
                           HARD_FREE_SPACE_BYTES check (unchanged) is a
                           SEPARATE, complementary layer to
                           BatchResourceGuard's own workspace-disk check
                           (Milestone 3) - the batch-level guard is the
                           coarser, claim-admission-time gate; the
                           extractor's own check is a second,
                           extraction-time safety net using the
                           SPECIFIC archive's own declared bytes. Both
                           exist; neither replaces the other (same
                           "primary + defense in depth" relationship
                           already established between BatchCreationService's
                           advisory lock and its DB UNIQUE index).
batch state              - RUNNING-only claim admission (section 2);
                           pause/abort behavior for IN-FLIGHT extraction
                           is section 16's own topic.
hard/soft resource stops - BatchResourceGuard is consulted BEFORE a new
                           archive claim is attempted (the same
                           "admission control signal only" role it
                           already plays for embedding reservations,
                           Milestone 4) - never duplicated inside
                           ArchiveExtractor itself.
```

**Whether failed/discarded staging contributes to durable
consumption**: **no.** A failed extraction's pre-flight reservation
(section 4) is released in full (section 13) - the batch's durable
`extracted_bytes_consumed` counter reflects only bytes from claim
attempts that reached the reconciliation step (section 6), success or
partial-nested-failure alike is undefined here since any exception
anywhere in the recursive walk is caught by the SAME outer try/except
(existing shape) and treated as a whole-attempt failure - there is no
"partial success, partial reconciliation" outcome in this design.

### 11. Crash / recovery rules

```
Crash BEFORE extraction (claim granted, pre-flight not yet attempted):
    -> claim goes stale by claimed_at, recovered by the existing
       claim-query mechanism (unchanged); the recovering worker's OWN
       idempotent-staging-cleanup step (section 3/6) finds nothing to
       clean (staging never existed) and proceeds normally.

Crash DURING/AROUND extractall() (atomic call in flight):
    -> staging directory left in an arbitrary, untrusted state.
       NEVER read from directly by anything else in this codebase
       (staging is never the source Document/DocumentChunk content is
       read from - section 3). The recovering worker's idempotent
       cleanup (section 6) deletes it unconditionally before its own
       fresh extractall() call - no partial staging content is ever
       trusted or reused across attempts.

Crash AFTER extraction, BEFORE member persistence:
    -> extracted_bytes_consumed's PRE-FLIGHT reservation (section 4)
       is durably persisted (it was a real, committed UPDATE) but never
       reconciled (section 6 never ran) - it remains at its estimated
       value until a LATER successful attempt reconciles it, or a
       LATER failed attempt explicitly releases it (section 13). No
       member SourceInstance rows exist yet for this crash point by
       definition (staging existed, but nothing was persisted to
       Postgres) - the next attempt starts member creation from empty.

Crash DURING member persistence (some members created, some not, for
this specific claim attempt):
    -> already the exact scenario the EXISTING idempotency mechanism
       is proven to handle (section 0/7) - unchanged by this design.
       A member with content_identity_group_id already set is skipped;
       one without it is completed (re-hashed, re-resolved) using the
       SAME existing SourceInstance row, never a new one.

Stale claim recovery:
    -> the SAME `claimed_at < stale_before` mechanism as every other
       SourceInstance-level claim (unchanged) - see section 17 for the
       one place this has a real, not-fully-closed consequence for
       archive processing specifically (concurrent staging writes
       under a too-short lease).

Cleanup/reuse of abandoned staging:
    -> idempotent delete-then-recreate, keyed on the ROOT SourceInstance's
       durable id (section 3) - the SAME mechanism handles "abandoned
       by a crash" and "abandoned by stale-claim recovery" identically,
       since both produce the same observable state: a claimed-but-
       incomplete archive whose staging tree (if any) predates this
       attempt.
```

### 12. Cleanup rules

```
staging_root(root_instance_id) is deleted UNCONDITIONALLY at the START
of every claim attempt (idempotent - a no-op if nothing exists) AND
UNCONDITIONALLY at the END of _process_claimed_archive, in a `finally`
block alongside the existing release_source_instance_claim call -
covering success, every FAILED outcome, and (per section 16) the
"batch paused/aborted concurrently but this extraction already
completed" case identically, since none of those distinguish the
cleanup path - only whether persistence/reconciliation already
happened before cleanup runs.
```

Cleanup NEVER touches `root_t7_path` or anything under the real T7 -
`ArchiveExtractor.extract()`'s `destination` argument is always a
`workspace_root`-rooted path (section 3); nothing in this design adds
a code path that deletes, moves, or writes to a T7 path, matching the
mandatory "cleanup is workspace-only" invariant exactly.

### 13. Failure taxonomy — max_depth outcome FROZEN (Design Correction Pass)

Reusing the existing `IngestionFailureCode` vocabulary (already
sufficient - no new enum value proposed by this design-only pass):

```
CORRUPT_INPUT              - not currently reachable from
                             _classify_extraction_failure (it maps to
                             MALFORMED_ARCHIVE for a bad ZIP/7z magic
                             number instead) - this design does not
                             change that mapping; CORRUPT_INPUT remains
                             reserved for non-archive content
                             (NormalizationService's own usage,
                             unchanged).
MALFORMED_ARCHIVE          - is_zipfile()/is_7zfile() returning False,
                             or any other ValueError not matching the
                             expansion-ratio message - unchanged.
OVERSIZED_OR_EXPANSION_LIMIT - expansion-ratio bomb (existing,
                             unchanged) AND max_depth exceeded.
EXTRACTION_ERROR_OTHER     - unchanged, the catch-all.
T7_UNAVAILABLE             - FileNotFoundError - unchanged; see
                             section 14 for drift specifically.
INSUFFICIENT_DISK_SPACE    - OSError from ArchiveExtractor's own
                             workspace-disk check - unchanged. Distinct
                             from a batch-envelope-exhausted deferral
                             (section 4), which records NO
                             IngestionAttempt at all - this code is
                             reserved for the extractor's OWN internal
                             refusal, a genuine attempted-and-failed
                             outcome.
PERMISSION_DENIED          - unchanged.
```

**`max_depth`-exceeded — exact terminal outcome, frozen, not left to
"implementation cleanup"** (review point 2's explicit requirement):
today, `_extract_recursive` raising at the depth check falls through
`_classify_extraction_failure`'s generic `ValueError` branch (which
only special-cases the expansion-ratio message) into `MALFORMED_
ARCHIVE` - semantically wrong, since a too-deeply-nested archive is
not unreadable or corrupt, it is a structural limit deliberately being
enforced. The frozen outcome:

```
outcome:       FAILED (never SUCCEEDED, never silently truncated -
               reconciliation and risk_tier_actual assignment,
               sections 6/7, both explicitly require a SUCCESSFUL
               whole-claim-attempt to have completed first, so a
               depth-limit failure structurally cannot leave either
               looking like a success)
failure_code:  OVERSIZED_OR_EXPANSION_LIMIT (reusing the existing
               value, grouped with the expansion-ratio bomb guard as
               the SAME category of frozen, deliberate hard structural
               limit - never MALFORMED_ARCHIVE, never CORRUPT_INPUT).
               A dedicated new enum value (e.g. a hypothetical
               ARCHIVE_DEPTH_LIMIT_EXCEEDED) would be more precise,
               but `IngestionFailureCode` is a Postgres ENUM type -
               adding a value requires `ALTER TYPE ... ADD VALUE`, a
               migration, forbidden by this design-only pass's strict
               boundaries. OVERSIZED_OR_EXPANSION_LIMIT is the correct
               choice available WITHOUT one; if a future pass judges
               the grouping too coarse, splitting it is its own,
               separate, small schema decision - not invented here.
retryable:     False. Unlike a transient condition (disk temporarily
               full, Ollama briefly unreachable), max_depth exceeded is
               a DETERMINISTIC property of this specific archive's own
               structure against a fixed configuration value - retrying
               with the SAME max_depth against the SAME archive fails
               identically, every time. (Note, scoped narrowly and not
               decided here: the CURRENT code hardcodes retryable=True
               for every archive failure category uniformly - this
               correction addresses ONLY the max_depth outcome
               specifically, per the review's own scoped request; it
               does not re-litigate retryable semantics for
               MALFORMED_ARCHIVE/EXTRACTION_ERROR_OTHER/etc., which
               remain exactly as they already are.)
distinctness:  never UNSUPPORTED (a ContentPipelineState value for
               MEMBER content eligibility, not an IngestionAttempt
               outcome for a container-level structural refusal) and
               never EXCLUDED (a policy decision made before any
               attempt, never the result of one) - a FAILED
               IngestionAttempt with this failure_code is the sole,
               unambiguous representation.
```

Required acceptance/test item (added to sections 18/20 below): a
synthetic archive nested exactly `max_depth` levels deep must produce
`IngestionAttempt.outcome=FAILED`, `failure_code=OVERSIZED_OR_
EXPANSION_LIMIT`, `retryable=False`, and `SourceInstance.risk_tier_
actual IS NULL` on the root instance - asserted directly, not merely
that "an exception was raised somewhere."

**EXCLUDED / UNSUPPORTED / FAILED, kept distinct exactly as already
established** (mandatory invariant): `.rar`/`.gz` and any other
non-extractable archive format never reach `ArchiveProcessingService`
at all - they are excluded at the SELECTION layer
(`BatchClassPolicy.require_extractable_archive`, Milestone 2,
unchanged), never claimed, never attempted, never recorded as FAILED.
A member's own eligibility (EXCLUDED for `.c9r`, UNSUPPORTED for a
known-unsupported member extension) is decided by the SAME
`classify_eligibility` function loose files already use, unchanged -
archive membership does not create a second eligibility vocabulary.

### 14. T7 unavailable / drift

Reusing the frozen "### 10. Live corpus drift" four-way outcome
taxonomy verbatim, applied to archive containers specifically:

```
OBSERVED_AT_SELECTION          - D0's declared (path, size) at batch
                                  admission - evidence only, never
                                  re-validated as if it were current.
SOURCE_PRESENT_AT_EXECUTION    - archive still exists, is still a
                                  valid zip/7z, metadata roughly matches.
SOURCE_CHANGED_AFTER_SELECTION - archive still opens, but its actual
                                  declared member sizes/count differ
                                  from D0's single declared-size
                                  estimate - NOT an error; extraction
                                  proceeds normally, and the ONLY
                                  consequence is that section 4's
                                  pre-flight estimate (D0's declared
                                  size) may diverge further than usual
                                  from section 6's measured actual -
                                  reconciliation (never the pre-flight
                                  estimate) is what stays authoritative.
SOURCE_MISSING_AT_EXECUTION    - the existing FileNotFoundError ->
                                  T7_UNAVAILABLE path, unchanged.
```

Frozen invariant, restated for archives specifically: **the bytes
actually extracted are authoritative for every member's content
identity; D0's declared size is selection-time evidence for admission
and pre-flight estimation ONLY, never re-asserted as fact once real
extraction has occurred.**

### 15. Duplicate archive members

Three cases, all shown to generalize cleanly from the already-proven
loose-file mechanism - **no new mechanism required**:

```
1. Two DIFFERENT member paths within ONE archive, byte-identical
   content: two SourceInstance rows (different member_path), two
   independent ProvenanceLink chains, ONE ContentIdentityGroup (hash
   convergence) - identical in shape to two loose files with the same
   content ("same content, new occurrence").

2. The SAME member re-observed by re-processing the SAME archive:
   - within ONE classification_run_id: _find_existing_member prevents
     a duplicate row (idempotent resume, section 7).
   - across DIFFERENT classification_run_ids (a later DiscoveryRun/
     batch re-selecting the same T7 archive path): a NEW SourceInstance
     is correctly created (matches BatchCreationService's own
     discovery-run-scoped eligibility precedent, Milestone 2) and its
     member, once re-extracted and re-hashed, converges to the SAME
     ContentIdentityGroup as before.

3. Identical content in TWO ENTIRELY DIFFERENT archives: each gets its
   own root_t7_path + member_path + independent ProvenanceLink chain
   (rooted at its OWN archive's T7_FILE), converging to ONE
   ContentIdentityGroup by hash - exactly what content-identity
   convergence exists for.
```

A crafted archive with two central-directory entries claiming the SAME
internal path (`zipfile`'s own last-write-wins behavior on
`extractall()`) is absorbed by the same convergence mechanism: both
central-directory entries produce their own `DiscoveredFile` record,
both read the SAME final (overwritten) on-disk bytes, both converge to
ONE `ContentIdentityGroup` - safe, not a gap, requiring no new logic.
**Content identity convergence never merges or deletes a
`SourceInstance`/`ProvenanceLink` row** in any of these cases - only
`content_identity_group_id` is set (write-once, existing mechanism) on
each independently-existing row.

### 16. Pause / abort behavior around an atomic operation

Restating the mandatory invariant precisely, since "extraction is
atomic" and "stop means STOP" can otherwise be read as contradictory:

```
Batch pauses/aborts BEFORE a new archive claim is attempted:
    -> the claim's own RUNNING-admission check (section 2, Milestone 4,
       unchanged) refuses the claim outright. No extraction begins.

Batch pauses/hard-stops WHILE an extractall() call is already
synchronously in flight (the claim was already granted; extraction is
running in this worker's own process, uninterruptible mid-call, per
the frozen atomicity finding):
    -> the in-flight extraction is ALLOWED TO FINISH. Neither this
       design nor anything in this codebase spawns/kills worker
       processes - there is no mechanism to interrupt a running
       extractall() call, and pretending otherwise would contradict
       the frozen atomicity finding this design was explicitly told to
       respect. "Stop means STOP" governs the NEXT admission decision
       (the next claim, the next pre-flight envelope check), never an
       already-granted, already-atomic operation.
    -> IF extraction then SUCCEEDS: reconciliation (section 6) and
       member persistence proceed normally and are PERSISTED - already-
       completed, correct work is never discarded merely because the
       batch's own state changed concurrently (exactly matching
       BatchControlService.abort()'s own documented position: "does
       NOT clean up any active extraction/embedding work in progress").
    -> IF extraction then FAILS (for an unrelated reason, or because a
       genuine hard-stop condition - e.g. workspace disk actually
       ran out - manifested as an OSError): the existing failure path
       (section 13) runs exactly as it would have regardless of batch
       state; the pre-flight reservation is released (section 4/13).

Operator aborts AFTER extraction has already completed (persisted):
    -> no consequence to already-persisted SourceInstance/
       ProvenanceLink/ContentIdentityGroup rows - those are durable,
       immutable historical facts (existing invariant, unchanged) -
       only NEW claims are refused going forward.
```

### 17. Claim fencing — RESOLVED (Design Correction Pass; see section 2)

**Original position, superseded.** This section originally recorded an
open, unclosed gap: `SourceInstance.release_source_instance_claim`
cleared `claimed_by`/`claimed_at` unconditionally by `instance_id`
alone, with no generation to fence on. The problem this identified,
preserved here for the historical record of WHY the fix in section 2
exists:

**The concrete consequence, as originally worked through**: worker A
claims a large archive; its extraction genuinely takes longer than
`lease_duration` (archives can legitimately take far longer than a
typical embedding call); stale-claim recovery grants the SAME archive
to worker B, who begins extracting into the SAME deterministic
`staging_root` (section 3); A, still genuinely running (not crashed -
a true zombie, not a corpse), eventually finishes and calls its own
`release_source_instance_claim(instance.id)` in its `finally` block -
unconditionally clearing B's live claim, since the call had no
generation to check against. A third worker C could then claim the
SAME archive while B was still working, producing a genuine three-way
concurrent write into one staging directory - not merely a
lease-window-bounded race, but one A's own "harmless" cleanup call
actively, deterministically reopened.

**Review correctly rejected the proposed resolution** ("add the column,
or accept bounded residual risk with a conservative lease") as
insufficient - a shorter lease narrows the race window but does not
remove the underlying correctness dependency on unconditional release,
and the same invariant Milestone 4 judged worth a real fix for
`ContentIdentityGroup` should apply consistently to `SourceInstance`.

**Resolution, now frozen in section 2 above**: `SourceInstance` gains
`claim_generation` (identical shape to `ContentIdentityGroup`'s),
`release_source_instance_claim` gains a required, fenced
`claim_generation` parameter, and both `SourceInstance`-level claim
methods increment it on every grant (fresh or reclaim) in the same
existing atomic `UPDATE`. Worker A's release above now matches zero
rows once B has reclaimed the row to a new generation - a safe no-op,
never a wrongful clear, never an opening for a third concurrent
claimant. This is no longer an open gap; it is a frozen design
requiring one new column and two small, mechanical call-site updates
at implementation time (see the gap register's final disposition,
section 19, and the acceptance criteria, section 20).

### 18. Concurrency / adversarial test matrix — UPDATED (Design Correction Pass)

```
TRAVERSAL / UNSAFE MEMBERS
  - member path containing ../.. escaping the staging root -> rejected
    (existing ArchiveExtractor check, needs a LOCKING test, not just
    reliance on the check existing)
  - absolute member path (e.g. "/etc/passwd") -> rejected (existing
    behavior is INCIDENTAL - Path's own "/" operator discards the left
    side when the right is absolute, so is_relative_to() still catches
    it - this design recommends a DEDICATED test proving this, not
    relying on the side effect remaining true across a future Python
    version)
  - 7z symlink member -> rejected (existing, needs a locking test)
  - 7z junction member -> rejected (NEW pre-extraction check this
    design adds, section 4a - Windows-junction recreation is
    win32-gated in py7zr, but this project runs on Linux; reject
    unconditionally rather than trust the fallback path)
  - 7z socket member -> rejected BEFORE extraction (NEW pre-extraction
    check, section 4a - closes the real _discover_extracted_7z_files
    FileNotFoundError crash bug found by this correction pass: py7zr
    itself never writes a file for a socket entry, so the existing
    discovery loop's unconditional path.stat() would otherwise raise
    uncaught)
  - ZIP "symlink-mode" member (external_attr S_IFLNK bits set) ->
    CONFIRMED EMPIRICALLY (section 4a, reproducible): extractall()
    writes a REGULAR file containing the symlink TARGET STRING as
    content, never a real symlink - the exact reproduction from
    section 4a becomes this test verbatim
  - hardlink-shaped entry (7z) -> py7zr's public FileInfo API has no
    is_hardlink flag (confirmed by reading py7zr/py7zr.py directly,
    section 4a); construct the closest obtainable adversarial case and
    confirm the post-extraction file-type audit (below) still fails
    closed even if the pre-extraction member check cannot name the
    entry precisely
  - post-extraction file-type audit (NEW, section 4a's fail-closed
    backstop) -> after any extractall() call, every entry under the
    destination must be a regular file or directory (lstat, never
    stat) - anything else raises, classified MALFORMED_ARCHIVE,
    staging discarded, claim released; this is the general safety net
    that does not depend on enumerating every unsafe member type by
    name
  - duplicate member names within one archive -> converges via content
    identity, never corrupts (section 15)

RESOURCE / ENVELOPE
  - declared-size-exceeds-envelope -> deferred, no IngestionAttempt,
    staging never created, claim released (section 4)
  - actual extraction undershoots/overshoots declared estimate ->
    reconciliation corrects the counter without re-litigating admission
    (section 6)
  - workspace disk exhausted mid-extraction -> ArchiveExtractor's own
    existing OSError path (unchanged), INSUFFICIENT_DISK_SPACE
  - expansion-ratio bomb -> existing HARD_EXPANSION_RATIO rejection
  - max_depth exceeded, EXACTLY at the configured boundary -> FAILED,
    OVERSIZED_OR_EXPANSION_LIMIT, retryable=False, risk_tier_actual
    remains NULL on the root SourceInstance (section 13's frozen
    outcome, asserted precisely, not just "an exception was raised")

CORRUPTION / FORMAT
  - corrupt/truncated zip or 7z -> MALFORMED_ARCHIVE
  - unsupported archive format (.rar/.gz) -> never claimed (selection-
    layer exclusion, unchanged)
  - archive with zero members -> succeeds trivially, zero members
    created, SUCCEEDED IngestionAttempt

CRASH / RESTART / ZOMBIE (real PostgreSQL, real filesystem)
  - crash before extraction -> stale-claim recovery, clean start
  - crash mid-extractall() (simulated: kill the staging dir mid-write)
    -> next attempt's idempotent cleanup discards it, re-extracts clean
  - crash after extraction, before any member persisted -> reservation
    remains estimated, next attempt starts member-creation from empty
  - crash mid-member-persistence (some members done, some not) ->
    existing idempotent resume (already covered by existing tests -
    this design does not weaken that coverage, only re-verifies it
    still holds with batch/envelope integration layered on)
  - genuine zombie (not crashed) returns after stale-recovery granted
    the SAME archive to a second worker -> the section 17 scenario,
    now REQUIRED to prove the FIX, not measure the gap: worker A claims
    (generation N), stale-recovery reclaims to generation N+1 for
    worker B, A's own delayed `release_source_instance_claim(instance_
    id, claim_generation=N)` call must be a no-op (zero rows matched);
    assert B's `claimed_by`/`claimed_at`/`claim_generation` are
    completely untouched by A's call - real PostgreSQL, mirroring
    Milestone 4's own zombie-worker test shape exactly, generalized to
    `SourceInstance`
  - concurrent stale-claim recovery race for the SAME archive (two
    workers both attempt to reclaim before either commits) -> exactly
    one wins, generation advances by exactly 1, never 2 (mirrors
    Milestone 4's `test_one_stale_recovery_wins_concurrently`,
    generalized to `SourceInstance`)

NESTED ARCHIVE
  - archive-of-archives resumed mid-recursion -> proven by induction
    (section 8), tested at depth 2+ explicitly, not just depth 0
  - duplicate nested member across two different nested archives ->
    converges via content identity (section 15)

PROVENANCE
  - duplicate ProvenanceLink rows never created on any resume path
    (assert exact row count before/after a simulated crash-resume)

CONCURRENCY (real PostgreSQL + real filesystem, separate
sessions/processes)
  - two workers race to claim two DIFFERENT archives in the SAME
    RUNNING batch -> both succeed, zero cross-contamination (mirrors
    Milestone 4's cross-batch claim race, applied within one batch)
  - two workers race to claim the SAME archive (both attempt
    concurrently, before either's claim commits) -> exactly one wins
    (existing SKIP LOCKED mechanism, re-verified under this milestone's
    added classification_run_id/RUNNING-admission conditions)
  - batch pause/abort races a claim attempt -> RUNNING-admission wins
    or loses cleanly (mirrors Milestone 4's pause-vs-claim race,
    reused directly for the archive-claim query)
  - concurrent reservation-vs-envelope race across MULTIPLE archives
    claimed simultaneously against a small max_extracted_bytes ->
    envelope never exceeded (mirrors Milestone 4's concurrent-
    reservation-vs-max_embeddings test, applied to
    extracted_bytes_consumed)

T7 UNAVAILABLE / DRIFT
  - archive deleted between selection and claim -> T7_UNAVAILABLE
  - archive content changed (different bytes, same declared size in D0)
    -> extraction proceeds, measured bytes/hash reflect the TRUE
    current content, never the stale D0 estimate (section 14)

STAGING CLEANUP
  - staging deleted after SUCCEEDED, FAILED, and simulated-crash-then-
    recovered paths - assert zero residual staging directories after
    each
```

### 19. Unresolved gaps / open questions — FINAL DISPOSITION (Design Correction Pass)

All eight gaps from the original pass, each given an explicit final
disposition per review's classification. None remain silently deferred
without an explicit status.

1. **`SourceInstance` claim release has no generation-fencing** —
   **RESOLVED (design-level).** Section 2 above freezes the full
   `claim_generation` design for `SourceInstance`, generalizing
   `ContentIdentityGroup`'s Milestone 4 pattern exactly: durable
   column, fenced acquisition, fenced release, unchanged stale-recovery
   mechanism, the zombie scenario now closed. **Remaining work is
   implementation-only**: add the column (migration), thread the
   parameter through both claim methods and `release_source_instance_
   claim`, update the two existing call sites. A shorter lease is
   explicitly rejected as a substitute correctness mechanism, per
   review's instruction - the lease question (item 8 below) is
   resolved independently, on its own merits, not as a stand-in fix
   for this one.
2. **py7zr hardlink/special-file behavior** — **RESOLVED (verified +
   fail-closed backstop), per section 4a.** Direct inspection of
   `py7zr/py7zr.py` (installed 1.1.3) confirms the public `FileInfo`
   API's exact shape-flag surface (`is_directory`/`is_file`/
   `is_symlink`/`is_junction`/`is_socket`, no `is_hardlink`), and
   confirms two concrete, previously-unknown facts: `is_junction` is
   not checked by the existing `_validate_7z_members` (a real gap, now
   closed by extending that check) and `is_socket` entries, which
   py7zr's own extraction silently skips, would crash `_discover_
   extracted_7z_files`'s unconditional `path.stat()` with an uncaught
   `FileNotFoundError` (a real, previously-undiscovered bug, now closed
   the same way). The remaining irreducible uncertainty (no distinct
   hardlink flag exists to check) is closed by the fail-closed
   post-extraction file-type audit, which verifies the ACTUAL result
   regardless of library internals.
3. **ZIP symlink-safety** — **RESOLVED (confirmed empirically), per
   section 4a.** A disposable, non-committed synthetic reproduction
   (crafted `ZipInfo` with `external_attr` symlink mode bits, extracted
   via this project's actual Python 3.12 `zipfile`) proved `extractall
   ()` writes a regular file containing the target string, never a real
   symlink. No longer a belief - a directly-observed fact, with the
   exact reproduction steps specified so it becomes a real, committed
   test at implementation time.
4. **`max_depth`-exceeded misclassification** — **RESOLVED (frozen
   outcome), per section 13.** Exact terminal state specified:
   `FAILED`, `failure_code=OVERSIZED_OR_EXPANSION_LIMIT`,
   `retryable=False`, `risk_tier_actual` remains NULL - never
   `MALFORMED_ARCHIVE`, never confusable with `UNSUPPORTED`/`EXCLUDED`.
   A dedicated new failure-code enum value was considered and
   explicitly rejected (it would require a migration, forbidden by
   this pass's boundaries) in favor of the correct existing value.
5. **`SourceInstance.risk_tier_actual` absence** — **RESOLVED
   (design-level), per section 7.** Exact column type (reuses
   `RiskTierEstimated`, no new enum), nullability, write-once timing
   (end of a successful whole-claim-attempt), and input formula (total
   member count, max depth reached, measured expansion ratio from
   section 6's reconciled total) are now fully specified. The frozen
   numeric pass's own deferral of NUMERIC THRESHOLDS (LOW/MEDIUM/HIGH/
   EXTREME boundaries) is correctly preserved as still-deferred - this
   design does not invent those, matching `risk_tier_estimated`'s own
   already-accepted gap. **Remaining work is implementation-only**: the
   column migration itself.
6. **Member-level classification unwired** — **RESOLVED (exact
   wiring specified), per section 7.** Exact function calls, exact
   parameters, and the `declared_size_bytes`-naming clarification (a
   real, measured value for members, despite the parameter's
   loose-file-era name) are now specified precisely, not left as "call
   the existing classifiers." `classify_backup_sync_context` is
   explicitly confirmed NOT invoked for archive members - archive
   membership is never treated as a backup/sync/snapshot signal.
   **Remaining work is implementation-only**: wiring the specified
   calls into `_extract_recursive`.
7. **`ArchiveExtractor.SUSPICIOUS_EXPANSION_RATIO` dead code** —
   **RESOLVED (remove, do not invent new semantics).** The constant
   was never given defined behavior by any prior design pass (unlike
   `HARD_EXPANSION_RATIO`, which has a clear, frozen, enforced
   meaning) - inventing a new "soft warning" tier for it now would be
   scope creep beyond "resolve the dead code," not requested by this
   correction and not decided here. Recommendation: delete the unused
   constant at implementation time. A future, SEPARATE design pass
   remains free to propose a real soft-expansion-ratio signal (e.g.
   feeding `IngestionBatch.review_required`) if a real need for one
   emerges - not proposed or decided by this pass.
8. **Archive-processing `lease_duration`** — **RESOLVED (mechanism
   frozen, per-class values tied to an existing calibration gap), per
   section 2.** The staleness formula and parameter-based shape are
   unchanged and frozen; the existing `timedelta(minutes=10)` default
   is confirmed correct for the smallest extractable archive class
   only. Larger classes' exact lease values are not invented here -
   they are folded into the SAME already-acknowledged `CALIBRATION_
   REQUIRED`/`TBD at gate` numeric-envelope gap the frozen numeric pass
   already named for those classes' other envelope fields, never left
   as a new, separately-undecided question.

**Net effect**: every gap has an explicit design-level resolution or a
named, bounded implementation-only remainder (a migration, a call-site
update, or a wiring task) - none are open architectural questions any
longer. Items 1 and 5 both require a migration; item 1's fencing logic
and item 5's column can reasonably be added in the SAME schema-only
implementation step, mirroring how Milestone 1 introduced multiple
related columns together.

### 20. Implementation acceptance criteria — UPDATED (Design Correction Pass)

A later, separately-authorized Milestone 5 implementation must be
reviewable against exactly these criteria (mirroring the discipline
already applied to Milestones 1-4):

```
1. claim_source_instance_for_archive_processing is called with
   classification_run_id set to the claiming batch's ClassificationRun.id
2. RUNNING-only admission is proven under real PostgreSQL concurrency
   (pause-vs-claim race), reusing Milestone 4's established pattern
3. staging_root/nested_staging_dir naming matches section 3 exactly;
   no archive-member-name is ever used directly as a path component
4. pre-flight admission (section 4) and post-extraction reconciliation
   (section 6) are both implemented as the exact conditional/plain
   UPDATE statements specified, never read-then-update
5. envelope-exhausted deferral records NO IngestionAttempt (matches
   the frozen numeric pass exactly)
6. staging cleanup is unconditional at both the start and end of every
   claim attempt, proven by a test asserting zero residual staging
   directories after success, failure, and simulated-crash-then-
   recovery
7. member-level source_category/workload_category/risk_tier_estimated
   are populated at member-creation time, using the exact calls
   specified in section 7 - classify_backup_sync_context is NOT called
   for archive members
8. risk_tier_actual is populated on the ROOT SourceInstance from real
   post-extraction evidence (section 7's exact formula), remaining
   NULL on any failure that precedes that evidence existing
9. SourceInstance.claim_generation exists, is incremented on every
   claim/reclaim (both claim_source_instance_for_identity_resolution
   AND claim_source_instance_for_archive_processing - the column lives
   on the model, not one use case), and release_source_instance_claim
   requires and fences on it - both existing call sites updated
10. the zombie-worker test (section 18) proves the FIX: a delayed
    worker's stale-generation release is a no-op and never clears a
    legitimately-reclaimed newer owner's claim - measured directly,
    not assumed
11. the pre-extraction member checks reject is_symlink AND is_junction
    AND is_socket (7z); the post-extraction file-type audit (section
    4a) exists as a general backstop and is proven to fire on at least
    one adversarial case
12. max_depth-exceeded produces exactly the frozen outcome (section 13):
    FAILED, OVERSIZED_OR_EXPANSION_LIMIT, retryable=False,
    risk_tier_actual NULL
13. ArchiveExtractor.SUSPICIOUS_EXPANSION_RATIO is removed (or, if a
    future need is found, given real defined semantics and its own
    test - never left as unwired dead code)
14. lease_duration for archive-processing claims is passed explicitly
    per batch class; Class 2's default (timedelta(minutes=10)) is used
    only for Class 2, with a documented rationale for any larger
    class's chosen value (no larger class's value is fabricated
    without at least a stated basis)
15. every test in section 18's matrix exists and passes
16. the full existing regression suite (861 passed, 0 skipped as of
    this design pass) still passes unchanged, plus all new focused/
    concurrency tests
17. zero real-T7 access, verified by grep across every changed file,
    exactly as every prior milestone's final verification has done
```

### 21. Mandatory invariants — re-reviewed after corrections (Design Correction Pass)

Each of the 20 authorization's mandatory invariants, re-checked against
the corrected design above:

```
1.  Real T7 is read-only.
    UNCHANGED - still holds; nothing in the corrections touches T7 access.
2.  Archive extraction writes only to an authorized isolated workspace.
    UNCHANGED - still holds; staging model unchanged in principle
    (section 3), only its member-safety detail was hardened (4a).
3.  No archive member can escape its staging root.
    STRENGTHENED - traversal checks unchanged, now reinforced by the
    is_junction addition and the post-extraction file-type audit (4a),
    which verifies the ACTUAL result rather than trusting member-type
    enumeration alone.
4.  Archive containers cannot be claimed by loose-file identity
    resolution.
    UNCHANGED - still holds (Milestone 1's not_archive_suffixed
    exclusion, untouched).
5.  Every materialized member has a complete provenance chain.
    UNCHANGED - still holds (section 9, untouched).
6.  Resume never creates duplicate SourceInstance or ProvenanceLink
    records.
    UNCHANGED at the ROW level (idempotent find-or-create was already
    sufficient for this specific guarantee) - but see #8: the
    CONCURRENT-WRITE risk that could have accompanied a wrongful claim
    reopening (never a duplicate-row risk per se) is now also closed.
7.  Existing evidence snapshots are preserved.
    UNCHANGED - still holds (section 7, untouched by the classification
    wiring, which uses dedicated columns, never evidence_snapshot).
8.  Zombie workers cannot mutate state after losing their claim
    generation.
    NOW SATISFIED FOR SourceInstance TOO - this is the invariant the
    correction pass actually closes. Originally true only for
    ContentIdentityGroup; section 2's SourceInstance.claim_generation
    design extends it uniformly to archive-processing (and identity-
    resolution) claims.
9.  Resource limits fail closed.
    UNCHANGED - still holds (pre-flight admission, section 4) -
    REINFORCED by the new post-extraction file-type audit, itself a
    fail-closed mechanism (4a).
10. Stop means STOP, never skip-and-continue.
    UNCHANGED - still holds, precisely scoped in section 16 (governs
    future admission, never an already-atomic in-flight extractall()).
11. Failed extraction cannot be represented as successful extraction.
    STRENGTHENED - now also explicitly true for max_depth-exceeded
    (section 13's frozen FAILED outcome, never SUCCEEDED) and for the
    post-extraction file-type audit's own failures (4a).
12. EXCLUDED, UNSUPPORTED, and FAILED remain semantically distinct.
    STRENGTHENED - reinforced by the max_depth fix (correctly FAILED,
    never misclassified toward MALFORMED_ARCHIVE, never confusable
    with UNSUPPORTED/EXCLUDED, section 13).
13. Duplicate content convergence never destroys physical provenance.
    UNCHANGED - still holds (section 15, untouched).
14. Cleanup is workspace-only.
    UNCHANGED - still holds (section 12, untouched).
15. No OS permission is relied upon as the safety boundary.
    STRENGTHENED - the post-extraction file-type audit (4a) verifies
    the actual on-disk result directly, rather than relying solely on
    zipfile's/py7zr's internal behavior (or the OS's own permission
    model) to have prevented an unsafe entry type.
```

**Net result**: zero invariants weakened; one (#8) newly satisfied for
a model it did not previously cover; four (#3, #9, #11, #12, #15)
strengthened by a second, independent verification layer rather than
resting on a single mechanism. None of the five items the review asked
to be explicitly preserved (the staging-vs-promotion distinction,
section 3; the three-way declared/actual/peak accounting distinction,
section 4; source_category vs. workload_category, section 7) were
touched by any correction - each remains exactly as originally frozen.

**This design pass authorizes no code, no migration, no test-code
changes, and no real-T7 access.** The next gate, not yet opened, is
either a further design-review round (if any of the above needs
correction) or a separate, explicit implementation authorization for
Milestone 5.

## Scaled Real-T7 Ingestion — Milestone 6 Design: Identity Resolution & Embedding-Reservation Batch Integration (DESIGN ONLY — no code, no migration, no real-T7 access authorized by this section)

Design pass for Implementation Milestone 6, opened after Milestone 5
(`6acdc53`) was committed and closed. This section establishes the M6
boundary by direct inspection of the current repository — not from an
assumed roadmap item — and freezes the design for that boundary before
any implementation is authorized.

**Design Correction Pass applied**: the initial pass specified *how* an
owning batch is resolved (lowest-`id` tie-break, section 4) but left
*what that ownership means* — its persistence across the owning batch's
own later status transitions, its interaction with `claim_generation`
fencing and stale-reservation recovery, and whether it carries any
priority beyond arbitration — unresolved. Review correctly declined to
freeze the design on that basis. Section 4a, added by this correction,
resolves all ten review points explicitly. Nothing in section 4a
changes section 3's pseudocode, section 5's "no schema change," or any
other section — it is a semantics clarification of already-specified
(and, for the reservation primitives themselves, already Milestone-4-built)
mechanism, never a new mechanism.

### 0. Establishing the boundary — what the repository actually shows

The frozen "Scaled Real-T7 Ingestion — Implementation Design Pass"
(`2fab4b3`), section 18, names an exact 12-step implementation order.
Mapping what is actually committed today against that order:

```
1. Schema/model design review                         -> done (Milestone 1, cc8dbec)
2. Migration                                            -> done (Milestone 1, cc8dbec)
3. Batch creation service                               -> done (Milestone 2, 4709ffd)
4. Policy evaluator                                     -> done (Milestone 2, 4709ffd)
5. WorkerClaimService extension (classification_run_id) -> done (Milestone 4, 6513948)
6. BatchResourceGuard                                   -> done (Milestone 3, ca3ab4e)
7. Extracted-bytes + embedding reservation (primitives) -> done (Milestone 4, 6513948:
                                                            embedding reservation lifecycle;
                                                            Milestone 5, 6acdc53: extracted-
                                                            bytes reservation + staging)
8. Runtime accounting                                   -> done (Milestone 3, ca3ab4e)
9. Worker/claim integration (the loop that ties 3-8
   together per batch)                                  -> PARTIAL — done for archive
                                                            processing only (Milestone 5);
                                                            NOT done for identity resolution
                                                            or embedding
10. Archive-processing integration                      -> done (Milestone 5, 6acdc53)
11. BatchReportService                                  -> NOT STARTED — no such service
                                                            exists anywhere in app/classification/
12. Adversarial/concurrency tests (final milestone)     -> NOT STARTED (correctly: this is
                                                            the LAST step, gated on 9 and 11)
```

Milestone 5 completed step 10 (archive-processing integration) ahead of
step 9 in the frozen numbering — an explicit, already-authorized and
already-closed divergence from the written order, not something this
pass reopens or corrects. What step 10 actually did for archive
processing (`classification_run_id`-scoped claim, `BatchResourceGuard`
admission, atomic pre-flight reservation, post-work reconciliation) is
**exactly the shape of integration step 9 still owes to the two
pipeline stages Milestone 5 did not touch**: identity resolution and
embedding. This is confirmed by reading the current code directly, not
assumed:

- `WorkerClaimService.claim_source_instance_for_identity_resolution`
  already accepts an optional `classification_run_id: int | None = None`
  (added by Milestone 4, per its own docstring) and already applies the
  exact frozen predicate from section 10 of the Implementation Design
  Pass when given one. But `IdentityResolutionService.resolve_next` —
  the only caller — never accepts or passes this parameter. The
  primitive is batch-aware; the service that uses it is not.
- `WorkerClaimService.reserve_embeddings` / `consume_embedding_
  reservation` / `release_embedding_reservation` are a complete,
  already-frozen (section 8 of the Implementation Design Pass),
  already-built (Milestone 4) fenced reservation lifecycle for
  `IngestionBatch.max_embeddings`/`embeddings_reserved`. **Verified
  directly: zero call sites in `app/` invoke any of these three
  methods.** `PipelineEmbeddingService.embed_next` calls
  `EmbeddingClient.embed()` unconditionally, with no reservation, no
  envelope check, and no `BatchResourceGuard` consultation of any kind.
  This means `max_embeddings` — one of the five envelope fields a real
  ingestion batch is bounded by — is **completely unenforced** by any
  code path that actually calls the embedding backend today.
- `NormalizationService.normalize_next` and `ChunkingService.
  chunk_next` both call `WorkerClaimService.claim_content_identity_
  group`, which the frozen design (Implementation Design Pass, section
  19, "DECIDED/FROZEN") explicitly and deliberately keeps **global** —
  "`claim_content_identity_group` remains a *global* claim (no batch
  filter)... a known, accepted imprecision in cross-batch budget
  attribution... not a correctness bug." No `classification_run_id`
  parameter exists on this method, and none is missing — the design
  already decided this. Normalization and chunking therefore need **no**
  batch-integration change; adding one would contradict frozen design,
  not complete it.
- `BatchReportService` (step 11) does not exist. Building it is a
  larger, independent unit of work (reporting/reconciliation across the
  whole batch, not a per-item pipeline change) and depends on nothing
  this pass touches; pursuing it here would be scope expansion beyond
  "the smallest necessary next milestone."

**Conclusion**: the smallest, most necessary, most concretely evidenced
next boundary is completing step 9 for exactly the two stages Milestone
5 did not cover — identity resolution and embedding — using the same
integration pattern already reviewed and closed for archive processing.
This is Milestone 6's scope. It is not real-T7 execution, not
`BatchReportService`, and not a change to normalization/chunking's
already-frozen global-claim behavior.

**A related, real documentation gap noted but explicitly NOT corrected
by this pass** (per this authorization's scope: documentation changes
are limited to the Milestone 6 design specification and its roadmap
status): the Milestone 5 commit (`6acdc53`) added a "Milestones 1–4 —
committed" roadmap entry but never added a corresponding "Milestone 5 —
committed" entry, or an implementation-report architecture section
analogous to this one — the Roadmap's Milestone 5 bullet still reads
"design-only pass produced, not yet reviewed or frozen," which is now
stale. This is flagged for a future, separately-authorized documentation
correction; fixing it here would be scope expansion beyond Milestone 6.

### 1. Objective and scope

Make identity resolution and embedding — the two remaining
non-archive, per-item pipeline stages with a Milestone-4-built,
batch-aware primitive sitting unused beneath them — actually batch-aware
and (for embedding) envelope-honest, mirroring exactly what Milestone 5
did for archive processing. Concretely:

- **A. Identity resolution**: `IdentityResolutionService.resolve_next`
  gains an optional `classification_run_id: int | None = None` parameter,
  threaded straight into the existing `claim_source_instance_for_
  identity_resolution` call. `None` preserves exact pre-Milestone-6
  behavior for any caller that omits it (the same backward-compatibility
  contract every prior milestone's optional parameters used).
- **B. Embedding**: `PipelineEmbeddingService.embed_next` gains an
  optional `guard: BatchResourceGuard | None = None` parameter and is
  wired to the already-frozen, already-built reservation lifecycle:
  resolve the owning batch (new logic, specified in section 4 below),
  reserve before calling `EmbeddingClient.embed()`, consume on success,
  release on failure — never calling `embed()` without a successful
  reservation when an owning `RUNNING` batch exists.
- **Explicitly NOT in scope**: `NormalizationService`, `ChunkingService`
  (frozen global-claim design, unchanged); `BatchReportService`; a
  scheduler-wide worker-orchestration loop; any new `ExpensiveOperationKind`
  value (identity resolution reading T7 bytes was never named as an
  "expensive operation" checkpoint anywhere in the frozen design —
  inventing one now would be a new resource-guard policy, not a wiring
  completion); any change to `claim_content_identity_group`'s
  global-claim scoping; any new `IngestionBatch` or `SourceInstance`
  column; any migration; any real-T7 access.

### 2. Existing capability being reused

Entirely reused, unchanged:
- `WorkerClaimService.claim_source_instance_for_identity_resolution`'s
  existing `classification_run_id` parameter and predicate (Milestone
  4) — this pass adds a caller, not a capability.
- `WorkerClaimService.reserve_embeddings` / `consume_embedding_
  reservation` / `release_embedding_reservation` and the full fenced
  reservation lifecycle they implement (Milestone 4, Implementation
  Design Pass section 8) — this pass adds the first real caller, not a
  capability.
- `BatchResourceGuard.check_before_expensive_operation(batch,
  ExpensiveOperationKind.EMBEDDING)` (Milestone 3) — already accepted
  as an optional parameter by `reserve_embeddings` itself; this pass
  threads it from `PipelineEmbeddingService.embed_next` down to that
  existing call, exactly as `ArchiveProcessingService` already does for
  `ExpensiveOperationKind.ARCHIVE_EXTRACTION` (Milestone 5).
- `DocumentChunk.embedding IS NULL` as the idempotent unit-of-work
  signal (existing, unchanged) — the count of such rows at claim time is
  the reservation's `n`.

### 3. Exact new capability

**A. `IdentityResolutionService.resolve_next`**:
```
def resolve_next(
    self, *, worker_id: str, workspace_root: Path,
    lease_duration: timedelta = timedelta(minutes=10),
    classification_run_id: int | None = None,
) -> SourceInstance | None:
    instance = self.claims.claim_source_instance_for_identity_resolution(
        worker_id=worker_id, lease_duration=lease_duration,
        classification_run_id=classification_run_id,
    )
    ...  # unchanged below this line
```
No other change to this service. `release_source_instance_claim`
already fences on `claim_generation` (Milestone 5) regardless of how
the claim was scoped, so nothing downstream of the claim needs to
change.

**B. `PipelineEmbeddingService.embed_next` / `_embed_claimed_group`**:
```
def embed_next(
    self, *, worker_id: str,
    lease_duration: timedelta = timedelta(minutes=10),
    guard: BatchResourceGuard | None = None,
) -> ContentIdentityGroup | None:
    group = self.claims.claim_content_identity_group(...)   # unchanged
    if group is None:
        return None
    try:
        self._embed_claimed_group(group, worker_id=worker_id, guard=guard)
    except Exception:
        self.claims.release_content_identity_group_claim(...)  # unchanged
        raise
    ...
```

`_embed_claimed_group`, new sequencing (pseudocode; transactional steps
matter here):
```
document = <existing lookup; unchanged, including the "no Document" failure path>

unembedded_chunks = <existing query: DocumentChunk WHERE document_id=... AND embedding IS NULL>
n = len(unembedded_chunks)

if n == 0:
    # Nothing left to embed - e.g. a crash-resume where a prior attempt
    # already embedded everything but did not advance pipeline_state.
    # No reservation is needed for zero work. Existing behavior, unchanged.
    goto <existing "record success, release with final_state" tail>

owning_batch_id = _resolve_owning_running_batch(group.id)   # NEW, section 4

if owning_batch_id is not None:
    outcome = self.claims.reserve_embeddings(
        group_id=group.id, my_generation=group.claim_generation,
        batch_id=owning_batch_id, n=n, guard=guard,
    )
    if not outcome.reserved:
        # reserve_embeddings has ALREADY released the claim on every
        # denial path (its own existing contract, unchanged). No
        # IngestionAttempt is recorded - this is a clean deferral, not
        # a failure of the item itself, exactly matching the frozen
        # "item deferred" outcome (Implementation Design Pass section 8).
        return   # embed_next returns None-equivalent to its caller
    reserved = True
else:
    # No RUNNING batch currently owns this group (e.g. every batch whose
    # SourceInstance(s) fed it has since paused/completed/aborted, or -
    # structurally impossible today since PipelineEmbeddingService has
    # no non-batch caller, but kept as an explicit, named case rather
    # than an unstated assumption). Proceed unguarded/unreserved -
    # embedding work must never silently strand a Document at CHUNKED
    # forever merely because no batch is currently claiming ownership of
    # its cost accounting (see section 4's explicit invariant).
    reserved = False

try:
    vectors = self.embedding_client.embed([c.content for c in unembedded_chunks])
except Exception as exc:
    if reserved:
        self.claims.release_embedding_reservation(group_id=group.id, my_generation=group.claim_generation)
        # release_embedding_reservation ALSO releases the claim (existing
        # contract) - do not call release_content_identity_group_claim again.
        self._record_failure(...)   # existing _fail() body, but do not
                                      # double-release the claim
        return
    else:
        <existing _fail() path, unchanged>
    return

for chunk, vector in zip(unembedded_chunks, vectors):
    chunk.embedding = vector
self.db.commit()

<existing: record SUCCEEDED attempt>
<existing: compute remaining_unembedded, final_state>

if reserved:
    self.claims.consume_embedding_reservation(
        group_id=group.id, my_generation=group.claim_generation,
        new_pipeline_state=final_state,
    )
    # consume_embedding_reservation ALSO releases the claim with the new
    # state (existing contract) - do not call release_content_identity_
    # group_claim again for this path.
else:
    <existing: self.claims.release_content_identity_group_claim(..., new_pipeline_state=final_state)>
```

The two release paths (`release_embedding_reservation` /
`consume_embedding_reservation` vs. the plain
`release_content_identity_group_claim`) are mutually exclusive per
attempt, selected by whether a reservation was actually taken — an
implementation-time discipline this design calls out explicitly to
prevent a double-release-claim bug (the fenced `WHERE claim_generation
= :my_generation` on the second call would simply no-op harmlessly if
this were gotten wrong, per every release method's existing "safe
no-op, never an exception" contract, but the intent must still be
implemented correctly, not left to that safety net).

### 4. Lifecycle / state transitions

No new `ContentPipelineState` values. The existing
`CHUNKED -(claim)-> [reserve] -(embed)-> EMBEDDED|INGESTED -(consume, releases claim)`
skeleton (Implementation Design Pass, section 8) is now actually
exercised end to end for the first time, exactly as originally frozen;
this pass adds no new state.

**New logic (not previously specified anywhere): resolving which batch
owns a `ContentIdentityGroup`'s embedding cost.**

```
def _resolve_owning_running_batch(group_id: int) -> int | None:
    SELECT ib.id
    FROM ingestion_batches ib
    JOIN source_instances si ON si.classification_run_id = ib.classification_run_id
    WHERE si.content_identity_group_id = :group_id
      AND ib.status = 'RUNNING'
    ORDER BY ib.id ASC
    LIMIT 1
```

**Determinism rule, frozen here**: when more than one `RUNNING` batch's
`SourceInstance` rows resolve to the same shared `ContentIdentityGroup`
(the already-proven, already-accepted cross-batch convergence case —
Implementation Design Pass section 10), the batch with the lowest `id`
(equivalently, the earliest-created batch, mirroring this codebase's
existing `ORDER BY created_at`/`id ASC` convention for "earliest wins"
selection) is charged. This is a new, previously-unspecified tie-break
rule this design pass freezes explicitly, rather than leaving the
"whichever batch's worker claims it first" language of section 10
under-specified at the SQL level. It governs only *which ledger records
the cost* — never which worker performs the (single, deduplicated) work,
which remains governed entirely by `claim_content_identity_group`'s own
existing `SELECT ... FOR UPDATE SKIP LOCKED` exclusivity.

**Explicit invariant, frozen here (also previously unspecified)**: if no
`RUNNING` batch currently owns the group (every owning batch has since
`PAUSED`/`COMPLETED`/`ABORTED`), embedding work proceeds **without** a
reservation — the item is never blocked or deferred merely for lack of
an active batch to charge. Rationale: `claim_content_identity_group` is
frozen as a global claim specifically so shared content converges and
completes regardless of any one batch's lifecycle; making embedding
completion conditional on a currently-`RUNNING` owning batch would
strand such a group at `CHUNKED` indefinitely whenever its only owning
batch(es) finish first — a real regression this design explicitly
avoids, not an oversight.

### 4a. Reservation-ownership semantics — CORRECTED / RESOLVED (Design Correction Pass)

**Correction context**: review correctly identified that section 4, as
originally written, specified *how* an owning batch is picked but never
resolved *what kind of ownership* that pick establishes, nor what
happens to it across the owning batch's own subsequent lifecycle
transitions. Both bear directly on `reserved_embeddings_batch_id`'s
durability and on stale-reservation recovery, so this cannot be left
implicit. The ten points below are resolved explicitly; nothing here
changes section 3's pseudocode or section 5's "no schema change" — this
is a semantics clarification of already-specified mechanism, not a new
mechanism.

**1. Only `RUNNING` batches are eligible owners — yes, explicitly, and
enforced twice.** `_resolve_owning_running_batch` (section 4) filters
`ib.status = 'RUNNING'` as its *candidate-selection* check. Independently,
`reserve_embeddings`'s own existing atomic `UPDATE ... WHERE id = :batch_id
AND status = 'RUNNING' AND embeddings_reserved + n <= max_embeddings`
(Milestone 4, unchanged by this pass) re-checks the identical condition
at the *instant of reservation*. A batch that is `PLANNED`, `PAUSED`,
`COMPLETED`, or `ABORTED` can never become a NEW reservation's owner
under any circumstance — both checks agree, and the second is the one
that actually matters for correctness (point 5 covers the gap between
them).

**2. The calling worker's own batch gets NO preference — there is no
such concept at this claim layer.** Unlike
`claim_source_instance_for_archive_processing` (which takes an explicit
`classification_run_id` naming the calling worker's batch),
`claim_content_identity_group` is frozen (Implementation Design Pass,
section 19) as a global claim with no batch-identifying parameter at
all. A worker calling `embed_next()` has no associated batch to prefer —
the method has no input through which one could even be named. If a
future, separately-scoped milestone builds a batch-scoped orchestration
loop that wants to bias `embed_next()` toward a specific batch's
content, that is new scope for that milestone (a new optional parameter
on `embed_next()`), not something M6 introduces or assumes.

**3 & 4. Why lowest `id`, and it is arbitration only — never business
priority.** Lowest `id` is chosen *solely* because it is cheap,
deterministic, and reproducible across repeated test runs and real
concurrent contention (an `ORDER BY ib.id ASC LIMIT 1` always returns
the same row for the same candidate set, independent of thread
scheduling) — matching this codebase's existing convention of using
creation order as an arbitrary-but-reproducible tie-break elsewhere
(e.g. `claim_content_identity_group`'s own `ORDER BY created_at` for
which stale/eligible row is claimed first). **It carries no semantic or
business meaning whatsoever**: it does NOT mean the lowest-id batch is
"more important," "should finish first," or has any claim-priority over
the group's actual processing — that is governed entirely and
separately by `claim_content_identity_group`'s own
`SELECT ... FOR UPDATE SKIP LOCKED` exclusivity, unaffected by which
batch's ledger happens to be charged. This is exactly the same
"known, accepted imprecision in cross-batch budget attribution... not a
correctness bug" the frozen Implementation Design Pass (section 10)
already named for `claim_content_identity_group`'s own global-claim
decision — this tie-break rule is that same accepted imprecision, made
concrete and reproducible rather than left as unspecified prose.

**5. If the resolved (lowest-id) batch is no longer `RUNNING` by the
time `reserve_embeddings` actually executes**: `_resolve_owning_running_batch`
(a plain `SELECT`) and `reserve_embeddings`'s atomic `UPDATE` are two
separate statements, so a race window exists between them. This is
already fully handled, not a new gap: `reserve_embeddings`'s own
`WHERE status = 'RUNNING'` re-check (point 1) will simply fail to match
if the batch left `RUNNING` in that window, and the method returns
`reserved=False` with `denial_reason=BATCH_NOT_RUNNING` (existing
`ReservationDenialReason` value, Milestone 4) — the claim is released
(existing contract) and the item is deferred (section 3/12's "clean
deferral" semantics, no `IngestionAttempt` recorded). **No in-attempt
fallback to a second candidate is attempted** — see point 6. The next
independent claim attempt (by any worker, at any later time) re-runs
`_resolve_owning_running_batch` fresh and will naturally resolve to
whatever batch is now the lowest-id `RUNNING` owner, or `None` if none
remains.

**6. A non-owner batch's spare capacity is never substituted, even
within the same attempt.** Resolution picks exactly one candidate. If
that specific batch denies (not `RUNNING`, envelope exhausted, or
guard-denied), the whole attempt defers — `embed_next()` does not loop
over other `RUNNING` batches that also reference the group and retry
their capacity instead. This is a **deliberate, named scope boundary**,
not an oversight: multi-candidate fallback would require either
retrying `reserve_embeddings` against successive candidates within one
call (added control-flow complexity for a rare edge case — simultaneous
cross-batch convergence *and* the specific resolved batch being
exhausted/stopped at the exact moment of reservation) or a
priority-ordered capacity-borrowing scheme neither the frozen design nor
this pass ever specifies. Deferring and letting a later attempt
re-resolve (point 5) already guarantees eventual progress once *any*
`RUNNING` batch remains eligible, or the "no owner" path (section 4's
existing invariant) takes over once none do — correctness is preserved,
only latency in the rare contended case is traded for simplicity, an
explicit exclusion added to section 17.

**7. What happens when the reservation owner transitions to `PAUSED`,
`ABORTED`, or `COMPLETED` *after* a reservation was already granted**:
the reservation's fate becomes **entirely decoupled from the owning
batch's live status** the instant `reserve_embeddings` commits — this is
already-frozen, already-built behavior (Implementation Design Pass
section 8; Milestone 4 code, unchanged by this pass), not new to M6.
Explicitly, quoting the existing frozen guarantee: *"reservations are
not released merely because the batch pauses — they represent real,
potentially still-in-flight work; only the stale-claim-triggered
recovery path ever clears one, always fenced to the generation it
actually belongs to."* Concretely: `consume_embedding_reservation`
(success) and `release_embedding_reservation` (failure) both check
**only** `claim_generation` — neither reads nor cares about the owning
batch's current `status` — so a worker that successfully reserved
against a batch that has since paused, aborted, or completed still
finishes normally (consume) or fails normally (release) exactly as if
nothing had changed. The same applies to abandoned-reservation recovery
(point 8): its own existing docstring states crediting back is
*"unconditional on the owning batch's status (even an already-`ABORTED`
batch's counter is still correctly reconciled — this is bookkeeping
correction, never a new-work admission decision)."* A batch leaving
`RUNNING` therefore only ever affects **future** reservation attempts
against it (point 1); it never retroactively invalidates, cancels, or
reassigns a reservation already granted.

**8. How `reserved_embeddings_batch_id` is cleared/recovered — three
existing paths, none introduced or altered by M6**:
```
(a) consume_embedding_reservation (success):
    fenced UPDATE ... WHERE id=:group_id AND claim_generation=:my_generation
    SET reserved_embeddings=NULL, reserved_embeddings_batch_id=NULL
    -- IngestionBatch.embeddings_reserved is NOT decremented (monotonic -
    -- real, completed work; unchanged from the frozen design)

(b) release_embedding_reservation (embed() failure, or any explicit release):
    SELECT reserved_embeddings, reserved_embeddings_batch_id FOR UPDATE
      WHERE id=:group_id AND claim_generation=:my_generation
    -- read FRESH, never a caller-supplied/remembered batch_id
    UPDATE ... SET reserved_embeddings=NULL, reserved_embeddings_batch_id=NULL
    UPDATE ingestion_batches SET embeddings_reserved = embeddings_reserved - :n
      WHERE id = <the batch_id just read fresh>

(c) abandoned-reservation recovery (built into claim_content_identity_group's
    own reclaim UPDATE, Milestone 4 - triggered when a NEW claim attempt
    reclaims a row whose claimed_at < stale_before while a reservation is
    still present):
    the SAME transaction that grants the new claim_generation reads the
    STALE row's reserved_embeddings/reserved_embeddings_batch_id fresh
    under the row lock, credits that exact amount back to that exact
    batch, and clears both columns - BEFORE the new generation's owner
    does anything
```
All three read `reserved_embeddings_batch_id` **fresh from the row**,
never from a value any caller remembers or passes in — this is precisely
what makes all three safe regardless of how much time, or how many
owning-batch status transitions, have elapsed since the reservation was
granted (point 7).

**9. How `claim_generation` fences stale workers from consuming or
releasing a reservation they no longer own**: every mutating reservation
operation — `reserve`, `consume`, `release`, and recovery's own internal
clear — includes `AND claim_generation = :my_generation` in its `WHERE`
clause against `ContentIdentityGroup`. A worker captures `:my_generation`
once, from the value its own `claim_content_identity_group` call
returned, and holds it for that attempt's lifetime. If the row is
reclaimed in the meantime (stale-claim recovery bumps `claim_generation`
to N+1, per point 8c), the original worker's later `consume`/`release`
call — still carrying generation N — matches **zero rows**: a safe,
silent no-op, never an exception, and structurally incapable of
touching generation N+1's live claim or reservation. This is the exact,
already-built (Milestone 4) fencing mechanism `SourceInstance.
claim_generation` (Milestone 5) later generalized for archive/identity
claims — M6 does not add or modify this mechanism, it is simply the
first pass whose new call site (`PipelineEmbeddingService`) actually
exercises it against real reservation traffic end to end.

**10. Preserved, stated explicitly as a standing principle governing
every point above**: content identity is globally convergent — a
property of bytes, never batch-scoped (Implementation Design Pass,
section 10). Embedding-reservation ownership
(`reserved_embeddings_batch_id`) is a **fourth, orthogonal concept**,
never to be conflated with the three already-frozen ones (batch
membership / content-identity convergence / worker claim ownership):
it is pure **resource-control cost attribution** — *which batch's
ledger pays for one specific, already-exclusively-claimed reservation*
— and confers **no ownership of, or priority over, the content itself**.
Whichever batch is charged, the underlying `ContentIdentityGroup` is
processed exactly once, by exactly one worker, exactly as the
already-proven global-claim exclusivity guarantees; the reservation
ledger is bookkeeping, never an admission or priority mechanism over
the work itself.

### 5. Database changes

None. No migration. No new columns, tables, indexes, or enum values.
Both new call sites (`_resolve_owning_running_batch`'s `SELECT`,
`reserve_embeddings`'s existing `UPDATE`s) use only already-existing
columns (`source_instances.classification_run_id`,
`source_instances.content_identity_group_id`, `ingestion_batches.id`,
`ingestion_batches.status`).

### 6. Worker/claim interactions

Unchanged claim mechanics for both stages — this milestone changes only
which parameters are *passed into* already-existing, already-fenced
claim/reservation methods, never the methods' own locking or fencing
logic. `claim_source_instance_for_identity_resolution`'s
`classification_run_id`-scoped `SELECT ... FOR UPDATE SKIP LOCKED`
behavior is exactly as Milestone 4 built and Milestone 5's tests already
exercise for the sibling `claim_source_instance_for_archive_processing`
method — no new proof of that mechanism itself is required, only proof
that `IdentityResolutionService` correctly passes the parameter through
(a wiring test, not a concurrency-primitive test).

### 7. Batch/resource-control interactions

- Identity resolution: no numeric envelope, no `BatchResourceGuard`
  checkpoint (none was ever named for this stage in the frozen design —
  see section 1's explicit "not in scope" note). Batch interaction is
  admission-only: a `classification_run_id`-scoped claim will not match
  a `SourceInstance` whose batch is not `RUNNING` (the existing predicate,
  re-checked in the `UPDATE`'s own `WHERE`, per Milestone 4's docstring).
- Embedding: full `max_embeddings`/`embeddings_reserved` envelope
  enforcement via `reserve_embeddings`, including its own existing
  `guard.check_before_expensive_operation(batch, EMBEDDING)` consultation
  (Ollama-reachability tiering, already built) — now actually reachable
  from a real embedding call for the first time.

### 8. Idempotency and retry semantics

Unchanged and already sufficient, verified by reading the existing code
paths this design reuses:
- Identity resolution: `resolve_next`'s existing `finally`-fenced
  release and write-once `content_identity_group_id` assignment are
  untouched; adding a claim-scoping parameter changes only which rows
  are eligible to be claimed, never the resolution logic's own
  idempotency.
- Embedding: `WHERE embedding IS NULL` chunk selection (existing,
  unchanged) remains the sole resumability mechanism — a crash after
  reserving but before `embed()` completes leaves the reservation to be
  recovered by the *existing*, already-Milestone-4-proven abandoned-
  reservation recovery path inside `claim_content_identity_group`
  (credits `embeddings_reserved` back, clears the reservation, advances
  the generation) the next time this group is claimed — no new recovery
  logic is introduced by this pass.

### 9. Crash/recovery behavior

No new crash scenario is introduced. The two crash points this pass's
new code path adds exposure to were both already designed for by
Milestone 4:
- Crash after `reserve_embeddings` returns `reserved=True` but before
  `embed()` returns: reservation is recovered via the existing
  abandoned-reservation path on the next claim of this row (unchanged
  mechanism, new exerciser).
- Crash after `embed()` succeeds but before `consume_embedding_
  reservation` commits: chunks already have embeddings written (the
  `for chunk, vector in zip(...): chunk.embedding = vector; self.db.commit()`
  step happens first); the reservation is still outstanding and will be
  recovered the same way; a later resumed attempt's `unembedded_chunks`
  query will find zero remaining rows (`n == 0` path, section 3) and
  proceed straight to state advancement — no duplicate embedding call,
  no lost work.

### 10. Concurrency behavior

Two properties require proof beyond what Milestone 4 already proved for
the reservation primitives themselves:
1. **Wiring correctness**: `PipelineEmbeddingService` actually calls
   `reserve_embeddings` before `embed()` and never calls `embed()` after
   a denied reservation — a direct-call/mock-boundary test, not a
   concurrency test.
2. **Deterministic batch attribution under real concurrency**: two real
   `IngestionBatch` rows, both `RUNNING`, each with its own
   `SourceInstance` resolving (via identity convergence) to the SAME
   `ContentIdentityGroup`; two real workers race `embed_next()` via
   `threading.Barrier` against real Postgres. Assert: exactly one
   worker's `embed()` call happens (existing global-claim exclusivity,
   already proven for `claim_content_identity_group` generally — this
   test is the first to also assert the *specific* batch charged), and
   the batch charged is always the lower-`id` batch, reproducibly across
   repeated runs (proving section 4's tie-break rule is genuinely
   deterministic under contention, not merely in the single-threaded
   case).

### 11. Provenance preservation

Untouched. Neither change reads, writes, or reinterprets
`ProvenanceLink`, `root_t7_path`, or `member_path`. Identity resolution's
existing "read `root_t7_path` exactly once" contract is unaffected by
adding a claim-scoping parameter.

### 12. Failure taxonomy

No new `IngestionFailureCode` value. A denied reservation is explicitly
**not** a failure — no `IngestionAttempt` row is recorded for it (per
the frozen "item deferred" semantics, section 3 above), exactly
matching how `ArchiveProcessingService`'s own pre-flight denial (Milestone
5) is already handled: a clean, silent deferral, retried on a future
claim, never a durable `FAILED` outcome. An `embed()` exception after a
successful reservation still records the existing
`EMBEDDING_UNAVAILABLE` failure code, unchanged, with the added step of
releasing the reservation first.

### 13. Observability/audit requirements

No new audit surface. `IngestionAttempt` rows continue to record exactly
what they do today for both stages. The one new piece of derived state
(which batch was charged for a given embedding reservation) is already
fully durable and queryable via the existing
`content_identity_groups.reserved_embeddings_batch_id` column during
the reservation's lifetime — no new column is needed to audit this
pass's new behavior.

### 14. Security/safety boundaries

Unchanged. Neither change touches T7 access, file-type validation, or
any OS-permission-adjacent logic — pure database/service wiring,
entirely synthetic-testable, matching every other milestone's boundary
in this chain.

### 15. Synthetic test strategy

All new/updated tests are pure `ArchiveExtractor`-free, T7-free,
synthetic-fixture tests against `aibrain_test`, matching this project's
established pattern:
```
1.  resolve_next(classification_run_id=None) behaves exactly as before
    (regression, existing fixture reused).
2.  resolve_next(classification_run_id=X) only claims SourceInstances
    under run X - a sibling SourceInstance under run Y is never claimed.
3.  resolve_next(classification_run_id=X) does not claim a SourceInstance
    under run X whose IngestionBatch is not RUNNING.
4.  embed_next() with n==0 unembedded chunks: no reservation attempted,
    existing success path taken unchanged.
5.  embed_next() with an owning RUNNING batch and available envelope:
    reserve -> embed -> consume, embeddings_reserved increases by n,
    reserved_embeddings/reserved_embeddings_batch_id return to NULL after
    consume.
6.  embed_next() with an owning RUNNING batch whose envelope is already
    exhausted: reservation denied, claim already released (verify via a
    fresh claim by a second worker succeeding immediately after), no
    IngestionAttempt row created, no embed() call made (assert via a
    call-counting stub EmbeddingClient).
7.  embed_next() with guard denying (HARD_STOP and SOFT_STOP both):
    same deferral shape as (6), verified via a stub guard.
8.  embed_next() where embed() raises after a successful reservation:
    release_embedding_reservation is invoked, embeddings_reserved
    returns to its pre-reservation value, FAILED IngestionAttempt
    recorded, claim released.
9.  embed_next() where NO RUNNING batch owns the group (all owning
    batches PAUSED/COMPLETED/ABORTED): embedding proceeds unreserved,
    completes normally, no batch counter touched (regression proof for
    section 4's explicit invariant).
10. Two SourceInstances under two different RUNNING batches converge on
    one ContentIdentityGroup: _resolve_owning_running_batch returns the
    lower batch id, deterministically, across repeated calls.
11. Real-Postgres concurrency (threading.Barrier, per this project's
    standard, repeated 8-10x): two workers race embed_next() for the
    cross-batch-converged group above; exactly one embed() call happens;
    the charged batch is always the lower-id batch.
12. Stale/abandoned embedding reservation recovery (existing Milestone 4
    mechanism) is exercised via this new call site at least once, end to
    end, rather than only via the lower-level WorkerClaimService tests -
    confirms the wiring does not accidentally bypass it.
```

### 16. Acceptance criteria

- `IdentityResolutionService.resolve_next` accepts and correctly
  threads `classification_run_id`; all pre-existing identity-resolution
  tests pass unchanged (regression).
- `PipelineEmbeddingService.embed_next` never calls
  `EmbeddingClient.embed()` without either (a) a successful reservation
  against a resolved `RUNNING` owning batch, or (b) confirmation that no
  `RUNNING` batch currently owns the group.
- `IngestionBatch.embeddings_reserved` is verifiably incremented and
  never exceeds `max_embeddings` under the new call path (DB
  `CHECK`/atomic-`UPDATE` guarantee, now actually exercised).
- All 12 scenarios in section 15 pass, including the two real-Postgres
  concurrency scenarios (10-11) repeated without flakiness across
  multiple runs, per this project's non-negotiable concurrency-test
  standard.
- Zero changes to `NormalizationService`, `ChunkingService`,
  `ArchiveProcessingService`, `ArchiveExtractor`, any model, or any
  migration.
- Full existing regression suite (886 tests collected as of `6acdc53`)
  continues to pass, plus the new tests from section 15.
- No real-T7 access; no production `aibrain` migration.

### 17. Explicit exclusions

- `BatchReportService` (frozen step 11) — a separate, later milestone.
- Any scheduler-wide, cross-batch, or cross-worker orchestration loop —
  this milestone governs two individual claim/reservation call sites,
  exactly as Milestone 5 governed one recursive archive-claim operation;
  outer loop scheduling remains a future milestone's responsibility.
- Any change to `claim_content_identity_group`'s frozen global-claim
  scoping, or to `NormalizationService`/`ChunkingService`.
- Any new `ExpensiveOperationKind`, `IngestionFailureCode`,
  `ContentPipelineState`, or `BatchStopReason` value.
- Correcting the stale Milestone 5 roadmap/architecture status gap noted
  in section 0 — flagged, not fixed, here.
- Real-T7 ingestion of any kind.
- Adversarial/concurrency tests beyond what section 15 lists (the full
  frozen step-12 "final milestone" test suite remains gated on
  `BatchReportService` and any further per-stage integration existing
  independently of this pass).
- **Multi-candidate reservation fallback** (Design Correction Pass,
  section 4a, point 6): if the single resolved (lowest-id) owning batch
  denies a reservation, `embed_next()` does not retry against a second,
  also-eligible `RUNNING` batch within the same attempt — it defers and
  relies on a later independent claim to re-resolve. A priority-ordered,
  multi-candidate fallback scheme is out of scope for this milestone.

### 18. Unresolved questions / gap register

**DECIDED / FROZEN by this pass** (new design content, not previously
specified anywhere):
```
The owning-batch resolution query and its ASC-by-id determinism rule
(section 4) - previously only described in prose ("whichever batch
claims first") without an exact, reproducible algorithm.

The explicit "no RUNNING owning batch -> proceed unreserved, never
strand the item" invariant (section 4) - previously unstated; without
it, embedding completion for a group whose only owning batch(es) have
since stopped would have been an unspecified/ambiguous case.

The mutually-exclusive release-path discipline (section 3) between
release_embedding_reservation/consume_embedding_reservation (when a
reservation was taken) and the plain release_content_identity_group_
claim (when it was not) - implementation-time discipline, not a new
mechanism.

**Design Correction Pass additions (section 4a)** - the full reservation-
ownership semantics review required before freezing:
lowest-id resolution is arbitration only, never business/semantic
priority (points 3-4); RUNNING-only eligibility is enforced twice,
independently, at resolution and at reservation (point 1); no
"calling worker's batch" preference exists at this claim layer (point 2);
a resolved-but-since-non-RUNNING batch cleanly denies via the existing
BATCH_NOT_RUNNING path with no in-attempt fallback to a second candidate
(points 5-6, the latter a named, bounded scope exclusion - see section 17);
a granted reservation's fate is fully decoupled from the owning batch's
later status transitions, unconditionally, per already-existing Milestone
4 guarantees (point 7); all three clear/recover paths read reserved_
embeddings_batch_id fresh from the row, never from a remembered value
(point 8); claim_generation fences every reserve/consume/release/recover
operation identically to the existing SourceInstance/ContentIdentityGroup
claim mechanism, exercised end to end for the first time by this
milestone's new call site (point 9); embedding-reservation ownership is
a fourth, orthogonal concept (pure resource-cost attribution) never to
be conflated with batch membership, content-identity convergence, or
worker claim ownership (point 10).
```

**REQUIRES IMPLEMENTATION-TIME VERIFICATION**:
```
Exact SQL join shape/index usage for _resolve_owning_running_batch under
real query-planning (logically specified above, not yet run against
aibrain_test's actual indexes - source_instances.classification_run_id
and content_identity_group_id are both already indexed per existing
schema, but this pass does not re-verify the query plan).

Whether the cross-batch-convergence concurrency scenario (test 11) is
reproducible without flakiness at the same 8-10x repetition standard
already achieved for every prior milestone's concurrency tests - expected
based on the identical underlying SELECT ... FOR UPDATE SKIP LOCKED
mechanism, but not yet run.
```

**DELIBERATELY DEFERRED** (unchanged from earlier gates):
```
BatchReportService (frozen step 11); a scheduler-wide worker-orchestration
loop; the stale Milestone 5 roadmap/architecture "committed" status entry
(section 0); risk_tier_actual numeric boundaries; any real-T7 access -
all remain gated behind their own, separate future authorizations.
```

### 19. Safety verification

- No real-T7 access: this design pass reads and reasons about only
  already-committed repository code, the frozen architecture document,
  and synthetic/conceptual examples. No T7 path is scanned, listed,
  hashed, extracted, or referenced as a fixture anywhere in this section.
- No production DB modification: no migration is specified as anything
  other than "none" (section 5); no command was run against `aibrain`
  during this design pass — only read-only inspection commands
  (`alembic heads`/`current`, `git log`/`diff`/`show`, `pytest
  --collect-only`) were executed, all against `aibrain_test`'s
  migration head or the git history, never against `aibrain` itself.
- No application/test/migration changes: this pass produced
  documentation only (this section and the accompanying roadmap entry).
- Working tree changes are limited to `docs/architecture/AI_Brain_Architecture.md`
  and `docs/roadmap/Roadmap.md`.

**Design Correction Pass outcome**: all ten reservation-ownership
review points (section 4a) are now resolved explicitly — lowest-id
resolution is confirmed arbitration-only with no business/semantic
priority; RUNNING-only eligibility, ownership persistence across the
owning batch's later status transitions, `reserved_embeddings_batch_id`
clear/recovery, and `claim_generation` fencing are all stated precisely
and grounded in already-existing, already-built (Milestone 4) mechanism
rather than left implicit. The one bounded scope decision surfaced by
this correction (no multi-candidate reservation fallback) is recorded
as an explicit exclusion (section 17), not an open question.

**This design pass authorizes no code, no migration, no test-code
changes, and no real-T7 access.** The next gate, not yet opened, is a
separate, explicit implementation authorization for Milestone 6.

## Scaled Real-T7 Ingestion — Milestones 7–24: Report, Reconciliation, Orchestration, Operator CLI, HTTP Composition, Retrieval Determinism, Embedding Failure Handling, Mixed-Chain Retrieval Proof & Provenance-Rich Citations (implemented and committed)

Milestones 7–12 were each separately authorized, implemented, and
committed following Milestone 6, but — unlike Milestones 1–6 above —
were not preceded by their own standalone frozen design-pass section in
this document; each was gated, reviewed, and corrected directly against
the running repository. This section is Milestone 13's factual
reconciliation of what those six milestones actually built, grounded in
their own commit messages and the code as it exists today — it does not
reconstruct a design narrative that was never separately written down.

**Milestone 7 — `BatchReportService`** (`3136ffb`). A pure read/
aggregation service: `generate_report(batch_id) -> BatchReport` holds no
claim, takes no lock, writes nothing, and needs no T7 access. Counts are
scoped correctly to root-level `SourceInstance` rows only (`member_path
IS NULL` — archive members share their parent's `classification_run_id`
but were never part of `source_instances_selected`, and must not
contaminate these denominators). `risk_tier_actual`'s distribution
carries an explicit, visible `NULL` bucket rather than hiding Milestone
5's honest non-population of that field. `actual_source_bytes_read` and
the four drift-outcome counts remain explicitly deferred, represented
via a new `Instrumented[T]` type (`available`/`value`) rather than a
bare `0`/`None` that could be misread as a checked value. Internal
denominator contradictions raise `BatchReportInvariantViolation` rather
than being silently clamped or repaired. 21 new tests; full regression
930 collected, 929 passed, 1 skipped (pre-existing, environmental). No
schema change.

**Milestone 8 — `BatchCompletionReconciliationService`** (`65b4b9c`).
Closes the one remaining piece of frozen step 9 ("worker/claim
integration — the loop that ties 3-8 together per batch"):
`check_and_complete(batch_id)` reads Milestone 7's own report and
declares `SOURCE_WORK_EXHAUSTED` iff `unattempted_selected_count == 0`
and `terminal_source_count == attempted_source_count`, then delegates
exclusively to the pre-existing `BatchControlService.complete()` — this
service never mutates `IngestionBatch` directly, and may observe a
stale positive reading under concurrency (its own report read and the
`complete()` call are two separate operations), which is safe only
because of three already-existing properties: immutable batch
membership, no claim query ever re-admitting a terminal
`ContentPipelineState`, and the existing Milestone-4 admission gate
against new claims once a batch leaves `RUNNING`. Envelope-exhaustion
and runtime-budget-exhaustion detection are explicitly **not**
implemented — deferred to a future, separate design decision.
`NEEDS_REVIEW` counts as terminal for this decision, distinct from
`successful_ingestion_count`. 15 new tests, including a real two-worker
completion race (80 total races, zero flakiness); full regression 945
collected, 944 passed, 1 skipped.

**Milestone 9 — final cross-stage adversarial validation** (`6ad5ffb`,
test-only, zero production code change). Frozen step 12 of the
Implementation Design Pass's 12-step order: eight real-Postgres
concurrency scenarios proving interactions *between* Milestones 1–8's
already-individually-proven components, rather than re-proving any one
primitive in isolation — mixed-stage claim fencing, three-way
cross-batch embedding arbitration, reservation-vs-reconciliation
ordering, dual-archive crash/idempotent-retry, cross-batch identity
convergence, pause→hard-stop-abort→reconciliation, and live report
reads under concurrent multi-stage load. Two genuine test-fixture bugs
were found and fixed here, never in production. Full regression 953
collected, 952 passed, 1 skipped.

**Milestone 10 — pipeline-to-retrieval composition proof** (`d4d26d7`,
test-only). Proves, with real execution rather than static reading,
that a `Document`/`DocumentChunk` produced by driving synthetic content
through the real Milestones 1–9 pipeline is genuinely consumable by the
pre-existing, previously-unrelated `RetrievalService`/`ChatService` — a
real `RetrievalService` is used throughout (only its embedding client
faked), deliberately not a mocked `RetrievalService`. **Also documents,
without extending, a real and still-current limitation**: the citation
contract surfaces only `document_chunk_id`/`document_id`/
`document_title`/`document_source` — no `SourceInstance`/`ProvenanceLink`
data — left open as a separate, not-yet-authorized "Answer-Provenance
Decision." Full regression 955 collected, 954 passed, 1 skipped.

**Milestone 11 — `BatchOrchestratorService`** (`62592cd`). Model A
(bounded, attended, single-process execution), selected over
persistent/multi-worker/distributed alternatives per this document's
own "no task queue, no worker pool, and no distributed-execution
architecture anywhere else in this codebase, and none is planned"
(§ above). `run_once()` composes the six existing pipeline services
through their own public methods only — it never constructs, queries,
or mutates `SourceInstance`/`ContentIdentityGroup`/`Document`/
`DocumentChunk` directly — running one fixed-order pass through all
five stages (archive processing → identity resolution → normalization →
chunking → embedding), each exhausted once, followed by exactly one
`BatchCompletionReconciliationService.check_and_complete()` call. This
is the first point in the whole chain where the scaled pipeline has a
real, callable entry point outside test code.

Two distinct defects were found and fixed during this milestone's own
design/implementation: (1) **termination** — `claim_source_instance_
for_archive_processing`/`claim_source_instance_for_identity_resolution`
release a failed claim by resetting `claimed_by`/`claimed_at` to `NULL`
unconditionally, so a deterministically-failing row becomes immediately
reclaimable with no lease wait, and a naive "loop until `None`" never
terminates for it; (2) **progress over distinct candidates** — found
only after fixing (1) — the claim query orders candidates by
`created_at` alone, so the same stuck row would keep winning that
ordering and could starve every newer, distinct, processable row for a
whole invocation. Both are closed by one mechanism: each stage's own
in-memory, per-invocation set of already-claimed ids is passed as a new,
additive, optional `exclude_ids` parameter directly into the claim
query itself (`worker_claim_service.py`) — never merely checked after
the fact — with no new retry-exhaustion policy, persisted counter, or
schema change. **Named but explicitly not fixed**: a pre-existing
(Milestone-5-origin) gap where `claim_source_instance_for_archive_
processing`'s `already_processed` exclusion checks only
`EXISTS(SUCCEEDED)`, never `retryable` — a durable `retryable=False`
archive remains claim-eligible forever at the predicate level, harmless
today only because this orchestrator's own `exclude_ids` prevents it
from looping or starving other work. Remains open as a real,
separately-authorizable follow-up. 12 new tests; full regression 967
collected, 966 passed, 1 skipped.

**Milestone 12 — operator CLI entrypoint** (`bee4f69`).
`scripts/run_ingestion_batch.py` is the pipeline's first human-usable
entry point, giving Milestone 11's `run_once()`/`BatchReportService` a
real caller (matching this repository's only existing entrypoint
convention — there is no `pyproject.toml`/console-script mechanism
anywhere in the repo). Contract, verified against the actual code
rather than assumed:
- `--database` defaults to `"aibrain_test"` and cannot reach production
  by omission — the engine is built via `make_url(settings.DATABASE_URL)
  .set(database=args.database)`, never `SessionLocal` (which binds to
  `settings.DATABASE_URL` as-is, i.e. production `aibrain`); the
  resolved database name is printed before anything else runs.
- Batch existence is checked with one direct `db.get(IngestionBatch,
  args.batch_id)` before calling `run_once()`/`generate_report()`, and
  there is no `except ValueError` anywhere in the script — `ValueError`
  is raised throughout `app.classification` for several unrelated
  reasons (e.g. `ContentIdentityService.assign_content_identity`'s
  write-once violation, reachable from `run_once()`'s own normal
  identity-resolution call chain), so a blanket catch could mislabel a
  real defect as a harmless "batch not found." Anything past the
  pre-check propagates uncaught.
- One `Session` spans the whole invocation (orchestration + report);
  the CLI issues no `commit()`/`rollback()` of its own, since every
  mutating service it composes already commits its own work — `close()`
  runs in a `finally` block regardless of outcome.
- No source-path argument of any kind exists — real T7 paths only ever
  enter earlier, via the separately-gated `BatchCreationService`/
  `SourceInstanceService` — so a T7-path guard here was evaluated and
  deliberately not added; there is no attack surface for one to defend.
- Prints a plain-text operator report using only existing
  `OrchestratorRunResult`/`BatchReportService` fields; stdlib `logging`
  only, no new dependency.

11 new integration tests (real database; every scenario stays within
`ContentPipelineState.UNSUPPORTED`, which no normalization/chunking/
embedding claim query ever re-admits, so this suite has zero Ollama
dependency by design). Full regression 977 passed, 1 skipped, stable
across 3 runs. No schema change.

**Milestone 14 — live HTTP composition proof** (`a1721b1`, test-only).
Proves, through the real HTTP layer rather than direct service
composition (Milestone 10) or the CLI's own report (Milestone 12), that
content driven through the real, unmodified Milestone 12 CLI is
genuinely retrievable and citable via `POST /rag/search` and
`POST /chat` - the same code path an actual user hits.
`EmbeddingClient.embed`/`ChatClient.chat` are monkeypatched only at the
class boundary; neither `RetrievalService` nor `ChatService` is mocked
or bypassed. Exact `DocumentChunk.id`/`Document.id` are asserted
against the HTTP response. No citation/provenance enrichment was
introduced - the existing four-field citation shape
(`document_chunk_id`/`document_id`/`document_title`/`document_source`)
is re-observed at this boundary, not extended.

**Milestone 15 — deterministic retrieval ranking** (`29c6dee`).
`RetrievalService.search()`'s `order_by` gained a deterministic
secondary key: `distance ASC, DocumentChunk.id ASC`. Primary ordering
by cosine distance is unchanged; the secondary key resolves only an
exact-distance tie - a real, already-present condition in
`test_retrieval.py`'s own fixture, previously tolerated via a
set-comparison rather than an ordered one. No reranking or
relevance-model change; `top_k` semantics and the `SearchResponse`/
`SearchResult` API shape are unchanged. No schema/migration change.

**Milestone 16 — embedding failure handling** (`cc53e02`).
`EmbeddingUnavailableError` (`app/embeddings/client.py`) mirrors the
pre-existing `ChatUnavailableError` convention exactly, including
chaining the original exception via `from exc`. `POST /rag/search` and
`POST /chat` each convert only this exception into a clean `HTTP 503`;
any other exception continues to propagate unconverted. Chain 1's
(`ImportJobService._embed_documents`) and Chain 2's
(`PipelineEmbeddingService`, both call sites) existing
`except Exception` boundaries required no change, since both already
caught any exception - confirmed by their own regression suites passing
unmodified. No retry/backoff framework was introduced. No
schema/migration change.

**Milestone 18 — mixed Chain 1 + Chain 2 retrieval proof** (`5d008eb`,
test-only). Directly proved, rather than argued from code, that genuine
Chain 1 content (`ImportJobService` → `Document`/`DocumentChunk`) and
genuine Chain 2 content (the real, unmodified Milestone 12 CLI driving
the real Chain 2 pipeline) can coexist in the same `aibrain_test`
database and both correctly participate in the existing
`RetrievalService`, `POST /rag/search`, and `POST /chat` paths, with no
chain-specific retrieval filtering anywhere. Both ingestion paths were
exercised through their own genuine, already-established entry points -
never by manually constructing final `Document`/`DocumentChunk` rows -
and `RetrievalService`/`ChatService` themselves were used unmodified,
with only the established model-client seams (`EmbeddingClient.embed`/
`ChatClient.chat`) faked. The current database-level provenance
distinction was observed as tested, not declared as a new rule: Chain 1
rows carry `import_job_id` populated and `content_identity_group_id`
`NULL`; Chain 2 rows carry the reverse. The existing four-field
citation shape is unchanged. No production, schema, or API change of
any kind was required. **The long-term Chain 1 ↔ Chain 2 relationship
remains intentionally unresolved** - this milestone proves the two
paths coexist safely today, not that their relationship has been
decided; see the subsection immediately below, unchanged since
Milestone 13.

**Milestones 20–24 — provenance-rich citations** (investigation:
Milestones 20/21; design/freeze: Milestone 22; implementation:
`facd7cf`, Milestone 23; post-implementation review: Milestone 24, M23
accepted). Milestones 20 and 21 were read-only investigations that
changed no production behavior; Milestone 22 froze a design without
writing code; Milestone 24 re-verified the already-committed Milestone
23 code and introduced no changes of its own.

*Source provenance model*: `SourceInstance` represents one physical
observed occurrence; multiple `SourceInstance`s can converge on one
`ContentIdentityGroup` (`SourceInstance.content_identity_group_id` is
many-to-one, unlike `Document.content_identity_group_id`, which is
`UNIQUE`), so a `ContentIdentityGroup` corresponds to at most one
`Document`. `ProvenanceLink` stores one `SourceInstance`'s own ordered
physical ancestry. User-visible provenance exposes ALL source
occurrences for a group - no occurrence is authoritative merely
because of ordering. `CanonicalDecisionService` is not currently used
to select or filter citation provenance (zero production callers;
nothing prevents multiple `SourceInstance`s in one group from
independently being marked `CANONICAL`); `NEEDS_REVIEW` is entirely
unrelated to this mechanism.

*Retrieval/citation flow*: `DocumentChunk` → `RetrievedChunk` (now
carrying `source_occurrences`) → `SearchResult`/`Citation`. Provenance
is computed exactly once, inside `RetrievalService.search()`, consumed
identically by `POST /rag/search` and `POST /chat` - never two
independent implementations.

*Performance*: exactly two additional batched queries per `search()`
call (`SourceInstance WHERE content_identity_group_id IN (...)`, then
`ProvenanceLink WHERE source_instance_id IN (...)`) - never one query
per result/document/instance; query count is independent of `top_k`
and of occurrence-set size.

*Chain 1*: `source_occurrences: null` - no `SourceInstance` graph
exists; none is fabricated.

*Chain 2*: every `SourceInstance` associated with the `Document`'s
`ContentIdentityGroup`, ordered by `SourceInstance.id` (presentation
determinism only, never semantic priority).

*Archive ancestry*: structured `{kind, path}` steps, populated only for
genuinely nested (more than one archive level) occurrences - never
flattened into an invented display-path string.

**Explicitly not implemented by Milestones 20–24** (each a distinct,
separately-authorizable, deliberately deferred item): canonical/
representative source selection, `NEEDS_REVIEW` integration, real-T7
path exposure/redaction policy, large-occurrence-set pagination or
capping, OS-level multi-process execution, any distributed/scheduler
infrastructure.

### Chain 1 ↔ Chain 2 relationship

**Two independent ingestion paths currently exist and are both live**,
writing into the same `documents`/`document_chunks` tables:

- **Chain 1**: `ImportJob` → `ImportJobService` → `app/api/import_jobs.py`
  (`/import-jobs`), wired into the running app and the frontend's
  Import Job Monitoring view. Sets `Document.import_job_id`, leaves
  `content_identity_group_id` `NULL`. Performs no deduplication at all
  — `Document.content_hash` is computed but never looked up before
  insert (the model's own comment: "Deliberately not unique -
  duplicates are exactly what this is for").
- **Chain 2**: `IngestionBatch` → `BatchOrchestratorService` →
  Milestone 12's CLI (`scripts/run_ingestion_batch.py`), the only
  trigger for it today. Sets `Document.content_identity_group_id`,
  leaves `import_job_id` `NULL`. Deduplicates by construction — one
  `ContentIdentityGroup` per distinct content hash, write-once.

Both chains reuse the identical chunker (`app.embeddings.chunker.
chunk_text`) and the identical `EmbeddingClient`, so the `DocumentChunk`
rows they each produce are structurally identical. `RetrievalService`/
`ChatService`/`app/rag/*.py` make zero reference to `import_job_id`,
`content_identity_group_id`, or `content_hash` anywhere (grep-verified)
— retrieval and chat do not distinguish which chain produced a given
`Document`.

**Coexistence hazard, concretely, not hypothetically**: both chains
hash raw bytes with the identical algorithm (SHA-256). Byte-identical
content ingested once through each chain produces the same hash value
but two separate `Document` rows and two separate sets of
`DocumentChunk`/embeddings, since neither chain's dedup logic consults
the other's tables. Nothing in the schema prevents a future `Document`
row from having both `import_job_id` and `content_identity_group_id`
set, or neither.

**ARCHITECTURAL DECISION REQUIRED: the long-term relationship between
Chain 1 and Chain 2 remains undecided.** Nothing in this repository —
no design doc, commit message, roadmap entry, or code comment — states
whether Chain 1 is legacy-and-to-be-replaced, transitional, intended to
permanently coexist with Chain 2 (e.g. for arbitrary on-demand imports,
as distinct from Chain 2's T7-scale classification/selection machinery),
or simply an independent path nobody has reconciled yet. This document
deliberately does not resolve that question — it did not exist to be
resolved from the repository as it stands at Milestone 12, and is
recorded here as open rather than inferred. Both chains remain live for
now.

## On-disk layout
- `documents/imports/<job_id>/` — working copies produced by ingestion for a
  given import job. Derived, disposable, safe to delete and re-ingest.
- `knowledge/` — reserved for curated/derived knowledge artifacts
  (future); `knowledge/t7_discovery/` holds T7 corpus inventory,
  duplicate-analysis, AND provenance-analysis reports specifically,
  gitignored since they contain real personal file/directory names.
- `postgres/`, `redis/` — data directories for the Dockerized services.
- `logs/`, `config/`, `prompts/` — reserved, currently empty.
- `scripts/t7_discovery.py`, `scripts/t7_duplicate_analysis.py`,
  `scripts/t7_provenance_analysis.py` — the T7 Corpus Discovery,
  Deduplication Analysis, and Provenance Analysis runners (see above);
  otherwise reserved, currently empty.

## Open/planned areas
- `docker-compose.yml` is currently empty — services (Postgres, Redis,
  Ollama) are expected to run some other way for now (e.g. local
  installs) until Compose is filled in.
- The `vector` Postgres extension (`postgresql-16-pgvector`) and the
  `document_chunks` migration are applied on this host, but neither step
  is automated yet — a fresh machine needs both done manually before
  `EmbeddingService` will work.
- Frontend covers chat, read-only import job monitoring, memory review,
  and dedup review (see Frontend, above) — no job-creation form
  (creating/running import jobs stays API/curl-only), no conversation
  list/switcher, and no dedup execution mechanism of any kind (still no
  code path anywhere that deletes, moves, or modifies a file).
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
