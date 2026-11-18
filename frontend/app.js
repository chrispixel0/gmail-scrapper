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
  location: $("location"),
  fetchBtn: $("fetch-btn"),
  stopBtn: $("stop-btn"),
  status: $("status"),
  info: $("info"),
  error: $("error"),
  checkedStatus: $("checked-status"),
  checkedCount: $("checked-count"),
  checkedForget: $("checked-forget"),
  // results
  results: $("results"),
  empty: $("empty"),
  count: $("count"),
  pager: $("pager"),
  pagerInfo: $("pager-info"),
  pagerButtons: $("pager-buttons"),
};

const state = {
  hasToken: false,
  changingToken: false,
  busy: false,            // a fetch is running
  checkedTotal: 0,        // profiles remembered as already checked
  foundThisRun: 0,
  checkedThisRun: 0,
  skippedThisRun: 0,
  total: 0,               // users GitHub found for the current search
  runId: null,            // server id of the running fetch (used by Stop)
  page: 1,
};
const records = new Map();   // login -> record
const fresh = new Set();     // logins found in the current fetch (highlighted)
let source = null;           // active EventSource

// ---------- helpers ----------

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

function pageButton(label, page, goTo, { disabled = false, current = false, aria } = {}) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "page-btn";
  b.textContent = label;
  b.disabled = disabled;
  if (current) b.setAttribute("aria-current", "page");
  if (aria) b.setAttribute("aria-label", aria);
  b.addEventListener("click", () => goTo(page));
  return b;
}

/**
 * Fill a pager: ui = {pager, info, buttons}; goTo(page) is called on click.
 * Shared by the Fetch-tab results table and the Send-tab recipients table.
 */
function renderPager(ui, { page, pages, totalRows, start, shown }, goTo) {
  ui.pager.hidden = totalRows === 0;
  if (!totalRows) return;
  ui.info.textContent =
    `Showing ${start + 1}–${start + shown} of ${totalRows.toLocaleString()}`;

  if (pages <= 1) {
    ui.buttons.replaceChildren();
    return;
  }
  const buttons = [pageButton("‹ Prev", page - 1, goTo,
    { disabled: page === 1, aria: "Previous page" })];
  for (const p of pageList(page, pages)) {
    if (p === null) {
      const gap = document.createElement("span");
      gap.className = "page-gap";
      gap.textContent = "…";
      buttons.push(gap);
    } else {
      buttons.push(pageButton(String(p), p, goTo, { current: p === page, aria: `Page ${p}` }));
    }
  }
  buttons.push(pageButton("Next ›", page + 1, goTo,
    { disabled: page === pages, aria: "Next page" }));
  ui.buttons.replaceChildren(...buttons);
}

/** Saved records, newest first (same order in both tables). */
function sortedRecords() {
  return [...records.values()].sort((a, b) =>
    (b.fetched_at || "").localeCompare(a.fetched_at || "") || a.login.localeCompare(b.login));
}

/** Clamp `page` and slice one page out of `rows`. */
function paginate(rows, page) {
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  page = Math.min(Math.max(1, page), pages);
  const start = (page - 1) * PAGE_SIZE;
  const pageRows = rows.slice(start, start + PAGE_SIZE);
  return { page, pages, start, pageRows, totalRows: rows.length, shown: pageRows.length };
}

// Called whenever `records` changes, so the Send tab can redraw its table.
const recordListeners = [];

function renderTable() {
  const view = paginate(sortedRecords(), state.page);
  state.page = view.page;

  els.results.replaceChildren(...view.pageRows.map(rowFor));
  els.count.textContent = records.size.toLocaleString();
  els.empty.hidden = records.size > 0;
  renderPager({ pager: els.pager, info: els.pagerInfo, buttons: els.pagerButtons }, view, (p) => {
    state.page = p;
    renderTable();
  });
  recordListeners.forEach((fn) => fn());
}

// ---------- rendering: controls + status ----------

function renderChecked() {
  els.checkedCount.textContent = state.checkedTotal.toLocaleString();
  els.checkedStatus.hidden = !state.hasToken || state.changingToken || state.checkedTotal === 0;
  els.checkedForget.disabled = state.busy;
}

function updateControls() {
  const showTokenForm = !state.hasToken || state.changingToken;
  els.tokenCard.hidden = !showTokenForm;
  els.tokenCancel.hidden = !state.hasToken;           // can only cancel if a token exists
  els.tokenStatus.hidden = !state.hasToken || state.changingToken;

  const ready = state.hasToken && !state.busy;
  els.location.disabled = !ready;
  els.fetchBtn.disabled = !ready || !els.location.value.trim();
  els.fetchBtn.textContent = state.busy ? "Fetching…" : "Fetch";
  els.stopBtn.hidden = !state.busy;
  els.tokenChange.disabled = state.busy;
  renderChecked();
}

function setStatus(text) {
  els.status.textContent = text;
  els.status.hidden = !text;
}

/** One line of progress text, e.g. "Checked 1,240 of 60,340 · 87 Gmail found · 3,000 skipped". */
function showProgress() {
  const done = state.checkedThisRun + state.skippedThisRun;
  let text = `Checked ${state.checkedThisRun.toLocaleString()}`;
  if (state.total) text += ` of ${state.total.toLocaleString()}`;
  text += ` · ${state.foundThisRun.toLocaleString()} Gmail found`;
  if (state.skippedThisRun) {
    text += ` · ${state.skippedThisRun.toLocaleString()} skipped (already checked)`;
  }
  if (state.checkedThisRun >= 5 && state.total > done) {
    const perProfile = (Date.now() - state.startedAt) / 1000 / state.checkedThisRun;
    text += ` · about ${formatDuration(perProfile * (state.total - done))} left`;
  }
  setStatus(text);
}

// ---------- API calls ----------

async function loadStatus() {
  try {
    const data = await (await fetch("/api/status")).json();
    state.hasToken = data.token;
    state.checkedTotal = data.checked || 0;
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
    els.location.focus();
  } catch (e) {
    showError(els.tokenError, e.message || "Could not reach the server.");
  } finally {
    els.tokenSave.disabled = false;
    els.tokenSave.textContent = "Save token";
  }
}

// ---------- fetching ----------

function finish(message) {
  if (source) {
    source.close();
    source = null;
  }
  state.busy = false;
  if (message !== undefined) setStatus(message);
  updateControls();
  loadStatus();          // refresh the "already checked" count
  loadResults();         // pick up anything saved after the last progress update
}

function startFetch() {
  const location = els.location.value.trim();
  if (!location || state.busy || !state.hasToken) return;

  showError(els.error, "");
  fresh.clear();
  renderTable();
  state.foundThisRun = 0;
  state.checkedThisRun = 0;
  state.skippedThisRun = 0;
  state.total = 0;
  state.startedAt = Date.now();
  state.runId = null;
  state.busy = true;
  updateControls();

  els.info.hidden = true;
  setStatus(`Searching GitHub for users in “${location}”…`);

  source = new EventSource(`/api/fetch?${new URLSearchParams({ location })}`);
  const on = (type, handler) =>
    source.addEventListener(type, (e) => handler(JSON.parse(e.data)));

  on("start", (d) => {
    state.runId = d.run;
  });

  on("total", (d) => {
    state.total = d.total;
    state.startedAt = Date.now();
    setStatus(d.total
      ? `GitHub found ${d.total.toLocaleString()} user(s) in “${location}”. Checking their profiles…`
      : `Searching GitHub for users in “${location}”…`);
  });

  on("info", (d) => {
    els.info.textContent = d.message;
    els.info.hidden = false;
  });

  on("wait", (d) => {
    setStatus(`GitHub rate limit reached. Continuing automatically in ${formatDuration(d.seconds)}… ` +
              `(${state.foundThisRun} Gmail address(es) found so far)`);
  });

  on("skipped", (d) => {
    state.skippedThisRun = d.skipped;
    showProgress();
  });

  on("checked", (d) => {
    state.checkedThisRun = d.checked;
    state.skippedThisRun = d.skipped;
    showProgress();
  });

  on("found", (d) => {
    state.foundThisRun += 1;
    state.checkedThisRun = d.checked;
    state.skippedThisRun = d.skipped;
    records.set(d.record.login, d.record);
    fresh.add(d.record.login);
    renderTable();          // keeps the current page
    showProgress();
  });

  on("done", (d) => {
    let msg;
    if (d.checked === 0 && d.skipped === 0) {
      msg = `No GitHub users matched “${location}”.`;
    } else if (d.checked === 0) {
      msg = `Nothing new. All ${d.skipped.toLocaleString()} matching profiles were already checked.`;
    } else {
      msg = `Done. Checked ${d.checked.toLocaleString()} new profile(s) and found ${d.found} Gmail address(es)`;
      if (d.skipped) msg += `, skipped ${d.skipped.toLocaleString()} already checked`;
      msg += ".";
    }
    finish(msg);
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

// Pressing Enter in Area (or clicking Fetch) starts the fetch.
els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  startFetch();
});

els.location.addEventListener("input", updateControls);

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
         "Click Fetch to continue — profiles already checked will be skipped.");
});

els.checkedForget.addEventListener("click", async () => {
  if (!confirm("Forget which GitHub profiles were already checked? " +
               "The next fetch will check every profile again. Saved addresses are kept.")) return;
  try {
    await fetch("/api/checked", { method: "DELETE" });
    state.checkedTotal = 0;
    updateControls();
  } catch {
    showError(els.error, "Could not clear the checked history.");
  }
});

// ---------- tabs ----------

const tabs = [
  { tab: $("tab-fetch"), panel: $("panel-fetch"), hash: "#fetch" },
  { tab: $("tab-send"), panel: $("panel-send"), hash: "#send" },
];

function selectTab(index, focus = false) {
  tabs.forEach((t, i) => {
    const active = i === index;
    t.tab.setAttribute("aria-selected", String(active));
    t.tab.tabIndex = active ? 0 : -1;
    t.panel.hidden = !active;
  });
  if (focus) tabs[index].tab.focus();
  history.replaceState(null, "", tabs[index].hash);
}

tabs.forEach((t, i) => {
  t.tab.addEventListener("click", () => selectTab(i));
  t.tab.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      const step = e.key === "ArrowRight" ? 1 : -1;
      selectTab((i + step + tabs.length) % tabs.length, true);
    }
  });
});

if (location.hash === "#send") selectTab(1);

updateControls();
loadStatus();
loadResults();


// ===========================================================================
// Send emails (self-contained; reuses $, showError and formatDuration above)
// ===========================================================================
(() => {
  const m = {
    card: $("send-card"),
    formWrap: $("gmail-form-wrap"),
    form: $("gmail-form"),
    address: $("gmail-address"),
    password: $("gmail-password"),
    senderName: $("gmail-name"),
    save: $("gmail-save"),
    cancel: $("gmail-cancel"),
    gmailError: $("gmail-error"),
    status: $("gmail-status"),
    who: $("gmail-who"),
    change: $("gmail-change"),
    compose: $("compose"),
    subject: $("subject"),
    content: $("content"),
    gap: $("gap"),
    cap: $("cap"),
    unsub: $("add-unsub"),
    sendBtn: $("send-btn"),
    stopBtn: $("send-stop-btn"),
    counter: $("sent-counter"),
    progress: $("send-progress"),
    bar: $("send-bar"),
    statusLine: $("send-status"),
    error: $("send-error"),
    logWrap: $("send-log-wrap"),
    logCount: $("send-log-count"),
    log: $("send-log"),
    recipientsTotal: $("recipients-total"),
    // stored-address picker
    pickRows: $("pick-rows"),
    pickEmpty: $("pick-empty"),
    pickSelected: $("pick-selected"),
    pickUnsent: $("pick-unsent"),
    pickAll: $("pick-all"),
    pickPager: $("pick-pager"),
    pickPagerInfo: $("pick-pager-info"),
    pickPagerButtons: $("pick-pager-buttons"),
    // block list
    blockForm: $("block-form"),
    blockInput: $("block-input"),
    blockAdd: $("block-add"),
    blockMsg: $("block-msg"),
    blockError: $("block-error"),
    blockFilter: $("block-filter"),
    blockList: $("block-list"),
    blockEmpty: $("block-empty"),
    blockCount: $("block-count"),
  };

  const s = {
    configured: false,
    changing: false,
    busy: false,
    jobId: null,
    processed: 0,
    capDefault: 200,
    capMax: 500,
  };
  let source = null;
  const sentAt = new Map();   // lower-case email -> when it was sent (from /api/sent)
  const selected = new Set(); // lower-case emails ticked in the stored-address table
  let blocked = new Map();    // lower-case email -> when it was blocked (from /api/blocked)
  let pickPage = 1;

  // ---------- stored-address picker ----------

  const emailKey = (r) => (r.email || "").trim().toLowerCase();

  /** Rows shown in the picker, after the "Only not sent yet" filter. */
  function pickList() {
    const rows = sortedRecords();
    return m.pickUnsent.checked ? rows.filter((r) => !sentAt.has(emailKey(r))) : rows;
  }

  /** Addresses in the current view that can be ticked (not sent, not blocked), de-duplicated. */
  function selectableKeys() {
    return [...new Set(pickList().map(emailKey)
      .filter((k) => k && !sentAt.has(k) && !blocked.has(k)))];
  }

  /** Everyone the next Send goes to: exactly the ticked rows, in table order. */
  function recipients() {
    return [...new Set(sortedRecords().map(emailKey).filter((k) => selected.has(k)))];
  }

  function formatSentAt(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "Sent" : `Sent ${d.toLocaleDateString()}`;
  }

  function pickRowFor(r) {
    const k = emailKey(r);
    const sent = sentAt.has(k);
    const isBlocked = blocked.has(k);
    const tr = document.createElement("tr");
    if (isBlocked) tr.className = "is-blocked";
    else if (sent) tr.className = "is-sent";

    const checkCell = td(undefined, "check-col");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(k);
    cb.disabled = sent || isBlocked || s.busy;
    cb.setAttribute("aria-label", `Select ${r.email}`);
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(k);
      else selected.delete(k);
      renderPick();     // also re-ticks other rows with the same address
    });
    checkCell.appendChild(cb);

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
    const status = isBlocked ? td("Blocked", "status-cell blocked")
      : td(sent ? formatSentAt(sentAt.get(k)) : "Not sent",
           sent ? "status-cell sent" : "status-cell");
    tr.append(
      checkCell,
      avatarCell,
      td(r.name || "—"),
      linkTd("@" + r.login, profile),
      td(r.location || "—"),
      td(r.email),
      status,
    );

    // Clicking anywhere on the row (except a link) toggles its checkbox.
    if (!cb.disabled) {
      tr.classList.add("selectable");
      tr.addEventListener("click", (e) => {
        if (e.target === cb || e.target.closest("a")) return;
        cb.click();
      });
    }
    return tr;
  }

  function renderPick() {
    const view = paginate(pickList(), pickPage);
    pickPage = view.page;
    m.pickRows.replaceChildren(...view.pageRows.map(pickRowFor));

    m.pickEmpty.hidden = view.totalRows > 0;
    m.pickEmpty.textContent = records.size === 0
      ? "No stored addresses. Fetch some in the Fetch tab first."
      : "Every stored address has already been sent to.";
    renderPager({ pager: m.pickPager, info: m.pickPagerInfo, buttons: m.pickPagerButtons },
                view, (p) => { pickPage = p; renderPick(); });

    const keys = selectableKeys();
    const allTicked = keys.length > 0 && keys.every((k) => selected.has(k));
    m.pickAll.textContent = allTicked ? "Unselect all" : `Select all (${keys.length.toLocaleString()})`;
    m.pickAll.disabled = s.busy || keys.length === 0;
    m.pickSelected.textContent = `${selected.size.toLocaleString()} selected`;
    updateRecipients();
  }

  function updateRecipients() {
    const total = recipients().length;
    m.recipientsTotal.textContent = total
      ? `Will send to the ${total.toLocaleString()} address(es) ticked in the table above.`
      : "No recipients yet. Tick addresses in the table above.";
    m.recipientsTotal.classList.toggle("muted", total === 0);
    updateSendButton();
  }

  async function loadSent() {
    try {
      const rows = await (await fetch("/api/sent")).json();
      sentAt.clear();
      rows.forEach((r) => sentAt.set(r.email.toLowerCase(), r.sent_at));
      sentAt.forEach((_, k) => selected.delete(k));   // can't be sent twice anyway
    } catch {
      /* table still works; sent status just isn't shown */
    }
    renderPick();
  }

  m.pickUnsent.addEventListener("change", () => {
    pickPage = 1;
    renderPick();
  });

  m.pickAll.addEventListener("click", () => {
    const keys = selectableKeys();
    const allTicked = keys.every((k) => selected.has(k));
    keys.forEach((k) => (allTicked ? selected.delete(k) : selected.add(k)));
    renderPick();
  });

  recordListeners.push(renderPick);

  // ---------- block list ----------

  function formatDate(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : `Added ${d.toLocaleDateString()}`;
  }

  function setBlocked(rows) {
    blocked = new Map(rows.map((r) => [r.email.toLowerCase(), r.added_at]));
    blocked.forEach((_, k) => selected.delete(k));   // a blocked address can't stay ticked
    renderBlocked();
    renderPick();
  }

  function blockItem(email, addedAt) {
    const li = document.createElement("li");
    const addr = document.createElement("span");
    addr.className = "block-email";
    addr.textContent = email;
    const when = document.createElement("span");
    when.className = "block-date muted";
    when.textContent = formatDate(addedAt);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn secondary small";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", `Remove ${email} from the block list`);
    remove.addEventListener("click", () => unblock(email, remove));
    li.append(addr, when, remove);
    return li;
  }

  function renderBlocked() {
    const q = m.blockFilter.value.trim().toLowerCase();
    const items = [...blocked].filter(([k]) => !q || k.includes(q));
    m.blockList.replaceChildren(...items.map(([k, t]) => blockItem(k, t)));
    m.blockCount.textContent = blocked.size.toLocaleString();
    m.blockFilter.hidden = blocked.size < 10 && !q;
    m.blockEmpty.hidden = items.length > 0;
    m.blockEmpty.textContent = blocked.size ? "No blocked address matches that search."
                                            : "No blocked addresses yet.";
  }

  async function loadBlocked() {
    try {
      setBlocked(await (await fetch("/api/blocked")).json());
    } catch {
      showError(m.blockError, "Could not load the block list.");
    }
  }

  async function unblock(email, button) {
    button.disabled = true;
    showError(m.blockError, "");
    try {
      const res = await fetch(`/api/blocked?email=${encodeURIComponent(email)}`, { method: "DELETE" });
      if (!res.ok) throw new Error();
      m.blockMsg.textContent = `Removed ${email} from the block list.`;
      m.blockMsg.hidden = false;
      await loadBlocked();
    } catch {
      button.disabled = false;
      showError(m.blockError, "Could not remove that address. Please try again.");
    }
  }

  m.blockForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = m.blockInput.value.trim();
    if (!text) return;
    m.blockAdd.disabled = true;
    m.blockMsg.hidden = true;
    showError(m.blockError, "");
    try {
      const res = await fetch("/api/blocked", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ emails: text }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not add to the block list.");
      setBlocked(data.blocked);
      const parts = [`Blocked ${data.added} address(es).`];
      if (data.already) parts.push(`${data.already} already on the list.`);
      if (data.invalid.length) parts.push(`Ignored (not valid): ${data.invalid.join(", ")}`);
      m.blockMsg.textContent = parts.join(" ");
      m.blockMsg.hidden = false;
      m.blockInput.value = "";
    } catch (err) {
      showError(m.blockError, err.message || "Could not reach the server.");
    } finally {
      m.blockAdd.disabled = false;
    }
  });

  // Enter adds; Shift+Enter makes a new line for pasting several addresses.
  m.blockInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      m.blockForm.requestSubmit();
    }
  });

  m.blockFilter.addEventListener("input", renderBlocked);

  // ---------- gmail account ----------

  function renderAccount() {
    const showForm = !s.configured || s.changing;
    m.formWrap.hidden = !showForm;
    m.cancel.hidden = !s.configured;
    m.status.hidden = !s.configured || s.changing;
    m.compose.style.display = s.configured && !s.changing ? "" : "none";
    updateSendButton();
  }

  async function loadGmailStatus() {
    try {
      const data = await (await fetch("/api/gmail/status")).json();
      s.configured = data.configured;
      s.capDefault = data.cap_default;
      s.capMax = data.cap_max;
      if (data.configured) {
        m.who.textContent = data.sender_name
          ? `${data.sender_name} <${data.address}>` : data.address;
      }
      m.cap.max = String(data.cap_max);
      updateCounter(data.sent_today, data.sent_total);
    } catch {
      /* leave the form visible */
    }
    renderAccount();
  }

  function updateCounter(sentToday, sentTotal) {
    if (sentToday === undefined) return;
    const cap = Number(m.cap.value) || s.capDefault;
    const left = Math.max(0, cap - sentToday);
    m.counter.textContent =
      `${sentToday} sent today (${left} left before the daily limit) · ${sentTotal} sent in total`;
  }

  m.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const address = m.address.value.trim();
    const app_password = m.password.value.trim();
    const sender_name = m.senderName.value.trim();
    if (!address || !app_password) return;

    m.save.disabled = true;
    m.save.textContent = "Checking…";
    showError(m.gmailError, "");
    try {
      const res = await fetch("/api/gmail", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address, app_password, sender_name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not connect.");
      s.configured = true;
      s.changing = false;
      m.password.value = "";
      m.who.textContent = data.sender_name ? `${data.sender_name} <${data.address}>` : data.address;
      renderAccount();
      loadGmailStatus();
    } catch (err) {
      showError(m.gmailError, err.message || "Could not reach the server.");
    } finally {
      m.save.disabled = false;
      m.save.textContent = "Connect";
    }
  });

  m.change.addEventListener("click", () => {
    s.changing = true;
    showError(m.gmailError, "");
    renderAccount();
    m.address.focus();
  });

  m.cancel.addEventListener("click", () => {
    s.changing = false;
    m.password.value = "";
    showError(m.gmailError, "");
    renderAccount();
  });

  // ---------- send ----------

  function updateSendButton() {
    const ready = s.configured && !s.changing && !s.busy &&
      recipients().length > 0 && m.subject.value.trim() && m.content.value.trim();
    m.sendBtn.disabled = !ready;
  }

  [m.subject, m.content, m.cap].forEach((el) =>
    el.addEventListener("input", () => { updateSendButton(); updateCounter(); }));

  function addLog(cls, text) {
    const li = document.createElement("li");
    li.className = cls;
    li.textContent = text;
    m.log.appendChild(li);
    m.logCount.textContent = m.log.children.length;
    m.logWrap.hidden = false;
  }

  function setSendBusy(busy) {
    s.busy = busy;
    m.sendBtn.textContent = busy ? "Sending…" : "Send";
    m.stopBtn.hidden = !busy;
    [m.subject, m.content, m.gap, m.cap, m.unsub, m.change].forEach((el) => (el.disabled = busy));
    renderPick();         // locks the checkboxes while sending; also updates the Send button
  }

  function finishSend(message) {
    if (source) { source.close(); source = null; }
    setSendBusy(false);
    if (message !== undefined) m.statusLine.textContent = message;
    loadGmailStatus();   // refresh sent-today / total
    loadSent();          // mark newly sent rows and untick them
  }

  async function startSend() {
    showError(m.error, "");
    m.log.replaceChildren();
    m.logCount.textContent = "0";
    m.logWrap.hidden = true;
    s.processed = 0;
    s.jobId = null;

    const list = recipients();
    const payload = {
      recipients: list,
      subject: m.subject.value.trim(),
      body: m.content.value,
      gap: Number(m.gap.value) || 0,
      cap: Number(m.cap.value) || s.capDefault,
      add_unsub: m.unsub.checked,
    };

    setSendBusy(true);
    m.progress.hidden = false;
    m.bar.style.width = "0%";
    m.statusLine.textContent = "Connecting to Gmail…";

    let job;
    try {
      const res = await fetch("/api/send/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        if (data.code === "gmail_not_set") s.configured = false;
        throw new Error(data.error || "Could not start sending.");
      }
      job = data.job;
      s.jobId = job;
    } catch (err) {
      showError(m.error, err.message || "Could not reach the server.");
      finishSend("");
      renderAccount();
      return;
    }

    const total = list.length;
    const tick = () => {
      s.processed += 1;
      m.bar.style.width = `${Math.min(100, (s.processed / total) * 100)}%`;
    };

    source = new EventSource(`/api/send/stream?job=${job}`);
    const on = (type, fn) => source.addEventListener(type, (e) => fn(JSON.parse(e.data)));

    on("start", (d) => {
      m.statusLine.textContent =
        `Sending to ${d.total} recipient(s)… (${d.remaining} can be sent before today's limit)`;
    });
    on("sent", (d) => {
      tick();
      updateCounter(d.sent_today, undefined);
      m.statusLine.textContent = `Sent ${d.sent} · to ${d.email}`;
      addLog("sent", `✓ sent to ${d.email}`);
      sentAt.set(d.email.toLowerCase(), new Date().toISOString());   // grey the row out right away
      selected.delete(d.email.toLowerCase());
      renderPick();
    });
    on("skipped", (d) => {
      tick();
      addLog("skip", `— skipped ${d.email} (${d.reason})`);
    });
    on("error", (d) => {
      tick();
      addLog("fail", `✗ failed ${d.email}: ${d.message}`);
    });
    on("limit", (d) => {
      addLog("skip", `Daily limit of ${d.cap} reached — stopping for today.`);
    });
    on("done", (d) => {
      m.bar.style.width = "100%";
      finishSend(`Done. Sent ${d.sent}, skipped ${d.skipped}, failed ${d.failed}. ` +
                 `${d.sent_today} sent today.`);
    });
    on("fail", (d) => {
      showError(m.error, d.message);
      if (d.code === "bad_credentials" || d.code === "gmail_not_set") {
        s.configured = false;
        renderAccount();
      }
      finishSend("");
    });

    source.onerror = () => {
      if (!source) return;
      showError(m.error, "Lost connection to the server.");
      finishSend("");
    };
  }

  m.sendBtn.addEventListener("click", startSend);

  m.stopBtn.addEventListener("click", () => {
    if (s.jobId) {
      fetch("/api/send/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job: s.jobId }),
      }).catch(() => {});
    }
    finishSend("Stopped. Addresses already sent are saved. Click Send to continue with the rest.");
  });

  loadGmailStatus();
  loadSent();
  loadBlocked();
})();
