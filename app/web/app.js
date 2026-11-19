// BioQL Studio's window. No framework, no CDN, nothing inline.
//
// The token arrives in the URL FRAGMENT and is cleared from the address bar at once. A fragment is never sent to a
// server, never written to a server log and never put in a Referer — unlike Agtr's two routes to the same secret:
// it substitutes the token into every .html it serves (service.py:1255) and also accepts it as the query parameter
// `?t=` (service.py:1228). Studio's service does neither.
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

// ------------------------------------------------------------------ the state line
async function drawState() {
  const { code, body } = await api("/api/state");
  if (code === 403) {
    $("state").textContent = "no token: open the window from the app, or with the URL that `python3 -m app token` prints";
    return null;
  }
  if (code !== 200) {
    $("state").textContent = "the service did not answer: " + (body.error || code);
    return null;
  }
  $("state").textContent =
    `${body.name} ${body.version} · 127.0.0.1:${body.port} · up ${Math.round(body.up_seconds)}s · ` +
    `BioQL at ${body.bioql} · ${body.calls} call(s), ${body.refusals} refused, ${body.approvals} approved · ` +
    `${(body.pending || []).length} waiting for a person`;
  $("footer").textContent = `data in ${body.home}. Studio never writes inside BioQL's folders.`;
  drawPending(body.pending || [], body.runs || []);
  return body;
}

// ------------------------------------------------------------------ the engine
async function drawEngine() {
  const { code, body } = await api("/api/call", { name: "backend_health", args: {} });
  const line = $("engine");
  line.textContent = "";
  if (code === 403) { line.appendChild(el("span", "pill warn", "refused")); line.appendChild(txt(" " + (body.detail || ""))); return; }
  if (body.state !== "RESOLVED") {
    line.appendChild(el("span", "pill high", body.state || "no answer"));
    line.appendChild(txt(" " + (body.detail || body.error || "")));
    return;
  }
  const v = body.value || {};
  line.appendChild(el("span", "pill clean", v.status || "answered"));
  line.appendChild(txt(" " + (v.service || v.message || "BioQL's backend answered") + " · " + (body.detail || "")));
}

// ------------------------------------------------------------------ findings (the default view)
function verdictPill(row) {
  if (!row.verdict_recorded) return el("span", "pill unknown", "not recorded");
  if (row.verdict === "contradicted") return el("span", "pill high", "contradicted");
  if (row.verdict === "concerns") return el("span", "pill warn", "concerns");
  if (row.verdict === "clean") return el("span", "pill clean", "clean");
  return el("span", "pill unknown", "not recorded");
}

function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleString();
}

function drawFindings(rows) {
  const box = $("findings");
  box.textContent = "";
  const seen = rows.filter((r) => r.verdict_recorded || (r.findings && r.findings.length)).reverse();
  if (!seen.length) {
    box.appendChild(el("p", "muted", "No verdict has been recorded yet. Make a call and it appears here."));
    return;
  }
  for (const r of seen) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", r.tool || "—"));
    row.appendChild(verdictPill(r));
    row.appendChild(el("span", "pill " + (r.state === "RESOLVED" ? "clean" : "warn"), r.state || "?"));
    const why = el("span", "why");
    for (const f of r.findings || []) {
      const d = el("div", "finding");
      d.appendChild(el("span", "pill " + (f.severity || "note"), f.severity || "?"));
      d.appendChild(txt(" " + (f.code || "")));
      d.appendChild(el("div", "path", f.path || ""));
      why.appendChild(d);
    }
    if (r.findings_total > (r.findings || []).length) {
      why.appendChild(el("div", "muted", `${r.findings_total} findings in all; the first ${(r.findings || []).length} are on the record.`));
    }
    if (!(r.findings || []).length && r.verdict_recorded) why.appendChild(el("span", "muted", "nothing contradicted"));
    row.appendChild(why);
    row.appendChild(el("span", "t", when(r.t)));
    box.appendChild(row);
  }
}

// ------------------------------------------------------------------ the ledger
async function drawLedger() {
  const { code, body } = await api("/api/ledger?limit=100");
  if (code !== 200) { $("ledger").textContent = body.error || String(code); return []; }
  const chain = body.chain || {};
  $("chain").textContent = chain.ok
    ? `chain intact over ${chain.lines} line(s) · ${body.file}`
    : `CHAIN BROKEN at line ${chain.broken_at}: ${chain.why} · ${body.file}`;
  const box = $("ledger");
  box.textContent = "";
  for (const r of (body.rows || []).slice().reverse()) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", r.tool || r.window_event || "—"));
    row.appendChild(el("span", "pill " + (r.state === "RESOLVED" ? "clean" : r.state === "REFUSED" ? "high" : "warn"), r.state || "?"));
    row.appendChild(verdictPill(r));
    const why = el("span", "why");
    why.appendChild(txt(`${r.by || "?"}${r.approved_by ? " · approved_by " + r.approved_by : ""}${r.detail ? " · " + r.detail : ""}`));
    if (r.result_sha256) why.appendChild(el("div", "path", "sha256 " + r.result_sha256));
    const p = r.provenance;
    if (p && (p.is_real_hardware !== null || (p.ibm_job_ids || []).length)) {
      why.appendChild(el("div", "path",
        `is_real_hardware ${JSON.stringify(p.is_real_hardware)} · jobs ${(p.ibm_job_ids || []).join(", ") || "none"}`));
    }
    row.appendChild(why);
    row.appendChild(el("span", "t", when(r.t)));
    box.appendChild(row);
  }
  return body.rows || [];
}

// ------------------------------------------------------------------ tools
async function drawTools() {
  const { code, body } = await api("/api/tools");
  const box = $("tools");
  box.textContent = "";
  if (code !== 200) { box.textContent = body.error || String(code); return; }
  for (const t of body.tools || []) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", t.name));
    row.appendChild(el("span", "pill " + (t.kind === "status" || t.kind === "local" ? "clean" : "warn"), t.kind));
    if (t.approval || t.kind === "hardware") row.appendChild(el("span", "pill high", "needs a person"));
    row.appendChild(el("span", "why", `${t.method} ${t.path} — ${t.why || ""}`));
    if (!t.approval && t.kind !== "hardware") {
      const b = el("button", null, "Call");
      b.addEventListener("click", () => call(t));
      row.appendChild(b);
    }
    box.appendChild(row);
  }
}

async function call(t) {
  const args = {};
  for (const k of t.required || []) {
    const v = prompt(`${t.name}: ${k}`);
    if (v === null) return;
    args[k] = v;
  }
  const { body } = await api("/api/call", { name: t.name, args });
  alert(`${body.state || body.error}\n${body.detail || ""}${body.verdict ? "\nverdict: " + body.verdict : ""}`);
  refresh();
}

// ------------------------------------------------------------------ guard
async function drawGuard() {
  const { code, body } = await api("/api/guard");
  const box = $("guard");
  box.textContent = "";
  if (code !== 200) { box.textContent = body.error || String(code); return; }
  const head = el("p");
  head.appendChild(el("span", "pill " + (body.state === "RESOLVED" ? "clean" : body.state === "ABSENT" ? "unknown" : "high"), body.state));
  head.appendChild(txt(" " + (body.detail || "")));
  box.appendChild(head);
  const v = body.value;
  if (!v) return;
  box.appendChild(el("p", v.clean ? "muted" : null, v.clean ? "BioQL is unchanged between those two snapshots."
    : "BioQL changed between those two snapshots. The guard sees THAT a file changed, not who changed it."));
  for (const [name, r] of Object.entries(v.roots || {})) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", name));
    row.appendChild(el("span", "pill " + (r.clean ? "clean" : "high"), r.clean ? "unchanged" : "changed"));
    row.appendChild(el("span", "why",
      `${r.files} file(s) watched · +${(r.added || []).length} added, -${(r.removed || []).length} removed, ` +
      `~${(r.changed || []).length} changed` +
      ((r.changed || []).length ? " · " + r.changed.slice(0, 6).join(", ") : "")));
    box.appendChild(row);
  }
}

// ------------------------------------------------------------------ pending approvals
function drawPending(pending, runs) {
  const box = $("pending");
  box.textContent = "";
  $("pending-box").classList.toggle("hidden", !pending.length && !runs.length);
  for (const p of pending) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", p.tool));
    row.appendChild(el("span", "why", `${p.why} · id ${p.id} · expires in ${Math.round(p.expires_in)}s`));
    const input = el("input");
    input.placeholder = "code from the log";
    input.setAttribute("aria-label", `approval code for ${p.tool}`);
    const b = el("button", null, "Approve");
    b.addEventListener("click", async () => {
      const { body } = await api("/api/approve", { id: p.id, code: input.value.trim() });
      alert(`${body.state}\n${body.detail || body.note || ""}`);
      refresh();
    });
    row.appendChild(input);
    row.appendChild(b);
    box.appendChild(row);
  }
  for (const r of runs.slice().reverse()) {
    const row = el("div", "row");
    row.appendChild(el("span", "name", r.tool));
    row.appendChild(el("span", "pill " + (r.state === "RESOLVED" ? "clean" : r.state === "RUNNING" ? "note" : "high"), r.state));
    if (r.verdict) row.appendChild(el("span", "pill " + (r.verdict === "contradicted" ? "high" : r.verdict === "concerns" ? "warn" : "clean"), r.verdict));
    row.appendChild(el("span", "why", `${r.detail || ""}${r.seconds ? " · " + r.seconds + "s" : ""}`));
    box.appendChild(row);
  }
}

// ------------------------------------------------------------------ tabs
const PANELS = ["findings", "ledger", "tools", "guard"];
for (const name of PANELS) {
  $("tab-" + name).addEventListener("click", () => {
    for (const other of PANELS) {
      $("tab-" + other).classList.toggle("on", other === name);
      $("tab-" + other).setAttribute("aria-selected", other === name ? "true" : "false");
      $("panel-" + other).classList.toggle("hidden", other !== name);
    }
  });
}

// ------------------------------------------------------------------ go
let busy = false;
async function refresh() {
  if (busy) return;
  busy = true;
  try {
    await drawState();
    const rows = await drawLedger();
    drawFindings(rows);
    await drawTools();
    await drawGuard();
  } finally {
    busy = false;
  }
}

refresh().then(drawEngine);
setInterval(() => { refresh(); }, 15000);

// The native shell talks to the page through window.bioqlStudio, and only through it.
window.bioqlStudio = { refresh };
