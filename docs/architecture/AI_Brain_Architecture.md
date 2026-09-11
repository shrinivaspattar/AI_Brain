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
   chunking + embeddings (app/embeddings) ──▶ DocumentChunk rows w/ pgvector embedding   [implemented, not yet wired into ingestion]
        │
        ▼
   RAG retrieval (app/rag) ──▶ context for chat   [planned]
        │
        ▼
   chat/tool loop (app/memory, app/tools) ↔ Ollama (DeepSeek-R1 / Qwen2.5-Coder)   [planned]
        │
        ▼
   response, with citations back to source Document(s)
```

## Backend module layout (`backend/app/`)

| Module | Status | Responsibility |
|---|---|---|
| `ingestion/` | Implemented | `SourceScanner` walks a source dir; `ArchiveExtractor` safely expands archives (zip-bomb/disk-space guarded); `DocumentIngestor` orchestrates scan → extract → persist. |
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `DocumentChunk`, `ImportJob`) and Pydantic schemas. `Document.import_job_id` carries provenance back to the import job that created it. |
| `services/` | Implemented | `DocumentService` (persistence), `ImportJobService` (job lifecycle, enforced state transitions), `EmbeddingService` (chunk + embed + persist a document's text). |
| `api/` | Implemented | FastAPI routers: health, version, database, documents, import_jobs. No endpoints yet for chunks/embeddings/retrieval. |
| `db/` | Implemented | Session/engine setup, health checks. |
| `core/` | Implemented | `Settings` (env-driven config: `DATABASE_URL`, `INGESTION_DIR`, `OLLAMA_HOST`, `CHAT_MODEL`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, ...). |
| `embeddings/` | Implemented | `chunker.chunk_text` (character-bounded, overlapping chunks); `client.EmbeddingClient` wraps Ollama's `embed` API for `nomic-embed-text`. Not yet wired into the ingestion pipeline — must be invoked explicitly via `EmbeddingService`. |
| `rag/` | Empty stub | Will query `document_chunks.embedding` (pgvector) for a given query and assemble retrieved context for chat. |
| `memory/` | Empty stub | Long-term memory: durable facts/preferences about the user, distinct from RAG document retrieval. |
| `tools/` | Empty stub | Tool-calling: lets the assistant act (run scripts, query local APIs, manipulate files) rather than only answer. |

## Chunking and embeddings
`EmbeddingService.embed_document(document, content)` (`app/services/embedding_service.py`):
1. Splits `content` into overlapping chunks (`app/embeddings/chunker.py`, default 1000 chars / 200 overlap).
2. Deletes any existing `DocumentChunk` rows for that document (idempotent re-embedding).
3. Calls Ollama's embed API in one batched request (`app/embeddings/client.py`) for `EMBEDDING_MODEL` (`nomic-embed-text`, `EMBEDDING_DIMENSIONS=768`).
4. Persists one `DocumentChunk` row per chunk, `(document_id, chunk_index)` unique, `embedding` as a pgvector column.

This is deliberately decoupled from `DocumentIngestor` — it takes already-extracted text `content`, not a file path. No text-extraction-by-file-type (PDF, DOCX, etc.) exists yet; today it only makes sense for plain text/markdown files. Wiring embedding into `execute_job` automatically (and deciding sync vs. background-worker-via-Redis for large imports) is a deliberate next decision, not yet made.

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
- The `vector` Postgres extension must be installed on the host
  (`sudo apt install postgresql-16-pgvector`) before the `document_chunks`
  migration can run; it is not part of any automated setup yet.
- No frontend yet; the backend is API-only.
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
