const state = {
  page: document.body.dataset.page || "home",
  conversations: [],
  currentConversation: null,
  currentId: null,
  pollHandle: null,
  models: {
    openai: [],
    anthropic: [],
    defaults: {},
  },
};

const els = {
  menuToggle: document.getElementById("menu-toggle"),
  drawerClose: document.getElementById("drawer-close"),
  drawerScrim: document.getElementById("drawer-scrim"),
  historyDrawer: document.getElementById("history-drawer"),
  conversationCount: document.getElementById("conversation-count"),
  conversationList: document.getElementById("conversation-list"),
  requestForm: document.getElementById("request-form"),
  questionInput: document.getElementById("question-input"),
  openaiModel: document.getElementById("openai-model"),
  anthropicModel: document.getElementById("anthropic-model"),
  roundsInput: document.getElementById("rounds-input"),
  anthropicWebSearch: document.getElementById("anthropic-web-search"),
  dryRunInput: document.getElementById("dry-run-input"),
  submitButton: document.getElementById("submit-button"),
  detailEmptyState: document.getElementById("detail-empty-state"),
  conversationView: document.getElementById("conversation-view"),
  conversationStatusLine: document.getElementById("conversation-status-line"),
  conversationTitle: document.getElementById("conversation-title"),
  statusPill: document.getElementById("status-pill"),
  configChips: document.getElementById("config-chips"),
  errorBanner: document.getElementById("error-banner"),
  transcriptStage: document.getElementById("transcript-stage"),
  summaryStage: document.getElementById("summary-stage"),
  resumeButton: document.getElementById("resume-button"),
};

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
    const langClass = language ? ` class="language-${escapeHtml(language)}"` : "";
    codeBlocks.push(`<pre><code${langClass}>${escapeHtml(code.trimEnd())}</code></pre>`);
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
      const items = lines
        .map((line) => line.replace(/^[-*]\s+/, ""))
        .map((line) => `<li>${renderInlineMarkdown(line)}</li>`)
        .join("");
      return `<ul>${items}</ul>`;
    }
    if (lines.every((line) => /^\d+\.\s+/.test(line))) {
      const items = lines
        .map((line) => line.replace(/^\d+\.\s+/, ""))
        .map((line) => `<li>${renderInlineMarkdown(line)}</li>`)
        .join("");
      return `<ol>${items}</ol>`;
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

function groupedTurns(turns) {
  const rounds = new Map();
  for (const turn of turns || []) {
    const key = turn.roundNumber;
    if (!rounds.has(key)) {
      rounds.set(key, { round: key, openai: null, anthropic: null });
    }
    const group = rounds.get(key);
    if (turn.provider === "openai") {
      group.openai = turn;
    }
    if (turn.provider === "anthropic") {
      group.anthropic = turn;
    }
  }
  return [...rounds.values()].sort((a, b) => a.round - b.round);
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

function setDrawerOpen(isOpen) {
  if (!els.historyDrawer || !els.menuToggle || !els.drawerScrim) {
    return;
  }
  els.historyDrawer.classList.toggle("open", isOpen);
  els.drawerScrim.classList.toggle("hidden", !isOpen);
  els.menuToggle.setAttribute("aria-expanded", String(isOpen));
  els.historyDrawer.setAttribute("aria-hidden", String(!isOpen));
}

function bindDrawer() {
  els.menuToggle?.addEventListener("click", () => {
    const nextOpen = !els.historyDrawer?.classList.contains("open");
    setDrawerOpen(nextOpen);
  });
  els.drawerClose?.addEventListener("click", () => setDrawerOpen(false));
  els.drawerScrim?.addEventListener("click", () => setDrawerOpen(false));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setDrawerOpen(false);
    }
  });
}

function renderConversationList() {
  if (!els.conversationList || !els.conversationCount) {
    return;
  }

  els.conversationCount.textContent = String(state.conversations.length);
  if (!state.conversations.length) {
    els.conversationList.innerHTML = `<p class="conversation-meta">No requests yet.</p>`;
    return;
  }

  els.conversationList.innerHTML = state.conversations
    .map((conversation) => {
      const activeClass = conversation.id === state.currentId ? "active" : "";
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

function renderConfigChips(config) {
  if (!els.configChips) {
    return;
  }
  const chips = [
    `OpenAI: ${config.openaiModel || "unknown"}`,
    `Anthropic: ${config.anthropicModel || "unknown"}`,
    config.rounds ? `Rounds: ${config.rounds}` : "",
    config.anthropicWebSearch ? `Anthropic web: ${config.anthropicWebSearch}` : "",
    config.dryRun ? "Dry run" : "",
  ].filter(Boolean);
  els.configChips.innerHTML = chips.map((chip) => `<span class="config-chip">${escapeHtml(chip)}</span>`).join("");
}

function renderTurnCard(turn, sideLabel, sideClass) {
  if (!turn) {
    return `
      <article class="turn-card ${sideClass} placeholder-card">
        <div>
          <p class="turn-title">${sideLabel}</p>
          <p class="status-hint">Waiting for this model to answer.</p>
        </div>
      </article>
    `;
  }

  const citations = (turn.citations || [])
    .slice(0, 8)
    .map((citation) => {
      const note = citation.note ? ` - ${escapeHtml(citation.note)}` : "";
      return `<li><a href="${escapeHtml(citation.url)}" target="_blank" rel="noreferrer">${escapeHtml(citation.title || citation.url)}</a>${note}</li>`;
    })
    .join("");

  return `
    <article class="turn-card ${sideClass}">
      <div class="turn-header">
        <div>
          <p class="turn-title">${escapeHtml(turn.speakerLabel)}</p>
          <p class="turn-subtitle">${escapeHtml(turn.model)}</p>
        </div>
      </div>
      <div class="turn-body">
        <div class="markdown-body">${renderMarkdown(turn.responseText)}</div>
        ${citations ? `<div class="citations"><p class="citations-title">Sources</p><ol>${citations}</ol></div>` : ""}
      </div>
    </article>
  `;
}

function splitSummarySections(markdown) {
  const source = String(markdown || "").replace(/\r\n/g, "\n");
  const lines = source.split("\n");
  const sections = [];
  let title = "Claude's Final Wrap-Up";
  let currentHeading = null;
  let currentLines = [];

  function pushCurrent() {
    if (currentHeading) {
      sections.push({
        heading: currentHeading,
        markdown: currentLines.join("\n").trim(),
      });
    }
  }

  for (const line of lines) {
    if (!title && /^#\s+/.test(line)) {
      title = line.replace(/^#\s+/, "").trim();
      continue;
    }
    if (/^#\s+/.test(line)) {
      title = line.replace(/^#\s+/, "").trim();
      continue;
    }
    if (/^##\s+/.test(line)) {
      pushCurrent();
      currentHeading = line.replace(/^##\s+/, "").trim();
      currentLines = [];
      continue;
    }
    currentLines.push(line);
  }
  pushCurrent();

  return { title, sections };
}

function conclusionTone(heading) {
  const normalized = heading.toLowerCase();
  if (normalized.includes("verdict")) {
    return "verdict";
  }
  if (normalized.includes("agreement")) {
    return "agreement";
  }
  if (normalized.includes("disagreement")) {
    return "disagreement";
  }
  if (normalized.includes("conclusion")) {
    return "conclusion";
  }
  if (normalized.includes("source")) {
    return "sources";
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

  const { title, sections } = splitSummarySections(markdown);
  if (!sections.length) {
    els.summaryStage.classList.remove("hidden");
    els.summaryStage.innerHTML = `
      <div class="summary-shell">
        <div class="summary-card">
          <div class="summary-title-row">
            <p class="eyebrow">Claude Wrap-Up</p>
            <span class="summary-tag">Final conclusion</span>
          </div>
          <div class="markdown-body">${renderMarkdown(markdown)}</div>
        </div>
      </div>
    `;
    return;
  }

  function headingRank(heading) {
    const normalized = heading.toLowerCase();
    if (normalized.includes("final verdict")) {
      return 0;
    }
    if (normalized.includes("agreement")) {
      return 1;
    }
    if (normalized.includes("disagreement")) {
      return 2;
    }
    if (normalized.includes("best insight")) {
      return 3;
    }
    if (normalized.includes("conclusion")) {
      return 4;
    }
    if (normalized.includes("source")) {
      return 5;
    }
    return 999;
  }

  const sortedSections = [...sections].sort((left, right) => headingRank(left.heading) - headingRank(right.heading));

  els.summaryStage.classList.remove("hidden");
  els.summaryStage.innerHTML = `
    <div class="summary-shell">
      <div class="summary-hero">
        <div>
          <p class="eyebrow">Claude Wrap-Up</p>
          <h2>${escapeHtml(title)}</h2>
        </div>
        <span class="summary-tag">Final conclusion</span>
      </div>
      <div class="summary-grid">
        ${sortedSections
          .map((section) => {
            const tone = conclusionTone(section.heading);
            const wideClass = tone === "conclusion" || tone === "sources" ? "wide" : "";
            return `
              <article class="conclusion-card ${tone} ${wideClass}">
                <h3>${escapeHtml(section.heading)}</h3>
                <div class="markdown-body">${renderMarkdown(section.markdown)}</div>
              </article>
            `;
          })
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
    document.title = "Duet Lab Conversation";
    return;
  }

  els.detailEmptyState.classList.add("hidden");
  els.conversationView.classList.remove("hidden");
  state.currentConversation = conversation;
  state.currentId = conversation.id;

  document.title = `${conversation.title} · Duet Lab`;
  els.conversationStatusLine.textContent = `${conversation.status} · ${timeLabel(conversation.updatedAt)}`;
  els.conversationTitle.textContent = conversation.question;
  els.statusPill.textContent = conversation.status;
  els.statusPill.className = `status-pill ${conversation.status}`;

  renderConfigChips(conversation.config);

  if (conversation.error) {
    els.errorBanner?.classList.remove("hidden");
    els.errorBanner.textContent = conversation.error;
  } else {
    els.errorBanner?.classList.add("hidden");
    if (els.errorBanner) {
      els.errorBanner.textContent = "";
    }
  }

  const rounds = groupedTurns(conversation.turns || []);
  els.transcriptStage.innerHTML = rounds.length
    ? rounds
        .map(
          (round) => `
            <section class="round-card">
              <div class="round-header">
                <h3>Round ${round.round}</h3>
                <span class="config-chip">Side-by-side duet</span>
              </div>
              <div class="duet-columns">
                ${renderTurnCard(round.openai, "OpenAI", "left")}
                ${renderTurnCard(round.anthropic, "Anthropic", "right")}
              </div>
            </section>
          `
        )
        .join("")
    : `
      <section class="round-card">
        <div class="placeholder-card">
          <div>
            <p class="turn-title">The duet is getting ready.</p>
            <p class="status-hint">The two columns will fill in as each model finishes its turn.</p>
          </div>
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

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data;
}

async function loadModels() {
  if (!els.openaiModel || !els.anthropicModel) {
    return;
  }
  const data = await fetchJson("/api/models");
  state.models = data;
  els.openaiModel.innerHTML = data.openai
    .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`)
    .join("");
  els.anthropicModel.innerHTML = data.anthropic
    .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`)
    .join("");
  els.openaiModel.value = data.defaults.openai;
  els.anthropicModel.value = data.defaults.anthropic;
}

async function loadConversationList() {
  const data = await fetchJson("/api/conversations");
  state.conversations = data.conversations || [];
  renderConversationList();
}

async function fetchConversation(id) {
  const conversation = await fetchJson(`/api/conversations/${id}`);
  renderConversation(conversation);
  renderConversationList();
  managePolling(conversation);
  return conversation;
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
      if (state.currentId) {
        const refreshed = await fetchJson(`/api/conversations/${state.currentId}`);
        renderConversation(refreshed);
        if (refreshed.status !== "running" && refreshed.status !== "queued") {
          managePolling(refreshed);
        }
      }
    } catch (error) {
      console.error(error);
    }
  }, 2000);
}

async function createConversation(event) {
  event.preventDefault();
  const payload = {
    question: els.questionInput?.value.trim() || "",
    openai_model: els.openaiModel?.value || "",
    anthropic_model: els.anthropicModel?.value || "",
    rounds: Number(els.roundsInput?.value || 6),
    anthropic_web_search: els.anthropicWebSearch?.value || "basic",
    dry_run: Boolean(els.dryRunInput?.checked),
  };

  if (!payload.question) {
    els.questionInput?.focus();
    return;
  }

  if (els.submitButton) {
    els.submitButton.disabled = true;
    els.submitButton.textContent = "Launching...";
  }

  try {
    const conversation = await fetchJson("/api/conversations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    window.location.assign(conversationUrl(conversation.id));
  } catch (error) {
    window.alert(error.message);
  } finally {
    if (els.submitButton) {
      els.submitButton.disabled = false;
      els.submitButton.textContent = "Launch duet";
    }
  }
}

async function resumeCurrentConversation() {
  if (!state.currentId) {
    return;
  }
  try {
    const conversation = await fetchJson(`/api/conversations/${state.currentId}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await loadConversationList();
    renderConversation(conversation);
    managePolling(conversation);
  } catch (error) {
    window.alert(error.message);
  }
}

async function bootHomePage() {
  await loadModels();
  await loadConversationList();
  els.requestForm?.addEventListener("submit", createConversation);
}

async function bootConversationPage() {
  state.currentId = currentConversationIdFromPath();
  await loadConversationList();
  if (!state.currentId) {
    renderConversation(null);
    return;
  }
  try {
    await fetchConversation(state.currentId);
  } catch (error) {
    console.error(error);
    renderConversation(null);
  }
  els.resumeButton?.addEventListener("click", resumeCurrentConversation);
}

async function boot() {
  bindDrawer();
  if (state.page === "home") {
    await bootHomePage();
    return;
  }
  await bootConversationPage();
}

boot().catch((error) => {
  console.error(error);
  window.alert("The Duet Lab UI could not start.");
});
