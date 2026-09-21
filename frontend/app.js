// A document ingested from an original file (Chain 2) is stored as a working
// copy named "content.<ext>", which says nothing to the reader. When the
// citation carries the original file path, show that file's name and folder.
function citationFullPath(citation) {
  const occurrence = citation.source_occurrences && citation.source_occurrences[0];
  if (!occurrence) {
    return citation.document_source;
  }
  return occurrence.member_path
    ? `${occurrence.root_t7_path} :: ${occurrence.member_path}`
    : occurrence.root_t7_path;
}

function citationLabel(citation) {
  const occurrence = citation.source_occurrences && citation.source_occurrences[0];
  if (!occurrence) {
    return `${citation.document_title} (${citation.document_source})`;
  }
  const path = (occurrence.member_path || occurrence.root_t7_path).replace(/\\/g, "/");
  const parts = path.split("/").filter(Boolean);
  const name = parts[parts.length - 1] || path;
  const folder = parts.length > 1 ? parts[parts.length - 2] : "";
  return folder ? `${name} (in ${folder})` : name;
}

const CONVERSATION_ID_KEY = "ai_brain_conversation_id";
const IMPORT_JOBS_POLL_INTERVAL_MS = 5000;
const TERMINAL_IMPORT_STATUSES = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

const messagesEl = document.getElementById("messages");
const composerEl = document.getElementById("composer");
const inputEl = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const newConversationBtn = document.getElementById("new-conversation-btn");

const chatViewEl = document.getElementById("chat-view");
const importJobsViewEl = document.getElementById("import-jobs-view");
const navChatBtn = document.getElementById("nav-chat-btn");
const navImportJobsBtn = document.getElementById("nav-import-jobs-btn");
const refreshImportJobsBtn = document.getElementById("refresh-import-jobs-btn");
const importJobsListEl = document.getElementById("import-jobs-list");
const importJobsStatusLineEl = document.getElementById("import-jobs-status-line");

const memoryViewEl = document.getElementById("memory-view");
const navMemoryBtn = document.getElementById("nav-memory-btn");
const refreshMemoryBtn = document.getElementById("refresh-memory-btn");
const memoryListEl = document.getElementById("memory-list");
const memoryStatusLineEl = document.getElementById("memory-status-line");
const memoryFilterTabs = document.querySelectorAll("#memory-filter-tabs .filter-tab");

const dedupReviewViewEl = document.getElementById("dedup-review-view");
const navDedupReviewBtn = document.getElementById("nav-dedup-review-btn");
const refreshDedupReviewBtn = document.getElementById("refresh-dedup-review-btn");
const dedupReviewListEl = document.getElementById("dedup-review-list");
const dedupReviewStatusLineEl = document.getElementById("dedup-review-status-line");
const dedupReviewFilterTabs = document.querySelectorAll(
  "#dedup-review-filter-tabs .filter-tab"
);

const MEMORY_EMPTY_MESSAGES = {
  pending: "No pending memories to review.",
  approved: "No approved memories yet.",
  rejected: "No rejected memories.",
  "": "No memories yet.",
};
const DEDUP_REVIEW_EMPTY_MESSAGES = {
  pending: "No pending duplicate findings to review.",
  approved: "No approved findings yet.",
  rejected: "No rejected findings.",
  "": "No duplicate findings yet.",
};
const SOURCE_SNIPPET_MAX_LENGTH = 300;

let currentDedupReviewFilter = "pending";

let conversationId = localStorage.getItem(CONVERSATION_ID_KEY);
let importJobsPollTimer = null;
let currentMemoryFilter = "pending";

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function clearMessages() {
  messagesEl.innerHTML = "";
}

function showEmptyState() {
  clearMessages();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = "Ask AI_Brain something to get started.";
  messagesEl.appendChild(empty);
}

function removeEmptyState() {
  const empty = messagesEl.querySelector(".empty-state");
  if (empty) empty.remove();
}

function renderMessage({ role, content, citations }) {
  removeEmptyState();

  const bubble = document.createElement("div");
  bubble.className = `message ${role}`;
  bubble.textContent = content;

  if (citations && citations.length > 0) {
    const citationsEl = document.createElement("div");
    citationsEl.className = "citations";

    const label = document.createElement("div");
    label.textContent = "Sources:";
    citationsEl.appendChild(label);

    const list = document.createElement("ol");
    citations.forEach((citation) => {
      const item = document.createElement("li");
      item.textContent = citationLabel(citation);
      item.title = citationFullPath(citation);
      list.appendChild(item);
    });
    citationsEl.appendChild(list);

    bubble.appendChild(citationsEl);
  }

  messagesEl.appendChild(bubble);
  scrollToBottom();
  return bubble;
}

function renderPending() {
  removeEmptyState();
  const bubble = document.createElement("div");
  bubble.className = "message pending";
  bubble.textContent = "Thinking...";
  messagesEl.appendChild(bubble);
  scrollToBottom();
  return bubble;
}

function renderError(text) {
  removeEmptyState();
  const bubble = document.createElement("div");
  bubble.className = "message error";
  bubble.textContent = text;
  messagesEl.appendChild(bubble);
  scrollToBottom();
}

function setComposerBusy(busy) {
  inputEl.disabled = busy;
  sendBtn.disabled = busy;
}

async function loadConversation(id) {
  try {
    const response = await fetch(`/chat/${id}`);

    if (response.status === 404) {
      localStorage.removeItem(CONVERSATION_ID_KEY);
      conversationId = null;
      showEmptyState();
      return;
    }

    if (!response.ok) {
      throw new Error(`Unexpected status ${response.status}`);
    }

    const history = await response.json();

    if (history.length === 0) {
      showEmptyState();
      return;
    }

    clearMessages();
    history.forEach((message) => {
      renderMessage({
        role: message.role,
        content: message.content,
        citations: message.citations,
      });
    });
  } catch (err) {
    renderError(`Could not load conversation history: ${err.message}`);
  }
}

async function sendMessage(text) {
  renderMessage({ role: "user", content: text });
  const pendingBubble = renderPending();
  setComposerBusy(true);

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        conversation_id: conversationId,
      }),
    });

    const body = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }

    conversationId = body.conversation_id;
    localStorage.setItem(CONVERSATION_ID_KEY, conversationId);

    pendingBubble.remove();
    renderMessage({
      role: body.message.role,
      content: body.message.content,
      citations: body.message.citations,
    });
  } catch (err) {
    pendingBubble.remove();
    renderError(`Something went wrong: ${err.message}`);
  } finally {
    setComposerBusy(false);
    inputEl.focus();
  }
}

composerEl.addEventListener("submit", (event) => {
  event.preventDefault();

  const text = inputEl.value.trim();
  if (!text) return;

  inputEl.value = "";
  sendMessage(text);
});

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composerEl.requestSubmit();
  }
});

newConversationBtn.addEventListener("click", () => {
  localStorage.removeItem(CONVERSATION_ID_KEY);
  conversationId = null;
  showEmptyState();
  inputEl.focus();
});

// --- Import Jobs view ---------------------------------------------------
// Read-only monitoring over the existing GET /import-jobs endpoint. No
// job business logic (status transitions, progress calculation, etc.)
// lives here - every field shown is exactly what the API returned.

function formatTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function statusBadgeClass(status) {
  return `status-badge status-${String(status).toLowerCase()}`;
}

function addDetail(dl, label, value) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  dl.appendChild(dt);
  dl.appendChild(dd);
}

function renderImportJobCard(job) {
  const card = document.createElement("div");
  card.className = "import-job-card";

  const header = document.createElement("div");
  header.className = "import-job-card-header";

  const name = document.createElement("span");
  name.className = "import-job-name";
  name.textContent = job.name;

  const badge = document.createElement("span");
  badge.className = statusBadgeClass(job.status);
  badge.textContent = job.status;

  header.appendChild(name);
  header.appendChild(badge);
  card.appendChild(header);

  const progressTrack = document.createElement("div");
  progressTrack.className = "import-job-progress-track";
  const progressFill = document.createElement("div");
  progressFill.className = "import-job-progress-fill";
  const progress = Math.max(0, Math.min(100, Number(job.progress) || 0));
  progressFill.style.width = `${progress}%`;
  progressTrack.appendChild(progressFill);
  card.appendChild(progressTrack);

  const progressLabel = document.createElement("div");
  progressLabel.className = "import-job-progress-label";
  progressLabel.textContent =
    `${progress}% — ${job.files_processed} / ${job.files_discovered} files processed`;
  card.appendChild(progressLabel);

  const details = document.createElement("dl");
  details.className = "import-job-details";
  addDetail(details, "Source path", job.source_path);
  addDetail(details, "Source type", job.source_type);
  addDetail(details, "Created", formatTimestamp(job.created_at));
  addDetail(details, "Updated", formatTimestamp(job.updated_at));
  addDetail(details, "Started", formatTimestamp(job.started_at));
  addDetail(details, "Finished", formatTimestamp(job.finished_at));
  card.appendChild(details);

  if (job.error_message) {
    const error = document.createElement("div");
    error.className = "import-job-error";
    error.textContent = job.error_message;
    card.appendChild(error);
  }

  return card;
}

function renderImportJobs(jobs) {
  importJobsListEl.innerHTML = "";

  if (jobs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No import jobs yet.";
    importJobsListEl.appendChild(empty);
    return;
  }

  jobs.forEach((job) => importJobsListEl.appendChild(renderImportJobCard(job)));
}

function stopImportJobsPolling() {
  if (importJobsPollTimer !== null) {
    clearTimeout(importJobsPollTimer);
    importJobsPollTimer = null;
  }
}

async function fetchImportJobs() {
  stopImportJobsPolling();
  refreshImportJobsBtn.disabled = true;

  try {
    const response = await fetch("/import-jobs");

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const jobs = await response.json();
    renderImportJobs(jobs);

    const hasActiveJob = jobs.some(
      (job) => !TERMINAL_IMPORT_STATUSES.has(job.status)
    );
    importJobsStatusLineEl.textContent = hasActiveJob
      ? `Updated ${new Date().toLocaleTimeString()} — watching for changes...`
      : `Updated ${new Date().toLocaleTimeString()}`;

    // Only keep polling while this view is still the one on screen - a
    // fetch that resolves after the user switched back to Chat should
    // not silently keep polling in the background.
    if (hasActiveJob && !importJobsViewEl.hidden) {
      importJobsPollTimer = setTimeout(fetchImportJobs, IMPORT_JOBS_POLL_INTERVAL_MS);
    }
  } catch (err) {
    importJobsListEl.innerHTML = "";
    const error = document.createElement("div");
    error.className = "message error";
    error.textContent = `Could not load import jobs: ${err.message}`;
    importJobsListEl.appendChild(error);
    importJobsStatusLineEl.textContent = "";
  } finally {
    refreshImportJobsBtn.disabled = false;
  }
}

const VIEWS = {
  chat: { section: chatViewEl, navBtn: navChatBtn },
  importJobs: { section: importJobsViewEl, navBtn: navImportJobsBtn },
  memory: { section: memoryViewEl, navBtn: navMemoryBtn },
  dedupReview: { section: dedupReviewViewEl, navBtn: navDedupReviewBtn },
};

function showView(name) {
  Object.entries(VIEWS).forEach(([key, { section, navBtn }]) => {
    const isActive = key === name;
    section.hidden = !isActive;
    navBtn.classList.toggle("active", isActive);
    navBtn.setAttribute("aria-pressed", String(isActive));
  });
  // Only one view is ever on screen - anything that polls in the
  // background must stop the moment its view isn't the visible one.
  stopImportJobsPolling();
}

function showChatView() {
  showView("chat");
}

function showImportJobsView() {
  showView("importJobs");
  fetchImportJobs();
}

function showMemoryView() {
  showView("memory");
  fetchMemories(currentMemoryFilter);
}

function showDedupReviewView() {
  showView("dedupReview");
  fetchDedupReviews(currentDedupReviewFilter);
}

navChatBtn.addEventListener("click", showChatView);
navImportJobsBtn.addEventListener("click", showImportJobsView);
navMemoryBtn.addEventListener("click", showMemoryView);
navDedupReviewBtn.addEventListener("click", showDedupReviewView);
refreshImportJobsBtn.addEventListener("click", fetchImportJobs);

// --- Memory Review view --------------------------------------------------
// Human review queue for candidate memories (Memory.status == "pending"),
// proposed by the model via the `remember` tool. Reuses the existing
// GET /memory, POST /memory/{id}/approve, POST /memory/{id}/reject
// endpoints verbatim - no memory review/business logic lives here, only
// display and the explicit approve/reject action itself.

function renderMemoryEmptyState() {
  memoryListEl.innerHTML = "";
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent =
    MEMORY_EMPTY_MESSAGES[currentMemoryFilter] ?? "No memories found.";
  memoryListEl.appendChild(empty);
}

async function fetchSourceMessage(sourceConversationId, messageId) {
  const response = await fetch(`/chat/${sourceConversationId}`);

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const history = await response.json();
  const message = history.find((m) => m.id === messageId);

  if (!message) {
    throw new Error("source message no longer available");
  }

  return message;
}

function renderSourceSnippet(container, message) {
  container.innerHTML = "";

  const snippet = document.createElement("div");
  snippet.className = "memory-source-snippet";

  const roleLabel = document.createElement("div");
  roleLabel.textContent = `[${message.role}]`;
  snippet.appendChild(roleLabel);

  const text = document.createElement("div");
  text.textContent =
    message.content.length > SOURCE_SNIPPET_MAX_LENGTH
      ? `${message.content.slice(0, SOURCE_SNIPPET_MAX_LENGTH)}...`
      : message.content;
  snippet.appendChild(text);

  if (message.citations && message.citations.length > 0) {
    const citationsEl = document.createElement("div");
    citationsEl.className = "citations";

    const label = document.createElement("div");
    label.textContent = "Sources:";
    citationsEl.appendChild(label);

    const list = document.createElement("ol");
    message.citations.forEach((citation) => {
      const item = document.createElement("li");
      item.textContent = citationLabel(citation);
      item.title = citationFullPath(citation);
      list.appendChild(item);
    });
    citationsEl.appendChild(list);
    snippet.appendChild(citationsEl);
  }

  container.appendChild(snippet);
}

function buildProvenanceBlock(memory) {
  const provenance = document.createElement("div");
  provenance.className = "memory-provenance";

  if (!memory.conversation_id) {
    const ref = document.createElement("div");
    ref.textContent = "No provenance recorded (written directly).";
    provenance.appendChild(ref);
    return provenance;
  }

  const ref = document.createElement("div");
  ref.textContent = memory.message_id
    ? `From conversation ${memory.conversation_id}, message #${memory.message_id}`
    : `From conversation ${memory.conversation_id}`;
  provenance.appendChild(ref);

  if (!memory.message_id) {
    return provenance;
  }

  const snippetContainer = document.createElement("div");
  snippetContainer.hidden = true;

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "memory-provenance-toggle";
  toggle.textContent = "View source message";

  toggle.addEventListener("click", async () => {
    if (!snippetContainer.hidden) {
      snippetContainer.hidden = true;
      toggle.textContent = "View source message";
      return;
    }

    toggle.disabled = true;

    try {
      const sourceMessage = await fetchSourceMessage(
        memory.conversation_id,
        memory.message_id
      );
      renderSourceSnippet(snippetContainer, sourceMessage);
      toggle.textContent = "Hide source message";
    } catch (err) {
      snippetContainer.innerHTML = "";
      const errorEl = document.createElement("div");
      errorEl.className = "memory-review-error";
      errorEl.textContent = `Could not load source message: ${err.message}`;
      snippetContainer.appendChild(errorEl);
      toggle.textContent = "Hide source message";
    } finally {
      toggle.disabled = false;
      snippetContainer.hidden = false;
    }
  });

  provenance.appendChild(toggle);
  provenance.appendChild(snippetContainer);
  return provenance;
}

async function reviewMemory(memoryId, action, cardEl) {
  const buttons = cardEl.querySelectorAll(
    ".memory-approve-btn, .memory-reject-btn"
  );
  buttons.forEach((btn) => (btn.disabled = true));

  try {
    const response = await fetch(`/memory/${memoryId}/${action}`, {
      method: "POST",
    });
    const body = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }

    // A reviewed memory no longer belongs in the Pending list (the only
    // filter that ever shows these buttons) - remove it rather than
    // re-fetch the whole list.
    cardEl.remove();
    if (memoryListEl.children.length === 0) {
      renderMemoryEmptyState();
    }
  } catch (err) {
    buttons.forEach((btn) => (btn.disabled = false));

    let errorEl = cardEl.querySelector(".memory-review-error");
    if (!errorEl) {
      errorEl = document.createElement("div");
      errorEl.className = "memory-review-error";
      cardEl.appendChild(errorEl);
    }
    errorEl.textContent = `Could not ${action} memory: ${err.message}`;
  }
}

const CONFIRM_RESET_DELAY_MS = 4000;

// Explicit human confirmation for approve/reject, without a native
// browser confirm() dialog (jarring next to the rest of the app's UI,
// and not reliably scriptable by browser automation/testing tools).
// First click arms the button ("Confirm Approve?"); a second click
// within CONFIRM_RESET_DELAY_MS actually performs the action. Clicking
// anything else, or waiting past the delay, disarms it automatically.
function createConfirmableActionButton(label, className, onConfirm) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = className;
  btn.textContent = label;

  let armed = false;
  let resetTimer = null;

  function disarm() {
    armed = false;
    btn.textContent = label;
    if (resetTimer !== null) {
      clearTimeout(resetTimer);
      resetTimer = null;
    }
  }

  btn.addEventListener("click", () => {
    if (!armed) {
      armed = true;
      btn.textContent = `Confirm ${label}?`;
      resetTimer = setTimeout(disarm, CONFIRM_RESET_DELAY_MS);
      return;
    }

    disarm();
    onConfirm();
  });

  return btn;
}

function renderMemoryCard(memory) {
  const card = document.createElement("div");
  card.className = "memory-card";

  const header = document.createElement("div");
  header.className = "memory-card-header";

  const content = document.createElement("div");
  content.className = "memory-content";
  content.textContent = memory.content;

  const badge = document.createElement("span");
  badge.className = statusBadgeClass(memory.status);
  badge.textContent = memory.status;

  header.appendChild(content);
  header.appendChild(badge);
  card.appendChild(header);

  const meta = document.createElement("div");
  meta.className = "memory-meta";
  const confidenceText =
    memory.confidence === null || memory.confidence === undefined
      ? "not scored"
      : `${Math.round(memory.confidence * 100)}% confidence`;
  meta.textContent = `${confidenceText} — created ${formatTimestamp(memory.created_at)}`;
  card.appendChild(meta);

  card.appendChild(buildProvenanceBlock(memory));

  if (memory.status === "pending") {
    const actions = document.createElement("div");
    actions.className = "memory-actions";

    actions.appendChild(
      createConfirmableActionButton("Approve", "memory-approve-btn", () =>
        reviewMemory(memory.id, "approve", card)
      )
    );
    actions.appendChild(
      createConfirmableActionButton("Reject", "memory-reject-btn", () =>
        reviewMemory(memory.id, "reject", card)
      )
    );
    card.appendChild(actions);
  }

  return card;
}

function renderMemories(memories) {
  memoryListEl.innerHTML = "";

  if (memories.length === 0) {
    renderMemoryEmptyState();
    return;
  }

  memories.forEach((memory) => memoryListEl.appendChild(renderMemoryCard(memory)));
}

async function fetchMemories(filter) {
  refreshMemoryBtn.disabled = true;

  try {
    const url = filter ? `/memory?status=${encodeURIComponent(filter)}` : "/memory";
    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const memories = await response.json();
    renderMemories(memories);
    memoryStatusLineEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    memoryListEl.innerHTML = "";
    const error = document.createElement("div");
    error.className = "message error";
    error.textContent = `Could not load memories: ${err.message}`;
    memoryListEl.appendChild(error);
    memoryStatusLineEl.textContent = "";
  } finally {
    refreshMemoryBtn.disabled = false;
  }
}

memoryFilterTabs.forEach((tab) => {
  if (tab.dataset.status === currentMemoryFilter) {
    tab.classList.add("active");
  }

  tab.addEventListener("click", () => {
    currentMemoryFilter = tab.dataset.status;
    memoryFilterTabs.forEach((t) => t.classList.toggle("active", t === tab));
    fetchMemories(currentMemoryFilter);
  });
});

refreshMemoryBtn.addEventListener("click", () => fetchMemories(currentMemoryFilter));

// --- Dedup Review view -----------------------------------------------
// Human review queue for KRM duplicate findings (DuplicateReview rows).
// Reuses GET /dedup/reviews, POST /dedup/reviews/{id}/approve, and
// POST /dedup/reviews/{id}/reject verbatim - no dedup decision logic
// (canonical arbitration, confidence scoring) lives here, only display
// and the explicit approve/reject action itself. Reviewing a finding
// never touches a file: there is no delete/move/quarantine action
// anywhere in this view, and none exists anywhere in the backend either.

function dedupMatchTypeBadgeClass(matchType) {
  return `match-type-badge match-type-${matchType}`;
}

function buildDedupMemberRow(member) {
  const row = document.createElement("div");
  row.className = "dedup-member-row";

  const title = document.createElement("span");
  title.className = "dedup-member-title";
  title.textContent = member.document.title;
  row.appendChild(title);

  const source = document.createElement("span");
  source.className = "dedup-member-source";
  source.textContent = member.document.source;
  row.appendChild(source);

  if (member.role === "recommended_canonical") {
    const badge = document.createElement("span");
    badge.className = "dedup-role-badge";
    badge.textContent = "System-recommended canonical";
    row.appendChild(badge);
  }

  if (member.document.import_job_id !== null && member.document.import_job_id !== undefined) {
    const provenance = document.createElement("span");
    provenance.className = "dedup-member-provenance";
    provenance.textContent = `Import job #${member.document.import_job_id}`;
    row.appendChild(provenance);
  }

  return row;
}

function buildCanonicalChoiceForm(review) {
  const container = document.createElement("div");
  container.className = "dedup-canonical-choice";

  const label = document.createElement("div");
  label.className = "dedup-canonical-choice-label";
  label.textContent =
    review.match_type === "exact"
      ? "Select the canonical copy to keep (required to approve):"
      : "Optionally select a canonical copy, or confirm with none chosen:";
  container.appendChild(label);

  const radioName = `canonical-choice-${review.id}`;
  const radios = [];

  if (review.match_type === "near") {
    const noneOption = document.createElement("label");
    noneOption.className = "dedup-canonical-option";
    const noneRadio = document.createElement("input");
    noneRadio.type = "radio";
    noneRadio.name = radioName;
    noneRadio.value = "";
    noneRadio.checked = true;
    noneOption.appendChild(noneRadio);
    noneOption.appendChild(document.createTextNode(" No canonical (undecided)"));
    container.appendChild(noneOption);
    radios.push(noneRadio);
  }

  review.members.forEach((member) => {
    const option = document.createElement("label");
    option.className = "dedup-canonical-option";
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = radioName;
    radio.value = member.document_id;
    option.appendChild(radio);
    option.appendChild(document.createTextNode(` ${member.document.title}`));
    container.appendChild(option);
    radios.push(radio);
  });

  return { container, radios };
}

function renderDedupReviewCard(review) {
  const card = document.createElement("div");
  card.className = "dedup-review-card";

  const header = document.createElement("div");
  header.className = "dedup-review-card-header";

  const matchBadge = document.createElement("span");
  matchBadge.className = dedupMatchTypeBadgeClass(review.match_type);
  matchBadge.textContent =
    review.match_type === "exact" ? "EXACT DUPLICATE" : "NEAR DUPLICATE";
  header.appendChild(matchBadge);

  const statusBadgeEl = document.createElement("span");
  statusBadgeEl.className = statusBadgeClass(review.status);
  statusBadgeEl.textContent = review.status;
  header.appendChild(statusBadgeEl);

  card.appendChild(header);

  const reason = document.createElement("div");
  reason.className = "dedup-recommendation-reason";
  reason.textContent = review.recommendation_reason;
  card.appendChild(reason);

  const meta = document.createElement("div");
  meta.className = "dedup-review-meta";
  const confidenceText = `${Math.round(review.confidence * 100)}% confidence this is a genuine match`;
  const similarityText =
    review.similarity !== null && review.similarity !== undefined
      ? ` — ${Math.round(review.similarity * 100)}% content similarity (similarity is not equivalence)`
      : "";
  meta.textContent = `${confidenceText}${similarityText} — found ${formatTimestamp(review.created_at)}`;
  card.appendChild(meta);

  const canonicalSummary = document.createElement("dl");
  canonicalSummary.className = "dedup-canonical-summary";

  const recommendedDt = document.createElement("dt");
  recommendedDt.textContent = "System recommendation";
  const recommendedDd = document.createElement("dd");
  if (review.recommended_canonical_document_id) {
    const recommendedMember = review.members.find(
      (m) => m.document_id === review.recommended_canonical_document_id
    );
    recommendedDd.textContent = recommendedMember
      ? `${recommendedMember.document.title} — a suggestion only, not a decision`
      : "Recommended, but document details unavailable.";
  } else {
    recommendedDd.textContent =
      "None. Near-duplicates never get an automatic recommendation - a human must decide.";
  }
  canonicalSummary.appendChild(recommendedDt);
  canonicalSummary.appendChild(recommendedDd);

  const approvedDt = document.createElement("dt");
  approvedDt.textContent = "Human-approved canonical";
  const approvedDd = document.createElement("dd");
  if (review.human_selected_canonical_document_id) {
    const approvedMember = review.members.find(
      (m) => m.document_id === review.human_selected_canonical_document_id
    );
    approvedDd.textContent = approvedMember
      ? approvedMember.document.title
      : review.human_selected_canonical_document_id;
  } else {
    approvedDd.textContent =
      review.status === "pending" ? "Not yet decided." : "None chosen.";
  }
  canonicalSummary.appendChild(approvedDt);
  canonicalSummary.appendChild(approvedDd);
  card.appendChild(canonicalSummary);

  const membersSection = document.createElement("div");
  membersSection.className = "dedup-members-list";
  const membersLabel = document.createElement("div");
  membersLabel.className = "dedup-members-label";
  membersLabel.textContent = "Participating documents:";
  membersSection.appendChild(membersLabel);
  review.members.forEach((member) =>
    membersSection.appendChild(buildDedupMemberRow(member))
  );
  card.appendChild(membersSection);

  if (review.status !== "pending") {
    const decisionInfo = document.createElement("div");
    decisionInfo.className = "dedup-review-decision-info";
    decisionInfo.textContent =
      `Reviewed ${formatTimestamp(review.reviewed_at)}` +
      (review.reviewer_decision ? ` — "${review.reviewer_decision}"` : "");
    card.appendChild(decisionInfo);
  }

  if (review.status === "pending") {
    const { container: canonicalChoice, radios } = buildCanonicalChoiceForm(review);
    card.appendChild(canonicalChoice);

    const noteInput = document.createElement("textarea");
    noteInput.className = "dedup-reviewer-note";
    noteInput.placeholder = "Optional note explaining your decision...";
    noteInput.rows = 2;
    card.appendChild(noteInput);

    const actions = document.createElement("div");
    actions.className = "memory-actions";

    const approveBtn = createConfirmableActionButton(
      "Approve",
      "memory-approve-btn",
      () => {
        const selected = radios.find((radio) => radio.checked);
        const canonicalDocumentId = selected && selected.value ? selected.value : null;
        approveDedupReview(
          review.id,
          canonicalDocumentId,
          noteInput.value.trim() || null,
          card
        );
      }
    );

    if (review.match_type === "exact") {
      approveBtn.disabled = true;
      radios.forEach((radio) => {
        radio.addEventListener("change", () => {
          approveBtn.disabled = !radios.some((r) => r.checked && r.value);
        });
      });
    }

    const rejectBtn = createConfirmableActionButton(
      "Reject",
      "memory-reject-btn",
      () => {
        rejectDedupReview(review.id, noteInput.value.trim() || null, card);
      }
    );

    actions.appendChild(approveBtn);
    actions.appendChild(rejectBtn);
    card.appendChild(actions);
  }

  return card;
}

function renderDedupReviewEmptyState() {
  dedupReviewListEl.innerHTML = "";
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent =
    DEDUP_REVIEW_EMPTY_MESSAGES[currentDedupReviewFilter] ?? "No duplicate findings found.";
  dedupReviewListEl.appendChild(empty);
}

function renderDedupReviews(reviews) {
  dedupReviewListEl.innerHTML = "";

  if (reviews.length === 0) {
    renderDedupReviewEmptyState();
    return;
  }

  reviews.forEach((review) =>
    dedupReviewListEl.appendChild(renderDedupReviewCard(review))
  );
}

function showDedupReviewError(cardEl, message) {
  let errorEl = cardEl.querySelector(".memory-review-error");
  if (!errorEl) {
    errorEl = document.createElement("div");
    errorEl.className = "memory-review-error";
    cardEl.appendChild(errorEl);
  }
  errorEl.textContent = message;
}

async function approveDedupReview(reviewId, canonicalDocumentId, reviewerDecision, cardEl) {
  const buttons = cardEl.querySelectorAll(".memory-approve-btn, .memory-reject-btn");
  buttons.forEach((btn) => (btn.disabled = true));

  try {
    const response = await fetch(`/dedup/reviews/${reviewId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        canonical_document_id: canonicalDocumentId,
        reviewer_decision: reviewerDecision,
      }),
    });
    const body = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }

    cardEl.remove();
    if (dedupReviewListEl.children.length === 0) {
      renderDedupReviewEmptyState();
    }
  } catch (err) {
    buttons.forEach((btn) => (btn.disabled = false));
    showDedupReviewError(cardEl, `Could not approve finding: ${err.message}`);
  }
}

async function rejectDedupReview(reviewId, reviewerDecision, cardEl) {
  const buttons = cardEl.querySelectorAll(".memory-approve-btn, .memory-reject-btn");
  buttons.forEach((btn) => (btn.disabled = true));

  try {
    const response = await fetch(`/dedup/reviews/${reviewId}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer_decision: reviewerDecision }),
    });
    const body = await response.json().catch(() => null);

    if (!response.ok) {
      const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
      throw new Error(detail);
    }

    cardEl.remove();
    if (dedupReviewListEl.children.length === 0) {
      renderDedupReviewEmptyState();
    }
  } catch (err) {
    buttons.forEach((btn) => (btn.disabled = false));
    showDedupReviewError(cardEl, `Could not reject finding: ${err.message}`);
  }
}

async function fetchDedupReviews(filter) {
  refreshDedupReviewBtn.disabled = true;

  try {
    const url = filter
      ? `/dedup/reviews?status=${encodeURIComponent(filter)}`
      : "/dedup/reviews";
    const response = await fetch(url);

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const reviews = await response.json();
    renderDedupReviews(reviews);
    dedupReviewStatusLineEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    dedupReviewListEl.innerHTML = "";
    const error = document.createElement("div");
    error.className = "message error";
    error.textContent = `Could not load duplicate findings: ${err.message}`;
    dedupReviewListEl.appendChild(error);
    dedupReviewStatusLineEl.textContent = "";
  } finally {
    refreshDedupReviewBtn.disabled = false;
  }
}

dedupReviewFilterTabs.forEach((tab) => {
  if (tab.dataset.status === currentDedupReviewFilter) {
    tab.classList.add("active");
  }

  tab.addEventListener("click", () => {
    currentDedupReviewFilter = tab.dataset.status;
    dedupReviewFilterTabs.forEach((t) => t.classList.toggle("active", t === tab));
    fetchDedupReviews(currentDedupReviewFilter);
  });
});

refreshDedupReviewBtn.addEventListener("click", () =>
  fetchDedupReviews(currentDedupReviewFilter)
);

showChatView();

if (conversationId) {
  loadConversation(conversationId);
} else {
  showEmptyState();
}
