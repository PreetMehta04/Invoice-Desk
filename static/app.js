/* Invoice Desk — single-page UI. No build step: plain ES2020. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const app = $("#app");
let CONFIG = null;
let cleanup = [];             // per-page teardown (intervals, event sources)

// ------------------------------------------------------------------------------------------- utils
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inr(x, opts = {}) {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  const s = Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: opts.dec ?? (Number.isInteger(n) ? 0 : 2), maximumFractionDigits: 2 });
  return (n < 0 ? "-" : "") + "₹" + s;
}
function compact(x) {
  const n = Number(x || 0);
  if (n >= 1e7) return "₹" + (n / 1e7).toFixed(2) + " Cr";
  if (n >= 1e5) return "₹" + (n / 1e5).toFixed(2) + " L";
  return inr(n);
}
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }) + " " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
}
const secs = (ms) => (ms == null ? "—" : ms < 1 ? "<1 ms" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s");
const LABEL = { APPROVED: "Approved", NEEDS_REVIEW: "Needs review", REJECTED: "Rejected", ERROR: "Error" };
const OWNER_LABEL = { treasury: "Treasury", ap: "AP team", buyer: "Buyer", receiving: "Receiving", controller: "Controller", procurement: "Procurement", tax: "Tax" };

const I = {
  check: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.2 3L13 4.5"/></svg>',
  warn: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M8 3.5v5.5M8 12.2v.1"/></svg>',
  x: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  info: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M8 7.5V12M8 4.3v.1"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3"/></svg>',
  play: '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.5v9l7.5-4.5z"/></svg>',
  shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6l8-3z"/><path d="M12 8v5M12 16v.1" stroke-linecap="round"/></svg>',
  arrow: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h10M9 4l4 4-4 4"/></svg>',
  dl: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2v8M4.5 7L8 10.5 11.5 7M3 13h10"/></svg>',
  logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>',
};
const statusIcon = (s) => ({ pass: I.check, warn: I.warn, fail: I.x, info: I.info }[s] || I.info);

const TOKEN_KEY = "invoice_desk_auth_token";
let CURRENT_USER = null;
const getToken = () => localStorage.getItem(TOKEN_KEY);
const setToken = (t) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

async function api(path, opts = {}) {
  const token = getToken();
  const headers = { ...(opts.headers || {}) };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    setToken(null);
    CURRENT_USER = null;
    showAuthScreen("login", "Session expired. Please sign in again.");
    throw new Error("Authentication required");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 3200);
}
function chip(decision, live) {
  if (live) return `<span class="chip RUNNING"><span class="spin" style="width:10px;height:10px;border-width:1.5px"></span>Processing</span>`;
  if (!decision) return `<span class="chip plain">Queued</span>`;
  return `<span class="chip ${decision}">${LABEL[decision] || decision}</span>`;
}

// ------------------------------------------------------------------------------------------- router
const routes = [
  [/^$/, "dashboard", renderDashboard],
  [/^process$/, "process", renderProcess],
  [/^run\/([\w-]+)$/, "process", renderRun],
  [/^review$/, "review", renderReview],
  [/^data$/, "data", renderData],
  [/^rules$/, "rules", renderRules],
  [/^how$/, "how", renderHow],
];
async function route() {
  cleanup.forEach((f) => f());
  cleanup = [];
  const path = location.hash.replace(/^#\/?/, "");
  for (const [re, nav, fn] of routes) {
    const m = path.match(re);
    if (m) {
      $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === nav));
      app.innerHTML = "";
      try { await fn(...m.slice(1)); } catch (e) { app.innerHTML = `<div class="panel empty"><strong>Couldn't load this page</strong>${esc(e.message)}</div>`; }
      app.focus({ preventScroll: true });
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", route);
function every(ms, fn) { const h = setInterval(fn, ms); cleanup.push(() => clearInterval(h)); }

async function refreshNav() {
  try {
    const s = await api("/api/stats");
    $("#nav-review").textContent = s.pending_review || "";
  } catch (_) { /* ignore */ }
}

// ------------------------------------------------------------------------------------------- dashboard
let dashFilter = "";
async function renderDashboard() {
  app.innerHTML = `
    <div class="page-head">
      <div><h1>Dashboard</h1><p>Every invoice the process has handled, the decision it reached, and why.</p></div>
      <div style="display:flex;gap:8px">
        <a class="btn" href="/api/runs.csv" onclick="this.href='/api/runs.csv?token=' + encodeURIComponent(getToken() || '')">${I.dl} Export CSV</a>
        <a class="btn primary" href="#/process">${I.upload} Process invoices</a>
      </div>
    </div>
    <div id="kpis"></div>
    <div class="panel">
      <div class="panel-head">
        <h2>Processing history</h2>
        <div class="seg" id="filters">
          ${[["", "All"], ["APPROVED", "Approved"], ["NEEDS_REVIEW", "Needs review"], ["REJECTED", "Rejected"]]
            .map(([k, l]) => `<button data-f="${k}" class="${dashFilter === k ? "active" : ""}">${l}</button>`).join("")}
        </div>
      </div>
      <div id="runs"></div>
    </div>`;
  $("#filters").onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    dashFilter = b.dataset.f;
    $$("#filters button").forEach((x) => x.classList.toggle("active", x === b));
    load();
  };
  async function load() {
    const [s, runs] = await Promise.all([api("/api/stats"), api("/api/runs" + (dashFilter ? `?decision=${dashFilter}` : ""))]);
    const done = s.completed || 0;
    const pct = (k) => (done ? (s.by_decision[k] / done) * 100 : 0);
    $("#kpis").innerHTML = `
      <div class="kpis">
        <div class="panel kpi"><div class="label">Invoices processed</div><div class="value">${s.total}</div>
          <div class="foot">${compact(s.value_processed)} total value${s.running ? ` · ${s.running} running` : ""}</div></div>
        <div class="panel kpi"><div class="label">Straight-through rate</div><div class="value">${s.straight_through_rate}%</div>
          <div class="foot">approved with no human touch</div></div>
        <div class="panel kpi"><div class="label">Awaiting review</div><div class="value">${s.pending_review}</div>
          <div class="foot"><a href="#/review">Open review queue</a></div></div>
        <div class="panel kpi"><div class="label">Value stopped before payment</div><div class="value">${compact(s.value_protected)}</div>
          <div class="foot">duplicates, over-billing, bank changes${s.fraud_flags ? ` · ${s.fraud_flags} fraud flag${s.fraud_flags > 1 ? "s" : ""}` : ""}</div></div>
      </div>
      ${done ? `<div class="panel panel-body" style="margin-bottom:18px">
        <div class="legend" style="justify-content:space-between"><strong>Decision mix</strong><span class="muted">avg ${secs(s.avg_duration_ms)} per invoice</span></div>
        <div class="split"><span style="width:${pct("APPROVED")}%;background:var(--ok)"></span><span style="width:${pct("NEEDS_REVIEW")}%;background:var(--hold)"></span><span style="width:${pct("REJECTED")}%;background:var(--bad)"></span></div>
        <div class="legend"><span><i style="background:var(--ok)"></i>Approved ${s.by_decision.APPROVED}</span><span><i style="background:var(--hold)"></i>Needs review ${s.by_decision.NEEDS_REVIEW}</span><span><i style="background:var(--bad)"></i>Rejected ${s.by_decision.REJECTED}</span></div>
      </div>` : ""}`;
    $("#runs").innerHTML = runs.length ? `
      <table class="grid">
        <thead><tr><th>Received</th><th>Invoice</th><th class="hide-sm">Vendor</th><th class="hide-sm">PO</th><th class="num">Amount</th><th>Decision</th><th class="hide-sm">Reason</th><th class="num hide-sm">Time</th></tr></thead>
        <tbody>${runs.map((r) => {
          const live = r.status === "queued" || r.status === "running";
          const reviewed = r.review_status && r.review_status !== "pending";
          return `<tr class="click" data-id="${r.id}">
            <td class="nw">${when(r.created_at)}<div class="sub">${esc(r.source)}</div></td>
            <td><strong>${esc(r.invoice_number || "—")}</strong><div class="sub">${esc(r.filename)}</div></td>
            <td class="hide-sm">${esc(r.vendor_name || "—")}</td>
            <td class="hide-sm nw">${esc(r.po_number || "—")}</td>
            <td class="num">${inr(r.gross_amount)}</td>
            <td>${chip(r.final_decision, live)}${reviewed ? `<div class="sub">by reviewer</div>` : r.review_status === "pending" ? `<div class="sub">${esc(OWNER_LABEL[r.route_to] || "")}</div>` : ""}</td>
            <td class="hide-sm" style="max-width:340px">${esc(r.headline || "")}</td>
            <td class="num hide-sm">${secs(r.duration_ms)}</td></tr>`;
        }).join("")}</tbody></table>`
      : `<div class="empty"><strong>No invoices yet</strong>Drop a PDF or run one of the sample invoices to see the process in action.<div style="margin-top:12px"><a class="btn primary" href="#/process">Process an invoice</a></div></div>`;
    $$("#runs tr.click").forEach((tr) => (tr.onclick = () => (location.hash = `#/run/${tr.dataset.id}`)));
    $("#nav-review").textContent = s.pending_review || "";
    return s.running > 0;
  }
  await load();
  every(2500, () => load());
}

// ------------------------------------------------------------------------------------------- process
async function renderProcess() {
  const samples = await api("/api/samples");
  const groups = [
    ["happy", "Happy path", "Invoices that should flow straight through to payment."],
    ["edge", "Edge cases", "Realistic situations where the process has to behave differently — and explain why."],
    ["more", "More scenarios", "Other exceptions the rules handle."],
  ];
  app.innerHTML = `
    <div class="page-head">
      <div><h1>Process invoices</h1><p>Upload vendor invoices as PDFs. Each one is read, checked against procurement data and decided on — live.</p></div>
      <button class="btn" id="run-all">${I.play} Run all ${samples.length} samples</button>
    </div>
    <label class="drop" id="drop">
      ${I.upload}
      <strong>Drop invoice PDFs here, or click to choose files</strong>
      <span class="muted">Digital or scanned PDFs up to 15 MB. Files dropped into the <code>${esc(CONFIG.inbox_dir.split(/[\\/]/).slice(-2).join("/"))}</code> folder are picked up automatically, like an AP mailbox.</span>
      <input type="file" id="file" accept="application/pdf" multiple hidden>
    </label>
    ${groups.map(([g, title, desc]) => `
      <section class="sample-group">
        <header><div><h2>${title}</h2><p>${desc}</p></div></header>
        <div class="samples">${samples.filter((s) => s.group === g).map((s) => `
          <article class="panel sample ${g === "edge" ? "edge" : ""}">
            <h3>${esc(s.title)}</h3>
            <p class="blurb">${esc(s.blurb)}</p>
            <div class="meta"><span>${esc(s.vendor)}</span><span>${inr(s.gross)}</span></div>
            <div class="actions">
              <a href="/api/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener" class="small">View PDF</a>
              <span class="expect">Expected: ${LABEL[s.expected]}</span>
              <button class="btn sm primary" data-run="${esc(s.file)}">${I.play} Run</button>
            </div>
          </article>`).join("")}</div>
      </section>`).join("")}`;

  const input = $("#file");
  const drop = $("#drop");
  input.onchange = () => upload([...input.files]);
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
  $$("[data-run]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api(`/api/samples/${encodeURIComponent(b.dataset.run)}/run`, { method: "POST" });
      location.hash = `#/run/${r.run_id}`;
    } catch (e) { toast(e.message, true); b.disabled = false; }
  }));
  $("#run-all").onclick = async (e) => {
    e.target.disabled = true;
    for (const s of samples) await api(`/api/samples/${encodeURIComponent(s.file)}/run`, { method: "POST" });
    toast(`Started ${samples.length} runs`);
    location.hash = "#/";
  };
}
async function upload(files) {
  files = files.filter((f) => f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"));
  if (!files.length) return toast("Only PDF files can be processed", true);
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  try {
    const r = await api("/api/runs", { method: "POST", body: fd });
    r.errors.forEach((e) => toast(`${e.file}: ${e.error}`, true));
    location.hash = r.run_ids.length === 1 ? `#/run/${r.run_ids[0]}` : "#/";
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------------------------------- run view
async function renderRun(id) {
  let run = await api(`/api/runs/${id}`);
  const stages = CONFIG.stages;
  const state = {};   // stage key -> {status, payload}
  let docTab = "doc";
  const t0 = new Date(run.created_at).getTime();

  app.innerHTML = `
    <div class="run-head">
      <div>
        <div class="crumbs"><a href="#/">Dashboard</a> / Run ${esc(run.id)}</div>
        <h1 id="run-title">${esc(run.invoice_number || run.filename)}</h1>
        <div class="meta" id="run-meta"></div>
      </div>
      <div style="display:flex;gap:8px">
        <a class="btn" href="#/process">${I.upload} Process another</a>
        <a class="btn" id="dl-json" href="/api/runs/${id}/output.json" onclick="this.href='/api/runs/${id}/output.json?token=' + encodeURIComponent(getToken() || '')">${I.dl} Decision JSON</a>
      </div>
    </div>
    <div class="run-layout">
      <ol class="pipeline" id="pipeline">${stages.map((s, i) => `
        <li class="stage pending" data-key="${s.key}">
          <div class="node">${i + 1}</div>
          <div class="stage-card">
            <div class="stage-top"><div><h3>${esc(s.label)}</h3><div class="sum"></div></div><span class="dur"></span></div>
            <div class="stage-body"></div>
          </div>
        </li>`).join("")}</ol>
      <div class="run-side">
        <section class="panel decision" id="decision"></section>
        <section class="panel" id="docpanel">
          <div class="tabs doc-tabs" id="doctabs"></div>
          <div id="docbody"></div>
        </section>
      </div>
    </div>`;

  $("#pipeline").addEventListener("click", (e) => {
    const top = e.target.closest(".stage-top");
    if (!top) return;
    const li = top.closest(".stage");
    if (!li.classList.contains("pending") && !li.classList.contains("running")) li.classList.toggle("open");
  });

  function meta() {
    const live = run.live || run.status === "running" || run.status === "queued";
    const elapsed = live ? Date.now() - t0 : run.duration_ms;
    $("#run-meta").innerHTML = `<span>${esc(run.filename)}</span><span>Source: ${esc(run.source)}</span>
      <span>Engine: ${esc(run.engine || CONFIG.engine)}</span><span id="elapsed">${live ? "Elapsed " : "Took "}${secs(elapsed)}</span>`;
  }

  function applyEvent(ev) {
    if (ev.stage === "run" || ev.stage === "review") return;
    const li = $(`.stage[data-key="${ev.stage}"]`);
    if (!li) return;
    state[ev.stage] = ev;
    li.className = "stage " + (ev.status === "running" ? "running" : "done " + ev.status);
    const node = $(".node", li);
    const idx = stages.findIndex((s) => s.key === ev.stage) + 1;
    if (ev.status === "running") {
      node.innerHTML = `<span class="spin"></span>`;
      $(".sum", li).textContent = runningText(ev.stage);
      $(".dur", li).textContent = "";
      $(".stage-body", li).innerHTML = "";
      return;
    }
    node.innerHTML = statusIcon(ev.status) || idx;
    const p = ev.payload || {};
    $(".sum", li).textContent = p.summary || "";
    $(".dur", li).textContent = secs(p.duration_ms);
    $(".stage-body", li).innerHTML = stageBody(ev.stage, p);
    if ((ev.status !== "pass" && ev.stage !== "decide") || ["extract", "match"].includes(ev.stage)) li.classList.add("open");
  }

  function runningText(k) {
    return {
      intake: "Receiving file, fingerprinting, checking for a text layer…",
      extract: CONFIG.ai_enabled ? `Reading the document with ${CONFIG.engine}…` : "Reading the document (OCR + layout parser)…",
      validate: "Checking required fields, arithmetic, dates and GST…",
      vendor: "Looking up the vendor master and verifying bank details…",
      duplicate: "Searching the AP ledger for duplicates…",
      match: "Matching to purchase orders and goods receipts…",
      decide: "Applying decision rules…",
      post: "Posting outcome…",
    }[k];
  }

  function renderDecision() {
    const box = $("#decision");
    const live = run.live || run.status === "running" || run.status === "queued";
    const res = run.result;
    if (live || !res) {
      if (run.status === "error") {
        box.innerHTML = `<div class="stamp ERROR">Error</div><div class="headline">${esc(run.headline)}</div><p class="next">${esc(run.error || "")}</p>`;
        return;
      }
      const doneN = Object.values(state).filter((e) => e.status !== "running").length;
      box.className = "panel decision waiting";
      box.innerHTML = `<div style="display:flex;gap:10px;align-items:center"><span class="spin"></span><strong style="color:var(--ink)">Processing</strong></div>
        <p style="margin-top:6px">Stage ${Math.min(doneN + 1, stages.length)} of ${stages.length}. The decision appears here when every check has run.</p>`;
      return;
    }
    box.className = "panel decision";
    const d = run.decision;
    const final = run.final_decision;
    const issues = (state.decide?.payload?.data?.issues) || [];
    const rv = res.review;
    box.innerHTML = `
      ${res.fraud_risk ? `<div class="fraud">${I.shield} Payment-fraud risk flagged</div>` : ""}
      <div class="stamp ${final} ${box.dataset.seen ? "" : "landed"}">${LABEL[final] || final}</div>
      <div class="headline">${esc(res.headline)}</div>
      <p class="next">${rv ? (rv.action === "approve" ? "Approved by reviewer — posted to the AP ledger and scheduled for payment." : "Rejected by reviewer — nothing posted.") : esc(res.next_action)}</p>
      ${issues.length ? `<ul class="issues">${issues.map((x) => `<li class="${x.severity === "block" ? "block" : ""}"><strong>${esc(x.title)}</strong>${esc(x.detail)}<div class="who">Owner: ${esc(x.owner)}</div></li>`).join("")}</ul>` : ""}
      <div class="facts">
        <div><span>Total</span><strong>${inr(res.invoice?.total)}</strong></div>
        <div><span>Purchase order</span><strong>${esc(res.matched_po || "—")}</strong></div>
        <div><span>${final === "APPROVED" ? "Payment due" : "Checks passed"}</span><strong>${final === "APPROVED" ? esc(res.payment_due || "—") : `${res.checks.filter((c) => c.status === "pass").length} / ${res.checks.length}`}</strong></div>
      </div>
      ${res.actions?.length && !rv ? `<ul class="actions-list">${res.actions.map((a) => `<li>${I.arrow}<span>${esc(a.text)}</span></li>`).join("")}</ul>` : ""}
      ${rv ? `<div class="reviewed"><strong>${rv.action === "approve" ? "Approved" : "Rejected"} by ${esc(rv.reviewer)}</strong> · ${when(rv.at)}${rv.note ? `<div>“${esc(rv.note)}”</div>` : ""}<div class="muted small">Original decision: ${LABEL[d]}</div></div>` : ""}
      ${run.review_status === "pending" ? `
        <div class="review-box">
          <strong>Your decision</strong>
          <textarea id="rv-note" placeholder="Note for the audit trail (e.g. 'Called vendor on master number, bank change confirmed')"></textarea>
          <div class="row"><button class="btn ok" id="rv-approve">${I.check} Approve &amp; post</button><button class="btn bad" id="rv-reject">${I.x} Reject</button></div>
        </div>` : ""}`;
    box.dataset.seen = "1";
    if (run.review_status === "pending") {
      const act = async (action) => {
        try {
          await api(`/api/runs/${id}/review`, { method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ action, note: $("#rv-note").value }) });
          toast(action === "approve" ? "Approved and posted to the ledger" : "Rejected");
          run = await api(`/api/runs/${id}`);
          box.dataset.seen = "";
          renderDecision();
          refreshNav();
        } catch (e) { toast(e.message, true); }
      };
      $("#rv-approve").onclick = () => act("approve");
      $("#rv-reject").onclick = () => act("reject");
    }
  }

  function renderDoc() {
    const res = run.result;
    const tabs = [["doc", "Document"], ["data", "Extracted data"]];
    if (res) tabs.push(["json", "Output"]);
    if (res?.vendor_email) tabs.push(["email", "Vendor email"]);
    $("#doctabs").innerHTML = tabs.map(([k, l]) => `<button data-t="${k}" class="${docTab === k ? "active" : ""}">${l}</button>`).join("");
    $("#doctabs").onclick = (e) => { const b = e.target.closest("button"); if (b) { docTab = b.dataset.t; renderDoc(); } };
    const body = $("#docbody");
    if (docTab === "doc") {
      if (!$("iframe", body)) body.innerHTML = `<iframe class="doc-frame" src="/api/runs/${id}/file?token=${encodeURIComponent(getToken() || '')}#view=FitH&navpanes=0" title="Invoice PDF"></iframe>`;
    } else if (docTab === "data") {
      const ex = run.extracted || state.extract?.payload?.data?.extracted;
      body.innerHTML = ex ? `<div class="pane">${extractedView(ex)}</div>` : `<div class="empty">Extraction hasn't finished yet.</div>`;
    } else if (docTab === "json") {
      body.innerHTML = `<div class="pane"><pre>${esc(JSON.stringify(res, null, 2))}</pre></div>`;
    } else if (docTab === "email") {
      const m = res.vendor_email;
      body.innerHTML = `<div class="pane"><div class="email"><header><div><span class="muted">To</span> ${esc(m.to)}</div><div><span class="muted">Subject</span> <strong>${esc(m.subject)}</strong></div></header><pre>${esc(m.body)}</pre></div>
        <p class="muted small" style="margin-top:8px">Drafted automatically — an AP clerk reviews before sending.</p></div>`;
    }
  }

  (run.events || []).forEach(applyEvent);
  meta();
  renderDecision();
  renderDoc();

  const live = run.live || run.status === "running" || run.status === "queued";
  if (live) {
    every(250, meta);
    const es = new EventSource(`/api/runs/${id}/events?token=${encodeURIComponent(getToken() || '')}`);
    cleanup.push(() => es.close());
    const seen = new Set((run.events || []).map((e) => e.seq));
    es.onmessage = async (m) => {
      const ev = JSON.parse(m.data);
      if (seen.has(ev.seq)) return;
      seen.add(ev.seq);
      applyEvent(ev);
      if (ev.stage === "extract" && ev.status !== "running" && docTab === "data") renderDoc();
      if (ev.stage === "run" && ev.status !== "running") {
        es.close();
        run = await api(`/api/runs/${id}`);
        $("#run-title").textContent = run.invoice_number || run.filename;
        meta();
        renderDecision();
        renderDoc();
        refreshNav();
      } else {
        renderDecision();
      }
    };
    es.addEventListener("end", () => es.close());
  }
}

function checksList(checks) {
  if (!checks?.length) return "";
  return `<ul class="checks">${checks.map((c) => `
    <li><span class="ic ${c.status}">${statusIcon(c.status)}</span>
      <div><span class="lbl">${esc(c.label)}</span>${c.owner && c.status !== "pass" && c.status !== "info" ? `<span class="owner">→ ${esc(OWNER_LABEL[c.owner] || c.owner)}</span>` : ""}
      <div class="det">${esc(c.detail)}</div></div></li>`).join("")}</ul>`;
}
function kv(obj) {
  const rows = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return `<dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
}

function stageBody(key, p) {
  const d = p.data || {};
  let extra = "";
  if (key === "intake") {
    extra = kv({ "Document": esc(d.document_kind), "Pages": d.pages, "Size": d.size_kb + " KB",
      "OCR confidence": d.ocr_confidence != null ? d.ocr_confidence + "%" : null, "SHA-256": `<code>${esc((d.sha256 || "").slice(0, 16))}…</code>` });
  } else if (key === "extract" && d.extracted) {
    const ex = d.extracted;
    extra = kv({ "Engine": esc(d.engine), "Key fields found": esc(d.fields_found),
      "Tokens": d.tokens ? `${d.tokens.input_tokens ?? "?"} in / ${d.tokens.output_tokens ?? "?"} out` : null }) +
      `<div style="margin-top:10px">${extractedView(ex, true)}</div>`;
  } else if (key === "validate") {
    extra = kv({ "Taxable value": inr(d.taxable_value), "GST": inr(d.tax_total), "Total": inr(d.total), "Pricing": esc(d.pricing) });
  } else if (key === "vendor" && d.vendor_id) {
    extra = kv({ "Vendor": `${esc(d.vendor)} <span class="muted">${esc(d.vendor_id)}</span>`, "Matched by": esc(d.match),
      "Payment terms": `Net ${d.payment_terms_days}`, "Bank on file": esc(d.master_bank) });
  } else if (key === "duplicate") {
    extra = kv({ "Prior invoices checked": d.prior_invoices_checked, "Normalised number": d.normalised_number ? `<code>${esc(d.normalised_number)}</code>` : null });
  } else if (key === "match") {
    extra = matchView(d);
  } else if (key === "decide" && d.decision) {
    const nb = (d.issues || []).filter((x) => x.severity === "block").length;
    extra = kv({ "Outcome": chip(d.decision), "Checks passed": `${d.checks_passed} of ${d.checks_total}`,
      "Blocking failures": nb, "Exceptions for review": (d.issues || []).length - nb,
      "Routed to": d.route_to_label ? esc(d.route_to_label) : null, "Rule applied": esc(
        d.decision === "REJECTED" ? "Any blocking failure → reject" : d.decision === "NEEDS_REVIEW" ? "No blocking failure, at least one exception → hold for review" : "No blocking failures and no exceptions → approve") });
  } else if (key === "post") {
    extra = d.actions?.length ? `<ul class="actions-list">${d.actions.map((a) => `<li>${I.arrow}<span>${esc(a.text)}</span></li>`).join("")}</ul>` : "";
  }
  const checks = key === "decide" ? [] : (p.checks || []).filter((c) => !(key === "post"));
  return checksList(checks) + (extra ? `<div class="stage-data">${extra}</div>` : "");
}

function matchView(d) {
  let out = "";
  if (d.candidates?.length) {
    out += `<h3 style="margin:4px 0 6px">PO candidates considered</h3><table class="grid"><thead><tr><th>PO</th><th>Description</th><th class="num">Remaining</th><th class="num">Score</th></tr></thead><tbody>
      ${d.candidates.map((c, i) => `<tr><td><strong>${esc(c.po_number)}</strong>${i === 0 && d.po ? ' <span class="chip APPROVED">chosen</span>' : ""}</td><td>${esc(c.description)}</td><td class="num">${inr(c.remaining)}</td><td class="num">${Math.round(c.score * 100)}%</td></tr>`).join("")}
      </tbody></table>`;
  }
  if (d.po) {
    const po = d.po;
    const billedPct = Math.min(100, (po.billed_net / po.total_amount) * 100);
    out += `<div style="margin-top:${d.candidates?.length ? 14 : 0}px">${kv({
      "Purchase order": `<strong>${esc(po.po_number)}</strong> · ${esc(po.description)}`,
      "Type": po.po_type === "goods" ? "Goods (3-way match)" : "Services (2-way match)",
      "Requester": `${esc(po.requester)}, ${esc(po.department)}`,
      "PO value": inr(po.total_amount),
      "Billed before": `${inr(po.billed_net)} across ${po.prior_invoices.length} invoice(s) <div class="minibar" style="margin-top:4px;max-width:220px"><span style="width:${billedPct}%"></span></div>`,
      "Prior invoices": po.prior_invoices.length ? po.prior_invoices.map((i) => `${esc(i.invoice_number)} (${inr(i.net_amount)})`).join(", ") : null,
    })}</div>`;
  }
  if (d.lines?.length) {
    out += `<table class="grid" style="margin-top:12px"><thead><tr><th>Invoice line → PO line</th><th class="num">Qty</th><th class="num">Ordered / received</th><th class="num">Unit price vs PO</th><th class="num">Variance</th></tr></thead><tbody>
      ${d.lines.map((l) => l.po_line ? `<tr>
        <td>${esc(l.invoice_line)}<div class="sub">→ L${l.po_line} ${esc(l.po_description)} · ${l.similarity}% match</div></td>
        <td class="num">${l.qty}${l.qty_billed_before ? `<div class="sub">+${l.qty_billed_before} earlier</div>` : ""}</td>
        <td class="num">${l.po_qty} / ${d.po?.po_type === "goods" ? l.qty_received : "n/a"}</td>
        <td class="num">${inr(l.invoice_unit_net)}<div class="sub">PO ${inr(l.po_unit_price)}</div></td>
        <td class="num ${l.price_variance > 0.5 ? "var-up" : l.price_variance < -0.5 ? "var-down" : ""}">${Math.abs(l.price_variance) < 0.5 ? "—" : inr(l.price_variance) + `<div class="sub">${l.price_variance_pct > 0 ? "+" : ""}${l.price_variance_pct}%</div>`}</td></tr>`
        : `<tr><td>${esc(l.invoice_line)}<div class="sub" style="color:var(--bad)">Not on the PO</div></td><td class="num">${l.qty ?? ""}</td><td class="num">—</td><td class="num">${inr(l.invoice_unit_net)}</td><td class="num">—</td></tr>`).join("")}
      </tbody></table>`;
  }
  return out;
}

function extractedView(ex, compactMode = false) {
  const head = {
    "Document type": esc((ex.document_type || "").replace(/_/g, " ")),
    "Vendor": esc(ex.vendor_name), "Vendor GSTIN": ex.vendor_gstin ? `<code>${esc(ex.vendor_gstin)}</code>` : "<span class='muted'>not found</span>",
    "Invoice number": ex.invoice_number ? `<strong>${esc(ex.invoice_number)}</strong>` : "<span style='color:var(--bad)'>missing</span>",
    "Invoice date": esc(ex.invoice_date),
    "PO reference": ex.po_number ? esc(ex.po_number) : `<span class="muted">none${ex.other_references ? ` — “${esc(ex.other_references)}”` : ""}</span>`,
    "Service period": esc(ex.service_period),
    "Taxable value": inr(ex.subtotal), "Tax": ex.tax_lines?.length ? ex.tax_lines.map((t) => `${esc(t.type)} ${t.rate ?? ""}% ${inr(t.amount)}`).join(" · ") : null,
    "Total": `<strong>${inr(ex.total)}</strong>${ex.prices_include_tax ? ' <span class="chip plain">tax-inclusive</span>' : ""}`,
    "Bank": ex.bank_account_number ? `${esc(ex.bank_name || "")} · ${esc(ex.bank_account_number)} · ${esc(ex.bank_ifsc || "")}` : null,
    "Notes": ex.notes?.length ? ex.notes.map((n) => `“${esc(n)}”`).join("<br>") : null,
  };
  const lines = ex.line_items?.length ? `<table class="grid" style="margin-top:10px"><thead><tr><th>Description</th>${compactMode ? "" : "<th>HSN/SAC</th>"}<th class="num">Qty</th><th class="num">Rate</th><th class="num">Amount</th></tr></thead><tbody>
    ${ex.line_items.map((l) => `<tr><td>${esc(l.description)}</td>${compactMode ? "" : `<td>${esc(l.hsn_sac || "")}</td>`}<td class="num">${l.quantity ?? ""} ${esc(l.unit || "")}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted" style="margin-top:8px">No line items extracted.</p>`;
  return kv(head) + lines;
}

// ------------------------------------------------------------------------------------------- review queue
async function renderReview() {
  app.innerHTML = `<div class="page-head"><div><h1>Review queue</h1><p>Invoices the process held back. Each one says what's wrong and who should look at it.</p></div></div><div class="panel" id="q"></div>`;
  async function load() {
    const runs = await api("/api/runs?decision=PENDING_REVIEW");
    $("#q").innerHTML = runs.length ? `<table class="grid"><thead><tr><th>Invoice</th><th>Why it's held</th><th>Owner</th><th class="num">Amount</th><th>Received</th><th></th></tr></thead><tbody>
      ${runs.map((r) => `<tr class="click" data-id="${r.id}"><td><strong>${esc(r.invoice_number || "—")}</strong><div class="sub">${esc(r.vendor_name || "")}</div></td>
        <td style="max-width:420px">${esc(r.headline)}</td><td><span class="chip NEEDS_REVIEW">${esc(OWNER_LABEL[r.route_to] || r.route_to || "AP")}</span></td>
        <td class="num">${inr(r.gross_amount)}</td><td>${when(r.created_at)}</td><td class="num"><span class="btn sm">Open</span></td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><strong>Nothing waiting for review</strong>Invoices with exceptions will appear here.</div>`;
    $$("#q tr.click").forEach((tr) => (tr.onclick = () => (location.hash = `#/run/${tr.dataset.id}`)));
    $("#nav-review").textContent = runs.length || "";
  }
  await load();
  every(4000, load);
}

// ------------------------------------------------------------------------------------------- procurement data
let dataTab = "pos";
async function renderData() {
  const d = await api("/api/procurement");
  const vmap = Object.fromEntries(d.vendors.map((v) => [v.vendor_id, v.name]));
  app.innerHTML = `<div class="page-head"><div><h1>Procurement data</h1><p>The system of record the process checks against. Approved invoices update PO billing live.</p></div></div>
    <div class="panel"><div class="tabs" id="dtabs" style="padding:0 10px">
      ${[["pos", `Purchase orders (${d.pos.length})`], ["vendors", `Vendors (${d.vendors.length})`], ["grns", "Goods receipts"], ["ledger", `AP ledger (${d.ledger.length})`]]
        .map(([k, l]) => `<button data-t="${k}" class="${dataTab === k ? "active" : ""}">${l}</button>`).join("")}
    </div><div id="dbody"></div></div>`;
  $("#dtabs").onclick = (e) => { const b = e.target.closest("button"); if (b) { dataTab = b.dataset.t; renderData(); } };
  const body = $("#dbody");
  if (dataTab === "pos") {
    body.innerHTML = d.pos.map((po) => {
      const pct = (po.billed_net / po.total_amount) * 100;
      return `<details class="po"><summary><strong>${esc(po.po_number)}</strong><span>${esc(po.description)}<div class="sub muted small">${esc(vmap[po.vendor_id])} · ${po.po_type}</div></span>
        <span class="num">${inr(po.total_amount)}<div class="muted small">${inr(po.remaining_net)} left</div></span>
        <span><div class="minibar"><span class="${pct > 100.5 ? "over" : ""}" style="width:${Math.min(100, pct)}%"></span></div><div class="muted small">${pct.toFixed(0)}% billed · ${po.invoices.length} invoice(s)</div></span>
        <span class="chip plain">${esc(po.status)}</span></summary>
        <div class="inner"><table class="grid"><thead><tr><th>Line</th><th>Item</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Billed</th><th class="num">Unit price</th><th class="num">Amount</th></tr></thead><tbody>
          ${po.lines.map((l) => `<tr><td>${l.line_no}</td><td>${esc(l.description)}<div class="sub">${esc(l.item_code)}</div></td><td class="num">${l.qty} ${esc(l.uom)}</td><td class="num">${po.po_type === "goods" ? l.qty_received : "n/a"}</td><td class="num">${l.qty_billed}</td><td class="num">${inr(l.unit_price)}</td><td class="num">${inr(l.amount)}</td></tr>`).join("")}
        </tbody></table>
        ${po.invoices.length ? `<p class="small" style="margin-top:8px"><span class="muted">Billed by:</span> ${po.invoices.map((i) => `${esc(i.invoice_number)} (${inr(i.net_amount)}, ${esc(i.status)})`).join(" · ")}</p>` : ""}</div></details>`;
    }).join("");
  } else if (dataTab === "vendors") {
    body.innerHTML = `<table class="grid"><thead><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>Status</th><th>Verified bank account</th><th class="num">Terms</th></tr></thead><tbody>
      ${d.vendors.map((v) => `<tr><td>${esc(v.vendor_id)}</td><td><strong>${esc(v.name)}</strong><div class="sub">${esc(v.category)} · ${esc(v.email)}</div></td><td><code>${esc(v.gstin)}</code></td>
        <td>${v.status === "approved" ? '<span class="chip APPROVED">Approved</span>' : `<span class="chip REJECTED">${esc(v.status)}</span><div class="sub">${esc(v.status_reason)}</div>`}</td>
        <td>${esc(v.bank_name)}<div class="sub">${esc(v.bank_account)} · ${esc(v.ifsc)}</div></td><td class="num">Net ${v.payment_terms_days}</td></tr>`).join("")}</tbody></table>`;
  } else if (dataTab === "grns") {
    body.innerHTML = `<table class="grid"><thead><tr><th>GRN</th><th>PO</th><th class="num">Line</th><th class="num">Qty received</th><th>Date</th><th>Received by</th></tr></thead><tbody>
      ${d.grns.map((g) => `<tr><td>${esc(g.grn_number)}</td><td>${esc(g.po_number)}</td><td class="num">${g.line_no}</td><td class="num">${g.qty_received}</td><td>${esc(g.received_date)}</td><td>${esc(g.received_by)}</td></tr>`).join("")}</tbody></table>`;
  } else {
    body.innerHTML = `<table class="grid"><thead><tr><th>Invoice</th><th>Vendor</th><th>Date</th><th>PO</th><th class="num">Taxable</th><th class="num">Gross</th><th>Status</th><th>Source</th></tr></thead><tbody>
      ${d.ledger.map((l) => `<tr><td><strong>${esc(l.invoice_number || "—")}</strong></td><td>${esc(vmap[l.vendor_id] || l.vendor_id)}</td><td>${esc(l.invoice_date)}</td><td>${esc(l.po_number || "—")}</td>
        <td class="num">${inr(l.net_amount)}</td><td class="num">${inr(l.gross_amount)}</td><td><span class="chip ${l.status === "approved" || l.status === "paid" ? "APPROVED" : l.status === "in_review" ? "NEEDS_REVIEW" : "REJECTED"}">${esc(l.status.replace("_", " "))}</span></td>
        <td>${l.run_id ? `<a href="#/run/${esc(l.run_id)}">this system</a>` : "history"}</td></tr>`).join("")}</tbody></table>`;
  }
}

// ------------------------------------------------------------------------------------------- rules
async function renderRules() {
  const r = await api("/api/rules");
  const fields = [
    ["price_tolerance_pct", "Price tolerance (%)", "Invoices may exceed PO prices by up to this percentage…"],
    ["price_tolerance_abs", "Price tolerance cap (₹)", "…but never by more than this amount in total (the lesser applies)."],
    ["po_balance_tolerance_abs", "PO balance tolerance (₹)", "Allowed over-billing of a PO's remaining value, for rounding."],
    ["auto_approve_limit", "Auto-approval limit (₹)", "Invoices above this need controller sign-off even when everything matches."],
    ["po_inference_min_confidence", "PO inference confidence (0–1)", "Minimum score to match an invoice without a PO number to an open PO."],
    ["duplicate_window_days", "Near-duplicate window (days)", "Same vendor and amount within this many days is flagged as a possible duplicate."],
    ["max_invoice_age_days", "Maximum invoice age (days)", "Older invoices are held — they may already have been paid."],
    ["arithmetic_tolerance_abs", "Arithmetic tolerance (₹)", "Rounding allowed when checking that lines, tax and total add up."],
    ["stage_delay_ms", "Live view pacing (ms)", "Minimum time each stage stays on screen so the run is easy to follow. Set to 0 for full speed."],
  ];
  app.innerHTML = `<div class="page-head"><div><h1>Rules</h1><p>The thresholds the decision engine applies. Changes take effect on the next invoice processed.</p></div></div>
    <div class="panel"><div class="panel-body"><form id="rf" class="rules-form">
      ${fields.map(([k, l, h]) => `<div class="field"><label for="f-${k}">${l}</label><div class="hint">${h}</div><input type="number" step="any" id="f-${k}" name="${k}" value="${esc(r[k])}"></div>`).join("")}
      <div class="field"><label><input type="checkbox" name="require_grn_for_goods" ${r.require_grn_for_goods ? "checked" : ""}>Require goods receipt for goods POs</label><div class="hint">3-way match: you only pay for what the warehouse has received.</div></div>
    </form>
    <div style="display:flex;gap:8px;margin-top:18px"><button class="btn primary" id="save">Save rules</button></div></div></div>
    <div class="panel" style="margin-top:18px"><div class="panel-body" style="display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap">
      <div><h2>Reset demo data</h2><p class="muted">Restores vendors, POs, receipts, AP ledger and rules to their original state and clears run history.</p></div>
      <button class="btn bad" id="reset">Reset demo data</button></div></div>`;
  $("#save").onclick = async () => {
    const body = {};
    $$("#rf input").forEach((i) => (body[i.name] = i.type === "checkbox" ? i.checked : i.value));
    try { await api("/api/rules", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }); toast("Rules saved"); CONFIG = await api("/api/config"); }
    catch (e) { toast(e.message, true); }
  };
  const rb = $("#reset");
  rb.onclick = async () => {
    if (!rb.dataset.armed) { rb.dataset.armed = "1"; rb.textContent = "Click again to confirm"; setTimeout(() => { rb.dataset.armed = ""; rb.textContent = "Reset demo data"; }, 4000); return; }
    try { await api("/api/reset", { method: "POST" }); toast("Demo data reset"); refreshNav(); renderRules(); }
    catch (e) { toast(e.message, true); }
  };
}

// ------------------------------------------------------------------------------------------- how it works
function renderHow() {
  const steps = [
    ["Intake", "PDF arrives by upload or the watched inbox folder. It is fingerprinted (SHA-256) and checked for a text layer — scanned images are flagged."],
    ["AI extraction", "A multimodal model reads the page — digital or scanned — into a fixed schema: vendor, GSTIN, invoice number, dates, PO reference, line items, tax, total, bank details, remarks. Key values are cross-checked against the page text."],
    ["Validation", "Required fields, line and total arithmetic, tax-inclusive pricing, GST type for the place of supply, GSTIN checksum, date sanity."],
    ["Vendor verification", "Match to the vendor master by GSTIN (then fuzzy name). Blocked vendors stop here. Bank details must equal the verified account on file."],
    ["Duplicate check", "Exact file, normalised invoice number (BLL/2026/0458 = BLL-2026-0458), and near-duplicates by amount, date and service period."],
    ["PO & receipt matching", "Explicit or inferred PO. Line-by-line price and quantity checks with tolerance, cumulative billing across split invoices, and goods-receipt (3-way) match."],
    ["Decision", "Any blocking failure → Rejected. Any exception → Needs review, routed to the owner who can resolve it. Otherwise → Approved."],
    ["Post & notify", "Approved invoices update the AP ledger and PO balance and are scheduled for payment. Rejections get a drafted vendor email."],
  ];
  app.innerHTML = `<div class="page-head"><div><h1>How it works</h1><p>AI reads the messy document. Deterministic rules make the decision — so every outcome can be explained and audited.</p></div></div>
    <ol class="flow">${steps.map(([t, d]) => `<li class="panel"><h3>${t}</h3><p>${d}</p></li>`).join("")}</ol>
    <div class="panel" style="margin-top:18px"><div class="panel-head"><h2>Decision outcomes</h2></div>
      <table class="grid"><thead><tr><th>Outcome</th><th>When</th><th>What happens</th></tr></thead><tbody>
        <tr><td><span class="chip APPROVED">Approved</span></td><td>Every check passes (price variance within tolerance is fine).</td><td>Posted to the AP ledger, PO balance consumed, scheduled for payment on the due date to the verified bank account.</td></tr>
        <tr><td><span class="chip NEEDS_REVIEW">Needs review</span></td><td>Something a person must judge: bank change, price above tolerance, over-billing a PO, unreceived goods, possible duplicate, uncertain PO, high value.</td><td>Held and routed to the right owner (Treasury, Buyer, Receiving, AP, Controller). Reviewer approves or rejects with a note; the ledger updates accordingly.</td></tr>
        <tr><td><span class="chip REJECTED">Rejected</span></td><td>The document can't be paid as-is: duplicate, unknown or blocked vendor, proforma, missing invoice number, totals that don't add up, no PO at all.</td><td>Nothing is posted. A vendor email explaining what's needed is drafted.</td></tr>
      </tbody></table></div>
    <div class="panel" style="margin-top:18px"><div class="panel-body prose">
      <h2 style="margin-bottom:8px">Assumptions</h2>
      <p>The buyer is Kestrel Foods Pvt Ltd (Bengaluru, GSTIN 29AAKCK4821F1ZF). PO values are tax-exclusive; matching and tolerance use taxable value. Service POs use a 2-way match, goods POs a 3-way match against goods receipts. Only paid or approved invoices consume PO balance; invoices in review are recorded so a resubmission is caught, but don't consume the PO.</p>
      <p>Bank-detail changes are never accepted from an invoice: they are held for Treasury to verify through the contact on the vendor master. Rejection emails are drafted, not sent — a person stays in the loop for outbound communication.</p>
    </div></div>`;
}

// ------------------------------------------------------------------------------------------- auth UI & session
const authScreen = $("#auth-screen");
const appShell = $("#app-shell");

function showAuthScreen(initialMode = "login", initialError = "") {
  cleanup.forEach((f) => f());
  cleanup = [];
  if (appShell) appShell.style.display = "none";
  if (authScreen) authScreen.style.display = "flex";
  let mode = initialMode;

  function renderForm(err = "") {
    authScreen.innerHTML = `
      <div class="auth-card">
        <div class="auth-brand">
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#2B3A8C"/><path d="M9 9h14M9 15h14M9 21h8" stroke="white" stroke-width="3" stroke-linecap="round"/></svg>
          </span>
          <h1>Invoice Desk</h1>
          <p>Kestrel Foods · Accounts Payable Portal</p>
        </div>

        <div class="auth-tabs" role="tablist">
          <button type="button" class="auth-tab ${mode === "login" ? "active" : ""}" id="tab-login">Sign In</button>
          <button type="button" class="auth-tab ${mode === "signup" ? "active" : ""}" id="tab-signup">Create Account</button>
        </div>

        <div id="auth-err" class="auth-error ${err ? "show" : ""}">${esc(err)}</div>

        ${mode === "login" ? `
          <form class="auth-form" id="form-auth">
            <div class="auth-field">
              <label for="auth-email">Work Email</label>
              <input id="auth-email" type="email" placeholder="name@kestrel.com" required autocomplete="username" />
            </div>
            <div class="auth-field">
              <label for="auth-password">Password</label>
              <input id="auth-password" type="password" placeholder="••••••••" required autocomplete="current-password" />
            </div>
            <button type="submit" class="btn primary auth-submit" id="btn-submit">Sign In</button>
          </form>

          <div class="auth-demo-box">
            <div>
              <strong>Quick Demo Access</strong><br>
              <span class="muted">demo@kestrel.com / Password@123</span>
            </div>
            <button type="button" class="auth-demo-btn" id="btn-fill-demo">Fill Demo</button>
          </div>

          <div class="auth-foot">
            Don't have an account? <a href="#" id="link-switch">Create one here</a>
          </div>
        ` : `
          <form class="auth-form" id="form-auth">
            <div class="auth-field">
              <label for="auth-name">Full Name</label>
              <input id="auth-name" type="text" placeholder="e.g. Preet Mehta" required />
            </div>
            <div class="auth-field">
              <label for="auth-email">Work Email</label>
              <input id="auth-email" type="email" placeholder="name@kestrel.com" required autocomplete="email" />
            </div>
            <div class="auth-field">
              <label for="auth-password">Password</label>
              <input id="auth-password" type="password" placeholder="Minimum 6 characters" minlength="6" required autocomplete="new-password" />
            </div>
            <div class="auth-field">
              <label for="auth-confirm">Confirm Password</label>
              <input id="auth-confirm" type="password" placeholder="Re-enter password" minlength="6" required autocomplete="new-password" />
            </div>
            <button type="submit" class="btn primary auth-submit" id="btn-submit">Create Account</button>
          </form>

          <div class="auth-foot">
            Already have an account? <a href="#" id="link-switch">Sign in here</a>
          </div>
        `}
      </div>
    `;

    $("#tab-login").onclick = () => { mode = "login"; renderForm(); };
    $("#tab-signup").onclick = () => { mode = "signup"; renderForm(); };
    const linkSwitch = $("#link-switch");
    if (linkSwitch) {
      linkSwitch.onclick = (e) => {
        e.preventDefault();
        mode = mode === "login" ? "signup" : "login";
        renderForm();
      };
    }

    const fillBtn = $("#btn-fill-demo");
    if (fillBtn) {
      fillBtn.onclick = () => {
        $("#auth-email").value = "demo@kestrel.com";
        $("#auth-password").value = "Password@123";
      };
    }

    const form = $("#form-auth");
    form.onsubmit = async (e) => {
      e.preventDefault();
      const submitBtn = $("#btn-submit");
      submitBtn.disabled = true;
      submitBtn.textContent = mode === "login" ? "Signing In..." : "Creating Account...";

      try {
        if (mode === "login") {
          const email = $("#auth-email").value.trim();
          const password = $("#auth-password").value;
          const res = await api("/api/auth/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
          });
          setToken(res.token);
          CURRENT_USER = res.user;
          toast(`Welcome back, ${CURRENT_USER.name}!`);
          showAppShell();
        } else {
          const name = $("#auth-name").value.trim();
          const email = $("#auth-email").value.trim();
          const password = $("#auth-password").value;
          const confirm_password = $("#auth-confirm").value;

          if (password !== confirm_password) {
            renderForm("Passwords do not match");
            return;
          }

          const res = await api("/api/auth/signup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, email, password, confirm_password }),
          });
          setToken(res.token);
          CURRENT_USER = res.user;
          toast(`Account created! Welcome, ${CURRENT_USER.name}!`);
          showAppShell();
        }
      } catch (err) {
        renderForm(err.message || "Authentication failed");
      }
    };
  }

  renderForm(initialError);
}

function renderUserBadge() {
  const ub = $("#user-badge");
  if (!ub || !CURRENT_USER) return;
  const initials = (CURRENT_USER.name || "U")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  ub.innerHTML = `
    <div class="user-info">
      <div class="user-avatar" title="${esc(CURRENT_USER.email)}">${esc(initials)}</div>
      <div class="user-meta">
        <span class="user-name">${esc(CURRENT_USER.name)}</span>
        <span class="user-role">${esc(CURRENT_USER.role || CURRENT_USER.email)}</span>
      </div>
    </div>
    <button class="user-logout" id="btn-logout" title="Log Out" aria-label="Log Out">
      ${I.logout}
    </button>
  `;

  $("#btn-logout").onclick = async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch (_) {}
    setToken(null);
    CURRENT_USER = null;
    toast("Signed out successfully");
    showAuthScreen("login");
  };
}

let navInterval = null;
function showAppShell() {
  if (authScreen) authScreen.style.display = "none";
  if (appShell) appShell.style.display = "grid";
  renderUserBadge();
  refreshNav();
  if (!navInterval) {
    navInterval = setInterval(() => {
      if (CURRENT_USER) refreshNav();
    }, 8000);
  }
  route();
}

// ------------------------------------------------------------------------------------------- boot
(async function boot() {
  try {
    CONFIG = await api("/api/config");
    $("#engine-badge").innerHTML = `<span class="engine-dot ${CONFIG.ai_enabled ? "" : "local"}"></span>Extraction engine<strong>${esc(CONFIG.engine)}</strong>`;
  } catch (e) {
    console.error("Config load error:", e);
  }

  const token = getToken();
  if (token) {
    try {
      const res = await api("/api/auth/me");
      CURRENT_USER = res.user;
    } catch (e) {
      setToken(null);
      CURRENT_USER = null;
    }
  }

  if (CURRENT_USER) {
    showAppShell();
  } else {
    showAuthScreen("login");
  }
})();
