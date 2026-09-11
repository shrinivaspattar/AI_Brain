# AI_Brain Roadmap

Target end-state: a personal knowledge assistant. Ingest your documents,
notes, and archives; chat with a fully offline LLM that can search and cite
your own knowledge base (RAG); have it remember things about you over time.
See [`docs/architecture/AI_Brain_Architecture.md`](../architecture/AI_Brain_Architecture.md)
for the current module-by-module status this roadmap tracks against.

## Phase 0 — Ingestion foundation (done)
- [x] Source scanning (`SourceScanner`)
- [x] Archive extraction with bomb / path-traversal / disk-space
      protection (`ArchiveExtractor`), `.zip` and `.7z` — `.7z` added
      after finding the user's real ~720GB archive corpus leans heavily
      on it for large backups. No `.tar`/`.tar.gz`/`.rar` support yet.
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

## Phase 2 — Conversation (done)
- [x] Chat endpoint wired to Ollama: `POST /chat` (`CHAT_MODEL=qwen3:8b`),
      via `ChatService`/`ChatClient` (`app/services`)
- [x] RAG-augmented prompting: retrieves top-k chunks via `RetrievalService`,
      injects them as numbered `[n]` context in the system prompt; verified
      end-to-end against the real model — it correctly cites `[n]` and uses
      prior turns' context on follow-up questions
- [x] Conversation/session persistence: `Conversation` + `Message` models
      (migration `ece8496c665b`), `GET /chat/{conversation_id}` for history.
      `Message.citations` is a denormalized snapshot (chunk id, document id/
      title/source) so citations stay legible even if the chunk is later
      re-embedded or deleted.
- [ ] Streaming responses (currently a single blocking call/response)
- [ ] Conversation titles/summaries, listing/deleting conversations

## Phase 3 — Memory
- [x] Long-term memory store: `Memory` model (migration `e67e8741cbc1`),
      `MemoryService` (`app/memory/service.py`). Flat store — content +
      optional confidence (0-1) + optional provenance (conversation_id/
      message_id it was derived from). No type taxonomy (episodic/semantic/
      preference/etc.) — deliberately not invented until something actually
      needs it, per the original project brief.
- [x] API: `POST /memory`, `GET /memory`, `DELETE /memory/{id}`.
- [x] Read hook into the chat loop: `ChatService` loads up to 50 memories
      per turn and injects them into the system prompt as a "What you know
      about the user" block, separate from the numbered document-context
      block (citation `[n]` markers apply only to the latter). Verified
      end-to-end against the real model.
- [ ] Write hook into the chat loop: writing is explicit-only for now (via
      the API). Automatic LLM-driven extraction of "facts" from casual
      conversation is a real quality/trust risk (a hallucinated "fact"
      silently becoming permanent memory) — needs a deliberate design
      (confirmation step? confidence threshold? review queue, echoing the
      KRM backlog's "Confidence Review Queue"?), not a default-on behavior.
- [ ] Relevance-based memory retrieval — currently "most recent N", not
      similarity-ranked like document retrieval. Fine at low volume; revisit
      if the memory store grows large enough that recency stops being a
      good proxy for relevance.

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
