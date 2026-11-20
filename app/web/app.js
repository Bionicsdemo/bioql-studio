// BioQL Studio's window: Agtr's workbench — title bar with the command centre, the activity sidebar, a tab per
// section, the status bar — filled with BioQL's own menu, whole.
//
// Where each section's content comes from:
//   · BioQL's routable pages (Lab Notebook, Screening, SAR, Report Builder, Drug Candidates, Viz, API keys) open as
//     BioQL's REAL page, in the frame, untouched. Studio never reaches inside: it is another origin.
//   · BioQL's activity-bar panels (Quantum, Molecular, Pipelines, Undruggable, TargetRadar, Results, MCP Tools)
//     open over the SAME engine routes, through Studio's closed allowlist, with Claude Code working them and every
//     call on the signed record.
//   · Studio's own: Findings, Whiteboard, Notebook, Guard, Server health, Usage, Settings.
//
// The token arrives in the URL FRAGMENT and is cleared from the address bar at once. A fragment is never sent to a
// server, never written to a server log and never put in a Referer.
"use strict";

const TOKEN = (() => {
  // The token arrives in the fragment and is cleared from the address bar at once — a fragment is never sent to a
  // server, never logged and never put in a Referer. But clearing it also meant a RELOAD had no token left, so
  // every call answered 403 and the window went dead with the word "forbidden" and no way back. It is kept for
  // THIS TAB only, in sessionStorage: it dies with the tab, never reaches another tab, another window, disk, or
  // any server.
  const m = /(?:^|[#&])t=([A-Za-z0-9_\-]+)/.exec(location.hash || "");
  if (m) {
    try { sessionStorage.setItem("studio.t", m[1]); } catch (e) { /* private window: this tab works, a reload asks again */ }
    try { history.replaceState(null, "", location.pathname); } catch (e) { location.hash = ""; }
    return m[1];
  }
  try { return sessionStorage.getItem("studio.t") || ""; } catch (e) { return ""; }
})();

function noToken() {
  // A window with no token can do NOTHING, so it says so in the one place that is always visible, with the command
  // that prints a working URL. It used to show "forbidden" in a status line and nothing else.
  const hero = $("hero");
  if (hero && !$("no-token")) {
    const box = el("div", "empty");
    box.id = "no-token";
    box.appendChild(el("p", "waiting-why", "This window has no token, so it cannot ask Studio anything."));
    box.appendChild(el("p", "fine", "Open it with the URL this prints, and keep that tab:"));
    box.appendChild(el("p", "cmd", "python3 -m app token"));
    hero.appendChild(box);
  }
  ring("down");
}

const $ = (id) => document.getElementById(id);
const txt = (s) => document.createTextNode(s === undefined || s === null ? "" : String(s));

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.appendChild(txt(text));
  return n;
}

function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", cls || "ic");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function chip(word, kind) {
  return el("span", "chip " + (kind || String(word || "").toLowerCase()), word);
}

function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function empty(node, words) { clear(node); node.appendChild(el("p", "empty", words)); }

function toast(words) {
  const t = $("toast");
  t.textContent = words;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 5200);
}

async function api(path, body) {
  const opts = { method: body ? "POST" : "GET", headers: { "X-Studio-Token": TOKEN }, cache: "no-store" };
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let j = null;
  try { j = await r.json(); } catch (e) { j = { error: "the service answered something that is not JSON" }; }
  return { code: r.status, body: j };
}

// The ring. `alarm` outranks everything: a contradiction is the one thing that must not wait behind a spinner.
const RING = { idle: 0, busy: 1, waiting: 2, down: 3, alarm: 4 };
let ringNow = "idle";
function ring(state) {
  if (RING[state] === undefined) return;
  if (RING[state] >= RING[ringNow] || state === "idle") {
    ringNow = state;
    document.body.setAttribute("data-ring", state);
  }
}

// ------------------------------------------------------------------ the menu
// Studio's OWN menu. It is not BioQL's activity bar any more, and that is the change: this window used to mirror
// :3000's panels (Pipelines, Quantum, Molecular, Screening, Targets, TargetRadar, Undruggable, MCP Tools) and open
// :3000's own pages inside a frame. Both are gone.
//
// Why they are gone, written here so nobody puts them back by accident:
//   1. The panels were LISTS OF ROUTES, not work. A person looking at 502 route names cannot see a molecule, a job
//      or a result; the chat already picks the route, from the same closed list, with the record behind it.
//   2. The framed pages were :3000's, which means another origin inside this window. The owner asked for an app
//      that does not reach :3000 at all, and that is now true: no iframe, no `frame-src`, nothing to reload.
//
// What is left is the work itself, each section built from BioQL's OWN history routes (GET, `status` kind, nothing
// that spends compute): what has run, what molecules were studied, what came back, and the record underneath.
const SECTIONS = {
  // --- the instrument
  chat:     { group: "Iris", label: "Chat", icon: "orb", kind: "chat" },
  chats:    { group: "Iris", label: "Chats", icon: "chats", kind: "chats",
              lead: "Every turn of this window, with the tools each one ran." },

  // --- the work
  jobs:     { group: "Work", label: "Jobs & Recovery", icon: "pulse", kind: "jobs",
              lead: "Every run BioQL has a record of — its own job list and its own recovery sessions, newest " +
                    "first. A run whose result is still there can be recovered here, and what comes back is drawn " +
                    "as cards, not as a receipt." },
  results:  { group: "Work", label: "Results", icon: "table", kind: "mols",
              lead: "The molecules and targets that have been studied, one row each, with the pipeline that " +
                    "studied them and whether the result is still recoverable." },
  sar:      { group: "Work", label: "SAR", icon: "tube", kind: "sar",
              lead: "Matched pairs, R-groups and activity cliffs over the molecules in those results. BioQL never " +
                    "declared these routes' parameters, so what it refuses is shown in its own words." },
  cands:    { group: "Work", label: "Drug Candidates", icon: "flask", kind: "cands",
              lead: "Every candidate BioQL named in a recovered result, with the numbers it stated beside it. " +
                    "Nothing here is scored by Studio." },
  report:   { group: "Work", label: "Report Builder", icon: "doc", kind: "report",
              lead: "A report over the runs you pick, built by BioQL's own report routes. The sections are " +
                    "BioQL's; Studio chooses nothing about the science." },

  // --- the record
  findings: { group: "Record", label: "Findings", icon: "check", kind: "findings",
              lead: "Every verdict the verifier recorded, newest first. A row whose line recorded no verdict says " +
                    "not recorded — it is never read as clean." },
  board:    { group: "Record", label: "Whiteboard", icon: "board", kind: "board",
              lead: "The plan drawn before anything is spent, and the evidence hung on it afterwards." },
  ledger:   { group: "Record", label: "Notebook", icon: "book", kind: "ledger",
              lead: "The hash-chained, Ed25519-signed record: one line per call to BioQL." },
  guard:    { group: "Record", label: "Guard", icon: "shield", kind: "guard",
              lead: "Has BioQL changed since Studio opened? The app fingerprints BioQL's source trees when it " +
                    "starts; this compares that baseline against a reading you take here. Opening the page takes " +
                    "no reading of its own, and the guard never says WHO changed a file — only that it changed." },
  servers:  { group: "Record", label: "Server Health", icon: "server", kind: "servers",
              lead: "BioQL's own health routes, asked now." },
  usage:    { group: "Record", label: "Usage", icon: "pulse", kind: "usage",
              lead: "Counted off the signed record, not off a tally kept beside it." },
  keys:     { group: "Record", label: "API Keys", icon: "key", kind: "keys",
              lead: "Who holds which secret, and where. Studio cannot read BioQL's keys and does not try: every " +
                    "route whose name contains auth, keys, secrets or token is on the deny list." },
  settings: { group: "Record", label: "Settings", icon: "gear", kind: "settings" },
};

const GROUPS = ["Iris", "Work", "Record"];

// ------------------------------------------------------------------ the state line and the status bar
// Studio keeps no address for BioQL's own window or for TargetRadar. It used to hold both, to put their pages in
// a frame; nothing frames them now, and an address kept for nothing is an invitation to reach for it again.
let lastState = null;

// Las etapas de los pipelines en curso. Lee `/api/progress`, que el adaptador llena desde
// `_follow`; esta pagina NUNCA pregunta a BioQL (`connect-src 'self'`) ni toca una corrida.
// Dibuja lo mismo que `PipelineProgressTracker.tsx` en :3000: barra hecho/total, la lista de
// etapas, y la que corre girando. Lo que BioQL no diga queda sin dibujar, no en cero: una barra
// al 0% y una barra sin dato se leen igual y no son lo mismo.
async function drawProgress() {
  const box = $("prog");
  if (!box) return;
  const { code, body } = await api("/api/progress");
  const runs = code === 200 && body && typeof body === "object" ? Object.entries(body) : [];
  if (!runs.length) { box.hidden = true; clear(box); return; }
  clear(box);
  box.hidden = false;
  runs.forEach(([jid, r]) => {
    const card = el("div", "prog-card");
    const head = el("div", "prog-head");
    head.appendChild(icon("flow", "ic"));
    head.appendChild(el("b", null, String(r.pipeline_type || "pipeline").replace(/_/g, " ")));
    if (r.target) head.appendChild(el("span", "sub", String(typeof r.target === "object" ? (r.target.name || "?") : r.target)));
    head.appendChild(el("span", "prog-id", jid));
    card.appendChild(head);

    if (r.stages_unavailable) {
      // La corrida sigue viva; lo que no se pudo localizar son sus etapas. Decirlo es el dato.
      card.appendChild(el("p", "prog-none", "running — sin etapas localizables: " + r.stages_unavailable));
      box.appendChild(card);
      return;
    }

    const done = Number(r.completed_stages), total = Number(r.total_stages);
    if (Number.isFinite(done) && Number.isFinite(total) && total > 0) {
      const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
      const bar = el("div", "prog-bar");
      const fill = el("div", "prog-fill");
      fill.style.width = pct + "%";
      bar.appendChild(fill);
      card.appendChild(bar);
      card.appendChild(el("div", "prog-count", `${done}/${total} etapas (${pct}%)`));
    }

    (r.stages || []).forEach((s) => {
      const row = el("div", "prog-row");
      const st = String(s.status || "");
      row.appendChild(icon(st === "completed" ? "check" : (st === "running" ? "pulse" : "orb"),
                           "ic " + (st === "completed" ? "ok" : (st === "running" ? "run" : "wait"))));
      row.appendChild(el("span", null, String(s.stage || "")));
      if (st === "running") row.appendChild(el("span", "prog-tag run", "corriendo"));
      else if (st === "completed") row.appendChild(el("span", "prog-tag ok", "hecho"));
      card.appendChild(row);
    });

    if (r.current_stage && r.status === "running") {
      card.appendChild(el("div", "prog-now", "ahora: " + String(r.current_stage)));
    }
    if (r.error) card.appendChild(el("p", "prog-none", String(r.error)));
    // Como se supo que estas etapas son de ESTE trabajo. Los dos ids no estan conectados en
    // BioQL, asi que el emparejamiento se ensena en vez de darse por hecho.
    if (r.matched_by) card.appendChild(el("div", "prog-why", "emparejado por " + String(r.matched_by)));
    box.appendChild(card);
  });
}

async function drawState() {
  const { code, body } = await api("/api/state");
  if (code === 403) {
    $("hubLine").textContent = "no token";
    $("hubDot").className = "dot bad";
    $("stEngine").querySelector("span").textContent =
      "no token — open this window with the URL `python3 -m app token` prints";
    noToken();
    return null;
  }
  if (code !== 200) {
    $("hubLine").textContent = "the service did not answer";
    $("hubDot").className = "dot bad";
    ring("down");
    return null;
  }
  lastState = body;
  const waiting = (body.pending || []).length;
  $("hubDot").className = "dot " + (waiting ? "warn" : "on");
  $("hubLine").textContent = waiting ? waiting + " waiting for a person" : "ready";
  $("ver").textContent = body.version + " · 127.0.0.1:" + body.port;
  $("stVerText").textContent = body.version;
  $("chatSub").textContent = `${body.calls} call(s) · ${body.refusals} refused · ${body.approvals} approved`;
  $("stateLine").textContent = `${body.calls} call(s) on the record · ${body.refusals} refused · ` +
    `${body.approvals} approved` + (waiting ? ` · ${waiting} waiting for a person` : "");
  drawPending(body.pending || []);
  if (waiting) ring("waiting");
  return body;
}

async function drawEngine() {
  const { code, body } = await api("/api/call", { name: "backend_health", args: {} });
  const span = $("stEngine").querySelector("span");
  if (code === 403 || body.state !== "RESOLVED") {
    span.textContent = (body.state || "no answer") + " · " + (body.detail || body.error || "the engine did not answer");
    $("stEngine").className = "st bad";
    $("chatDot").className = "dot bad";
    ring("down");
    return;
  }
  const v = body.value || {};
  span.textContent = (v.service || "BioQL") + " · " + (v.status || "answered");
  $("stEngine").className = "st" + (v.status === "healthy" ? "" : " warn");
  $("chatDot").className = "dot " + (v.status === "healthy" ? "on" : "warn");
  $("chatName").textContent = v.service || "BioQL Studio";
}

// ------------------------------------------------------------------ waiting for a person
function drawPending(pending) {
  const modal = $("modal"), sheet = $("sheet");
  if (!pending.length) {
    if (modal.dataset.why === "pending") modal.hidden = true;
    return;
  }
  if (!modal.hidden && modal.dataset.why !== "pending") return;
  modal.dataset.why = "pending";
  modal.hidden = false;
  clear(sheet);
  sheet.appendChild(el("h2", null, "Waiting for a person"));
  sheet.appendChild(el("p", null, "The code for a pending call is in Studio's log and in HOME/pending/<id>. It was " +
    "never sent to this page. Pasting it is an explicit, single-use, call-bound act — not a proof that a human is " +
    "present."));
  pending.forEach((p) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", p.tool));
    head.appendChild(el("span", "hash", p.detail || ""));
    row.appendChild(head);
    const act = el("div", "row-head");
    const input = el("input", "field-in");
    input.placeholder = "code from the log";
    input.setAttribute("aria-label", "approval code");
    const go = el("button", "go", "Approve");
    go.addEventListener("click", async () => {
      go.disabled = true;
      ring("busy");
      const { body } = await api("/api/approve", { id: p.id, code: input.value.trim() });
      go.disabled = false;
      ring("idle");
      toast(body.detail || body.error || body.state || "sent");
      await drawState();
      if (view === "findings") await draw();
    });
    act.appendChild(input);
    act.appendChild(go);
    row.appendChild(act);
    sheet.appendChild(row);
  });
  const acts = el("div", "acts");
  const shut = el("button", "btn", "Later");
  shut.addEventListener("click", () => { modal.hidden = true; });
  acts.appendChild(shut);
  sheet.appendChild(acts);
}

// ------------------------------------------------------------------ the sections
let TOOLS = [];

async function loadTools() {
  const { code, body } = await api("/api/tools");
  if (code === 200) TOOLS = body.tools || [];
  const n = TOOLS.length;
  const approval = TOOLS.filter((t) => t.approval).length;
  $("stTools").querySelector("span").textContent = n ? `${n} tools · ${approval} ask first` : "";
  const em = document.querySelector('nav button[data-v=tools] em');
  if (em) em.textContent = n ? String(n) : "";
  return TOOLS;
}

async function drawFindings(box) {
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).filter((r) => r.verdict || (r.findings || []).length).reverse();
  const flagged = rows.filter((r) => r.verdict && r.verdict !== "clean");
  const bad = flagged.filter((r) => r.verdict === "contradicted").length;

  // The badge counts what NEEDS ATTENTION, not how much work was checked. It used to show the total — "92" beside
  // Findings, where 86 of those rows said "nothing contradicted" — and a number in a badge is read as a number of
  // problems. A clean verdict is good news and good news does not belong in a badge.
  const em = document.querySelector('nav button[data-v=findings] em');
  if (em) { em.textContent = flagged.length ? String(flagged.length) : ""; em.className = bad ? "hot" : ""; }
  $("stFind").querySelector("span").textContent = rows.length
    ? (flagged.length ? `${flagged.length} of ${rows.length} need a look` : `${rows.length} checked · all clean`)
    : "nothing verified yet";
  $("stFind").className = "st" + (bad ? " bad" : "");
  if (bad) ring("alarm");
  if (!rows.length) return empty(box, "nothing verified yet. A verdict is recorded on every call that resolves.");
  clear(box);
  box.appendChild(el("p", "fine", flagged.length
    ? `${flagged.length} verdict(s) need a look, shown first; ${rows.length - flagged.length} were clean.`
    : `${rows.length} verdict(s), all clean. A clean verdict means nothing in the answer contradicted itself — ` +
      `it is not a statement that the science is right.`));
  // Flagged first. Scrolling past eighty-six clean rows to reach the one that is not is how a contradiction gets
  // missed, and a contradiction missed is the whole point of this page failing silently.
  const ordered = flagged.concat(rows.filter((r) => !(r.verdict && r.verdict !== "clean")));
  const g = el("div", "group");
  ordered.forEach((r) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", r.tool));
    head.appendChild(chip(r.verdict || "not recorded", r.verdict || "plain"));
    head.appendChild(chip(r.state, (r.state || "").toLowerCase()));
    row.appendChild(head);
    row.appendChild(el("span", "row-when", when(r.t)));
    (r.findings || []).forEach((f) => {
      const fd = el("div", "finding");
      fd.appendChild(el("span", "finding-code", f.code));
      fd.appendChild(el("p", null, f.detail || ""));
      if (f.path) fd.appendChild(el("p", "where", f.path));
      row.appendChild(fd);
    });
    if (!(r.findings || []).length) {
      row.appendChild(el("p", "row-why", r.verdict === "clean" ? "nothing contradicted" : (r.detail || "")));
    }
    g.appendChild(row);
  });
  box.appendChild(g);
}

// ---- the work pages -------------------------------------------------------
// Every one of these reads BioQL's OWN history: `/api/pipeline/jobs`, `/api/recovery/sessions` and
// `/api/recovery/session/<id>/result` — GET, `status` kind in the allowlist, nothing that spends compute and
// nothing that touches a run. They are the same routes BioQL's own window uses, so a run started anywhere (this
// window, its window, the command line) appears in all of them.
//
// The three states are kept apart on every one of these pages: RESOLVED is drawn, ABSENT says BioQL answered and
// has nothing, and UNRESOLVABLE says the question could not be asked and why. A page that drew an empty table for
// all three would turn "I could not ask" into "there is nothing", which is the one substitution this whole
// program exists to refuse.

async function callTool(name, args) {
  const { code, body } = await api("/api/call", { name, args: args || {} });
  return { code, body: body || {} };
}

// What a call's answer was, as a chip, in BioQL's own words underneath.
function stateLine(box, body, what) {
  const st = String(body.state || "");
  if (st === "RESOLVED") return true;
  const row = el("div", "row");
  const head = el("div", "row-head");
  head.appendChild(el("span", "row-tool", what));
  head.appendChild(chip(st || "no answer", st === "ABSENT" ? "absent" : "bad"));
  row.appendChild(head);
  row.appendChild(el("p", "row-why", body.detail || body.error ||
    (st === "ABSENT" ? "BioQL answered and has nothing here." : "the call did not go through")));
  if (body.pending) {
    row.appendChild(el("p", "fine", "This one needs a person: the code is in Studio's log and in " +
      "HOME/pending/" + body.pending.id + "."));
  }
  box.appendChild(row);
  return false;
}

function tableNode(cols, rows) {
  const t = el("table", "wtab");
  const thead = el("thead");
  const hr = el("tr");
  cols.forEach((c) => hr.appendChild(el("th", null, c)));
  thead.appendChild(hr);
  t.appendChild(thead);
  const tb = el("tbody");
  rows.forEach((cells) => {
    const tr = el("tr");
    cells.forEach((c) => {
      const td = el("td");
      if (c instanceof Node) td.appendChild(c);
      else td.appendChild(txt(c === null || c === undefined ? "" : c));
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  return t;
}

function whenISO(s) {
  if (!s) return "";
  // BioQL writes a NAIVE timestamp — "2026-11-20T08:56:45.649433", no zone — and it means local time on this Mac.
  // This used to append a Z, which read every one of them as UTC: the peptide run started at 08:56 was drawn at
  // 03:56, five hours earlier than it happened. A time that is wrong by the machine's offset is worse than no
  // time, because it looks right. A string that DOES carry a zone is left alone and honoured.
  const raw = String(s);
  const zoned = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(raw);
  const d = new Date(zoned ? raw : raw.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? raw.slice(0, 19).replace("T", " ") : when(d.getTime() / 1000);
}

// A recovered result, drawn as cards. The cards are built in Python by `studio/cards.py` — the same machinery the
// chat uses, so a result opened here and the same result in the chat are the same cards, section for section.
function openResult(host, sid) {
  clear(host);
  host.appendChild(el("p", "fine", "recovering " + sid + "…"));
  return callTool("recovery_session_id_result", { id: sid }).then(({ body }) => {
    clear(host);
    if (!stateLine(host, body, sid)) return;
    if (body.card) {
      host.appendChild(cardNode(body.card));
    } else {
      // The payload came back and the card did not. Showing the keys is not the result, and it does not pretend
      // to be: it says plainly that the card could not be built.
      host.appendChild(el("p", "row-why", "the result arrived but no card could be built from it"));
      const v = body.value;
      host.appendChild(el("pre", "sdf", JSON.stringify(v, null, 1).slice(0, 4000)));
    }
  });
}

// ---- Jobs & Recovery ------------------------------------------------------
async function drawJobs(box) {
  clear(box);
  const [jobs, sess, ibm] = await Promise.all([
    callTool("pipeline_jobs", {}),
    callTool("recovery_sessions", {}),
    callTool("recovery_ibm_status", {}),
  ]);

  // IBM first, because it is the one line that can cost money and the one most often wrong. Its own error is
  // shown; "no counts" is never drawn as "no jobs".
  const ibmBody = ibm.body || {};
  const ibmRow = el("p", "fine");
  if (String(ibmBody.state) === "RESOLVED") {
    const v = ibmBody.value || {};
    const counts = v.counts && Object.keys(v.counts).length
      ? Object.entries(v.counts).map(([k, n]) => k + " " + n).join(" · ") : null;
    ibmRow.textContent = "IBM Quantum: " + (counts || (v.error ? "could not be read — " + v.error
      : "BioQL reported no job counts"));
  } else {
    ibmRow.textContent = "IBM Quantum: " + (ibmBody.detail || "could not be asked");
  }
  box.appendChild(ibmRow);

  box.appendChild(el("h3", "sect", "BioQL's job list"));
  if (stateLine(box, jobs.body, "pipeline_jobs")) {
    const list = ((jobs.body.value || {}).jobs) || [];
    if (!list.length) box.appendChild(el("p", "empty", "BioQL's job list is empty."));
    else {
      box.appendChild(tableNode(["job", "target", "status", "started"],
        list.map((j) => [el("code", null, String(j.job_id || "")), String(j.target || "—"),
                         chip(String(j.status || "?"), jobStatus(j.status)), whenISO(j.created_at)])));
    }
  }

  box.appendChild(el("h3", "sect", "Recovery sessions"));
  if (!stateLine(box, sess.body, "recovery_sessions")) return;
  const rows = ((sess.body.value || {}).sessions) || [];
  if (!rows.length) return void box.appendChild(el("p", "empty", "BioQL kept no recovery session."));
  const g = el("div", "group");
  rows.forEach((s) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", String(s.pipeline || "pipeline")));
    head.appendChild(chip(String(s.status || "?"), jobStatus(s.status)));
    if (s.target) head.appendChild(el("span", "hash", String(s.target)));
    if (s.indication) head.appendChild(el("span", "hash", String(s.indication)));
    head.appendChild(el("span", "hash", String(s.session_id || "")));
    row.appendChild(head);
    row.appendChild(el("span", "row-when", whenISO(s.started_at || s.completed_at)
      + (s.wall_seconds !== undefined ? " · " + s.wall_seconds + "s" : "")));
    if (s.smiles) row.appendChild(el("code", "smi", String(s.smiles)));
    if (s.error) row.appendChild(el("p", "row-why", String(s.error)));
    const out = el("div", "wout");
    const acts = el("div", "row-head");
    if (s.has_result) {
      const b = el("button", "tool", "Recover the result");
      b.type = "button";
      b.addEventListener("click", () => { b.disabled = true; openResult(out, String(s.session_id)).then(() => { b.disabled = false; }); });
      acts.appendChild(b);
    } else {
      // Why it cannot be recovered, in BioQL's own words. An absent result is information; a greyed button with
      // no reason is not.
      acts.appendChild(el("span", "fine", s.status === "running"
        ? "still running — nothing to recover yet"
        : "BioQL says this session kept no result"));
    }
    row.appendChild(acts);
    row.appendChild(out);
    g.appendChild(row);
  });
  box.appendChild(g);
}

function jobStatus(s) {
  const w = String(s || "").toLowerCase();
  if (w === "completed" || w === "success") return "clean";
  if (w === "running" || w === "queued" || w === "pending") return "busy";
  if (w === "interrupted" || w === "failed" || w === "error") return "bad";
  return "plain";
}

// ---- Results: the molecules and targets that were studied -----------------
async function drawMols(box) {
  clear(box);
  const { body } = await callTool("recovery_sessions", {});
  if (!stateLine(box, body, "recovery_sessions")) return;
  const rows = ((body.value || {}).sessions) || [];
  if (!rows.length) return void box.appendChild(el("p", "empty", "nothing has been studied yet."));

  // One row per thing studied, newest run first. A session with a SMILES is a molecule; one without is the target
  // it was run against — they are not merged, because "PD-L1" and a SMILES are not the same kind of subject.
  const bySubject = new Map();
  rows.forEach((s) => {
    const key = (s.smiles && String(s.smiles)) || ("target:" + String(s.target || "?"));
    const seen = bySubject.get(key) || { smiles: s.smiles || "", target: s.target || "", runs: [] };
    if (!seen.target && s.target) seen.target = s.target;
    seen.runs.push(s);
    bySubject.set(key, seen);
  });

  box.appendChild(el("p", "fine", bySubject.size + " subject(s) · " + rows.length + " run(s) · "
    + rows.filter((s) => s.has_result).length + " with a result still kept"));

  const g = el("div", "group");
  [...bySubject.values()].forEach((m) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", m.smiles ? "molecule" : "target"));
    if (m.target) head.appendChild(el("b", null, String(m.target)));
    head.appendChild(el("span", "hash", m.runs.length + " run(s)"));
    row.appendChild(head);
    if (m.smiles) row.appendChild(el("code", "smi", m.smiles));

    const pics = el("div", "wpics");
    row.appendChild(pics);
    if (m.smiles) {
      const d2 = el("button", "tool", "2D");
      d2.type = "button";
      d2.title = "BioQL's own depiction (RDKit), through /api/render/2d";
      d2.addEventListener("click", () => { d2.disabled = true; draw2D(pics, m.smiles).then(() => { d2.disabled = false; }); });
      head.appendChild(d2);
      const d3 = el("button", "tool", "3D");
      d3.type = "button";
      d3.title = "BioQL embeds the molecule in 3D (/api/smiles-to-3d) and it is turned here";
      d3.addEventListener("click", () => { d3.disabled = true; draw3D(pics, m.smiles).then(() => { d3.disabled = false; }); });
      head.appendChild(d3);
    }

    const out = el("div", "wout");
    m.runs.forEach((s) => {
      const r = el("div", "wrun");
      r.appendChild(chip(String(s.status || "?"), jobStatus(s.status)));
      r.appendChild(el("span", null, String(s.pipeline || "pipeline")));
      if (s.indication) r.appendChild(el("span", "hash", String(s.indication)));
      r.appendChild(el("span", "row-when", whenISO(s.started_at || s.completed_at)));
      if (s.has_result) {
        const b = el("button", "tool", "Open");
        b.type = "button";
        b.addEventListener("click", () => { b.disabled = true; openResult(out, String(s.session_id)).then(() => { b.disabled = false; }); });
        r.appendChild(b);
      } else {
        r.appendChild(el("span", "fine", s.error ? String(s.error) : "no result kept"));
      }
      out.appendChild(r);
    });
    row.appendChild(out);
    g.appendChild(row);
  });
  box.appendChild(g);
}

async function draw2D(host, smiles) {
  const old = host.querySelector(".twod");
  if (old) old.remove();
  const { body } = await callTool("render_2d", { smiles: smiles });
  const wrap = el("figure", "twod");
  if (String(body.state) !== "RESOLVED") {
    wrap.appendChild(el("figcaption", "fine", "2D: " + (body.detail || "no answer")));
    host.appendChild(wrap);
    return;
  }
  const v = body.value || {};
  const svg = v.svg || v.image || v.data;
  if (typeof svg !== "string" || !svg.includes("<svg")) {
    wrap.appendChild(el("figcaption", "fine", "BioQL answered without an SVG for this molecule"));
    host.appendChild(wrap);
    return;
  }
  // A data: URL on an <img>, never innerHTML: the SVG stays a picture and can run nothing. `img-src 'self' data:`
  // is what allows exactly this and nothing else.
  const img = el("img", "twod-img");
  img.alt = "2D depiction of " + smiles;
  img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(svg)));
  wrap.appendChild(img);
  wrap.appendChild(el("figcaption", "fine", "RDKit, through BioQL · " + (v.smiles || smiles)));
  host.appendChild(wrap);
}

async function draw3D(host, smiles) {
  const old = host.querySelector(".mol3d");
  if (old) old.remove();
  const { body } = await callTool("smiles_to_3d", { smiles: smiles });
  if (String(body.state) !== "RESOLVED") {
    host.appendChild(el("p", "fine", "3D: " + (body.detail || "no answer")));
    return;
  }
  const v = body.value || {};
  const mol = v.mol || v.sdf || v.molblock;
  if (typeof mol !== "string" || !mol.trim()) {
    host.appendChild(el("p", "fine", "BioQL answered without 3D coordinates for this molecule"));
    return;
  }
  host.appendChild(structureNode({
    parts: [{ role: "molecule", format: v.sdf ? "sdf" : "mol", data: mol, path: "smiles-to-3d", bytes: mol.length }],
  }));
}

// ---- SAR ------------------------------------------------------------------
// BioQL never declared these three routes' parameters (`required: []`, `optional: []` in the allowlist, which is
// its registry's own silence, not a guess of Studio's). So the call carries the set of molecules under the names
// BioQL's own window uses, and when BioQL refuses, its words are what the page shows — never a made-up shape.
const SAR_RUNS = [
  { name: "sar_mmp", label: "Matched pairs" },
  { name: "sar_rgroup", label: "R-groups" },
  { name: "sar_cliffs", label: "Activity cliffs" },
];

async function drawSar(box) {
  clear(box);
  const { body } = await callTool("recovery_sessions", {});
  if (!stateLine(box, body, "recovery_sessions")) return;
  const rows = ((body.value || {}).sessions) || [];
  const smiles = [...new Set(rows.map((s) => String(s.smiles || "").trim()).filter(Boolean))];
  if (!smiles.length) {
    return void box.appendChild(el("p", "empty",
      "no run in BioQL's history carries a SMILES, so there is no series to compare. SAR needs molecules; " +
      "a target name is not one."));
  }
  box.appendChild(el("p", "fine", smiles.length + " molecule(s) in BioQL's own history. Pick the series:"));
  const picked = new Set(smiles);
  const list = el("div", "wpick");
  smiles.forEach((s) => {
    const lab = el("label", "wcheck");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = true;
    cb.addEventListener("change", () => { if (cb.checked) picked.add(s); else picked.delete(s); });
    lab.appendChild(cb);
    lab.appendChild(el("code", "smi", s));
    list.appendChild(lab);
  });
  box.appendChild(list);

  const out = el("div", "wout");
  const acts = el("div", "row-head");
  SAR_RUNS.forEach((r) => {
    const b = el("button", "tool", r.label);
    b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      clear(out);
      out.appendChild(el("p", "fine", r.label + "…"));
      const got = await callTool(r.name, { smiles: [...picked] });
      clear(out);
      if (stateLine(out, got.body, r.name)) {
        if (got.body.card) out.appendChild(cardNode(got.body.card));
        else out.appendChild(el("pre", "sdf", JSON.stringify(got.body.value, null, 1).slice(0, 4000)));
      }
      b.disabled = false;
    });
    acts.appendChild(b);
  });
  box.appendChild(acts);
  box.appendChild(out);
}

// ---- Drug Candidates ------------------------------------------------------
// A candidate is whatever BioQL itself called one: an object carrying a SMILES inside a recovered result. They are
// found by walking the payload, and the PATH each one was found at is shown, so a number on this page can always
// be traced back to the key BioQL wrote it under. Studio scores nothing and ranks nothing.
function candidatesIn(o, path, out, depth) {
  if (depth > 6 || out.length > 400) return out;
  if (Array.isArray(o)) {
    o.forEach((v, i) => candidatesIn(v, path + "[" + i + "]", out, depth + 1));
    return out;
  }
  if (o && typeof o === "object") {
    const smi = o.smiles || o.SMILES || o.smile;
    if (typeof smi === "string" && smi.trim() && smi.length < 400) {
      const nums = {};
      Object.keys(o).forEach((k) => {
        const v = o[k];
        if (typeof v === "number" && Number.isFinite(v)) nums[k] = v;
        else if (typeof v === "string" && v.length < 40 && /score|rank|name|id|class|verdict|flag/i.test(k)) nums[k] = v;
      });
      out.push({ smiles: smi.trim(), path: path, fields: nums });
    }
    Object.keys(o).forEach((k) => candidatesIn(o[k], path + "." + k, out, depth + 1));
  }
  return out;
}

async function drawCands(box) {
  clear(box);
  const { body } = await callTool("recovery_sessions", {});
  if (!stateLine(box, body, "recovery_sessions")) return;
  const rows = (((body.value || {}).sessions) || []).filter((s) => s.has_result);
  if (!rows.length) {
    return void box.appendChild(el("p", "empty",
      "no run has a result still kept, so there is nothing to read candidates out of."));
  }
  box.appendChild(el("p", "fine", rows.length + " run(s) with a result kept. Reading one recovers it from BioQL " +
    "and takes nothing from its engine."));
  const g = el("div", "group");
  rows.forEach((s) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", String(s.pipeline || "pipeline")));
    if (s.target) head.appendChild(el("b", null, String(s.target)));
    head.appendChild(el("span", "hash", String(s.session_id)));
    const out = el("div", "wout");
    const b = el("button", "tool", "Read its candidates");
    b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      clear(out);
      out.appendChild(el("p", "fine", "recovering…"));
      const got = await callTool("recovery_session_id_result", { id: String(s.session_id) });
      clear(out);
      b.disabled = false;
      if (!stateLine(out, got.body, String(s.session_id))) return;
      const found = candidatesIn((got.body.value || {}).result || got.body.value, "$", [], 0);
      if (!found.length) {
        out.appendChild(el("p", "row-why", "this result names no object with a SMILES in it. That is BioQL's " +
          "answer about this run, not an empty table."));
        return;
      }
      const cols = ["molecule", "where BioQL put it", "what BioQL stated"];
      out.appendChild(tableNode(cols, found.slice(0, 120).map((c) => [
        el("code", "smi", c.smiles),
        el("span", "hash", c.path),
        Object.entries(c.fields).slice(0, 8).map(([k, v]) => k + " " + v).join(" · "),
      ])));
      if (found.length > 120) out.appendChild(el("p", "fine", "120 of " + found.length + " shown"));
    });
    head.appendChild(b);
    row.appendChild(head);
    row.appendChild(el("span", "row-when", whenISO(s.completed_at || s.started_at)));
    row.appendChild(out);
    g.appendChild(row);
  });
  box.appendChild(g);
}

// ---- Report Builder -------------------------------------------------------
// The report is BioQL's. These routes' parameters were never declared either, so the session ids go in under the
// names its own window uses and a refusal is shown verbatim. Nothing here writes a report of Studio's own: a
// document assembled by this window would carry no provenance at all.
const REPORT_RUNS = [
  { name: "reports_generate", label: "Report" },
  { name: "reports_generate_dossier", label: "Dossier" },
  { name: "api_reports_html", label: "HTML" },
  { name: "report_export", label: "Export" },
];

async function drawReport(box) {
  clear(box);
  const { body } = await callTool("recovery_sessions", {});
  if (!stateLine(box, body, "recovery_sessions")) return;
  const rows = (((body.value || {}).sessions) || []).filter((s) => s.has_result);
  if (!rows.length) {
    return void box.appendChild(el("p", "empty", "a report is built over runs that still have a result; " +
      "BioQL has none kept."));
  }
  const picked = new Set();
  const list = el("div", "wpick");
  rows.forEach((s) => {
    const lab = el("label", "wcheck");
    const cb = el("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => { if (cb.checked) picked.add(String(s.session_id)); else picked.delete(String(s.session_id)); });
    lab.appendChild(cb);
    lab.appendChild(el("b", null, String(s.pipeline || "pipeline")));
    lab.appendChild(el("span", "hash", String(s.target || "")));
    lab.appendChild(el("span", "hash", String(s.session_id)));
    list.appendChild(lab);
  });
  box.appendChild(list);

  const out = el("div", "wout");
  const acts = el("div", "row-head");
  REPORT_RUNS.forEach((r) => {
    const b = el("button", "tool", r.label);
    b.type = "button";
    b.addEventListener("click", async () => {
      if (!picked.size) return void toast("pick at least one run");
      b.disabled = true;
      clear(out);
      out.appendChild(el("p", "fine", r.label + "…"));
      const ids = [...picked];
      const got = await callTool(r.name, { session_ids: ids, session_id: ids[0] });
      clear(out);
      b.disabled = false;
      if (!stateLine(out, got.body, r.name)) return;
      reportNode(out, got.body);
    });
    acts.appendChild(b);
  });
  box.appendChild(acts);
  box.appendChild(out);
}

function reportNode(host, body) {
  const v = body.value;
  // HTML that came back stays TEXT. Putting it in the page would run BioQL's markup inside Studio's window, which
  // is exactly the thing the policy here forbids; the text is the report and it can be read and copied.
  const html = v && typeof v === "object" ? (v.html || v.report_html) : null;
  if (typeof html === "string" && html.trim()) {
    host.appendChild(el("p", "fine", "BioQL returned " + html.length + " bytes of HTML. It is shown as text: " +
      "this window runs no markup it did not write."));
    host.appendChild(el("pre", "sdf", html.slice(0, 20000)));
    return;
  }
  if (body.card) return void host.appendChild(cardNode(body.card));
  host.appendChild(el("pre", "sdf", JSON.stringify(v, null, 1).slice(0, 20000)));
}

// ---- API Keys -------------------------------------------------------------
// There is no key route to show, and that is the page's first sentence rather than its omission: the allowlist's
// DENY pattern refuses every path whose name contains auth, admin, login, secrets, keys or token, so Studio cannot
// read BioQL's keys through it and no entry here can be added to let it.
async function drawKeys(box) {
  clear(box);
  const g = el("div", "group");
  const say = el("div", "row");
  say.appendChild(el("p", "row-why",
    "Studio holds no key of BioQL's and can read none: the closed list refuses, by name, every route containing " +
    "auth, admin, login, secrets, keys or token. What it does hold is its own two secrets, below — and it shows " +
    "where they are and how they are protected, never what is in them."));
  g.appendChild(say);
  box.appendChild(g);

  const { code, body } = await api("/api/secrets");
  if (code !== 200) return void box.appendChild(el("p", "row-why", (body && body.error) || "could not be read"));

  box.appendChild(el("h3", "sect", "What Studio holds"));
  box.appendChild(tableNode(["file", "what it is", "mode", "bytes", "last written"],
    (body.files || []).map((f) => [
      el("code", null, f.name),
      f.what,
      chip(f.mode || "?", f.mode === "0600" ? "clean" : "bad"),
      f.state === "RESOLVED" ? String(f.bytes) : "",
      f.state === "RESOLVED" ? when(f.mtime) : (f.detail || f.state),
    ])));
  box.appendChild(el("p", "fine", "In " + (body.home || "") +
    ". A mode other than 0600 is drawn in red: that is a file another account on this Mac could read."));

  // Whose keys BioQL itself says are working. This is BioQL's own health answer, not a key read.
  box.appendChild(el("h3", "sect", "What BioQL says about its own providers"));
  const h = await callTool("backend_health", {});
  if (!stateLine(box, h.body, "backend_health")) return;
  const v = h.body.value || {};
  const svc = v.services || v.providers || v;
  const rows = Object.entries(svc).filter(([, x]) => typeof x !== "object" || x === null)
    .map(([k, x]) => [k, String(x)]);
  if (!rows.length) {
    box.appendChild(el("p", "row-why", "BioQL's health answer names no provider. That is its answer, not a blank."));
    return;
  }
  box.appendChild(tableNode(["what BioQL named", "what it said"], rows));
}

const LANES = ["target", "mechanism", "candidate", "risk", "plan", "evidence"];
let boardId = null;

async function drawBoard(box) {
  const { code, body } = await api("/api/boards");
  if (code !== 200) return empty(box, body.error || "the boards did not answer");
  const list = body.boards || [];
  if (!list.length) {
    return empty(box, "no board yet. A board is the plan drawn before anything is spent, and every piece of " +
      "evidence on it names the line of the record it came from.");
  }
  if (!boardId || !list.some((b) => b.id === boardId)) boardId = list[0].id;
  clear(box);

  const bar = el("div", "board-bar");
  list.forEach((b) => {
    const pick = el("button", "board-pick", b.title || b.id);
    pick.setAttribute("aria-current", b.id === boardId ? "true" : "false");
    pick.addEventListener("click", () => { boardId = b.id; draw(); });
    bar.appendChild(pick);
  });
  box.appendChild(bar);

  const one = await api("/api/board?id=" + encodeURIComponent(boardId));
  if (one.code !== 200) {
    const why = one.body.detail || one.body.error || "";
    box.appendChild(el("p", "empty", (one.body.state === "UNRESOLVABLE"
      ? "this board cannot be read, so it is not drawn: " : "no such board: ") + why));
    return;
  }
  const b = one.body.board, rep = one.body.report || {};
  if (b.question) box.appendChild(el("p", "fine", b.question));
  if (rep.high_findings || (rep.contradicted || []).length) {
    box.appendChild(el("p", "alarm-strip",
      `${rep.high_findings} high finding(s) · ${(rep.contradicted || []).length} piece(s) of evidence the verifier ` +
      `calls contradicted. Read those before anything else on this board.`));
    ring("alarm");
  }
  if ((rep.unsupported || []).length) {
    box.appendChild(el("p", "alarm-strip", "contradicted by evidence: " +
      rep.unsupported.map((k) => (b.nodes[k] || {}).label).join(", ")));
  }
  const lanes = el("div", "lanes");
  LANES.forEach((kind) => {
    const mine = Object.values(b.nodes).filter((n) => n.kind === kind);
    if (!mine.length) return;
    const lane = el("div", "lane");
    lane.appendChild(el("h3", "lane-name", kind === "evidence" ? "evidence · from the record" : kind));
    mine.forEach((n) => {
      const card = el("div", "node " + kind + (n.verdict ? " " + n.verdict : ""));
      card.appendChild(el("div", "node-label", n.label));
      const foot = el("div", "node-foot");
      if (kind === "evidence") {
        foot.appendChild(chip(n.verdict || "not recorded", n.verdict || "plain"));
        foot.appendChild(chip(n.state, (n.state || "").toLowerCase()));
        if (n.is_real_hardware === true) foot.appendChild(chip("real hardware", "concerns"));
        (n.ibm_job_ids || []).forEach((j) => foot.appendChild(el("span", "hash", j)));
        foot.appendChild(el("span", "hash", String(n.ledger_hash || "").slice(0, 12)));
      } else {
        const s = (rep.support || {})[n.id];
        if (s) {
          foot.appendChild(el("span", "state-word " + s.state, s.state));
          foot.appendChild(el("span", "hash", s.why));
        }
      }
      card.appendChild(foot);
      lane.appendChild(card);
    });
    lanes.appendChild(lane);
  });
  box.appendChild(lanes);
  box.appendChild(el("p", "fine", "Every evidence card names the line of the hash-chained record it came from. " +
    "Only evidence the verifier called clean counts as support: a verdict never recorded is not a clean one."));
}

async function drawLedger(box) {
  const { code, body } = await api("/api/ledger?limit=200");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).slice().reverse();
  const chain = body.chain || {};
  $("stLedger").querySelector("span").textContent = chain.ok
    ? `chain intact · ${chain.lines} line(s)` : `chain broken: ${chain.why || "unknown"}`;
  $("stLedger").className = "st" + (chain.ok ? "" : " bad");
  clear(box);
  box.appendChild(el("p", chain.ok ? "fine" : "alarm-strip", chain.ok
    ? `chain intact over ${chain.lines} line(s)` : `the chain is broken: ${chain.why || "unknown"}`));
  if (!rows.length) { box.appendChild(el("p", "empty", "nothing recorded yet.")); return; }
  const g = el("div", "group");
  rows.forEach((r) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", r.tool));
    head.appendChild(chip(r.state, (r.state || "").toLowerCase()));
    if (r.verdict) head.appendChild(chip(r.verdict, r.verdict));
    if (r.by) head.appendChild(el("span", "hash", r.by));
    row.appendChild(head);
    row.appendChild(el("span", "row-when", when(r.t)));
    const why = el("p", "row-why");
    why.appendChild(txt(r.detail || ""));
    if (r.result_sha256) {
      why.appendChild(el("br"));
      why.appendChild(el("code", null, "sha256 " + String(r.result_sha256).slice(0, 24)));
    }
    row.appendChild(why);
    g.appendChild(row);
  });
  box.appendChild(g);
}

// The button that makes this page about NOW. A snapshot writes a file, so it is a POST and never something a
// page does by being opened: a guard that changed what it watches as you looked at it would be worth nothing.
function guardCheckButton(box) {
  const b = el("button", "tool", "Take a reading now");
  b.type = "button";
  b.title = "Fingerprint BioQL again and compare it against the baseline this app took when it opened";
  const note = el("p", "fine");
  b.addEventListener("click", async () => {
    b.disabled = true;
    const was = b.textContent;
    b.textContent = "reading ~47 000 files…";
    const { body } = await api("/api/guard/snapshot", { take: true });
    b.textContent = was;
    b.disabled = false;
    if (body && body.state === "RESOLVED") {
      note.textContent = `${body.files} file(s) in ${body.seconds}s`;
      await draw();
      return;
    }
    note.textContent = (body && body.detail) || "the snapshot did not happen";
  });
  box.appendChild(b);
  box.appendChild(note);
}

async function drawGuard(box) {
  const { code, body } = await api("/api/guard");
  if (code !== 200) return empty(box, body.error || "the guard did not answer");
  if (body.state && body.state !== "RESOLVED") {
    // ABSENT is not a fault and is not drawn as one: it means the baseline is there and nothing has been compared
    // against it yet. The nav badge and the status line stay quiet, because quiet is the honest reading.
    const waiting = body.state === "ABSENT";
    $("stGuard").querySelector("span").textContent = body.pending ? "taking the baseline…"
      : (waiting ? "baseline taken" : "guard unreadable");
    $("stGuard").className = "st" + (waiting ? "" : " warn");
    const em0 = document.querySelector('nav button[data-v=guard] em');
    if (em0) { em0.textContent = waiting ? "" : "!"; em0.className = waiting ? "" : "hot"; }
    clear(box);
    box.appendChild(el("p", waiting ? "fine" : "alarm-strip", body.detail || ""));
    if (body.pending) {
      // The baseline takes about ten seconds, and this page does not redraw itself on the tick (it would re-parse
      // two 8 MB snapshots every six seconds for nothing). So it asks again, ONLY while it is waiting, and only
      // while this is still the open section — a timer that outlives its page is a page that writes into another.
      clearTimeout(drawGuard._again);
      drawGuard._again = setTimeout(() => { if (view === "guard") draw(); }, 4000);
      return;
    }
    guardCheckButton(box);
    return;
  }
  clearTimeout(drawGuard._again);
  // `value`, which is what /api/guard sends. It used to read `body.report`, which that route has never set, so
  // `rep.roots` was undefined and the page drew the alarm line over an EMPTY list: a red strip saying BioQL had
  // changed, naming not one file. An alarm a person cannot act on is worse than no alarm.
  const rep = body.value || body.report || {};
  const roots = rep.roots || {};
  clear(box);

  // How old the two snapshots are. A comparison of yesterday's files is not a reading of this minute, and saying
  // so is the difference between "BioQL changed" and "BioQL had changed by 11:25 yesterday".
  guardCheckButton(box);
  const taken = body.taken;
  if (taken) {
    const age = Math.max(0, (Date.now() / 1000) - Number(taken.newer.t));
    const old_ = age < 90 ? "just now"
      : age < 5400 ? Math.round(age / 60) + " min ago"
      : age < 172800 ? Math.round(age / 3600) + " h ago"
      : Math.round(age / 86400) + " days ago";
    box.appendChild(el("p", "fine", `${taken.older.name} → ${taken.newer.name} · the newer one was taken ${old_}` +
      ". Opening this page takes no reading of its own — press the button above for one about this minute."));
  }

  // The alarm is for SOURCE. A finished pipeline writes its result and its report, and that used to turn this red
  // after every single run — a guard that is always red is a guard nobody reads. Outputs are still listed, still
  // counted, and still keep the report from being clean; they just do not raise the alarm.
  const srcClean = rep.source_clean !== false;
  const outs = Number(rep.outputs_written || 0);
  box.appendChild(el("p", srcClean ? "fine" : "alarm-strip", srcClean
    ? ("BioQL's own files are as they were between these two snapshots."
       + (outs ? ` ${outs} file(s) were WRITTEN by BioQL as it ran — listed below, which is not an edit.` : ""))
    : "BioQL's own files changed between these two snapshots. The guard names them; it never says who changed them."));

  const g = el("div", "group");
  Object.keys(roots).forEach((name) => {
    const r = roots[name];
    const src = r.source || { added: r.added || [], removed: r.removed || [], changed: r.changed || [] };
    const outList = r.outputs || [];
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", name));
    const dirty = (src.added || []).length + (src.removed || []).length + (src.changed || []).length;
    head.appendChild(chip(dirty ? "source changed" : "source unchanged", dirty ? "concerns" : "clean"));
    if (outList.length) head.appendChild(chip(outList.length + " written", "neutral"));
    head.appendChild(el("span", "hash", `${r.files} file(s) watched`));
    row.appendChild(head);
    if (!r.readable) {
      row.appendChild(el("p", "row-why", "part of this root could not be read, so nothing is claimed about it: "
        + (r.unreadable || []).slice(0, 3).join("; ")));
    }
    ["added", "removed", "changed"].forEach((k) => {
      const list = src[k] || [];
      if (!list.length) return;
      row.appendChild(el("p", "row-why", `${k}: ${list.slice(0, 8).join(", ")}` +
        (list.length > 8 ? ` … and ${list.length - 8} more` : "")));
    });
    if (outList.length) {
      row.appendChild(el("p", "fine", `written while running: ${outList.slice(0, 6).join(", ")}` +
        (outList.length > 6 ? ` … and ${outList.length - 6} more` : "")));
    }
    if (r.git && Object.keys(r.git).length) {
      row.appendChild(el("p", "row-why", "git: " + Object.keys(r.git).join(", ") + " moved"));
    }
    g.appendChild(row);
  });
  box.appendChild(g);

  $("stGuard").querySelector("span").textContent = srcClean ? "BioQL unchanged" : "BioQL changed";
  $("stGuard").className = "st" + (srcClean ? "" : " warn");
  const em = document.querySelector('nav button[data-v=guard] em');
  if (em) { em.textContent = srcClean ? "" : "!"; em.className = srcClean ? "" : "hot"; }
}

// BioQL's own health panel checks four servers (Sidebar.tsx::SERVERS): the backend, its liveness, the quantum
// engine and TargetRadar. Those are asked on opening; every other health route the engine declares is listed and
// asked only when you ask for it, because a window that fires fifty calls to draw itself is not a health panel.
const PRIMARY_HEALTH = ["backend_health", "system_live_status", "v1_quantum_health", "targetradar_health",
                        "protenix_health", "quantum_health"];

function healthRow(t) {
  const row = el("div", "row");
  const head = el("div", "row-head");
  head.appendChild(el("span", "row-tool", t.name));
  head.appendChild(el("span", "hash", t.path));
  const mark = chip("not asked", "plain");
  head.appendChild(mark);
  const said = el("p", "row-why");
  const go = el("button", "go", "Check");
  go.style.marginLeft = "auto";
  head.appendChild(go);
  row.appendChild(head);
  row.appendChild(said);
  row.ask = async () => {
    mark.className = "chip plain";
    mark.textContent = "asking…";
    const out = await api("/api/call", { name: t.name, args: {} });
    const b = out.body || {};
    // Three states, kept apart: it answered, it answered with nothing, or it could not be asked. The third is
    // never drawn as the second.
    mark.className = "chip " + String(b.state || "plain").toLowerCase();
    mark.textContent = b.state || "no answer";
    const v = b.value || {};
    clear(said);
    said.appendChild(txt(b.state === "RESOLVED"
      ? (v.status || v.service || JSON.stringify(v).slice(0, 220))
      : (b.detail || b.error || "")));
  };
  go.addEventListener("click", () => row.ask());
  return row;
}

async function drawServers(box) {
  // `area` is BioQL's own label on the entry, straight off its registry. It used to be read through a helper that
  // guessed an area from the path for every section of the old menu; the menu is gone and only this one reader is
  // left, so it reads the field itself and falls back to nothing rather than to a guess.
  const health = TOOLS.filter((t) => t.method === "GET" && !t.approval && !(t.required || []).length &&
                                     (/health|live_status|coherence/.test(t.path) ||
                                      ((t.from_registry || {}).area === "health")));
  if (!health.length) return empty(box, "no health route in the allowlist.");
  const seen = new Set();
  const uniq = health.filter((t) => (seen.has(t.path) ? false : (seen.add(t.path), true)));
  const first = uniq.filter((t) => PRIMARY_HEALTH.includes(t.name));
  const rest = uniq.filter((t) => !PRIMARY_HEALTH.includes(t.name));
  clear(box);

  const acts = el("div", "board-bar");
  const all = el("button", "board-pick", "Check every route (" + uniq.length + ")");
  acts.appendChild(all);
  box.appendChild(acts);

  box.appendChild(el("h3", "sect", "the servers BioQL's own panel watches"));
  const g1 = el("div", "group");
  const rows = [];
  first.forEach((t) => { const r = healthRow(t); rows.push(r); g1.appendChild(r); });
  box.appendChild(g1);

  box.appendChild(el("h3", "sect", `every other health route the engine declares (${rest.length})`));
  const g2 = el("div", "group");
  rest.forEach((t) => { const r = healthRow(t); rows.push(r); g2.appendChild(r); });
  box.appendChild(g2);

  all.addEventListener("click", async () => { all.disabled = true; for (const r of rows) await r.ask(); all.disabled = false; });
  // The four BioQL watches ask themselves once the page is on screen. Awaiting them here held the whole section
  // behind five network reads, and a server that times out held it for the timeout.
  setTimeout(async () => { for (const r of rows.slice(0, first.length)) await r.ask(); }, 0);
}

async function drawUsage(box) {
  const { code, body } = await api("/api/ledger?limit=500");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = body.rows || [];
  clear(box);
  const st = lastState || {};

  const tiles = el("div", "tiles");
  [["calls", st.calls], ["refused", st.refusals], ["approved", st.approvals],
   ["lines read", rows.length]].forEach(([k, v]) => {
    const t = el("div", "tile");
    t.appendChild(el("div", "tile-n", v === undefined || v === null ? "—" : String(v)));
    t.appendChild(el("div", "fact-k", k));
    tiles.appendChild(t);
  });
  box.appendChild(tiles);
  if (!rows.length) {
    box.appendChild(el("p", "empty", "nothing on the record yet."));
    return;
  }

  const byState = {}, byTool = {}, byVerdict = {}, byDay = {};
  rows.forEach((r) => {
    byState[r.state || "—"] = (byState[r.state || "—"] || 0) + 1;
    byTool[r.tool || "—"] = (byTool[r.tool || "—"] || 0) + 1;
    byVerdict[r.verdict || "not recorded"] = (byVerdict[r.verdict || "not recorded"] || 0) + 1;
    const d = r.t ? new Date(r.t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";
    byDay[d] = (byDay[d] || 0) + 1;
  });

  // A count beside a name is a list; a count against the largest is a reading. The bar is drawn to the biggest
  // value in ITS OWN table, and the number stays, because a bar without its number cannot be checked.
  const table = (title, obj, chips) => {
    const keys = Object.keys(obj).sort((a, b) => obj[b] - obj[a]).slice(0, 20);
    if (!keys.length) return;
    box.appendChild(el("h3", "sect", title));
    const top = Math.max(...keys.map((k) => obj[k])) || 1;
    const g = el("div", "group");
    keys.forEach((k) => {
      const row = el("div", "row meter");
      const head = el("div", "row-head");
      head.appendChild(chips ? chip(k, String(k).toLowerCase()) : el("span", "row-tool", k));
      const n = el("span", "count", String(obj[k]));
      head.appendChild(n);
      row.appendChild(head);
      const track = el("div", "track");
      const fill = el("div", "fill");
      fill.style.width = Math.max(2, Math.round((obj[k] / top) * 100)) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      g.appendChild(row);
    });
    box.appendChild(g);
  };
  table("by state", byState, true);
  table("by verdict", byVerdict, true);
  table("by day", byDay, false);
  table("by tool", byTool, false);
}


function drawSettings(box) {
  const st = lastState || {};
  clear(box);
  const g = el("div", "group");
  // Label left, value right, one line each — the same grouped list the rest of the window uses, instead of two
  // grey captions side by side with nothing telling them apart.
  [["version", st.version], ["service", "127.0.0.1:" + st.port], ["data", st.home],
   ["BioQL's engine", st.bioql], ["BioQL's window", "not reached from here"],
   ["tools in the allowlist", TOOLS.length ? String(TOOLS.length) : "—"],
   ["tools that ask a person", String(TOOLS.filter((t) => t.approval).length)],
   ["the brain", "Claude Code, and nothing else"]]
    .forEach(([k, v]) => {
      const row = el("div", "row pair");
      row.appendChild(el("span", "pair-k", k));
      row.appendChild(el("span", "pair-v", v === undefined || v === null ? "—" : String(v)));
      g.appendChild(row);
    });
  box.appendChild(g);
  box.appendChild(el("p", "fine", "BioQL's own model routes — vMLX, DeepSeek, openrouter, localllm — are not in " +
    "the allowlist, so nothing here can reach one."));
  box.appendChild(el("p", "fine", "Studio never writes inside BioQL's folders. The Guard section compares the two " +
    "most recent snapshots of its source trees and names every file that moved."));
}

function drawChats(box) {
  clear(box);
  if (!TURNS.length) return empty(box, "nothing asked in this window yet.");
  const g = el("div", "group");
  TURNS.slice().reverse().forEach((t) => {
    const row = el("div", "row");
    const b = el("button", "pick");
    b.appendChild(icon("chats"));
    const main = el("div", "pick-main");
    main.appendChild(el("b", null, t.text));
    main.appendChild(el("small", null, (t.tools.length ? t.tools.join(" · ") : "no tool called") + " · " + when(t.t)));
    b.appendChild(main);
    b.addEventListener("click", () => { open_("chat"); t.node.scrollIntoView({ block: "center" }); });
    row.appendChild(b);
    g.appendChild(row);
  });
  box.appendChild(g);
}

// ------------------------------------------------------------------ BioQL's own window
// ------------------------------------------------------------------ the structure, and the charts
//
// BioQL's own window draws a result's structure with 3Dmol.js (`3dmol` 2.5.5 in its package.json) and its charts
// with recharts. The same 3Dmol build travels inside this app, served from its own origin — `script-src 'self'`
// forbids a CDN, and an app whose whole design is 127.0.0.1 does not reach the internet to draw a molecule. There
// is no recharts here (it is React): the charts below are plain SVG, which is also what keeps them inside
// `style-src 'self'` — every attribute is a presentation attribute or a class, never an inline `style=`.

let molLoading = null;
let starLoading = null;

/** Mol*: el visor grande. 4,7 MB, asi que se trae solo cuando alguien lo pide — la ventana abre sin el. */
function loadMolstar() {
  if (window.molstar) return Promise.resolve(window.molstar);
  if (starLoading) return starLoading;
  starLoading = new Promise((ok, bad) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "/vendor/molstar.css";
    document.head.appendChild(css);
    const s = document.createElement("script");
    s.src = "/vendor/molstar.js";
    s.onload = () => (window.molstar ? ok(window.molstar) : bad(new Error("Mol* cargó y no se registró")));
    s.onerror = () => bad(new Error("no se pudo cargar /vendor/molstar.js"));
    document.head.appendChild(s);
  });
  return starLoading;
}

function load3Dmol() {
  if (window.$3Dmol) return Promise.resolve(window.$3Dmol);
  if (molLoading) return molLoading;
  molLoading = new Promise((ok, bad) => {
    const s = document.createElement("script");
    s.src = "/vendor/3Dmol-min.js";
    s.onload = () => (window.$3Dmol ? ok(window.$3Dmol) : bad(new Error("3Dmol cargó y no se registró")));
    s.onerror = () => bad(new Error("no se pudo cargar /vendor/3Dmol-min.js"));
    document.head.appendChild(s);
  });
  return molLoading;
}

// What each part is drawn as. A protein is a cartoon and the ligand inside it is sticks, which is how BioQL's own
// viewer shows a docked pair; the ligand is what the view centres on, because it is the thing that was designed.
const STRUCT_STYLE = {
  protein: { cartoon: { color: "spectrum" } },
  complex: { cartoon: { color: "spectrum" } },
  pocket: { cartoon: { color: "spectrum" } },
  // `colorscheme: Jmol` — by ELEMENT, the colours every chemist reads without a legend (O red, N blue, S yellow).
  // Without it 3Dmol draws a molecule loaded from a molfile in one grey, and a grey oxygen beside a 2D depiction
  // that draws it red is the same molecule telling two different stories.
  ligand: { stick: { radius: 0.18, colorscheme: "Jmol" }, sphere: { scale: 0.22, colorscheme: "Jmol" } },
  molecule: { stick: { radius: 0.18, colorscheme: "Jmol" }, sphere: { scale: 0.22, colorscheme: "Jmol" } },
  structure: { cartoon: { color: "spectrum" } },
};

function structureNode(b) {
  const parts = Array.isArray(b.parts) && b.parts.length
    ? b.parts
    : (b.sdf ? [{ role: "molecule", format: "sdf", data: b.sdf, path: b.label || "structure" }] : []);
  const box = el("div", "mol3d");
  const head = el("div", "mol3d-head");
  parts.forEach((p) => head.appendChild(chip(p.role + " · " + p.format, "neutral")));
  box.appendChild(head);
  if (!parts.length) return box;

  // PyMOL: la misma estructura, trazada. No vive dentro de la ventana —es un programa nativo de OpenGL— asi que
  // lo que da es la FOTO: 0,45 s de ray tracing sobre la proteina real, medido. Lo interactivo se queda aqui.
  // Mol*: superficies, seleccion, secuencia y medidas. Sustituye la vista rapida en el mismo hueco.
  const big = el("button", "tool", "Mol*");
  big.type = "button";
  big.title = "Abrir esta estructura en Mol* (superficies, selección, secuencia, medidas)";
  head.appendChild(big);
  const ray = el("button", "tool mol3d-ray", "PyMOL");
  ray.type = "button";
  ray.title = "Trazar esta estructura con PyMOL (imagen fija, calidad de publicacion)";
  head.appendChild(ray);
  const stage = el("div", "mol3d-stage");
  box.appendChild(stage);
  const note = el("p", "fine");
  box.appendChild(note);
  big.addEventListener("click", async () => {
    big.disabled = true;
    const was = big.textContent;
    big.textContent = "abriendo…";
    try {
      const star = await loadMolstar();
      clear(stage);
      stage.classList.add("mstar");
      const v = await star.Viewer.create(stage, {
        layoutIsExpanded: false, layoutShowControls: true, layoutShowSequence: true,
        layoutShowLog: false, layoutShowLeftPanel: true,
        viewportShowExpand: true, viewportShowSelectionMode: true, viewportShowAnimation: false,
      });
      for (const p of parts) {
        // `format` sale de la clave del cuerpo, comprobado contra el texto antes de llegar aqui.
        await v.loadStructureFromData(p.data, p.format === "cif" ? "mmcif" : p.format, false);
      }
      note.textContent = "Mol* · " + parts.map((p) => p.role + " " + p.format).join(" · ");
      big.remove();
    } catch (e) {
      note.textContent = "Mol* no pudo abrir: " + String((e && e.message) || e);
      big.disabled = false;
      big.textContent = was;
    }
  });

  ray.addEventListener("click", async () => {
    const part = parts.find((p) => p.role !== "ligand") || parts[0];
    ray.disabled = true;
    const was = ray.textContent;
    ray.textContent = "trazando…";
    try {
      const got = await api("/api/render3d", { data: part.data, format: part.format, style: "cartoon" });
      const r = got.body;
      if (r && r.state === "RESOLVED" && r.value && r.value.png_base64) {
        const f = el("figure", "shot-fig");
        const img = el("img", "shot");
        img.src = "data:image/png;base64," + r.value.png_base64;
        img.alt = "estructura trazada con PyMOL";
        f.appendChild(img);
        f.appendChild(el("figcaption", "fine",
          `PyMOL ${r.value.pymol || "?"} · ${part.format} · ${r.value.seconds}s · ${r.value.width}x${r.value.height}`));
        box.appendChild(f);
        ray.remove();
        return;
      }
      // ABSENT y UNRESOLVABLE son respuestas distintas y se dicen distintas: "no esta instalado" no es "fallo".
      note.textContent = (r && r.state === "ABSENT" ? "PyMOL: " : "PyMOL no pudo: ")
        + ((r && r.detail) || "sin respuesta");
    } catch (e) {
      note.textContent = "PyMOL no pudo: " + String((e && e.message) || e);
    } finally {
      ray.disabled = false;
      ray.textContent = was;
    }
  });

  const fallback = (why) => {
    // A viewer that cannot run is said, and the structure is still shown as what it is: text BioQL wrote. Drawing
    // nothing, or drawing a placeholder molecule, would both be this window inventing an answer.
    stage.remove();
    note.textContent = "la estructura no se pudo dibujar aquí (" + why + "); va entera en el resultado";
    parts.forEach((p) => {
      const lines = String(p.data).split("\n");
      const pre = el("pre", "sdf", lines.slice(0, 6).join("\n")
        + (lines.length > 6 ? "\n… " + (lines.length - 6) + " línea(s) más" : ""));
      box.appendChild(pre);
    });
  };

  load3Dmol().then(($) => {
    let viewer;
    try {
      viewer = $.createViewer(stage, { backgroundColor: "white", antialias: true });
      parts.forEach((p) => {
        viewer.addModel(String(p.data), p.format);
      });
      viewer.setStyle({}, { line: {} });
      parts.forEach((p, i) => {
        viewer.setStyle({ model: i }, STRUCT_STYLE[p.role] || STRUCT_STYLE.molecule);
      });
      // Encuadre: la estructura ENTERA. Centrar en el ligando deja la camara dentro de la proteina y lo que se ve
      // es un trozo de helice, no un complejo (comprobado en WebKit con el cuerpo real: 129 KB de proteina).
      const lig = parts.findIndex((p) => p.role === "ligand" || p.role === "molecule");
      viewer.zoomTo();
      if (lig >= 0 && parts.length > 1) viewer.center({ model: lig }, 0);
      viewer.render();
      note.textContent = parts.map((p) => p.path + " (" + (p.bytes || String(p.data).length) + " bytes)").join(" · ");
    } catch (e) {
      fallback(String((e && e.message) || e));
    }
  }).catch((e) => fallback(String((e && e.message) || e)));

  return box;
}

// ---- charts, in plain SVG -------------------------------------------------
function svgEl(tag, attrs) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.keys(attrs || {}).forEach((k) => n.setAttribute(k, String(attrs[k])));
  return n;
}

function niceTicks(lo, hi, count) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step / 2; v += step) out.push(v);
  return out;
}

function fmtNum(v) {
  if (!isFinite(v)) return String(v);
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e6)) return v.toExponential(1);
  return String(Math.round(v * 1000) / 1000);
}

/** A series BioQL measured: one line, its own axes, and no scale this window invented. */
function seriesNode(b) {
  const pts = (b.points || []).filter((p) => p && isFinite(p[0]) && isFinite(p[1]));
  const box = el("div", "chart");
  box.appendChild(el("div", "chart-title", (b.y_label || "series") + " / " + (b.x_label || "x")));
  if (pts.length < 2) {
    box.appendChild(el("p", "fine", pts.length + " punto(s): hacen falta dos para una línea"));
    return box;
  }
  const W = 560, H = 210, L = 54, R = 12, T = 12, B = 30;
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 0.5; y1 += 0.5; }          // una serie plana es una línea recta, no una división por cero
  const sx = (v) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const sy = (v) => H - B - ((v - y0) / (y1 - y0)) * (H - T - B);

  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${W} ${H}`, role: "img",
                             "aria-label": (b.y_label || "series") + " contra " + (b.x_label || "x") });
  niceTicks(y0, y1, 4).forEach((v) => {
    svg.appendChild(svgEl("line", { class: "ch-grid", x1: L, x2: W - R, y1: sy(v), y2: sy(v) }));
    const tx = svgEl("text", { class: "ch-tick", x: L - 6, y: sy(v) + 4, "text-anchor": "end" });
    tx.textContent = fmtNum(v);
    svg.appendChild(tx);
  });
  [x0, x1].forEach((v, i) => {
    const tx = svgEl("text", { class: "ch-tick", x: sx(v), y: H - 10, "text-anchor": i ? "end" : "start" });
    tx.textContent = fmtNum(v);
    svg.appendChild(tx);
  });
  const d = pts.map((p, i) => (i ? "L" : "M") + sx(p[0]).toFixed(1) + " " + sy(p[1]).toFixed(1)).join(" ");
  const path = svgEl("path", { class: "ch-line", d: d, fill: "none" });
  svg.appendChild(path);
  pts.slice(0, 200).forEach((p) => svg.appendChild(svgEl("circle", { class: "ch-dot", cx: sx(p[0]), cy: sy(p[1]), r: 2.5 })));
  box.appendChild(svg);
  // El trazo se dibuja solo; `prefers-reduced-motion` lo apaga en app.css.
  const len = path.getTotalLength ? path.getTotalLength() : 0;
  if (len) { path.setAttribute("stroke-dasharray", String(len)); path.setAttribute("stroke-dashoffset", String(len));
             requestAnimationFrame(() => path.classList.add("drawn")); }
  return box;
}

/** El radar de ADMET, con los ejes y la escala 0..1 de `ADMETRadarChart.tsx` — su rama `radar_summary`, la que su
    propio comentario llama "quantum-computed, not heuristic". Un eje que BioQL no dio se dibuja como radio sin
    vertice: el poligono se cierra solo entre lo medido, porque una figura rellena con valores por defecto se lee
    como una evaluacion que nadie hizo. */
function radarNode(b) {
  const axes = (b.axes || []).filter((a) => a && a.label);
  const box = el("div", "chart");
  box.appendChild(el("div", "chart-title", "ADMET · " + (b.source || "radar_summary")));
  if (axes.length < 3) {
    box.appendChild(el("p", "fine", axes.length + " eje(s): un radar necesita tres"));
    return box;
  }
  const S = 320, c = S / 2, R = c - 54, max = b.max || 1;
  const ang = (i) => (Math.PI * 2 * i) / axes.length - Math.PI / 2;
  const at = (i, r) => [c + Math.cos(ang(i)) * r, c + Math.sin(ang(i)) * r];
  const svg = svgEl("svg", { class: "chart-svg radar", viewBox: `0 0 ${S} ${S}`, role: "img",
                             "aria-label": "radar ADMET de " + axes.length + " ejes" });
  [0.25, 0.5, 0.75, 1].forEach((f) => {
    const pts = axes.map((_, i) => at(i, R * f).map((n) => n.toFixed(1)).join(",")).join(" ");
    svg.appendChild(svgEl("polygon", { class: "ch-ring", points: pts, fill: "none" }));
  });
  axes.forEach((a, i) => {
    const [x, y] = at(i, R);
    svg.appendChild(svgEl("line", { class: "ch-spoke" + (a.value === null || a.value === undefined ? " none" : ""),
                                    x1: c, y1: c, x2: x, y2: y }));
    const [lx, ly] = at(i, R + 20);
    const tx = svgEl("text", {
      class: "ch-axis" + (a.value === null || a.value === undefined ? " none" : ""),
      x: lx.toFixed(1), y: (ly + 4).toFixed(1),
      "text-anchor": Math.abs(lx - c) < 6 ? "middle" : (lx > c ? "start" : "end"),
    });
    tx.textContent = a.label;
    svg.appendChild(tx);
  });
  const known = axes.map((a, i) => [a, i]).filter(([a]) => typeof a.value === "number" && isFinite(a.value));
  if (known.length >= 3) {
    const pts = known.map(([a, i]) => at(i, (R * Math.min(1, a.value / max))).map((n) => n.toFixed(1)).join(",")).join(" ");
    const poly = svgEl("polygon", { class: "ch-area", points: pts });
    svg.appendChild(poly);
    requestAnimationFrame(() => poly.classList.add("drawn"));
  }
  known.forEach(([a, i]) => {
    const [x, y] = at(i, R * Math.min(1, a.value / max));
    const dot = svgEl("circle", { class: "ch-dot", cx: x.toFixed(1), cy: y.toFixed(1), r: 3 });
    const title = svgEl("title", {});
    title.textContent = a.label + ": " + fmtNum(a.value);
    dot.appendChild(title);
    svg.appendChild(dot);
  });
  box.appendChild(svg);
  const sin = axes.filter((a) => a.value === null || a.value === undefined).map((a) => a.label);
  if (sin.length) box.appendChild(el("p", "fine", "sin medir: " + sin.join(", ")));
  return box;
}

/** Numbers BioQL measured, side by side. Bars, not a radar: a radar needs a range per property to normalise
    against, and a range this window chose would be a scale nobody measured. The values are drawn as they came. */
function barsNode(rows, title) {
  const all = rows.filter((r) => typeof r.value === "number" && isFinite(r.value));
  if (all.length < 2) return null;
  // Un tope: 290 barras en una tarjeta (medido con el resultado real de anticuerpos) no es un grafico, es una
  // pared. Se dibujan las primeras y se dice cuantas quedan, que siguen enteras arriba en la lista de medidas.
  const MAX_BARS = 24;
  const num = all.slice(0, MAX_BARS);
  const box = el("div", "chart");
  box.appendChild(el("div", "chart-title",
    title + (all.length > MAX_BARS ? ` · ${MAX_BARS} de ${all.length}` : "")));
  const W = 560, rowH = 22, L = 190, R = 60;
  const H = num.length * rowH + 10;
  const hi = Math.max(...num.map((r) => Math.abs(r.value))) || 1;
  const svg = svgEl("svg", { class: "chart-svg", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title });
  num.forEach((r, i) => {
    const y = i * rowH + 6;
    const w = (Math.abs(r.value) / hi) * (W - L - R);
    const lab = svgEl("text", { class: "ch-tick", x: L - 8, y: y + 11, "text-anchor": "end" });
    lab.textContent = String(r.label).slice(0, 30);
    svg.appendChild(lab);
    svg.appendChild(svgEl("rect", { class: "ch-bar" + (r.value < 0 ? " neg" : ""), x: L, y: y, width: Math.max(1, w), height: rowH - 8, rx: 2 }));
    const v = svgEl("text", { class: "ch-tick", x: L + w + 6, y: y + 11 });
    v.textContent = fmtNum(r.value) + (r.unit ? " " + r.unit : "");
    svg.appendChild(v);
  });
  box.appendChild(svg);
  return box;
}

// ------------------------------------------------------------------ the chat: Claude Code, and the cards
const SEEDS = ["Is the engine up and what did it actually run?",
               "ADMET on paracetamol, and tell me if the label matches the execution",
               "Design a therapeutic peptide — start by telling me which routes you have",
               "Show me the findings the verifier recorded today"];

function cardNode(card) {
  // A fan-out: several independent calls that ran AT THE SAME TIME. One card each, under a head that counts the
  // states — merging them into one card would lose which answer came from which call.
  if (card.kind === "group") {
    const wrap = el("div", "group-card");
    const head = el("div", "group-head");
    head.appendChild(el("h3", "card-title", card.title || "run together"));
    Object.keys(card.counts || {}).forEach((s) => head.appendChild(chip(s.toLowerCase() + " " + card.counts[s],
                                                                       s.toLowerCase())));
    head.appendChild(el("span", "hash", "group " + String(card.group || "").slice(0, 8)));
    if (card.subtitle) head.appendChild(el("p", "card-sub", card.subtitle));
    wrap.appendChild(head);
    (card.cards || []).forEach((c) => { if (c) wrap.appendChild(cardNode(c)); });
    return wrap;
  }
  const box = el("div", "card" + (card.verdict === "contradicted" ? " contradicted" : ""));
  const head = el("div", "card-head");
  head.appendChild(el("h3", "card-title", card.title || card.kind || "result"));
  if (card.verdict) head.appendChild(chip(card.verdict, card.verdict));
  (card.findings || []).slice(0, 4).forEach((f) => head.appendChild(chip(f.code, "contradicted")));
  if (card.subtitle) head.appendChild(el("p", "card-sub", card.subtitle));
  box.appendChild(head);

  const body = el("div", "card-body");
  (card.blocks || []).forEach((b) => {
    if (b.type === "facts") {
      const g = el("div", "facts");
      (b.rows || []).forEach((r) => {
        const f = el("div", "fact" + (r.absent ? " absent" : ""));
        f.appendChild(el("div", "fact-k", r.label));
        const v = el("div", "fact-v");
        v.appendChild(txt(r.value === null || r.value === undefined ? (r.note || "not computed") : r.value));
        if (r.unit) v.appendChild(el("span", "unit", r.unit));
        f.appendChild(v);
        g.appendChild(f);
      });
      body.appendChild(g);
      const bars = barsNode((b.rows || []).filter((r) => !r.absent), b.title || "medidas");
      if (bars) body.appendChild(bars);
    } else if (b.type === "table") {
      const t = el("table", "grid"), hr = el("tr");
      (b.columns || []).forEach((c) => hr.appendChild(el("th", null, c)));
      t.appendChild(hr);
      (b.rows || []).slice(0, 60).forEach((row) => {
        const tr = el("tr");
        row.forEach((c) => tr.appendChild(el("td", null, c === null ? "—" : c)));
        t.appendChild(tr);
      });
      body.appendChild(t);
    } else if (b.type === "molecule2d") {
      if (b.png_base64) {
        const img = el("img", "shot");
        img.src = "data:image/png;base64," + b.png_base64;
        img.alt = b.smiles || "molecule";
        body.appendChild(img);
      } else if (b.smiles) {
        // No depiction in the answer, only the SMILES. Showing the string is honest; drawing something would be
        // this window inventing a structure the engine did not return.
        const f = el("div", "fact");
        f.appendChild(el("div", "fact-k", "smiles"));
        f.appendChild(el("div", "fact-v hash", b.smiles));
        body.appendChild(f);
      }
    } else if (b.type === "molecule3d") {
      body.appendChild(structureNode(b));
    } else if (b.type === "series") {
      body.appendChild(seriesNode(b));
    } else if (b.type === "radar") {
      body.appendChild(radarNode(b));
    } else if (b.type === "chart_image") {
      // El grafico que BioQL YA dibujo. Se muestra como lo hizo el motor: redibujarlo peor desde los numeros
      // seria la tarjeta prefiriendose a si misma antes que al motor.
      const f = el("figure", "shot-fig");
      const img = el("img", "shot");
      img.src = "data:image/png;base64," + b.png_base64;
      img.alt = b.title || "grafico";
      img.loading = "lazy";
      f.appendChild(img);
      if (b.title) f.appendChild(el("figcaption", "fine", b.title));
      body.appendChild(f);
    } else if (b.type === "status") {
      const s = el("div", "stat " + (b.tone || "plain"));
      s.appendChild(el("b", "stat-w", b.word));
      Object.keys(b.counts || {}).forEach((k) => {
        s.appendChild(chip(k.replace(/^n_/, "") + " " + b.counts[k], b.counts[k] ? "warn" : "neutral"));
      });
      s.appendChild(el("span", "fine", b.where));
      body.appendChild(s);
      (b.issues || []).forEach((i) => body.appendChild(el("p", "stat-i", i)));
      if (b.issues_total > (b.issues || []).length) {
        body.appendChild(el("p", "fine", (b.issues_total - b.issues.length) + " mas no mostradas"));
      }
    } else if (b.type === "gates") {
      // Una puerta sin su razon pierde justo la mitad que dice lo que vale la afirmacion.
      const g = el("div", "gates");
      (b.rows || []).forEach((r) => {
        const row = el("div", "gate" + (r.open ? " on" : ""));
        row.appendChild(icon(r.open ? "check" : "tabx", "ic gate-i"));
        row.appendChild(el("div", "gate-k", r.label));
        row.appendChild(el("div", "gate-w", r.why || (r.open ? "sin razon declarada" : "sin razon declarada")));
        g.appendChild(row);
      });
      body.appendChild(g);
    } else if (b.type === "absent") {
      body.appendChild(el("p", "absent-block", (b.what || "this") + " — " + (b.why || "not computed")));
    } else if (b.type === "links") {
      const d = el("div", "row-head");
      // The card already refuses a bad scheme, and so does this: a link is an anchor, and `javascript:` or a
      // protocol-relative `//host` in one is a script or another origin, not a download.
      (b.items || []).forEach((i) => {
        const u = String(i.url || "");
        const ok = /^https?:\/\//i.test(u) || (/^\/(?!\/)/.test(u) && !/[\\\s]/.test(u));
        if (!ok) { d.appendChild(el("span", "hash", i.label + " — link refused: " + u.slice(0, 40))); return; }
        const a = el("a", "hash", i.label);
        a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer";
        d.appendChild(a);
      });
      body.appendChild(d);
    } else if (b.text) {
      const t = String(b.text);
      const cut = t.length > 600;
      body.appendChild(el("pre", "sdf", cut ? t.slice(0, 600) + "\n… " + (t.length - 600) + " more character(s)" : t));
    }
  });
  box.appendChild(body);

  const p = card.provenance || {};
  const prov = el("div", "prov");
  prov.appendChild(el("span", "fact-k", "ran on"));
  prov.appendChild(chip(p.is_real_hardware === true ? "real hardware"
                      : p.is_real_hardware === false ? "not hardware" : "not stated",
                      p.is_real_hardware === true ? "concerns" : "plain"));
  (p.ibm_job_ids || []).forEach((j) => prov.appendChild(el("span", "hash", j)));
  (p.quantum_backends || []).slice(0, 3).forEach((bk) => prov.appendChild(el("span", "hash", bk)));
  box.appendChild(prov);
  return box;
}

const TURNS = [];
let asking = false;

function beat(host, e, turn) {
  if (e.type === "say") host.appendChild(el("div", "beat beat-say", e.text));
  else if (e.type === "tool") {
    const name = String(e.name || "").replace(/^mcp__bioql__/, "");
    if (turn && !turn.tools.includes(name)) turn.tools.push(name);
    const d = el("div", "beat beat-tool");
    d.appendChild(txt(name));
    d.appendChild(el("span", "args", JSON.stringify(e.args || {}).slice(0, 90)));
    host.appendChild(d);
  } else if (e.type === "tool_result" && e.card) host.appendChild(cardNode(e.card));
  else if (e.type === "fail") host.appendChild(el("div", "beat beat-fail", e.text));
  else if (e.type === "raw") host.appendChild(el("div", "beat fine", e.text));
}

async function send(text) {
  if (asking || !text.trim()) return;
  open_("chat");
  asking = true;
  $("ask-go").disabled = true;
  ring("busy");
  document.body.classList.add("has-lines");
  $("hero").hidden = true;
  const scroll = $("scroll"), lines = $("lines");

  const node = el("div", "turn");
  node.appendChild(el("div", "said", text));
  const host = el("div", "beats");
  const wait = el("div", "thinking");
  wait.appendChild(el("i")); wait.appendChild(el("i")); wait.appendChild(el("i"));
  wait.appendChild(txt("working the engine"));
  host.appendChild(wait);
  node.appendChild(host);
  lines.appendChild(node);
  scroll.scrollTop = scroll.scrollHeight;
  const turn = { text, t: Date.now() / 1000, tools: [], node };
  TURNS.push(turn);

  const started = await api("/api/chat", { text });
  if (started.code !== 200) {
    wait.remove();
    host.appendChild(el("div", "beat beat-fail", started.body.error || "the brain did not start"));
    asking = false; $("ask-go").disabled = false; ring("idle");
    return;
  }
  let since = 0, done = false;
  while (!done) {
    await new Promise((r) => setTimeout(r, 700));
    const { body } = await api(`/api/chat?id=${encodeURIComponent(started.body.id)}&since=${since}`);
    (body.events || []).forEach((e) => { wait.remove(); beat(host, e, turn); });
    since = body.next !== undefined ? body.next : since;
    done = body.state === "done";
    scroll.scrollTop = scroll.scrollHeight;
  }
  wait.remove();
  asking = false;
  $("ask-go").disabled = !$("ask-in").value.trim();
  ring("idle");
  await drawState();
}

// ------------------------------------------------------------------ nav, tabs, views
let view = "chat";
const openTabs = ["chat"];

// Que grupos estan plegados. Es una comodidad por ventana, no estado del trabajo: si el navegador no deja
// guardarla (ventana privada, datos bloqueados) el menu abre entero, que es el estado util.
const SHUT_KEY = "bioql-studio.nav.shut";

function shutGroups() {
  try {
    const raw = localStorage.getItem(SHUT_KEY);
    const v = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(v) ? v.filter((x) => typeof x === "string") : []);
  } catch (e) {
    return new Set();
  }
}

function rememberShut(set) {
  try { localStorage.setItem(SHUT_KEY, JSON.stringify([...set])); } catch (e) { /* nada que recordar, nada roto */ }
}

const GROUP_NAME = { Iris: "Instrument", Work: "The work", Record: "The record" };

function buildNav() {
  const nav = $("nav");
  clear(nav);
  const shut = shutGroups();
  GROUPS.forEach((g) => {
    const keys = Object.keys(SECTIONS).filter((k) => SECTIONS[k].group === g);
    if (!keys.length) return;
    const box = el("div", "side-g" + (shut.has(g) ? " shut" : ""));
    const head = el("button", "side-h");
    head.type = "button";
    head.setAttribute("aria-expanded", shut.has(g) ? "false" : "true");
    const chev = icon("chev", "ic chev");
    head.appendChild(chev);
    head.appendChild(el("span", "nm", GROUP_NAME[g] || g));
    head.appendChild(el("span", "ct", String(keys.length)));
    const items = el("div", "side-items");
    head.addEventListener("click", () => {
      const now = shutGroups();
      if (now.has(g)) { now.delete(g); } else { now.add(g); }
      rememberShut(now);
      box.classList.toggle("shut", now.has(g));
      head.setAttribute("aria-expanded", now.has(g) ? "false" : "true");
    });
    box.appendChild(head);
    keys.forEach((k) => {
      const s = SECTIONS[k];
      const b = el("button");
      b.type = "button";
      b.dataset.v = k;
      b.title = s.label;
      b.appendChild(icon(s.icon));
      b.appendChild(el("span", null, s.label));
      b.appendChild(el("em"));
      b.addEventListener("click", () => open_(k));
      items.appendChild(b);
    });
    box.appendChild(items);
    nav.appendChild(box);
  });
}

function drawTabs() {
  const bar = $("tabs");
  clear(bar);
  openTabs.forEach((k) => {
    const s = SECTIONS[k];
    const t = el("div", "tab" + (k === view ? " on" : ""));
    t.setAttribute("role", "tab");
    t.appendChild(icon(s.icon));
    const name = el("span", null, s.label);
    name.addEventListener("click", () => open_(k));
    t.appendChild(name);
    if (k !== "chat") {
      const x = el("button", "tab-x");
      x.setAttribute("aria-label", "Close " + s.label);
      x.appendChild(icon("tabx"));
      x.addEventListener("click", (e) => {
        e.stopPropagation();
        const i = openTabs.indexOf(k);
        openTabs.splice(i, 1);
        if (view === k) open_(openTabs[Math.max(0, i - 1)] || "chat");
        else drawTabs();
      });
      t.appendChild(x);
    }
    t.addEventListener("click", () => open_(k));
    bar.appendChild(t);
  });
}

// Every section is drawn under a number. Two clicks in a row used to leave the SLOWER draw writing into the page
// of the section that had already replaced it — a blank frame, or one section's rows under another's title. A draw
// that is no longer the open one is dropped.
let seq = 0;

async function draw() {
  const s = SECTIONS[view];
  if (s.kind === "chat") return;
  const mine = seq;
  // Built DETACHED and swapped in whole, so a half-drawn page is never on screen and a stale one never lands.
  const box = el("div");
  switch (s.kind) {
    case "jobs": await drawJobs(box); break;
    case "mols": await drawMols(box); break;
    case "sar": await drawSar(box); break;
    case "cands": await drawCands(box); break;
    case "report": await drawReport(box); break;
    case "keys": await drawKeys(box); break;
    case "findings": await drawFindings(box); break;
    case "board": await drawBoard(box); break;
    case "ledger": await drawLedger(box); break;
    case "guard": await drawGuard(box); break;
    case "servers": await drawServers(box); break;
    case "usage": await drawUsage(box); break;
    case "settings": drawSettings(box); break;
    case "chats": drawChats(box); break;
  }
  if (mine !== seq) return;                       // a later click already took this page
  const host = $("pageBody");
  clear(host);
  while (box.firstChild) host.appendChild(box.firstChild);
}

async function open_(k) {
  const s = SECTIONS[k];
  if (!s) return;
  seq += 1;
  view = k;
  document.body.setAttribute("data-view", k);
  if (!openTabs.includes(k)) openTabs.push(k);
  drawTabs();
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.v === k));
  $("v-chat").hidden = s.kind !== "chat";
  $("v-page").hidden = s.kind === "chat";
  if (s.kind !== "chat") {
    $("pageTitle").textContent = s.label;
    $("pageLead").textContent = s.lead || "";
    empty($("pageBody"), "reading…");
  }
  // There is no second half any more. A section used to offer "Panel | Routes" — BioQL's page in a frame, or the
  // list of routes behind it. Neither exists here now, so neither does the switch.
  const old = document.getElementById("halfSeg");
  if (old) old.remove();
  await draw();
}


// ------------------------------------------------------------------ the command centre
let palSel = 0, palRows = [];

function palItems(q) {
  const out = [];
  const needle = q.trim().toLowerCase();
  Object.keys(SECTIONS).forEach((k) => {
    const s = SECTIONS[k];
    if (!needle || s.label.toLowerCase().includes(needle) || k.includes(needle)) {
      out.push({ icon: s.icon, label: s.label, note: s.group, go: () => open_(k) });
    }
  });
  if (needle) {
    // A matching route is still worth naming — but it is named into the CHAT, which is the only thing that runs
    // one now. The palette used to open a list of 502 route names, and a list is not work.
    TOOLS.filter((t) => t.name.includes(needle.replace(/\s+/g, "_")) || t.path.includes(needle))
      .slice(0, 8).forEach((t) => out.push({
        icon: "tools", label: t.name, note: t.method + " " + t.path,
        go: () => {
          open_("chat");
          $("ask-in").value = "Use " + t.name + " and tell me what it actually ran.";
          $("ask-go").disabled = false;
          $("ask-in").focus();
        },
      }));
    out.push({ icon: "orb", label: "Ask: " + q.trim(), note: "⌘↩", go: () => send(q.trim()) });
  }
  return out.slice(0, 40);
}

function drawPal() {
  const box = $("palList");
  clear(box);
  palRows = palItems($("palText").value);
  if (palSel >= palRows.length) palSel = 0;
  palRows.forEach((r, i) => {
    const b = el("button", "pal-item" + (i === palSel ? " sel" : ""));
    b.appendChild(icon(r.icon));
    b.appendChild(el("span", null, r.label));
    b.appendChild(el("small", null, r.note || ""));
    b.addEventListener("click", () => { closePal(); r.go(); });
    box.appendChild(b);
  });
}

function openPal() {
  $("palette").hidden = false;
  $("palText").value = "";
  palSel = 0;
  drawPal();
  $("palText").focus();
}
function closePal() { $("palette").hidden = true; }

// ------------------------------------------------------------------ start
async function boot() {
  buildNav();
  drawTabs();
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.v === "chat"));

  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("ask-in").value;
    $("ask-in").value = "";
    $("ask-go").disabled = true;
    send(v);
  });
  $("ask-in").addEventListener("input", () => { $("ask-go").disabled = !$("ask-in").value.trim() || asking; });
  SEEDS.forEach((q) => {
    const b = el("button", "seed", q);
    b.type = "button";
    b.addEventListener("click", () => send(q));
    $("seeds").appendChild(b);
  });
  $("chatNew").addEventListener("click", () => {
    clear($("lines"));
    document.body.classList.remove("has-lines");
    $("hero").hidden = false;
  });
  // Orb or transcript. The orb is the main screen; the transcript is the same turns without it.
  document.querySelectorAll("#chatMode button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#chatMode button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
    const orb = b.dataset.m === "orb";
    $("hero").hidden = !orb;
    document.body.classList.toggle("has-lines", !orb || TURNS.length > 0);
  }));

  document.querySelectorAll(".st[data-go]").forEach((b) => b.addEventListener("click", () => open_(b.dataset.go)));
  $("cmdCenter").addEventListener("click", openPal);
  $("palText").addEventListener("input", () => { palSel = 0; drawPal(); });
  $("palette").addEventListener("click", (e) => { if (e.target === $("palette")) closePal(); });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPal(); return; }
    if ($("palette").hidden) {
      if (e.key === "Escape" && !$("modal").hidden) $("modal").hidden = true;
      return;
    }
    if (e.key === "Escape") { closePal(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); palSel = Math.min(palSel + 1, palRows.length - 1); drawPal(); }
    if (e.key === "ArrowUp") { e.preventDefault(); palSel = Math.max(palSel - 1, 0); drawPal(); }
    if (e.key === "Enter") {
      e.preventDefault();
      const q = $("palText").value.trim();
      closePal();
      if ((e.metaKey || e.ctrlKey) && q) send(q);
      else if (palRows[palSel]) palRows[palSel].go();
    }
  });

  const st = await drawState();
  if (st) {
    await loadTools();
    await drawEngine();
    await drawFindings(document.createElement("div"));   // counts and lights the ring without opening anything
    await drawGuard(document.createElement("div"));
  }
  // Las etapas, cada 3 s — el mismo paso que `PipelineProgressTracker.tsx` en :3000. Aparte del
  // tick de 6 s: una etapa que dura 5 s se la salta un sondeo de 6, y la ventana se veria
  // congelada justo en lo que el usuario esta mirando.
  await drawProgress();
  setInterval(drawProgress, 3000);
  setInterval(async () => {
    await drawState();
    // The engine line is asked again every tick. Read once at boot it went STALE: the backend could die and the
    // status bar would still read "healthy" while every call in BioQL's own window answered 502 — a status line
    // that stops being asked is a claim, not a reading.
    await drawEngine();
    // The work pages are NOT redrawn on the tick: each one holds results a person recovered by hand, and a
    // six-second redraw would throw them away while they were being read.
    if (["findings", "ledger", "usage", "board"].includes(SECTIONS[view].kind)) await draw();
  }, 6000);
}

boot();
