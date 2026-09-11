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
- Frontend: not yet built

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
   get_current_datetime, list_recent_documents (all read-only)   [implemented]
```

## Backend module layout (`backend/app/`)

| Module | Status | Responsibility |
|---|---|---|
| `ingestion/` | Implemented | `SourceScanner` walks a source dir; `ArchiveExtractor` safely expands `.zip` and `.7z` archives (bomb/path-traversal/disk-space guarded — see below); `DocumentIngestor` orchestrates scan → extract → persist; `text_extractor.extract_text` pulls plain text out of `.pdf`/`.docx`/anything-UTF-8-decodable, used before embedding. |
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`, `Conversation`, `Message`, `Memory`, `ToolCallRecord`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it. |
| `services/` | Implemented | `DocumentService` (persistence, `list_documents`), `ImportJobService` (job lifecycle, enforced state transitions, embeds documents synchronously during `execute_job`), `EmbeddingService` (chunk + embed + persist a document's text), `ChatService`/`ChatClient` (RAG-augmented chat over Ollama, conversation persistence, memory injection, tool-calling loop). |
| `api/` | Implemented | FastAPI routers: health, version, database, documents, import_jobs, rag, chat, memory, tools. |
| `db/` | Implemented | Session/engine setup, health checks. |
| `core/` | Implemented | `Settings` (env-driven config: `DATABASE_URL`, `INGESTION_DIR`, `OLLAMA_HOST`, `CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, ...). |
| `embeddings/` | Implemented | `chunker.chunk_text` (character-bounded, overlapping chunks); `client.EmbeddingClient` wraps Ollama's `embed` API for `nomic-embed-text`. |
| `rag/` | Implemented | `retrieval_service.RetrievalService.search(query, top_k)`: embeds the query, ranks `document_chunks` by pgvector cosine distance, joined to source `Document`. Exposed via `POST /rag/search`. No reranking/relevance filtering beyond raw distance yet. |
| `memory/` | Implemented | `service.MemoryService`: flat `content` + optional `confidence`/provenance store, no type taxonomy. `POST /memory`, `GET /memory`, `DELETE /memory/{id}`. Read-only hook into `ChatService` (loads up to 50, most-recent-first); no automatic write hook from chat yet. |
| `tools/` | Implemented | `registry.Tool`/`ToolRegistry` (dispatch by name, `call()` never raises, returns a structured `ToolCallResult`). `builtin.build_default_registry`: `search_knowledge_base`, `get_current_datetime`, `list_recent_documents` — all read-only. Exposed via `GET /tools`; driven by `ChatService._run_tool_loop`, which persists a `ToolCallRecord` audit row per call. No filesystem or external-network tools yet. |

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

`app/tools/builtin.build_default_registry(db)` registers three tools, all
**read-only**:
- `search_knowledge_base(query, top_k=5)` — explicit, on-demand
  `RetrievalService` search, letting the model search multiple times
  with different queries within one turn, distinct from the always-on
  context already injected into the prompt.
- `get_current_datetime()` — UTC, ISO 8601.
- `list_recent_documents(limit=10)` — wraps `DocumentService.list_documents`.

Exposed via `GET /tools`. Deliberately no filesystem read/write or
external-network-call tools yet — before building around
`qwen3:8b`'s tool support, confirmed via `ollama /api/show` that it
actually advertises the `tools` capability. Verified end-to-end: asked
the real model for the current date/time and got back a minute-precise
answer only obtainable by actually calling `get_current_datetime` (the
model's training data doesn't include 2026).

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
RAG, or memory. Today's three tools don't take secrets as arguments or
return raw filesystem contents, so no redaction logic exists yet; a future
filesystem/write tool would need to revisit that before this trail could
be trusted not to store something sensitive.

Verified end-to-end against the real model and directly against Postgres:
asked it to list recently ingested documents, it called
`list_recent_documents(limit=10)` on its own, and the resulting
`ToolCallRecord` showed the correct arguments, `SUCCESS` status,
`message_id` linkage, and a 2ms `duration_ms`.

## Memory
`Memory` (`app/models/memory.py`): `content` (the fact/preference itself),
optional `confidence` (0–1), optional provenance (`conversation_id`/
`message_id` it was derived from — null for anything written directly via
the API). No `memory_type` taxonomy (episodic/semantic/preference/etc.) —
the original project brief explicitly said not to invent one until
something in the actual implementation needs it, and nothing does yet.

`MemoryService` (`app/memory/service.py`): `create_memory`, `get_memory`,
`list_memories` (most-recent-first, capped), `delete_memory` (raises
`ValueError` → 404 if missing). Exposed via `POST /memory`, `GET /memory`,
`DELETE /memory/{id}`.

Read-only hook into `ChatService` today: every chat turn loads up to 50
memories and injects them into the prompt (see Chat, above). There is
deliberately no write hook yet — nothing in the chat loop automatically
extracts "facts" from casual conversation into memory. An LLM silently
deciding what's worth remembering is a real trust/quality risk (a
hallucinated "fact" becoming permanent, unreviewed context for every future
turn); that needs its own deliberate design — e.g. a confidence threshold,
an explicit confirmation step, or a review queue akin to the KRM backlog's
"Confidence Review Queue" — not a default-on side effect of chatting.
Memory retrieval is "most recent N", not similarity-ranked like document
retrieval; revisit if the store grows large enough that recency stops
being a good proxy for relevance.

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
- No frontend yet; the backend is API-only.
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
