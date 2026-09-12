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
- [x] Import job execute error handling: `POST /import-jobs/{id}/execute`
      returns a clean `422` (`detail` = the same message stored in the
      job's `error_message`) for an expected execution failure — a
      missing or invalid source path — instead of a bare 500. Found
      while building the Import Job Monitoring frontend view; fixed as
      its own follow-up commit rather than bundled into that one. The
      job row itself was always correctly persisted as `FAILED` either
      way; only the HTTP response to the `/execute` caller was wrong.
      `ValueError`-based state-conflict (409) and not-found (404)
      responses, and the success path, are unchanged.
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

## Phase 4 — Tool calling (done)
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
- [x] File operations, scoped: after an explicit scoping conversation
      (read-only only; paths restricted to a completed import job's
      `source_path`; any future destructive action must require human
      confirmation, the model must never be able to delete/move a file
      directly), added exactly one capability — `read_file_content`,
      via `FileAccessService` (`app/files/service.py`). It refuses any
      path that doesn't resolve under a COMPLETED `ImportJob.source_path`
      (`Path.resolve()` + `is_relative_to`, closing off `..` traversal
      the same way `ArchiveExtractor` already does), truncates at
      `MAX_FILE_READ_LENGTH` (20,000 chars), and reuses
      `text_extractor.extract_text` so PDF/DOCX/etc. read as text, not
      garbage bytes. No write, move, or delete tool exists — that
      remains explicitly out of scope pending its own confirmation-gate
      design. Verified end-to-end against the real model both ways: it
      correctly read a real ingested file and quoted an exact string
      from it, and separately, when told to read a system file
      (`/etc/hostname`), it called the tool, got refused, and correctly
      explained why — both calls confirmed in the real `ToolCallRecord`
      audit trail (`SUCCESS` and `ERROR` respectively).
      External-API tools remain fully out of scope; revisit tool-result
      redaction now that one tool *can* return raw file contents (not
      just chunk-level `search_knowledge_base` snippets) — no secrets
      have surfaced in practice yet, but this is the first tool where
      that's now possible in principle.

## Phase 5 — Repository health & knowledge management (KRM, started)
Tracked in [`docs/backlog.md`](../backlog.md); architecture TBD in
`docs/architecture/KRM_Architecture.md` (currently empty). Must-have items:
- [x] Provenance chain (every derived fact traceable to a source file) —
      formalized as an actual queryable trace, not just documented FKs:
      `ProvenanceService.trace_document` (`app/provenance/service.py`)
      walks `ImportJob` → `Document` (`import_job_id`) → `DocumentChunk`
      (count) → every `Message` whose denormalized `citations` names the
      document, exposed via `GET /documents/{id}/provenance`. Verified
      end-to-end against the real database and the real model: ingested
      a real file, confirmed the trace showed its import job and one
      chunk with an empty citation list, asked the model a question it
      answered by citing that document, then confirmed the same trace
      now included the real message/conversation that cited it.
      Scoped to `Document` as the trace root (the one entity everything
      else ultimately derives from); doesn't yet expose a symmetric
      trace starting from a `Memory` or `ToolCallRecord`, though both
      already carry the FKs (`conversation_id`/`message_id`) needed to
      walk in that direction if a future consumer needs it.
      Writing the real-DB test for this surfaced two unrelated bugs,
      both fixed in the same milestone: (1) a JSONB column storing
      Python `None` round-trips as a JSON `null`, not a SQL `NULL`, so
      `Message.citations.is_not(None)` doesn't reliably filter out
      citation-less messages - the service now re-checks in Python
      instead of trusting the query; (2) most integration tests in
      `test_import_execution.py` had never cleaned up after themselves
      against `aibrain_test` (only one, added for the dedup milestone,
      did) - across many suite runs this session they'd piled up 160
      import jobs' worth of documents/chunks with repeated deterministic
      embeddings, which pushed a real pair out of dedup's near-duplicate
      query past its result `limit` and made that test fail
      deterministically. All eight now clean up via a shared
      `_cleanup_import_job` helper; the accumulated pollution was
      deleted once and the full suite reran clean 5x in a row.
- [x] Perceptual deduplication — **detection only**, started:
      `DeduplicationService` (`app/dedup/service.py`) finds exact
      duplicates (`Document.content_hash`, SHA-256) and near-duplicate
      text documents (pgvector cosine similarity on first-chunk
      embeddings). Exposed via `GET /dedup/exact`, `GET /dedup/near`,
      and a `find_duplicate_documents` chat tool. Verified against real
      ingested duplicate files and the real model. **Not** included:
      any actual file action (delete/move/quarantine), and image
      perceptual hashing (pHash) — this only covers text documents with
      embeddings, not photos/images, which AI_Brain doesn't process at
      all yet.
- [x] Immutable audit log — substantially covered for tool calls
      specifically (`ToolCallRecord`, Phase 4) and memory proposals
      (`Memory.status`, Phase 3); not yet extended to other subsystems
      (e.g. ingestion decisions, dedup findings themselves).
- [x] Dry-run mode for destructive/derived operations — started, scoped
      to exact duplicates: `DeduplicationService.plan_exact_duplicate_cleanup`
      performs Best Copy Arbitration (keep the oldest copy by
      `created_at`, tie-broken by `id`; propose deleting the rest) and
      returns a plan — computing one has zero effect on any file or row.
      Exposed via `GET /dedup/exact/plan` and a `plan_duplicate_cleanup`
      chat tool. Verified end-to-end against the real model (it correctly
      called the tool and reported the same keep/delete decision the API
      returned) and confirmed via direct DB query that both documents
      were untouched. Deliberately **not** included: near-duplicate
      arbitration (similarity isn't identity — needs human judgment, not
      a heuristic) and any mechanism to actually execute a plan (still no
      code path anywhere that deletes, moves, or modifies a file).
- [x] Confidence review queue — substantially covered for memory
      specifically (`Memory.status: pending/approved/rejected`,
      `POST /memory/{id}/approve`/`/reject`, Phase 3); now also covered
      for dedup — see below — via the same status-enum pattern, not a
      shared/generic mechanism between the two (each has its own model).
- [x] Human-review decision model for dedup, designed and built (no
      frontend/API yet — see Architecture's "Dedup review decision
      model" for the full design): `DuplicateReview` +
      `DuplicateReviewMember` (migration `a7ad86ac80d3`),
      `DedupReviewService` (`app/dedup/review_service.py`). This is the
      DECISION stage inserted between existing DETECTION
      (`DeduplicationService`, stateless) and the still-nonexistent
      EXECUTION stage: `create_review_from_exact_group`/
      `create_review_from_near_pair` persist a detected finding as a
      reviewable record (evidence snapshot, confidence, a human-readable
      recommendation reason, PENDING status); `approve_review`/
      `reject_review` record a human decision. Neither creating nor
      reviewing a record touches a file, a `Document` row, or an
      `ImportJob` row — confirmed via real-database tests. Exact
      reviews get a proposed canonical (the existing, safe "oldest
      ingested" heuristic); near-duplicate reviews **always** propose no
      canonical at all — similarity is evidence that two documents are
      related, not evidence of which one is better, so the ambiguous
      case is a first-class, structurally-guaranteed outcome rather than
      a fallback. 18 new tests (unit + real-database), covering exact,
      near/ambiguous, already-reviewed, invalid-id, provenance
      preservation, and conflicting sequential review calls. Deliberately
      **not** built this milestone: any API endpoint, any frontend, and
      any execution mechanism — this was a design-and-model-only
      milestone, stopped for review before proceeding.

Should/nice-to-have: temporal diffing, repository health score, best copy
arbitration, forgotten knowledge surfacing, topic drift timeline, decade
capsules. Future research: cross-source entity resolution, repository time
machine.

## Phase 6 — Frontend (started)
- [x] Stack decision, made explicitly before writing any code: plain
      HTML/CSS/vanilla JS served directly by FastAPI (`app.mount("/",
      StaticFiles(directory=BASE_DIR / "frontend", html=True))`) — no
      Node/npm toolchain, no build step, no second process/port. First
      slice scoped to chat only; import job monitoring, memory review,
      and dedup review are deliberately deferred to later slices rather
      than bundled in.
- [x] Chat UI (`frontend/index.html`/`style.css`/`app.js`): single page,
      message history, citations rendered under each assistant reply,
      light/dark theme via `prefers-color-scheme`. Conversation
      continuity across a page reload via `localStorage` (stores
      `conversation_id`, replays history through the existing
      `GET /chat/{id}`) — a stale/deleted conversation id is detected
      (404) and cleared automatically rather than getting stuck. A "New
      conversation" button clears it explicitly. Message content is
      rendered via `textContent`, never `innerHTML`, so a message
      containing markup (from the model or pasted by the user) can't
      inject into the page.
      Verified in a real browser against the real backend and a real
      `qwen3:8b` call: ingested a real file, asked a question through
      the UI, confirmed the rendered answer and citation matched the
      ingested content: reload-persistence, "New conversation" reset,
      the empty-submit no-op, and the stale-conversation-id recovery
      were all exercised directly in the browser, not just asserted.
- [x] Import job monitoring: a second view (`nav-import-jobs-btn`) added
      to the same single page, reusing the existing `GET /import-jobs`
      endpoint verbatim — no new backend endpoints, no business logic
      duplicated in JS (every field shown is exactly what the API
      returned: name, source path/type, status, progress, files
      discovered/processed, error message, all four timestamps).
      Read-only by design: no create/start/execute/retry buttons exist
      in the UI — those remain API/curl-only, matching the explicit
      "monitoring must remain read-only" scoping constraint for this
      slice. Status is shown as a colored badge for each of the six
      `ImportStatus` values (PENDING/RUNNING/PAUSED/COMPLETED/FAILED/
      CANCELLED); a FAILED job's `error_message` renders in a dedicated
      error block. Polling: refetches every 5s only while at least one
      listed job is non-terminal, and only while the Import Jobs view is
      the one currently on screen — switching to Chat stops it
      immediately (verified via the network log: request count stayed
      flat for 8+ seconds after switching away), so a long-running
      import can't interfere with the chat experience. A manual
      "Refresh" button is always available regardless of polling state.
      Verified end-to-end in a real browser against the real database:
      created real PENDING/COMPLETED/FAILED jobs, confirmed each
      rendered with correct badge/progress/error text, then executed the
      PENDING job via a separate curl call and watched the UI update
      itself on its next poll tick with no manual action — proving the
      polling loop is genuinely live, not just present in the code.
      Also confirmed chat (including citations and reload-persistence)
      is unaffected by this addition.
      A bug was found during this verification and fixed as a separate
      follow-up commit: `POST /import-jobs/{id}/execute` used to return
      a bare 500 when the source path didn't exist or wasn't a
      directory, because the handler only caught `ValueError`, not the
      `FileNotFoundError`/`NotADirectoryError` the service actually
      raises for those cases. It now returns a clean `422` with
      `detail` matching the job's own `error_message` — see "Import job
      execute error handling," below.
- [x] Memory review queue: a third view (`nav-memory-btn`) reusing the
      existing `GET /memory?status=`, `POST /memory/{id}/approve`, and
      `POST /memory/{id}/reject` endpoints verbatim — no new backend
      endpoints, no memory business logic duplicated in JS. Filter tabs
      (Pending/Approved/Rejected/All) map straight to the existing
      `status` query param. Each candidate shows its content, confidence
      (or "not scored"), created timestamp, and a status badge; Approve/
      Reject buttons only appear for `pending` memories, matching the
      one-way candidate → review → approved/rejected flow (the backend
      itself doesn't enforce this - see below).
      Provenance: `conversation_id`/`message_id` are always shown as raw
      references; when a `message_id` exists, an on-demand "View source
      message" toggle fetches the existing `GET /chat/{conversation_id}`
      endpoint (no new endpoint - see the "Design trade-off" note below)
      and displays a truncated snippet of that specific message plus its
      citations, so a human reviewer can see exactly what grounded the
      proposal without a full document dump.
      Safety: approve/reject require an explicit two-click in-page
      confirmation (arm, then confirm within 4s) rather than a native
      `window.confirm()` - found during browser verification that native
      dialogs are unreliable to interact with via browser automation and
      are visually inconsistent with the rest of the app anyway; the
      in-page pattern is more robust and equally explicit. No bulk-approve
      exists anywhere in the UI.
      Verified end-to-end in a real browser against the real database and
      a real chat turn: asked the real model to remember a synthetic fact,
      confirmed the resulting PENDING memory appeared with correct
      provenance (including the `message_id` backfill that only completes
      once the turn fully finishes), approved one candidate and rejected
      another, confirmed both moved between filter tabs correctly and
      their source Message/Conversation rows were completely unmodified
      afterward, and reconfirmed Chat and Import Job Monitoring both
      still work unaffected.
      **Design trade-off, deliberately not addressed**: reusing
      `GET /chat/{conversation_id}` to find one specific message means
      fetching the whole conversation's history client-side rather than
      a single message - acceptable for now (personal-scale conversation
      lengths, and it's an on-demand fetch, not eager), but a dedicated
      `GET /messages/{id}` endpoint would be more precise if this becomes
      a real cost.
      **Also found, not changed**: `approve_memory`/`reject_memory` have
      no guard against reviewing an already-reviewed memory - either can
      be called from any status and the last call wins. The UI never
      exposes this path (buttons only show for `pending`), but the API
      itself permits it; documented and tested (both service- and
      HTTP-level), not fixed, since nothing in this milestone's scope
      needed it changed.
- [ ] Dedup review — frontend/API not started; its own later slice, now
      unblocked by the KRM `DuplicateReview` decision model above (see
      Phase 5).
- [ ] No conversation list/switcher yet — only ever one active
      conversation per browser (`localStorage`), matching the
      chat-only scope of the original slice.
- [ ] No job-creation form in the UI — creating/executing an import job
      remains API/curl-only, consistent with "monitoring, not management."

## Non-goals (for now)
- No cloud LLM fallback — offline-first is a hard requirement, not a default.
- No multi-user support — this is a single-user personal system.
