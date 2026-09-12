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
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`, `Conversation`, `Message`, `Memory`, `ToolCallRecord`, `DuplicateReview`, `DuplicateReviewMember`, `DedupExecutionPlan`, `DedupExecutionPlanAction`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it. |
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
an error, though - the plan honestly records `observed_exists=false`
for that member rather than crashing; a plan is allowed to describe a
file that's already gone, precisely so that fact is visible.

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
below - but automatic crash *detection/resolution tooling* (something
that notices a stuck `RUNNING` execution and investigates it) remains
out of scope, since no long-running executor process exists yet to
actually crash.

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

**A genuine, currently-unresolved gap this design pass surfaced**:
re-planning a *partially*-executed review's remaining work is not
actually possible today. Once a duplicate has really been removed by
an earlier execution, `generate_plan_for_review` has no concept of
"already resolved" at the review-member level - it will include that
member in any new plan regardless. Because a plan action's own
`observed_exists` is frozen at *that* plan's generation time, and
`check_plan_validity`'s `is_valid` requires the file to exist *now*
for that exact action, an action generated against an already-missing
file can never be valid - not now, not ever. This blocks authorization
of the **entire** new plan, not just the still-outstanding part of it,
even though the outstanding member's own file is completely untouched
and its own action would otherwise be perfectly valid on its own.
Verified directly (`test_replanning_after_a_successful_deletion_
produces_a_permanently_invalid_plan`): a real successful deletion (a
test simulating what an executor would have left behind - no AI_Brain
code performs it) followed by a real plan regeneration reproduces
this exactly. **Not fixed here** - resolving it needs either a way to
mark specific review members "already resolved" (excluding them from
a regenerated plan), or a way for validity checking to treat "file
confirmed already gone per an earlier `COMPLETED`/`PARTIALLY_COMPLETED`
execution" as an accepted terminal state rather than staleness.
Deferred until partial re-execution is actually needed in practice.

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
- Frontend covers chat, read-only import job monitoring, memory review,
  and dedup review (see Frontend, above) — no job-creation form
  (creating/running import jobs stays API/curl-only), no conversation
  list/switcher, and no dedup execution mechanism of any kind (still no
  code path anywhere that deletes, moves, or modifies a file).
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
