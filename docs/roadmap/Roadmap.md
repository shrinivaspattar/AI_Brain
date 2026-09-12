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
      `.pdf` (pypdf), `.docx` (python-docx), `.pptx` (python-pptx),
      `.xlsx` (openpyxl); everything else falls back to plain UTF-8
      (unchanged for txt/md/code/etc.). Tolerant of a conflict-
      resolution/dedup rename suffix (`.pdf_<timestamp>`, found for real
      in the user's corpus — see the `project-master-data-corpus` memory
      note for the full extension survey and what got fixed vs.
      deliberately left out of scope). No OCR or legacy
      `.doc`/`.ppt`/`.xls` support yet — a scanned/image-only PDF yields
      no text (skipped gracefully, not an error).
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
- [x] Write hook into the chat loop, review-gated: `Memory.status`
      (migration `bb3be4e51d72`) — `pending`/`approved`/`rejected`. The
      model can propose a memory via the new `remember` tool, but a
      proposal is always `PENDING` and never reaches the read-hook
      (which explicitly filters `status=APPROVED`) until a human calls
      `POST /memory/{id}/approve` (or `/reject`, which keeps the row as
      a record rather than deleting it). `POST /memory` (human-authored,
      via the API) still writes `APPROVED` directly — typing "remember
      this" is already the confirmation step. This is the "review queue"
      design the note above called for, echoing the KRM backlog's
      "Confidence Review Queue". Verified end-to-end against the real
      model through the full cycle: propose → confirm pending →
      confirm a genuinely separate conversation has no knowledge of it
      → approve → confirm it's used afterward.
- [ ] Relevance-based memory retrieval — currently "most recent N", not
      similarity-ranked like document retrieval. Fine at low volume; revisit
      if the memory store grows large enough that recency stops being a
      good proxy for relevance.

## Phase 4 — Tool calling (started)
- [x] Confirmed `qwen3:8b` advertises Ollama's `tools` capability before
      building around it (`ollama /api/show`).
- [x] Tool registry and execution loop: `Tool`/`ToolRegistry`
      (`app/tools/registry.py`). `ToolRegistry.call()` never raises — an
      unknown tool or handler exception becomes a result string fed back
      to the model, not a crashed chat turn.
- [x] `ChatService._run_tool_loop`: real agentic loop — send
      messages+tools, execute any requested tool calls, feed results back
      as `role="tool"` messages, repeat until a plain reply or
      `MAX_TOOL_ITERATIONS` (5) is hit (graceful fallback message, not an
      infinite loop). Verified end-to-end against the real model: asked
      for the current date/time, got a minute-precise real answer the
      model could only have produced by actually calling the tool.
- [x] First tools (`app/tools/builtin.py`), read-only over the user's
      data: `search_knowledge_base` (explicit on-demand RAG via
      `RetrievalService`), `get_current_datetime`, `list_recent_documents`.
      Plus `remember` (Phase 3) — the one narrow exception, and even it
      never writes anything live; see Phase 3. Exposed via `GET /tools`.
- [x] Persisted tool-call audit trail: `ToolCallRecord` (migration
      `db6a38db9323`) — conversation/message linkage, tool name,
      iteration/call_index, arguments, status, truncated result (full
      result still goes to the model; only the stored copy is capped at
      `MAX_TOOL_RESULT_LENGTH`=4000 chars), error message, duration_ms.
      Audit persistence is best-effort — a DB failure while writing a
      record is logged and swallowed, never breaks the chat turn.
      Explicitly an audit/debug record, never read back into a prompt.
      Verified end-to-end against the real model and directly against
      Postgres. This substantially covers the KRM backlog's "Immutable
      Audit Log" item for tool calls specifically (not yet extended to
      other subsystems).
- [ ] File operations / external API tools — deliberately not built yet.
      A materially bigger security surface for a system meant to index
      720GB of personal data (see the `project-master-data-corpus`
      memory note) than the read-only tools above; needs its own explicit
      scoping decision (which paths, read vs. write, sandboxing) rather
      than being bundled into "first tools." If/when they land, revisit
      whether tool arguments/results need redaction before persisting —
      none of today's tools take secrets or return raw filesystem
      contents, so the audit trail didn't need that logic yet.

## Phase 5 — Repository health & knowledge management (KRM, started)
Tracked in [`docs/backlog.md`](../backlog.md); architecture TBD in
`docs/architecture/KRM_Architecture.md` (currently empty). Must-have items:
- [ ] Provenance chain (every derived fact traceable to a source file) —
      largely already true in practice (`Document.import_job_id`,
      `DocumentChunk.document_id`, `Message.conversation_id`,
      `ToolCallRecord`/`Memory` → `message_id` all link back to source),
      but never formalized/documented as one coherent "chain" end to end.
- [x] Perceptual deduplication — **detection only**, started:
      `DeduplicationService` (`app/dedup/service.py`) finds exact
      duplicates (`Document.content_hash`, SHA-256) and near-duplicate
      text documents (pgvector cosine similarity on first-chunk
      embeddings). Exposed via `GET /dedup/exact`, `GET /dedup/near`,
      and a `find_duplicate_documents` chat tool. Verified against real
      ingested duplicate files and the real model. **Not** included:
      Best Copy Arbitration (deciding which copy to keep), any actual
      file action (delete/move/quarantine — needs Dry-Run Mode first,
      below), and image perceptual hashing (pHash) — this only covers
      text documents with embeddings, not photos/images, which AI_Brain
      doesn't process at all yet.
- [x] Immutable audit log — substantially covered for tool calls
      specifically (`ToolCallRecord`, Phase 4) and memory proposals
      (`Memory.status`, Phase 3); not yet extended to other subsystems
      (e.g. ingestion decisions, dedup findings themselves).
- [ ] Dry-run mode for destructive/derived operations — not started;
      becomes relevant once any dedup/repository-health finding can
      trigger a real file action.
- [x] Confidence review queue — substantially covered for memory
      specifically (`Memory.status: pending/approved/rejected`,
      `POST /memory/{id}/approve`/`/reject`, Phase 3); not yet a
      general-purpose mechanism other subsystems (e.g. dedup arbitration)
      could reuse.

Should/nice-to-have: temporal diffing, repository health score, best copy
arbitration, forgotten knowledge surfacing, topic drift timeline, decade
capsules. Future research: cross-source entity resolution, repository time
machine.

## Phase 6 — Frontend
- [ ] Not yet started. Chat UI + import job monitoring once Phases 1–2 are usable.

## Non-goals (for now)
- No cloud LLM fallback — offline-first is a hard requirement, not a default.
- No multi-user support — this is a single-user personal system.
