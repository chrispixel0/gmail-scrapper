"use strict";

const PAGE_SIZE = 20;
const $ = (id) => document.getElementById(id);

const els = {
  // token
  tokenCard: $("token-card"),
  tokenForm: $("token-form"),
  tokenInput: $("token"),
  tokenSave: $("token-save"),
  tokenCancel: $("token-cancel"),
  tokenError: $("token-error"),
  tokenStatus: $("token-status"),
  tokenChange: $("token-change"),
  // search
  form: $("fetch-form"),
  name: $("name"),
  location: $("location"),
  estimateBtn: $("estimate-btn"),
  fetchBtn: $("fetch-btn"),
  stopBtn: $("stop-btn"),
  estimate: $("estimate"),
  progress: $("progress"),
  query: $("query"),
  barFill: $("bar-fill"),
  status: $("status"),
  info: $("info"),
  error: $("error"),
  // results
  results: $("results"),
  empty: $("empty"),
  count: $("count"),
  clearBtn: $("clear-btn"),
  pager: $("pager"),
  pagerInfo: $("pager-info"),
  pagerButtons: $("pager-buttons"),
};

const state = {
  hasToken: false,
  changingToken: false,
  busy: false,            // a fetch is running
  estimating: false,      // an estimate request is running
  estimate: null,         // {name, location, total, seconds} for the last successful estimate
  foundThisRun: 0,
  startedAt: 0,
  runId: null,            // server id of the running fetch (used by Stop)
  page: 1,
};
const records = new Map();   // login -> record
const fresh = new Set();     // logins found in the current fetch (highlighted)
let source = null;           // active EventSource

// ---------- helpers ----------

function inputs() {
  return { name: els.name.value.trim(), location: els.location.value.trim() };
}

/** True when the last estimate was made for exactly the current Name and Area. */
function estimateMatchesInputs() {
  const { name, location } = inputs();
  return !!state.estimate && state.estimate.name === name && state.estimate.location === location;
}

function safeUrl(url, fallback) {
  return typeof url === "string" && url.startsWith("https://") ? url : fallback;
}

function avatarSrc(r) {
  const url = safeUrl(r.avatar_url, `https://github.com/${encodeURIComponent(r.login)}.png`);
  return url + (url.includes("?") ? "&" : "?") + "s=72";
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function roughDuration(seconds) {
  if (seconds < 60) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `about ${h} hour${h === 1 ? "" : "s"}` + (m ? ` ${m} min` : "");
}

function showError(el, message) {
  el.textContent = message || "";
  el.hidden = !message;
}

// ---------- rendering: results table + pagination ----------

function td(text, className) {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  if (text !== undefined) cell.textContent = text ?? "";
  return cell;
}

function linkTd(text, href, external = true) {
  const cell = td();
  const a = document.createElement("a");
  a.textContent = text;
  a.href = href;
  if (external) {
    a.target = "_blank";
    a.rel = "noopener";
  }
  cell.appendChild(a);
  return cell;
}

function rowFor(r) {
  const tr = document.createElement("tr");
  if (fresh.has(r.login)) tr.className = "new";

  const avatarCell = td(undefined, "avatar-col");
  const img = document.createElement("img");
  img.className = "avatar";
  img.src = avatarSrc(r);
  img.alt = "";
  img.loading = "lazy";
  img.width = 36;
  img.height = 36;
  avatarCell.appendChild(img);

  const profile = safeUrl(r.profile_url, `https://github.com/${encodeURIComponent(r.login)}`);
  tr.append(
    avatarCell,
    td(r.name || "—"),
    linkTd("@" + r.login, profile),
    td(r.location || "—"),
    linkTd(r.email, "mailto:" + r.email, false),
  );
  return tr;
}

/** Page numbers to show, e.g. [1, null, 4, 5, 6, null, 12]; null = "…". */
function pageList(current, pages) {
  const wanted = [...new Set([1, pages, current - 1, current, current + 1])]
    .filter((p) => p >= 1 && p <= pages)
    .sort((a, b) => a - b);
  const out = [];
  let prev = 0;
  for (const p of wanted) {
    if (p - prev === 2) out.push(prev + 1);     // fill a single-page gap instead of "…"
    else if (p - prev > 2) out.push(null);
    out.push(p);
    prev = p;
  }
  return out;
}

function pageButton(label, page, { disabled = false, current = false, aria } = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "page-btn";
  b.textContent = label;
  b.disabled = disabled;
  if (current) b.setAttribute("aria-current", "page");
  if (aria) b.setAttribute("aria-label", aria);
  b.addEventListener("click", () => {
    state.page = page;
    renderTable();
  });
  return b;
}

function renderPager(totalRows, pages, start, shown) {
  els.pager.hidden = totalRows === 0;
  if (!totalRows) return;
  els.pagerInfo.textContent =
    `Showing ${start + 1}–${start + shown} of ${totalRows.toLocaleString()}`;

  if (pages <= 1) {
    els.pagerButtons.replaceChildren();
    return;
  }
  const buttons = [pageButton("‹ Prev", state.page - 1,
    { disabled: state.page === 1, aria: "Previous page" })];
  for (const p of pageList(state.page, pages)) {
    if (p === null) {
      const gap = document.createElement("span");
      gap.className = "page-gap";
      gap.textContent = "…";
      buttons.push(gap);
    } else {
      buttons.push(pageButton(String(p), p, { current: p === state.page, aria: `Page ${p}` }));
    }
  }
  buttons.push(pageButton("Next ›", state.page + 1,
    { disabled: state.page === pages, aria: "Next page" }));
  els.pagerButtons.replaceChildren(...buttons);
}

function renderTable() {
  const rows = [...records.values()].sort((a, b) =>
    (b.fetched_at || "").localeCompare(a.fetched_at || "") || a.login.localeCompare(b.login));

  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(Math.max(1, state.page), pages);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);

  els.results.replaceChildren(...pageRows.map(rowFor));
  els.count.textContent = records.size.toLocaleString();
  els.empty.hidden = records.size > 0;
  renderPager(rows.length, pages, start, pageRows.length);
}

// ---------- rendering: controls + estimate ----------

function renderEstimate() {
  const el = els.estimate;
  el.className = "estimate";
  el.replaceChildren();

  if (!state.hasToken) return;
  if (state.estimating) {
    el.textContent = "Counting matching GitHub users…";
    return;
  }
  if (!estimateMatchesInputs()) {
    el.textContent = "Click Estimate to see how many GitHub users match. Fetch unlocks after that.";
    return;
  }

  const { name, location, total, seconds } = state.estimate;
  const where = location ? ` in “${location}”` : "";
  if (total === 0) {
    el.classList.add("result", "empty-result");
    el.textContent = `No GitHub users match “${name}”${where}. Try a different name or area.`;
    return;
  }
  const count = document.createElement("strong");
  count.textContent = `${total.toLocaleString()} user${total === 1 ? "" : "s"}`;
  el.classList.add("result");
  el.append(
    "GitHub found ", count, ` for “${name}”${where}. ` +
    `Fetching their Gmail addresses will take ${roughDuration(seconds)}.`,
  );
}

function updateControls() {
  const showTokenForm = !state.hasToken || state.changingToken;
  els.tokenCard.hidden = !showTokenForm;
  els.tokenCancel.hidden = !state.hasToken;           // can only cancel if a token exists
  els.tokenStatus.hidden = !state.hasToken || state.changingToken;

  const ready = state.hasToken && !state.busy;
  const { name } = inputs();
  els.name.disabled = !ready;
  els.location.disabled = !ready;
  els.estimateBtn.disabled = !ready || state.estimating || !name;
  els.estimateBtn.textContent = state.estimating ? "Estimating…" : "Estimate";
  els.fetchBtn.disabled = !ready || state.estimating ||
    !(estimateMatchesInputs() && state.estimate.total > 0);
  els.fetchBtn.textContent = state.busy ? "Fetching…" : "Fetch";
  els.stopBtn.hidden = !state.busy;
  els.clearBtn.disabled = state.busy;
  els.tokenChange.disabled = state.busy;
  renderEstimate();
}

function setProgress(checked, total) {
  if (total > 0) {
    els.barFill.classList.remove("indeterminate");
    els.barFill.style.width = `${Math.min(100, (checked / total) * 100)}%`;
  }
  let text = `Checked ${checked.toLocaleString()} of ${total.toLocaleString()} profile(s) · ` +
             `${state.foundThisRun} Gmail address(es) found`;
  if (checked >= 5 && total > checked) {
    const perProfile = (Date.now() - state.startedAt) / 1000 / checked;
    text += ` · about ${formatDuration(perProfile * (total - checked))} left`;
  }
  els.status.textContent = text;
}

// ---------- API calls ----------

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    state.hasToken = (await res.json()).token;
  } catch {
    showError(els.error, "Could not reach the server. Is it running?");
  }
  updateControls();
  if (!state.hasToken) els.tokenInput.focus();
}

async function loadResults() {
  try {
    const res = await fetch("/api/results");
    const rows = await res.json();
    records.clear();
    rows.forEach((r) => records.set(r.login, r));
    renderTable();
  } catch {
    showError(els.error, "Could not load saved results. Is the server running?");
  }
}

async function saveToken(token) {
  els.tokenSave.disabled = true;
  els.tokenSave.textContent = "Checking…";
  showError(els.tokenError, "");
  try {
    const res = await fetch("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not save the token.");
    state.hasToken = true;
    state.changingToken = false;
    els.tokenInput.value = "";
    showError(els.error, "");
    updateControls();
    els.name.focus();
  } catch (e) {
    showError(els.tokenError, e.message || "Could not reach the server.");
  } finally {
    els.tokenSave.disabled = false;
    els.tokenSave.textContent = "Save token";
  }
}

async function runEstimate() {
  const { name, location } = inputs();
  if (!name) {
    els.name.focus();
    return;
  }
  showError(els.error, "");
  state.estimating = true;
  state.estimate = null;
  updateControls();
  try {
    const res = await fetch(`/api/estimate?${new URLSearchParams({ name, location })}`);
    const data = await res.json();
    if (!res.ok) {
      if (data.code === "no_token" || data.code === "bad_token") state.hasToken = false;
      throw new Error(data.error || "Could not get an estimate.");
    }
    // Remember which Name/Area this estimate belongs to.
    state.estimate = { name, location, total: data.total, seconds: data.seconds };
  } catch (e) {
    showError(els.error, e.message || "Could not reach the server.");
  } finally {
    state.estimating = false;
    updateControls();
  }
}

// ---------- fetching ----------

/**
 * End the current fetch. `succeeded` = finished normally ("done"): the
 * estimate is used up, so Fetch stays disabled until the user estimates again.
 * After Stop or an error the estimate is kept, so Fetch can be clicked again.
 */
function finish(message, succeeded = false) {
  if (source) {
    source.close();
    source = null;
  }
  state.busy = false;
  if (succeeded) state.estimate = null;
  els.barFill.classList.remove("indeterminate");
  if (message !== undefined) els.status.textContent = message;
  updateControls();
}

function startFetch() {
  if (!estimateMatchesInputs() || !state.estimate.total) return;
  const { name, location } = state.estimate;

  showError(els.error, "");
  fresh.clear();
  renderTable();
  state.foundThisRun = 0;
  state.startedAt = Date.now();
  state.runId = null;
  state.busy = true;
  updateControls();

  els.progress.hidden = false;
  els.query.textContent = "";
  els.info.hidden = true;
  els.barFill.style.width = "";
  els.barFill.classList.add("indeterminate");
  els.status.textContent = "Searching GitHub…";

  source = new EventSource(`/api/fetch?${new URLSearchParams({ name, location })}`);
  const on = (type, handler) =>
    source.addEventListener(type, (e) => handler(JSON.parse(e.data)));

  on("start", (d) => {
    state.runId = d.run;
    els.query.textContent = "Query: " + d.query;
  });

  on("total", (d) => {
    state.startedAt = Date.now();
    els.status.textContent = d.total
      ? `GitHub found ${d.total.toLocaleString()} user(s). Checking their profiles…`
      : "Searching GitHub…";
  });

  on("info", (d) => {
    els.info.textContent = d.message;
    els.info.hidden = false;
  });

  on("wait", (d) => {
    els.status.textContent =
      `GitHub rate limit reached. Continuing automatically in ${formatDuration(d.seconds)}… ` +
      `(${state.foundThisRun} Gmail address(es) found so far)`;
  });

  on("checked", (d) => setProgress(d.checked, d.total));

  on("found", (d) => {
    state.foundThisRun += 1;
    records.set(d.record.login, d.record);
    fresh.add(d.record.login);
    renderTable();          // keeps the current page
    setProgress(d.checked, d.total);
  });

  on("done", (d) => {
    els.barFill.style.width = "100%";
    finish(d.checked === 0
      ? "No GitHub users matched this search."
      : `Done. Checked ${d.checked.toLocaleString()} profile(s) and found ${d.found} Gmail address(es).`,
      true);
  });

  on("fail", (d) => {
    showError(els.error, d.message);
    if (d.code === "no_token" || d.code === "bad_token") state.hasToken = false;
    finish(state.foundThisRun
      ? `Stopped early. ${state.foundThisRun} address(es) found before the error are saved.`
      : "");
  });

  // Fired if the connection drops before "done" or "fail" arrives.
  source.onerror = () => {
    if (!source) return;
    showError(els.error, "Lost connection to the server.");
    finish("");
  };
}

// ---------- event wiring ----------

els.tokenForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const token = els.tokenInput.value.trim();
  if (token) saveToken(token);
});

els.tokenChange.addEventListener("click", () => {
  state.changingToken = true;
  showError(els.tokenError, "");
  updateControls();
  els.tokenInput.focus();
});

els.tokenCancel.addEventListener("click", () => {
  state.changingToken = false;
  els.tokenInput.value = "";
  showError(els.tokenError, "");
  updateControls();
});

// Pressing Enter in Name/Area runs Estimate (the form's submit button).
els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!els.estimateBtn.disabled) runEstimate();
});

// Changing Name or Area invalidates the estimate, so Fetch is disabled again.
[els.name, els.location].forEach((input) =>
  input.addEventListener("input", () => {
    if (state.estimate && !estimateMatchesInputs()) state.estimate = null;
    updateControls();
  }));

els.fetchBtn.addEventListener("click", startFetch);

els.stopBtn.addEventListener("click", () => {
  // Tell the server to stop checking profiles right away (saves API quota).
  if (state.runId) {
    fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: state.runId }),
    }).catch(() => {});
  }
  finish(`Stopped. ${state.foundThisRun} address(es) found so far are saved. ` +
         "Click Fetch to run it again.");
});

els.clearBtn.addEventListener("click", async () => {
  if (!records.size || !confirm("Delete all saved addresses?")) return;
  try {
    await fetch("/api/results", { method: "DELETE" });
    records.clear();
    fresh.clear();
    state.page = 1;
    renderTable();
  } catch {
    showError(els.error, "Could not clear results.");
  }
});

updateControls();
loadStatus();
loadResults();
