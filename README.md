# AI_Brain

An offline, privacy-first personal AI knowledge system. Ingest your own files,
retrieve them with real semantic search, chat with a fully local LLM that
cites its sources, and let it remember things about you over time — with a
human always in the loop for anything that touches your data.

No cloud LLM fallback, no multi-user support: this is a single-user, offline-
first personal system, by design.

## Two histories in this project

This repository actually holds two independent generations of AI_Brain,
kept as separate branches rather than merged into one confusing history:

- **`develop` (tags `v0.1`–`v1.5`, July 2026)** — the original prototype: a
  SQLite-based file scanner with dedup detection and full-text search. Its
  own roadmap called for chunking, embeddings, a vector store, RAG, and a
  local LLM as future work.
- **`main` (this branch, `v2.0.0`+)** — a full rewrite on FastAPI +
  PostgreSQL/pgvector + Ollama that actually delivers that roadmap, on a
  different and more production-grade stack than originally planned, plus
  a lot the original roadmap didn't anticipate (memory, tool calling,
  review-gated writes, a real frontend). `develop` is kept as-is for
  history, not superseded in place.

## Stack

- FastAPI + PostgreSQL (pgvector) + SQLAlchemy/Alembic
- Ollama, local models: `qwen3:8b` (chat), `nomic-embed-text` (embeddings)
- Plain HTML/CSS/vanilla JS frontend, served directly by FastAPI — no
  Node/npm toolchain, no build step, no second process
- 989 tests (`backend/tests`)

## What's built

**Ingestion — two pipelines, by design, coexisting:**
- **Chain 1** (`ImportJob`/`ImportJobService`): the original, simple
  ingestion path — point it at a directory, it scans, extracts text
  (PDF/DOCX/PPTX/XLSX, plus plain text/markdown/code), embeds, and stores.
- **Chain 2** (Milestones 1–32): a scaled, bounded, attended batch
  orchestrator for large real-world corpora — classification → batch
  creation → resource-guarded claiming → archive extraction → content-
  identity resolution (converging exact-duplicate files to one canonical
  document) → normalization → chunking → embedding, all under an explicit
  envelope (max files/bytes/embeddings/runtime) an operator sets per batch.
  **Milestone 32 proved this pipeline end-to-end against real personal
  files** on an actual 669GB/619K-file external drive — real notes ingested,
  correctly deduplicated across multiple real copies, retrieved via
  `/rag/search`, and answered through `/chat` with citations pointing back
  to the real source paths.

**Retrieval & conversation:**
- `POST /rag/search` — semantic search over ingested content via pgvector
  cosine distance.
- `POST /chat` — RAG-augmented chat against a local `qwen3:8b`, with
  numbered `[n]` citations back to real source documents, and persisted
  conversation history (`GET /chat/{id}`).

**Memory** — a long-term store the model can propose to, gated by human
review: it can call a `remember` tool, but every proposal lands as
`PENDING` and is invisible to future chat turns until approved via
`POST /memory/{id}/approve` (or rejected, keeping the record either way).

**Tool calling** — a real agentic loop (`qwen3:8b` supports Ollama's tool
API): `search_knowledge_base`, `get_current_datetime`,
`list_recent_documents`, a path-scoped `read_file_content` (refuses
anything outside a completed import job's own source directory), and
`remember`. Every call is audited (`ToolCallRecord`): tool name, arguments,
result, status, duration — nothing here writes to a file; it's read-only
except for the review-gated memory proposal.

**Deduplication & provenance (KRM)** — exact and near-duplicate detection
over ingested content (`GET /dedup/exact`, `GET /dedup/near`), a *dry-run*
cleanup planner (`GET /dedup/exact/plan`) that proposes which copy to keep
without touching any file, an explicit human-authorization gate on one
immutable plan, and a full provenance trace
(`GET /documents/{id}/provenance`) from source file through chunks to every
chat message that ever cited it.

There is a real filesystem executor (`DedupFilesystemExecutor`) — DELETE
always means quarantine-move via `os.rename()`, never a true delete — with
serious TOCTOU-closure: it pins the source file's device/inode and hashes
its content through one open file descriptor, then verifies the
post-move destination identity matches that exact pin before calling
anything a success. Fail-closed by construction: no default paths, no env
var, rejects symlinked roots, refuses cross-filesystem moves. It is **not
wired into any API endpoint or router** — constructible only from trusted
Python code, exercised today only by its own tests, never reachable from
the running application.

**Frontend** — a single page (`frontend/`) with three views: chat (with
citations, conversation persistence, light/dark theme), read-only import
job monitoring (live polling), and a memory review queue (approve/reject
with provenance back to the source chat message).

## What's not built yet

- OCR, image understanding, speech-to-text, a knowledge graph
- Streaming chat responses (currently single blocking calls)
- Multiple/switchable conversations in the UI (one active conversation per
  browser today)
- `.rar`/`.gz` archive extraction (only `.zip`/`.7z` today)
- Any actual file executor for dedup cleanup (planning only, by design)
- Docker Compose for Postgres/Ollama (placeholder file, not filled in)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Postgres with pgvector, and Ollama with qwen3:8b + nomic-embed-text,
# must be running and reachable — see backend/app/core/config.py for
# the settings these read from.

cd backend
alembic upgrade head
uvicorn app.main:app --reload
```

## Design goals

- Local-first, privacy-first: your data never leaves your machine
- Every derived fact traceable back to a real source file
- Nothing destructive happens without an explicit human decision
- Extensible without an ever-growing pile of speculative abstraction —
  see `docs/roadmap/Roadmap.md` for the evidence-driven, milestone-gated
  process this project is actually built with
