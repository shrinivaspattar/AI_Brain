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
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`, `Conversation`, `Message`, `Memory`, `ToolCallRecord`, `DuplicateReview`, `DuplicateReviewMember`, `DedupExecutionPlan`, `DedupExecutionPlanAction`, `DiscoveryRun`, `ClassificationRun`, `ContentIdentityGroup`, `SourceInstance`, `ProvenanceLink`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it; `Document.content_identity_group_id` links it to its `ContentIdentityGroup` (see `classification/`, below — schema/model implementation only, no ingestion pipeline calls this yet). |
| `classification/` | Implemented (schema/model layer only — no ingestion pipeline calls it yet) | `DiscoveryRunService`, `ClassificationRunService`, `ContentIdentityService` (`get_or_create_group` — the database-concurrency-safe claim for a `ContentIdentityGroup`; `assign_content_identity` — write-once `SourceInstance.content_identity_group_id`), `SourceInstanceService` (creates a `SourceInstance` + its full `ProvenanceLink` ancestry chain atomically), `CanonicalDecisionService` (records a canonical-status decision with required evidence). Implements the `29d6864`/`14b8063` frozen design — see "Schema/Model Implementation," below. No T7 access anywhere in this module. |
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
