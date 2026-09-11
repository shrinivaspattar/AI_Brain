# AI_Brain Roadmap

Target end-state: a personal knowledge assistant. Ingest your documents,
notes, and archives; chat with a fully offline LLM that can search and cite
your own knowledge base (RAG); have it remember things about you over time.
See [`docs/architecture/AI_Brain_Architecture.md`](../architecture/AI_Brain_Architecture.md)
for the current module-by-module status this roadmap tracks against.

## Phase 0 — Ingestion foundation (done)
- [x] Source scanning (`SourceScanner`)
- [x] Archive extraction with zip-bomb / disk-space protection (`ArchiveExtractor`)
- [x] Document persistence (`Document` model + `DocumentService`)
- [x] Import job lifecycle with enforced state transitions (`ImportJobService`)
- [x] REST API for documents and import jobs
- [x] Provenance: `Document.import_job_id` FK back to the import job
- [ ] Docker Compose for Postgres, Redis, Ollama (currently empty)

## Phase 1 — Retrieval (RAG)
- [x] Chunking (`app/embeddings/chunker.py`)
- [x] Embedding generation via Ollama (`app/embeddings/client.py`, `nomic-embed-text`)
- [x] Vector storage: pgvector `document_chunks` table (`EmbeddingService`),
      migration applied to `aibrain` and `aibrain_test`, verified end-to-end
      against a real Ollama `nomic-embed-text` call
- [x] Wire embedding into `execute_job`, synchronously: each ingested
      document has its text extracted and embedded inline before the job
      completes. Best-effort per document (unextractable/failed embeds are
      logged and skipped, not fatal to the job). Revisit sync vs. a
      background worker (Redis) once import volumes get large enough
      that embedding noticeably slows down `execute_job`.
- [x] Retrieval: `RetrievalService.search(query, top_k)` (`app/rag`) embeds
      the query and ranks `document_chunks` by pgvector cosine distance,
      joined to source `Document`. Exposed via `POST /rag/search`.
- [x] Text extraction by file type (`app/ingestion/text_extractor.py`):
      `.pdf` via pypdf, `.docx` via python-docx, everything else falls
      back to plain UTF-8 (unchanged for txt/md/code/etc.). No OCR or
      legacy `.doc` support yet — a scanned/image-only PDF yields no text
      (skipped gracefully, not an error).
- [ ] Reranking / relevance filtering beyond raw cosine distance (Phase 1's
      diagram anticipates this; not implemented — `/rag/search` returns
      raw nearest-neighbor results)

## Phase 2 — Conversation
- [ ] Chat endpoint wired to Ollama (`CHAT_MODEL`, currently `qwen3:8b` per config)
- [ ] RAG-augmented prompting: inject retrieved context, cite sources
- [ ] Conversation/session persistence

## Phase 3 — Memory
- [ ] Long-term memory store (`app/memory`): durable facts/preferences about
      the user, distinct from document retrieval
- [ ] Memory read/write hooks into the chat loop

## Phase 4 — Tool calling
- [ ] Tool registry and execution loop (`app/tools`)
- [ ] First tools: local file operations, local API calls

## Phase 5 — Repository health & knowledge management (KRM)
Tracked in [`docs/backlog.md`](../backlog.md); architecture TBD in
`docs/architecture/KRM_Architecture.md` (currently empty). Must-have items:
- [ ] Provenance chain (every derived fact traceable to a source file)
- [ ] Perceptual deduplication
- [ ] Immutable audit log
- [ ] Dry-run mode for destructive/derived operations
- [ ] Confidence review queue

Should/nice-to-have: temporal diffing, repository health score, best copy
arbitration, forgotten knowledge surfacing, topic drift timeline, decade
capsules. Future research: cross-source entity resolution, repository time
machine.

## Phase 6 — Frontend
- [ ] Not yet started. Chat UI + import job monitoring once Phases 1–2 are usable.

## Non-goals (for now)
- No cloud LLM fallback — offline-first is a hard requirement, not a default.
- No multi-user support — this is a single-user personal system.
