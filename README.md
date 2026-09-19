# AI_Brain

![CI](https://github.com/shrinivaspattar/AI_Brain/actions/workflows/ci.yml/badge.svg)

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
- Redis (optional): a read-through cache for query embeddings, off by
  default (`EMBEDDING_CACHE_ENABLED`)
- Plain HTML/CSS/vanilla JS frontend, served directly by FastAPI — no
  Node/npm toolchain, no build step, no second process
- 1,000 tests (`backend/tests`)

## Architecture

```mermaid
flowchart LR
  UI["Browser UI<br/>static HTML + JS"]
  CLI["Operator CLI<br/>run_ingestion_batch.py"]
  SRC["Source files<br/>read-only"]
  OL["Ollama, local<br/>qwen3:8b + nomic-embed-text"]
  PG[("PostgreSQL + pgvector<br/>documents, chunks, provenance,<br/>batches, memories, audits")]
  RD[("Redis<br/>optional query-embedding cache")]
  EX["DedupFilesystemExecutor<br/>not wired to any endpoint"]

  subgraph API["FastAPI backend"]
    R["Routers<br/>chat, rag, memory, tools,<br/>import-jobs, documents, dedup"]
    CS["ChatService<br/>retrieval + memory + tool loop"]
    RS["RetrievalService"]
    C1["Chain 1<br/>ImportJobService"]
    C2["Chain 2<br/>BatchOrchestratorService<br/>archive, identity, normalize, chunk, embed"]
    KRM["Dedup review services<br/>review, plan, authorize, audit"]
  end

  UI --> R
  R --> CS
  CS --> RS
  RS --> PG
  RS -->|query embedding| OL
  RS -.->|cache, fails open| RD
  CS -->|chat + tools| OL
  R --> C1
  SRC --> C1
  C1 --> PG
  C1 -->|chunk embeddings| OL
  CLI --> C2
  SRC --> C2
  C2 --> PG
  C2 -->|chunk embeddings| OL
  R --> KRM
  KRM --> PG
  KRM -.-> EX
```

Chain 2 has no HTTP surface: it is driven only by the operator CLI, one
bounded, attended batch at a time. Everything the chat loop can reach is
read-only except one review-gated tool (`remember`).

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
- Query embeddings can be cached in Redis (`EMBEDDING_CACHE_ENABLED=true`,
  `REDIS_URL`, `EMBEDDING_CACHE_TTL_SECONDS`, default one day). It fails
  open: if Redis is down or returns a bad value, search behaves exactly as
  without a cache. On the development machine (CPU only) a repeated query
  took 5 ms from cache versus 49 ms for a warm Ollama call and 350 ms for a
  cold one, so the gain is small and mostly appears when Ollama has
  unloaded the embedding model. Ingestion embeddings are never cached.
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

**Frontend** — a single page (`frontend/`) with four views: chat (with
citations, conversation persistence, light/dark theme), read-only import
job monitoring (live polling), a memory review queue (approve/reject with
provenance back to the source chat message), and a dedup review queue
(approve/reject a duplicate finding, choosing the canonical copy to keep —
this view reviews the *finding*, never touches a file; nothing here or in
the backend it calls can delete, move, or quarantine anything).

## What's not built yet

- OCR, image understanding, speech-to-text, a knowledge graph
- Streaming chat responses (currently single blocking calls)
- Multiple/switchable conversations in the UI (one active conversation per
  browser today)
- `.rar`/`.gz` archive extraction (only `.zip`/`.7z` today)
- Any way to trigger the dedup executor (no endpoint or CLI calls it, by
  design - see above)
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

### Docker

The image contains only the app (`backend/` and `frontend/`); Postgres and
Ollama stay outside it. `.dockerignore` keeps `.env`, `knowledge/`,
`documents/` and `scripts/` out of every layer.

```bash
docker build -t ai-brain .
docker run --rm -p 8000:8000 \
  --add-host=host.docker.internal:host-gateway \
  -e DATABASE_URL=postgresql+psycopg2://USER:PASS@host.docker.internal:5432/aibrain \
  -e OLLAMA_HOST=http://host.docker.internal:11434 \
  ai-brain
```

### CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull
request to `main`: `pgvector/pgvector:pg16` and `redis:7` services, both
databases migrated with Alembic, the backend test suite, then a Docker build followed
by a smoke test that starts the container and requires `GET /health` to
answer. Tests that need a live Ollama skip themselves in CI; the Redis cache has a
real round-trip test against the CI Redis service.

## What broke and how it was fixed

Each entry below comes from this repository's history, its roadmap
(`docs/roadmap/Roadmap.md`) or a recorded test run.

1. **An orchestrator loop that never ended, then one that starved.** A
   failed batch item released its claim immediately, so a file that always
   failed was claimed again forever - and because claims are ordered by age,
   that stuck row also blocked every newer one. The fix passes an in-memory
   set of already-attempted ids into the claim query itself (not checked
   afterwards), so nothing is retried within one run. An integration test
   with a permanently failing archive created before a valid one had shown
   the valid one was never attempted.
2. **A delayed worker clearing a live worker's claim.** Releases matched on
   row id alone, so a worker that was slow (not crashed) past its lease could
   release after another worker had legitimately taken the row over. Every
   grant now increments a `claim_generation` fencing token, and a release
   only takes effect for the generation it was granted.
3. **Embedding capacity leaked by a crash.** A worker dying mid-embedding
   left its reserved capacity counted against the batch permanently. Each
   reservation now records its owning batch and is credited back atomically
   when the row is reclaimed.
4. **Test-database pollution.** Most import integration tests never cleaned
   up after themselves; across many runs about 160 import jobs' worth of
   rows accumulated and pushed a real pair out of the near-duplicate query's
   result limit, so a dedup test failed deterministically. A shared cleanup
   helper replaced the ad-hoc cleanup, the pollution was deleted once, and
   the suite was re-run clean five times in a row.
5. **A JSON `null` is not a SQL `NULL`.** The provenance trace filtered
   `citations IS NOT NULL`, but a JSONB column that stores Python `None`
   holds a JSON `null`, which that filter does not exclude. The service now
   re-checks in Python after the query.
6. **A bare 500 on a bad import path.** `POST /import-jobs/{id}/execute`
   returned 500 for a missing or non-directory source path because the
   handler caught only `ValueError`. It now returns a 422 carrying the same
   message stored on the job. Found while building the monitoring view.
7. **A failure the design predicted but had never met.** The first ingestion
   from a real external drive failed at the embedding step because Ollama was
   not running. The affected content group moved to `FAILED`, which the
   pipeline deliberately never retries on its own. A read-only diagnostic
   script found the cause and a one-off, attended reset script re-queued
   exactly that group - a manual step by design, not a new retry mechanism.
8. **Cleanup scripts that broke on foreign keys.** Removing an accidental
   duplicate batch failed twice: first because rows joined only by foreign
   keys (no ORM relationship) are not ordered by autoflush, then because
   provenance links still referenced the rows being deleted. The fix flushes
   deletions in dependency order and refuses to touch anything already
   processed. A later demo script left one document behind and hit the same
   class of error; because each cleanup ran as one transaction, nothing was
   ever half-deleted.
9. **Documentation that contradicted the code.** Several docstrings said the
   codebase contained no filesystem executor, long after
   `DedupFilesystemExecutor` had been built, and an early README repeated
   that claim (and undercounted the frontend views). The corrections came
   from reading the code, not the comments.
10. **An assumption measured instead of trusted.** An experiment
    (`scripts/exp001/`, one public corpus of 154 documents) tested whether
    PostgreSQL's built-in full-text ranking can stand in for BM25. It cannot.
    Alone it scored 0.709 nDCG@10 against 0.811 for real BM25 in a 110-query
    pilot, and 0.704 against 0.848 in a separate 279-query confirmation round.
    Combined with dense search it lowered meaning-style scores in both rounds,
    significantly in the pilot (-0.059) but not in the confirmation (-0.026,
    within noise). Dense search combined with real BM25 beat dense alone in
    both rounds, but BM25 alone was about as good as that combination on
    this corpus, and the queries were drafted from the same public docs, so
    this says little about personal notes. Nothing in production changed.
11. **Local-model latency.** Bulk query generation with Qwen3 took about five
    minutes for two passages on CPU-only hardware because of hidden reasoning
    tokens; disabling thinking cut it to 18 seconds with similar output.

## Design goals

- Local-first, privacy-first: your data never leaves your machine
- Every derived fact traceable back to a real source file
- Nothing destructive happens without an explicit human decision
- Extensible without an ever-growing pile of speculative abstraction —
  see `docs/roadmap/Roadmap.md` for the evidence-driven, milestone-gated
  process this project is actually built with
