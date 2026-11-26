"use strict";

const KIND_LABELS = {
  page: "Page", git: "Repo", "git-author": "Author", pdf: "PDF", youtube: "YouTube",
  "youtube-channel": "Channel", video: "Talk", crawl: "Crawl", import_file: "Upload", other: "Other",
  update_repos: "Repos",
};
const ENTRY_KIND_LABELS = { page: "Pages", git: "Repos", pdf: "PDFs", youtube: "YouTube", video: "Talks", other: "Other" };

const $ = (sel) => document.querySelector(sel);
let token = safeGet("saver-token") || "";
let config = {};
const expanded = new Map(); // job id -> last log id
let pollTimer = null;

function safeGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function safeSet(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* ------------------------------------------------------------------ API */
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    await askToken();
    return api(path, options);
  }
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.detail ? JSON.stringify(data.detail) : `HTTP ${response.status}`);
  return data;
}

function askToken() {
  return new Promise((resolve) => {
    const dialog = $("#token-dialog");
    if (dialog.open) { dialog.addEventListener("close", resolve, { once: true }); return; }
    $("#token-input").value = "";
    dialog.addEventListener("close", () => {
      token = $("#token-input").value.trim();
      safeSet("saver-token", token);
      resolve();
    }, { once: true });
    dialog.showModal();
  });
}

/* ------------------------------------------------------------ formatting */
// The server's timestamps are naive local time in *its* time zone, which the
// browser parses as its own. Measuring them against the server's clock (sent
// with every poll, parsed the same way) cancels the zone and any clock drift.
let serverOffset = 0;
const hasZone = (iso) => /(Z|[+-]\d\d:?\d\d)$/i.test(iso);
const nowFor = (iso) => Date.now() + (hasZone(iso) ? 0 : serverOffset);

function ago(iso) {
  if (!iso) return "";
  const seconds = (nowFor(iso) - new Date(iso).getTime()) / 1000;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 86400 * 7) return `${Math.round(seconds / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function duration(startIso, endIso) {
  if (!startIso) return "";
  const end = endIso ? new Date(endIso).getTime() : nowFor(startIso);
  const seconds = Math.max(0, (end - new Date(startIso).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

const fmt = (n) => (n ?? 0).toLocaleString();

function blobFolderLink(entry) {
  const st = config.storage;
  if (!st?.enabled || !["git", "youtube", "video"].includes(entry.kind) || !entry.note?.startsWith("Dumps/")) return null;
  const rel = entry.note.slice("Dumps/".length).split("/").map(encodeURIComponent).join("/");
  return el("a", { class: "files-link", href: `/blobs/${rel}/`, target: "_blank", rel: "noopener" },
    entry.kind === "git" ? "Files" : "Watch / files");
}

function obsidianLink(path) {
  if (!config.vault_name || !path) return null;
  return `obsidian://open?vault=${encodeURIComponent(config.vault_name)}&file=${encodeURIComponent(path)}`;
}

/* ------------------------------------------------------------- dashboard */
function renderStats(stats) {
  const e = stats.entries;
  $("#stat-total").textContent = fmt(e.total);
  $("#stat-last").textContent = stats.last_saved_at ? `last ${ago(stats.last_saved_at)}` : "";
  $("#stat-today").textContent = fmt(e.today);
  $("#stat-week").textContent = fmt(e.week);
  $("#stat-queue").textContent = fmt(stats.jobs.queued + stats.jobs.running);
  $("#stat-running").textContent = stats.jobs.running ? `${stats.jobs.running} running` : "";
  $("#stat-failed").textContent = fmt(stats.jobs.failed_24h);

  renderChart(stats.per_day);
  renderBars($("#kinds"), Object.entries(e.by_kind)
    .sort((a, b) => b[1] - a[1])
    .map(([kind, n]) => [ENTRY_KIND_LABELS[kind] || kind, n]));
  renderBars($("#areas"), stats.top_areas.map((a) => [a.area, a.count]));
}

function renderChart(days) {
  const chart = $("#chart");
  const max = Math.max(1, ...days.map((d) => d.count));
  const width = 300, height = 120, gap = 2;
  const barWidth = (width - gap * (days.length - 1)) / days.length;
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("preserveAspectRatio", "none");
  days.forEach((day, i) => {
    const h = day.count ? Math.max(3, (day.count / max) * (height - 4)) : 2;
    const rect = document.createElementNS(ns, "rect");
    rect.setAttribute("x", i * (barWidth + gap));
    rect.setAttribute("y", height - h);
    rect.setAttribute("width", barWidth);
    rect.setAttribute("height", h);
    rect.setAttribute("rx", 1.5);
    if (!day.count) rect.setAttribute("class", "zero");
    const title = document.createElementNS(ns, "title");
    title.textContent = `${new Date(day.date).toLocaleDateString(undefined, { month: "short", day: "numeric" })}: ${day.count}`;
    rect.append(title);
    svg.append(rect);
  });
  const label = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  chart.replaceChildren(svg, el("div", { class: "axis" },
    el("span", {}, label(days[0].date)), el("span", {}, `peak ${max}`), el("span", {}, "today")));
}

function renderBars(list, rows) {
  const max = Math.max(1, ...rows.map((r) => r[1]));
  list.replaceChildren(...(rows.length ? rows.map(([name, n]) => el("li", {},
    el("span", {}, name), el("span", { class: "n" }, fmt(n)),
    el("span", { class: "track" }, el("span", { style: `width:${(n / max) * 100}%` }))))
    : [el("li", { class: "muted" }, "Nothing yet")]));
}

function renderStatus(state) {
  const pill = $("#status-pill");
  const w = state.whisper;
  if (state.paused) { pill.className = "pill warn"; pill.textContent = "Paused"; }
  else if (state.vault_lock.lease_held) { pill.className = "pill warn"; pill.textContent = "Vault locked by maintenance"; }
  else { pill.className = "pill ok"; pill.textContent = state.stats.jobs.running ? `Working · ${state.stats.jobs.running}/${state.workers}` : "Idle"; }
  const pauseBtn = $("#pause-btn");
  pauseBtn.hidden = false;
  pauseBtn.textContent = state.paused ? "Resume" : "Pause";
  pauseBtn.dataset.paused = state.paused ? "1" : "";

  const info = [
    ["Vault", state.config.vault],
    ["LLM", `${state.config.llm_model} (${PROVIDER_LABELS[state.config.llm_provider] || state.config.llm_provider})`],
    ["Whisper", w.enabled ? `${w.model}${w.loaded ? ` · loaded (${w.device})` : " · idle"}${w.busy ? " · busy" : ""}` : "disabled"],
    ["Cookies", cookieSummary(state.config.cookies)],
    ["Inbox", state.config.inbox_watch ? "watching singlefile_inbox" : "off"],
    ["Waypoints", waypointSummary(state.waypoints)],
    ["Version", state.version],
  ];
  $("#whisper-state").textContent = !w.enabled ? "off"
    : `${w.model}${w.loaded ? ` · loaded on ${w.device}` : " · not loaded"}${w.busy ? " · transcribing" : ""}`;
  $("#service-info").replaceChildren(...info.flatMap(([k, v]) => [
    el("dt", {}, k), el("dd", { title: v, class: String(v).startsWith("⚠") ? "warn" : null }, v)]));
}

/* --------------------------------------------------------------- storage */
function renderStorage(st) {
  const box = $("#storage-status");
  const line = (cls, text) => el("div", { class: `line ${cls}` }, el("span", { class: "dot" }), el("span", {}, text));
  $("#storage-actions").hidden = !st?.enabled;
  $("#storage-link").hidden = !st?.enabled;
  if (!st) return;
  if (st.problem) {
    box.replaceChildren(line("bad", st.problem));
  } else if (!st.enabled) {
    box.replaceChildren(line("warn", "Off: videos and clones go into the vault. Set BLOB_DIR and PUBLIC_URL to store them offsite."));
  } else {
    const lines = [line("ok", `Offsite store at ${st.blob_dir}`)];
    const u = st.usage;
    if (u?.error) lines.push(line("warn", `Could not measure the store: ${u.error}`));
    else if (u?.bytes != null) lines.push(line("", `Holds ${formatBytes(u.bytes)}${u.counting ? " · recounting…" : ""}`));
    else if (u?.counting) lines.push(line("", "Measuring the store…"));
    if (st.free != null) lines.push(line("", `${formatBytes(st.free)} free of ${formatBytes(st.total)}`));
    lines.push(line("", `Links use ${st.public_url}`));
    box.replaceChildren(...lines);
  }
  renderStorageUsage(st.enabled && !st.problem ? st.usage : null);
}

const STORE_CATEGORIES = [["youtube", "YouTube", "video"], ["video", "Talks", "video"], ["git", "Repos", "repo"]];

// Per-category sizes of the blob store, bars relative to the biggest.
function renderStorageUsage(u) {
  const list = $("#storage-usage");
  const rows = u?.categories ? STORE_CATEGORIES.map(([key, label, noun]) => {
    const c = u.categories[key] || { bytes: 0, items: 0 };
    return [`${label} · ${fmt(c.items)} ${noun}${c.items === 1 ? "" : "s"}`, c.bytes];
  }).filter(([, bytes], i) => bytes || STORE_CATEGORIES[i][0] !== "video") : [];
  if (u?.other_bytes) rows.push(["Other", u.other_bytes]);
  list.hidden = !rows.length;
  const max = Math.max(1, ...rows.map((r) => r[1]));
  list.replaceChildren(...rows.map(([name, bytes]) => el("li", {},
    el("span", {}, name), el("span", { class: "n" }, formatBytes(bytes)),
    el("span", { class: "track" }, el("span", { style: `width:${(bytes / max) * 100}%` })))));
}

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function updateRepos() {
  try {
    const res = await api("/api/storage/update-repos", { method: "POST" });
    showResults([{ url: "Update repos", status: res.status, job_id: res.job_id }]);
  } catch (err) {
    alert(err.message);
  }
  poll();
}

function setupStorage() {
  $("#repos-update").addEventListener("click", updateRepos);
}

/* --------------------------------------------------------------- cookies */
function waypointSummary(w) {
  if (!w?.managed) return w?.plugin_enabled ? "Obsidian plugin (not managed here)" : "not managed";
  if (w.plugin_enabled) return "⚠ the Waypoint plugin is also on in Obsidian - turn it off there, or both write and sync conflicts follow";
  return w.notes_written ? `managed · ${w.notes_written} updated` : "managed";
}

function cookieSummary(c) {
  if (!c?.present) return "none (YouTube may refuse)";
  if (c.error) return "unreadable";
  return `${fmt(c.count)} cookies${c.logged_in ? " · signed in" : ""}`;
}

function inDays(epochSeconds) {
  const days = Math.round((epochSeconds * 1000 - Date.now()) / 86400000);
  return days >= 0 ? `in ${days} d` : `${-days} d ago`;
}

function renderCookies(c) {
  const box = $("#cookie-status");
  const line = (cls, text) => el("div", { class: `line ${cls}` }, el("span", { class: "dot" }), el("span", {}, text));
  $("#cookie-remove").hidden = !c?.present;
  if (!c?.present) {
    box.replaceChildren(line("warn", "No cookies. YouTube may answer \u201cSign in to confirm you\u2019re not a bot\u201d."));
    return;
  }
  if (c.error) {
    box.replaceChildren(line("bad", `Cookie file unreadable: ${c.error}`));
    return;
  }
  const lines = [line("", `${fmt(c.count)} cookies for ${fmt(c.domain_count)} domain${c.domain_count === 1 ? "" : "s"} · updated ${ago(new Date(c.updated_at * 1000).toISOString())}`)];
  if (!c.youtube) lines.push(line("warn", "No youtube.com cookies in this file"));
  else if (c.logged_in) lines.push(line("ok", `Signed in to YouTube${c.auth_expires ? ` · login expires ${inDays(c.auth_expires)}` : ""}`));
  else lines.push(line("warn", "YouTube cookies, but no (valid) login - age-restricted or bot-checked videos may fail"));
  if (c.expired) lines.push(line("warn", `${fmt(c.expired)} expired cookie${c.expired === 1 ? "" : "s"}`));
  if (!c.valid_header) lines.push(line("bad", "Missing the Netscape header - re-upload the file"));
  box.replaceChildren(...lines);
}

// A message under a form that hides itself again after a while.
function flash(selector, text, isError = false) {
  const msg = $(selector);
  msg.textContent = text;
  msg.classList.toggle("error", isError);
  msg.hidden = false;
  clearTimeout(msg.timer);
  msg.timer = setTimeout(() => { msg.hidden = true; }, isError ? 15000 : 6000);
}

const cookieMessage = (text, isError = false) => flash("#cookie-message", text, isError);

function errorDetail(err) {
  try { return JSON.parse(err.message); } catch { return err.message; }
}

async function sendCookies(form) {
  try {
    const status = await api("/api/cookies", { method: "POST", body: form });
    renderCookies(status);
    cookieMessage(`Saved ${fmt(status.count)} cookies.`);
    $("#cookie-form").hidden = true;
    $("#cookie-text").value = "";
  } catch (err) {
    cookieMessage(`Not saved: ${errorDetail(err)}`, true);
  }
}

function setupCookies() {
  const fileInput = $("#cookie-file");
  $("#cookie-upload").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (!fileInput.files.length) return;
    const form = new FormData();
    form.append("file", fileInput.files[0]);
    fileInput.value = "";
    sendCookies(form);
  });
  $("#cookie-paste").addEventListener("click", () => {
    $("#cookie-form").hidden = false;
    $("#cookie-text").focus();
  });
  $("#cookie-cancel").addEventListener("click", () => { $("#cookie-form").hidden = true; });
  $("#cookie-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = $("#cookie-text").value.trim();
    if (!text) return;
    const form = new FormData();
    form.append("text", text);
    sendCookies(form);
  });
  $("#cookie-remove").addEventListener("click", async () => {
    if (!confirm("Remove the stored YouTube cookies?")) return;
    const status = await api("/api/cookies", { method: "DELETE" });
    renderCookies(status);
    cookieMessage("Cookies removed.");
  });
}

/* -------------------------------------------------------------- settings */
const PROVIDER_LABELS = { ollama: "Ollama", openai: "OpenAI-compatible", anthropic: "Anthropic" };
let settingsInfo = null;

const settingInputs = (form) => [...form.querySelectorAll("[data-setting]")];

function envText(value) {
  if (value === null || value === undefined) return "none";
  if (value === true) return "on";
  if (value === false) return "off";
  return value === "" ? "empty" : String(value);
}

function setOptions(select, values) {
  if (select.options.length) return;
  select.replaceChildren(...values.map((v) => el("option", { value: v }, v)));
}

// A model field: a dropdown of known names, plus "Other…", which shows the
// text field (the setting itself) to type any name. A value the list
// doesn't have stays selectable, marked as such.
const OTHER = "\u0000other";

function setPicker(select, input, names, unlistedNote = "not in the list") {
  const current = input.value.trim();
  const list = [...new Set(names)];
  const options = list.map((n) => el("option", { value: n }, n));
  if (current && !list.includes(current)) {
    options.unshift(el("option", { value: current }, unlistedNote ? `${current} (${unlistedNote})` : current));
  }
  if (!current) options.unshift(el("option", { value: "", disabled: true }, "Choose a model…"));
  options.push(el("option", { value: OTHER }, "Other…"));
  select.replaceChildren(...options);
  select.value = input.hidden ? current : OTHER;
}

function setupPicker(select, input) {
  select.addEventListener("change", () => {
    if (select.value === OTHER) {
      input.hidden = false;
      input.focus();
      input.select();
    } else {
      input.value = select.value;
      input.hidden = true;
    }
  });
}

// Puts the server's values into the forms. After a save, only into the
// saved form (root), so unsaved edits in the other card survive.
function fillSettings(info, root = $("#settings")) {
  settingsInfo = info;
  setOptions($('[data-setting="whisper_compute_type"]'), info.choices.whisper_compute_type);
  for (const input of root.querySelectorAll("[data-setting]")) {
    const name = input.dataset.setting;
    const field = info.fields[name];
    if (!field) continue;
    if (input.type === "checkbox") input.checked = !!field.value;
    else if (input.type === "password") input.value = "";
    else input.value = field.value ?? "";
    const label = input.closest("label");
    let note = label.querySelector(".setting-note");
    if (field.overridden && input.type !== "password") {
      if (!note) { note = el("small", { class: "setting-note" }); label.append(note); }
      note.textContent = `Set here · environment: ${envText(field.env)}`;
    } else if (note) {
      note.remove();
    }
  }
  const key = info.fields.llm_api_key;
  $("#llm-key-hint").textContent = key.value
    ? (key.overridden ? "A key set here is stored. Leave empty to keep it." : "A key from the environment (LLM_API_KEY) is used. Enter one to replace it.")
    : "None. Hosted APIs need one; Ollama and most local servers don't.";
  $("#llm-key-clear-wrap").hidden = !(key.value && key.overridden);
  $("#llm-key-clear").checked = false;
  $("#llm-effective").textContent = `${PROVIDER_LABELS[info.fields.llm_provider.value] || ""} · ${info.fields.llm_model.value}`;
  $("#settings-path").textContent = info.path;
  if (root.contains($("#whisper-model-pick"))) {
    $('[data-setting="whisper_model"]').hidden = true;
    setPicker($("#whisper-model-pick"), $('[data-setting="whisper_model"]'), info.choices.whisper_model);
  }
  if (root.contains($("#llm-model-pick"))) {
    const input = $('[data-setting="llm_model"]');
    input.hidden = true;
    // keep the server's list (if loaded), just select the saved model in it
    setPicker($("#llm-model-pick"), input, llmModels || [], llmModels ? "not offered by this server" : null);
  }
  updateProviderUI();
}

function updateProviderUI() {
  if (!settingsInfo) return;
  const provider = $('[data-setting="llm_provider"]').value;
  for (const node of document.querySelectorAll("#llm-form .ollama-only")) node.hidden = provider !== "ollama";
  const url = $('[data-setting="llm_url"]');
  const fallback = settingsInfo.default_urls[provider] || "";
  url.placeholder = fallback;
  $("#llm-url-hint").textContent = provider === "openai"
    ? `OpenAI, or any server with an OpenAI-compatible API: LM Studio, llama.cpp, vLLM, OpenRouter… Empty uses ${fallback}; a local server needs its /v1 URL, e.g. http://host:1234/v1.`
    : `Empty uses ${fallback}.`;
}

function collectSettings(form) {
  const values = {};
  for (const input of settingInputs(form)) {
    values[input.dataset.setting] = input.type === "checkbox" ? input.checked : input.value.trim();
  }
  return values;
}

async function loadSettings() {
  try {
    fillSettings(await api("/api/settings"));
  } catch (err) {
    flash("#llm-message", `Could not load the settings: ${errorDetail(err)}`, true);
    return;
  }
  loadModels();
}

async function saveSettings(form, messageSel, body) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    fillSettings(await api("/api/settings", { method: "PUT", json: body }), form);
    flash(messageSel, body.values ? "Saved." : "Back to the environment's settings.");
    poll();
  } catch (err) {
    flash(messageSel, `Not saved: ${errorDetail(err)}`, true);
  } finally {
    button.disabled = false;
  }
}

// Fills the model dropdown from the (possibly unsaved) provider, URL and
// key. Only the latest request counts when several overlap.
let modelsRequest = 0;
let llmModels = null;  // the server's list, null until (or unless) it loaded

async function loadModels() {
  const hint = $("#llm-models-hint");
  const select = $("#llm-model-pick");
  const input = $('[data-setting="llm_model"]');
  const request = ++modelsRequest;
  hint.textContent = "Asking the server for its models…";
  try {
    const { models, base_url } = await api("/api/settings/llm/models", { method: "POST", json: { values: collectSettings($("#llm-form")) } });
    if (request !== modelsRequest) return;
    llmModels = models;
    setPicker(select, input, models, "not offered by this server");
    hint.textContent = models.length
      ? `${models.length} model${models.length === 1 ? "" : "s"} at ${base_url}.`
      : `${base_url} lists no models. Pick \u201cOther\u2026\u201d to enter a name.`;
  } catch (err) {
    if (request !== modelsRequest) return;
    llmModels = null;
    setPicker(select, input, [], null);
    hint.textContent = `${errorDetail(err)} Pick \u201cOther\u2026\u201d to enter a name.`;
  }
}

async function testLLM() {
  const button = $("#llm-test");
  button.disabled = true;
  flash("#llm-message", "Sending a test prompt…");
  try {
    const r = await api("/api/settings/llm/test", { method: "POST", json: { values: collectSettings($("#llm-form")) } });
    const text = `${r.model} answered in ${r.seconds} s: ${r.reply || "(empty)"}`;
    flash("#llm-message", r.json ? text : `${text}\nThat isn't JSON: the prompts need a model that follows instructions closely.`, !r.json);
  } catch (err) {
    flash("#llm-message", `Test failed: ${errorDetail(err)}`, true);
  } finally {
    button.disabled = false;
  }
}

function setupSettings() {
  const llmForm = $("#llm-form");
  const whisperForm = $("#whisper-form");
  $('[data-setting="llm_provider"]').addEventListener("change", () => {
    updateProviderUI();
    loadModels();
  });
  // a different server or key offers different models
  for (const name of ["llm_url", "llm_api_key"]) {
    $(`[data-setting="${name}"]`).addEventListener("change", loadModels);
  }
  setupPicker($("#llm-model-pick"), $('[data-setting="llm_model"]'));
  setupPicker($("#whisper-model-pick"), $('[data-setting="whisper_model"]'));
  $("#llm-load-models").addEventListener("click", loadModels);
  $("#llm-test").addEventListener("click", testLLM);
  llmForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings(llmForm, "#llm-message", {
      values: collectSettings(llmForm),
      reset: $("#llm-key-clear").checked ? ["llm_api_key"] : [],
    });
  });
  whisperForm.addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings(whisperForm, "#whisper-message", { values: collectSettings(whisperForm), reset: [] });
  });
  for (const button of document.querySelectorAll("#settings [data-reset]")) {
    button.addEventListener("click", () => {
      const form = button.closest("form");
      const what = button.dataset.reset === "llm" ? "LLM" : "Whisper";
      if (!confirm(`Drop the ${what} settings changed here and use the environment's again?`)) return;
      saveSettings(form, `#${button.dataset.reset}-message`, { reset: settingInputs(form).map((i) => i.dataset.setting) });
    });
  }
}

/* ------------------------------------------------------------------ jobs */
function jobLabel(job) {
  if (job.type === "import_file") return job.url;
  return job.url || "";
}

function jobMeta(job) {
  if (job.status === "running" || job.status === "queued") {
    if (job.type === "crawl" && job.children) {
      const c = job.children;
      const parts = [job.stage, job.progress_label].filter(Boolean);
      parts.push(`${c.done || 0} saved · ${c.skipped || 0} skipped · ${c.failed || 0} failed · ${(c.queued || 0) + (c.running || 0)} pending`);
      return parts.join(" · ");
    }
    if (job.status === "queued") return "waiting for a worker";
    return [job.stage, job.progress_label].filter(Boolean).join(" · ") || "starting";
  }
  if (job.type === "crawl" && job.children) {
    const c = job.children;
    const pending = (c.queued || 0) + (c.running || 0);
    return `${job.result || ""} — ${c.done || 0} saved, ${c.skipped || 0} skipped, ${c.failed || 0} failed${pending ? `, ${pending} pending` : ""}`;
  }
  return job.error || job.result || "";
}

function renderJobs(list, jobs, emptyText) {
  if (!jobs.length) {
    list.replaceChildren(el("li", { class: "empty" }, emptyText));
    return;
  }
  const existing = new Map([...list.querySelectorAll(":scope > li.job")].map((li) => [li.dataset.id, li]));
  const nodes = jobs.map((job) => {
    const li = existing.get(String(job.id)) || buildJob(job);
    updateJob(li, job);
    return li;
  });
  // Re-inserting a row detaches it, which resets the scroll position of an
  // open log inside it; so only move rows that aren't in place already.
  nodes.forEach((node, i) => {
    if (list.children[i] !== node) list.insertBefore(node, list.children[i] || null);
  });
  while (list.children.length > nodes.length) list.lastElementChild.remove();
}

function jobActionFor(job, active) {
  if (active) return ["cancel", "Cancel", "Stop this job"];
  if (job.status === "failed" || job.status === "cancelled") return ["retry", "Retry", "Run it again"];
  if (job.type === "import_file") return [null];  // the uploaded file was consumed
  if (job.type === "crawl") return ["retry", "Run again", "Crawl again for new pages"];
  if (job.status === "skipped" && /no real content/.test(job.error || "")) {
    return ["retry?allow_thin=true", "Keep anyway", "Save it even though the AI judged it to have no real content (index, archive, login wall…)"];
  }
  return ["retry?force=true", "Re-save", "Capture, tag and file it again"];
}

function buildJob(job) {
  const li = el("li", { class: "job", dataset: { id: job.id } });
  const toggle = () => toggleLog(li, job.id);
  li.append(
    el("div", { class: "job-row" },
      el("span", { class: "kind" }),
      el("div", { class: "job-main", onclick: toggle, title: "Show log" },
        el("div", { class: "job-title" }, el("span", { class: "job-url" })),
        el("div", { class: "job-meta" })),
      el("span", { class: "badge" }),
      el("span", { class: "time" }),
      el("button", { class: "icon-btn library-link", title: "Show this entry in the library", hidden: true }, "Library"),
      el("button", { class: "icon-btn action" })),
    el("div", { class: "progress" }, el("span")),
    el("ul", { class: "children", hidden: true }),
    el("pre", { class: "log", hidden: true }));
  li.querySelector(".action").addEventListener("click", (event) => {
    event.stopPropagation();
    const action = event.currentTarget.dataset.action;
    jobAction(li.dataset.id, action);
  });
  li.querySelector(".library-link").addEventListener("click", (event) => {
    event.stopPropagation();
    openInLibrary(li.dataset.url);
  });
  return li;
}

// Switch to the Library tab, searching for one saved link.
function openInLibrary(url) {
  $("#lib-q").value = url;
  $("#lib-kind").value = "";
  document.querySelector('.tab[data-tab="library"]').click();
}

function updateJob(li, job) {
  const kind = job.kind || job.type;
  const kindEl = li.querySelector(".kind");
  kindEl.className = `kind ${kind}`;
  kindEl.textContent = KIND_LABELS[kind] || kind;
  const urlEl = li.querySelector(".job-url");
  urlEl.textContent = jobLabel(job);
  urlEl.title = jobLabel(job);
  const meta = li.querySelector(".job-meta");
  meta.textContent = jobMeta(job);
  meta.classList.toggle("error", job.status === "failed");
  const badge = li.querySelector(".badge");
  badge.className = `badge ${job.status}`;
  badge.textContent = job.status;
  const active = job.status === "running" || job.status === "queued";
  li.querySelector(".time").textContent = active
    ? (job.status === "running" ? duration(job.started_at) : ago(job.created_at))
    : ago(job.finished_at);
  // Saved links have a library entry (a crawl's pages are listed one by one)
  li.dataset.url = job.url || "";
  li.querySelector(".library-link").hidden = !(job.type === "save" && job.status === "done" && job.url);
  const action = li.querySelector(".action");
  const [name, label, title] = jobActionFor(job, active);
  action.hidden = !name;
  action.dataset.action = name || "";
  action.textContent = label || "";
  action.title = title || "";

  const bar = li.querySelector(".progress");
  bar.hidden = job.status !== "running";
  const determinate = typeof job.progress === "number";
  bar.classList.toggle("indeterminate", !determinate);
  bar.firstChild.style.width = determinate ? `${Math.round(job.progress * 100)}%` : "";
}

async function toggleLog(li, id) {
  const log = li.querySelector(".log");
  if (expanded.has(id)) {
    expanded.delete(id);
    log.hidden = true;
    li.querySelector(".children").hidden = true;
    return;
  }
  expanded.set(id, 0);
  log.textContent = "";
  log.hidden = false;
  await refreshLog(li, id);
}

function logLine(text) {
  const cls = text.startsWith("»") ? "stage"
    : /^(WARNING|NOTE)/.test(text) ? "warn"
    : /^(FAILED|ERROR|Traceback|\s+File )/.test(text) ? "err" : null;
  return el("span", { class: cls }, text + "\n");
}

async function refreshLog(li, id) {
  if (!expanded.has(id)) return;
  const job = await api(`/api/jobs/${id}?after=${expanded.get(id)}`);
  const log = li.querySelector(".log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  for (const line of job.logs) log.append(logLine(line.line));
  if (job.logs.length) expanded.set(id, job.logs[job.logs.length - 1].id);
  if (!log.childNodes.length) log.textContent = "No log output yet.";
  if (atBottom) log.scrollTop = log.scrollHeight;
  const children = li.querySelector(".children");
  if (job.child_jobs) {
    children.hidden = false;
    children.replaceChildren(...job.child_jobs.slice(0, 200).map((c) => el("li", {},
      el("span", { class: `badge ${c.status}` }, c.status),
      el("span", { class: "job-url", title: c.url }, c.url),
      el("span", { class: "muted" }, c.status === "running" ? (c.stage || "") : (c.error || "")))));
  }
}

async function jobAction(id, action) {
  if (!action) return;
  try {
    await api(`/api/jobs/${id}/${action}`, { method: "POST" });
  } catch (err) {
    alert(`Could not ${action.split("?")[0]} job ${id}: ${err.message}`);
  }
  poll();
}

/* ------------------------------------------------------------------ poll */
async function poll() {
  clearTimeout(pollTimer);
  let delay = 5000;
  try {
    const state = await api("/api/state");
    if (state.server_time) serverOffset = new Date(state.server_time).getTime() - Date.now();
    config = state.config;
    renderStats(state.stats);
    renderStatus(state);
    renderCookies(state.config.cookies);
    renderStorage(state.config.storage);
    renderJobs($("#active-jobs"), state.active, "Nothing queued.");
    renderJobs($("#recent-jobs"), state.recent, "No finished jobs yet.");
    const q = state.stats.jobs;
    $("#queue-summary").textContent = q.running || q.queued ? `${q.running} running · ${q.queued} waiting` : "";
    for (const id of expanded.keys()) {
      const li = document.querySelector(`li.job[data-id="${id}"]`);
      if (li) {
        li.querySelector(".log").hidden = false;
        await refreshLog(li, id);
      } else {
        expanded.delete(id);
      }
    }
    delay = state.active.length ? 1500 : 5000;
  } catch (err) {
    $("#status-pill").className = "pill bad";
    $("#status-pill").textContent = "Offline";
    console.error(err);
  }
  if (document.hidden) delay = 15000;
  pollTimer = setTimeout(poll, delay);
}

/* ---------------------------------------------------------------- submit */
function showResults(results) {
  const list = $("#submit-results");
  list.replaceChildren(...results.map((r) => {
    const label = r.url || r.file;
    let badge, text;
    switch (r.status) {
      case "queued": badge = "queued"; text = `job #${r.job_id}`; break;
      case "already_queued": badge = "queued"; text = `already queued (#${r.job_id})`; break;
      case "already_saved": badge = "skipped"; text = `already saved → ${r.note}`; break;
      default: badge = "failed"; text = r.error || r.status;
    }
    return el("li", {}, el("span", { class: `badge ${badge}` }, badge === "skipped" ? "saved" : badge),
      el("span", { class: "url", title: label }, label), el("span", { class: "muted" }, text));
  }));
}

async function submitLinks(event) {
  event?.preventDefault();
  const text = $("#urls").value.trim();
  if (!text) return;
  const body = { urls: [text], force: $("#opt-force").checked, allow_thin: $("#opt-thin").checked };
  if ($("#opt-crawl").checked) {
    body.crawl = {
      max_depth: Number($("#crawl-depth").value), max_pages: Number($("#crawl-pages").value),
      scope: $("#crawl-scope").value, pdf_only: $("#crawl-pdf").checked,
      extra_hosts: $("#crawl-hosts").value.split(/[\s,]+/).filter(Boolean),
    };
  }
  const button = $("#submit-btn");
  button.disabled = true;
  try {
    const { results } = await api("/api/links", { method: "POST", json: body });
    showResults(results);
    if (results.every((r) => r.status !== "invalid")) $("#urls").value = "";
  } catch (err) {
    showResults([{ url: text.split(/\s+/)[0], status: "error", error: err.message }]);
  } finally {
    button.disabled = false;
    poll();
  }
}

async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  const source = $("#file-source").value.trim();
  if (source) form.append("source", source);
  form.append("force", $("#opt-force").checked ? "true" : "false");
  try {
    const { results } = await api("/api/upload", { method: "POST", body: form });
    showResults(results);
    $("#file-source").value = "";
  } catch (err) {
    showResults([{ file: files[0].name, status: "error", error: err.message }]);
  }
  poll();
}

function setupDropzone() {
  const zone = $("#dropzone");
  const input = $("#file-input");
  $("#browse-btn").addEventListener("click", () => input.click());
  input.addEventListener("change", () => { uploadFiles([...input.files]); input.value = ""; });
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  }
  zone.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
}

/* --------------------------------------------------------------- library */
let libOffset = 0;
let libTimer = null;

function parseEntry(entry) {
  const lines = (entry.area_entry || "").split("\n");
  let tags = "", summary = "";
  if (lines[0]?.startsWith("- #")) {
    tags = lines[0].slice(2);
    summary = (lines[1] || "").trim();
  } else if (lines[0]?.startsWith("- ")) {
    summary = lines[0].slice(2);
  }
  let title = "";
  if (entry.filename) title = entry.filename.split("/").pop().replace(/ \(\d{4}-\d\d-\d\d \d\d-\d\d-\d\d\)\.html$/, "").replace(/\.(pdf|html)$/, "");
  if (!title && entry.note && !entry.note.startsWith("Areas")) title = entry.note.split("/").pop();
  if (!title) {
    try { const u = new URL(entry.url); title = u.hostname.replace(/^www\./, "") + u.pathname.replace(/\/$/, ""); }
    catch { title = entry.url; }
  }
  return { tags, summary, title };
}

async function loadLibrary(reset = true) {
  if (reset) libOffset = 0;
  const params = new URLSearchParams({ limit: 50, offset: libOffset });
  const q = $("#lib-q").value.trim();
  const kind = $("#lib-kind").value;
  if (q) params.set("q", q);
  if (kind) params.set("kind", kind);
  const { entries, total } = await api(`/api/entries?${params}`);
  const list = $("#lib-list");
  const items = entries.map((entry) => {
    const { tags, summary, title } = parseEntry(entry);
    const noteLink = obsidianLink(entry.area_note ? `Areas/${entry.area_note}` : entry.note);
    const isWeb = /^https?:/.test(entry.url);
    return el("li", { class: "entry" },
      el("span", { class: `kind ${entry.kind}` }, KIND_LABELS[entry.kind] || entry.kind),
      el("a", { class: "title", href: isWeb ? entry.url : null, target: "_blank", rel: "noopener", title: entry.url }, title),
      el("span", { class: "time" }, ago(entry.processed_at)),
      tags ? el("span", { class: "tags" }, tags) : null,
      summary ? el("span", { class: "summary" }, summary) : null,
      el("span", { class: "where" },
        noteLink ? el("a", { href: noteLink }, entry.area_note ? `Areas/${entry.area_note}` : entry.note)
          : (entry.area_note ? `Areas/${entry.area_note}` : entry.note),
        blobFolderLink(entry)));
  });
  if (reset) list.replaceChildren(...items); else list.append(...items);
  libOffset += entries.length;
  $("#lib-count").textContent = `${fmt(total)} saved`;
  $("#lib-more").hidden = libOffset >= total;
  if (!total) list.replaceChildren(el("li", { class: "empty muted" }, "Nothing matches."));
}

/* ------------------------------------------------------------------ init */
function setupTabs() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== tab.dataset.tab; });
      if (tab.dataset.tab === "library") loadLibrary().catch(console.error);
      if (tab.dataset.tab === "settings") loadSettings();
    });
  }
}

function init() {
  setupTabs();
  setupDropzone();
  setupCookies();
  setupSettings();
  setupStorage();
  $("#submit-form").addEventListener("submit", submitLinks);
  $("#urls").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitLinks(e); });
  $("#opt-crawl").addEventListener("change", (e) => { $("#crawl-options").hidden = !e.target.checked; });
  $("#pause-btn").addEventListener("click", async (e) => {
    await api(e.currentTarget.dataset.paused ? "/api/resume" : "/api/pause", { method: "POST" });
    poll();
  });
  $("#clear-btn").addEventListener("click", async () => { await api("/api/jobs", { method: "DELETE" }); poll(); });
  $("#lib-q").addEventListener("input", () => { clearTimeout(libTimer); libTimer = setTimeout(() => loadLibrary().catch(console.error), 250); });
  $("#lib-kind").addEventListener("change", () => loadLibrary().catch(console.error));
  $("#lib-more").addEventListener("click", () => loadLibrary(false).catch(console.error));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

  const bookmarklet = `javascript:(()=>{window.open('${location.origin}/?submit=1&url='+encodeURIComponent(location.href),'_blank')})()`;
  $("#bookmarklet").setAttribute("href", bookmarklet);
  $("#bookmarklet").addEventListener("click", (e) => e.preventDefault());

  const params = new URLSearchParams(location.search);
  if (params.get("crawl")) {
    $("#opt-crawl").checked = true;
    $("#crawl-options").hidden = false;
  }
  if (params.get("url")) {
    $("#urls").value = params.get("url");
    history.replaceState(null, "", location.pathname);
    if (params.get("submit")) submitLinks();
  }
  poll();
}

init();
