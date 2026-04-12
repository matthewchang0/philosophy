const SIDEBAR_STORAGE_KEY = "pantheon:sidebar-collapsed";
const FLASH_STORAGE_KEY = "pantheon:flash-message";

const state = {
  page: document.body.dataset.page || "home",
  conversationId: null,
  currentConversation: null,
  conversations: [],
  providers: [],
  user: null,
  googleAuthEnabled: false,
  maxParticipants: 5,
  participantCounter: 0,
  pollHandle: null,
  sidebarCollapsed: true,
  flashTimeout: null,
};

const els = {
  appLayout: document.getElementById("app-layout"),
  menuToggle: document.getElementById("menu-toggle"),
  drawerClose: document.getElementById("drawer-close"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  drawerScrim: document.getElementById("drawer-scrim"),
  historyDrawer: document.getElementById("history-drawer"),
  topLinks: document.getElementById("top-links"),
  sidebarAccountSlot: document.getElementById("sidebar-account-slot"),
  conversationCount: document.getElementById("conversation-count"),
  conversationList: document.getElementById("conversation-list"),
  requestForm: document.getElementById("request-form"),
  questionInput: document.getElementById("question-input"),
  roundsInput: document.getElementById("rounds-input"),
  dryRunInput: document.getElementById("dry-run-input"),
  submitButton: document.getElementById("submit-button"),
  providerPicker: document.getElementById("provider-picker"),
  participantsList: document.getElementById("participants-list"),
  participantTemplate: document.getElementById("participant-template"),
  summarizerSelect: document.getElementById("summarizer-select"),
  detailEmptyState: document.getElementById("detail-empty-state"),
  conversationView: document.getElementById("conversation-view"),
  conversationStatusLine: document.getElementById("conversation-status-line"),
  conversationTitle: document.getElementById("conversation-title"),
  statusPill: document.getElementById("status-pill"),
  configChips: document.getElementById("config-chips"),
  participantsStage: document.getElementById("participants-stage"),
  errorBanner: document.getElementById("error-banner"),
  transcriptStage: document.getElementById("transcript-stage"),
  summaryStage: document.getElementById("summary-stage"),
  resumeButton: document.getElementById("resume-button"),
  loginForm: document.getElementById("login-form"),
  loginEmail: document.getElementById("login-email"),
  loginPassword: document.getElementById("login-password"),
  loginError: document.getElementById("login-error"),
  loginSubmit: document.getElementById("login-submit"),
  signupForm: document.getElementById("signup-form"),
  signupName: document.getElementById("signup-name"),
  signupEmail: document.getElementById("signup-email"),
  signupPassword: document.getElementById("signup-password"),
  signupConfirmPassword: document.getElementById("signup-confirm-password"),
  signupTerms: document.getElementById("signup-terms"),
  signupCompany: document.getElementById("signup-company"),
  signupError: document.getElementById("signup-error"),
  signupSubmit: document.getElementById("signup-submit"),
  googleLoginButton: document.getElementById("google-login-button"),
  googleSignupButton: document.getElementById("google-signup-button"),
  accountContent: document.getElementById("account-content"),
  passwordForm: document.getElementById("password-form"),
  currentPassword: document.getElementById("current-password"),
  newPassword: document.getElementById("new-password"),
  confirmPassword: document.getElementById("confirm-password"),
  passwordError: document.getElementById("password-error"),
  passwordSubmit: document.getElementById("password-submit"),
};

function displayNameForUser(user) {
  const explicit = (user?.name || "").trim();
  if (explicit) {
    return explicit;
  }
  const email = (user?.email || "").trim();
  if (!email) {
    return "Pantheon";
  }
  return email
    .split("@")[0]
    .split(/[._-]+/)
    .filter(Boolean)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function avatarMarkup(user) {
  const name = displayNameForUser(user);
  const avatarUrl = (user?.avatarUrl || "").trim();
  if (avatarUrl) {
    return `<img src="${escapeHtml(avatarUrl)}" alt="${escapeHtml(name)}" class="account-avatar account-avatar-image" />`;
  }
  const initial = escapeHtml(name.slice(0, 1).toUpperCase() || "P");
  return `<div class="account-avatar">${initial}</div>`;
}

function storeFlashMessage(message) {
  sessionStorage.setItem(FLASH_STORAGE_KEY, JSON.stringify({ message }));
}

function consumeFlashMessage() {
  const params = new URLSearchParams(window.location.search);
  const queryMessage = params.get("success");
  if (queryMessage) {
    params.delete("success");
    const nextQuery = params.toString();
    const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ""}${window.location.hash || ""}`;
    window.history.replaceState({}, "", nextUrl);
    return queryMessage;
  }

  const raw = sessionStorage.getItem(FLASH_STORAGE_KEY);
  if (!raw) {
    return "";
  }
  sessionStorage.removeItem(FLASH_STORAGE_KEY);
  try {
    return JSON.parse(raw)?.message || "";
  } catch (_error) {
    return "";
  }
}

function showSuccessToast(message) {
  if (!message) {
    return;
  }
  let toast = document.getElementById("app-flash-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "app-flash-toast";
    toast.className = "flash-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.add("visible");
  toast.classList.remove("fading");
  if (state.flashTimeout) {
    window.clearTimeout(state.flashTimeout);
  }
  state.flashTimeout = window.setTimeout(() => {
    toast.classList.add("fading");
    toast.classList.remove("visible");
    window.setTimeout(() => {
      toast?.remove();
    }, 500);
  }, 5000);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderInlineMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  html = html.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
  html = html.replace(/(?<!_)_([^_]+)_(?!_)/g, "<em>$1</em>");
  html = html.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  return html;
}

function renderMarkdown(markdown) {
  if (!markdown || !markdown.trim()) {
    return "<p></p>";
  }

  const codeBlocks = [];
  let source = markdown.replace(/\r\n/g, "\n");
  source = source.replace(/```([a-zA-Z0-9_-]+)?\n([\s\S]*?)```/g, (_, language = "", code) => {
    const token = `@@CODEBLOCK${codeBlocks.length}@@`;
    const className = language ? ` class="language-${escapeHtml(language)}"` : "";
    codeBlocks.push(`<pre><code${className}>${escapeHtml(code.trimEnd())}</code></pre>`);
    return token;
  });

  const blocks = source.split(/\n{2,}/);
  const rendered = blocks.map((block) => {
    const trimmed = block.trim();
    if (!trimmed) {
      return "";
    }
    if (trimmed.startsWith("@@CODEBLOCK")) {
      return trimmed;
    }
    if (/^#{1,6}\s/.test(trimmed)) {
      const level = trimmed.match(/^#+/)[0].length;
      const content = trimmed.replace(/^#{1,6}\s*/, "");
      return `<h${level}>${renderInlineMarkdown(content)}</h${level}>`;
    }
    if (trimmed.startsWith(">")) {
      const content = trimmed
        .split("\n")
        .map((line) => line.replace(/^>\s?/, ""))
        .join("<br>");
      return `<blockquote>${renderInlineMarkdown(content)}</blockquote>`;
    }
    const lines = trimmed.split("\n").filter((line) => line.trim());
    if (lines.every((line) => /^[-*]\s+/.test(line))) {
      return `<ul>${lines.map((line) => `<li>${renderInlineMarkdown(line.replace(/^[-*]\s+/, ""))}</li>`).join("")}</ul>`;
    }
    if (lines.every((line) => /^\d+\.\s+/.test(line))) {
      return `<ol>${lines.map((line) => `<li>${renderInlineMarkdown(line.replace(/^\d+\.\s+/, ""))}</li>`).join("")}</ol>`;
    }
    return `<p>${trimmed.split("\n").map((line) => renderInlineMarkdown(line)).join("<br>")}</p>`;
  });

  let html = rendered.join("\n");
  codeBlocks.forEach((block, index) => {
    html = html.replace(`@@CODEBLOCK${index}@@`, block);
  });
  return html;
}

function timeLabel(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function conversationUrl(id) {
  return `/conversations/${encodeURIComponent(id)}`;
}

function currentConversationIdFromPath() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] === "conversations" && parts[1]) {
    return decodeURIComponent(parts[1]);
  }
  return null;
}

function storageKeyForRun(runId) {
  return `pantheon:run:${runId}`;
}

function fetchJson(url, options = {}) {
  return fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    credentials: "same-origin",
    ...options,
  }).then(async (response) => {
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Request failed.");
    }
    return data;
  });
}

function isMobileViewport() {
  return window.matchMedia("(max-width: 980px)").matches;
}

function getStoredSidebarState() {
  if (state.page === "home") {
    return true;
  }
  const stored = localStorage.getItem(SIDEBAR_STORAGE_KEY);
  if (stored === null) {
    return true;
  }
  return stored === "true";
}

function setSidebarCollapsed(collapsed) {
  state.sidebarCollapsed = Boolean(collapsed);
  localStorage.setItem(SIDEBAR_STORAGE_KEY, String(state.sidebarCollapsed));
  applySidebarState();
}

function applySidebarState() {
  if (!els.appLayout) {
    return;
  }
  const collapsed = !isMobileViewport() && state.sidebarCollapsed;
  els.appLayout.classList.toggle("sidebar-collapsed", collapsed);
  if (els.sidebarToggle) {
    els.sidebarToggle.textContent = collapsed ? "☰" : "‹";
    els.sidebarToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  }
  if (els.menuToggle) {
    els.menuToggle.setAttribute("aria-label", collapsed ? "Open sidebar" : "Collapse sidebar");
  }
}

function setDrawerOpen(isOpen) {
  if (!els.historyDrawer || !els.menuToggle || !els.drawerScrim) {
    return;
  }
  if (!isMobileViewport()) {
    setSidebarCollapsed(!isOpen);
    return;
  }
  els.historyDrawer.classList.toggle("open", isOpen);
  els.drawerScrim.classList.toggle("hidden", !isOpen);
  els.menuToggle.setAttribute("aria-expanded", String(isOpen));
  els.historyDrawer.setAttribute("aria-hidden", String(!isOpen));
}

function bindShell() {
  state.sidebarCollapsed = getStoredSidebarState();
  applySidebarState();

  els.sidebarToggle?.addEventListener("click", () => {
    setSidebarCollapsed(!state.sidebarCollapsed);
  });

  els.menuToggle?.addEventListener("click", () => {
    if (isMobileViewport()) {
      setDrawerOpen(!els.historyDrawer?.classList.contains("open"));
      return;
    }
    setSidebarCollapsed(!state.sidebarCollapsed);
  });
  els.drawerClose?.addEventListener("click", () => setDrawerOpen(false));
  els.drawerScrim?.addEventListener("click", () => setDrawerOpen(false));

  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!isMobileViewport()) {
        setSidebarCollapsed(true);
        return;
      }
      setDrawerOpen(false);
    }
  });

  window.addEventListener("resize", () => {
    if (!isMobileViewport()) {
      els.historyDrawer?.classList.remove("open");
      els.drawerScrim?.classList.add("hidden");
      els.menuToggle?.setAttribute("aria-expanded", "false");
      els.historyDrawer?.setAttribute("aria-hidden", "true");
    }
    applySidebarState();
  });
}

function getProviderCatalog(providerId) {
  return state.providers.find((provider) => provider.id === providerId);
}

function providerLabel(providerId) {
  return getProviderCatalog(providerId)?.label || providerId;
}

function providerShortName(providerId) {
  return {
    openai: "ChatGPT",
    anthropic: "Claude",
    gemini: "Gemini",
    xai: "Grok",
  }[providerId] || providerId;
}

function providerIcon(providerId, className = "provider-icon") {
  const label = providerShortName(providerId);
  return `<img src="/providers/${escapeHtml(providerId)}.svg" alt="${escapeHtml(label)} logo" class="${escapeHtml(className)}" />`;
}

function participantNodes() {
  return [...(els.participantsList?.querySelectorAll("[data-participant-id]") || [])];
}

function renderTopLinks() {
  if (!els.topLinks) {
    return;
  }
  const links = [];

  if (state.user) {
    if (state.page === "conversation") {
      links.push(`<a href="/">New conversation</a>`);
    } else if (state.page === "privacy") {
      links.push(`<a href="/">Home</a>`);
    }
    links.push(`<a href="/account">Account</a>`);
    links.push(`<a href="/privacy">Privacy Policy</a>`);
    links.push(`<button class="top-link-button" type="button" data-action="logout">Log out</button>`);
  } else if (state.page === "login") {
    links.push(`<a href="/signup">Sign up</a>`);
    links.push(`<a href="/privacy">Privacy Policy</a>`);
  } else if (state.page === "signup") {
    links.push(`<a href="/login">Log in</a>`);
    links.push(`<a href="/privacy">Privacy Policy</a>`);
  } else {
    links.push(`<a href="/login">Log in</a>`);
    links.push(`<a href="/signup">Sign up</a>`);
    links.push(`<a href="/privacy">Privacy Policy</a>`);
  }

  els.topLinks.innerHTML = links.join("");
  els.topLinks.querySelector('[data-action="logout"]')?.addEventListener("click", logout);
}

function renderSidebarAccount() {
  if (!els.sidebarAccountSlot) {
    return;
  }
  if (state.user) {
    els.sidebarAccountSlot.innerHTML = `
      <a href="/account" class="sidebar-account sidebar-account-minimal sidebar-account-flat sidebar-account-profile">
        ${avatarMarkup(state.user)}
        <div class="sidebar-account-copy sidebar-account-copy-minimal">
          <strong>${escapeHtml(displayNameForUser(state.user))}</strong>
        </div>
      </a>
    `;
  } else {
    els.sidebarAccountSlot.innerHTML = "";
  }
}

function renderConversationList() {
  if (!els.conversationList || !els.conversationCount) {
    return;
  }
  els.conversationCount.textContent = String(state.conversations.length);
  if (!state.conversations.length) {
    els.conversationList.innerHTML = `<p class="conversation-meta">No saved threads.</p>`;
    return;
  }
  els.conversationList.innerHTML = state.conversations
    .map((conversation) => {
      const activeClass = conversation.id === state.conversationId ? "active" : "";
      return `
        <a class="conversation-item ${activeClass}" href="${conversationUrl(conversation.id)}">
          <p class="conversation-title">${escapeHtml(conversation.title)}</p>
          <div class="conversation-meta-row">
            <span>${escapeHtml(conversation.status)}</span>
            <span>${escapeHtml(timeLabel(conversation.updatedAt))}</span>
          </div>
        </a>
      `;
    })
    .join("");
}

async function loadCurrentUser() {
  const data = await fetchJson("/api/auth/me");
  state.user = data.user || null;
  state.googleAuthEnabled = Boolean(data.googleAuthEnabled);
  renderTopLinks();
  renderSidebarAccount();
  renderGoogleAuthButtons();
}

async function logout() {
  try {
    await fetchJson("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    state.user = null;
    renderTopLinks();
    renderSidebarAccount();
    if (state.page === "account") {
      window.location.assign("/login");
      return;
    }
    await loadConversationList();
    if (state.page === "conversation" && state.conversationId) {
      try {
        const conversation = await fetchJson(`/api/conversations/${state.conversationId}`);
        renderConversation(conversation);
      } catch (_error) {
        window.location.assign("/");
      }
    }
  } catch (error) {
    window.alert(error.message);
  }
}

async function loadAccountDetails() {
  return fetchJson("/api/account");
}

function participantDisplayLabel(node) {
  const providerId = node.dataset.providerId;
  const model = node.querySelector(".participant-model-select")?.value || "";
  return `${providerShortName(providerId)}${model ? ` · ${model}` : ""}`;
}

function updateSummarizerOptions() {
  if (!els.summarizerSelect) {
    return;
  }
  const existingValue = els.summarizerSelect.value;
  const participants = participantNodes().map((node) => ({
    id: node.dataset.participantId,
    label: participantDisplayLabel(node),
  }));
  els.summarizerSelect.innerHTML = participants
    .map((participant) => `<option value="${escapeHtml(participant.id)}">${escapeHtml(participant.label)}</option>`)
    .join("");
  if (participants.find((item) => item.id === existingValue)) {
    els.summarizerSelect.value = existingValue;
  } else if (participants.length) {
    els.summarizerSelect.value = participants[participants.length - 1].id;
  }
}

function removeParticipantCard(node) {
  node.remove();
  updateSummarizerOptions();
  renderProviderPicker();
}

function renderProviderPicker() {
  if (!els.providerPicker) {
    return;
  }
  const selectedCount = participantNodes().length;
  const disabled = selectedCount >= state.maxParticipants;
  els.providerPicker.innerHTML = state.providers
    .map((provider) => `
      <button class="provider-tile ${provider.id}" type="button" data-provider-id="${escapeHtml(provider.id)}" ${disabled ? "disabled" : ""}>
        ${providerIcon(provider.id, "provider-icon provider-icon-large")}
        <strong>${escapeHtml(provider.label)}</strong>
        <span>${escapeHtml((provider.suggestedModels || []).join(" · "))}</span>
      </button>
    `)
    .join("");

  els.providerPicker.querySelectorAll("[data-provider-id]").forEach((button) => {
    button.addEventListener("click", () => addParticipantCard({ provider: button.dataset.providerId }));
  });
}

function bindParticipantCard(node) {
  const providerId = node.dataset.providerId;
  const provider = getProviderCatalog(providerId);
  const nameHeading = node.querySelector(".participant-name");
  const readonlyProvider = node.querySelector(".participant-provider-readonly");
  const modelSelect = node.querySelector(".participant-model-select");
  const tokensInput = node.querySelector(".participant-max-tokens");
  const reasoningWrap = node.querySelector(".participant-reasoning-wrap");
  const reasoningSelect = node.querySelector(".participant-reasoning");
  const removeButton = node.querySelector(".remove-participant");
  const badge = node.querySelector(".provider-badge-icon");

  badge.innerHTML = providerIcon(providerId, "provider-icon provider-icon-small");
  readonlyProvider.innerHTML = `<span class="provider-inline-badge ${providerId}">${escapeHtml(provider.label)}</span>`;
  modelSelect.innerHTML = (provider?.suggestedModels || [])
    .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`)
    .join("");
  if (!modelSelect.value) {
    modelSelect.value = provider?.defaultModel || "";
  }
  if (!tokensInput.value) {
    tokensInput.value = String(provider?.defaultMaxOutputTokens || 1600);
  }

  const isOpenAI = providerId === "openai";
  reasoningWrap.classList.toggle("hidden", !isOpenAI);
  if (!isOpenAI) {
    reasoningSelect.value = "none";
  }

  function refreshLabel() {
    nameHeading.textContent = participantDisplayLabel(node);
    updateSummarizerOptions();
  }

  modelSelect.addEventListener("change", refreshLabel);
  removeButton.addEventListener("click", () => removeParticipantCard(node));
  refreshLabel();
}

function addParticipantCard(initial = {}) {
  if (!els.participantsList || !els.participantTemplate) {
    return;
  }
  if (participantNodes().length >= state.maxParticipants) {
    return;
  }
  state.participantCounter += 1;
  const providerId = initial.provider || "openai";
  const participantId = `participant-${state.participantCounter}`;
  const fragment = els.participantTemplate.content.cloneNode(true);
  const card = fragment.querySelector(".participant-card");
  card.dataset.participantId = participantId;
  card.dataset.providerId = providerId;
  els.participantsList.appendChild(fragment);
  const node = els.participantsList.lastElementChild;
  bindParticipantCard(node);

  const provider = getProviderCatalog(providerId);
  const modelSelect = node.querySelector(".participant-model-select");
  const tokensInput = node.querySelector(".participant-max-tokens");
  const apiKeyInput = node.querySelector(".participant-api-key");
  const reasoningSelect = node.querySelector(".participant-reasoning");

  if (initial.model && (provider?.suggestedModels || []).includes(initial.model)) {
    modelSelect.value = initial.model;
  }
  if (initial.maxOutputTokens) {
    tokensInput.value = String(initial.maxOutputTokens);
  } else {
    tokensInput.value = String(provider?.defaultMaxOutputTokens || 1600);
  }
  if (initial.apiKey) {
    apiKeyInput.value = initial.apiKey;
  }
  if (initial.reasoning) {
    reasoningSelect.value = initial.reasoning;
  }
  node.querySelector(".participant-name").textContent = participantDisplayLabel(node);
  updateSummarizerOptions();
  renderProviderPicker();
}

function defaultParticipants() {
  return [
    {
      provider: "openai",
      model: getProviderCatalog("openai")?.defaultModel || "gpt-5.4",
      maxOutputTokens: getProviderCatalog("openai")?.defaultMaxOutputTokens || 4000,
      reasoning: "none",
    },
    {
      provider: "anthropic",
      model: getProviderCatalog("anthropic")?.defaultModel || "claude-opus-4-6",
      maxOutputTokens: getProviderCatalog("anthropic")?.defaultMaxOutputTokens || 1600,
      reasoning: "none",
    },
  ];
}

function collectHomePayload() {
  const question = els.questionInput?.value.trim() || "";
  const rounds = Number(els.roundsInput?.value || 3);
  const participants = participantNodes().map((node) => {
    const providerId = node.dataset.providerId;
    const model = node.querySelector(".participant-model-select")?.value || "";
    const label = `${providerShortName(providerId)}${model ? ` (${model})` : ""}`;
    return {
      participant_id: node.dataset.participantId,
      label,
      provider: providerId,
      model,
      max_output_tokens: Number(node.querySelector(".participant-max-tokens")?.value || 1600),
      reasoning: node.querySelector(".participant-reasoning")?.value || "none",
      api_key: node.querySelector(".participant-api-key")?.value.trim() || "",
    };
  });

  const dryRun = Boolean(els.dryRunInput?.checked);
  return {
    question,
    rounds,
    participants,
    summarizerId: els.summarizerSelect?.value || participants[participants.length - 1]?.participant_id || "",
    dry_run: dryRun,
    dryRun,
  };
}

function storeRunKeys(runId, participants) {
  const securePayload = participants.map((participant) => ({
    participant_id: participant.participant_id,
    api_key: participant.api_key,
  }));
  sessionStorage.setItem(storageKeyForRun(runId), JSON.stringify(securePayload));
}

function buildResumePayload(conversation) {
  const stored = sessionStorage.getItem(storageKeyForRun(conversation.id));
  let storedParticipants = [];
  if (stored) {
    try {
      storedParticipants = JSON.parse(stored);
    } catch (_error) {
      storedParticipants = [];
    }
  }
  const keysById = new Map(storedParticipants.map((item) => [item.participant_id, item.api_key]));

  const participants = (conversation.config.participants || []).map((participant) => {
    let apiKey = keysById.get(participant.participantId) || "";
    if (!conversation.config.dryRun && !apiKey) {
      apiKey = window.prompt(`Enter the API key for ${participant.label}.`, "") || "";
    }
    return {
      participant_id: participant.participantId,
      api_key: apiKey,
    };
  });

  return { participants };
}

function renderConfigChips(config) {
  if (!els.configChips) {
    return;
  }
  const chips = [
    config.rounds ? `Rounds: ${config.rounds}` : "",
    config.dryRun ? "Dry run" : "",
    config.summarizerLabel ? `Final summary: ${config.summarizerLabel}` : "",
  ].filter(Boolean);
  els.configChips.innerHTML = chips.map((chip) => `<span class="config-chip">${escapeHtml(chip)}</span>`).join("");
}

function renderParticipantsStage(participants, summarizerId) {
  if (!els.participantsStage) {
    return;
  }
  els.participantsStage.innerHTML = (participants || [])
    .map((participant) => `
      <article class="participant-pill ${participant.participantId === summarizerId ? "summarizer" : ""}">
        ${providerIcon(participant.provider, "provider-icon provider-icon-pill")}
        <div>
          <p>${escapeHtml(participant.label)}</p>
          <span>${escapeHtml(participant.model)}</span>
        </div>
      </article>
    `)
    .join("");
}

function renderTurnCard(turn) {
  const citations = (turn.citations || [])
    .slice(0, 6)
    .map((citation) => `<li><a href="${escapeHtml(citation.url)}" target="_blank" rel="noreferrer">${escapeHtml(citation.title || citation.url)}</a></li>`)
    .join("");

  return `
    <article class="turn-card turn-card-feed provider-${escapeHtml(turn.provider)}">
      <div class="turn-feed-top">
        <div class="turn-feed-id">
          ${providerIcon(turn.provider, "provider-icon provider-icon-pill")}
          <div>
            <p class="turn-title">${escapeHtml(turn.speakerLabel)}</p>
            <p class="turn-subtitle">${escapeHtml(providerLabel(turn.provider))} · ${escapeHtml(turn.model)}</p>
          </div>
        </div>
        <span class="turn-chip">Round ${escapeHtml(turn.roundNumber)}</span>
      </div>
      <div class="turn-body">
        <div class="markdown-body">${renderMarkdown(turn.responseText)}</div>
        ${citations ? `<div class="citations"><p class="citations-title">Sources</p><ol>${citations}</ol></div>` : ""}
      </div>
    </article>
  `;
}

function splitSummarySections(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const sections = [];
  let currentHeading = null;
  let currentLines = [];

  function pushSection() {
    if (!currentHeading) {
      return;
    }
    sections.push({
      heading: currentHeading,
      markdown: currentLines.join("\n").trim(),
    });
  }

  for (const line of lines) {
    if (/^##\s+/.test(line)) {
      pushSection();
      currentHeading = line.replace(/^##\s+/, "").trim();
      currentLines = [];
      continue;
    }
    currentLines.push(line);
  }
  pushSection();
  return sections;
}

function summaryTone(heading) {
  const normalized = heading.toLowerCase();
  if (normalized.includes("snapshot")) {
    return "snapshot";
  }
  if (normalized.includes("agreed")) {
    return "agreement";
  }
  if (normalized.includes("disagreed")) {
    return "disagreement";
  }
  if (normalized.includes("best answer")) {
    return "answer";
  }
  return "default";
}

function renderSummary(conversation) {
  if (!els.summaryStage) {
    return;
  }
  const markdown = conversation.summaryMarkdown || "";
  if (!markdown.trim()) {
    els.summaryStage.classList.add("hidden");
    els.summaryStage.innerHTML = "";
    return;
  }
  const sections = splitSummarySections(markdown);
  els.summaryStage.classList.remove("hidden");
  els.summaryStage.innerHTML = `
    <div class="summary-shell summary-shell-single">
      <div class="summary-head summary-head-centered">
        <div>
          <p class="eyebrow">Final synthesis</p>
          <h2>${escapeHtml(conversation.config.summarizerLabel || "Summary")}</h2>
        </div>
        <span class="summary-badge">Closest answer right now</span>
      </div>
      <div class="summary-stack">
        ${sections
          .map((section) => `
            <article class="summary-card ${summaryTone(section.heading)}">
              <h3>${escapeHtml(section.heading)}</h3>
              <div class="markdown-body">${renderMarkdown(section.markdown)}</div>
            </article>
          `)
          .join("")}
      </div>
    </div>
  `;
}

function renderConversation(conversation) {
  if (!els.conversationView || !els.detailEmptyState) {
    return;
  }
  if (!conversation) {
    els.detailEmptyState.classList.remove("hidden");
    els.conversationView.classList.add("hidden");
    document.title = "Pantheon Conversation";
    return;
  }

  els.detailEmptyState.classList.add("hidden");
  els.conversationView.classList.remove("hidden");
  state.currentConversation = conversation;
  state.conversationId = conversation.id;
  document.title = `${conversation.title} · Pantheon`;

  els.conversationStatusLine.textContent = `${conversation.status} · ${timeLabel(conversation.updatedAt)}`;
  els.conversationTitle.textContent = conversation.question;
  els.statusPill.textContent = conversation.status;
  els.statusPill.className = `status-pill ${conversation.status}`;

  renderConfigChips(conversation.config);
  renderParticipantsStage(conversation.config.participants, conversation.config.summarizerId);

  if (conversation.error) {
    els.errorBanner.classList.remove("hidden");
    els.errorBanner.textContent = conversation.error;
  } else {
    els.errorBanner.classList.add("hidden");
    els.errorBanner.textContent = "";
  }

  els.transcriptStage.innerHTML = (conversation.turns || []).length
    ? (conversation.turns || []).map((turn) => renderTurnCard(turn)).join("")
    : `
      <section class="round-card">
        <div class="empty-card">
          <p class="turn-title">The models are getting started.</p>
          <p class="status-hint">Messages will appear here in order as each model responds.</p>
        </div>
      </section>
    `;

  renderSummary(conversation);

  if (conversation.status === "failed" || conversation.status === "interrupted") {
    els.resumeButton?.classList.remove("hidden");
  } else {
    els.resumeButton?.classList.add("hidden");
  }
}

async function loadConversationList() {
  if (!els.conversationList) {
    return;
  }
  const data = await fetchJson("/api/conversations");
  state.conversations = data.conversations || [];
  renderConversationList();
}

async function loadProviders() {
  const data = await fetchJson("/api/models");
  state.providers = data.providers || [];
  state.maxParticipants = data.maxParticipants || 5;
}

function managePolling(conversation) {
  if (state.pollHandle) {
    clearInterval(state.pollHandle);
    state.pollHandle = null;
  }
  if (!conversation || (conversation.status !== "running" && conversation.status !== "queued")) {
    return;
  }
  state.pollHandle = setInterval(async () => {
    try {
      await loadConversationList();
      if (!state.conversationId) {
        return;
      }
      const refreshed = await fetchJson(`/api/conversations/${state.conversationId}`);
      renderConversation(refreshed);
      if (refreshed.status !== "running" && refreshed.status !== "queued") {
        managePolling(refreshed);
      }
    } catch (error) {
      console.error(error);
    }
  }, 2000);
}

async function createConversation(event) {
  event.preventDefault();
  const payload = collectHomePayload();
  if (!payload.question) {
    els.questionInput?.focus();
    return;
  }
  if (!payload.participants.length) {
    window.alert("Add at least one model.");
    return;
  }
  if (payload.participants.length > state.maxParticipants) {
    window.alert(`You can add at most ${state.maxParticipants} models.`);
    return;
  }
  for (const participant of payload.participants) {
    if (!payload.dry_run && !participant.api_key) {
      window.alert(`Add an API key for ${participant.label}.`);
      return;
    }
    if (!participant.model) {
      window.alert("Each selected model needs a concrete model version.");
      return;
    }
  }

  els.submitButton.disabled = true;
  els.submitButton.textContent = "Starting...";
  try {
    const conversation = await fetchJson("/api/conversations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    storeRunKeys(conversation.id, payload.participants);
    window.location.assign(conversationUrl(conversation.id));
  } catch (error) {
    window.alert(error.message);
  } finally {
    els.submitButton.disabled = false;
    els.submitButton.textContent = "Start conversation";
  }
}

async function resumeConversation() {
  if (!state.currentConversation) {
    return;
  }
  try {
    const payload = buildResumePayload(state.currentConversation);
    const conversation = await fetchJson(`/api/conversations/${state.currentConversation.id}/resume`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    sessionStorage.setItem(storageKeyForRun(state.currentConversation.id), JSON.stringify(payload.participants));
    await loadConversationList();
    renderConversation(conversation);
    managePolling(conversation);
  } catch (error) {
    window.alert(error.message);
  }
}

function showFormError(node, message) {
  if (!node) {
    return;
  }
  if (!message) {
    node.classList.add("hidden");
    node.textContent = "";
    return;
  }
  node.classList.remove("hidden");
  node.textContent = message;
}

function authPageMessageFromQuery() {
  const params = new URLSearchParams(window.location.search);
  return params.get("error") || "";
}

function renderGoogleAuthButtons() {
  [els.googleLoginButton, els.googleSignupButton].forEach((button) => {
    if (!button) {
      return;
    }
    if (state.googleAuthEnabled) {
      button.classList.remove("disabled");
      button.removeAttribute("aria-disabled");
      button.setAttribute("href", "/auth/google/start");
      return;
    }
    button.classList.add("disabled");
    button.setAttribute("aria-disabled", "true");
    button.setAttribute("href", "/signup?error=Google+sign-in+is+not+configured+yet.");
  });
}

async function handleLogin(event) {
  event.preventDefault();
  showFormError(els.loginError, "");
  els.loginSubmit.disabled = true;
  els.loginSubmit.textContent = "Logging in...";
  try {
    await fetchJson("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: els.loginEmail?.value.trim() || "",
        password: els.loginPassword?.value || "",
      }),
    });
    storeFlashMessage("You've successfully logged in.");
    window.location.assign("/account");
  } catch (error) {
    showFormError(els.loginError, error.message);
  } finally {
    els.loginSubmit.disabled = false;
    els.loginSubmit.textContent = "Log in";
  }
}

async function handleSignup(event) {
  event.preventDefault();
  showFormError(els.signupError, "");
  if ((els.signupCompany?.value || "").trim()) {
    showFormError(els.signupError, "Sign up could not be completed.");
    return;
  }
  if ((els.signupPassword?.value || "") !== (els.signupConfirmPassword?.value || "")) {
    showFormError(els.signupError, "Passwords do not match.");
    return;
  }
  if (!els.signupTerms?.checked) {
    showFormError(els.signupError, "Please agree before creating an account.");
    return;
  }
  els.signupSubmit.disabled = true;
  els.signupSubmit.textContent = "Creating...";
  try {
    await fetchJson("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({
        name: els.signupName?.value.trim() || "",
        email: els.signupEmail?.value.trim() || "",
        password: els.signupPassword?.value || "",
        confirm_password: els.signupConfirmPassword?.value || "",
        company: els.signupCompany?.value || "",
      }),
    });
    storeFlashMessage("You've successfully signed up.");
    window.location.assign("/account");
  } catch (error) {
    showFormError(els.signupError, error.message);
  } finally {
    els.signupSubmit.disabled = false;
    els.signupSubmit.textContent = "Sign up";
  }
}

function renderAccountPage() {
  if (!els.accountContent) {
    return;
  }
  if (!state.user) {
    els.passwordForm?.classList.add("hidden");
    els.accountContent.innerHTML = `
      <p class="auth-copy">You are not signed in yet.</p>
      <div class="account-actions">
        <a class="primary-button primary-button-large" href="/login">Log in</a>
        <a class="secondary-button" href="/signup">Sign up</a>
      </div>
    `;
    return;
  }

  const isGoogleUser = state.user.authProvider === "google";
  els.passwordForm?.classList.toggle("hidden", isGoogleUser);

  els.accountContent.innerHTML = `
    <div class="account-row">
      <span class="account-label">Name</span>
      <strong>${escapeHtml(displayNameForUser(state.user))}</strong>
    </div>
    <div class="account-row">
      <span class="account-label">Email</span>
      <strong>${escapeHtml(state.user.email)}</strong>
    </div>
    <div class="account-row">
      <span class="account-label">Last activity</span>
      <strong id="account-latest-run">-</strong>
    </div>
    ${isGoogleUser ? `
      <div class="account-row">
        <span class="account-label">Password</span>
        <strong>Managed by Google</strong>
      </div>
    ` : ""}
  `;
}

function renderAccountStats(stats) {
  const latestRun = document.getElementById("account-latest-run");
  if (latestRun) {
    latestRun.textContent = stats.latestRunAt ? timeLabel(stats.latestRunAt) : "No activity yet";
  }
}

async function handlePasswordChange(event) {
  event.preventDefault();
  showFormError(els.passwordError, "");
  if ((els.newPassword?.value || "") !== (els.confirmPassword?.value || "")) {
    showFormError(els.passwordError, "New passwords do not match.");
    return;
  }
  els.passwordSubmit.disabled = true;
  els.passwordSubmit.textContent = "Updating...";
  try {
    await fetchJson("/api/account/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: els.currentPassword?.value || "",
        new_password: els.newPassword?.value || "",
        confirm_password: els.confirmPassword?.value || "",
      }),
    });
    els.passwordForm?.reset();
    showFormError(els.passwordError, "Password updated.");
    els.passwordError?.classList.remove("hidden");
  } catch (error) {
    showFormError(els.passwordError, error.message);
  } finally {
    els.passwordSubmit.disabled = false;
    els.passwordSubmit.textContent = "Update password";
  }
}

async function bootHomePage() {
  await loadProviders();
  renderProviderPicker();
  defaultParticipants().forEach((participant) => addParticipantCard(participant));
  els.requestForm?.addEventListener("submit", createConversation);
}

async function bootConversationPage() {
  state.conversationId = currentConversationIdFromPath();
  await loadProviders();
  if (!state.conversationId) {
    renderConversation(null);
    return;
  }
  try {
    const conversation = await fetchJson(`/api/conversations/${state.conversationId}`);
    renderConversation(conversation);
    managePolling(conversation);
  } catch (error) {
    console.error(error);
    renderConversation(null);
  }
  els.resumeButton?.addEventListener("click", resumeConversation);
}

function bootLoginPage() {
  els.loginForm?.addEventListener("submit", handleLogin);
  showFormError(els.loginError, authPageMessageFromQuery());
}

function bootSignupPage() {
  els.signupForm?.addEventListener("submit", handleSignup);
  showFormError(els.signupError, authPageMessageFromQuery());
}

function bootAccountPage() {
  renderAccountPage();
  if (!state.user) {
    return;
  }
  loadAccountDetails()
    .then((payload) => {
      state.user = payload.user || state.user;
      renderSidebarAccount();
      renderAccountPage();
      renderAccountStats(payload.stats || {});
    })
    .catch((error) => {
      showFormError(els.passwordError, error.message);
    });
  els.passwordForm?.addEventListener("submit", handlePasswordChange);
}

async function boot() {
  bindShell();
  await loadCurrentUser();
  showSuccessToast(consumeFlashMessage());
  if (els.conversationList) {
    await loadConversationList();
  }

  if (state.page === "home") {
    await bootHomePage();
    return;
  }
  if (state.page === "conversation") {
    await bootConversationPage();
    return;
  }
  if (state.page === "login") {
    bootLoginPage();
    return;
  }
  if (state.page === "signup") {
    bootSignupPage();
    return;
  }
  if (state.page === "account") {
    bootAccountPage();
  }
}

boot().catch((error) => {
  console.error(error);
  window.alert("Pantheon could not start.");
});
