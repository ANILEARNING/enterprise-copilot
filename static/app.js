"use strict";

/* ==========================================================================
   Utilities
   ========================================================================== */

const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

async function post(url, body = {}) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (networkErr) {
    throw new Error("Network error — the server may be unreachable.");
  }
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[c]));
}

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function timeAgo(iso) {
  if (!iso) return "—";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 5) return "just now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function formatTime(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

/* ==========================================================================
   Toasts
   ========================================================================== */

const TOAST_ICONS = { success: "✓", danger: "✕", warning: "⚠", info: "ℹ" };

function toast(type, title, message = "") {
  const stack = $("toastStack");
  const el = document.createElement("div");
  el.className = `toast-item ${type}`;
  el.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
    <div class="toast-body">
      <p class="toast-title">${escapeHtml(title)}</p>
      ${message ? `<p class="toast-msg">${escapeHtml(message)}</p>` : ""}
    </div>
    <button class="toast-close" aria-label="Dismiss">✕</button>`;
  stack.appendChild(el);
  const remove = () => { el.style.opacity = "0"; setTimeout(() => el.remove(), 150); };
  el.querySelector(".toast-close").addEventListener("click", remove);
  setTimeout(remove, 5000);
}

/* ==========================================================================
   Theme
   ========================================================================== */

const THEME_KEY = "ec-theme";

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", choice);
  }
  localStorage.setItem(THEME_KEY, choice);
  $$("#themeSwitch button").forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === choice));
  const isDark = choice === "dark" || (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  $("themeToggle").textContent = isDark ? "☀️" : "🌙";
}

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || "system";
  applyTheme(saved);
  $("themeToggle").addEventListener("click", () => {
    const current = localStorage.getItem(THEME_KEY) || "system";
    const isDark = current === "dark" || (current === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
    applyTheme(isDark ? "light" : "dark");
  });
  $$("#themeSwitch button").forEach((b) => {
    b.addEventListener("click", () => applyTheme(b.dataset.themeChoice));
  });
}

/* ==========================================================================
   View routing
   ========================================================================== */

const VIEW_META = {
  copilot: { title: "Copilot", sub: "Ask questions, run agent workflows, and review guardrails" },
  knowledge: { title: "Knowledge base", sub: "Manage documents used to ground Copilot's answers" },
  skills: { title: "Skills", sub: "Generation skills that ask a few questions, then produce a file" },
  sessions: { title: "Session", sub: "Context Copilot is currently using for this conversation" },
  agents: { title: "Agents & tools", sub: "Registry, skills, and human-in-the-loop approvals" },
  settings: { title: "Settings", sub: "Appearance and read-only system status" },
  login: { title: "Login", sub: "Placeholder only — v1 has no real authentication" },
  about: { title: "About", sub: "What this workspace is and how it's built" },
};

function switchView(name) {
  if (!VIEW_META[name]) return;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $$(".nav-link[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("pageTitle").textContent = VIEW_META[name].title;
  $("pageSub").textContent = VIEW_META[name].sub;
  if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  closeSidebarMobile();
  if (name === "knowledge") loadDocuments();
  if (name === "skills") loadSkillPackages();
  if (name === "agents") { loadAgentsAndSkills(); loadHitlRequests(); }
  if (name === "sessions") { renderSessionView(); loadPastSessions(); loadCheckpoints(); }
  if (name === "settings") renderSettingsView();
}

function openSidebarMobile() { $("sidebar").classList.add("open"); $("sidebarBackdrop").classList.add("open"); }
function closeSidebarMobile() { $("sidebar").classList.remove("open"); $("sidebarBackdrop").classList.remove("open"); }

function initNav() {
  $$(".nav-link[data-view]").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  $$("[data-view-link]").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.viewLink)));
  $("menuToggle").addEventListener("click", openSidebarMobile);
  $("sidebarBackdrop").addEventListener("click", closeSidebarMobile);
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1)));
  const initial = location.hash.slice(1);
  if (VIEW_META[initial]) switchView(initial);
}

/* ==========================================================================
   Command palette
   ========================================================================== */

const COMMANDS = [
  { icon: "💬", label: "Go to Copilot", hint: "Nav", run: () => switchView("copilot") },
  { icon: "📚", label: "Go to Knowledge base", hint: "Nav", run: () => switchView("knowledge") },
  { icon: "🕒", label: "Go to Session", hint: "Nav", run: () => switchView("sessions") },
  { icon: "🧭", label: "Go to Agents & Tools", hint: "Nav", run: () => switchView("agents") },
  { icon: "⚙️", label: "Go to Settings", hint: "Nav", run: () => switchView("settings") },
  { icon: "🔐", label: "Go to Login", hint: "Nav", run: () => switchView("login") },
  { icon: "ℹ️", label: "Go to About", hint: "Nav", run: () => switchView("about") },
  { icon: "➕", label: "Add a document", hint: "Action", run: () => { switchView("knowledge"); openDocModal(); } },
  { icon: "🔄", label: "Refresh document list", hint: "Action", run: () => { switchView("knowledge"); loadDocuments(); } },
  { icon: "🆕", label: "Start a new session", hint: "Action", run: () => startNewSession() },
  { icon: "🌙", label: "Toggle theme", hint: "Action", run: () => $("themeToggle").click() },
  { icon: "🧹", label: "Clear chat transcript", hint: "Action", run: () => clearChat() },
];

let cmdkActiveIndex = 0;

function renderCmdk(filter = "") {
  const list = $("cmdkList");
  const f = filter.trim().toLowerCase();
  const matches = COMMANDS.filter((c) => c.label.toLowerCase().includes(f));
  cmdkActiveIndex = 0;
  if (!matches.length) {
    list.innerHTML = `<div class="cmdk-empty">No matching commands</div>`;
    return;
  }
  list.innerHTML = matches.map((c, i) => `
    <div class="cmdk-item ${i === 0 ? "active" : ""}" data-idx="${i}">
      <span class="cmdk-icon">${c.icon}</span><span>${escapeHtml(c.label)}</span>
      <span class="cmdk-hint">${c.hint}</span>
    </div>`).join("");
  list.dataset.matches = JSON.stringify(matches.map((c) => c.label));
  $$(".cmdk-item", list).forEach((el) => {
    el.addEventListener("click", () => {
      const cmd = matches[Number(el.dataset.idx)];
      closeCmdk();
      cmd.run();
    });
  });
  window.__cmdkMatches = matches;
}

function openCmdk() {
  $("cmdkBackdrop").classList.add("open");
  $("cmdkInput").value = "";
  renderCmdk("");
  setTimeout(() => $("cmdkInput").focus(), 20);
}
function closeCmdk() { $("cmdkBackdrop").classList.remove("open"); }

function initCmdk() {
  $("cmdkTrigger").addEventListener("click", openCmdk);
  $("cmdkBackdrop").addEventListener("click", (e) => { if (e.target === $("cmdkBackdrop")) closeCmdk(); });
  $("cmdkInput").addEventListener("input", (e) => renderCmdk(e.target.value));
  $("cmdkInput").addEventListener("keydown", (e) => {
    const items = $$(".cmdk-item");
    if (e.key === "ArrowDown") { e.preventDefault(); cmdkActiveIndex = Math.min(cmdkActiveIndex + 1, items.length - 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); cmdkActiveIndex = Math.max(cmdkActiveIndex - 1, 0); }
    else if (e.key === "Enter") { e.preventDefault(); items[cmdkActiveIndex]?.click(); return; }
    else if (e.key === "Escape") { closeCmdk(); return; }
    else return;
    items.forEach((it, i) => it.classList.toggle("active", i === cmdkActiveIndex));
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); }
    if (e.key === "Escape") closeCmdk();
  });
}

/* ==========================================================================
   Guardrails
   ========================================================================== */

let guardrailInfo = null;

async function loadGuardrails() {
  try {
    guardrailInfo = await post("/api/guardrails/status");
    $("guardrailPill").className = "pill pill-success";
    $("guardrailPill").innerHTML = `<span class="pill-dot"></span><span>Guardrails · Phase ${guardrailInfo.phase}</span>`;
    $("envLabel").textContent = guardrailInfo.enabled ? "Guardrails active" : "Guardrails disabled";
    $("envDot").style.background = guardrailInfo.enabled ? "var(--success)" : "var(--danger)";

    $("guardrailPanel").innerHTML = `
      <p class="small mb-2">${escapeHtml(guardrailInfo.description)}</p>
      <div class="d-flex flex-wrap gap-2">
        ${guardrailInfo.checks.map((c) => `<span class="chip">${escapeHtml(c)}</span>`).join("")}
      </div>`;

    $("settingsGuardrails").innerHTML = `
      <div class="settings-row">
        <div>
          <div class="settings-label">Status</div>
          <div class="settings-desc">${escapeHtml(guardrailInfo.description)}</div>
        </div>
        <span class="pill ${guardrailInfo.enabled ? "pill-success" : "pill-danger"}"><span class="pill-dot"></span>${guardrailInfo.enabled ? "Active" : "Disabled"}</span>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">Active checks</div>
          <div class="settings-desc">Input is checked before the model/agent runs; output is checked before it's returned.</div>
        </div>
        <div class="d-flex flex-wrap gap-2 justify-content-end">
          ${guardrailInfo.checks.map((c) => `<span class="chip">${escapeHtml(c)}</span>`).join("")}
        </div>
      </div>`;
  } catch (e) {
    $("guardrailPill").className = "pill pill-danger";
    $("guardrailPill").innerHTML = `<span class="pill-dot"></span><span>Guardrails unavailable</span>`;
    $("guardrailPanel").innerHTML = `<p class="small mb-0" style="color:var(--danger)">Could not reach the guardrails service.</p>`;
  }
}

/* ==========================================================================
   Chat / Copilot
   ========================================================================== */

const SESSION_STORAGE_KEY = "copilot.sessionId";

let sessionId = null;
let sessionCreatedAt = null;
let messages = []; // {role, content, meta?, error?, at}
let lastMeta = null;
// Images staged for the *next* send (see app/models.py:ImageAttachment) —
// {data (base64, no data: prefix), mime_type, previewUrl (a local
// data: URI, purely for the composer thumbnail — never sent to the server)}.
let pendingImages = [];

// Persists the active session id so a page reload can reconnect to the same
// server-side (file-backed, see app/storage.py) chat history instead of
// silently starting a fresh, empty conversation.
function setActiveSessionId(id) {
  sessionId = id;
  if (id) localStorage.setItem(SESSION_STORAGE_KEY, id);
  else localStorage.removeItem(SESSION_STORAGE_KEY);
}

function chatSessionLabel() {
  return sessionId ? `session ${sessionId.slice(0, 8)}…` : "no session yet";
}

function syncSessionChrome() {
  $("chatSessionTag").textContent = chatSessionLabel();
  $("profileSessionId").textContent = sessionId ? `Session ${sessionId.slice(0, 8)}…` : "No session yet";
}

async function startNewSession() {
  try {
    const data = await post("/api/session/start");
    setActiveSessionId(data.session_id);
    sessionCreatedAt = data.created_at;
    messages = [];
    lastMeta = null;
    renderChat();
    syncSessionChrome();
    renderSessionView();
    sessionCheckpoints = [];
    showInterruptedTurnBanner(null);
    toast("success", "New session started", `Session ${sessionId.slice(0, 8)}… is now active.`);
  } catch (e) {
    toast("danger", "Couldn't start a session", e.message);
  }
}

// Reconnects to whatever session (if any) was active before the last reload,
// loading its persisted history (app/storage.py) back into the chat. Server
// history only carries role/content/at, not the rich per-message metadata
// (agent/skills/sources) a live response has — those chips just don't show
// on restored turns, which is an honest reflection of what's actually stored.
async function restoreActiveSession() {
  const savedId = localStorage.getItem(SESSION_STORAGE_KEY);
  if (!savedId) return;
  try {
    const session = await post("/api/session/get", { session_id: savedId });
    setActiveSessionId(session.session_id);
    sessionCreatedAt = session.created_at;
    messages = (session.messages || []).map((m) => ({ role: m.role, content: m.content, at: m.at }));
    renderChat();
    syncSessionChrome();
    sessionCheckpoints = session.checkpoints || [];
    showInterruptedTurnBanner(session.turn_checkpoint || null);
  } catch (e) {
    // The saved session no longer exists server-side (e.g. its file was
    // removed) -- fall back to starting fresh rather than surfacing an error.
    setActiveSessionId(null);
  }
}

async function loadPastSessions() {
  try {
    const data = await post("/api/session/list");
    renderPastSessions(data.sessions || []);
  } catch (e) {
    $("pastSessionsContainer").innerHTML = `<p class="small mb-0" style="color:var(--danger)">Couldn't load past sessions: ${escapeHtml(e.message)}</p>`;
  }
}

function renderPastSessions(sessions) {
  const container = $("pastSessionsContainer");
  if (!container) return;
  if (!sessions.length) {
    container.innerHTML = `<p class="small mb-0" style="color:var(--text-faint)">No past sessions yet.</p>`;
    return;
  }
  container.innerHTML = `
    <div class="table-responsive">
      <table class="data-table">
        <thead><tr><th>Preview</th><th>Messages</th><th>Updated</th><th></th></tr></thead>
        <tbody>
          ${sessions.map((s) => `
            <tr>
              <td><span class="row-title truncate" style="max-width:320px;">${escapeHtml(s.preview || "(empty)")}</span><div class="row-sub">${s.session_id.slice(0, 8)}…</div></td>
              <td>${s.message_count}</td>
              <td>${timeAgo(s.updated_at)}</td>
              <td class="text-end"><button class="btn btn-sm btn-outline-secondary" data-action="resume-session" data-id="${s.session_id}">Resume</button></td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  container.querySelectorAll('[data-action="resume-session"]').forEach((btn) => {
    btn.addEventListener("click", () => resumeSession(btn.dataset.id));
  });
}

async function resumeSession(id) {
  try {
    const session = await post("/api/session/get", { session_id: id });
    setActiveSessionId(session.session_id);
    sessionCreatedAt = session.created_at;
    messages = (session.messages || []).map((m) => ({ role: m.role, content: m.content, at: m.at }));
    lastMeta = null;
    renderChat();
    syncSessionChrome();
    sessionCheckpoints = session.checkpoints || [];
    showInterruptedTurnBanner(session.turn_checkpoint || null);
    switchView("copilot");
    toast("success", "Session resumed", `${messages.length} message(s) loaded.`);
  } catch (e) {
    toast("danger", "Couldn't resume session", e.message);
  }
}

/* ==========================================================================
   Checkpoints: named save points a session can be rolled back to, plus the
   "your last turn didn't finish" banner surfaced from session.turn_checkpoint
   (see app/storage.py's module docstring and POST /session/checkpoint/*).
   ========================================================================== */

// This session's saved checkpoints, refreshed by loadCheckpoints() — kept in
// sync with restoreActiveSession()/resumeSession() so the Sessions view
// doesn't need its own extra round-trip just to know what's there.
let sessionCheckpoints = [];

function showInterruptedTurnBanner(turnCheckpoint) {
  const banner = $("interruptedTurnBanner");
  if (!banner) return;
  if (!turnCheckpoint) {
    banner.classList.add("d-none");
    return;
  }
  const stageLabel = turnCheckpoint.label || turnCheckpoint.stage || "an earlier step";
  const hitlNote = turnCheckpoint.hitl_request_id
    ? " Check Pending approvals — it may be waiting on you." : "";
  $("interruptedTurnText").textContent =
    `Your last turn didn't finish (last known step: ${stageLabel}).${hitlNote} Send a new message to continue.`;
  banner.classList.remove("d-none");
}

async function saveCheckpoint() {
  if (!sessionId) {
    toast("warning", "No active session", "Start a conversation first, then save a checkpoint.");
    return;
  }
  const label = window.prompt("Name this checkpoint:", `Checkpoint ${new Date().toLocaleString()}`);
  if (!label) return;
  try {
    await post("/api/session/checkpoint/save", { session_id: sessionId, label });
    toast("success", "Checkpoint saved", `"${label}" can be restored from the Sessions view.`);
    loadCheckpoints();
  } catch (e) {
    toast("danger", "Couldn't save checkpoint", e.message);
  }
}

async function loadCheckpoints() {
  if (!sessionId) {
    sessionCheckpoints = [];
    renderCheckpoints([]);
    return;
  }
  try {
    const data = await post("/api/session/checkpoint/list", { session_id: sessionId });
    sessionCheckpoints = data.checkpoints || [];
    renderCheckpoints(sessionCheckpoints);
  } catch (e) {
    const container = $("checkpointsContainer");
    if (container) container.innerHTML = `<p class="small mb-0" style="color:var(--danger)">Couldn't load checkpoints: ${escapeHtml(e.message)}</p>`;
  }
}

function renderCheckpoints(checkpoints) {
  const container = $("checkpointsContainer");
  if (!container) return;
  if (!checkpoints.length) {
    container.innerHTML = `<div class="state-block" style="padding:28px 16px;"><p style="margin:0;">No checkpoints saved yet. Use "Save checkpoint" in Copilot to create one.</p></div>`;
    return;
  }
  container.innerHTML = `
    <div class="table-responsive">
      <table class="data-table">
        <thead><tr><th>Label</th><th>Messages</th><th>Saved</th><th></th></tr></thead>
        <tbody>
          ${checkpoints.map((c) => `
            <tr>
              <td><span class="row-title">${escapeHtml(c.label)}</span></td>
              <td>${c.message_count}</td>
              <td>${timeAgo(c.created_at)}</td>
              <td class="text-end"><button class="btn btn-sm btn-outline-secondary" data-action="restore-checkpoint" data-id="${c.checkpoint_id}">Restore</button></td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
  container.querySelectorAll('[data-action="restore-checkpoint"]').forEach((btn) => {
    btn.addEventListener("click", () => restoreCheckpoint(btn.dataset.id));
  });
}

async function restoreCheckpoint(checkpointId) {
  const checkpoint = sessionCheckpoints.find((c) => c.checkpoint_id === checkpointId);
  const label = checkpoint ? checkpoint.label : "this checkpoint";
  const confirmed = window.confirm(
    `Restore "${label}"? Messages sent after this checkpoint will be permanently discarded from this session.`,
  );
  if (!confirmed) return;
  try {
    const session = await post("/api/session/checkpoint/restore", { session_id: sessionId, checkpoint_id: checkpointId });
    messages = (session.messages || []).map((m) => ({ role: m.role, content: m.content, at: m.at }));
    lastMeta = null;
    renderChat();
    syncSessionChrome();
    renderSessionView();
    showInterruptedTurnBanner(null);
    sessionCheckpoints = session.checkpoints || [];
    renderCheckpoints(sessionCheckpoints);
    toast("success", "Checkpoint restored", `Rolled back to "${label}".`);
  } catch (e) {
    toast("danger", "Couldn't restore checkpoint", e.message);
  }
}

function clearChat() {
  messages = [];
  lastMeta = null;
  renderChat();
  $("lastRunPanel").innerHTML = `<p class="text-faint small mb-0" style="color:var(--text-faint)">Send a message to see execution details here.</p>`;
  toast("info", "Chat cleared", "The visible transcript was cleared. Server-side session context is unchanged.");
}

function renderChat() {
  const scroll = $("chatScroll");
  $("chatWelcome").style.display = messages.length ? "none" : "flex";
  $$(".msg-row, .thinking-row, .thinking-flow").forEach((n) => n.remove());
  messages.forEach((m, i) => scroll.appendChild(renderMessage(m, i)));
  scroll.scrollTop = scroll.scrollHeight;
}

// Shared by the per-message timeline (timelineChipsHtml, below) and the
// "Last run" side panel (updateLastRunPanel) — collapsed by default as a
// small row of numbered link icons (🔗1 🔗2 ...), each already independently
// clickable/hoverable (title = page title) without expanding anything;
// <summary> reveals the full title + URL list on demand.
function webSourcesHtml(sources) {
  if (!sources?.length) return "";
  const icons = sources.map((s, i) => `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer" class="chip" title="${escapeHtml(s.title)}" style="text-decoration:none; padding:2px 7px; font-size:11px;">🔗${i + 1}</a>`).join("");
  const fullList = sources.map((s) => `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(s.content || "")}" style="text-decoration:none; display:block; white-space:normal; text-align:left; padding:4px 0;">${escapeHtml(s.title)} <span style="color:var(--text-faint); font-size:11px;">↗ ${escapeHtml(s.url)}</span></a>`).join("");
  return `
    <details class="context-disclosure" style="margin-top:4px;">
      <summary class="d-flex align-items-center gap-1" style="cursor:pointer; list-style:none;">
        <span class="small" style="color:var(--text-faint)">🔎</span>
        <span class="d-flex gap-1 flex-wrap">${icons}</span>
      </summary>
      <div class="mt-1" style="max-width:480px;">${fullList}</div>
    </details>`;
}

function timelineChipsHtml(meta) {
  if (!meta) return "";
  const chips = [];
  // meta.provider is only set when a model call actually happened this turn
  // (a direct/agent chat reply, or a skill run's finish-turn spec draft).
  // Mid-flow skill Q&A turns and blocked-input turns call no provider at
  // all, so omit the chip rather than mislabeling it "mock". meta.model (real
  // token-streamed direct chat only) names the actual model, not just the
  // provider — e.g. "ollama/gpt-oss:20b" instead of the old "autogen-stream".
  if (meta.provider) {
    const label = meta.model ? `${meta.provider}/${meta.model}` : meta.provider;
    chips.push(`<span class="timeline-step"><span class="dot"></span>${escapeHtml(label)}${meta.used_fallback ? " (fallback)" : ""}</span>`);
  }
  if (meta.agent) chips.push(`<span class="timeline-step"><span class="dot"></span>agent: ${escapeHtml(meta.agent)}</span>`);
  (meta.skills || []).forEach((s) => chips.push(`<span class="timeline-step"><span class="dot"></span>skill: ${escapeHtml(s)}</span>`));
  if (meta.sources?.length) chips.push(`<span class="timeline-step"><span class="dot"></span>${meta.sources.length} source${meta.sources.length > 1 ? "s" : ""}</span>`);
  // Real MCP tool calls this turn (agent-mode only, see OrchestrationResult.tool_calls) —
  // not shown at all when empty, same honesty rule as the provider chip above.
  if (meta.tool_calls?.length) chips.push(`<span class="timeline-step"><span class="dot"></span>tools: ${meta.tool_calls.map(escapeHtml).join(", ")}</span>`);
  if (meta.hitl_pending?.length) chips.push(`<span class="timeline-step" style="color:var(--warning);border-color:var(--warning)"><span class="dot" style="background:var(--warning)"></span>awaiting approval</span>`);
  const timelineHtml = `<div class="timeline">${chips.join("")}</div>`;
  // Real, clickable web_search results this specific turn actually read (see
  // OrchestrationResult.web_sources) — kept collapsed by default (a small
  // numbered chip row) since the full list is also visible in the Last Run
  // side panel; <details> gives every past message in the transcript its
  // own expandable citation list without cluttering the answer text itself.
  const sourcesHtml = webSourcesHtml(meta.web_sources);
  // A generated .html/.pdf the coding agent's approved code produced this
  // turn (see OrchestrationResult.downloadable_artifacts, app/artifacts.py)
  // — a real, working file, not a claim: view_url genuinely serves the
  // stored bytes (GET /artifacts/{id}, inline) and the download button
  // reuses the same POST+blob mechanism the skill-package downloads use
  // (triggerBlobDownload).
  const artifactsHtml = meta.downloadable_artifacts?.length ? `
    <div class="d-flex flex-column gap-1 mt-1" style="max-width:520px;">
      ${meta.downloadable_artifacts.map((a) => `
        <div class="d-flex align-items-center gap-2 chip" style="justify-content:space-between;">
          <span>📄 ${escapeHtml(a.filename)} <span style="color:var(--text-faint); font-size:11px;">(${formatBytes(a.size_bytes)})</span></span>
          <span class="d-flex gap-2">
            <a href="${escapeHtml(a.view_url)}" target="_blank" rel="noopener noreferrer">View</a>
            <button type="button" class="btn-link-download" data-artifact-id="${escapeHtml(a.artifact_id)}" data-artifact-filename="${escapeHtml(a.filename)}" style="background:none; border:none; padding:0; color:var(--accent,inherit); text-decoration:underline; cursor:pointer; font-size:inherit;">Download</button>
          </span>
        </div>`).join("")}
    </div>` : "";
  return timelineHtml + sourcesHtml + artifactsHtml;
}

// What the model actually saw this turn — the fully-assembled prompt
// (RAG-grounded context, retrieved-source citations instructions, guardrail-
// screened), the buffered/summarized prior-turn memory (app/memory.py —
// render_memory_preview), and where the path has one, the system message —
// captured straight from the backend's own "model_call"/"model_call_started"
// status events (see sendMessage's "status" handler) rather than
// reconstructed client-side, so this is never a guess. Collapsed by default
// since it's a debugging/transparency aid, not primary content.
function contextDisclosureHtml(m) {
  if (!m.promptPreview) return "";
  const block = (label, value) => `
        <div>
          <div class="small mb-1" style="color:var(--text-faint);">${label}</div>
          <pre class="text-mono small" style="white-space:pre-wrap; background:var(--bg-subtle,rgba(127,127,127,0.08)); border-radius:6px; padding:8px; max-height:280px; overflow:auto; margin:0;">${escapeHtml(value)}</pre>
        </div>`;
  return `
    <details class="context-disclosure" style="margin-top:6px;">
      <summary class="small" style="color:var(--text-faint); cursor:pointer;">🔍 View context sent to model</summary>
      <div class="mt-2" style="display:flex; flex-direction:column; gap:8px;">
        ${m.systemPreview ? block("System prompt", m.systemPreview) : `
        <div class="small" style="color:var(--text-faint); font-style:italic;">This turn's provider has no separate system prompt — only the combined prompt below.</div>`}
        ${m.memoryPreview ? block("Conversation memory (buffered recent turns + summary of older ones)", m.memoryPreview) : `
        <div class="small" style="color:var(--text-faint); font-style:italic;">No prior-turn memory sent yet — this is early enough in the session that there's nothing to buffer or summarize.</div>`}
        ${block("Prompt (incl. any retrieved/grounded context)", m.promptPreview)}
        ${m.responsePreview ? block("Raw model response", m.responsePreview) : ""}
      </div>
    </details>`;
}

// Per-turn guardrail activity — what check_input/check_context/check_output
// (app/services.py:GuardrailService) actually found and did, not just the
// pass/fail chip. Never renders matched terms/patterns themselves (only
// category + count), per .claude/rules/guardrails.md's "never expose ...
// hidden policies" rule — a category name like "email" or "api_key" says
// what kind of thing was caught without repeating the sensitive value.
function guardrailFindingsHtml(label, check) {
  if (!check) return "";
  const rows = [];
  (check.pii || []).forEach((f) => rows.push(`PII redacted — ${escapeHtml(f.category)} ×${f.count}`));
  (check.sensitive_data || []).forEach((f) => rows.push(`Sensitive data redacted — ${escapeHtml(f.category)} ×${f.count}`));
  (check.unsafe_content || []).forEach((f) => rows.push(`Unsafe content policy — ${escapeHtml(f.category)} ×${f.count}`));
  if (check.matched_rules?.some((r) => !r.startsWith("unsafe-content:") && !["sensitive-data-redacted", "pii-redacted"].includes(r))) {
    rows.push(`Prompt-injection pattern matched`);
  }
  if (!rows.length) return "";
  const tone = check.allowed ? "var(--warning)" : "var(--danger)";
  return `
    <div class="small" style="padding:4px 0; border-left:2px solid ${tone}; padding-left:8px;">
      <div style="color:var(--text-faint);">${label}${check.allowed ? "" : " — blocked"}</div>
      ${rows.map((r) => `<div>${r}</div>`).join("")}
    </div>`;
}

function guardrailActivityHtml(meta) {
  const g = meta?.guardrails;
  if (!g) return "";
  const sections = [
    guardrailFindingsHtml("Input", g.input),
    guardrailFindingsHtml("Retrieved context", g.context),
    guardrailFindingsHtml("Output", g.output),
  ].filter(Boolean);
  if (!sections.length) return "";
  return `
    <details class="context-disclosure" style="margin-top:6px;">
      <summary class="small" style="color:var(--text-faint); cursor:pointer;">🛡️ Guardrail activity (${sections.length})</summary>
      <div class="mt-2" style="display:flex; flex-direction:column; gap:6px;">${sections.join("")}</div>
    </details>`;
}

// Minimal, safe markdown: fenced code blocks and inline code are pulled out
// and escaped/rendered FIRST (protected from every pass below), then the
// rest is escaped and re-gains **bold**, a CommonMark-style _italic_, and
// newlines. "CommonMark-style" matters here specifically: "_" only opens/
// closes emphasis when it isn't sitting directly between two word
// characters -- so a generated Python script's own snake_case names
// (value_counts, to_dict, ...) render as literal text instead of being
// half-eaten as italic markers (the naive `/_(.+?)_/g` this used to be did
// exactly that: it silently ate underscores out of ordinary identifiers).
// Only http(s), or a same-origin-relative path -- never javascript:/data:/
// anything else, since link targets here can come from model output
// (web_search results, generated artifact links) and must never become an
// injectable/clickable script vector.
const _SAFE_LINK_PATTERN = /^https?:\/\//i;

function renderMarkdownLite(text) {
  const codeBlocks = [];
  const inlineCode = [];
  const links = [];

  // 1) Fenced ```lang\n...\n``` blocks, pulled out before anything else
  // touches the text, so their content is never bold/italic-processed.
  let working = String(text ?? "").replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push(`<pre class="code-block mb-2">${escapeHtml(code.replace(/\n$/, ""))}</pre>`);
    return `\x00CB${idx}\x00`;
  });

  // 2) [label](url) links -- pulled out before the escape pass too, same
  // reason as code blocks (the emitted <a> markup must survive escaping,
  // but `label` itself still goes through escapeHtml since it's arbitrary
  // model/user text). A relative artifact view_url ("/artifacts/<id>") is
  // allowed through as-is (same-origin, not attacker-controlled -- see
  // OrchestrationResult.downloadable_artifacts); only absolute non-http(s)
  // URLs (javascript:, data:, ...) are rejected and left as plain text.
  working = working.replace(/\[([^\]\n]+)\]\((\/[^\s)]+|https?:\/\/[^\s)]+)\)/g, (whole, label, url) => {
    if (!url.startsWith("/") && !_SAFE_LINK_PATTERN.test(url)) return whole;
    const idx = links.length;
    links.push(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    return `\x00LK${idx}\x00`;
  });

  // 3) Escape everything else, then pull out inline `code` spans the same way.
  working = escapeHtml(working).replace(/`([^`\n]+)`/g, (_, code) => {
    const idx = inlineCode.length;
    inlineCode.push(`<code>${code}</code>`);
    return `\x00IC${idx}\x00`;
  });

  working = working
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w])_([^\s_][^_]*?)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/\n/g, "<br>");

  // 4) Splice the protected spans back in (their own HTML must survive).
  return working
    .replace(/\x00CB(\d+)\x00/g, (_, i) => codeBlocks[Number(i)])
    .replace(/\x00LK(\d+)\x00/g, (_, i) => links[Number(i)])
    .replace(/\x00IC(\d+)\x00/g, (_, i) => inlineCode[Number(i)]);
}

// A self-contained "quiz box" for the skill Q&A: option buttons (when the
// question declares them) AND a typeable input (always, unless it's a
// required-options-only select) -- either path calls answerSkillQuestion(),
// which freezes the box into "you answered: X" immediately so it can't be
// submitted twice while the next turn is in flight.
function skillRunExtrasHtml(skillRun) {
  if (!skillRun) return "";
  if (skillRun.status === "AWAITING_ANSWERS" && skillRun.question) {
    const q = skillRun.question;
    const options = q.options || [];
    const optionsHtml = options.length
      ? `<div class="d-flex flex-wrap gap-1 mb-2">${options.map((o) => `<button type="button" class="btn btn-outline-secondary btn-sm" data-skill-option="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join("")}</div>`
      : "";
    const showTextInput = q.type !== "select" || q.allow_other || options.length === 0;
    const textHtml = showTextInput ? `
      <div class="d-flex gap-2">
        <input type="text" class="form-control form-control-sm" data-skill-text-input placeholder="${options.length ? "Or type your own…" : "Type your answer…"}">
        <button type="button" class="btn btn-primary btn-sm" data-skill-text-submit>Send</button>
      </div>` : "";
    const skipHtml = !q.required ? `<button type="button" class="btn btn-ghost btn-sm mt-2" data-skill-option="skip">Skip this one</button>` : "";
    return `<div class="skill-question-box mt-2" data-skill-question-box>${optionsHtml}${textHtml}${skipHtml}</div>`;
  }
  if (skillRun.status === "COMPLETED" && skillRun.download_ready) {
    return `<div class="skill-question-box mt-2 d-flex align-items-center justify-content-between gap-2">
      <span class="small" style="color:var(--text-faint)">Ready to download</span>
      <button type="button" class="btn btn-primary btn-sm" data-skill-download="${escapeHtml(skillRun.run_id)}" data-skill-filename="${escapeHtml((skillRun.skill_name || "output") + "." + (skillRun.output || "bin"))}">Download ${escapeHtml(skillRun.skill_name || "file")}</button>
    </div>`;
  }
  return "";
}

function answerSkillQuestion(row, value) {
  const box = row.querySelector("[data-skill-question-box]");
  if (box) box.outerHTML = `<div class="small mt-2" style="color:var(--text-faint)">You answered: <strong>${escapeHtml(value)}</strong></div>`;
  sendMessage(value);
}

function renderMessage(m, idx) {
  const isUser = m.role === "user";
  // Real backend progress (see CopilotService.chat_stream) arrives before any
  // content for the multi-step paths (skill drafting/generation, agent-mode
  // retrieval/thinking) — shown as a lightweight typing-style indicator
  // instead of an empty bubble until actual content starts.
  if (!isUser && m.streaming && !m.content && m.statusLabel) {
    const row = document.createElement("div");
    row.className = "thinking-flow";
    // Every completed step so far (see sendMessage's "status" handling) —
    // a real, visible flow (agent selection, retrieval, tool calls, ...)
    // instead of one line that silently overwrites itself.
    const history = (m.statusLog || [])
      .map((label) => `<div class="thinking-log-item"><span class="check"></span><span>${escapeHtml(label)}</span></div>`)
      .join("");
    row.innerHTML = `
      ${history}
      <div class="thinking-row">
        <div class="thinking-dots"><span></span><span></span><span></span></div>
        <span>${escapeHtml(m.statusLabel)}</span>
      </div>`;
    return row;
  }

  const row = document.createElement("div");
  row.className = `msg-row ${m.role}`;
  const avatar = isUser ? "You" : "EC";
  const bubbleClass = m.error ? "msg-bubble error" : "msg-bubble";
  const streamingCursor = m.streaming ? `<span class="stream-cursor">▍</span>` : "";
  const cancelledNote = m.cancelled ? `<div class="small mt-1" style="color:var(--warning)">⏹ Stopped by user</div>` : "";
  const imagesHtml = (m.images || []).map((src) => `<img class="msg-image-attachment" src="${src}" alt="Attached image">`).join("");
  row.innerHTML = `
    <div class="msg-avatar">${avatar}</div>
    <div class="msg-bubble-wrap">
      <div class="${bubbleClass}">${imagesHtml}${renderMarkdownLite(m.content)}${streamingCursor}${cancelledNote}${skillRunExtrasHtml(m.meta?.skill_run)}</div>
      ${!isUser ? timelineChipsHtml(m.meta) : ""}
      ${!isUser ? guardrailActivityHtml(m.meta) : ""}
      ${!isUser ? hitlCardsHtml(m.hitlRecords) : ""}
      ${!isUser ? contextDisclosureHtml(m) : ""}
      <div class="msg-meta-row">
        <span class="row-sub" style="margin:0;">${formatTime(m.at)}</span>
        <div class="msg-actions">
          <button class="btn-icon btn-ghost btn btn-sm" data-action="copy" title="Copy">⧉</button>
          ${!isUser ? `<button class="btn-icon btn-ghost btn btn-sm" data-action="regenerate" title="Regenerate">↻</button>` : ""}
          ${m.error ? `<button class="btn btn-sm btn-outline-danger" data-action="retry">Retry</button>` : ""}
        </div>
      </div>
    </div>`;
  row.querySelector('[data-action="copy"]')?.addEventListener("click", () => {
    navigator.clipboard?.writeText(m.content).then(() => toast("success", "Copied to clipboard"));
  });
  row.querySelector('[data-action="regenerate"]')?.addEventListener("click", () => regenerateFrom(idx));
  row.querySelector('[data-action="retry"]')?.addEventListener("click", () => retryMessage(idx));
  // Inline HITL approval card buttons (hitlCardsHtml/hitlCardHtml above) —
  // scoped to this row so they work independently of the global listeners
  // loadHitlRequests() attaches for the side panel / Agents & Tools queue.
  row.querySelectorAll('[data-hitl-action]').forEach((btn) => {
    btn.addEventListener("click", () => decideHitl(btn.dataset.id, btn.dataset.hitlAction === "approve"));
  });
  row.querySelectorAll('[data-skill-option]').forEach((btn) => {
    btn.addEventListener("click", () => answerSkillQuestion(row, btn.dataset.skillOption));
  });
  const skillTextInput = row.querySelector("[data-skill-text-input]");
  const skillTextSubmit = row.querySelector("[data-skill-text-submit]");
  if (skillTextInput && skillTextSubmit) {
    const submitSkillText = () => {
      const value = skillTextInput.value.trim();
      if (value) answerSkillQuestion(row, value);
    };
    skillTextSubmit.addEventListener("click", submitSkillText);
    skillTextInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitSkillText(); }
    });
  }
  row.querySelector('[data-skill-download]')?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const runId = btn.dataset.skillDownload;
    try {
      await triggerBlobDownload("/api/skill-packages/run/download", { run_id: runId }, btn.dataset.skillFilename);
    } catch (err) {
      toast("danger", "Download failed", err.message);
    }
  });
  // Generated dashboards/reports (OrchestrationResult.downloadable_artifacts,
  // see timelineChipsHtml) — same POST+blob download mechanism as skill
  // packages above, just a different backend route/id field.
  row.querySelectorAll('[data-artifact-id]').forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await triggerBlobDownload(
          "/api/artifacts/download", { artifact_id: btn.dataset.artifactId }, btn.dataset.artifactFilename,
        );
      } catch (err) {
        toast("danger", "Download failed", err.message);
      }
    });
  });
  return row;
}

let currentStreamId = null;

function setStreaming(isStreaming) {
  $("send").disabled = isStreaming;
  $("sendLabel").innerHTML = isStreaming ? `<span class="spinner-mini"></span>` : "Send";
  $("stopStream").classList.toggle("d-none", !isStreaming);
}

async function cancelActiveStream() {
  if (!currentStreamId) return;
  try {
    await post("/api/chat/cancel", { stream_id: currentStreamId });
  } catch (e) {
    // Most likely the stream already finished naturally (404) -- not worth surfacing.
  }
}

// Consumes the /api/chat/stream SSE body (fetch + ReadableStream, since
// EventSource doesn't support POST) and forwards each {"data": ...} frame's
// parsed JSON to onEvent as it arrives.
async function readSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      if (frame.startsWith("data: ")) onEvent(JSON.parse(frame.slice(6)));
    }
  }
}

async function sendMessage(text, opts = {}) {
  const message = (text ?? $("message").value).trim();
  if (!message) return;
  const agentMode = opts.agentMode ?? $("agentMode").checked;
  const webSearch = agentMode && $("webSearch").checked;
  // Independent of agentMode — Deck Builder fires on a "pptx" chat-trigger
  // match regardless of the Agent Mode toggle (see CopilotService.chat()'s
  // routing), so this must stay checkable/sendable either way.
  const autoGenerate = opts.autoGenerate ?? $("autoGenerate").checked;
  // Attachments are opt.images (retry/regenerate replaying an earlier user
  // message) or whatever's staged in the composer for a fresh send.
  const images = opts.images ?? pendingImages.map(({ data, mime_type }) => ({ data, mime_type }));
  const previewUrls = opts.images ? undefined : pendingImages.map((img) => img.previewUrl);

  messages.push({ role: "user", content: message, at: new Date().toISOString(), images: previewUrls });
  const assistantIdx = messages.length;
  messages.push({ role: "assistant", content: "", at: new Date().toISOString(), streaming: true, statusLabel: null, statusLog: [] });
  renderChat();
  $("message").value = "";
  autoGrow($("message"));
  pendingImages = [];
  renderComposerAttachments();
  setStreaming(true);

  let finalData = null;
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message, agent_mode: agentMode, web_search: webSearch, auto_generate: autoGenerate,
        session_id: sessionId, images,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`Request failed (${res.status})`);

    await readSseStream(res, (evt) => {
      if (evt.type === "start") {
        currentStreamId = evt.stream_id;
      } else if (evt.type === "session") {
        setActiveSessionId(evt.session_id);
        syncSessionChrome();
      } else if (evt.type === "delta") {
        messages[assistantIdx].content += evt.text;
        renderChat();
      } else if (evt.type === "status") {
        // model_call (agent-mode/tool-calling paths) and model_call_started
        // (plain direct-chat streaming, app/streaming.py) carry the exact
        // prompt — and, where the path has one, system message — actually
        // sent to the model this turn (RAG-grounded context, guardrail-
        // screened, system instructions and all). Captured here, not shown
        // as a status line, so "View context sent to model" (renderMessage /
        // contextDisclosureHtml) can show precisely what the agent saw,
        // instead of a guess reconstructed from the UI's own state — and
        // instead of the raw event name leaking into the visible "thinking"
        // flow below.
        if (evt.stage === "model_call_started") {
          messages[assistantIdx].systemPreview = evt.system_preview;
          messages[assistantIdx].promptPreview = evt.prompt_preview;
          messages[assistantIdx].memoryPreview = evt.memory_preview;
          renderChat();
          return;
        }
        // Real progress from the backend (see CopilotService.chat_stream /
        // AutoGenOrchestrator.run / SkillRunService.submit_answers) — e.g.
        // "Thinking…", "Searching knowledge base…", "Calling tool
        // calculator…". The previous label moves into statusLog (a visible
        // running flow, see renderMessage) as the new one becomes current.
        const m = messages[assistantIdx];
        if (m.statusLabel) m.statusLog.push(m.statusLabel);
        m.statusLabel = evt.label || evt.stage;
        if (evt.stage === "model_call" && evt.prompt_preview) {
          m.systemPreview = evt.system_preview;
          m.promptPreview = evt.prompt_preview;
          m.responsePreview = evt.response_preview;
          m.memoryPreview = evt.memory_preview;
        }
        renderChat();
      } else if (evt.type === "model_info") {
        // The real resolved provider/model, known as soon as streaming starts
        // — replaces the old generic "autogen-stream" placeholder.
        messages[assistantIdx].statusModel = evt.model ? `${evt.provider}/${evt.model}` : evt.provider;
        renderChat();
      } else if (evt.type === "team_event") {
        console.debug("[team_event]", evt.event_type, evt.source); // emit_team_events, for transparency only
      } else if (evt.type === "done") {
        finalData = evt;
      }
    });

    if (finalData) {
      const priorPreview = messages[assistantIdx]; // carries systemPreview/promptPreview/responsePreview/memoryPreview captured from "status" events above
      messages[assistantIdx] = {
        role: "assistant", content: finalData.response, at: new Date().toISOString(),
        meta: finalData, cancelled: !!finalData.cancelled,
        systemPreview: priorPreview?.systemPreview, promptPreview: priorPreview?.promptPreview,
        memoryPreview: priorPreview?.memoryPreview,
        // Direct-mode streaming (model_call_started) never gets a separate
        // response_preview event — the response only exists as the deltas
        // that became finalData.response — so fall back to that instead of
        // leaving "raw model response" blank for the most common chat path.
        responsePreview: priorPreview?.responsePreview ?? finalData.response,
      };
      lastMeta = finalData;
      renderChat();
      updateGuardrailChip(finalData.guardrails);
      updateLastRunPanel(finalData);
      if (finalData.hitl_pending?.length) {
        toast("warning", "Action needs your approval", `${finalData.hitl_pending.length} request(s) below — review and decide right here.`);
        // Attach the full records (not just ids) to THIS message so
        // renderMessage can render an actionable approve/reject card inline,
        // right where the user is already looking, instead of only in the
        // side panel / Agents & Tools tab (see hitlCardsHtml, reused from
        // the existing Agents & Tools queue rendering).
        await attachHitlCardsToMessage(assistantIdx, finalData.hitl_pending);
        await loadHitlRequests();
      }
      renderSettingsProvider(finalData);
    } else {
      messages[assistantIdx].streaming = false;
      renderChat();
    }
  } catch (e) {
    messages[assistantIdx] = { role: "assistant", content: e.message, at: new Date().toISOString(), error: true };
    renderChat();
    toast("danger", "Message failed", e.message);
  } finally {
    currentStreamId = null;
    setStreaming(false);
  }
}

function retryMessage(idx) {
  // find the user message preceding this failed assistant message
  for (let i = idx - 1; i >= 0; i--) {
    if (messages[i].role === "user") {
      messages.splice(idx, 1); // drop the failed bubble
      renderChat();
      sendMessage(messages[i].content);
      return;
    }
  }
}

function regenerateFrom(idx) {
  for (let i = idx - 1; i >= 0; i--) {
    if (messages[i].role === "user") {
      messages.splice(idx, 1);
      renderChat();
      sendMessage(messages[i].content);
      return;
    }
  }
}

function updateGuardrailChip(guardrails) {
  const input = guardrails?.input;
  const context = guardrails?.context;
  const output = guardrails?.output;
  const blocked = (input && !input.allowed) || (context && !context.allowed) || (output && !output.allowed);
  const chip = $("guardrailChip");
  if (blocked) {
    const failing = !input?.allowed ? input : (!context?.allowed ? context : output);
    chip.textContent = `Blocked: ${failing?.message || "policy violation"}`;
    chip.style.color = "var(--danger)";
    chip.style.borderColor = "var(--danger)";
  } else {
    // Redaction activity (PII/secrets caught but not blocking) is worth
    // surfacing on the chip itself, not just in the per-message disclosure —
    // it's the difference between "nothing happened" and "something was
    // caught and handled."
    const redactedCount = [input, context, output]
      .filter(Boolean)
      .reduce((n, c) => n + (c.pii?.length || 0) + (c.sensitive_data?.length || 0), 0);
    const parts = ["input", context ? "context" : null, "output"].filter(Boolean);
    chip.textContent = redactedCount
      ? `Guardrails: ${parts.join(", ")} allowed · ${redactedCount} item(s) redacted`
      : `Guardrails: ${parts.join(", ")} allowed`;
    chip.style.color = redactedCount ? "var(--warning)" : "";
    chip.style.borderColor = redactedCount ? "var(--warning)" : "";
  }
}

function updateLastRunPanel(data) {
  const providerLabel = data.provider
    ? `${data.model ? `${data.provider}/${data.model}` : data.provider}${data.used_fallback ? " (fallback)" : ""}`
    : "— (no model call)";
  const g = data.guardrails;
  const redactedCount = [g?.input, g?.context, g?.output]
    .filter(Boolean)
    .reduce((n, c) => n + (c.pii?.length || 0) + (c.sensitive_data?.length || 0), 0);
  const blocked = [g?.input, g?.context, g?.output].some((c) => c && !c.allowed);
  const guardrailSummary = blocked ? "Blocked" : redactedCount ? `${redactedCount} item(s) redacted` : "Clean";
  const rows = [
    ["Provider", providerLabel],
    ["Agent", data.agent || "— (direct call)"],
    ["Skills", data.skills?.length ? data.skills.join(", ") : "—"],
    ["Tools called", data.tool_calls?.length ? data.tool_calls.join(", ") : "—"],
    ["Sources", data.sources?.length ? `${data.sources.length} document(s)` : "—"],
    ["Pending approvals", data.hitl_pending?.length ? String(data.hitl_pending.length) : "0"],
    ["Guardrail activity", guardrailSummary],
  ];
  const guardrailDetail = guardrailActivityHtml({ guardrails: g });
  $("lastRunPanel").innerHTML = `
    <div class="d-flex flex-column gap-2">
      ${rows.map(([k, v]) => `<div class="d-flex justify-content-between small"><span style="color:var(--text-faint)">${k}</span><span class="text-end"${k === "Guardrail activity" ? ` style="color:${blocked ? "var(--danger)" : redactedCount ? "var(--warning)" : ""}"` : ""}>${escapeHtml(v)}</span></div>`).join("")}
      ${data.sources?.length ? `<hr class="divider my-1"><div class="d-flex flex-wrap gap-1">${data.sources.map((s) => `<span class="chip" title="vector ${s.vector_score ?? "—"} · bm25 ${s.bm25_score ?? "—"} · rerank ${s.rerank_score ?? "—"}${s.embedding_model ? ` · embed ${s.embedding_model}` : ""}">${escapeHtml(s.filename)}${s.chunk_index != null ? `#${s.chunk_index}` : ""}</span>`).join("")}</div>` : ""}
      ${data.web_sources?.length ? `<hr class="divider my-1"><div class="small mb-1" style="color:var(--text-faint)">Web sources — what the agent actually read</div>${webSourcesHtml(data.web_sources)}` : ""}
      ${guardrailDetail ? `<hr class="divider my-1">${guardrailDetail}` : ""}
    </div>`;
  const modePill = $("modePill");
  if (!data.provider) {
    modePill.className = "pill pill-neutral"; modePill.innerHTML = `<span class="pill-dot"></span>No model call`;
  } else if (data.used_fallback) {
    modePill.className = "pill pill-warning"; modePill.innerHTML = `<span class="pill-dot"></span>Fallback (mock)`;
  } else if (data.provider === "mock") {
    modePill.className = "pill pill-neutral"; modePill.innerHTML = `<span class="pill-dot"></span>Demo mode`;
  } else {
    modePill.className = "pill pill-success"; modePill.innerHTML = `<span class="pill-dot"></span>Live · ${escapeHtml(data.provider)}`;
  }
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 160) + "px";
}

// Multimodal (see docs/multimodal.md): reads dropped/picked image files as
// base64 for pendingImages, previewed in the composer until the next send.
function renderComposerAttachments() {
  const el = $("composerAttachments");
  el.classList.toggle("d-none", pendingImages.length === 0);
  el.innerHTML = pendingImages.map((img, i) => `
    <div class="composer-attachment">
      <img src="${img.previewUrl}" alt="Attached image ${i + 1}">
      <button type="button" class="remove-attachment" data-remove-image="${i}" title="Remove">✕</button>
    </div>`).join("");
  el.querySelectorAll("[data-remove-image]").forEach((btn) => {
    btn.addEventListener("click", () => {
      pendingImages.splice(Number(btn.dataset.removeImage), 1);
      renderComposerAttachments();
    });
  });
}

function addImageFiles(fileList) {
  Array.from(fileList).forEach((file) => {
    if (!file.type.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result; // "data:image/png;base64,AAAA..."
      const data = dataUrl.split(",")[1] || "";
      pendingImages.push({ data, mime_type: file.type || "image/png", previewUrl: dataUrl });
      renderComposerAttachments();
    };
    reader.readAsDataURL(file);
  });
}

// Web Search only ever takes effect in agent mode (tool-calling is an
// agent-mode-only path — see AutoGenOrchestrator._run_with_tools). Keeping
// the checkbox visually enabled but syncing a "needs agent mode" hint (via
// syncWebSearchToggle) rather than hard-disabling it avoids a confusing
// "why won't this check" moment if someone taps it before Agent mode.
let webSearchConfigured = true; // optimistic until renderMcpStatus() confirms

function updateWebSearchToggleAvailability(configured) {
  webSearchConfigured = configured;
  syncWebSearchToggle();
}

function syncWebSearchToggle() {
  const wrap = $("webSearchToggleWrap");
  const input = $("webSearch");
  if (!wrap || !input) return;
  const agentOn = $("agentMode").checked;
  if (!webSearchConfigured) {
    input.checked = false;
    input.disabled = true;
    wrap.title = "Set TAVILY_API_KEY in .env to enable web search (see Settings → MCP tools).";
    wrap.style.opacity = "0.5";
  } else {
    input.disabled = false;
    wrap.style.opacity = agentOn ? "1" : "0.6";
    wrap.title = agentOn
      ? "Lets the agent search the live web when it decides that helps answer your question."
      : "Requires Agent mode to take effect.";
  }
}

function initChat() {
  $("send").addEventListener("click", () => sendMessage());
  $("agentMode").addEventListener("change", syncWebSearchToggle);
  $("message").addEventListener("input", (e) => autoGrow(e.target));
  $("message").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $("attachImage").addEventListener("click", () => $("imageInput").click());
  $("imageInput").addEventListener("change", (e) => {
    addImageFiles(e.target.files);
    e.target.value = ""; // allow re-picking the same file later
  });
  $("message").addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith("image/"));
    if (files.length) addImageFiles(files);
  });
  $$(".prompt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      if (chip.dataset.agent) $("agentMode").checked = true;
      sendMessage(chip.dataset.prompt);
    });
  });
  $("clearChat").addEventListener("click", clearChat);
  $("saveCheckpointBtn").addEventListener("click", saveCheckpoint);
  $("dismissInterruptedTurn").addEventListener("click", () => showInterruptedTurnBanner(null));
  $("profileNewSession").addEventListener("click", startNewSession);
  $("startNewSessionBtn").addEventListener("click", startNewSession);
  $("settingsNewSession").addEventListener("click", startNewSession);
  $("stopStream").addEventListener("click", cancelActiveStream);
}

/* ==========================================================================
   Sessions view
   ========================================================================== */

function renderSessionView() {
  $("sessSessionId").textContent = sessionId || "none yet";
  $("sessMsgCount").textContent = String(messages.length);
  $("sessStarted").textContent = sessionCreatedAt ? formatTime(sessionCreatedAt) : "—";

  const box = $("sessionTranscript");
  if (!messages.length) {
    box.innerHTML = `
      <div class="state-block">
        <div class="state-icon">🕒</div>
        <h3>No messages yet</h3>
        <p>Start a conversation in Copilot to see the transcript here.</p>
      </div>`;
    return;
  }
  box.innerHTML = `<div class="p-3 d-flex flex-column gap-3">` + messages.map((m) => `
    <div class="d-flex gap-2">
      <span class="chip" style="min-width:70px;justify-content:center;">${m.role}</span>
      <div class="flex-grow-1">
        <div class="small">${escapeHtml(m.content)}</div>
        <div class="row-sub">${formatTime(m.at)}</div>
      </div>
    </div>`).join("") + `</div>`;
}

/* ==========================================================================
   Knowledge / RAG
   ========================================================================== */

let documents = [];
let pendingFileContent = null;
let pendingFileName = null;
let pendingFileEncoding = "text";
let pendingDeleteId = null;

function docStatusPill(status) {
  if (status === "indexed") return `<span class="pill pill-success"><span class="pill-dot"></span>Indexed</span>`;
  if (status === "error") return `<span class="pill pill-danger"><span class="pill-dot"></span>Error</span>`;
  return `<span class="pill pill-neutral"><span class="pill-dot"></span>${escapeHtml(status || "unknown")}</span>`;
}

// What embedding model is actually indexing/searching this knowledge base,
// plus which vector-store backend is live (see app/routes.py POST
// /rag/status) — "configured" is static, "last_used" reflects the most
// recent ingest/query and can differ if a provider fell back.
async function loadKnowledgeEmbeddingStatus() {
  const el = $("knowledgeEmbeddingStatus");
  if (!el) return;
  try {
    const data = await post("/api/rag/status", {});
    const configured = data.configured;
    const configuredLabel = configured.model ? `${configured.provider}/${configured.model}` : configured.provider;
    const lastUsed = data.last_used;
    const lastUsedLabel = lastUsed
      ? ` · last used: ${lastUsed.model ? `${lastUsed.provider}/${lastUsed.model}` : lastUsed.provider}${lastUsed.used_fallback ? " (fallback)" : ""}`
      : "";
    const vs = data.vector_store;
    const backendLabel = vs
      ? vs.backend === "qdrant"
        ? ` · Backend: Qdrant Cloud${vs.reachable ? ` (${vs.point_count ?? "?"} pts)` : " (unreachable)"}`
        : ` · Backend: in-memory (${vs.point_count ?? 0} pts)`
      : "";
    el.textContent = `Embedding: ${configuredLabel}${lastUsedLabel}${backendLabel}`;
  } catch (e) {
    el.textContent = "Embedding: couldn't load status.";
  }
}

async function loadDocuments() {
  $("documentsContainer").innerHTML = `<div class="p-3"><div class="skeleton" style="height:180px;"></div></div>`;
  loadKnowledgeEmbeddingStatus();
  try {
    const data = await post("/api/rag/document/list");
    documents = data.documents || [];
    renderDocumentStats();
    renderDocuments();
  } catch (e) {
    $("documentsContainer").innerHTML = `
      <div class="state-block state-error">
        <div class="state-icon">⚠</div>
        <h3>Couldn't load documents</h3>
        <p>${escapeHtml(e.message)}</p>
        <button class="btn btn-outline-primary btn-sm" id="retryLoadDocs">Try again</button>
      </div>`;
    $("retryLoadDocs")?.addEventListener("click", loadDocuments);
  }
}

function renderDocumentStats() {
  $("statDocCount").textContent = String(documents.length);
  $("statDocIndexed").textContent = String(documents.filter((d) => d.status === "indexed").length);
  $("statDocErrors").textContent = String(documents.filter((d) => d.status === "error").length);
  $("statDocSize").textContent = formatBytes(documents.reduce((sum, d) => sum + (d.size || 0), 0));
}

function filteredDocuments() {
  const q = ($("docSearch").value || "").trim().toLowerCase();
  const status = $("docStatusFilter").value;
  return documents.filter((d) => {
    const matchesQuery = !q || d.filename.toLowerCase().includes(q);
    const matchesStatus = !status || d.status === status;
    return matchesQuery && matchesStatus;
  });
}

function renderDocuments() {
  const list = filteredDocuments();
  const container = $("documentsContainer");

  if (!documents.length) {
    container.innerHTML = `
      <div class="state-block">
        <div class="state-icon">📚</div>
        <h3>No documents yet</h3>
        <p>Add or upload a document so the research agent can retrieve and cite it in Copilot's answers.</p>
        <button class="btn btn-primary btn-sm" id="emptyAddDoc">+ Add document</button>
      </div>`;
    $("emptyAddDoc")?.addEventListener("click", openDocModal);
    return;
  }
  if (!list.length) {
    container.innerHTML = `
      <div class="state-block">
        <div class="state-icon">🔎</div>
        <h3>No matches</h3>
        <p>No documents match your search or filter.</p>
      </div>`;
    return;
  }

  container.innerHTML = `
    <div class="table-responsive">
      <table class="data-table">
        <thead><tr><th>Document</th><th>Status</th><th>Size</th><th>Updated</th><th></th></tr></thead>
        <tbody>
          ${list.map((d) => `
            <tr data-id="${d.document_id}" class="clickable">
              <td>
                <div class="d-flex align-items-center gap-2">
                  <div class="file-icon">${escapeHtml((d.filename.split(".").pop() || "doc").slice(0, 3).toUpperCase())}</div>
                  <div class="min-w-0">
                    <div class="row-title truncate" style="max-width:260px;">${escapeHtml(d.filename)}</div>
                    <div class="row-sub">${d.document_id.slice(0, 8)}… · ${d.chunk_count ?? 0} chunk${d.chunk_count === 1 ? "" : "s"}</div>
                  </div>
                </div>
              </td>
              <td>${docStatusPill(d.status)}${d.index_error ? `<div class="row-sub" style="color:var(--danger)">${escapeHtml(d.index_error)}</div>` : ""}</td>
              <td>${formatBytes(d.size || 0)}</td>
              <td>${timeAgo(d.updated_at)}</td>
              <td class="text-end">
                <button class="btn btn-sm btn-ghost" data-action="view">View</button>
                <button class="btn btn-sm btn-ghost" data-action="edit">Edit</button>
                <button class="btn btn-sm btn-ghost" data-action="delete" style="color:var(--danger)">Delete</button>
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  $$("tr[data-id]", container).forEach((row) => {
    const id = row.dataset.id;
    row.querySelector('[data-action="view"]').addEventListener("click", (e) => { e.stopPropagation(); openDocViewModal(id); });
    row.querySelector('[data-action="edit"]').addEventListener("click", (e) => { e.stopPropagation(); openDocModal(id); });
    row.querySelector('[data-action="delete"]').addEventListener("click", (e) => { e.stopPropagation(); openDeleteConfirm(id); });
    row.addEventListener("click", () => openDocViewModal(id));
  });
}

/* --- Add / Edit modal --- */

function docModal() { return bootstrap.Modal.getOrCreateInstance($("docModal")); }

function switchDocTab(tab) {
  $$("#docModalTabs .nav-link").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $("tab-paste").classList.toggle("d-none", tab !== "paste");
  $("tab-upload").classList.toggle("d-none", tab !== "upload");
  // The "Paste content" fields are `required` — hiding that tab isn't enough to
  // exempt them from native form validation, which silently blocks submit with
  // no error and no console output. Toggle `required` with the active tab instead.
  const isPaste = tab === "paste";
  $("docName").required = isPaste;
  $("docContent").required = isPaste;
}

function resetDocForm() {
  $("docId").value = "";
  $("docName").value = "";
  $("docContent").value = "";
  pendingFileContent = null;
  pendingFileName = null;
  pendingFileEncoding = "text";
  $("docFilePreview").classList.add("d-none");
  $("docFileInput").value = "";
  $("docModalTitle").textContent = "Add document";
  $("saveDoc").textContent = "Add document";
  $("docSaveProgress").classList.add("d-none");
  switchDocTab("paste");
}

function openDocModal(documentId) {
  resetDocForm();
  if (documentId) {
    const doc = documents.find((d) => d.document_id === documentId);
    if (doc) {
      post("/api/rag/document/get", { document_id: documentId }).then((full) => {
        $("docId").value = full.document_id;
        $("docName").value = full.filename;
        $("docContent").value = full.content;
        $("docModalTitle").textContent = "Edit document";
        $("saveDoc").textContent = "Save changes";
      }).catch((e) => toast("danger", "Couldn't load document", e.message));
    }
  }
  docModal().show();
}

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Could not read that file."));
    reader.readAsText(file);
  });
}

function readFileAsBase64(file) {
  // readAsDataURL gives "data:<mime>;base64,<data>" — the extraction endpoint
  // only wants the base64 payload, decoded server-side (app/extraction.py).
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const commaIndex = result.indexOf(",");
      resolve(commaIndex === -1 ? "" : result.slice(commaIndex + 1));
    };
    reader.onerror = () => reject(new Error("Could not read that file."));
    reader.readAsDataURL(file);
  });
}

// Binary uploads (base64, extracted server-side, see app/extraction.py) vs.
// text uploads (read directly in the browser) — .csv/.html/.json/.log/.md/
// .txt have no binary container so they stay text-encoded even though the
// server does real structural parsing on some of them.
const BINARY_UPLOAD_EXTENSIONS = [".pdf", ".docx", ".xlsx"];

function isBinaryUploadFile(file) {
  const name = file.name.toLowerCase();
  return BINARY_UPLOAD_EXTENSIONS.some((ext) => name.endsWith(ext));
}

async function handleFile(file) {
  const MAX_MB = 20;
  if (file.size > MAX_MB * 1024 * 1024) {
    toast("danger", "File too large", `Files are limited to ${MAX_MB} MB.`);
    return;
  }
  try {
    const binary = isBinaryUploadFile(file);
    pendingFileContent = binary ? await readFileAsBase64(file) : await readFileAsText(file);
    pendingFileEncoding = binary ? "base64" : "text";
    pendingFileName = file.name;
    $("docFileName").textContent = file.name;
    $("docFileSize").textContent = formatBytes(file.size);
    $("docFilePreview").classList.remove("d-none");
  } catch (e) {
    toast("danger", "Couldn't read file", e.message);
  }
}

/* --- Save progress: the ingestion pipeline (clean -> chunk -> embed -> index,
   plus a PDF extract step) happens server-side in one request with no
   incremental signal back, so this simulates staged feedback on a timer and
   snaps to "done" the moment the real request resolves — better than an
   opaque spinner, honest that it's not literal server progress. --- */

function docSaveStages(filename, contentEncoding) {
  const base = ["Cleaning & chunking", "Generating embeddings", "Indexing"];
  if (contentEncoding !== "base64") return base;
  const name = (filename || "").toLowerCase();
  const format = name.endsWith(".docx") ? "DOCX" : name.endsWith(".xlsx") ? "Excel" : "PDF";
  return ["Uploading file", `Extracting text from ${format}`, ...base];
}

// Generalized over container/steps element ids so both the document-save modal
// and the skill-run modal (see initSkillsView) can drive their own progress UI
// with the same stepper look and "flash done, then hide" behavior.
function renderProgressSteps(stepsElId, stages, activeIndex) {
  $(stepsElId).innerHTML = stages.map((label, i) => {
    const state = i < activeIndex ? "done" : i === activeIndex ? "active" : "";
    const icon = i < activeIndex
      ? `<span class="step-icon"></span>`
      : i === activeIndex
        ? `<span class="step-icon"><span class="spinner-mini dark"></span></span>`
        : `<span class="step-icon"><span class="step-dot"></span></span>`;
    return `<li class="save-progress-step ${state}">${icon}<span>${escapeHtml(label)}</span></li>`;
  }).join("");
}

function startProgress(containerElId, stepsElId, stages) {
  $(containerElId).classList.remove("d-none");
  let index = 0;
  renderProgressSteps(stepsElId, stages, index);
  const timer = setInterval(() => {
    if (index < stages.length - 1) {
      index += 1;
      renderProgressSteps(stepsElId, stages, index);
    }
  }, 650);
  return {
    stop(success) {
      clearInterval(timer);
      if (success) {
        renderProgressSteps(stepsElId, stages, stages.length); // flash every step as done
        setTimeout(() => $(containerElId).classList.add("d-none"), 250);
      } else {
        $(containerElId).classList.add("d-none");
      }
    },
  };
}

function initDocModal() {
  $$("#docModalTabs .nav-link").forEach((b) => b.addEventListener("click", () => switchDocTab(b.dataset.tab)));
  $("openAddDoc").addEventListener("click", () => openDocModal());

  $("docDrop").addEventListener("click", () => $("docFileInput").click());
  $("docDrop").addEventListener("dragover", (e) => { e.preventDefault(); $("docDrop").classList.add("dragover"); });
  $("docDrop").addEventListener("dragleave", () => $("docDrop").classList.remove("dragover"));
  $("docDrop").addEventListener("drop", (e) => {
    e.preventDefault();
    $("docDrop").classList.remove("dragover");
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  });
  $("docFileInput").addEventListener("change", (e) => { if (e.target.files[0]) handleFile(e.target.files[0]); });
  $("docFileClear").addEventListener("click", () => {
    pendingFileContent = null; pendingFileName = null; pendingFileEncoding = "text";
    $("docFileInput").value = "";
    $("docFilePreview").classList.add("d-none");
  });

  $("docForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const isUpload = !$("tab-upload").classList.contains("d-none");
    const id = $("docId").value;
    let payload;
    if (isUpload && !id) {
      if (!pendingFileContent) { toast("warning", "No file selected", "Choose a file to upload first."); return; }
      payload = { filename: pendingFileName, content: pendingFileContent, content_encoding: pendingFileEncoding };
    } else {
      const filename = $("docName").value.trim();
      const content = $("docContent").value;
      if (!filename || !content.trim()) { toast("warning", "Missing information", "Document name and content are required."); return; }
      payload = { filename, content };
    }

    const btn = $("saveDoc");
    const cancelBtn = $("docCancelBtn");
    const closeBtn = $("docCloseBtn");
    const originalLabel = btn.textContent;
    btn.disabled = true;
    cancelBtn.disabled = true;
    closeBtn.disabled = true;
    btn.innerHTML = `<span class="spinner-mini dark"></span>`;
    const stages = docSaveStages(payload.filename, payload.content_encoding);
    const progress = startProgress("docSaveProgress", "docSaveProgressSteps", stages);
    try {
      if (id) {
        await post("/api/rag/document/update", { document_id: id, ...payload });
        toast("success", "Document updated", payload.filename);
      } else {
        await post("/api/rag/document/add", payload);
        toast("success", "Document added", payload.filename);
      }
      progress.stop(true);
      docModal().hide();
      await loadDocuments();
    } catch (e2) {
      progress.stop(false);
      toast("danger", "Couldn't save document", e2.message);
    } finally {
      btn.disabled = false;
      cancelBtn.disabled = false;
      closeBtn.disabled = false;
      btn.textContent = originalLabel;
    }
  });
}

/* --- View modal --- */

function docViewModalEl() { return bootstrap.Modal.getOrCreateInstance($("docViewModal")); }
let viewingDocId = null;

async function openDocViewModal(documentId) {
  viewingDocId = documentId;
  try {
    const doc = await post("/api/rag/document/get", { document_id: documentId });
    $("docViewName").textContent = doc.filename;
    $("docViewMeta").innerHTML = `
      ${docStatusPill(doc.status)}
      <span class="row-sub" style="margin:0;">${formatBytes(doc.size || 0)}</span>
      <span class="row-sub" style="margin:0;">Updated ${timeAgo(doc.updated_at)}</span>`;
    $("docViewContent").textContent = doc.content;
    docViewModalEl().show();
  } catch (e) {
    toast("danger", "Couldn't load document", e.message);
  }
}

function initDocViewModal() {
  $("docViewEdit").addEventListener("click", () => {
    docViewModalEl().hide();
    openDocModal(viewingDocId);
  });
  $("docViewDelete").addEventListener("click", () => {
    docViewModalEl().hide();
    openDeleteConfirm(viewingDocId);
  });
  $("docViewReindex").addEventListener("click", async () => {
    const btn = $("docViewReindex");
    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-mini dark"></span> Re-indexing…`;
    try {
      const doc = await post("/api/rag/document/get", { document_id: viewingDocId });
      await post("/api/rag/document/update", { document_id: viewingDocId, filename: doc.filename, content: doc.content });
      toast("success", "Document re-indexed", doc.filename);
      await loadDocuments();
      await openDocViewModal(viewingDocId);
    } catch (e) {
      toast("danger", "Re-index failed", e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

/* --- Delete confirm --- */

function deleteModalEl() { return bootstrap.Modal.getOrCreateInstance($("deleteConfirmModal")); }

function openDeleteConfirm(documentId) {
  pendingDeleteId = documentId;
  const doc = documents.find((d) => d.document_id === documentId);
  $("deleteDocName").textContent = doc ? doc.filename : "this document";
  deleteModalEl().show();
}

function initDeleteConfirm() {
  $("confirmDeleteBtn").addEventListener("click", async () => {
    if (!pendingDeleteId) return;
    const btn = $("confirmDeleteBtn");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-mini"></span>`;
    try {
      await post("/api/rag/document/delete", { document_id: pendingDeleteId });
      toast("success", "Document deleted");
      deleteModalEl().hide();
      await loadDocuments();
    } catch (e) {
      toast("danger", "Couldn't delete document", e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Delete document";
    }
  });
}

/* ==========================================================================
   Skills: upload SKILL.md packages, and run one — ask its declared
   pre-flight questions (HITL form), collect answers, generate, download.
   ========================================================================== */

let skillPackages = [];
let pendingSkillZipContent = null;   // base64, set once a .zip is chosen
let pendingSkillZipName = null;
let currentSkillRun = null;          // { run_id, skill } while the run modal is open
// type: "file" question answers — question_id -> { fileIds: [file_id, ...], names: ["a.csv", ...] }.
// Populated as soon as a file is chosen (uploaded immediately via
// POST /skill-packages/run/upload-answer-file, see wireFileQuestionInput),
// not on form submit — collectSkillAnswers just reads file_id(s) back out of
// here. fileIds has more than one entry only for a "folder" (multi-file)
// question; reset every time the run modal (re)opens (openSkillRunModal).
let pendingFileAnswers = {};

function skillUploadModalEl() { return bootstrap.Modal.getOrCreateInstance($("skillUploadModal")); }
function skillRunModalEl() { return bootstrap.Modal.getOrCreateInstance($("skillRunModal")); }

async function loadSkillPackages() {
  try {
    const data = await post("/api/skill-packages/list");
    skillPackages = data.skills || [];
    renderSkillPackages(skillPackages);
  } catch (e) {
    $("skillsContainer").innerHTML = `<p class="small mb-0" style="color:var(--danger)">Couldn't load skills: ${escapeHtml(e.message)}</p>`;
  }
}

function renderSkillPackages(skills) {
  const container = $("skillsContainer");
  if (!skills.length) {
    container.innerHTML = `
      <div class="state-block">
        <div class="state-icon">🧩</div>
        <h3>No skills yet</h3>
        <p>Upload a skill package (SKILL.md + scripts/ + reference/) to get started.</p>
      </div>`;
    return;
  }
  container.innerHTML = `
    <div class="row g-3">
      ${skills.map((s) => `
        <div class="col-12 col-md-6 col-lg-4">
          <div class="stat-card h-100 d-flex flex-column gap-2">
            <div class="d-flex align-items-center justify-content-between">
              <span class="fw-semibold">${escapeHtml(s.name)}</span>
              <span class="chip">${escapeHtml((s.output || "file").toUpperCase())}</span>
            </div>
            <p class="small mb-0 flex-grow-1" style="color:var(--text-faint)">${escapeHtml(s.description || "No description.")}</p>
            <div class="small" style="color:var(--text-faint)">${s.questions.length} question${s.questions.length === 1 ? "" : "s"} · ${s.builtin ? "built-in" : "uploaded"}</div>
            <div class="d-flex gap-2 mt-1">
              <button class="btn btn-primary btn-sm flex-grow-1" data-action="run" data-skill-id="${escapeHtml(s.skill_id)}">Run</button>
              ${s.builtin ? "" : `<button class="btn btn-outline-secondary btn-sm" data-action="delete-skill" data-skill-id="${escapeHtml(s.skill_id)}">Delete</button>`}
            </div>
          </div>
        </div>`).join("")}
    </div>`;

  container.querySelectorAll('[data-action="run"]').forEach((btn) => {
    btn.addEventListener("click", () => openSkillRunModal(btn.dataset.skillId));
  });
  container.querySelectorAll('[data-action="delete-skill"]').forEach((btn) => {
    btn.addEventListener("click", () => deleteSkillPackage(btn.dataset.skillId));
  });
}

async function deleteSkillPackage(skillId) {
  const skill = skillPackages.find((s) => s.skill_id === skillId);
  if (!confirm(`Delete the skill "${skill ? skill.name : skillId}"? This can't be undone.`)) return;
  try {
    await post("/api/skill-packages/delete", { skill_id: skillId });
    toast("success", "Skill deleted");
    await loadSkillPackages();
  } catch (e) {
    toast("danger", "Couldn't delete skill", e.message);
  }
}

/* --- Upload modal --- */

function resetSkillUploadForm() {
  pendingSkillZipContent = null;
  pendingSkillZipName = null;
  $("skillFileInput").value = "";
  $("skillFilePreview").classList.add("d-none");
}

async function handleSkillZipFile(file) {
  const MAX_MB = 20;
  if (file.size > MAX_MB * 1024 * 1024) {
    toast("danger", "File too large", `Skill packages are limited to ${MAX_MB} MB.`);
    return;
  }
  try {
    pendingSkillZipContent = await readFileAsBase64(file);
    pendingSkillZipName = file.name;
    $("skillFileName").textContent = file.name;
    $("skillFileSize").textContent = formatBytes(file.size);
    $("skillFilePreview").classList.remove("d-none");
  } catch (e) {
    toast("danger", "Couldn't read file", e.message);
  }
}

function initSkillUploadModal() {
  $("openUploadSkill").addEventListener("click", () => {
    resetSkillUploadForm();
    skillUploadModalEl().show();
  });
  $("skillDrop").addEventListener("click", () => $("skillFileInput").click());
  $("skillDrop").addEventListener("dragover", (e) => { e.preventDefault(); $("skillDrop").classList.add("dragover"); });
  $("skillDrop").addEventListener("dragleave", () => $("skillDrop").classList.remove("dragover"));
  $("skillDrop").addEventListener("drop", (e) => {
    e.preventDefault();
    $("skillDrop").classList.remove("dragover");
    if (e.dataTransfer.files[0]) handleSkillZipFile(e.dataTransfer.files[0]);
  });
  $("skillFileInput").addEventListener("change", (e) => { if (e.target.files[0]) handleSkillZipFile(e.target.files[0]); });
  $("skillFileClear").addEventListener("click", resetSkillUploadForm);

  $("skillUploadSubmit").addEventListener("click", async () => {
    if (!pendingSkillZipContent) { toast("warning", "No file selected", "Choose a skill .zip to upload first."); return; }
    const btn = $("skillUploadSubmit");
    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-mini dark"></span>`;
    try {
      const skill = await post("/api/skill-packages/upload", { content: pendingSkillZipContent });
      toast("success", "Skill uploaded", skill.name);
      skillUploadModalEl().hide();
      await loadSkillPackages();
    } catch (e) {
      toast("danger", "Couldn't upload skill", e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

/* --- Run modal: ask the skill's declared questions (HITL), then generate --- */

function renderSkillQuestions(questions) {
  pendingFileAnswers = {};
  $("skillRunQuestions").innerHTML = questions.map((q) => {
    const req = q.required ? ' <span style="color:var(--danger)">*</span>' : "";
    const showIfAttr = q.show_if ? ` data-show-if='${escapeHtml(JSON.stringify(q.show_if))}'` : "";
    if (q.type === "multiselect") {
      const boxes = q.options.map((o, i) => `
        <div class="form-check">
          <input class="form-check-input" type="checkbox" value="${escapeHtml(o)}"
            id="ms-${escapeHtml(q.id)}-${i}" data-multiselect-option="${escapeHtml(q.id)}">
          <label class="form-check-label" for="ms-${escapeHtml(q.id)}-${i}">${escapeHtml(o)}</label>
        </div>`).join("");
      const otherBox = q.allow_other ? `
        <div class="form-check">
          <input class="form-check-input" type="checkbox" id="ms-${escapeHtml(q.id)}-other" data-multiselect-other-toggle="${escapeHtml(q.id)}">
          <label class="form-check-label" for="ms-${escapeHtml(q.id)}-other">Other…</label>
        </div>
        <input type="text" class="form-control mt-1 d-none" data-multiselect-other-input="${escapeHtml(q.id)}" placeholder="Please specify">` : "";
      return `
        <div data-question-id="${escapeHtml(q.id)}" data-question-container="${escapeHtml(q.id)}" data-multiselect="true" data-required="${q.required}"${showIfAttr}>
          <label class="form-label">${escapeHtml(q.prompt)}${req}</label>
          <div class="d-flex flex-column gap-1">${boxes}${otherBox}</div>
        </div>`;
    }
    if (q.type === "select") {
      const opts = q.options.map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
      const otherOpt = q.allow_other ? `<option value="__other__">Other…</option>` : "";
      const otherInput = q.allow_other
        ? `<input type="text" class="form-control mt-2 d-none" data-other-for="${escapeHtml(q.id)}" placeholder="Please specify">`
        : "";
      return `
        <div data-question-container="${escapeHtml(q.id)}"${showIfAttr}>
          <label class="form-label">${escapeHtml(q.prompt)}${req}</label>
          <select class="form-select" data-question-id="${escapeHtml(q.id)}" data-required="${q.required}">
            <option value="">Select…</option>
            ${opts}${otherOpt}
          </select>
          ${otherInput}
        </div>`;
    }
    if (q.type === "file") {
      const isFolder = q.accept.includes("folder");
      const acceptAttr = isFolder ? "" : q.accept.map((e) => `.${e}`).join(",");
      const hint = isFolder
        ? "Choose one or more files"
        : (q.accept.length ? `Accepted: ${q.accept.join(", ")}` : "Any file type");
      return `
        <div data-question-id="${escapeHtml(q.id)}" data-question-container="${escapeHtml(q.id)}" data-file-question="true" data-required="${q.required}"${showIfAttr}>
          <label class="form-label">${escapeHtml(q.prompt)}${req}</label>
          <div class="doc-drop" data-file-drop style="padding:16px;">
            <strong>Drop a file here, or click to browse</strong>
            ${escapeHtml(hint)}
            <input type="file" class="d-none" data-file-input
              ${acceptAttr ? `accept="${escapeHtml(acceptAttr)}"` : ""} ${isFolder ? "multiple webkitdirectory" : ""}>
          </div>
          <div class="d-none mt-2" data-file-preview></div>
        </div>`;
    }
    return `
      <div data-question-container="${escapeHtml(q.id)}"${showIfAttr}>
        <label class="form-label">${escapeHtml(q.prompt)}${req}</label>
        <input type="text" class="form-control" data-question-id="${escapeHtml(q.id)}" data-required="${q.required}" placeholder="${escapeHtml(q.placeholder || "")}">
      </div>`;
  }).join("");

  $$('select[data-question-id]', $("skillRunQuestions")).forEach((sel) => {
    sel.addEventListener("change", () => {
      const other = document.querySelector(`[data-other-for="${sel.dataset.questionId}"]`);
      if (other) other.classList.toggle("d-none", sel.value !== "__other__");
      applyShowIfVisibility(questions);
    });
  });

  $$('[data-multiselect-other-toggle]', $("skillRunQuestions")).forEach((toggle) => {
    toggle.addEventListener("change", () => {
      const input = document.querySelector(`[data-multiselect-other-input="${toggle.dataset.multiselectOtherToggle}"]`);
      if (input) input.classList.toggle("d-none", !toggle.checked);
      applyShowIfVisibility(questions);
    });
  });
  $$('[data-multiselect-option]', $("skillRunQuestions")).forEach((box) => {
    box.addEventListener("change", () => applyShowIfVisibility(questions));
  });
  $$('input[type="text"][data-question-id]', $("skillRunQuestions")).forEach((input) => {
    // A plain text question can drive a show_if too (e.g. {equals: "..."})
    // — re-evaluate on every keystroke, not just blur, so a dependent
    // section reveals itself immediately as the user types.
    input.addEventListener("input", () => applyShowIfVisibility(questions));
  });

  $$('[data-file-question]', $("skillRunQuestions")).forEach((container) => {
    wireFileQuestionInput(container, questions.find((q) => q.id === container.dataset.questionId));
  });

  applyShowIfVisibility(questions);
}

// Reads every question's current in-progress answer straight from the DOM
// (mirrors collectSkillAnswers' per-type logic, but doesn't require a full
// form submit) and toggles each show_if-bearing question's container
// (d-none) accordingly — the client-side mirror of show_if_met()
// (app/skills.py), so a hidden required question never blocks the Generate
// button and a shown one always reflects the live driver answer.
function applyShowIfVisibility(questions) {
  const liveAnswers = {};
  questions.forEach((q) => {
    if (q.type === "multiselect") {
      const checked = $$(`[data-multiselect-option="${q.id}"]:checked`, $("skillRunQuestions")).map((b) => b.value);
      const otherInput = document.querySelector(`[data-multiselect-other-input="${q.id}"]`);
      if (otherInput && !otherInput.classList.contains("d-none") && otherInput.value.trim()) {
        checked.push(otherInput.value.trim());
      }
      liveAnswers[q.id] = JSON.stringify(checked);
    } else if (q.type === "file") {
      liveAnswers[q.id] = pendingFileAnswers[q.id] ? "1" : "";
    } else {
      const el = document.querySelector(`[data-question-id="${q.id}"]`);
      liveAnswers[q.id] = el ? el.value : "";
    }
  });

  questions.forEach((q) => {
    if (!q.show_if) return;
    const container = document.querySelector(`[data-question-container="${q.id}"]`);
    if (!container) return;
    const shown = showIfMet(q.show_if, liveAnswers);
    container.classList.toggle("d-none", !shown);
  });
}

// JS mirror of app/skills.py's show_if_met — same two condition shapes
// ({includes: "..."} for a multiselect/text driver, {equals: "..."} for a
// select/text driver), kept in sync deliberately rather than fetched from
// the server, since this only ever gates client-side form visibility (the
// server independently re-validates via show_if_met on submit).
function showIfMet(showIf, answers) {
  const driverValue = (answers[showIf.question_id] || "").trim();
  if ("includes" in showIf) {
    let choices;
    try {
      const parsed = JSON.parse(driverValue);
      choices = Array.isArray(parsed) ? parsed : [driverValue];
    } catch {
      choices = [driverValue];
    }
    return choices.map(String).includes(String(showIf.includes));
  }
  if ("equals" in showIf) {
    return driverValue.trim().toLowerCase() === String(showIf.equals).trim().toLowerCase();
  }
  return true;
}

// Wires one type:"file" question's drop-zone: click-to-browse + drag/drop
// (same three-listener pattern as initDocModal/initSkillUploadModal), and
// uploads each chosen file immediately via
// POST /skill-packages/run/upload-answer-file — by-reference, per the
// confirmed design, so collectSkillAnswers only ever sends a file_id, never
// raw bytes, in the plain answers dict.
function wireFileQuestionInput(container, question) {
  const drop = container.querySelector("[data-file-drop]");
  const input = container.querySelector("[data-file-input]");
  const preview = container.querySelector("[data-file-preview]");
  const questionId = container.dataset.questionId;

  const uploadFiles = async (files) => {
    if (!files.length || !currentSkillRun) return;
    preview.classList.remove("d-none");
    preview.innerHTML = `<span class="small" style="color:var(--text-faint)"><span class="spinner-mini"></span> Uploading…</span>`;
    const fileIds = [];
    const names = [];
    try {
      for (const file of files) {
        const content = await readFileAsBase64(file);
        const result = await post("/api/skill-packages/run/upload-answer-file", {
          run_id: currentSkillRun.run_id, question_id: questionId, filename: file.name, content,
        });
        fileIds.push(result.file_id);
        names.push(file.name);
      }
      pendingFileAnswers[questionId] = { fileIds, names };
      preview.innerHTML = names.map((n) => `<span class="chip">📄 ${escapeHtml(n)}</span>`).join(" ");
    } catch (e) {
      delete pendingFileAnswers[questionId];
      preview.innerHTML = `<span class="small" style="color:var(--danger)">${escapeHtml(e.message)}</span>`;
    }
  };

  drop.addEventListener("click", () => input.click());
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("dragover"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("dragover"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("dragover");
    if (e.dataTransfer.files.length) uploadFiles(Array.from(e.dataTransfer.files));
  });
  input.addEventListener("change", (e) => { if (e.target.files.length) uploadFiles(Array.from(e.target.files)); });
}

function collectSkillAnswers() {
  const answers = {};
  const missing = [];
  $$('[data-question-id]', $("skillRunQuestions")).forEach((el) => {
    const id = el.dataset.questionId;
    // A show_if-hidden question (see applyShowIfVisibility) was never shown
    // or required to the user — mirror show_if_met's server-side skip so a
    // hidden required question never blocks Generate. Its answer still
    // collects whatever's there (usually empty), just never enters `missing`.
    const container = document.querySelector(`[data-question-container="${id}"]`) || el.closest("div");
    const hidden = container ? container.classList.contains("d-none") : false;

    if (el.dataset.multiselect === "true") {
      const checked = $$(`[data-multiselect-option="${id}"]:checked`, el).map((b) => b.value);
      const otherInput = el.querySelector(`[data-multiselect-other-input="${id}"]`);
      if (otherInput && !otherInput.classList.contains("d-none") && otherInput.value.trim()) {
        checked.push(otherInput.value.trim());
      }
      answers[id] = JSON.stringify(checked);
      if (!hidden && el.dataset.required === "true" && !checked.length) {
        missing.push(el.querySelector("label").textContent.replace("*", "").trim());
      }
      return;
    }
    if (el.dataset.fileQuestion === "true") {
      // Not an input's .value — the real upload already happened
      // (wireFileQuestionInput), this just reads back the file_id(s) it
      // staged. A single file_id for a plain file question; a JSON array of
      // file_ids for a "folder" (multi-file) question, matching what
      // SkillRunService.submit_answers expects server-side.
      const staged = pendingFileAnswers[id];
      const fileIds = staged ? staged.fileIds : [];
      answers[id] = !fileIds.length ? "" : (fileIds.length > 1 || el.querySelector("[data-file-input]")?.multiple)
        ? JSON.stringify(fileIds) : fileIds[0];
      if (!hidden && el.dataset.required === "true" && !fileIds.length) {
        missing.push(el.querySelector("label").textContent.replace("*", "").trim());
      }
      return;
    }
    let value = el.value;
    if (el.tagName === "SELECT" && value === "__other__") {
      const other = document.querySelector(`[data-other-for="${id}"]`);
      value = other ? other.value : "";
    }
    answers[id] = value.trim();
    if (!hidden && el.dataset.required === "true" && !answers[id]) {
      const label = el.closest("div").querySelector("label").textContent.replace("*", "").trim();
      missing.push(label);
    }
  });
  return { answers, missing };
}

async function openSkillRunModal(skillId) {
  try {
    const data = await post("/api/skill-packages/run/start", { skill_id: skillId });
    currentSkillRun = { run_id: data.run_id, skill: data.skill };
    $("skillRunModalTitle").textContent = `Run ${data.skill.name}`;
    renderSkillQuestions(data.skill.questions);
    $("skillRunResult").classList.add("d-none");
    $("skillRunProgress").classList.add("d-none");
    $("skillRunForm").classList.remove("d-none");
    $("skillRunSubmit").classList.remove("d-none");
    $("skillRunSubmit").disabled = false;
    $("skillRunSubmit").textContent = "Generate";
    skillRunModalEl().show();
  } catch (e) {
    toast("danger", "Couldn't start skill", e.message);
  }
}

async function triggerBlobDownload(url, body, fallbackFilename) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try { detail = (await res.json()).detail || detail; } catch { /* empty body */ }
    throw new Error(detail);
  }
  const blob = await res.blob();
  const disposition = res.headers.get("content-disposition") || "";
  const match = /filename="?([^";]+)"?/.exec(disposition);
  const filename = match ? match[1] : fallbackFilename;
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}

// Maps a skill's output type onto the shape of its editable content list —
// pptx slides (title + bullets) and docx sections (heading + paragraphs)
// share the same "title-like field + list-of-text-lines" structure, so the
// same editor UI drives both.
function specListConfig(skill) {
  return skill.output === "docx"
    ? { listKey: "sections", itemTitleKey: "heading", itemBodyKey: "paragraphs", itemLabel: "Section" }
    : { listKey: "slides", itemTitleKey: "title", itemBodyKey: "bullets", itemLabel: "Slide" };
}

function specEditorItemHtml(item, cfg, index) {
  return `
    <div class="p-2 mb-2" data-spec-item style="border:1px solid var(--border); border-radius:var(--radius-sm);">
      <div class="d-flex align-items-center justify-content-between mb-1">
        <span class="small fw-semibold">${escapeHtml(cfg.itemLabel)} ${index + 1}</span>
        <button type="button" class="btn btn-ghost btn-sm" data-action="remove-spec-item">Remove</button>
      </div>
      <input type="text" class="form-control form-control-sm mb-1" data-spec-item-title value="${escapeHtml(item[cfg.itemTitleKey] || "")}" placeholder="${escapeHtml(cfg.itemLabel)} title">
      <textarea class="form-control form-control-sm" data-spec-item-body rows="3" placeholder="One point per line">${escapeHtml((item[cfg.itemBodyKey] || []).join("\n"))}</textarea>
    </div>`;
}

function renderSpecEditor(run, skill) {
  const cfg = specListConfig(skill);
  const spec = run.spec || {};
  const items = spec[cfg.listKey] || [];
  $("skillRunResult").innerHTML = `
    <div id="skillSpecEditor">
      <div class="mb-2">
        <label class="form-label small">Title</label>
        <input type="text" class="form-control form-control-sm" id="specTitle" value="${escapeHtml(spec.title || "")}">
      </div>
      <div class="mb-2">
        <label class="form-label small">Subtitle</label>
        <input type="text" class="form-control form-control-sm" id="specSubtitle" value="${escapeHtml(spec.subtitle || "")}">
      </div>
      <div id="specItems">${items.map((item, i) => specEditorItemHtml(item, cfg, i)).join("")}</div>
      <button type="button" class="btn btn-outline-secondary btn-sm mb-2" id="specAddItem">+ Add ${escapeHtml(cfg.itemLabel.toLowerCase())}</button>
      <div class="d-flex gap-2">
        <button type="button" class="btn btn-primary btn-sm" id="specRegenerate">Regenerate</button>
        <button type="button" class="btn btn-outline-secondary btn-sm" id="specCancelEdit">Cancel</button>
      </div>
    </div>`;

  const renumber = () => {
    $$('[data-spec-item]', $("specItems")).forEach((el, i) => {
      el.querySelector(".fw-semibold").textContent = `${cfg.itemLabel} ${i + 1}`;
    });
  };
  $("specItems").addEventListener("click", (e) => {
    if (e.target.dataset.action === "remove-spec-item") {
      e.target.closest("[data-spec-item]").remove();
      renumber();
    }
  });
  $("specAddItem").addEventListener("click", () => {
    $("specItems").insertAdjacentHTML("beforeend", specEditorItemHtml({}, cfg, $$('[data-spec-item]', $("specItems")).length));
  });
  $("specCancelEdit").addEventListener("click", () => showSkillRunResult(run, skill));
  $("specRegenerate").addEventListener("click", async () => {
    const editedSpec = { title: $("specTitle").value.trim(), subtitle: $("specSubtitle").value.trim() };
    editedSpec[cfg.listKey] = $$('[data-spec-item]', $("specItems")).map((el) => ({
      [cfg.itemTitleKey]: el.querySelector("[data-spec-item-title]").value.trim(),
      [cfg.itemBodyKey]: el.querySelector("[data-spec-item-body]").value.split("\n").map((s) => s.trim()).filter(Boolean),
    })).filter((item) => item[cfg.itemTitleKey] || item[cfg.itemBodyKey].length);

    const btn = $("specRegenerate");
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-mini dark"></span>`;
    try {
      const updated = await post("/api/skill-packages/run/regenerate", { run_id: run.run_id, spec: editedSpec });
      if (currentSkillRun) currentSkillRun.lastRun = updated;
      toast("success", "Regenerated");
      showSkillRunResult(updated, skill);
    } catch (e) {
      toast("danger", "Regeneration failed", e.message);
      btn.disabled = false;
      btn.textContent = "Regenerate";
    }
  });
}

function showSkillRunResult(run, skill) {
  const result = $("skillRunResult");
  result.classList.remove("d-none");
  if (currentSkillRun) currentSkillRun.lastRun = run;
  if (run.status === "COMPLETED") {
    // One Download button per real file this run produced (run.outputs —
    // e.g. ["docx", "pdf"] for a MULTI_FORMAT_OUTPUT skill like
    // brd-prd-generator; every ordinary single-output skill's `outputs`
    // has exactly one entry, so this renders exactly one button, same as
    // before). Falls back to skill.output if `outputs` is ever absent
    // (defensive — every current build always sends it).
    const outputs = run.outputs?.length ? run.outputs : [skill.output || "bin"];
    const downloadButtons = outputs.map((ext) => `
      <button class="btn btn-primary btn-sm" data-skill-download-format="${escapeHtml(ext)}">
        Download .${escapeHtml(ext)}
      </button>`).join("");
    result.innerHTML = `
      <div class="d-flex align-items-center justify-content-between gap-2 p-3 flex-wrap" style="border:1px solid var(--border); border-radius:var(--radius-sm); background:var(--surface-2)">
        <div>
          <div class="fw-semibold">Generated ✓</div>
          <div class="small" style="color:var(--text-faint)">${escapeHtml(skill.name)}.${escapeHtml(outputs.join(" / "))}</div>
        </div>
        <div class="d-flex gap-2 flex-wrap">
          <button class="btn btn-outline-secondary btn-sm" id="skillViewEditBtn">View / Edit</button>
          ${downloadButtons}
        </div>
      </div>`;
    $$('[data-skill-download-format]', result).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const ext = btn.dataset.skillDownloadFormat;
        try {
          await triggerBlobDownload(
            "/api/skill-packages/run/download",
            { run_id: run.run_id, format: outputs.length > 1 ? ext : undefined },
            `${skill.name}.${ext}`,
          );
        } catch (e) {
          toast("danger", "Download failed", e.message);
        }
      });
    });
    $("skillViewEditBtn").addEventListener("click", () => renderSpecEditor(run, skill));
  } else {
    result.innerHTML = `
      <div class="p-3" style="border:1px solid var(--danger); border-radius:var(--radius-sm)">
        <div class="fw-semibold" style="color:var(--danger)">Generation failed</div>
        <div class="small mb-2" style="color:var(--text-faint)">${escapeHtml(run.error || "Unknown error")}</div>
        <button class="btn btn-outline-secondary btn-sm" id="skillRetryBtn">Try again</button>
      </div>`;
    $("skillRetryBtn").addEventListener("click", () => openSkillRunModal(run.skill_id));
  }
}

function initSkillRunModal() {
  $("skillRunForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!currentSkillRun) return;
    const { answers, missing } = collectSkillAnswers();
    if (missing.length) {
      toast("warning", "Missing answers", missing.join(", "));
      return;
    }

    const { run_id, skill } = currentSkillRun;
    const stages = ["Reviewing answers", `Drafting ${skill.output === "docx" ? "sections" : "slides"}`, `Generating .${skill.output}`];
    const btn = $("skillRunSubmit");
    const cancelBtn = $("skillRunCancelBtn");
    const closeBtn = $("skillRunCloseBtn");
    btn.disabled = true; cancelBtn.disabled = true; closeBtn.disabled = true;
    $("skillRunForm").classList.add("d-none");
    const progress = startProgress("skillRunProgress", "skillRunProgressSteps", stages);
    try {
      const run = await post("/api/skill-packages/run/answer", { run_id, answers });
      progress.stop(run.status === "COMPLETED");
      showSkillRunResult(run, skill);
      btn.classList.add("d-none");
    } catch (e) {
      progress.stop(false);
      toast("danger", "Generation failed", e.message);
      showSkillRunResult({ status: "FAILED", error: e.message, skill_id: skill.skill_id, run_id }, skill);
      btn.classList.add("d-none");
    } finally {
      cancelBtn.disabled = false; closeBtn.disabled = false;
    }
  });
}

function initSkillsView() {
  $("refreshSkills").addEventListener("click", loadSkillPackages);
  initSkillUploadModal();
  initSkillRunModal();
}

function initDocSearch() {
  $("docSearch").addEventListener("input", renderDocuments);
  $("docStatusFilter").addEventListener("change", renderDocuments);
  $("refreshDocs").addEventListener("click", loadDocuments);
}

/* ==========================================================================
   Agents & Skills
   ========================================================================== */

async function loadAgentsAndSkills() {
  try {
    const [agentsData, skillsData] = await Promise.all([
      post("/api/agents/list"), post("/api/skills/list"),
    ]);
    renderAgents(agentsData.agents || []);
    renderSkills(skillsData.skills || []);
  } catch (e) {
    $("agentsContainer").innerHTML = `<div class="state-block state-error"><div class="state-icon">⚠</div><h3>Couldn't load agents</h3><p>${escapeHtml(e.message)}</p></div>`;
  }
}

function renderAgents(agents) {
  $("agentsContainer").innerHTML = `
    <div class="table-responsive">
      <table class="data-table">
        <thead><tr><th>Agent</th><th>Triggers on</th><th>Skills</th></tr></thead>
        <tbody>
          ${agents.map((a) => `
            <tr>
              <td><span class="row-title">${escapeHtml(a.name)}</span><div class="row-sub">${escapeHtml(a.purpose)}</div></td>
              <td>${a.trigger_keywords?.length ? a.trigger_keywords.map((k) => `<span class="chip me-1">${escapeHtml(k)}</span>`).join("") : `<span class="row-sub">default (direct call)</span>`}</td>
              <td>${a.skills?.length ? a.skills.map((s) => `<span class="chip me-1">${escapeHtml(s)}</span>`).join("") : "—"}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function renderSkills(skills) {
  $("registeredSkillsContainer").innerHTML = `
    <div class="table-responsive">
      <table class="data-table">
        <thead><tr><th>Skill</th><th>Trigger</th><th>Workflow</th></tr></thead>
        <tbody>
          ${skills.map((s) => `
            <tr>
              <td class="row-title">${escapeHtml(s.name)}</td>
              <td class="row-sub">${escapeHtml(s.trigger)}</td>
              <td class="row-sub">${escapeHtml(s.workflow)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

/* ==========================================================================
   HITL
   ========================================================================== */

const HITL_PILL = {
  WAITING_FOR_APPROVAL: "pill-warning",
  APPROVED: "pill-info",
  COMPLETED: "pill-success",
  REJECTED: "pill-danger",
};

async function loadHitlRequests() {
  try {
    const data = await post("/api/hitl/list");
    const items = [...data.requests].reverse();
    const pending = items.filter((r) => r.status === "WAITING_FOR_APPROVAL");

    $("pendingCountLabel").textContent = String(pending.length);
    const badge = $("hitlNavBadge");
    if (pending.length) { badge.textContent = String(pending.length); badge.classList.remove("d-none"); }
    else badge.classList.add("d-none");

    $("pendingHitlPanel").innerHTML = pending.length
      ? pending.map((r) => hitlCardHtml(r)).join("")
      : `<div class="state-block" style="padding:28px 16px;"><p style="margin:0;">Nothing waiting on you right now.</p></div>`;

    $("hitlRequests").innerHTML = items.length
      ? items.map((r) => hitlCardHtml(r)).join("")
      : `<p class="small mb-0" style="color:var(--text-faint)">No requests yet.</p>`;

    // Scoped to just the two containers rendered above (not a document-wide
    // $$) — the inline chat cards (hitlCardsHtml) also use [data-hitl-action]
    // and wire their own row-scoped listeners in renderMessage; a document-
    // wide selector here would double-attach to those and fire decideHitl twice.
    [$("pendingHitlPanel"), $("hitlRequests")].forEach((container) => {
      $$('[data-hitl-action]', container).forEach((btn) => {
        btn.addEventListener("click", () => decideHitl(btn.dataset.id, btn.dataset.hitlAction === "approve"));
      });
    });
  } catch (e) {
    $("hitlRequests").innerHTML = `<p class="small mb-0" style="color:var(--danger)">Couldn't load the approval queue: ${escapeHtml(e.message)}</p>`;
  }
}

// Fetches the full records for this turn's newly-queued HITL request ids
// (ChatResponse.hitl_pending only carries ids, see app/models.py) and stores
// them on the assistant message so renderMessage can show an actionable
// approve/reject card inline, right in the transcript, instead of the user
// having to notice a toast and go find it in the Agents & Tools tab.
async function attachHitlCardsToMessage(messageIdx, requestIds) {
  try {
    const data = await post("/api/hitl/list");
    const byId = new Map(data.requests.map((r) => [r.request_id, r]));
    const records = requestIds.map((id) => byId.get(id)).filter(Boolean);
    if (messages[messageIdx]) messages[messageIdx].hitlRecords = records;
    renderChat();
  } catch (e) {
    // Non-fatal: the side panel / Agents & Tools tab still show the request.
  }
}

// After a decision is recorded (from an inline chat card OR the Agents &
// Tools queue), re-fetch and refresh any chat message currently showing that
// request so its inline card reflects the new status/result too.
async function refreshHitlRecordsInChat(requestId) {
  const touched = messages.some((m) => m.hitlRecords?.some((r) => r.request_id === requestId));
  if (!touched) return;
  try {
    const record = await post("/api/hitl/get", { request_id: requestId });
    messages.forEach((m) => {
      if (!m.hitlRecords) return;
      m.hitlRecords = m.hitlRecords.map((r) => (r.request_id === requestId ? record : r));
    });
    renderChat();
  } catch (e) {
    // Non-fatal: the side panel / Agents & Tools tab already reflect it.
  }
}

// Inline approval block for a chat message (see attachHitlCardsToMessage) —
// reuses hitlCardHtml so the card looks/behaves identically to the Agents &
// Tools queue, just surfaced where the user is already looking.
function hitlCardsHtml(records) {
  if (!records?.length) return "";
  return `
    <div class="hitl-inline-cards mt-2">
      <div class="small mb-1" style="color:var(--warning); font-weight:600;">⏳ Needs your approval</div>
      ${records.map((r) => hitlCardHtml(r)).join("")}
    </div>`;
}

function hitlCardHtml(r) {
  const pillClass = HITL_PILL[r.status] || "pill-neutral";
  return `
    <div class="panel mb-2" style="box-shadow:none;">
      <div class="panel-body">
        <div class="d-flex justify-content-between align-items-start gap-2 mb-2">
          <span class="pill ${pillClass}"><span class="pill-dot"></span>${escapeHtml(r.status.replace(/_/g, " "))}</span>
          <span class="row-sub" style="margin:0;">${timeAgo(r.created_at)}</span>
        </div>
        <pre class="code-block mb-2">${escapeHtml(r.code)}</pre>
        ${r.status === "WAITING_FOR_APPROVAL" ? `
          <div class="d-flex gap-2">
            <button class="btn btn-sm btn-primary flex-grow-1" data-hitl-action="approve" data-id="${r.request_id}">Approve &amp; run</button>
            <button class="btn btn-sm btn-outline-danger flex-grow-1" data-hitl-action="reject" data-id="${r.request_id}">Reject</button>
          </div>` : ""}
        ${r.result ? `<div class="row-sub mb-1">Result</div><pre class="code-block mb-0">${escapeHtml(JSON.stringify(r.result, null, 2))}</pre>` : ""}
      </div>
    </div>`;
}

async function decideHitl(requestId, approved) {
  try {
    await post("/api/hitl/decide", { request_id: requestId, approved });
    toast(approved ? "success" : "info", approved ? "Approved & executed" : "Request rejected");
    await loadHitlRequests();
    await refreshHitlRecordsInChat(requestId);
  } catch (e) {
    toast("danger", "Couldn't record decision", e.message);
  }
}

function initHitl() {
  $("codeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = $("codeInput").value;
    const btn = e.target.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      await post("/api/tools/code/submit", { code, session_id: sessionId });
      $("codeInput").value = "";
      toast("info", "Queued for approval", "Review it in the approval queue below.");
      await loadHitlRequests();
    } catch (err) {
      toast("danger", "Couldn't submit code", err.message);
    } finally {
      btn.disabled = false;
    }
  });
  $("refreshHitl").addEventListener("click", loadHitlRequests);
}

/* ==========================================================================
   Settings view
   ========================================================================== */

function renderSettingsProvider(data) {
  if (!data) return;
  const pillClass = !data.provider ? "pill-neutral" : data.used_fallback ? "pill-warning" : "pill-success";
  const providerModel = data.model ? `${data.provider}/${data.model}` : data.provider;
  const label = data.provider ? `${providerModel}${data.used_fallback ? " (fallback)" : ""}` : "no model call";
  $("settingsProvider").innerHTML = `
    <div class="settings-row">
      <div>
        <div class="settings-label">Provider</div>
        <div class="settings-desc">From the most recent chat response.</div>
      </div>
      <span class="pill ${pillClass}"><span class="pill-dot"></span>${escapeHtml(label)}</span>
    </div>`;
}

// RAG embedding status (see app/routes.py POST /rag/status): "configured" is
// static (from settings, no network call); "last_used" is what actually
// embedded the most recent ingest/query this session — they can differ if a
// configured provider fell back.
// Options for a <select>, sourced from the live /api/models/list catalog
// (never a hardcoded guess — model names go stale). `current` is always
// included even if the catalog doesn't have it (e.g. a value set before the
// catalog loaded, or a provider that's momentarily unreachable) so the
// dropdown never silently drops the user's actual configured value.
function _modelOptionsHtml(catalogModels, provider, current) {
  const names = catalogModels.filter((m) => m.provider === provider).map((m) => m.model);
  if (current && !names.includes(current)) names.unshift(current);
  if (!names.length) return `<option value="">(none available)</option>`;
  return names.map((name) => `<option value="${escapeHtml(name)}" ${name === current ? "selected" : ""}>${escapeHtml(name)}</option>`).join("");
}

async function renderSettingsModels() {
  const el = $("settingsModels");
  if (!el) return;
  try {
    const [cfg, catalog, embedCatalog] = await Promise.all([
      post("/api/settings/models", {}),
      post("/api/models/list", {}).catch(() => ({ models: [] })),
      // Ollama's side of this scans every pulled model's real capabilities
      // (no shortcut in Ollama's API — see app/providers.py), so this can
      // take a couple seconds; still shown alongside everything else rather
      // than blocking the rest of the panel on it.
      post("/api/models/list/embedding", {}).catch(() => ({ models: [], errors: [] })),
    ]);
    const models = catalog.models || [];
    const embedModels = embedCatalog.models || [];
    const embedErrors = embedCatalog.errors || [];
    const ollamaEmbedError = embedErrors.find((e) => e.provider === "ollama")?.message;
    const geminiWarn = cfg.gemini_configured ? "" : ` <span style="color:var(--warning)">— no GEMINI_API_KEY, .env only</span>`;
    const ollamaWarn = cfg.ollama_configured ? "" : ` <span style="color:var(--warning)">— no OLLAMA_BASE_URL, .env only</span>`;

    el.innerHTML = `
      <div class="settings-row">
        <div>
          <div class="settings-label">Provider</div>
          <div class="settings-desc">Default chat-completion provider (MODEL_PROVIDER).</div>
        </div>
        <select class="form-select" id="modelsProvider" style="max-width:220px;">
          <option value="gemini" ${cfg.model_provider === "gemini" ? "selected" : ""}>Gemini${geminiWarn}</option>
          <option value="ollama" ${cfg.model_provider === "ollama" ? "selected" : ""}>Ollama${ollamaWarn}</option>
        </select>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">Chat model — Gemini</div>
          <div class="settings-desc">Used when Provider is Gemini. Empty = provider default.</div>
        </div>
        <select class="form-select" id="modelsGeminiChat" style="max-width:280px;" ${cfg.gemini_configured ? "" : "disabled"}>
          <option value="">(provider default)</option>
          ${_modelOptionsHtml(models, "gemini", cfg.gemini_model)}
        </select>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">Chat model — Ollama</div>
          <div class="settings-desc">Used when Provider is Ollama.</div>
        </div>
        <select class="form-select" id="modelsOllamaChat" style="max-width:280px;" ${cfg.ollama_configured ? "" : "disabled"}>
          ${_modelOptionsHtml(models, "ollama", cfg.ollama_model)}
        </select>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">Agent router model</div>
          <div class="settings-desc">Classifies which agent handles an agent-mode turn — always calls Gemini regardless of Provider above. See docs/agent-routing.md.</div>
        </div>
        <select class="form-select" id="modelsRouter" style="max-width:280px;" ${cfg.gemini_configured ? "" : "disabled"}>
          ${_modelOptionsHtml(models, "gemini", cfg.agent_router_model)}
        </select>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">RAG embedding — Gemini</div>
          <div class="settings-desc">Used when Provider is Gemini (or as fallback when Ollama's embedder is unavailable).</div>
        </div>
        <select class="form-select" id="modelsGeminiEmbed" style="max-width:280px;" ${cfg.gemini_configured ? "" : "disabled"}>
          ${_modelOptionsHtml(embedModels, "gemini", cfg.gemini_embedding_model)}
        </select>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">RAG embedding — Ollama</div>
          <div class="settings-desc">${ollamaEmbedError ? `<span style="color:var(--warning)">${escapeHtml(ollamaEmbedError)}</span>` : "Used when Provider is Ollama."}</div>
        </div>
        <select class="form-select" id="modelsOllamaEmbed" style="max-width:280px;" ${cfg.ollama_configured ? "" : "disabled"}>
          ${_modelOptionsHtml(embedModels, "ollama", cfg.ollama_embedding_model)}
        </select>
      </div>
      <div class="d-flex justify-content-end gap-2 pt-1">
        <span class="small align-self-center" id="modelsSaveStatus" style="color:var(--text-faint)"></span>
        <button class="btn btn-primary btn-sm" id="modelsSaveBtn">Save &amp; apply</button>
      </div>`;

    $("modelsSaveBtn").addEventListener("click", async () => {
      const btn = $("modelsSaveBtn");
      const status = $("modelsSaveStatus");
      btn.disabled = true;
      status.textContent = "Applying…";
      try {
        const updated = await post("/api/settings/models/update", {
          model_provider: $("modelsProvider").value,
          gemini_model: $("modelsGeminiChat").value,
          ollama_model: $("modelsOllamaChat").value,
          agent_router_model: $("modelsRouter").value,
          gemini_embedding_model: $("modelsGeminiEmbed").value,
          ollama_embedding_model: $("modelsOllamaEmbed").value,
        });
        status.textContent = "Applied — takes effect on the next request.";
        toast("success", "Model settings updated", `Provider: ${updated.model_provider}`);
      } catch (err) {
        status.textContent = "";
        toast("danger", "Couldn't update model settings", err.message);
      } finally {
        btn.disabled = false;
        setTimeout(() => { if (status) status.textContent = ""; }, 4000);
      }
    });
  } catch (err) {
    el.innerHTML = `<div class="settings-desc" style="color:var(--danger)">Couldn't load model settings: ${escapeHtml(err.message)}</div>`;
  }
}

async function renderEmbeddingStatus() {
  const el = $("settingsEmbedding");
  if (!el) return;
  try {
    const data = await post("/api/rag/status", {});
    const configured = data.configured;
    const configuredLabel = configured.model ? `${configured.provider}/${configured.model}` : configured.provider;
    const lastUsed = data.last_used;
    const lastUsedLabel = lastUsed
      ? `${lastUsed.model ? `${lastUsed.provider}/${lastUsed.model}` : lastUsed.provider}${lastUsed.used_fallback ? " (fallback)" : ""}`
      : "not used yet this session";
    el.innerHTML = `
      <div class="settings-row">
        <div>
          <div class="settings-label">Embedding (configured)</div>
          <div class="settings-desc">RAG ingestion/search embedder — chain: ${escapeHtml(configured.fallback_chain.join(" → "))}</div>
        </div>
        <span class="pill pill-neutral"><span class="pill-dot"></span>${escapeHtml(configuredLabel)}</span>
      </div>
      <div class="settings-row">
        <div>
          <div class="settings-label">Embedding (last used)</div>
          <div class="settings-desc">What actually ran the most recent ingest/search.</div>
        </div>
        <span class="pill ${lastUsed?.used_fallback ? "pill-warning" : "pill-success"}"><span class="pill-dot"></span>${escapeHtml(lastUsedLabel)}</span>
      </div>`;
  } catch (err) {
    el.innerHTML = `<div class="settings-desc" style="color:var(--danger)">Couldn't load embedding status: ${escapeHtml(err.message)}</div>`;
  }
}

// MCP tool servers (see app/routes.py POST /tools/mcp/status): static config
// for both the local stdio server and the optional remote server, plus
// whichever tools the most recent load actually found (null until the first
// agent-mode chat turn that needed them — this endpoint never loads
// anything itself).
async function renderMcpStatus() {
  const el = $("settingsMcp");
  if (!el) return;
  try {
    const data = await post("/api/tools/mcp/status", {});
    const rows = [];
    const stdioLabel = data.stdio.enabled ? `${data.stdio.command} ${data.stdio.args}` : "disabled";
    rows.push(`
      <div class="settings-row">
        <div>
          <div class="settings-label">Local (stdio)</div>
          <div class="settings-desc text-mono">${escapeHtml(stdioLabel)}</div>
        </div>
        <span class="pill ${data.stdio.enabled ? "pill-success" : "pill-neutral"}"><span class="pill-dot"></span>${data.stdio.enabled ? "enabled" : "disabled"}</span>
      </div>`);
    const remoteLabel = data.remote.configured ? `${data.remote.url} (${data.remote.transport})` : "not configured";
    rows.push(`
      <div class="settings-row">
        <div>
          <div class="settings-label">Remote</div>
          <div class="settings-desc text-mono">${escapeHtml(remoteLabel)}</div>
        </div>
        <span class="pill ${data.remote.configured ? "pill-success" : "pill-neutral"}"><span class="pill-dot"></span>${data.remote.configured ? "configured" : "not configured"}</span>
      </div>`);
    rows.push(`
      <div class="settings-row">
        <div>
          <div class="settings-label">🔎 Web Search (Tavily)</div>
          <div class="settings-desc">${data.web_search.configured ? "TAVILY_API_KEY set — offered when the Web Search toggle is on in agent mode." : "Set TAVILY_API_KEY in .env to enable."}</div>
        </div>
        <span class="pill ${data.web_search.configured ? "pill-success" : "pill-neutral"}"><span class="pill-dot"></span>${data.web_search.configured ? "configured" : "not configured"}</span>
      </div>`);
    updateWebSearchToggleAvailability(data.web_search.configured);
    if (data.last_loaded) {
      const toolNames = [
        ...(data.last_loaded.stdio?.tools || []),
        ...(data.last_loaded.remote?.tools || []),
        ...(data.last_loaded.web_search?.tools || []),
      ];
      const failed = [data.last_loaded.stdio, data.last_loaded.remote, data.last_loaded.web_search].filter((s) => s?.configured && !s.ok);
      rows.push(`
        <div class="settings-row">
          <div>
            <div class="settings-label">Loaded tools</div>
            <div class="settings-desc">${toolNames.length ? escapeHtml(toolNames.join(", ")) : "none"}${failed.length ? ` — ${failed.map((f) => escapeHtml(f.error || "failed")).join("; ")}` : ""}</div>
          </div>
          <span class="pill ${failed.length ? "pill-warning" : "pill-success"}"><span class="pill-dot"></span>${data.tool_count} tool(s)</span>
        </div>`);
    } else {
      rows.push(`<p class="small mb-0" style="color:var(--text-faint)">Not loaded yet — loads on the first agent-mode chat turn that needs a tool.</p>`);
    }
    el.innerHTML = rows.join("");
  } catch (err) {
    el.innerHTML = `<div class="settings-desc" style="color:var(--danger)">Couldn't load MCP status: ${escapeHtml(err.message)}</div>`;
  }
}

// Langfuse tracing (see app/routes.py POST /observability/status) — enabled
// only when both LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are configured;
// never shows the secret key itself.
async function renderObservabilityStatus() {
  const el = $("settingsObservability");
  if (!el) return;
  try {
    const data = await post("/api/observability/status", {});
    el.innerHTML = `
      <div class="settings-row">
        <div>
          <div class="settings-label">Langfuse tracing</div>
          <div class="settings-desc">${data.enabled ? escapeHtml(data.host) : "Set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY to enable."}</div>
        </div>
        <span class="pill ${data.enabled ? "pill-success" : "pill-neutral"}"><span class="pill-dot"></span>${data.enabled ? "enabled" : "disabled"}</span>
      </div>`;
  } catch (err) {
    el.innerHTML = `<div class="settings-desc" style="color:var(--danger)">Couldn't load observability status: ${escapeHtml(err.message)}</div>`;
  }
}

function renderSettingsView() {
  $("settingsSessionId").textContent = sessionId || "none yet";
  if (lastMeta) renderSettingsProvider(lastMeta);
  renderSettingsModels();
  renderEmbeddingStatus();
  renderMcpStatus();
  renderObservabilityStatus();
}

/* ==========================================================================
   Init
   ========================================================================== */

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initNav();
  initCmdk();
  initChat();
  initDocModal();
  initDocViewModal();
  initDeleteConfirm();
  initDocSearch();
  initHitl();
  initSkillsView();

  syncSessionChrome();
  syncWebSearchToggle();
  loadGuardrails();
  loadHitlRequests();
  restoreActiveSession();
  // Cheap/no-op-safe (see describe_mcp_config's docstring) — just resolves
  // whether the Web Search toggle should be enabled without waiting for the
  // Settings tab to be opened first.
  post("/api/tools/mcp/status", {}).then((data) => updateWebSearchToggleAvailability(data.web_search.configured)).catch(() => {});
});
