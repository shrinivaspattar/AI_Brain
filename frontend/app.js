const CONVERSATION_ID_KEY = "ai_brain_conversation_id";

const messagesEl = document.getElementById("messages");
const composerEl = document.getElementById("composer");
const inputEl = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const newConversationBtn = document.getElementById("new-conversation-btn");

let conversationId = localStorage.getItem(CONVERSATION_ID_KEY);

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

if (conversationId) {
  loadConversation(conversationId);
} else {
  showEmptyState();
}
