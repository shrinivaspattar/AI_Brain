# Design: role-model traits ("who I want to become like")

Status: DRAFT for review (2026-09-22). Nothing in this note has been run.

## Goal
Let the user name a person and specific traits of theirs they want to adopt
(e.g. "Adnan Khan — purpose over comfort, high energy, fast self-correction"),
have AI_Brain remember it, and have chat weave those traits into answers about
the user's own career/personal-growth notes when relevant — without breaking
the offline/privacy-first principle stated in the README.

## What already does most of this, today, with no new code
The existing memory system (`app/memory/service.py`, `Memory` model) is a
review-gated store of facts about the user, injected into every chat prompt
under "What you know about the user." A role-model trait is just a fact of
that shape. Today, the user can already say in chat:

> "Remember: I want to adopt Adnan Khan's traits — purpose over comfort, high
> energy, fast self-correction."

The model's `remember` tool proposes it, the user approves it in the Memory
Review view, and from then on every chat answer can draw on it. **This works
right now, with zero changes.** The rest of this design is for making that
experience better, not for making it possible.

## What a dedicated feature adds
1. **A distinct kind of memory**, so role-model traits can be told apart from
   other remembered facts (browsed, edited, or turned off as a group) instead
   of being just more rows in one undifferentiated list.
2. **Structure**, so "who" and "which traits" are separate fields instead of
   one free-text sentence — enabling a UI list ("Role Models") instead of
   requiring the chat transcript to hold the record.
3. **Deliberate use in retrieval**, so a career-planning question doesn't just
   passively have the trait sitting in context, but the system prompt
   explicitly invites connecting the two when relevant.

## Proposed design

### Data model
Extend `Memory` with a nullable `kind` column (`SQLEnum`, default `FACT` for
every existing row — additive, no backfill needed) and a nullable `metadata_json`
(`JSONB`) for kind-specific structure. For `kind = ROLE_MODEL_TRAIT`,
`metadata_json` holds `{"person": str, "traits": list[str], "source_url": str | None}`.

This follows the project's existing pattern (`Document.source`, `SourceInstance`
provenance columns) of adding a nullable column rather than inventing a
parallel table, and keeps `content` (used everywhere memories are rendered
into a prompt) as the single human-readable summary regardless of kind.

Alembic migration: one additive column set, `aibrain_test` first, matching how
`D3_MASTER_MANIFEST` was added to `DiscoveryRunKind` in this project already.

### Capturing it
Two ways in, both already-existing mechanisms, not new ones:
- **Chat, today:** the user asks the model to remember it; the `remember` tool
  gains an optional `kind` argument (default `fact`) it can set to
  `role_model_trait` when the user's phrasing matches, with `person`/`traits`
  extracted from the sentence, no new tool needed.
- **API, direct:** `POST /memory` (already exists) accepts the same optional
  `kind` and `metadata` fields for a person who wants to type it in a form
  instead of asking the model to infer it. Whether the frontend ever grows a
  form is a separate, later decision.

### Using it in chat
`ChatService._format_memories` (unchanged mechanism, just formats differently
per kind) renders role-model-trait memories under their own heading, e.g.:

```
People whose traits you want to adopt:
- Adnan Khan: purpose over comfort, high energy, fast self-correction
```

The system prompt gains one sentence: "When the context or the user's question
touches their career or personal-growth notes, and a role-model trait is
listed above, you may connect the two explicitly." This is additive to the
existing prompt in `docs/designs/...` (the current `SYSTEM_PROMPT` in
`chat_service.py`), not a rewrite of it.

No retrieval change is needed: this is prompt content, not a search feature —
it does not need its own embedding or its own retrieval pass, because the
existing document retrieval already finds the user's career notes for a career
question, and the trait is simply always present in the prompt once approved
(the existing memory mechanism, capped at `MAX_MEMORIES`).

### The offline boundary (the one deliberate exception)
Looking someone up in real time — "who is Adnan Khan?" — needs a network call
this project does not otherwise make. Per the README's offline/privacy-first
principle, this must be **opt-in per use**, not silently reachable by the
default tool loop:
- A new tool, `look_up_public_figure`, calling Wikidata's public API
  (no key, no account, structured data only — not a general web search), is
  registered **only if `settings.WEB_LOOKUP_ENABLED` is true**, default false,
  same pattern as `EMBEDDING_CACHE_ENABLED`/`SEARCH_HYBRID_ENABLED`.
- When used, the result (a short bio/description) is shown to the user before
  anything is saved — the same review step that already exists for proposed
  memories, so a network answer never gets stored as a fact silently.
- The README gains one line documenting the exception and the setting that
  guards it, so "offline" continues to describe what the running app actually
  does by default.
- Without the setting, the feature above works exactly as designed — the
  person types the traits they already know instead of asking the model to
  look them up first.

## What this does NOT do (kept out on purpose)
- No automatic "personality profile" scoring or comparison metric — the
  earlier "never invent numbers" project rule applies; this is qualitative
  by design.
- No change to retrieval/ranking. This is memory-prompt content only.
- No general web search tool. Wikidata only, structured, read-only, and only
  when explicitly enabled.

## Gates (each a separate step with its own approval, per this project's
milestone-gating rule)
1. Approve this design.
2. Migration + `Memory.kind`/`metadata_json` columns, `aibrain_test` first,
   with tests.
3. `remember` tool's optional `kind`/`person`/`traits` arguments + prompt
   formatting change, with tests.
4. (Optional, separate approval) `look_up_public_figure` tool behind
   `WEB_LOOKUP_ENABLED=false` by default, with tests, before it ever touches
   production.
