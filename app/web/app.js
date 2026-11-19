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
  const m = /(?:^|[#&])t=([A-Za-z0-9_\-]+)/.exec(location.hash || "");
  if (m) {
    try { history.replaceState(null, "", location.pathname); } catch (e) { location.hash = ""; }
    return m[1];
  }
  return "";
})();

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
// BioQL's own menu, group by group: its activity bar, its sidebar's pipeline groups, its pages, its health panel.
// `kind` says who answers: `tools` = the engine through Studio's allowlist, `frame` = BioQL's own page, the rest is
// Studio's record.
const BIOQL_PANELS = ["Chat", "Explorer", "Search", "MCP Tools", "Quantum", "Molecular", "Pipelines",
                      "Undruggable", "TargetRadar", "Results", "Targets", "3D Viewer"];

// BioQL's own sidebar groups, verbatim from src/frontend/src/components/layout/Sidebar.tsx (PIPELINE_GROUPS): the
// same names, the same descriptions and the same prompt each one starts.
const PIPES = [
  { name: "Drug Discovery", icon: "flask", items: [
    { name: "Full Development", desc: "78 quantum algos + ADMET + Tox + Clinical", prompt: "discover drug for" },
    { name: "Custom Molecule", desc: "SMILES/IUPAC → full pipeline", prompt: "analyze molecule" },
    { name: "Peptide Discovery", desc: "Therapeutic peptide + QAOA", prompt: "design peptide for" }]},
  { name: "Analysis", icon: "tube", items: [
    { name: "ADMET (92 params)", desc: "Pharmacokinetic profile", prompt: "ADMET for" },
    { name: "Toxicology", desc: "QSVM/QNN tox assessment", prompt: "toxicology study for" },
    { name: "Pharmacology", desc: "PK/PD modeling", prompt: "pharmacology for" },
    { name: "Clinical Trial", desc: "Phase I-III simulation", prompt: "clinical trial for" }]},
  { name: "Structure", icon: "atom", items: [
    { name: "Protenix Predict", desc: "AlphaFold3-equiv (GPU)", prompt: "predict structure of" },
    { name: "AlphaGenomics", desc: "Gene expression + variants", prompt: "analyze gene" }]},
  { name: "Screening & SAR", icon: "find", items: [
    { name: "Virtual Screening", desc: "Vina + Quantum scoring", prompt: "screen compounds for" },
    { name: "Classical Docking", desc: "AutoDock Vina + IBM Q", prompt: "dock molecule to" },
    { name: "Selectivity Profile", desc: "Off-target IC50 panel", prompt: "selectivity for" },
    { name: "SAR Analysis", desc: "MMP, R-group, activity cliffs", prompt: "SAR analysis for" }]},
  { name: "Simulation", icon: "cube", items: [
    { name: "Molecular Dynamics", desc: "Quantum-enhanced MD", prompt: "MD simulation for" },
    { name: "FEP/ΔΔG", desc: "Free energy perturbation", prompt: "FEP calculation for" }]},
];

const AREA_OF = (t) => ((t.from_registry || {}).area) || (
  /^\/api\/(health|system)/.test(t.path) ? "health" :
  /^\/api\/(pipeline\/job|pipeline\/submit|recovery|scripts\/)/.test(t.path) ? "jobs" :
  /^\/api\/(smiles|render|rdkit|motifs|chem|structure|viz)/.test(t.path) ? "chem" :
  /^\/api\/(targets|targetradar)/.test(t.path) ? "targets" :
  /^\/api\/(screening|docking|vina|sar)/.test(t.path) ? "screening" :
  /^\/api\/(eln|report|candidates)/.test(t.path) ? "notebook" :
  /^\/api\/(quantum|v1\/quantum|v11|complexa|protenix)/.test(t.path) ? "quantum" :
  /^\/api\/(pipeline|casestudy|aging|bionics|genomics)/.test(t.path) ? "pipeline" : "other");

const SECTIONS = {
  // --- the instrument
  chat:     { group: "Iris", label: "Chat", icon: "orb", kind: "chat" },
  chats:    { group: "Iris", label: "Chats", icon: "chats", kind: "chats",
              lead: "Every turn of this window, with the tools each one ran." },

  // --- BioQL's activity bar, over the same engine routes
  pipelines: { group: "Discovery", label: "Pipelines", icon: "flow", kind: "pipes",
               lead: "BioQL's own pipeline groups. Each one starts the question it starts in BioQL; Claude Code " +
                     "picks the route from the closed list and nothing compute-bearing runs without a person." },
  quantum:  { group: "Discovery", label: "Quantum", icon: "cpu", kind: "tools", pick: (t) => AREA_OF(t) === "quantum",
              lead: "Every quantum route BioQL declares. Nothing here is offered a shortcut past approval, and a run " +
                    "is only called real hardware when the answer carries an IBM job id." },
  molecular:{ group: "Discovery", label: "Molecular", icon: "atom", kind: "tools", pick: (t) => AREA_OF(t) === "chem",
              lead: "Depiction, 3D, motifs, descriptors — BioQL's own chemistry routes." },
  screening:{ group: "Discovery", label: "Screening & SAR", icon: "find", kind: "tools",
              pick: (t) => AREA_OF(t) === "screening", lead: "Docking, virtual screening, SAR." },
  targets:  { group: "Discovery", label: "Targets", icon: "cross", kind: "tools",
              pick: (t) => AREA_OF(t) === "targets" && !/targetradar/.test(t.path) },
  radar:    { group: "Discovery", label: "TargetRadar", icon: "eye", kind: "tools",
              pick: (t) => /targetradar/.test(t.path),
              lead: "TargetRadar's routes as BioQL's backend exposes them." },
  undrug:   { group: "Discovery", label: "Undruggable", icon: "boxes", kind: "tools",
              pick: (t) => /(enterprise|undrug|complexa|ppi|allosteric)/i.test(t.path + " " + (t.why || "")),
              lead: "The enterprise pipeline BioQL labels Undruggable." },
  jobs:     { group: "Discovery", label: "Jobs & Recovery", icon: "pulse", kind: "tools",
              pick: (t) => AREA_OF(t) === "jobs",
              lead: "Submitting, polling and recovering a run. A receipt is not a result: a body with a job id is " +
                    "followed until it is final, and a receipt with no job id is UNRESOLVABLE, never empty." },
  pipeline: { group: "Discovery", label: "Pipeline routes", icon: "flow", kind: "tools",
              pick: (t) => AREA_OF(t) === "pipeline" },
  tools:    { group: "Discovery", label: "MCP Tools", icon: "tools", kind: "tools", pick: () => true,
              lead: "The whole closed list Studio may call. An entry marked needs a person is refused from this " +
                    "window and offers an approval instead; anything that could reach real quantum hardware asks first." },
  results:  { group: "Discovery", label: "Results", icon: "table", kind: "results",
              lead: "Every call that resolved, newest first, with the digest of what came back and the verdict the " +
                    "verifier recorded." },

  // --- BioQL's own window and its own pages
  bioql:    { group: "BioQL", label: "Workbench", icon: "win", kind: "frame", route: "", panels: true,
              lead: "BioQL's own window, whole and untouched. Studio never starts, stops or restarts it." },
  eln:      { group: "BioQL", label: "Lab Notebook", icon: "book", kind: "frame", route: "#/eln" },
  scr:      { group: "BioQL", label: "Screening", icon: "find", kind: "frame", route: "#/screening" },
  sar:      { group: "BioQL", label: "SAR Analysis", icon: "tube", kind: "frame", route: "#/sar" },
  report:   { group: "BioQL", label: "Report Builder", icon: "doc", kind: "frame", route: "#/report-builder" },
  cands:    { group: "BioQL", label: "Drug Candidates", icon: "flask", kind: "frame", route: "#/candidates" },
  viz:      { group: "BioQL", label: "BioQL Viz", icon: "cube", kind: "frame", route: "#/bioql-viz" },
  keys:     { group: "BioQL", label: "API Keys", icon: "key", kind: "frame", route: "#/api-keys" },

  // --- the record
  findings: { group: "Record", label: "Findings", icon: "check", kind: "findings",
              lead: "Every verdict the verifier recorded, newest first. A row whose line recorded no verdict says " +
                    "not recorded — it is never read as clean." },
  board:    { group: "Record", label: "Whiteboard", icon: "board", kind: "board",
              lead: "The plan drawn before anything is spent, and the evidence hung on it afterwards." },
  ledger:   { group: "Record", label: "Notebook", icon: "book", kind: "ledger",
              lead: "The hash-chained, Ed25519-signed record: one line per call to BioQL." },
  guard:    { group: "Record", label: "Guard", icon: "shield", kind: "guard",
              lead: "The two most recent saved snapshots of BioQL's source trees, compared. Opening this takes no " +
                    "new snapshot." },
  servers:  { group: "Record", label: "Server Health", icon: "server", kind: "servers",
              lead: "BioQL's own health routes, asked now." },
  usage:    { group: "Record", label: "Usage", icon: "pulse", kind: "usage",
              lead: "Counted off the signed record, not off a tally kept beside it." },
  settings: { group: "Record", label: "Settings", icon: "gear", kind: "settings" },
};

const GROUPS = ["Iris", "Discovery", "BioQL", "Record"];

// ------------------------------------------------------------------ the state line and the status bar
let uiUrl = "http://127.0.0.1:3000";
let lastState = null;

async function drawState() {
  const { code, body } = await api("/api/state");
  if (code === 403) {
    $("hubLine").textContent = "no token";
    $("hubDot").className = "dot bad";
    $("stEngine").querySelector("span").textContent =
      "no token — open this window from the app, or with the URL `python3 -m app token` prints";
    ring("down");
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
  if (body.bioql_ui) uiUrl = body.bioql_ui;
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

function toolRow(t) {
  const row = el("div", "row");
  const head = el("div", "row-head");
  head.appendChild(el("span", "tool-name", t.name));
  head.appendChild(chip(t.kind, "plain"));
  head.appendChild(el("span", "hash", t.method + " " + t.path));
  if (t.approval) head.appendChild(chip("needs a person", "concerns"));
  if (t.schema_known === false) head.appendChild(chip("schema not declared", "absent"));
  row.appendChild(head);

  const form = el("div", "row-head");
  const inputs = {};
  (t.required || []).concat(t.optional || []).slice(0, 6).forEach((k) => {
    const i = el("input", "field-in");
    i.placeholder = k + ((t.required || []).includes(k) ? "" : " (optional)");
    i.setAttribute("aria-label", k);
    inputs[k] = i;
    form.appendChild(i);
  });
  const go = el("button", "go", t.approval ? "Ask" : "Run");
  go.addEventListener("click", async () => {
    go.disabled = true;
    ring("busy");
    const args = {};
    Object.keys(inputs).forEach((k) => { if (inputs[k].value.trim()) args[k] = inputs[k].value.trim(); });
    const out = await api("/api/call", { name: t.name, args });
    go.disabled = false;
    ring("idle");
    const said = el("p", "row-why");
    said.appendChild(chip(out.body.state || out.code, String(out.body.state || "plain").toLowerCase()));
    said.appendChild(txt(" " + (out.body.detail || out.body.error || "")));
    row.appendChild(said);
    await drawState();
  });
  form.appendChild(go);
  const ask = el("button", "go", "Ask Claude");
  ask.style.background = "var(--fill-2)";
  ask.style.color = "var(--label)";
  ask.addEventListener("click", () => {
    open_("chat");
    $("ask-in").value = `Use ${t.name} and tell me what it actually ran.`;
    $("ask-go").disabled = false;
    $("ask-in").focus();
  });
  form.appendChild(ask);
  row.appendChild(form);
  if (t.why) row.appendChild(el("p", "tool-why", t.why));
  return row;
}

function drawToolsSection(box, pick) {
  const mine = TOOLS.filter(pick);
  if (!mine.length) return empty(box, "no route in the allowlist answers to this section.");
  clear(box);
  box.appendChild(el("p", "fine", `${mine.length} route(s) · ` +
    `${mine.filter((t) => t.approval).length} ask a person first`));
  // Grouped as BioQL groups its own: by what the route is for.
  const by = {};
  mine.forEach((t) => { (by[AREA_OF(t)] = by[AREA_OF(t)] || []).push(t); });
  Object.keys(by).sort().forEach((area) => {
    box.appendChild(el("h3", "sect", area));
    const g = el("div", "group");
    by[area].sort((a, b) => a.name.localeCompare(b.name)).forEach((t) => g.appendChild(toolRow(t)));
    box.appendChild(g);
  });
}

function drawPipes(box) {
  clear(box);
  PIPES.forEach((grp) => {
    box.appendChild(el("h3", "sect", grp.name));
    const g = el("div", "group");
    grp.items.forEach((it) => {
      const row = el("div", "row");
      const b = el("button", "pick");
      b.appendChild(icon(grp.icon));
      const main = el("div", "pick-main");
      main.appendChild(el("b", null, it.name));
      main.appendChild(el("small", null, it.desc));
      main.appendChild(el("span", "pick-seed", "“" + it.prompt + " …”"));
      b.appendChild(main);
      b.addEventListener("click", () => {
        open_("chat");
        $("ask-in").value = it.prompt + " ";
        $("ask-go").disabled = false;
        $("ask-in").focus();
      });
      row.appendChild(b);
      g.appendChild(row);
    });
    box.appendChild(g);
  });
}

async function drawFindings(box) {
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).filter((r) => r.verdict || (r.findings || []).length).reverse();
  const bad = rows.filter((r) => r.verdict === "contradicted").length;
  const em = document.querySelector('nav button[data-v=findings] em');
  if (em) { em.textContent = rows.length ? String(rows.length) : ""; em.className = bad ? "hot" : ""; }
  $("stFind").querySelector("span").textContent = rows.length
    ? `${rows.length} verdict(s)` + (bad ? ` · ${bad} contradicted` : "") : "nothing verified yet";
  $("stFind").className = "st" + (bad ? " bad" : "");
  if (bad) ring("alarm");
  if (!rows.length) return empty(box, "nothing verified yet. A verdict is recorded on every call that resolves.");
  clear(box);
  const g = el("div", "group");
  rows.forEach((r) => {
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

async function drawResults(box) {
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).filter((r) => r.state === "RESOLVED").reverse();
  if (!rows.length) return empty(box, "nothing has resolved yet.");
  clear(box);
  const g = el("div", "group");
  rows.forEach((r) => {
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", r.tool));
    if (r.verdict) head.appendChild(chip(r.verdict, r.verdict));
    if (r.by) head.appendChild(el("span", "hash", "by " + r.by));
    if (r.job_id) head.appendChild(el("span", "hash", "job " + r.job_id));
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

async function drawGuard(box) {
  const { code, body } = await api("/api/guard");
  if (code !== 200) return empty(box, body.error || "the guard did not answer");
  if (body.state && body.state !== "RESOLVED") {
    $("stGuard").querySelector("span").textContent = "no snapshot pair";
    return empty(box, (body.detail || "") + "  Take one with: python3 -m studio guard snapshot <name>");
  }
  const rep = body.report || body;
  clear(box);
  box.appendChild(el("p", rep.clean ? "fine" : "alarm-strip", rep.clean
    ? "BioQL is as it was between the two most recent snapshots."
    : "BioQL changed between the two most recent snapshots. The guard names the files; it never says who changed them."));
  const g = el("div", "group");
  Object.keys(rep.roots || {}).forEach((name) => {
    const r = rep.roots[name];
    const row = el("div", "row");
    const head = el("div", "row-head");
    head.appendChild(el("span", "row-tool", name));
    head.appendChild(chip(r.clean ? "unchanged" : "changed", r.clean ? "clean" : "concerns"));
    head.appendChild(el("span", "hash", `${r.files} file(s) watched`));
    row.appendChild(head);
    ["added", "removed", "changed"].forEach((k) => {
      if (!(r[k] || []).length) return;
      row.appendChild(el("p", "row-why", `${k}: ${r[k].slice(0, 6).join(", ")}` +
        (r[k].length > 6 ? ` … and ${r[k].length - 6} more` : "")));
    });
    g.appendChild(row);
  });
  box.appendChild(g);
  $("stGuard").querySelector("span").textContent = rep.clean ? "BioQL unchanged" : "BioQL changed";
  $("stGuard").className = "st" + (rep.clean ? "" : " warn");
  const em = document.querySelector('nav button[data-v=guard] em');
  if (em) { em.textContent = rep.clean ? "" : "!"; em.className = rep.clean ? "" : "hot"; }
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
  const health = TOOLS.filter((t) => t.method === "GET" && !t.approval && !(t.required || []).length &&
                                     (/health|live_status|coherence/.test(t.path) || AREA_OF(t) === "health"));
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
  for (const r of rows.slice(0, first.length)) await r.ask();
}

async function drawUsage(box) {
  const { code, body } = await api("/api/ledger?limit=500");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = body.rows || [];
  clear(box);
  const st = lastState || {};
  const g0 = el("div", "group pad");
  g0.appendChild(el("p", null, `${st.calls || 0} call(s) · ${st.refusals || 0} refused · ` +
    `${st.approvals || 0} approved · ${rows.length} line(s) read from the record`));
  box.appendChild(g0);

  const byState = {}, byTool = {}, byVerdict = {};
  rows.forEach((r) => {
    byState[r.state || "—"] = (byState[r.state || "—"] || 0) + 1;
    byTool[r.tool || "—"] = (byTool[r.tool || "—"] || 0) + 1;
    if (r.verdict) byVerdict[r.verdict] = (byVerdict[r.verdict] || 0) + 1;
  });
  const table = (title, obj) => {
    box.appendChild(el("h3", "sect", title));
    const g = el("div", "group");
    Object.keys(obj).sort((a, b) => obj[b] - obj[a]).slice(0, 25).forEach((k) => {
      const row = el("div", "row");
      const head = el("div", "row-head");
      head.appendChild(el("span", "row-tool", k));
      head.appendChild(el("span", "hash", String(obj[k])));
      row.appendChild(head);
      g.appendChild(row);
    });
    box.appendChild(g);
  };
  table("by state", byState);
  table("by verdict", byVerdict);
  table("by tool", byTool);
}

function drawSettings(box) {
  const st = lastState || {};
  clear(box);
  const g = el("div", "group pad");
  [["version", st.version], ["service", "127.0.0.1:" + st.port], ["data", st.home],
   ["BioQL's window", uiUrl], ["tools in the allowlist", String(TOOLS.length)],
   ["tools that ask a person", String(TOOLS.filter((t) => t.approval).length)],
   ["the brain", "Claude Code, and nothing else — BioQL's own model routes are not in the allowlist"]]
    .forEach(([k, v]) => {
      const row = el("div", "row");
      const head = el("div", "row-head");
      head.appendChild(el("span", "fact-k", k));
      head.appendChild(el("span", "hash", v === undefined || v === null ? "—" : String(v)));
      row.appendChild(head);
      g.appendChild(row);
    });
  box.appendChild(g);
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
async function showFrame(route, panels) {
  const frame = $("bioql"), room = $("waiting-room");
  const want = uiUrl + "/" + (route || "");
  const { body } = await api("/api/ui");
  const up = body && body.up === true;
  $("frameUrl").textContent = want;
  if (up) {
    if (frame.dataset.want !== want) { frame.dataset.want = want; frame.src = want; }
    frame.classList.remove("hidden");
    room.classList.add("hidden");
  } else {
    frame.classList.add("hidden");
    room.classList.remove("hidden");
    $("waiting-why").textContent = "BioQL's window is not up at " + uiUrl;
    ring("down");
  }
  if (panels) {
    $("frameTitle").title = "BioQL's own activity bar: " + BIOQL_PANELS.join(" · ");
  }
  return up;
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
    } else if (b.type === "molecule3d" && b.sdf) {
      const lines = String(b.sdf).split("\n");
      const top = lines.slice(0, 6).join("\n");
      body.appendChild(el("pre", "sdf", top + (lines.length > 6 ? "\n… " + (lines.length - 6) + " more line(s)" : "")));
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

function buildNav() {
  const nav = $("nav");
  clear(nav);
  GROUPS.forEach((g) => {
    nav.appendChild(el("div", "side-h", g === "Iris" ? "Instrument" : g === "BioQL" ? "BioQL's own window" : g));
    Object.keys(SECTIONS).filter((k) => SECTIONS[k].group === g).forEach((k) => {
      const s = SECTIONS[k];
      const b = el("button");
      b.dataset.v = k;
      b.appendChild(icon(s.icon));
      b.appendChild(el("span", null, s.label));
      b.appendChild(el("em"));
      b.addEventListener("click", () => open_(k));
      nav.appendChild(b);
    });
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

async function draw() {
  const s = SECTIONS[view];
  if (s.kind === "chat") return;
  if (s.kind === "frame") return void (await showFrame(s.route, s.panels));
  const box = $("pageBody");
  switch (s.kind) {
    case "tools": return drawToolsSection(box, s.pick);
    case "pipes": return drawPipes(box);
    case "findings": return await drawFindings(box);
    case "results": return await drawResults(box);
    case "board": return await drawBoard(box);
    case "ledger": return await drawLedger(box);
    case "guard": return await drawGuard(box);
    case "servers": return await drawServers(box);
    case "usage": return await drawUsage(box);
    case "settings": return drawSettings(box);
    case "chats": return drawChats(box);
  }
}

async function open_(k) {
  const s = SECTIONS[k];
  if (!s) return;
  view = k;
  document.body.setAttribute("data-view", k);
  if (!openTabs.includes(k)) openTabs.push(k);
  drawTabs();
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.v === k));
  $("v-chat").hidden = s.kind !== "chat";
  $("v-page").hidden = s.kind === "chat" || s.kind === "frame";
  $("v-frame").hidden = s.kind !== "frame";
  if (s.kind === "frame") $("frameTitle").textContent = s.label;
  if (s.kind !== "chat" && s.kind !== "frame") {
    $("pageTitle").textContent = s.label;
    $("pageLead").textContent = s.lead || "";
    empty($("pageBody"), "reading…");
  }
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
    TOOLS.filter((t) => t.name.includes(needle.replace(/\s+/g, "_")) || t.path.includes(needle))
      .slice(0, 12).forEach((t) => out.push({
        icon: "tools", label: t.name, note: t.method + " " + t.path,
        go: () => { open_("tools").then(() => toast("in MCP Tools: " + t.name)); },
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

  $("frameReload").addEventListener("click", () => {
    const s = SECTIONS[view];
    $("bioql").dataset.want = "";
    showFrame(s.route, s.panels);
  });
  $("waiting-retry").addEventListener("click", () => {
    const s = SECTIONS[view];
    showFrame(s.route, s.panels);
  });

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
  setInterval(async () => {
    await drawState();
    // The engine line is asked again every tick. Read once at boot it went STALE: the backend could die and the
    // status bar would still read "healthy" while every call in BioQL's own window answered 502 — a status line
    // that stops being asked is a claim, not a reading.
    await drawEngine();
    if (SECTIONS[view].kind === "frame") await showFrame(SECTIONS[view].route, SECTIONS[view].panels);
    else if (["findings", "ledger", "results", "usage", "board"].includes(SECTIONS[view].kind)) await draw();
  }, 6000);
}

boot();
