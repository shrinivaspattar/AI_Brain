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

### execute() vs recover_stale_execution() race - closed, not merely deferred

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
action_result` for the SAME unresolved plan action.

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
