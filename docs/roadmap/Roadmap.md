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
