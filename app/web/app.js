// BioQL Studio's window. No framework, no CDN, nothing inline.
//
// The token arrives in the URL FRAGMENT and is cleared from the address bar at once. A fragment is never sent to a
// server, never written to a server log and never put in a Referer — unlike Agtr's two routes to the same secret:
// it substitutes the token into every .html it serves (service.py:1255) and also accepts it as the query parameter
// `?t=` (service.py:1228). Studio's service does neither.
//
// The instrument shows one thing at rest — the orb and what the engine said — and opens a drawer when asked. The
// ring is the state, so a glance is enough: dim at rest, quantum while working, amber while a person is needed,
// red when the verifier contradicted something or the engine is down.
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

// ------------------------------------------------------------------ the state line
async function drawState() {
  const { code, body } = await api("/api/state");
  if (code === 403) {
    $("state").textContent = "no token — open this window from the app, or with the URL `python3 -m app token` prints";
    ring("down");
    return null;
  }
  if (code !== 200) {
    $("state").textContent = "the service did not answer: " + (body.error || code);
    ring("down");
    return null;
  }
  const waiting = (body.pending || []).length;
  $("state").textContent =
    `${body.version} · 127.0.0.1:${body.port} · up ${Math.round(body.up_seconds)}s · ` +
    `${body.calls} call(s) · ${body.refusals} refused · ${body.approvals} approved` +
    (waiting ? ` · ${waiting} waiting for a person` : "");
  $("footer").textContent = `data in ${body.home} · Studio never writes inside BioQL's folders`;
  if (body.bioql_ui) uiUrl = body.bioql_ui;
  drawPending(body.pending || [], body.runs || []);
  if (waiting) ring("waiting");
  return body;
}

// ------------------------------------------------------------------ the engine, and BioQL's own window
async function drawEngine() {
  ring("busy");
  const { code, body } = await api("/api/call", { name: "backend_health", args: {} });
  const line = $("engine");
  clear(line);
  if (code === 403 || body.state !== "RESOLVED") {
    line.appendChild(chip(body.state || "no answer", "contradicted"));
    line.appendChild(txt(" " + (body.detail || body.error || "the engine did not answer")));
    ring("down");
    return;
  }
  const v = body.value || {};
  line.appendChild(chip(v.status || "answered", v.status === "healthy" ? "clean" : "concerns"));
  line.appendChild(txt(v.service || "BioQL"));
  ring("idle");
}

// BioQL's own window fills the stage, whole and untouched: the chat, the sidebar, the health panel, the budget,
// every tab exactly as they are. Studio adds the frame around it and never reaches inside — the iframe is another
// origin, so it could not even if it wanted to.
let uiUrl = "http://127.0.0.1:3000";

async function showBioQL() {
  const frame = $("bioql"), room = $("waiting-room");
  // `connect-src 'self'` forbids this page from fetching another origin, so "is it up?" is asked of Studio's own
  // service, which has no such limit. A page that cannot ask does not guess.
  const { body } = await api("/api/ui");
  const up = body && body.up === true;
  if (up) {
    if (frame.src !== uiUrl + "/") frame.src = uiUrl + "/";
    frame.classList.remove("hidden");
    room.classList.add("hidden");
  } else {
    frame.classList.add("hidden");
    room.classList.remove("hidden");
    $("waiting-why").textContent = "BioQL's window is not up at " + uiUrl;
    ring("down");
  }
  return up;
}

// ------------------------------------------------------------------ findings
async function drawFindings() {
  const box = $("panel-findings");
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).filter((r) => r.verdict || (r.findings || []).length).reverse();
  $("count-findings").textContent = rows.length ? String(rows.length) : "";
  const bad = rows.filter((r) => r.verdict === "contradicted").length;
  $("count-findings").className = "lens-count" + (bad ? " hot" : "");
  if (bad) ring("alarm");
  if (!rows.length) return empty(box, "nothing verified yet. A verdict is recorded on every call that resolves.");
  clear(box);
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
      const p = el("p", null, f.detail || "");
      fd.appendChild(p);
      if (f.path) fd.appendChild(el("p", "where", f.path));
      row.appendChild(fd);
    });
    if (!(r.findings || []).length) {
      row.appendChild(el("p", "row-why", r.verdict === "clean" ? "nothing contradicted" : (r.detail || "")));
    }
    box.appendChild(row);
  });
}

// ------------------------------------------------------------------ the board
const LANES = ["target", "mechanism", "candidate", "risk", "plan", "evidence"];
let boardId = null;

async function drawBoard() {
  const box = $("panel-board");
  const { code, body } = await api("/api/boards");
  if (code !== 200) return empty(box, body.error || "the boards did not answer");
  const list = body.boards || [];
  $("count-board").textContent = list.length ? String(list.length) : "";
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
    pick.addEventListener("click", () => { boardId = b.id; drawBoard(); });
    bar.appendChild(pick);
  });
  box.appendChild(bar);

  const one = await api("/api/board?id=" + encodeURIComponent(boardId));
  if (one.code !== 200) {
    // ABSENT and UNRESOLVABLE read differently: a board that cannot be read is never drawn as an empty one.
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

// ------------------------------------------------------------------ the notebook
async function drawLedger() {
  const box = $("panel-ledger");
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) return empty(box, body.error || "the record did not answer");
  const rows = (body.rows || []).slice().reverse();
  $("count-ledger").textContent = rows.length ? String(rows.length) : "";
  const chain = body.chain || {};
  $("drawer-note").textContent = chain.ok
    ? `chain intact over ${chain.lines} line(s)`
    : `the chain is broken: ${chain.why || "unknown"}`;
  if (!rows.length) return empty(box, "nothing recorded yet.");
  clear(box);
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
    box.appendChild(row);
  });
}

// ------------------------------------------------------------------ tools
async function drawTools() {
  const box = $("panel-tools");
  const { code, body } = await api("/api/tools");
  if (code !== 200) return empty(box, body.error || "the list did not answer");
  const tools = body.tools || [];
  $("count-tools").textContent = String(tools.length);
  clear(box);
  tools.forEach((t) => {
    const row = el("div", "tool");
    const head = el("div", "row-head");
    head.appendChild(el("span", "tool-name", t.name));
    head.appendChild(chip(t.kind, "plain"));
    if (t.approval) head.appendChild(chip("needs a person", "concerns"));
    row.appendChild(head);

    const needs = (t.required || []).filter((k) => k !== "job_id" || true);
    const box2 = el("div", "row-head");
    const inputs = {};
    needs.forEach((k) => {
      const i = el("input", "field-in");
      i.placeholder = k;
      i.setAttribute("aria-label", k);
      inputs[k] = i;
      box2.appendChild(i);
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
      said.appendChild(chip(out.body.state || out.code, (out.body.state || "").toLowerCase() || "plain"));
      said.appendChild(txt(" " + (out.body.detail || out.body.error || "")));
      row.appendChild(said);
      await drawState();
      await drawFindings();
    });
    box2.appendChild(go);
    row.appendChild(box2);
    if (t.why) row.appendChild(el("p", "tool-why", t.why));
    box.appendChild(row);
  });
}

// ------------------------------------------------------------------ the guard
async function drawGuard() {
  const box = $("panel-guard");
  const { code, body } = await api("/api/guard");
  if (code !== 200) return empty(box, body.error || "the guard did not answer");
  if (body.state && body.state !== "RESOLVED") {
    return empty(box, (body.detail || "") + "  Take one with: python3 -m studio guard snapshot <name>");
  }
  const rep = body.report || body;
  clear(box);
  box.appendChild(el("p", "fine", rep.clean
    ? "BioQL is as it was between the two most recent snapshots."
    : "BioQL changed between the two most recent snapshots. The guard names the files; it never says who changed them."));
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
    box.appendChild(row);
  });
  $("count-guard").textContent = rep.clean ? "" : "!";
  $("count-guard").className = "lens-count" + (rep.clean ? "" : " hot");
}

// ------------------------------------------------------------------ waiting for a person
function drawPending(pending, runs) {
  const box = $("pending");
  const wrap = $("pending-box");
  clear(box);
  if (!pending.length) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
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
      const said = el("p", "row-why");
      said.appendChild(txt(body.detail || body.error || body.state || "sent"));
      row.appendChild(said);
      ring("idle");
      await drawState();
      await drawFindings();
    });
    act.appendChild(input);
    act.appendChild(go);
    row.appendChild(act);
    box.appendChild(row);
  });
}


// ------------------------------------------------------------------ the chat: Claude Code, and the cards
const SEEDS = ["Is the engine up and what did it actually run?",
               "ADMET on paracetamol, and tell me if the label matches the execution",
               "What tools do you have for screening?",
               "Show me the findings the verifier recorded today"];

function el2(tag, cls, text) { return el(tag, cls, text); }

function cardNode(card) {
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
    } else if (b.type === "molecule2d" && b.png_base64) {
      const img = el("img", "shot");
      img.src = "data:image/png;base64," + b.png_base64;
      img.alt = b.smiles || "molecule";
      body.appendChild(img);
    } else if (b.type === "molecule3d" && b.sdf) {
      body.appendChild(el("pre", "sdf", String(b.sdf).split("\n").slice(0, 8).join("\n") + "\n…"));
    } else if (b.type === "absent") {
      body.appendChild(el("p", "absent-block", (b.what || "this") + " — " + (b.why || "not computed")));
    } else if (b.type === "links") {
      const d = el("div", "row-head");
      // The card already refuses a bad scheme, and so does this: a link is an anchor, and `javascript:` or a
      // protocol-relative `//host` in one is a script or another origin, not a download. Two barriers, because
      // the payload comes from the engine and the window is where it becomes clickable.
      (b.items || []).forEach((i) => {
        const u = String(i.url || "");
        const ok = /^https?:\/\//i.test(u) || (/^\/(?!\/)/.test(u) && !/[\\\s]/.test(u));
        if (!ok) { d.appendChild(el("span", "hash", i.label + " \u2014 link refused: " + u.slice(0, 40))); return; }
        const a = el("a", "hash", i.label);
        a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer";
        d.appendChild(a);
      });
      body.appendChild(d);
    } else if (b.text) {
      body.appendChild(el("p", "beat-say", b.text));
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

let asking = false;

function beat(host, e) {
  if (e.type === "say") host.appendChild(el("div", "beat beat-say", e.text));
  else if (e.type === "tool") {
    const d = el("div", "beat beat-tool");
    d.appendChild(txt(String(e.name || "").replace(/^mcp__bioql__/, "")));
    d.appendChild(el("span", "args", JSON.stringify(e.args || {}).slice(0, 90)));
    host.appendChild(d);
  } else if (e.type === "tool_result" && e.card) host.appendChild(cardNode(e.card));
  else if (e.type === "fail") host.appendChild(el("div", "beat beat-fail", e.text));
  else if (e.type === "raw") host.appendChild(el("div", "beat fine", e.text));
}

async function send(text) {
  if (asking || !text.trim()) return;
  asking = true;
  $("ask-go").disabled = true;
  ring("busy");
  const opening = $("opening");
  if (opening) opening.remove();
  const scroll = $("scroll");

  const turn = el("div", "turn");
  turn.appendChild(el("div", "said", text));
  const host = el("div", "beats");
  const wait = el("div", "thinking");
  wait.appendChild(el("i")); wait.appendChild(el("i")); wait.appendChild(el("i"));
  wait.appendChild(txt("working the engine"));
  host.appendChild(wait);
  turn.appendChild(host);
  scroll.appendChild(turn);
  scroll.scrollTop = scroll.scrollHeight;

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
    (body.events || []).forEach((e) => { wait.remove(); beat(host, e); });
    since = body.next !== undefined ? body.next : since;
    done = body.state === "done";
    scroll.scrollTop = scroll.scrollHeight;
  }
  wait.remove();
  asking = false;
  $("ask-go").disabled = false;
  ring("idle");
  await drawState();
  await drawFindings();
}

// ------------------------------------------------------------------ the drawer
const LENSES = {
  findings: { title: "Findings", note: "Every verdict the verifier recorded, newest first. A row whose line recorded no verdict says not recorded — it is never read as clean.", draw: drawFindings },
  board: { title: "Board", note: "The plan drawn before anything is spent, and the evidence hung on it afterwards.", draw: drawBoard },
  ledger: { title: "Notebook", note: "", draw: drawLedger },
  tools: { title: "Tools", note: "The closed list Studio may call. An entry marked needs a person is refused from this window and offers an approval instead; anything that could reach real quantum hardware is not offered here at all.", draw: drawTools },
  bioql: { title: "BioQL", note: "BioQL\u2019s own window, whole and untouched. Studio never starts, stops or restarts it.", draw: drawBioQLLens },
  guard: { title: "Guard", note: "The two most recent saved snapshots of BioQL's source trees, compared. Opening this takes no new snapshot.", draw: drawGuard },
};

async function drawBioQLLens() { await showBioQL(); }

let openLens = null;

function closeDrawer() {
  openLens = null;
  $("bioql-wrap").classList.add("hidden");
  $("talk").classList.remove("hidden");
  document.body.classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
  document.querySelectorAll(".lens").forEach((b) => b.setAttribute("aria-selected", "false"));
}

async function openDrawer(name) {
  const lens = LENSES[name];
  if (!lens) return;
  if (openLens === name) return closeDrawer();
  openLens = name;
  document.body.classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
  $("drawer-title").textContent = lens.title;
  $("drawer-note").textContent = lens.note;
  document.querySelectorAll(".lens").forEach((b) =>
    b.setAttribute("aria-selected", b.dataset.lens === name ? "true" : "false"));
  Object.keys(LENSES).forEach((k) => { const n = $("panel-" + k); if (n) n.classList.toggle("hidden", k !== name); });
  // The BioQL lens is not a drawer panel: it is BioQL's own window, shown in place of the chat.
  $("bioql-wrap").classList.toggle("hidden", name !== "bioql");
  $("talk").classList.toggle("hidden", name === "bioql");
  if (name === "bioql") { document.body.classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); }
  await lens.draw();
}

// ------------------------------------------------------------------ start
async function boot() {
  document.querySelectorAll(".lens").forEach((b) =>
    b.addEventListener("click", () => openDrawer(b.dataset.lens)));
  $("drawer-close").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openLens) closeDrawer(); });

  $("waiting-retry").addEventListener("click", showBioQL);
  $("ask").addEventListener("submit", (e) => { e.preventDefault(); const v = $("ask-in").value; $("ask-in").value = ""; send(v); });
  SEEDS.forEach((q) => {
    const b = el("button", "seed", q);
    b.type = "button";
    b.addEventListener("click", () => send(q));
    $("seeds").appendChild(b);
  });

  const st = await drawState();
  if (st) {
    await drawEngine();
    await drawFindings();   // counts the findings and lights the ring, without opening anything
    await drawBoard();
    await drawGuard();
    const { body } = await api("/api/tools");
    $("count-tools").textContent = String((body.tools || []).length);
  }
  setInterval(async () => {
    await drawState();
    if (openLens) await LENSES[openLens].draw();
    if (openLens === "bioql") await showBioQL();   // it comes back on its own when you start it
  }, 5000);
}

boot();
