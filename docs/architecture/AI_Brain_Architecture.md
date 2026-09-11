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
- LLM runtime: Ollama, running DeepSeek-R1 and Qwen2.5-Coder locally
- Vector store: ChromaDB
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
   DocumentIngestor ──▶ Document rows (Postgres)   [current: implemented]
        │
        ▼
   chunking + embeddings (app/embeddings) ──▶ ChromaDB vectors   [planned]
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
| `models/`, `schemas/` | Implemented | SQLAlchemy models (`Document`, `ImportJob`) and Pydantic schemas. |
| `services/` | Implemented | `DocumentService` (persistence), `ImportJobService` (job lifecycle: create, mark_running, mark_completed, execute_job, with enforced state transitions). |
| `api/` | Implemented | FastAPI routers: health, version, database, documents, import_jobs. |
| `db/` | Implemented | Session/engine setup, health checks. |
| `core/` | Implemented | `Settings` (env-driven config: `DATABASE_URL`, `INGESTION_DIR`, `OLLAMA_HOST`, `CHAT_MODEL`, `EMBEDDING_MODEL`, ...). |
| `embeddings/` | Empty stub | Will chunk `Document` content and call the embedding model (`nomic-embed-text` via Ollama) to produce vectors. |
| `rag/` | Empty stub | Will store/query vectors in ChromaDB and assemble retrieved context for chat. |
| `memory/` | Empty stub | Long-term memory: durable facts/preferences about the user, distinct from RAG document retrieval. |
| `tools/` | Empty stub | Tool-calling: lets the assistant act (run scripts, query local APIs, manipulate files) rather than only answer. |

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
- `chroma/` — ChromaDB persistent store (future use).
- `postgres/`, `redis/` — data directories for the Dockerized services.
- `logs/`, `config/`, `prompts/`, `scripts/` — reserved, currently empty.

## Open/planned areas
- `docker-compose.yml` is currently empty — services (Postgres, Redis,
  Chroma, Ollama) are expected to run some other way for now (e.g. local
  installs) until Compose is filled in.
- No frontend yet; the backend is API-only.
- See [`docs/backlog.md`](../backlog.md) for prioritized future work
  (provenance chain, dedup, audit log, etc.) and
  [`docs/roadmap/Roadmap.md`](../roadmap/Roadmap.md) for the phased plan.
