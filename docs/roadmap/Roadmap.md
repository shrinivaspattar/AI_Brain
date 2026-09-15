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
- [x] Human-review decision model for dedup, now with an API and a
      frontend view (see Architecture's "Dedup review decision model"
      for the full design): `DuplicateReview` + `DuplicateReviewMember`
      (migrations `a7ad86ac80d3`, `6af80a472ca2`), `DedupReviewService`
      (`app/dedup/review_service.py`), `GET /dedup/reviews`\|`/{id}`,
      `POST /dedup/reviews/{id}/approve`\|`/reject`. This is the
      DECISION stage inserted between existing DETECTION
      (`DeduplicationService`, stateless) and the still-nonexistent
      EXECUTION stage. Neither creating nor reviewing a record touches a
      file, a `Document` row, or an `ImportJob` row — confirmed via
      real-database tests and, now, via a real browser session too.
      Design refinements added after the initial model was reviewed:
      the system's suggestion (`RECOMMENDED_CANONICAL` member role,
      renamed from `PROPOSED_CANONICAL` for clarity) and an actual human
      decision (`human_selected_canonical_document_id`) are two
      explicitly separate fields, never conflated — approving an EXACT
      review requires the caller to pass a real `canonical_document_id`
      even when it matches the recommendation; nothing is ever promoted
      automatically. Near-duplicate reviews can optionally record a
      human-chosen canonical too (explicit only, never inferred), or
      none at all — "confirmed as related, no canonical chosen" is a
      first-class, valid outcome. `approve_review`/`reject_review` also
      became stricter than `MemoryService`'s permissive precedent: a
      review that's already been decided cannot be reviewed again
      (409 Conflict) — a deliberate divergence, since this stage will
      eventually gate a real filesystem action and silent inconsistent
      decisions are exactly what this design exists to prevent.
      Frontend: a fourth view (`nav-dedup-review-btn`) with the same
      filter-tab/card pattern as Memory Review, a persistent "Reviewing
      this finding does not modify your files" banner, and no delete/
      move/quarantine control anywhere. EXACT and NEAR findings are
      visually distinct (a purple vs. orange match-type badge) and a
      canonical-choice radio group only lets an EXACT review's Approve
      button enable once a real member is selected.
      33 new tests total across both milestones (unit, API, real-database
      integration, frontend-serving), covering exact, near/ambiguous,
      already-reviewed (now a 409 rejection, not a silent overwrite),
      invalid/missing canonical selection, missing members, provenance
      preservation, and conflicting sequential review attempts.
      Verified end-to-end in a real browser: created controlled synthetic
      exact and near findings, approved the exact one after selecting
      its canonical, rejected the near one, confirmed both moved to the
      correct filter tab and their source Document/ImportJob rows were
      completely unmodified, and reconfirmed Chat, Import Job
      Monitoring, and Memory Review are all unaffected.
      Still deliberately **not** built (at the time): any execution
      mechanism — there was still no code path anywhere in AI_Brain
      that deletes, moves, renames, quarantines, or overwrites a file.
- [x] Dry-run execution planning layer — the DECISION stage's natural
      successor, and still strictly upstream of any real filesystem
      action: `DedupExecutionPlan` + `DedupExecutionPlanAction`
      (migration `8eebb7e854fb`), `DedupExecutionPlanService`
      (`app/dedup/execution_plan_service.py`), `POST
      /dedup/reviews/{id}/plans`, `GET /dedup/plans`\|`/{id}`\|`/{id}/validity`.
      `generate_plan_for_review` only accepts an **APPROVED** review
      that carries an explicit `human_selected_canonical_document_id`
      (409 if not approved, 422 if approved with no canonical decision
      — the near-duplicate "confirmed related, undecided" case) — reads
      every member's file from disk *right now* (SHA-256 + size, a
      plain read) and persists an immutable snapshot: what would happen
      to which file, why, and what it looked like at that moment. Every
      call creates a brand-new plan row; regenerating is expected, not
      an error, matching the audit-trail convention of never
      overwriting history. `check_plan_validity` re-reads the
      filesystem *and* the live `Document` rows again, freshly, on
      every call — no cached verdict is ever stored — and reports six
      independent staleness dimensions per file (document still exists,
      path unchanged, file still exists, type/extension matches, hash
      matches, size matches), collapsing to one `is_valid` a future
      executor must check before acting. Real-filesystem tests (real
      temp files, not the corpus) proved this actually catches a file
      edited or deleted after plan generation, and that generating a
      plan and checking its validity never modify, move, rename, or
      delete anything — confirmed via byte-for-byte comparison before
      and after. Deliberately not built: any endpoint or mechanism that
      turns a plan into a real filesystem action — that remains a later,
      separately-approved milestone (its own explicit "execution
      authorization" step, re-validating against this same staleness
      check immediately before acting, is the natural next design).
      41 new tests (unit incl. real synthetic files via `tmp_path`, API,
      real-database + real-filesystem integration).
- [x] Explicit execution authorization layer — the one thing in this
      pipeline that is actual permission, and still strictly upstream
      of any real filesystem action: `DedupPlanAuthorization` (migration
      `d0ac67db7d95`), `DedupPlanAuthorizationService`
      (`app/dedup/authorization_service.py`), `POST
      /dedup/plans/{id}/authorize`, `GET /dedup/authorizations`\|`/{id}`\|`/{id}/currency`,
      `POST /dedup/authorizations/{id}/revoke`. **Approval is not
      execution authorization. Authorization is not execution.** —
      neither `DuplicateReview.status == APPROVED` nor
      `DedupExecutionPlan.status == GENERATED` is ever treated as
      permission to touch a file anywhere in this codebase; only a
      `DedupPlanAuthorization` row with `status == authorized`
      represents that, and even that performs zero filesystem writes
      itself. `authorize_plan` re-checks everything fresh, in order,
      before creating a row: the plan exists (404), its review is
      **APPROVED** (409), no other *active* authorization already
      exists for this plan (409 — at most one active authorization per
      plan, enforced at the service level), and — reusing the existing
      `check_plan_validity` verbatim, not duplicated — the plan still
      passes a validity re-check run at that exact moment (422 if
      stale). A stale plan is refused outright, never silently
      regenerated, updated, or patched — a human must deliberately
      generate and review a new plan. Requires an explicit
      `confirm: true` in the request body; there is no default that
      authorizes anything from an empty request. **Filesystem state is
      revalidated immediately before execution** — `validity_snapshot`
      freezes what was true *at authorization time*, and is explicitly
      not a permanent guarantee; the new `GET
      /dedup/authorizations/{id}/currency` endpoint (the TOCTOU check)
      re-runs `check_plan_validity` fresh on every call and reports a
      combined `is_still_actionable`, proven against real files to flip
      from `true` to `false` the instant a planned file is edited after
      authorization, while the authorization's own frozen snapshot
      stays exactly as it was. A future filesystem executor must still
      perform its own fresh revalidation immediately before every
      mutation regardless of this check. 45 new tests (17 unit, 15 API,
      13 real-database + real-filesystem integration), covering every
      case from the spec: approved+valid succeeds; pending/rejected
      review rejected; stale plan (changed hash/size/path/file
      type/missing source) rejected; nonexistent plan 404; duplicate
      active authorization rejected; an authorization proven bound to
      its exact plan and unusable for any other plan, including a
      second plan from the same review; the plan, its review, and both
      real synthetic files proven byte-for-byte unchanged by
      authorization, revocation, or a currency check. Verified against
      the real running API and the real `aibrain` database (synthetic
      temp files only) through the full 8-step protocol: authorize a
      valid plan → no mutation → edit the file → old plan's
      authorization now refused → generate a fresh plan → it captures
      the new file state → authorize the fresh plan → still no
      mutation — then fully cleaned up with zero leftover rows.
      Deliberately not built: any endpoint or executor that consumes an
      authorization to perform a real file action, any execution-audit
      record (nothing exists yet to audit), and any frontend (this
      layer's "Authorize this exact plan for future execution" concept
      is the highest-stakes yet exposed by this system; its UI is
      deferred until a real executor exists to authorize *for*).
- [x] Execution audit model — prepares this pipeline for a future
      filesystem executor without introducing one: `DedupExecution` +
      `DedupExecutionActionAudit` (migration `16a7cf136058`),
      `DedupExecutionService` (`app/dedup/execution_service.py`),
      `POST /dedup/authorizations/{id}/executions`, `GET
      /dedup/executions`\|`/{id}`\|`/{id}/actions`, `POST
      /dedup/executions/{id}/actions`, `POST
      /dedup/executions/{id}/complete`. Every method is pure
      bookkeeping — none performs a filesystem operation; there is
      still no filesystem executor anywhere in this codebase.
      **Plan vs. actual, made structural**: `DedupExecutionActionAudit`
      freezes what was *planned* (source/target path, expected
      hash/size, action type — copied straight from the plan action,
      never accepted as request input) alongside what *actually
      happened* (result, observed hash/size, whether a mutation truly
      occurred, error detail) — a `PRECONDITION_FAILED` row proves a
      mismatch was caught with `filesystem_mutation_occurred=false`
      sitting right next to the expected/observed values that
      triggered it. **State machine**: `RUNNING` →
      `COMPLETED`/`FAILED`/`PARTIALLY_COMPLETED`, always *derived* from
      the execution's own action-audit rows by `complete_execution`,
      never accepted as caller input — all-`SUCCESS` is `COMPLETED`,
      zero-`SUCCESS` is `FAILED`, some-but-not-all is
      `PARTIALLY_COMPLETED`. No `EXECUTION_STARTED`/pending states
      exist — a row is only ever created already `RUNNING`, matching
      how `authorize_plan` never creates a row for a failed attempt
      either. **Failure semantics — stop on first unexpected failure,
      always**: the spec's own worked example (10 actions, 2 succeed,
      1 fails, 7 never attempted) is represented as exactly 10 audit
      rows — 2 `SUCCESS`, 1 `FAILED`, 7 `NOT_ATTEMPTED` — with
      `NOT_ATTEMPTED` an explicit recorded fact, never inferred from a
      missing row. `complete_execution` refuses to finalize (422)
      unless every planned action has exactly one recorded outcome —
      "do not claim the whole plan completed" is enforced structurally.
      **Duplicate execution prevention**: an authorization backs at
      most one execution *ever* (`UNIQUE` constraint on
      `dedup_executions.authorization_id`, verified to raise a real
      Postgres `IntegrityError` when bypassed) — stricter than
      authorization's own "at most one active" rule, since there is no
      retry path through the same authorization; a second attempt
      needs a brand-new authorization. `start_execution` re-checks
      everything fresh — authorization exists, is currently
      `AUTHORIZED`, has no existing execution, and the plan still
      passes a `check_plan_validity` re-check run at that exact moment
      (the TOCTOU check: being `AUTHORIZED` doesn't mean the files
      haven't changed since). **Documented, not implemented**: the
      future executor's required per-action sequence (load
      authorization → confirm still valid → load plan → revalidate →
      **revalidate the source file immediately before mutation, not
      once for the whole plan** → perform the operation → verify →
      record the audit → continue/stop per policy), and the
      reversible-delete decision — recommending quarantine/staging
      over permanent deletion for AI_Brain's first executor, noting
      explicitly that no `_DUPLICATES_QUARANTINE` mechanism exists
      anywhere in this codebase today. 59 new tests (26 unit, 22 API,
      11 real-database integration), covering successful/failed/
      partially-completed representation, the plan-vs-actual
      distinction, authorization linkage, invalid/revoked
      authorization, a plan gone stale after authorization, duplicate
      execution prevention (service-level and real Postgres
      `IntegrityError`), and audit-row immutability. Verified against
      the real running API and the real `aibrain` database: a
      synthetic file changed externally (never through any AI_Brain
      code path) after execution start, recorded as
      `PRECONDITION_FAILED` with zero mutation, execution correctly
      derived to `FAILED`, a second execution attempt under the same
      authorization correctly refused — then fully cleaned up with
      zero leftover rows. Deliberately not built: the filesystem
      executor itself, any endpoint that performs a real file action,
      and any frontend.
- [x] Executor Safety & Recovery Design — a dedicated design pass on
      the single hardest correctness boundary in this architecture
      (what happens at, and around, the moment of a filesystem
      mutation, including a crash), kept deliberately separate from
      writing the executor. **No filesystem mutation capability was
      added, no quarantine mechanism was implemented, and no executor
      code exists anywhere in this codebase after this milestone.**
      Two schema additions (migration `6189c90d31a8`) exist only
      because the audit model built in the prior milestone genuinely
      could not represent "we don't know" without them:
      `DedupExecutionActionResult.UNKNOWN` (the outcome of an action
      that was attempted, or may have been, but whose fate could not
      be confirmed before the process that would have recorded it
      died — always paired with `filesystem_mutation_occurred=None`
      and `ended_at=None`, never written by a live executor, only by a
      future recovery step reconstructing what it can) and
      `DedupExecutionStatus.NEEDS_REVIEW` (an execution finalized with
      any `UNKNOWN` action — takes priority over
      COMPLETED/FAILED/PARTIALLY_COMPLETED regardless of how many
      other actions cleanly succeeded, since this system must never
      describe an execution's fate as known when part of it isn't).
      `filesystem_mutation_occurred` became genuinely tri-state
      (`True`/`False`/`None` — a plain boolean cannot represent
      "unknown"), enforced by `record_action_result`: every result but
      `UNKNOWN` must report a definite value, and `UNKNOWN` may never
      report anything but `None`. **Reversible-delete decision made
      explicitly, not implemented**: quarantine/staging recommended
      over permanent deletion, with a documented destination
      convention (namespaced by execution/plan-action id, making
      filename collisions structurally impossible), and a documented
      atomicity requirement (same-filesystem moves only — a
      cross-device `EXDEV` must be treated as FAILED, never silently
      degraded to non-atomic copy+delete). **Audit ordering contract
      made explicit**: mutation first, `record_action_result` after,
      never the reverse — a crash between those two steps leaves no
      row at all, genuinely ambiguous between "not reached yet" and
      "attempted, then lost," resolvable only by a future recovery
      step gathering independent evidence. **A real bug was found and
      fixed while building this**: a column-level `default=False`
      silently coerced an explicit `None` into `False` at insert time
      (SQLAlchemy cannot distinguish a deliberate `None` from an unset
      value for a scalar default) — caught by the real-Postgres
      integration test asserting a NULL round-trip, not by any mocked
      unit test, which is exactly the case for insisting on real-
      database verification here. **Idempotency documented**: the
      existing one-execution-per-authorization constraint already
      prevents a naive restart-and-retry; a genuinely new attempt
      needs a new authorization, which itself requires the old one to
      be explicitly revoked first — verified directly that a consumed
      (already-executed) authorization still reads as active and
      blocks re-authorization of its plan until revoked, an
      intentional, auditable boundary rather than an automatic one.
      **A real, currently-unresolved gap was also surfaced and locked
      in with a test**: re-planning a partially-executed review's
      remaining work isn't actually possible today, since a
      regenerated plan will always include already-resolved
      (now-missing) members and `check_plan_validity` can never
      validate an action whose file was already gone at that plan's
      own generation time — blocking authorization of the entire new
      plan, not just the outstanding part. **Concurrency decision**:
      the existing DB-level unique constraints (verified to raise real
      `IntegrityError`s) are judged sufficient given AI_Brain's
      single-process, single-user architecture — no distributed
      locking infrastructure was built for a multi-worker scenario
      that cannot occur today. 15 new tests (8 unit, 3 API, 4
      real-database integration). Full suite: 466 tests, three
      consecutive runs, zero flakiness. Verified against the real
      running API and `aibrain`: a synthetic execution with one
      SUCCESS and one UNKNOWN action correctly finalized to
      NEEDS_REVIEW, the null mutation flag confirmed round-tripping
      through real Postgres, the consumed-authorization/explicit-
      revoke interaction reproduced exactly as documented — then fully
      cleaned up with zero leftover rows.
- [x] Execution Recovery & Partial-Replanning Design — resolves the two
      gaps the prior milestone explicitly surfaced and deferred (crash
      closure, and re-planning after partial execution), kept
      deliberately separate from the executor itself. **Still no
      filesystem mutation capability anywhere in this codebase** —
      every change here either reads the filesystem without writing to
      it, or changes what a plan *proposes*, never what happens to a
      real file. `DedupExecutionService.recover_stale_execution` closes
      out a `RUNNING` execution that will never progress further (an
      explicit, human-confirmed action — AI_Brain has no process
      supervision and cannot know a process is actually dead; a
      `started_before` filter on `GET /dedup/executions` helps a human
      find *candidates*, never asserts one is stuck). For every
      unresolved planned action, it re-observes the file and picks
      between exactly two outcomes, never a third that guesses success:
      a file confirmed byte-identical to the plan's pre-mutation
      expectation is `NOT_ATTEMPTED` (the mutation demonstrably did not
      happen — if it had, the file couldn't still be there unchanged);
      anything else (missing, or changed) is `UNKNOWN`, with what was
      observed attached for a human to investigate — a missing file is
      *consistent with* success but is never promoted into a claimed
      one, since recovery cannot rule out the file vanishing for an
      unrelated reason. Recovery then finalizes through the *existing*
      `complete_execution` — no new state-machine logic was needed,
      since any `UNKNOWN` row already forces `NEEDS_REVIEW`.
      **The prior milestone's re-planning gap is fixed**:
      `generate_plan_for_review` now excludes a non-canonical member
      from a regenerated plan's actions entirely if its file no longer
      exists at generation time — a live re-check (reusing the same
      file read the method already performed), not a stored "resolved"
      flag or a `SUCCESS`-audit lookup. That's deliberately more
      general than tracking resolution history: it correctly excludes
      both a cleanly-`SUCCESS`-recorded deletion AND a crash-recovered
      `UNKNOWN` case where the file also happens to be gone, closing
      the loop for the messier scenario too. If every non-canonical
      member is already gone, the whole call now raises (422) — nothing
      to plan. A plan's API response gained a computed (never stored)
      `excluded_document_ids` field so a human sees which members were
      left out of a smaller-than-expected plan, without the field
      guessing why. Answered, requiring zero new code: a regenerated
      plan always needs its own fresh authorization (plans and
      authorizations are already bound 1:1). Left deliberately
      unresolved, and documented as such rather than papered over: once
      a human manually confirms an `UNKNOWN` finding really was a
      successful deletion, there is still no supported way to convert
      that into a recorded `SUCCESS` fact after the fact (audit rows
      are immutable by design) — safe in practice, since the
      live-file-existence exclusion above still drops that document
      from a future plan once its file is actually gone, but not smooth
      until a real executor makes this scenario worth resolving
      further. 19 new tests (8 unit, 6 API, 5 real-database
      integration) plus two existing tests intentionally rewritten to
      prove fixed behavior rather than document a since-resolved gap.
      Full suite: 487 tests, three consecutive runs, zero flakiness.
      Verified against the real running API and `aibrain`: a
      partially-executed review recovered via `POST
      /dedup/executions/{id}/recover`, correctly classifying an
      untouched action `NOT_ATTEMPTED`, then a regenerated plan
      correctly excluding only the genuinely-resolved document and
      authorizing successfully in a single call — then fully cleaned up
      with zero leftover rows.
- [x] Filesystem Executor Design (no implementation) — settles every
      open question before writing any mutation code, kept as its own
      milestone specifically so executor design and executor
      implementation stay separate decisions. **Zero code changed** —
      no model, migration, service, API, or test; nothing in this
      codebase performs a filesystem mutation after this milestone any
      more than before it. Key decisions: the operation is a
      same-filesystem `rename()` (move to quarantine), never a copy or
      an unlink — `DedupPlanActionType.DELETE` keeps meaning "remove
      this duplicate" as user intent, permanently mapped to a
      reversible quarantine move, not a stand-in for eventual real
      deletion (a genuinely irreversible delete, if ever added, would
      need its own new, separately-authorized action type). **A real,
      verified finding**: checking this actual deployment (read-only
      `stat`, no mutation) showed `BASE_DIR` and the real corpus
      (`/mnt/t7ssd/vscode/data`) sit on different filesystem devices —
      the natural instinct to put a quarantine directory under
      `BASE_DIR` would force a non-atomic cross-device move on every
      single quarantine operation, exactly the failure mode the design
      exists to forbid; the quarantine root must instead live on the
      same device as the corpus. A complete crash-window inventory
      (seven distinct windows, W1–W7) maps every point a process could
      die relative to the mutation-then-audit ordering contract to
      exactly what recovery would see and how it's already handled —
      including confirming a crash during `complete_execution` itself
      (after every action is already correctly audited) is already
      handled correctly today by recovery's existing "nothing to
      recover" refusal. **Recovery/reconciliation fully designed, not
      built**: a proposed `DedupExecutionActionReconciliation` record
      — referencing an existing `UNKNOWN` audit row, never editing it
      — for when a human later verifies what an indeterminate action's
      outcome actually was, keeping audit immutability intact while
      still capturing the human's later-confirmed truth; purely
      forensic, since live-file-existence exclusion already makes
      re-planning safe with or without a reconciliation record. New
      safety requirements specified for the first time: allowed
      mutation roots with path canonicalization (defending against a
      `Document` row somehow pointing outside the corpus), refusing
      symlinks and multi-linked files outright (a hard link's data
      would survive quarantining one name, misleadingly implying the
      duplicate is gone), and requiring a regular file before touching
      anything. Refined `FAILED`'s semantics for this specific
      operation: because a same-device `rename()` is atomic,
      `FAILED` for this executor always means
      `filesystem_mutation_occurred=False` — an atomic operation either
      fully succeeds or has no effect. Closed with an explicit list of
      what the executor may never touch (the canonical's file, anything
      outside its own plan's actions or the allowed roots, an earlier
      run's quarantined output, and — always — `Document` rows
      themselves, since filesystem state and database records stay
      deliberately decoupled).
- [x] Filesystem executor implementation (synthetic-filesystem-only) —
      the first code in AI_Brain that can perform a real filesystem
      mutation, implementing exactly the reviewed design and nothing
      more: `DedupFilesystemExecutor` (`app/dedup/executor.py`),
      **not wired into any API endpoint, router, or `app/main.py`** —
      only ever constructible from trusted Python code, today its own
      tests. `__init__(db, allowed_root, quarantine_root)` has no
      default for either root anywhere in the module — omitting one
      raises `TypeError` before anything else runs, and a source-scan
      test asserts the module contains no string reference to the real
      corpus location at all; that plus a real sibling-directory device
      check under `tmp_path` are how "cannot be selected implicitly"
      is actually proven, not just asserted. The constructor also
      refuses non-directory roots, nested/identical roots, and —
      carried over from the design milestone — roots on different
      filesystem devices. `execute(execution_id, *, confirm)` requires
      explicit confirmation (no API layer exists yet to gate this
      itself), runs exactly once against a fully fresh `RUNNING`
      execution with zero existing audits (refusing outright — not
      guessing — if called twice, or on a partially-audited execution;
      that case belongs to `recover_stale_execution` instead), and
      performs the full per-action pipeline independently for every
      action: authorization/plan re-validation scoped to that one
      action specifically (not the whole plan's aggregate validity,
      which would wrongly block a fine action over an unrelated one
      gone stale), symlink refusal on the un-resolved path, resolve
      and verify containment inside `allowed_root` on the *resolved*
      path (defeating a symlinked intermediate directory), regular-
      file and hard-link checks, a **second**, later hash/size
      re-observation deliberately redundant with the plan-validity
      check moments earlier (each check closer to the mutation shrinks
      the TOCTOU window), a same-device check, a destination-collision
      check that is load-bearing rather than defensive theater
      (`os.rename()` silently overwrites an existing destination on
      POSIX with no error at all), exactly one atomic `os.rename()`,
      and post-move verification before ever recording `SUCCESS` — a
      `rename()` that raised no error but left something unverifiable
      is `UNKNOWN`, never a guessed `SUCCESS`. Failure policy exactly
      as specified: first failure stops the run, prior successes stay
      recorded, everything after is explicit `NOT_ATTEMPTED`, and a
      last-resort exception handler around each action ensures the
      execution always reaches a terminal state rather than getting
      stuck `RUNNING` from something unanticipated. Post-move
      verification checks each condition independently and by name —
      source gone, destination is a genuine regular file, destination
      is explicitly *not* a symlink, hash/size match — rather than one
      opaque combined boolean, so a failure names exactly which check
      broke; "no unexpected second filesystem operation" holds by
      construction (exactly one `os.rename()` call site in the whole
      class). 30 new tests (all real-database + real-synthetic-
      filesystem, `aibrain_test` + `tmp_path`) covering every scenario
      asked for: successful quarantine with byte-for-byte content
      verification, hash/size mismatch, missing source, symlinked
      source, multi-hard-linked source, source outside the allowed
      root, authorization revoked mid-run, a live
      `Document.source_type` change distinct from a raw file change,
      destination collision, a real permission failure, stop-on-first-
      failure with the middle of three actions corrupted, an all-fail
      case, a mocked post-move hash/size mismatch and a separately
      mocked destination-is-a-symlink mismatch (each its own `UNKNOWN`
      test), a mocked unexpected exception, repeated execution
      attempts (both after completion and against partial audits), the
      full constructor fail-closed suite, and — matching the explicit
      test-isolation requirement for this milestone — a dedicated test
      that scans the test file's own source and asserts it contains no
      reference to the real corpus, the user's home directory, or any
      dynamic host-environment root discovery, proven the same way the
      production code's own isolation is proven. Full suite: 517
      tests, three consecutive runs, zero flakiness. One unrelated
      finding surfaced while verifying zero leftover rows: two stray
      authorization/review/document rows in `aibrain_test`, left
      behind by an *earlier* milestone's now-fixed test cleanup bug
      (never touched by any later successful run) — found and removed;
      `aibrain_test`'s dedup tables confirmed empty. **No test in this
      suite references, reads, or could reach the real personal
      corpus, the user's home directory, or any other existing
      personal-data directory** — every mutation happened inside a
      disposable `tmp_path` pair, and none of this milestone's tests
      discover a filesystem root dynamically from the host environment.
      Deliberately not built: any API/router wiring, any default or
      configured production root, permanent deletion, resumption of a
      partially-run `execute()` call, `DedupExecutionActionReconciliation`
      (still deferred), and any frontend.
- [x] Hostile security review of the executor (no code changes) —
      adversarial, line-by-line audit of `executor.py` and its tests
      covering mutation surface, path security, symlink/hard-link
      safety, TOCTOU, same-filesystem guarantees, authorization
      revalidation, concurrency, audit correctness, crash windows,
      post-move verification, failure semantics, directory/special-file
      safety, quarantine destination, real-corpus safety, DB/filesystem
      atomicity, and test coverage. Delivered as a report only — zero
      lines of `executor.py` or its tests changed by the review itself.
- [x] Executor hardening: concurrency, freshness, symlink roots,
      destination-device check — closes the review's findings one at a
      time, no unrelated behavior changed. **Concurrency**: new
      `DedupExecution.claimed_at` column (migration `02467162321c`) set
      once under `SELECT ... FOR UPDATE` on the execution row via a new
      `_claim_execution` method — Postgres blocks a second concurrent
      `FOR UPDATE` on the same row until the first transaction commits
      or rolls back, so two callers for one `execution_id` can never
      both enter the action loop; the second caller gets a clean,
      deliberate `ValueError`, never a raw `IntegrityError` or an
      uncaught exception from `complete_execution`. Proven under real
      contention, not just reasoned about:
      `test_concurrent_execute_calls_only_one_claims_and_mutates` drives
      two real threads against two separate database sessions on real
      Postgres via a `threading.Barrier`, run 5× in isolation plus in
      every full-suite run with zero flakiness. **Authorization
      freshness**: `_attempt_action` now opens with an explicit
      `self.db.expire_all()` rather than silently depending on
      SQLAlchemy's `expire_on_commit=True` default; proven independent
      of that default by a regression test that deliberately opens the
      executor's session with `expire_on_commit=False` and confirms an
      authorization revoked from another session mid-run is still
      observed. **Symlinked roots**: `allowed_root`/`quarantine_root`
      are now rejected outright if either is itself a symlink (checked
      before `resolve()`, which would otherwise follow it silently) —
      a documented fail-closed decision, not a silent behavior change,
      since a symlinked root has nothing above it to contain it against
      the way an intermediate path component inside a plan action does.
      **Destination-parent device check**: a new explicit check before
      `os.rename()` verifies the destination's parent directory is
      genuinely on the quarantine device, previously only assumed.
      **Confirmed, not changed**: directory/FIFO/socket sources are
      excluded two layers above the executor (at plan generation, via
      the shared `_observe_file`/`is_file()` primitive) — proven by
      tracing the real code path, with a separate mocked test proving
      the executor's own redundant `is_file()` check is independently
      correct rather than merely agreeing by construction; `'..'`
      traversal and a symlinked intermediate source directory were
      already defeated by the existing resolve-then-contain check; a
      real `os.rename()` `EXDEV` falls into the existing generic
      same-device-failure bucket, no copy-fallback added. **TOCTOU**:
      explicitly *not* eliminated this milestone — the exact
      check-to-rename race window, the one residual case post-move
      verification cannot catch (a same-hash/same-size substitution),
      and the decision not to implement fd-pinning or advisory locking
      here are all documented directly in the executor's class
      docstring, with fd-pinning/locking stated as a **required gate
      before any real-corpus use**, not an optional future nicety.
      12 new tests (30 → 42 in the executor's own file; 517 → 529 full
      suite), full suite run 3× consecutively with zero flakiness.
      `aibrain_test`'s dedup tables confirmed empty after the final run
      (a batch of stray rows from this milestone's own earlier, since-
      fixed test-writing iterations was found and purged — never
      produced by a passing test, only by draft attempts along the
      way). **No API/UI exposure, no automatic execution, no default
      root discovery, no real-corpus execution, and no permanent
      deletion were added** — the real corpus was not read, mutated, or
      referenced by any code or test in this milestone.
- [x] Executor Reconciliation & TOCTOU Strategy — design only, zero
      code changed, settling every question the hardening milestone
      left deferred. **TOCTOU**: current containment (re-observation +
      post-move verification) judged sufficient for continued
      synthetic-only development but explicitly *not* sufficient to
      authorize real-corpus use — the residual same-hash/same-size
      substitution gap is real, if narrow, given AI_Brain's
      single-process, no-API threat model. Closing primitive chosen:
      inode-identity pinning via file descriptor (open the source,
      `fstat` for `(st_dev, st_ino)`, hash through the fd, then after
      `os.rename()` compare the destination's `stat()` identity against
      the pinned one) — proves the object actually moved is the exact
      inode last observed, independent of content, with no advisory
      locking needed since the actors capable of racing this window
      (the user, an editor, a backup job) have no reason to cooperate
      with a lock this system invents. **Refined before implementation**:
      the invariant is that the object the eventual `os.rename()`
      operates on is *proven* identical to what was pinned and hashed —
      a successful earlier `open()`/`fstat()` proves nothing by itself
      until the destination's post-rename identity is compared against
      it; if that identity cannot be established at all, or is
      established but does not match, the result is always `UNKNOWN`,
      reported as its own independently-named condition alongside the
      existing hash/size/symlink/existence checks, never folded
      silently into one of those or guessed as `SUCCESS`.
      **Reconciliation**: finalizes
      the `DedupExecutionActionReconciliation` shape first proposed in
      the Executor Safety & Recovery Design milestone (audit-scoped,
      unique per audit, `verified_result` restricted to SUCCESS/FAILED,
      purely additive — never edits the original audit row or
      retroactively changes `DedupExecution.status`). New design
      decision: reconciliation must corroborate the human's claim
      against the system's own fresh, independent re-observation of the
      source path before recording it (`SUCCESS` requires the file to
      currently not exist; `FAILED` requires it to still match its
      pre-mutation expected hash/size) — a claim inconsistent with what
      the system can itself observe is refused, not trusted blindly.
      **Confirmed, not changed**: re-planning's existing live-file-
      existence exclusion already makes reconciliation's effect on
      future plans exactly zero, by design — the live filesystem check
      is the sole authority regardless of whether a historical `UNKNOWN`
      was ever reconciled. **Confirmed**: reconciliation can never
      auto-authorize a new execution — a new attempt always requires
      the full explicit plan → authorization → execution chain,
      unchanged. **One genuine gap surfaced**: `recover_stale_execution`
      does not itself take a `SELECT ... FOR UPDATE` claim before
      writing, unlike the executor's own `_claim_execution` — flagged as
      a required fix for the next implementation milestone, not fixed
      here. Deliberately not built this pass: the reconciliation
      model/migration/service/API/tests themselves, the fd-pinning
      implementation, the `recover_stale_execution` locking fix, any
      API/UI exposure, any real-corpus execution.
- [x] Reconciliation & TOCTOU implementation — the six-point scope of
      the design pass above, implemented exactly, zero unrelated
      change. `DedupExecutionActionReconciliation` (migration
      `ef65da409302`) reuses the existing `dedup_execution_action_
      result` enum for `verified_result` rather than inventing a
      duplicate type, restricted to SUCCESS/FAILED at the service layer
      (not a DB CHECK constraint, matching this codebase's existing
      convention); `verified_by` is required, deliberately unlike the
      nullable `authorized_by` it otherwise resembles, since
      accountability is the whole point of the record.
      `DedupReconciliationService.reconcile_action` corroborates a
      human's claim against a fresh `_observe_file` re-observation
      before recording it — SUCCESS requires the source to currently
      not exist, FAILED requires it to still match its original
      pre-mutation hash/size — refusing (ValueError) any claim
      inconsistent with what the system itself can see; not guarded by
      FOR UPDATE (a documented, accepted residual — reconciliation is a
      rare single-human action, not a systemic concurrency surface).
      **TOCTOU closed, not merely minimized**: a new `_pin_and_hash`
      opens the source once, captures `(device, inode)` via `fstat` on
      that descriptor, and hashes through the SAME descriptor — this
      identity is compared against the destination's own post-rename
      `os.stat(..., follow_symlinks=False)` identity, added as its own
      independently-named post-move check. Proven for real, not just
      reasoned about: a new adversarial test performs a genuine
      `os.replace()` substitution with byte-for-byte IDENTICAL content
      inside the actual race window and confirms the executor still
      reports UNKNOWN — the exact residual case the pre-pinning design
      explicitly accepted as unclosable. `recover_stale_execution` now
      has FOR-UPDATE claim parity with the executor's own
      `_claim_execution`, via a new `recovery_claimed_at` column and
      `_claim_for_recovery` helper — proven under genuine two-thread,
      two-session contention exactly like the executor's own
      concurrency test. 18 new tests (529 → 547): 4 direct
      `_pin_and_hash` unit tests, 3 full-pipeline adversarial tests
      (the substitution attack; destination identity becoming entirely
      unstat-able post-rename, still correctly UNKNOWN even though
      pathlib's own is_symlink()/is_file() share the same underlying
      syscall so every destination check fails together — a realistic
      scenario, not an artificially isolated one; source vanishing
      immediately before pinning, correctly PRECONDITION_FAILED), 10
      reconciliation integration tests (both happy paths, structural
      preconditions, and — the corroboration requirement's actual
      teeth — both contradictory-claim directions refused), and 1
      concurrent-recovery test mirroring the executor's own. Full
      suite run 3× consecutively with zero flakiness; the new
      concurrent-recovery and identity-substitution tests additionally
      run 5× each in isolation with zero flakiness. `aibrain_test`
      confirmed empty after the final run, `dedup_execution_action_
      reconciliations` included. **No API/UI exposure, no automatic
      authorization, no permanent deletion — the real corpus was not
      read, mutated, or referenced by any code or test in this
      milestone.**
- [x] execute() vs recover_stale_execution() race — safe convergence,
      not mutual exclusion. **Invariant, stated precisely**: at most one
      action-result audit is ever persisted per plan action, and any
      concurrent loser converges to the existing terminal state without
      leaking a raw database exception — not "execution and recovery
      can never overlap" (they still can; only the consequence changed).
      Follow-up review of the milestone above's own
      documented residual asked the sharper question directly:
      `_claim_execution` and `_claim_for_recovery` check/set
      independent columns (`claimed_at` vs `recovery_claimed_at`), so
      nothing prevents BOTH from being granted for the same
      still-RUNNING execution — a genuinely still-running `execute()`
      and a `recover_stale_execution()` call can both reach
      `record_action_result` for the same unresolved action. **Proven
      real, not theoretical**: a synchronized two-thread probe (a
      shared `threading.Barrier` gating both callers' `record_action_
      result` INSERTs so they genuinely race at the database level)
      reproduced a raw `psycopg2.errors.UniqueViolation` propagating
      uncaught out of `recover_stale_execution` in ~2 of 5 runs before
      the fix. **Why a stronger claim predicate can't close this**:
      requiring `claimed_at IS NOT NULL` for recovery would block the
      normal, intended use of recovery (recovering an execution that
      already started), not the dangerous case; truly preventing the
      overlap needs process supervision or a lease/heartbeat this
      codebase deliberately doesn't have — out of scope as a "smallest
      correction." **Fix actually applied**: make the consequence
      always safe instead. Two new `ValueError` subclasses (backward
      compatible with every existing `except ValueError` call site) —
      `DuplicateActionResultError` (record_action_result now catches
      the database's own `IntegrityError` and converts it to this,
      matching what its pre-insert check already raises) and
      `AlreadyFinalizedError` (complete_execution's existing "not
      RUNNING" case). `recover_stale_execution`'s per-action loop now
      tolerates a duplicate on any one action and moves to the next
      rather than aborting the whole attempt; its final
      `complete_execution` call tolerates losing the finalize race and
      returns the already-finalized execution instead of raising.
      `DedupFilesystemExecutor.execute`'s own final call made
      symmetric. **Verified**: the same probe that reproduced the raw
      IntegrityError now completes cleanly every time — zero
      exceptions from either caller, exactly one audit row, both
      callers agreeing on the final status — across 10 isolated runs
      plus every full-suite run. Permanent regression test:
      `test_execute_vs_recover_stale_execution_race_never_corrupts_
      state`. **Also added**: the corpus hygiene guard now checks for
      both real, confirmed T7 paths (the canonical protected path and
      the drive actually mounted with real data) alongside the earlier
      reference this project's memory once incorrectly protected —
      neither path was scanned or accessed to make this change, it's
      pure string containment against this project's own source.
      Deliberately not built: any process-supervision/lease mechanism
      (the only way to prevent the claim overlap itself, not just its
      consequences); any API/UI exposure; any real-corpus execution.

- [x] T7 Corpus Discovery / Inventory — a deliberately separate,
      newly-authorized gate (not a continuation of the dedup execution/
      recovery chain, which was explicitly closed at `0ef1d92` first).
      Read-only understanding of the user's real T7 backup drive,
      gated independently of any mutation-capable work. New module
      `app/discovery/corpus_inventory.py`: `scan_corpus(root, *,
      top_n=20)` walks the tree via `os.walk(topdown=False,
      followlinks=False)`, reading only `os.lstat` metadata per entry —
      no file is ever opened, read, hashed, or extracted. `lstat` (not
      `stat`) means a symlink is measured by its own size, never its
      target's; `followlinks=False` means a symlinked directory is
      never descended into. Not wired into `SourceScanner`/
      `DocumentIngestor`/the database — a fully standalone module.
      Resilient to a foreign, possibly-flaky external drive: an
      unlistable directory or an unreadable file is recorded in the
      inventory's own `errors` list and skipped, never raised — a
      single bad entry can't abort a multi-hundred-gigabyte scan.
      Memory-bounded: aggregate counts are plain running totals; the
      largest-files/largest-directories rankings are each a bounded
      min-heap holding only the top N seen so far, and per-directory
      totals are computed bottom-up in the SAME single pass (`os.walk`
      yields each directory only after its subdirectories, so a
      directory's total is just its own files plus its already-known
      children's totals — no second pass needed). Archive
      classification is extension-based only for this first pass,
      deliberately not signature-verified (that would require opening
      every candidate file — more than a "cheap first inventory" calls
      for) — reports both `KNOWN_ARCHIVE_SUFFIXES` (`.zip`/`.7z`,
      matching what `DocumentIngestor` can already extract) and
      `OTHER_ARCHIVE_SUFFIXES` (everything else this backup corpus
      might contain) so a human can see the gap. Reports are plain
      JSON, written to `knowledge/t7_discovery/` — gitignored, since a
      real inventory's rankings necessarily contain real personal file
      and directory names. Runner: `scripts/t7_discovery.py <root>
      <output.json> [--top-n N]`. 16 new tests, entirely
      synthetic-`tmp_path` — counting, extension/archive
      classification (including two-part `.tar.gz`-style extensions),
      bottom-up directory totals, `top_n` bounding, a mocked `lstat`
      failure and a real `chmod 000` directory both proving the scan
      doesn't abort over an unreadable entry, a real symlink proving
      `lstat`-not-`stat` semantics, a real symlinked directory proving
      `followlinks=False`, and JSON report round-tripping. Full suite:
      565 tests. **The T7 was scanned read-only, exactly as
      authorized** — no file on it was opened, moved, renamed, deleted,
      or otherwise modified. Deliberately not built: content hashing,
      archive-signature verification, archive extraction, any wiring
      into ingestion/the database, any deduplication analysis, any
      mutation of any kind.
- [x] T7 Deduplication Analysis (Phase D1) — read-only, a separate
      explicitly-authorized gate opened only after Discovery was
      independently, critically re-verified and committed (`ee043c6`).
      Authorization scope: reading metadata AND content for hashing,
      reading directory structure, comparing files/directories,
      producing reports — explicitly NOT delete/move/rename/extract/
      quarantine/execution of any kind. New module
      `app/discovery/duplicate_analysis.py`: `analyze_duplicates()`
      runs a metadata-only pass (full size index + bottom-up
      per-directory structural signatures, in one `os.walk` traversal —
      `inventory.json`'s own report doesn't retain a full per-file
      listing, only a bounded top-N, so this couldn't literally reuse
      it as an index the way first assumed; noted rather than silently
      deviating from the stated plan) followed by a content-reading
      pass that opens and SHA-256-hashes ONLY files sharing a size with
      at least one other file — a uniquely-sized file can never be an
      exact duplicate and is never opened. Three kept-separate finding
      types: `exact_duplicate_groups` (hash-confirmed, zero-byte files
      excluded), `cryptomator_chunk_duplicate_groups` (the same
      hash-match logic, but `.c9r` groups reported separately since a
      same-hash Cryptomator chunk pair is ciphertext-identical, not
      necessarily "the same document twice" the way a repeated `.jpg`
      is — a mixed group with even one non-`.c9r` member stays
      ordinary), and `directory_duplicate_groups` (whole-subtree
      structural matches by name+size, empty dirs excluded, only the
      TOPMOST match reported when an entire tree is nested-duplicated,
      to avoid redundant sub-match noise). Archives treated as plain
      files for this pass — compared by size→hash like anything else,
      never opened internally. `write_duplicate_report` reuses a new
      shared `app/discovery/safety.py` (`reject_destination_inside_
      root`) rather than re-implementing that safety-critical check a
      second time — `corpus_inventory.write_inventory_report` was
      refactored onto the same shared helper, its existing test suite
      re-run and passed unchanged, confirming no regression to
      already-committed, already-hardened behavior. Runner:
      `scripts/t7_duplicate_analysis.py <root> <output.json>`. 22 new
      tests (17 core + 5 for the extracted safety helper, including a
      sibling-directory-with-shared-name-prefix case proving the check
      is genuinely resolution-based, not a naive string comparison),
      entirely synthetic `tmp_path`. Full suite: 594 tests. **The T7
      was only ever opened read-only for hashing** — no file on it was
      written to, truncated, moved, renamed, deleted, or otherwise
      modified. Deliberately not built: any deletion/move/rename/
      extraction/quarantine/execution; any archive-internal inspection;
      any database/executor wiring; any automatic decision-making from
      these findings.
      **Reviewed before commit, per explicit request** — see "T7
      Deduplication Analysis (Phase D1)" in AI_Brain_Architecture.md
      for the full seven-point "read this before acting on any D1
      finding" summary (live-corpus caveat, forensic-not-deletion
      framing, the reclaimable-bytes formula, the never-sum-the-two-
      totals rule, `.c9r` separation, the exact zero-byte explanation
      for the hashing-count gap, and the accepted linear-memory
      tradeoff). One real bug was found and fixed during this review: a
      symlink's tiny `lstat` size could have collided with an unrelated
      file's size, causing `_hash_file`'s `open(path, "rb")` to
      silently follow the symlink and hash its TARGET's content while
      reporting the symlink's own size — fixed by excluding symlinks
      from hash-based duplicate detection entirely (still counted in
      aggregates, still part of directory structural signatures).
      **Confirmed to have had zero effect on the completed real-corpus
      report**: exFAT cannot represent symlinks, so the triggering
      condition never existed on the actual T7 data — no re-scan was
      needed or performed. 3 new tests from this review pass; full
      suite 597 tests.
- [x] T7 Provenance-Aware Duplicate Analysis (Phase D2) — read-only, a
      third separate gate opened only after D1 (`64887ba`) was
      committed. Purpose: understand WHY D1's duplicates exist — backup
      generations, current-plus-historical pairs, intentionally
      independent copies, or genuinely ambiguous cases — never to
      decide what should be deleted. New module `app/discovery/
      provenance_analysis.py`: `analyze_provenance()` does not re-walk
      or re-hash the corpus — it reads D1's `duplicate_analysis.json`
      as established fact, then performs a targeted, read-only
      `os.lstat` on exactly the paths D1 already named. Every
      conclusion is labeled across three explicit tiers: OBSERVED FACT
      (path text, directory structure, a fresh `lstat`), INFERENCE
      (what that might mean — always "this suggests," never settled
      fact), and CONFIDENCE (high/medium/low). Classification rules in
      priority order: differing date-like tokens across copies →
      cross_generation_backup_copies (medium); one dated + one undated
      copy, or a keyword on a deeper path with none on a shallower
      sibling → current_plus_historical (medium); a shared keyword with
      nothing further distinguishing the copies → same_backup_
      generation (low, flagged for review); no signal at all →
      possibly_intentionally_independent (low, flagged — absence of a
      signal is weak evidence, not a safety conclusion); fewer than two
      of a group's D1-named paths still existing → insufficient_
      current_evidence (low, flagged — D1's finding can't be
      corroborated against the corpus's current state). Question 8
      (overlap) answered directly: each file-level group is checked
      against D1's own directory-duplicate groups to see whether it
      sits entirely inside an already-reported structural match.
      Live-corpus divergence handled explicitly (D1 ran while Syncthing
      was active; D2's own reads happen later) — every vanished/moved
      path is its own recorded fact, never silently reasoned from a
      known-stale snapshot. The report's own JSON carries a
      `_read_this_first` disclaimer stating D2 is forensic
      interpretation, not a deletion recommendation. Runner:
      `scripts/t7_provenance_analysis.py <d1.json> <output.json>
      <summary.txt>` — writes both machine-readable JSON and a
      human-readable top-10-by-reclaimable-bytes summary per category.
      24 new tests, entirely synthetic. Full suite: 621 tests. **The T7
      was only ever read via `os.lstat`** — no file opened, written to,
      moved, renamed, deleted, or otherwise modified. Deliberately not
      built: any deletion/move/rename/quarantine/extraction; any
      `DedupFilesystemExecutor` invocation; any authorization creation;
      any automatic canonical-file choice or deletion marking.
      **Reviewed before commit, per explicit request** — found a real
      naming/epistemics defect, not a style nit: the original
      classification codes doubled as both "which evidence pattern
      fired" and "what that means," so `current_plus_historical`
      (one dated/keyword-marked copy + one undated one) asserted a
      temporal claim the evidence doesn't support — a path lacking a
      dating convention is not proof a file is in active use. Fixed by
      splitting every classification into a neutral `inference_code`
      (naming only the evidence pattern — `DIVERGENT_DATE_SIGNAL`,
      `PARTIAL_DATE_OR_KEYWORD_SIGNAL`, `UNIFORM_KEYWORD_NO_FURTHER_
      SIGNAL`, `NO_PROVENANCE_SIGNAL`, `STALE_REFERENCE_UNVERIFIABLE`)
      and separately-rendered hedged prose (`INFERENCE_PROSE`, never
      stored per group). The exact pattern under review
      (`PARTIAL_DATE_OR_KEYWORD_SIGNAL`) was downgraded from medium to
      low confidence with human review now required — on the real
      corpus this moved 50,970 groups from "medium, unflagged" to "low,
      flagged." Pipeline also split into two independent stages
      (`collect_path_signals` — the only T7-touching step — and a pure
      `classify()`), specifically so this correction could be re-run
      against the real, already-collected corpus data with **zero
      additional T7 access** via a new `load_raw_from_previous_report`
      reconstruction path — confirmed via the real run: 12.9 seconds,
      not a second ~14-minute `os.lstat` pass. Report size cut 62.6%
      (498.4MB → 186.6MB) by dropping unused per-path `mtime`/`ctime`
      and the previously-repeated inference prose. Documentation now
      states precisely (not merely implies): all D1-referenced paths
      being observable during D2 does NOT prove the corpus was
      unchanged between the two runs (D2 ran ~2 hours after D1, with
      Syncthing continuously active throughout both); low-confidence
      findings mean "requires human provenance review," never "likely
      safe to remove" (enforced by a dedicated test asserting no
      "safe to remove"/"safe to delete" language anywhere in the
      rendered prose); `.c9r` provenance stays explicitly scoped to
      ciphertext chunks via a report-level note. 10 new tests from this
      review pass (34 total for this module). Full suite: 631 tests.
      **The real corpus was NOT re-scanned to fix the defect** — the
      correction was re-derived entirely from already-collected data.
- [x] D2 Safety Follow-up — a procedural incident, disclosed precisely,
      not smoothed over. D2's final pre-commit verification included a
      test that turned out to be a REAL write attempt against
      `/media/personal/Seenu_T7SSD` (the canonical, unmounted T7 path) —
      denied by OS permissions before any bytes were written, confirmed
      via `find` that nothing changed, but the verification procedure
      itself violated this project's own rule of never directing a
      mutating operation at a real protected path, even as a test. Two
      separate questions, kept separate rather than collapsed: did D2
      mutate the T7 (no); did the verification procedure obey the
      never-touch-the-real-corpus-for-testing rule (no). The mounted
      real corpus, `/media/personal/Seenu_T7SSD1`, was not targeted by
      this test and was not mutated. Revealed a genuine scope gap:
      `reject_destination_inside_root` protects only the specific root
      it's given, not "other locations the caller separately considers
      sensitive" — not a defect in the helper (its single-root
      guarantee holds exactly as designed), but a real gap in what this
      project's tools protect beyond the one root actually scanned.
      Decision recorded, not built: a protected-root policy belongs in
      a separate, explicit layer (caller/config-supplied list, never
      hardcoded literals in library code) — deferred to its own future
      milestone. 1 new synthetic-only regression test
      (`test_does_not_protect_a_second_root_it_was_never_told_about`)
      reproduces the exact scope gap with two disposable `tmp_path`
      directories, never touching any real path — plus a second test
      demonstrating the correct manual multi-root pattern available
      today. Full suite: 633 tests. **Zero T7 access of any kind during
      this follow-up**, including read-only re-verification (already
      confirmed clean by D2's own checks, not repeated here).
- [x] T7 → AI_Brain Ingestion & Representation Design — design pass
      only, no implementation, no T7 access of any kind, three review
      rounds, APPROVED and frozen. Round 1 answered nine user-posed
      questions with a
      proposed pipeline (`discovery → classification → archive
      handling → document extraction → normalization → provenance →
      chunking → embeddings → AI_Brain knowledge`) mapped onto the
      existing ingestion codebase. Round 2 (this revision) corrected
      several conflations the first draft made, restructured around
      four explicit **identity layers** — physical source occurrence,
      content identity, logical document identity, document/version —
      of which only the first two are modeled now; the latter two are
      named and deliberately deferred. Key corrections: a D1
      directory-structural match no longer drives any content-identity
      or canonical decision (only a proven hash match does — D1's own
      report already keeps exact vs. directory-structural
      reclaimable-bytes separate for this exact reason); canonical
      status is now an explicit three-state field
      (`UNRESOLVED`/`CANONICAL`/non-canonical) that defaults to
      `UNRESOLVED` and is never inferred from "no open review flag" —
      D2 deliberately never decided physical-copy authority, and
      ingestion (extraction/chunk/embed) is gated by content identity
      alone, not by canonical resolution; `archive_chain` became a
      structural `ProvenanceLink` chain (`t7_file`/`archive_member`
      nodes with a `parent` reference) instead of an opaque path list,
      so archive-ancestry queries are possible later; four distinct
      hash types (source file / archive member / extracted document
      content / normalized-content) are now named explicitly, and
      `Document.content_hash` is documented as usable for a
      pre-extraction dedup check only for loose T7 files — D1 never
      opened archives, so an archive member has no hash until *after*
      it is extracted, meaning a content match found post-extraction
      saves the chunk/embed step, not the extraction itself; ingestion
      gets its own lifecycle sketch (`DISCOVERED → CLASSIFIED →
      EXTRACTING → EXTRACTED → NORMALIZED → CHUNKED → EMBEDDED →
      INGESTED`, plus `QUARANTINED`/`NEEDS_REVIEW`), reusing only Chain
      1's *durability principle* (one durable row per item = resume
      checkpoint) and explicitly not its `PLANNED/AUTHORIZED/EXECUTING/
      .../RECONCILED` state machine, which exists specifically for
      authorized T7 mutation that ingestion never performs. The T7
      remains treated as an instance of the master backup under the
      existing [0002](../decisions/0002-master-backup-is-read-only.md)
      decision. Still explicitly deferred: exact schema/migration for
      `SourceInstance`/`ProvenanceLink`, any layer-3/4 modeling, any
      canonical-copy selection algorithm, working-copy retention
      policy, resource limits/backpressure/scheduling (Track 2), and
      ingestion's retry/partial-failure semantics.

      **Round 3 (final clarifications, design APPROVED and frozen)**:
      `canonical_status` is explicitly a relationship, not a property
      of a file — it lives on the (`SourceInstance`, new first-class
      `ContentIdentityGroup`) pair, documented as "canonical within
      this content-identity group," never "canonical" unqualified.
      `NON_CANONICAL` was corrected to require its own explicit
      evidence/decision, exactly like `CANONICAL` does — it no longer
      follows automatically from a sibling instance being marked
      `CANONICAL`; a group can sit at "one `CANONICAL`, N
      `UNRESOLVED`, zero `NON_CANONICAL`" indefinitely as a normal
      state. An explicit **ingestion idempotency key** was defined:
      `SourceInstance → ContentIdentityGroup → one extraction/
      normalization/chunking pipeline run → multiple provenance
      references`, restating the archive caveat as its own chain
      (`T7 archive → archive member → member hash known only after
      reading/extracting it`) — archive-aware dedup can only save
      downstream chunk/embed work, never the initial read of an
      unidentified member. `QUARANTINED` was defined precisely as an
      ingestion-workspace concept (an artifact rejected/isolated under
      `documents/imports/<job_id>/`) and explicitly distinguished from
      Chain 1's dedup-executor quarantine (a reversible T7-adjacent
      filesystem relocation) — the two must never be conflated in
      code, logs, or docs. See `AI_Brain_Architecture.md`'s "T7 →
      AI_Brain Ingestion & Representation Design" section for the
      full, frozen proposal. **Next gate, not yet authorized**: a
      schema/model design milestone settling the exact representation
      of `SourceInstance → ProvenanceLink → ContentIdentityGroup →
      Document → DocumentChunk`, still leaving logical-document/version
      semantics (identity layers 3/4) deferred — not immediate
      extraction.
- [x] Schema/Model Design for `SourceInstance → ProvenanceLink →
      ContentIdentityGroup → Document → DocumentChunk` — design pass
      only, no T7 access, no migrations, no model/service code written.
      **APPROVED and frozen after five review rounds.** Follows
      two existing house patterns rather than inventing new ones:
      `DuplicateReview`/`DuplicateReviewMember` (parent finding + N-way
      child table) and `DedupExecutionPlan`/`DedupExecutionPlanAction`
      (immutable, snapshotted audit rows with evidence duplicated onto
      child rows). Five new tables sketched: `DiscoveryRun` (one row
      per already-completed D0/D1/D2 run, deliberately **not** the
      heavier atomic `CorpusSnapshot` concept — D0/D1/D2 are honestly
      non-atomic, sequential runs against a live corpus; round 4 froze
      the exact invariant: `report_sha256` proves which report artifact
      was consumed, never that the T7 was quiescent while producing
      it); `ClassificationRun` (one row per classification pass,
      referencing up to one D0/D1/D2 `DiscoveryRun` each, plus — added
      in round 4 — an immutable `classifier_version` column so two runs
      over the identical D2 report that classify differently have a
      durable explanation); `SourceInstance` (physical occurrence, with
      an immutable JSONB `evidence_snapshot` for filesystem facts +
      D1/D2 machine inference — round 4 froze this as historical
      evidence at classification time, never a live filesystem view —
      structurally separate from mutable `canonical_status`/
      `canonical_status_reason`/`canonical_status_decided_by`/
      `canonical_status_decided_at` columns for human decisions only,
      with round 4 stating explicitly that `canonical_status_reason` is
      a decision annotation, never source evidence; a `CHECK` constraint
      sketch enforces that `NON_CANONICAL` requires the same evidentiary
      bar as `CANONICAL`); `ProvenanceLink` (self-referential
      `parent_link_id` + `sequence_index`, replacing the earlier opaque
      `archive_chain` with something a plain indexed query can walk);
      `ContentIdentityGroup` (round 4 added an explicit `identity_kind`
      + `identity_algorithm` domain alongside `identity_hash`, so the
      four hash layers from `29d6864` can never be silently compared
      across layers — the `UNIQUE` constraint now covers all three
      columns together; `pipeline_state` carries a round-4-refined
      lifecycle: the earlier catch-all `QUARANTINED` is retired in
      favor of four distinct terminal/review states —
      `NEEDS_REVIEW`/`UNSUPPORTED`/`EXCLUDED`/`FAILED` — so a
      non-processing outcome, like `.c9r` ciphertext being explicitly
      out of scope, is never forced into language implying a failure it
      isn't; this remains the single per-group idempotency boundary,
      never duplicated onto `SourceInstance`). `Document` gains exactly
      one new nullable, UNIQUE `content_identity_group_id` column, with
      round 4 stating the invariant explicitly: one `ContentIdentityGroup`
      represents one ingestible content identity, and at most one
      `Document` currently represents it today — not a claim that two
      semantically different representations can never relate;
      `DocumentChunk` is unchanged (provenance reaches it transitively
      through the existing `document_id` FK). Migration is purely
      additive — zero columns dropped, existing `Document` rows start
      with the new column NULL, backfill logic explicitly deferred.

      **Round 5 (pre-freeze cross-table invariant check, APPROVED)**:
      before freezing, every edge was verified as a single table
      (cardinality, optional/required, immutability, unique
      constraints, creating event, allowed-to-change event) rather than
      only as prose, specifically checking for contradictions between
      immutable `evidence_snapshot`, mutable `canonical_status`, the
      `ContentIdentityGroup` lifecycle, nullable identity pre-hash, and
      nullable `Document` pre-processing. Found and fixed one real
      classification error: `SourceInstance.content_identity_group_id`
      is **write-once** (starts `NULL`, set exactly once, then fixed),
      not "fully immutable" as round 4 implied — and the
      deferred-identity case is **not archive-member-exclusive**: D1's
      size-collision filter also skipped uniquely-sized loose files, so
      those start `NULL` too. Added two missing `CHECK` constraints:
      `ClassificationRun` must reference at least one `DiscoveryRun`;
      `ProvenanceLink`'s `sequence_index = 0` position must be exactly
      the `T7_FILE` root (previously only prevented by convention, not
      the schema). Pinned down a rule round 4 left implicit: `Document`
      existence is not synonymous with `pipeline_state = INGESTED` — a
      `Document` must exist by `CHUNKED` at the latest, since
      `DocumentChunk.document_id` requires a parent; `INGESTED` marks
      the whole pipeline (through `EMBEDDED`) finishing, a related but
      different fact. Walked the specific scenario named in the
      pre-freeze request (a second, later `SourceInstance` resolving to
      an already-`INGESTED` group's `identity_hash`) and confirmed it
      is the idempotency key working as designed, with no contradiction
      among the five flagged concepts; named one genuine open question
      as explicitly deferred to implementation (concurrent-discovery
      race serialization over the same `identity_hash`), not a schema
      gap. See `AI_Brain_Architecture.md`'s "Schema/Model Design"
      section for the full, frozen proposal, including the round-5
      invariant table and cardinality/checklist walkthroughs.
- [x] Schema/Model Implementation for `SourceInstance → ProvenanceLink
      → ContentIdentityGroup → Document → DocumentChunk` — implements
      the `14b8063` design exactly, no T7 access, no real-corpus
      ingestion, no embeddings. **Committed at `1253a2e`.** Five new
      models (`DiscoveryRun`,
      `ClassificationRun`, `ContentIdentityGroup`, `SourceInstance`,
      `ProvenanceLink`) plus one new nullable, UNIQUE
      `Document.content_identity_group_id` column; five new services
      under `app/classification/`. Migration `b9a82d073399`
      (`ef65da409302` → `b9a82d073399`) applied and verified against
      `aibrain_test` only — upgrade/downgrade/upgrade all clean, every
      constraint from the frozen design present (`uq_content_identity_
      groups_identity`, `uq_provenance_links_source_instance_id_
      sequence_index`, `uq_documents_content_identity_group_id`,
      `ck_classification_runs_at_least_one_discovery_run`,
      `ck_provenance_links_root_shape`, `ck_source_instances_canonical_
      status_requires_evidence`). Write-once `content_identity_group_id`
      enforced via an `UPDATE ... WHERE content_identity_group_id IS
      NULL` + rowcount check, proven by test. **The user's explicit,
      non-negotiable requirement for this gate** — same-`identity_hash`
      concurrent claims proven by a real database concurrency test, not
      merely assumed from the UNIQUE constraint — satisfied by
      `test_content_identity_concurrency_execution.py`: two, then ten,
      genuinely separate sessions/threads (same `threading.Barrier`
      pattern as Chain 1's `execute()` race test) all converge on one
      `ContentIdentityGroup` row with zero errors; a separate, ad-hoc,
      forced-contention script confirmed the `IntegrityError`-then-
      refetch path is actually exercised, not untested good luck; 15/15
      consecutive runs with no flakiness. Test isolation via SQLAlchemy
      2.0's `join_transaction_mode="create_savepoint"` — confirmed zero
      leftover rows in `aibrain_test` after every run.

      **Review round 6 (pre-commit audit, two real bugs found and
      fixed)**: (1) `get_or_create_group`'s `except IntegrityError` was
      catching ANY integrity failure, not specifically the identity
      UNIQUE violation — fixed to inspect `exc.orig.diag.constraint_
      name` and re-raise anything else, proven by 4 new mocked unit
      tests in `tests/classification/`. (2) `ProvenanceLink`'s CHECK
      constraint forbade a non-root row from looking like a root but
      did NOT require it to have a parent — an orphan `ARCHIVE_MEMBER`
      at `sequence_index > 0` with `parent_link_id = NULL` would have
      been silently accepted; tightened to `(seq=0 AND T7_FILE AND
      parent IS NULL) OR (seq>0 AND ARCHIVE_MEMBER AND parent IS NOT
      NULL)`, verified against real Postgres. Also added the
      explicitly-required real concurrency test for write-once
      `SourceInstance.content_identity_group_id` under genuine
      contention (two threads assigning DIFFERENT groups to the SAME
      instance — exactly one wins, one cleanly refused, no silent
      overwrite; 15/15 runs, no flakiness), and stated `evidence_
      snapshot`'s immutability enforcement precisely (documented
      contract + structural absence of any mutator — verified via
      `grep` — NOT a DB constraint or trigger, matching this design's
      own accepted precedent for `Document.content_hash`). Transaction
      scope was verified and documented (not redesigned, per explicit
      instruction): every classification service commits its own
      transaction, matching house convention throughout this codebase.
      `ClassificationRun`'s minimum-one-`DiscoveryRun` cardinality was
      re-verified directly against the running schema, no defect found.

      Full suite after this round: **659 passed, 1 skipped** (652 +
      7 new), run three times, no flakiness. `aibrain_test` still
      empty of synthetic rows afterward; the main `aibrain` database
      remains untouched (still at `ef65da409302`) — this migration is
      ready to apply there whenever a future gate calls for it. **Not
      yet committed** — holding for review per this project's standing
      discipline. **Next gate, not yet authorized**: real T7
      discovery-report ingestion into this schema (populating real
      `DiscoveryRun`/`ClassificationRun`/`SourceInstance` rows from the
      actual D0/D1/D2 reports), followed eventually by real
      extraction/ingestion — still no T7 access beyond what D0/D1/D2
      already performed, no embeddings, no
      real-corpus mutation, until each is separately opened.
- [x] Controlled T7 → AI_Brain Ingestion Design — design pass only, no
      T7 access, no code/schema/migration changes. **APPROVED and
      frozen after two review rounds.** Answers 18 questions
      across eligibility, the ingestion unit, `SourceInstance` creation
      timing, content-identity timing, archive processing, idempotency,
      the state machine, workspace design, snapshot semantics,
      provenance queryability, failure/retry handling, partial-corpus
      state, T7 unavailability, path-vs-identity, rebuildability,
      privacy boundaries, and human review. Key findings: (1) `14b8063`
      implicitly assumed classification creates every `SourceInstance`
      up front, including archive members — corrected: D1/D2 never
      opened archives, so archive-member `SourceInstance`s are only
      ever created live, during extraction, referencing their parent
      archive's `classification_run_id`. (2) The ingestion unit of work
      is `ContentIdentityGroup` — no new "work item" table needed,
      `pipeline_state` already is the queue state, decomposed into two
      queues expressible in the existing schema (`SourceInstance`s
      needing identity resolution vs. `ContentIdentityGroup`s needing a
      pipeline run). (3) A real inconsistency between `29d6864` and
      `14b8063` was found and resolved: `ContentPipelineState.
      NEEDS_REVIEW` cannot mean "D2 flagged this" (ingestion must never
      wait on canonical resolution, per `29d6864`) — narrowed to mean
      eligibility ambiguity at classification time only, entirely
      separate from D2's per-instance review flag, which drives
      `canonical_status` via the already-built `CanonicalDecisionService`
      and nothing else. (4) A concrete security risk was identified and
      mitigated at the design level: `FileAccessService._allowed_roots()`
      trusts any `COMPLETED` `ImportJob.source_path` as a live,
      chat-readable root — a T7-sourced `ImportJob` must use a
      non-path reference string (e.g. `"classification_run:<id>"`),
      verified to structurally fail `FileAccessService`'s containment
      check rather than accidentally exposing the whole T7. (5) Reused,
      not reinvented: `ArchiveExtractor`'s existing safety guards
      (path-traversal, expansion-ratio, disk-space, symlink rejection)
      apply recursively at every nesting level plus a new depth limit;
      `EmbeddingService.embed_document`'s existing delete-then-recreate
      pattern is the model for idempotent normalization/chunking;
      `get_or_create_group`'s already-proven concurrency safety (from
      `1253a2e`) is called, not redesigned. Named, not fixed (schema
      changes are out of scope for design): `ContentIdentityGroup` has
      no `failure_reason` column today; a durable off-machine backup
      for `knowledge/t7_discovery/*.json` is a real rebuildability gap;
      a `superseded_by_classification_run_id`-style field for
      `UNSUPPORTED`/`EXCLUDED`/`NEEDS_REVIEW` reclassification is
      recommended for a future schema-extension gate. See
      `AI_Brain_Architecture.md`'s "Controlled T7 → AI_Brain Ingestion
      Design" section for the full proposal, including the state
      machine, failure/retry matrix, and provenance query graph.

      **Review round 2 (direction approved, tightened before freezing)**:
      the reviewer's opening instruction was explicit — "do not assume
      the unique identity constraint solves pipeline concurrency" — and
      round 1 left exactly that implicit. (1) `ContentIdentityGroup`
      worker/queue semantics now designed precisely: `claimed_by`/
      `claimed_at` columns (recommended, not added) + a
      `SELECT ... FOR UPDATE SKIP LOCKED` claim query, generalizing
      Chain 1's claiming primitive without reusing Chain 1's state
      machine; stale-claim recovery is lease-expiry on the same query,
      no separate recovery service needed; retries are safe because
      each step is idempotent, not because the claim mechanism prevents
      sequential duplication. (2) `SourceInstance` identity-resolution
      queuing: archive members need no per-instance claim field at all
      — the natural claim unit is their parent archive, one worker
      opens it once and creates every member's `SourceInstance` inside
      that single claimed unit of work; uniquely-sized loose files have
      no such coarser unit and genuinely need their own `claimed_by`/
      `claimed_at` fields (recommended, not added). (3)
      `FileAccessService` hardened from a naming convention to a
      structural check: `_allowed_roots()` must filter by an explicit
      `source_type` **allow-list** (verified via `grep` that every real
      caller today already uses `"filesystem"`, so this requires zero
      change to existing behavior) rather than trusting every
      `COMPLETED` `ImportJob` — an allow-list fails closed for any
      future `source_type` nobody thought to block yet, a deny-list
      would not. (4) `EmbeddingService`'s delete-then-recreate
      confirmed, by re-reading the code and its transaction semantics
      (not assumed), to already be atomic — `DELETE` and `INSERT`s
      share one uncommitted transaction, so any crash before the final
      `commit()` leaves old chunks fully intact; the one residual
      ambiguity common to any transactional system (commit sent,
      acknowledgment lost) is resolved by the operation's own
      idempotency, not a false atomicity claim. (5) Failure-detail
      direction decided: a per-attempt audit table (recommended,
      matching this codebase's existing `DedupExecutionActionAudit`/
      `DiscoveryRun` one-row-per-event precedent) over mutable columns
      on `ContentIdentityGroup`, populated only when `pipeline_state =
      FAILED` — never blurring `EXCLUDED`/`UNSUPPORTED`/`NEEDS_REVIEW`,
      each of which already carries its own distinct reasoning
      elsewhere. (6) Discovery-report rebuildability: gitignored
      explicitly redefined as *not* disposable — reports are durable,
      hard-to-reproduce-identically artifacts requiring an operational,
      off-machine backup routine outside AI_Brain's own code; backing
      them up onto the T7 itself or committing them to git are both
      explicitly rejected, for reasons named precisely (violates T7
      immutability; doesn't remove the privacy reason they were
      gitignored). (7) The real/derived source boundary is now an
      explicit allow/forbid list tying points 3 and 7 together: a real
      T7 path existing anywhere in the database never by itself grants
      filesystem access — only `DiscoveryRun`/`SourceInstance`/
      `ProvenanceLink` may hold one (as inert metadata), `ImportJob.
      source_path`/`Document.source` never may, and access is gated
      exclusively through the `source_type` allow-list. **APPROVED and
      frozen** — a resend of the same review was confirmed as such
      (not a new round) and the design was approved for freeze without
      further changes. **Next gate, not yet authorized**:
      implementation of this design — still no T7 access, extraction,
      embeddings, or real-corpus ingestion until that is separately
      opened, and still requiring its own schema-extension gate first
      for the `claimed_by`/`claimed_at`/failure-detail fields this
      design recommends but does not add.
- [x] Ingestion Schema-Extension Implementation — implements exactly
      the two extensions `6491dad` recommended: worker claim/lease
      fields and a durable per-attempt failure record. No T7 access, no
      extraction, no embeddings, no real ingestion jobs, no pipeline
      logic. `ContentIdentityGroup` and `SourceInstance` each gain
      `claimed_by`/`claimed_at` (transient in-flight markers, cleared
      on every attempt's completion); new `IngestionAttempt` table
      (one durable row per attempt, matching this codebase's existing
      `DedupExecutionActionAudit`/`DiscoveryRun` one-row-per-event
      precedent) with `attempt_kind`/`attempted_stage`/`outcome`/
      `failure_code`/`failure_detail`/`retryable`/`worker_id`, and two
      `CHECK` constraints: exactly one of `content_identity_group_id`/
      `source_instance_id` set matching `attempt_kind`
      (`ck_ingestion_attempts_parent_matches_kind`, mirroring
      `DuplicateReview`'s existing "exactly one of two fields depending
      on kind" pattern), and failure fields present iff `outcome =
      FAILED` (`ck_ingestion_attempts_failure_requires_detail`,
      mirroring `SourceInstance.canonical_status`'s own evidence-
      required constraint). New `app/classification/worker_claim_
      service.py` (`SELECT ... FOR UPDATE SKIP LOCKED` + `UPDATE`,
      generalizing Chain 1's claiming primitive without reusing its
      state machine; lease-expiry alone recovers stale claims, no
      separate recovery service) and `ingestion_attempt_service.py`
      (records outcomes a caller determined, validates failure fields
      in Python before the DB CHECK ever has to). Migration
      `fd9f81672e59` (`b9a82d073399` → `fd9f81672e59`), purely
      additive, applied and verified against `aibrain_test` only.
      **Concurrency required and delivered, not assumed**: 7 real
      Postgres, real-thread tests — one-winner claim races (2 and 10
      concurrent workers), stale-claim recovery under contention, a
      mixed fresh/stale/unclaimed pool proving the claim query's
      three-way logic holds under real load, concurrent attempt
      persistence across 8 workers/8 groups, and retry-history
      idempotency (5 concurrent valid retries all persist independently;
      5 concurrent invalid retries all correctly rejected, zero rows
      persisted) — 15/15 consecutive runs, no flakiness. Full suite:
      **687 passed, 1 skipped**, run three times, zero flakiness (659
      pre-existing + 28 new). Main `aibrain` database untouched, still
      at `ef65da409302`. See `AI_Brain_Architecture.md`'s "Ingestion
      Schema-Extension Implementation" section for the full report.
      **Next gate, not yet authorized**: the actual ingestion pipeline
      (extraction/normalization/chunking/embedding) built on these
      primitives — still no T7 access, real ingestion jobs, or
      embeddings until that is separately opened.
- [x] Controlled Ingestion Implementation — the pipeline itself, built
      on `6491dad`'s design and `ce50875`'s claim/attempt primitives,
      entirely against synthetic fixtures. **Zero schema changes** —
      archive-completion tracking reuses the existing `IngestionAttempt`
      table (a prior `SUCCEEDED` attempt against an archive's own
      `SourceInstance` means "already processed") instead of adding a
      column. Six new services under `app/classification/`:
      `eligibility_service` (suffix-only `ELIGIBLE`/`EXCLUDED`/
      `UNSUPPORTED` decision, never opens a file), `workspace` (shared
      layout helpers), `identity_resolution_service` (claims + hashes
      + resolves a loose file's identity), `archive_processing_service`
      (claims + recursively extracts an archive, idempotent
      check-before-create member discovery — crash-safe by
      construction, not by wrapping the walk in one transaction),
      `normalization_service` (claims `EXTRACTED` → creates `Document`
      → `NORMALIZED`), `chunking_service` (claims `NORMALIZED` →
      `DocumentChunk` rows with `embedding=NULL` → `CHUNKED` —
      deliberately not reusing `EmbeddingService.embed_document`'s
      atomic chunk+embed, since the frozen state machine needs these
      as two independently resumable steps), `pipeline_embedding_service`
      (claims `CHUNKED`/`EMBEDDED`, embeds only `WHERE embedding IS
      NULL`, advances to `INGESTED` once none remain — resumability is
      structural, not special-cased). `ContentIdentityService.
      get_or_create_group` gained an `initial_pipeline_state` parameter
      (default unchanged); `DocumentCreate`/`DocumentService.
      create_document` gained `content_identity_group_id`.
      `FileAccessService` hardened per `6491dad`'s recommendation: an
      explicit `source_type` allow-list (`{"filesystem"}`), verified via
      `grep` to cost zero behavior change for any existing caller.

      **All nine required test scenarios proven**, real Postgres where
      concurrency/crash behavior is at stake: same identity → one
      group; concurrent workers → one owner per work item (10 identity-
      resolution workers/10 files, 6 archive workers/6 archives, 8
      normalization workers/8 groups — every item processed exactly
      once); worker crash → recoverable claim (inherited from `ce50875`,
      unchanged); retry → no duplicated Document/Chunk/embedding state;
      archive crash halfway → resumable without duplicate members
      (proven by pre-creating one member row, simulating the crash);
      unsupported/corrupt input → durable `FAILED`/`UNSUPPORTED` — a
      real bug found and fixed here: `extract_text()` can raise
      library-specific exceptions from `pypdf`/`docx`/`pptx`/`openpyxl`,
      not only `UnicodeDecodeError`/`OSError` — the original narrower
      handler would have let such an exception escape uncaught instead
      of recording a durable `FAILED` attempt; excluded input →
      `EXCLUDED`, never `FAILED`; embedding crash → resumable/idempotent
      (proven via a fake embedding client's call log confirming an
      already-embedded chunk is never re-sent); T7-unavailable
      simulation → source remains represented, no destructive behavior.

      A separate bug was found and fixed while writing the concurrency
      tests themselves (not the pipeline code): synthetic file content
      that wasn't unique per test invocation caused `ContentIdentityGroup`
      rows to be legitimately shared across separate test runs —
      correct production behavior, but it made a naive delete-by-id
      test cleanup fail with a foreign-key violation against leftover
      data from an earlier run. Fixed by making test content unique per
      invocation and cleanup delete a group only if nothing still
      references it.

      Full suite: **711 passed, 1 skipped**, run three times, zero
      flakiness (687 pre-existing + 24 new). The 3 concurrency tests
      additionally run 10 consecutive times with no flakiness.
      `aibrain_test` confirmed empty of synthetic rows after every run.
      Main `aibrain` database completely untouched — no migration was
      even needed this gate. No T7 access of any kind at any point. See
      `AI_Brain_Architecture.md`'s "Controlled Ingestion Implementation"
      section for the full report.

      **APPROVED, with two precise corrections preserved in the
      architecture doc**: (1) the concurrency proof's invariant is
      stated more carefully than "every item processed exactly once" -
      each work item is claimed by at most one ACTIVE worker at a time,
      and retries converge idempotently to the existing durable state;
      this is claim-ownership-plus-idempotency, never a promise of
      global exactly-once execution (a crash can land after an
      external side effect but before its state is recorded - claim
      ownership and idempotency are what supply safety there, not
      atomicity between the two). (2) The archive-crash-halfway
      invariant is now stated explicitly, not just demonstrated: a
      retry must deterministically identify already-existing members
      and create only the missing ones, never duplicating their
      `ProvenanceLink` rows and never mutating their already-recorded
      `evidence_snapshot` - flagged for re-verification one nesting
      level deeper before any real-T7 authorization, since archive
      nesting is where future complexity concentrates.

      **Next gate, not yet authorized**: real T7 discovery-report
      ingestion into this pipeline, resource limits/scheduling/
      orchestration for running multiple workers, and eventually real
      extraction/embeddings against the actual T7 — still no T7 access
      of any kind until each is separately opened. The user's
      recommendation: one more synthetic-only pre-real-T7 review gate,
      specifically verifying the integration boundary between this
      ingestion code and the real-path/security layer (`FileAccessService`'s
      allow-list, the `ImportJob` source-reference convention), before
      the next authorization opens read-only real-T7 ingestion.

- [x] Synthetic-only pre-real-T7 review gate — closed at `a0523b7`,
      test-only, no production code changed (the implementation already
      matched the frozen design). Extended nested-archive crash-resume
      coverage to three levels deep (`A.zip → B.zip → {file 1, file 2,
      C.zip → file 3}`) and closed the `FileAccessService` ↔ `ImportJob`
      boundary gap, including the defense-in-depth "mislabeled
      `source_type`" case — deliberately with zero literal T7 mount
      path anywhere, even as a "should be rejected" input, since
      `Path.resolve()` performs real stat/readlink syscalls against any
      real, mounted path prefix. Full suite: 714 passed, 1 skipped, run
      three times. Neither T7 path was accessed or referenced anywhere
      in this gate. **APPROVED for closure.**

- [x] Real T7 Read-Only Ingestion Pilot — the first-ever authorization
      ("AUTHORIZE: REAL T7 READ-ONLY INGESTION MILESTONE", checkpoint
      `a0523b7`) to read actual file content from the mounted real T7
      corpus, strictly read-only and limited to a small controlled
      pilot batch per the authorization's explicit first-pass-limit
      instruction. Mount confirmed against D1's own recorded report
      root before selecting candidates; the `b9a82d073399`/
      `fd9f81672e59` migrations were applied to the main `aibrain`
      database for the first time (previously `aibrain_test`-only),
      with before/after row-count verification on every pre-existing
      table. Ollama confirmed not running, so real embedding calls were
      expected (and correctly took) the `EMBEDDING_UNAVAILABLE` failure
      path, not a success path.

      Pilot batch (`scripts/t7_ingestion_pilot.py`): three small loose
      files and one small archive, selected programmatically from the
      already-committed D1 report (never re-scanned). All three loose
      files resolved identity correctly; the archive was correctly
      found to contain zero real file members (four empty directory
      entries only, independently confirmed via `unzip -l`). Every
      source path's `(size, mtime)` was verified identical before,
      during, and after — including one final re-check after all
      diagnosis/cleanup below — confirming the real T7 corpus was never
      altered.

      **A real bug was found** via the pilot script's own idempotency
      check: `WorkerClaimService.claim_source_instance_for_identity_
      resolution` had no exclusion for archive-suffixed or
      archive-member `SourceInstance` rows (both legitimately and
      permanently keep `content_identity_group_id IS NULL`), so a
      second, unnecessary claim call wrongly claimed the archive's own
      row and hashed its raw compressed bytes as content — producing a
      bogus `ContentIdentityGroup` that correctly then failed at
      normalization (`CORRUPT_INPUT`, invalid UTF-8). No `Document`/
      `DocumentChunk` rows resulted. The real T7 file itself was
      confirmed completely unchanged throughout — this bug corrupted
      only this application's own database bookkeeping, never the
      source corpus. Fixed by adding `member_path IS NULL` and an
      archive-suffix exclusion to the claim query (mirroring the
      exclusion `claim_source_instance_for_archive_processing` already
      had); two regression tests added, each independently verified to
      fail against the pre-fix query and pass against the fixed one.
      The erroneous database rows and stray workspace file were cleaned
      up; the three legitimate resolved groups were left untouched.
      Full suite re-run three times after the fix: **716 passed, 1
      skipped**, zero flakiness (714 pre-existing + 2 new). `documents/`
      (the extraction workspace, now holding real personal file
      content for the first time) was added to `.gitignore`, mirroring
      the existing `knowledge/t7_discovery/` reasoning. See
      `AI_Brain_Architecture.md`'s "Real T7 Read-Only Ingestion Pilot"
      section for the full report.

      **Stopped at the authorized scope**, per the authorization's
      explicit closing instruction: no broader ingestion, mutation, or
      dedup disposition was started. **Next gate, not yet authorized**:
      any ingestion beyond this four-item pilot batch, and the
      dedup-disposition/mutation gate the authorization explicitly
      excluded.

- [x] Controlled Real-T7 Batch Ingestion — the second real-T7
      read-only gate, opened once the embedding-infrastructure
      prerequisite named after the first pilot was verified: Ollama
      running (v0.31.2), `nomic-embed-text` pulled (768 dimensions),
      raw `EmbeddingClient` verified, and the full synthetic
      `PipelineEmbeddingService` path verified end-to-end. Ollama was
      already running before this batch and was not restarted by it.

      Seven representative real cases (already-known identity reuse,
      ordinary loose file, duplicate-content convergence, archive with
      real members, unsupported file, corrupt input, and an item
      reaching real embedding), selected via a systematic read-only
      D1-report survey plus two direct T7 lookups. A naturally-
      occurring nested archive was deliberately not included — 42 real
      `.zip`/`.7z` files up to 50MB were checked and none contained a
      nested member; by explicit decision, synthetic 3-level coverage
      (`a0523b7`) remains authoritative for that scenario rather than
      expanding the search into larger archives.

      **Full chain proven on real, diverse content**: already-known
      identity correctly reused the SAME pre-existing production
      `ContentIdentityGroup` (id=1) from the first pilot; two distinct
      real duplicate paths converged onto one new group; a real
      archive's two real CSV members extracted with correct `T7_FILE →
      ARCHIVE_MEMBER` provenance; a real unsupported file reached
      durable `UNSUPPORTED` with zero attempt; a real corrupt file (a
      genuine Office lock file under a `.pptx` extension, confirmed
      invalid via direct inspection before selection) reached durable
      `FAILED`/`CORRUPT_INPUT`; 8 real chunks across 4 groups were
      embedded via genuine Ollama calls, all reaching `INGESTED`;
      every claim method's second and third call returned `None` with
      zero new `SourceInstance`/`ProvenanceLink` rows on repeated
      no-op retries. **No new defect was found** — confirmation that
      the claim-exclusion bug fixed after the first pilot did not
      recur under a second, more diverse real-data run.

      Source integrity verified at four checkpoints plus one
      independent recheck: every real path's filesystem metadata
      (size, mtime) remained unchanged throughout — stated precisely,
      per review, as strong corroborating evidence of no mutation
      rather than a cryptographic proof of byte-for-byte identity; the
      stronger safety property is that this pipeline has never had any
      write/rename/delete/quarantine capability against a source path.

      Main `aibrain` database: `ContentIdentityGroup` 3→9,
      `SourceInstance` 4→13, `ProvenanceLink` 4→15, `Document` 3→7 — no
      schema change. This is now genuine real-ingestion data, not a
      synthetic validation artifact. Full suite: **717 passed, 0
      skipped**, run three times (the one previously-always-skipped
      real-Ollama test on the older, unrelated `ImportJob` path now
      runs and passes, incidentally).

      **Pre-commit sensitivity fix**: the batch script initially
      hardcoded real T7 paths/personal filenames as source constants.
      Per review, refactored so the committed script contains only
      generic case labels and loads real paths at runtime from a
      sibling `scripts/t7_batch_selection.json`, gitignored via a new
      `scripts/*_selection.json` rule (same reasoning as
      `knowledge/t7_discovery/`/`documents/`) — verified via direct
      `grep` that the committed file contains no T7 path fragment or
      personal filename. See `AI_Brain_Architecture.md`'s "Controlled
      Real-T7 Batch Ingestion" section for the full report.

      **APPROVED as a successful controlled-batch milestone — explicitly
      not permission for broader ingestion.** Full corpus ingestion
      (~727 GB remaining), dedup execution/deletion/quarantine, and any
      T7 mutation all remain closed. **Next gate, not yet authorized**:
      a deliberate, separate decision about the size and rules of the
      next real-T7 ingestion batch.

- [x] Scaled Real-T7 Ingestion Design — design-only, **APPROVED and
      frozen at the conceptual/design level** after 4 review rounds; no
      T7 access, no schema/code written. Grounded in the current
      codebase: no batch/envelope/
      pause/resource-monitoring concept exists anywhere yet; the only
      disk check anywhere is `ArchiveExtractor`'s hardcoded per-archive
      constants.

      Corrects "D0 = frozen snapshot" to "D0 = observed corpus
      manifest; the filesystem may have drifted since." Eligibility is
      scoped to the current `DiscoveryRun`'s own observations, not
      "ever touched, globally" — preserving `path != physical
      occurrence != content identity`, so a later `DiscoveryRun` can
      legitimately re-observe a changed path as a new `SourceInstance`.

      **Frozen invariants**: every `IngestionBatch` owns exactly one
      dedicated, never-shared `ClassificationRun` — batch membership is
      therefore just "the `SourceInstance` rows under that run,"
      immutable once created, never re-derived by re-running selection
      (the scaling analogue of Chain 1's immutable execution-plan
      snapshot). A deterministic selection fingerprint (D0 hash +
      policy/ordering versions + envelope + selected references) proves
      exactly what was authorized.

      Two independent per-`SourceInstance` classification dimensions:
      `source_category` (strictly physical: `LOOSE_FILE`/`ARCHIVE`/
      `SPECIAL` — backup/sync/snapshot naming is a separate, explicitly
      deferred contextual signal, not a peer category) and
      `workload_category` (`TEXT_DOCUMENT`/`STRUCTURED_DATA`/`MEDIA`/
      `CODE`/`SOFTWARE`/`ENCRYPTED`/`CONTAINER`/`UNKNOWN` — an archive's
      own row gets `CONTAINER`, never a guessed blend of its unopened
      members). Archive admission (container-level, pre-extraction) and
      archive member policy (post-extraction, once members are real
      rows) are two distinct evaluation stages — unopened members never
      affect initial batch-selection sizing. Risk tier is two-phase:
      `risk_tier_estimated` at selection, `risk_tier_actual` honestly
      **nullable**, populated only once real post-extraction evidence
      exists, never fabricated on early failure.

      `max_extracted_bytes` enforced via isolated per-item staging
      directories, discarded wholesale on overflow (zero partial
      artifacts ever become durable); `max_embeddings` enforced via the
      same atomic-claim discipline as `WorkerClaimService` (a single
      conditional `UPDATE`, not read-then-act, closing a real
      concurrent-overshoot race); `max_runtime` uses a monotonic clock
      for stop decisions, wall-clock timestamps for audit only. Batch
      state (`IngestionBatch.status`) and work-item state
      (`pipeline_state`) are independent axes, always reported
      together. Completion is reported as four denominators (eligible /
      attempted / terminal / successfully-ingested), never one
      percentage. Host resource monitoring beyond disk/Postgres/Ollama
      is optional and pluggable — `psutil` is deliberately not added as
      a dependency.

      New schema (not yet migrated): one `ingestion_batches` table plus
      four new nullable columns on `SourceInstance`
      (`source_category`/`workload_category`/`risk_tier_estimated`/
      `risk_tier_actual`). See `AI_Brain_Architecture.md`'s "Scaled
      Real-T7 Ingestion Design" section for the full specification.

      **Not decided in this design**: numeric envelope defaults, risk-
      tier numeric boundaries, the backup/sync context field's exact
      structure/pattern list, and per-batch-class (the earlier "Batch
      A–G" sketch) policy filter definitions.

      **Next gate, not yet authorized**: a separate numeric/policy-
      definition pass (batch-class inclusion/exclusion rules, size/risk
      thresholds, archive admission thresholds, resource envelopes,
      promotion criteria) must be completed and explicitly approved
      before any implementation design. The real T7 remains outside the
      ingestion gate until then — this freeze authorizes no code, no
      schema migration, and no T7 access.

- [x] Scaled Real-T7 Ingestion — Numeric + Policy Definition Pass —
      **APPROVED and frozen**, design-only, sitting on top of the frozen
      architecture (`f2b9815`).
      No T7 access, no scan, no schema/code change. Every number is
      derived from the two existing reports (`inventory.json`,
      `duplicate_analysis.json`) plus direct local disk/database
      measurement — never invented, never a fresh corpus traversal.

      Replaces the illustrative "Batch A–G" sketch with 8 concrete
      classes grounded in real D0/D1 evidence: Class 1 (Text/Document,
      `LOOSE_FILE` only) is the primary initial-rollout candidate — the
      corpus's document category is only 2.6% of bytes (17.6GB /
      377,943 files, ~46KB average) but by far the lowest-risk. Classes
      2–5 (Small/Medium/Large/Extreme Archive) use D1-derived
      **source-size** tiers (<10MB / 10MB–1GB / 1–5GB / 5GB+ — declared
      compressed bytes only, explicitly not an expansion-risk claim),
      covering 97.9% of D0's known `.zip`/`.7z` files. Classes 6/7
      (Media/Software) are **deferred, completeness-only** — not
      ordinary low-risk ingestion classes — since no extractor exists
      for that content today; they exist purely so the completeness
      denominator accounts for the corpus's `UNSUPPORTED` share. Class 8
      (Special/Deferred) explicitly excludes `.c9r`, `.html` (40.1GB —
      the corpus's single largest "unaccounted" contributor, which
      would otherwise silently succeed as raw-markup "text" with no
      real extractor), `.dat`/`.db`/no-extension, and Anki files.

      **Real finding, worded to keep the architecture invariant and the
      scaling optimization distinct**: archive containers are not
      deduplicated through `ContentIdentityGroup` (correct — a
      container isn't itself content), so identical archive *copies*
      may otherwise be independently, redundantly extracted. The 1–5GB
      tier alone has 31 distinct archives appearing as 68 physical
      copies (~2.2× average duplication). Selection **may** admit at
      most one representative per D1 exact-duplicate group (SHA-256
      content hash) for extraction — never on filename/path similarity,
      directory-naming convention, or compressed size alone. This is a
      selection/scheduling decision only: the other physical copies
      remain exactly what they already are — real, distinct
      `SourceInstance` rows and provenance observations — never treated
      as deleted, discarded, or globally equivalent, and no dedup
      mutation of any kind occurs.

      **Resource-envelope correction, per review**: `max_extracted_bytes`
      must not be read as "however much workspace disk happens to be
      free." Each physical resource (workspace `/home`, PostgreSQL `/`)
      is governed by the same inequality with an explicit, visible
      safety reserve: `projected_consumption + projected_background_growth
      + safety_reserve <= currently_available_capacity`, re-checked live,
      never assumed fixed. **The 10GB/5GB reserve values are configured
      policy; the 25GB/13GB free-space figures are runtime measurements
      of this machine right now, not each other** — a future
      implementation must re-measure free space live, never treat
      today's numbers as permanently known facts. Concretely, using
      today's measurement as a worked example: workspace reserve 10GB
      (25GB free today → 15GB extraction budget); PostgreSQL reserve
      5GB (13GB free today → 8GB db-growth budget). No DB-growth-per-
      embedding formula is invented — the governing invariant instead
      uses the three tiers already defined per-guard (soft stop, review-
      required, hard stop): **soft-stop fires before hard-stop** as a
      resource depletes (a controlled pause — no further work admitted —
      well before an emergency stop), review-required is a separate,
      projection-based check orthogonal to that live sequence, and
      **the tightest remaining resource budget governs continued
      execution** across every guard, with hard-stop as the last-resort
      tier, never the first threshold that matters. Per-embedding DB
      cost is an empirical output Class 1 must measure, not an input
      assumed now.

      First scaled batch defined precisely: Class 1, 1,000 files
      (an explicit **policy choice**, not a threshold derived from a
      rate/scaling formula), 2GB source-byte safety cap, and a 2-hour
      runtime ceiling — **also an explicit provisional policy choice
      requiring later calibration, the same status as the 1,000-file
      figure**, not a measured limit. `max_embeddings` is **CALIBRATION_REQUIRED**
      — per review, the earlier "~5 chunks/file" estimate was an
      arithmetic error: the two pilots' only real evidence (5 items,
      3–2,821 bytes each, ~1 chunk per 700–1,400 bytes) actually implies
      ~33–66 chunks/file against this category's real ~46KB average, and
      5 tiny items cannot be responsibly extrapolated to that population.
      This gap must be closed by Class 1's own execution-authorizing
      gate, not guessed here; the atomic-reservation mechanism applies
      to whatever ceiling is ultimately set. Class 1's promotion evidence
      now explicitly includes the calibration inputs every later class
      needs: chunks-per-source distribution, embeddings-per-source
      distribution, embedding throughput/latency, DB growth per embedded
      chunk, workspace consumption, processing time, and retry/error
      distribution. Promotion ladder remains evidence-based, not
      size-doubling: each step requires zero safety incidents,
      denominator reconciliation, and (starting at Class 2) real
      expansion-ratio/risk-estimation-accuracy evidence this corpus has
      never produced before, since both real pilots' archives had zero
      actual members.

      Resource-guard thresholds set from real, just-measured local
      state: the PostgreSQL data volume (`/`, 13GB free of 110GB) is
      the *tighter* constraint, not the workspace volume (`/home`, 25GB
      free) — worth naming explicitly since it's easy to assume
      otherwise. Ollama embedding-latency ceiling and all archive-
      extraction-envelope/risk-tier-actual numeric boundaries remain
      explicitly marked **UNRESOLVED**, deferred to calibration from
      Class 1/2's own real results, not guessed. Archive source-size
      tiers (SMALL/MEDIUM/LARGE/EXTREME) name declared compressed bytes
      only — `risk_tier_estimated` is informed by, but kept distinct
      from, that size tier, leaving room for future non-size signals.
      `selection_policy_version` is named per batch-class/policy (e.g.
      `"batch-class-1-text-document-v1"`), separate from envelope
      values, which already feed the selection fingerprint directly.

      See `AI_Brain_Architecture.md`'s "Scaled Real-T7 Ingestion —
      Numeric + Policy Definition Pass" section for the full
      specification, including the decided-now vs. requires-calibration
      table.

      **APPROVED as the numeric/policy baseline** — Class 1's five
      envelope values (`max_source_instances=1,000`,
      `max_source_bytes=2GB`, `max_extracted_bytes=N/A`,
      `max_embeddings=CALIBRATION_REQUIRED`,
      `max_runtime_seconds=7,200` provisional), the 10GB/5GB policy
      reserves, the four archive source-size tiers, D1-duplicate
      scheduling-only handling, and Media/Software deferred/
      completeness-only status are all frozen as the baseline.
      UNRESOLVED/`CALIBRATION_REQUIRED` items remain intentionally
      deferred, not disguised as settled numbers. **This approval
      authorizes no implementation, schema/migration work, or real-T7
      access.** **Next gate, not yet authorized**: a separate
      implementation design pass translating this policy layer into
      `IngestionBatch`, selection, resource-guard, reservation,
      reporting, and worker-control behavior while preserving every
      contract frozen at `f2b9815` — implementation itself is a further,
      later gate beyond that.

- [x] Scaled Real-T7 Ingestion — Implementation Design Pass —
      **APPROVED and frozen**, design-only, sitting on top of both
      frozen gates (`f2b9815` architecture, `3d37ec0` numeric/policy).
      No T7 access, no code, no schema/migration.

      Two real findings from directly re-reading the current code (not
      assumed): `EmbeddingClient.embed()` is genuinely all-or-nothing —
      one Ollama HTTP call for the whole chunk list, all vectors or an
      exception. `ArchiveExtractor.extract()` is atomic per archive
      (`extractall()` in one call, no mid-extraction hook) — meaning the
      frozen "monitor extracted bytes during extraction" language is
      implemented as a **pre-flight check** (sum declared member sizes
      before calling `extract()`, refuse to start if it would overflow
      the remaining budget) rather than literal interruption mid-write —
      achieving the same "never a durable partial artifact" invariant
      via prevention, not a weakening of what was frozen.

      Full `IngestionBatch` schema specified (fields, types, nullability,
      immutability rules, `BatchStatus`/`BatchStopReason` enums, a new
      `review_required` boolean kept orthogonal to `status`), plus three
      new selection-time count fields
      (`eligible_source_count`/`policy_filtered_count`/
      `selectable_count`) needed because those funnel counts can't be
      reconstructed later from the final rows alone. Batch creation is a
      two-phase design: read-only computation, then one atomic write
      transaction serialized by a Postgres advisory lock keyed on the
      `DiscoveryRun` (the same class of DB-native primitive this
      codebase already uses elsewhere, applied where no row yet exists
      to lock).

      **Round 2 correction — batch isolation, now fully specified, not
      merely flagged**: today's `WorkerClaimService` claim queries have
      no `classification_run_id` filter at all (verified directly) —
      the frozen fix adds it as a required parameter to both
      `SourceInstance`-level claim methods, with batch identity reaching
      the claim via explicit parameter passing only (no implicit/global
      context, matching this codebase's existing style), plus a required
      adversarial concurrency test (two batches, real-Postgres
      `threading.Barrier` workers, proving zero cross-batch claims).
      `claim_content_identity_group` stays global by explicit decision —
      a shared group from cross-batch duplicate convergence is charged
      to whichever batch claims it first (accepted budget-attribution
      imprecision, not a correctness bug or an isolation violation,
      since `SourceInstance`-level claims — the only ones that touch T7
      — remain strictly batch-scoped regardless). Batch membership,
      content-identity convergence, and worker ownership are now stated
      as three explicitly distinct concepts to prevent conflating them.

      **Round 2 correction, then Round 3 hardening — embedding-
      reservation crash recovery, now a fenced, closed design**: Round 2
      added `ContentIdentityGroup.reserved_embeddings` (co-located with
      the existing `claimed_by`/`claimed_at` markers — no new table),
      released via a presence-only conditional `UPDATE ... WHERE
      reserved_embeddings IS NOT NULL`. Round 3 correctly identified
      that presence alone is insufficient: a worker merely *delayed*
      (not crashed) can wake after recovery has already reclaimed its
      row and a *new* worker has reserved fresh capacity — its stale
      release could match the new reservation purely because both leave
      the column non-NULL. **Fix**: a second new column,
      `ContentIdentityGroup.claim_generation` (integer, incremented on
      every claim/reclaim), fences every reserve/consume/release/
      recover operation to the exact generation that created it — a
      delayed worker's remembered, older generation value matches zero
      rows once the row has moved on, structurally, not by timing luck.
      Recovery is authorized precisely because it reads the row fresh
      under its own claim lock, never a remembered value. Explicitly
      proven sufficient across content-identity/work-item/batch/
      generation axes without a new table, since the claim is globally
      exclusive per row regardless of how many batches' `SourceInstance`s
      reference the same group.

      **Round 2 correction — archive extraction pre-flight, now
      precisely distinguishes three quantities**: `declared_member_bytes`
      (an estimate from the archive's central directory, before any
      extraction), `actual_durable_extracted_bytes` (measured after a
      successful atomic `extractall()`, via the extractor's own existing
      per-file `stat()` mechanism), and peak/transient staging usage (a
      live-disk-pressure concern for the resource guard, never the
      extracted-bytes counter). Declared size is never treated as proof
      of actual bytes written — a post-extraction reconciliation step
      corrects the durable counter to the measured value.

      **Round 2 correction — advisory lock, now exactly specified**:
      scope (per `DiscoveryRun`), key derivation (a namespaced string
      hash, never a raw integer id, avoiding future collision with
      unrelated advisory-lock use), transaction-scoped lifetime
      (`pg_advisory_xact_lock`, auto-released on commit/rollback, no
      leak risk), and an explicit statement that the lock is paired
      with, never a substitute for, a recommended `SourceInstance`
      `UNIQUE` constraint as independent defense-in-depth.

      Selection's stop-not-skip rule (point 5) is now explicitly tied to
      `selection_fingerprint` reproducibility, not merely stated as a
      simplicity preference. `COMPLETED` vs. `PAUSED` vs. `ABORTED`
      remain mapped precisely by `stop_reason` category: envelope/work
      exhaustion is a *designed* path to `COMPLETED`, never treated as a
      failure; only genuine resource-guard hard-stops or safety
      violations reach `ABORTED` (terminal — retry needs a brand-new
      batch, never resuming one).

      A required adversarial test is added for the fencing mechanism
      specifically: a delayed worker's late release must be proven, on
      real Postgres, to leave a subsequent claimant's reservation and
      the batch counter completely untouched.

      See `AI_Brain_Architecture.md`'s "Scaled Real-T7 Ingestion —
      Implementation Design Pass" section for the full specification,
      including the 12-milestone implementation order and the updated
      decided/requires-verification/deferred gap register (all Round-2
      and Round-3 corrections now live in "decided," not "requires
      verification").

      **APPROVED as the implementation design — this freeze authorizes
      no code, schema/migration, or real-T7 access.** **Next gate, not
      yet authorized**: implementation itself, beginning with the first
      independently reviewable milestone from the 12-step sequence
      (schema/model design review) rather than jumping directly to
      real-T7 execution — each subsequent milestone remains its own,
      separately authorized step.

- [x] Scaled Real-T7 Ingestion — Implementation Milestones 1–4 —
      **committed** (`cc8dbec` schema/model, `4709ffd` batch creation +
      deterministic selection, `ca3ab4e` resource guard + batch
      runtime/state control, `6513948` batch-aware worker claims +
      generation-fenced reservations), each independently reviewed and
      separately authorized. 861 tests passed, 0 skipped as of the
      Milestone 4 commit. `aibrain` remains at `fd9f81672e59`
      (unmigrated) — every milestone's migration has been applied only
      to `aibrain_test`; real-T7 access remains completely closed
      throughout. See the individual commit messages and
      `AI_Brain_Architecture.md`'s corresponding sections for each
      milestone's exact scope.

- [x] Scaled Real-T7 Ingestion — Milestone 5 Design: Archive Processing
      / Extraction — **design pass reviewed and frozen (after a
      correction pass), then implemented and committed at `6acdc53`**.
      Sits on top of the already-frozen implementation design
      (`2fab4b3`) and the four committed milestones above; makes the
      **already-existing, already-tested** archive pipeline from the
      earlier "Controlled T7 -> AI_Brain Ingestion Design" chain
      (`ArchiveExtractor`, `ArchiveProcessingService`, `ProvenanceLink`)
      batch-aware and envelope-honest — explicitly not a from-scratch
      redesign of archive extraction.

      Confirms by reading the code directly (not assumed): extraction
      is atomic per format (`extractall()`, no mid-write interruption
      point), crash-resumability already exists via idempotency (not
      transactional wrapping) through `_find_existing_member`, and
      nested-archive recursion/provenance-chain construction already
      work. Identifies the concrete gaps Milestone 5 must actually
      close: the archive claim call site doesn't yet pass
      `classification_run_id` (Milestone 4 already added the parameter
      it needs); the current extraction destination
      (`workspace_root/archive_<id>/depth_<n>_<stem>`) doesn't match
      the frozen `_staging/<source_instance_id>/` isolation-and-cleanup
      model; member-level classification
      (`source_category`/`workload_category`/`risk_tier_estimated`) is
      specified but not wired into the member-creation loop; and
      `SourceInstance.risk_tier_actual` doesn't exist yet.

      Freezes a reconciled staging/promotion model (staging root keyed
      by the root archive's durable `SourceInstance.id`, nested
      subdirectories named by a stable hash of each member's own full
      internal path rather than an attacker/data-controlled string),
      an envelope pre-flight/reconciliation algorithm generalized to
      whole recursive claim attempts (not per nested level), and an
      unconditional staging-cleanup rule at both the start and end of
      every claim attempt.

      **Design Correction Pass applied — the blocking finding is now
      resolved at the design level.** Review correctly rejected the
      original pass's proposed resolution for the `SourceInstance`
      fencing gap (add the column, OR accept the risk with a shorter
      lease) — a shorter lease narrows the race window but does not
      remove the underlying correctness dependency. The correction
      freezes a full `SourceInstance.claim_generation` design
      (durable column, fenced acquisition/release, unchanged
      stale-recovery mechanism), generalizing `ContentIdentityGroup`'s
      Milestone 4 pattern exactly — remaining work is implementation-
      only (one migration, two call-site updates). The same pass
      empirically confirmed ZIP's symlink-mode extraction behavior
      (a synthetic reproduction proved `extractall()` never creates a
      real symlink) and, by reading `py7zr` 1.1.3's own source
      directly, found and closed two previously-unknown, concrete
      issues: `is_junction` (Windows-junction) members were never
      checked by the existing 7z-member validation, and `is_socket`
      members — which `py7zr` itself silently declines to write — would
      have crashed the existing file-discovery step with an uncaught
      `FileNotFoundError`. A general, library-version-independent
      post-extraction file-type audit (regular files and directories
      only, verified via `lstat`) closes the remaining irreducible
      uncertainty (`py7zr` exposes no distinct hardlink flag) as a
      fail-closed backstop. The `max_depth`-exceeded outcome is now an
      exact, frozen terminal state (`FAILED`/`OVERSIZED_OR_EXPANSION_
      LIMIT`/`retryable=False`), member classification has exact
      verified function calls (with an explicit confirmation that
      archive membership is never treated as a backup/sync/snapshot
      signal), `SourceInstance.risk_tier_actual`'s exact shape and
      input formula are specified, the dead `SUSPICIOUS_EXPANSION_
      RATIO` constant is recommended for removal rather than given
      invented semantics, and the archive-processing lease question is
      tied to the SAME already-acknowledged per-class calibration gap
      the numeric pass already named for other envelope fields — never
      a newly-invented number. All eight original gaps now carry an
      explicit final disposition (design-resolved, or a named,
      bounded implementation-only remainder) rather than an open
      question; all fifteen mandatory invariants were re-reviewed
      against the corrected design — none weakened, one (zombie-worker
      protection) newly satisfied for `SourceInstance`, four
      strengthened by an added independent verification layer.

      See `AI_Brain_Architecture.md`'s "Scaled Real-T7 Ingestion —
      Milestone 5 Design: Archive Processing / Extraction" section for
      the full specification, including the corrected claim/generation
      design (section 2), the archive-member safety findings (section
      4a), the frozen `max_depth` outcome (section 13), the resolved
      gap register (section 19), the updated acceptance criteria
      (section 20), and the invariant re-review (section 21).

      **This pass authorizes no code, no migration, no test-code
      changes, and no real-T7 access.** **Next gate, not yet opened**:
      either a further design-review round or a separate, explicit
      implementation authorization for Milestone 5.

- [x] Scaled Real-T7 Ingestion — Milestone 6 Design: Identity Resolution
      & Embedding-Reservation Batch Integration — **design pass,
      reviewed and FROZEN after a correction pass; implemented and
      committed at `e06de5a`**. Opened after Milestone 5
      (`6acdc53`) closed; establishes the boundary by inspecting the
      repository directly against the frozen Implementation Design
      Pass's (`2fab4b3`) 12-step order rather than assuming a roadmap
      item: steps 1-8 and 10 are done (Milestones 1-5); step 9
      ("worker/claim integration — the loop that ties 3-8 together per
      batch") is done only for archive processing (Milestone 5) and
      still open for identity resolution and embedding; step 11
      (`BatchReportService`) does not exist yet; step 12 (final
      adversarial-test milestone) remains correctly gated on both.

      Identifies two concrete, already-evidenced gaps by reading the
      code directly: `IdentityResolutionService.resolve_next` never
      passes the `classification_run_id` parameter its own claim method
      has already accepted since Milestone 4; and
      `PipelineEmbeddingService.embed_next` never calls any of the
      three embedding-reservation methods (`reserve_embeddings`/
      `consume_embedding_reservation`/`release_embedding_reservation`)
      Milestone 4 already built and froze — meaning `max_embeddings` is
      completely unenforced by any code path that actually calls the
      embedding backend today. Confirms `NormalizationService`/
      `ChunkingService` need no equivalent change: `claim_content_
      identity_group`'s global (non-batch-scoped) claim is
      already-frozen, deliberate design, not a gap.

      Freezes the exact owning-batch resolution query and its
      deterministic (lowest-`id`-wins) tie-break rule for cross-batch
      identity convergence, and the explicit "no currently-`RUNNING`
      owning batch → proceed unreserved, never strand the item at
      `CHUNKED`" invariant — both previously unspecified. Adds no new
      schema, migration, `ExpensiveOperationKind`, or pipeline state.

      **Design Correction Pass applied — reservation-ownership semantics
      now resolved explicitly.** Review correctly declined to freeze the
      original pass because it specified *how* an owning batch is picked
      (lowest-`id` tie-break) without resolving *what that ownership
      means*: whether `RUNNING`-only eligibility is enforced once or
      continuously, whether lowest-id implies any business/semantic
      priority, what happens when the resolved batch stops being
      `RUNNING` (before or after reservation), whether a non-owner
      batch's spare capacity can substitute, how `reserved_embeddings_
      batch_id` is cleared/recovered, and how `claim_generation` fences
      stale workers from touching a reservation they no longer own. The
      correction (new section 4a) resolves all of this: lowest-id is
      confirmed pure, reproducible arbitration with **no** semantic
      priority; `RUNNING`-only eligibility is enforced twice
      (independently, at resolution and again atomically at reservation);
      a reservation's fate, once granted, is fully decoupled from the
      owning batch's later status transitions — unconditionally, per
      already-existing Milestone 4 guarantees, never newly invented
      here; all three clear/recovery paths (consume, release, abandoned-
      reservation recovery) read `reserved_embeddings_batch_id` fresh
      from the row, never from a remembered value; `claim_generation`
      fences every reservation operation identically to the existing
      claim mechanism. One bounded scope decision was surfaced and named
      explicitly rather than left ambiguous: no multi-candidate
      reservation fallback within a single attempt if the resolved batch
      denies — the item defers and a later attempt re-resolves. The
      standing principle is restated explicitly: content identity is
      globally convergent; embedding-reservation ownership is a fourth,
      orthogonal concept — pure resource-cost attribution, never content
      or claim ownership.

      See `AI_Brain_Architecture.md`'s "Scaled Real-T7 Ingestion —
      Milestone 6 Design: Identity Resolution & Embedding-Reservation
      Batch Integration" section for the full specification, including
      the boundary derivation (section 0), the exact pseudocode for
      both call sites (section 3), the owning-batch resolution algorithm
      and its corrected ownership semantics (sections 4 and 4a), the
      synthetic test matrix (section 15), and the gap register (section 18).

      Also notes, without correcting (out of this pass's scope): the
      Milestone 5 roadmap entry above still reads "design-only pass
      produced, not yet reviewed or frozen" despite Milestone 5 having
      been implemented, reviewed, and committed at `6acdc53` — a stale
      status left for a future, separately-authorized documentation
      correction.

      **Corrected by Milestone 13's documentation reconciliation pass**:
      both the Milestone 5 and Milestone 6 entries above now reflect
      their actual implemented/committed status; this paragraph is kept
      as a historical record of when and why the staleness was first
      noticed, not deleted.

      **This pass authorizes no code, no migration, no test-code
      changes, and no real-T7 access.** **Next gate, not yet opened**:
      a separate, explicit implementation authorization for Milestone 6.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 7:
      `BatchReportService` — **committed** (`3136ffb`). A pure read/
      aggregation service (`generate_report(batch_id) -> BatchReport`):
      no claim, no lock, no write, no T7 access. Counts are scoped
      correctly to root-level `SourceInstance` rows only (archive
      members excluded from every denominator), with an explicit,
      visible `NULL` bucket for `risk_tier_actual` (still honestly
      unpopulated since Milestone 5) and `actual_source_bytes_read`/
      drift-outcome counts represented via a new `Instrumented[T]`
      type rather than a bare `0`/`None` — both remain explicitly
      deferred, not computed. Denominator contradictions raise
      `BatchReportInvariantViolation` rather than being silently
      clamped. 21 new tests; full regression 930 collected, 929
      passed, 1 skipped (pre-existing, environmental). No schema
      change; `aibrain` unmigrated throughout.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 8:
      `BatchCompletionReconciliationService` — **committed** (`65b4b9c`).
      Closes the last piece of frozen step 9: `check_and_complete
      (batch_id)` reads Milestone 7's own report, declares
      `SOURCE_WORK_EXHAUSTED` iff `unattempted_selected_count == 0`
      and `terminal_source_count == attempted_source_count`, then
      delegates exclusively to the existing `BatchControlService.
      complete()` — never mutates `IngestionBatch` directly.
      Envelope-exhaustion and runtime-budget-exhaustion detection are
      explicitly **not** implemented, deferred to a future, separate
      design decision; `NEEDS_REVIEW` is treated as terminal for this
      decision, distinct from `successful_ingestion_count`. 15 new
      tests, including a real two-worker completion race (80 total
      races, zero flakiness); full regression 945 collected, 944
      passed, 1 skipped. No schema change.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 9: final
      cross-stage adversarial validation — **committed** (`6ad5ffb`),
      test-only, zero production code change. Frozen step 12 of the
      Implementation Design Pass's 12-step order: eight real-Postgres
      concurrency scenarios proving interactions between Milestones
      1–8's already-individually-proven components (mixed-stage claim
      fencing, three-way cross-batch embedding arbitration, reservation
      vs. reconciliation ordering, dual-archive crash/idempotent-retry,
      cross-batch identity convergence, pause→abort→reconciliation, and
      live report reads under concurrent multi-stage load). Two genuine
      test-only fixture bugs were found and fixed, never in production.
      Full regression 953 collected, 952 passed, 1 skipped.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 10: pipeline-
      to-retrieval composition proof — **committed** (`d4d26d7`),
      test-only. Proves, with real execution rather than static
      reading, that a `Document`/`DocumentChunk` produced by driving
      synthetic content through the real Milestones 1–9 pipeline is
      genuinely consumable by the pre-existing, previously-unrelated
      `RetrievalService`/`ChatService`. **Also documents, without
      extending, a real and still-current limitation**: the citation
      contract surfaces only `document_chunk_id`/`document_id`/
      `document_title`/`document_source` — no `SourceInstance`/
      `ProvenanceLink` data — left open as a separate, not-yet-
      authorized "Answer-Provenance Decision." Full regression 955
      collected, 954 passed, 1 skipped. No production code changed.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 11:
      `BatchOrchestratorService` — **committed** (`62592cd`). Model A
      (bounded, attended, single-process execution): `run_once()` runs
      one fixed-order pass through all five pipeline stages (archive
      processing → identity resolution → normalization → chunking →
      embedding), each exhausted once, then exactly one completion-
      reconciliation call — the first real, callable entry point the
      scaled pipeline has had outside test code. Two distinct defects
      were found and fixed during this milestone's own design/
      implementation: a termination gap (a deterministically-failing
      claim is immediately reclaimable, so a naive loop never
      terminates) and, found only after fixing that, a starvation gap
      (the same stuck row keeps winning the claim query's ordering and
      could block newer, distinct work for a whole invocation). Both
      are closed by one additive, in-memory-only `exclude_ids`
      parameter threaded directly into the claim queries
      (`worker_claim_service.py`) — no retry-exhaustion policy,
      counter, or schema change. **Explicitly named but not fixed**: a
      pre-existing (Milestone-5-origin) gap where `claim_source_
      instance_for_archive_processing`'s `already_processed` check
      never excludes a durable `retryable=False` failure — harmless
      today only because the orchestrator's own `exclude_ids` prevents
      it from looping, and remains open as a real, separately-
      authorizable follow-up. 12 new tests; full regression 967
      collected, 966 passed, 1 skipped. No schema change.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 12: operator
      CLI entrypoint — **committed** (`bee4f69`). `scripts/
      run_ingestion_batch.py` — the pipeline's first human-usable entry
      point, giving Milestone 11's `run_once()`/`BatchReportService` a
      real caller. `--database` defaults to `aibrain_test` and cannot
      reach production by omission (built via `make_url(settings.
      DATABASE_URL).set(database=...)`, never `SessionLocal`); batch
      existence is checked explicitly before calling `run_once()`, with
      no broad `except ValueError` around it, since `ValueError` is
      raised throughout `app.classification` for several unrelated
      reasons (proven by direct inspection, not assumed); the CLI opens
      one session for the whole invocation and issues no commit/
      rollback of its own, fully composing the existing per-service
      commit model. Prints a plain-text operator report
      (`BatchReportService`'s fields) after each run; stdlib logging
      only. 11 new integration tests (real database, zero Ollama
      dependency by design); full regression 977 passed, 1 skipped,
      stable across 3 runs. No schema change.

      **Post-Milestone-12 review found two real, previously-unsurfaced
      documentation/architecture gaps** (see `AI_Brain_Architecture.md`'s
      "Scaled Real-T7 Ingestion — Milestones 7–12" section for the full
      writeup): this roadmap and `AI_Brain_Architecture.md` had not been
      updated since Milestone 6 (corrected by this same Milestone 13
      pass), and a second, independent, already-live ingestion path
      (`ImportJob`/`ImportJobService`, "Chain 1") coexists with this one
      ("Chain 2") with their relationship undecided — see
      `AI_Brain_Architecture.md`'s "Chain 1 ↔ Chain 2 relationship"
      subsection, marked **ARCHITECTURAL DECISION REQUIRED**.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 14: live HTTP
      composition proof — **committed** (`a1721b1`), test-only. Proves,
      through the real HTTP layer rather than direct service
      composition (Milestone 10) or the CLI's own report (Milestone 12),
      that content driven through the real, unmodified Milestone 12 CLI
      is genuinely retrievable and citable via `POST /rag/search` and
      `POST /chat`. `EmbeddingClient.embed`/`ChatClient.chat` are
      monkeypatched only at the class boundary; `RetrievalService` and
      `ChatService` themselves are not mocked. Exact `DocumentChunk.id`/
      `Document.id` asserted against the HTTP response, not "a result
      came back." No citation/provenance enrichment introduced - the
      existing four-field citation shape is re-observed, not extended.
      2 new tests; full regression 979 passed, 1 skipped, stable across
      3 runs; zero residue. No schema change, no T7 access.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 15:
      deterministic retrieval ranking — **committed** (`29c6dee`).
      `RetrievalService.search()`'s `order_by` gained a deterministic
      secondary key: `distance ASC, DocumentChunk.id ASC` - primary
      ordering by cosine distance is unchanged; the secondary key only
      resolves an exact-distance tie (a real, already-present condition
      in `test_retrieval.py`'s own fixture, previously tolerated via a
      set-comparison rather than an ordered one). No reranking or
      relevance-model change. `top_k` semantics and the `SearchResponse`/
      `SearchResult` API shape are unchanged. The existing
      `test_retrieval.py` test was extended, not replaced, to assert
      exact tie order and call-to-call repeatability. Full regression
      979 passed, 1 skipped. No schema/migration change, no T7 access.

- [x] Scaled Real-T7 Ingestion — Implementation Milestone 16: embedding
      failure handling — **committed** (`cc53e02`). `EmbeddingUnavailableError`
      (`app/embeddings/client.py`) mirrors the pre-existing
      `ChatUnavailableError` convention exactly - the original exception
      is chained via `from exc`. `POST /rag/search` and `POST /chat` each
      convert only this exception into a clean `HTTP 503`; any other
      exception continues to propagate unconverted, proven by a
      dedicated test. Chain 1's (`ImportJobService._embed_documents`)
      and Chain 2's (`PipelineEmbeddingService`, both call sites)
      existing `except Exception` boundaries required no change - both
      already caught any exception, confirmed by their own regression
      suites (37/37, 46/46) passing unmodified. No retry/backoff
      framework introduced. 4 new tests; full regression 983 passed,
      1 skipped. No schema/migration change, no T7 access.

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
- [x] Dedup review — frontend/API done, see Phase 5's `DuplicateReview`
      entry above for the full design.
- [ ] No conversation list/switcher yet — only ever one active
      conversation per browser (`localStorage`), matching the
      chat-only scope of the original slice.
- [ ] No job-creation form in the UI — creating/executing an import job
      remains API/curl-only, consistent with "monitoring, not management."

## Non-goals (for now)
- No cloud LLM fallback — offline-first is a hard requirement, not a default.
- No multi-user support — this is a single-user personal system.
