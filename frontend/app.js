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

let conversationId = localStorage.getItem(CONVERSATION_ID_KEY);
let importJobsPollTimer = null;

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
      item.textContent = `${citation.document_title} (${citation.document_source})`;
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

function showChatView() {
  chatViewEl.hidden = false;
  importJobsViewEl.hidden = true;
  navChatBtn.classList.add("active");
  navChatBtn.setAttribute("aria-pressed", "true");
  navImportJobsBtn.classList.remove("active");
  navImportJobsBtn.setAttribute("aria-pressed", "false");
  stopImportJobsPolling();
}

function showImportJobsView() {
  chatViewEl.hidden = true;
  importJobsViewEl.hidden = false;
  navImportJobsBtn.classList.add("active");
  navImportJobsBtn.setAttribute("aria-pressed", "true");
  navChatBtn.classList.remove("active");
  navChatBtn.setAttribute("aria-pressed", "false");
  fetchImportJobs();
}

navChatBtn.addEventListener("click", showChatView);
navImportJobsBtn.addEventListener("click", showImportJobsView);
refreshImportJobsBtn.addEventListener("click", fetchImportJobs);

showChatView();

if (conversationId) {
  loadConversation(conversationId);
} else {
  showEmptyState();
}
