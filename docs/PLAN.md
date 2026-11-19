# BioQL Studio — the five phases

Written 2026-11-19, after reading all of Agtr (~99,000 lines) and probing BioQL's live backend read-only.
Effort figures are estimates for one person reusing Agtr's code, not measurements.

## What changed from the design document, and why

The design is sound and is being built as written. Four corrections come from what the code actually shows.

**1. The adapter calls BioQL over HTTP. It does not import it.**
The design left the mechanism open ("command line, Python functions or an API"). Importing is not an option here: the
backend is a 35,000-line Flask monolith, `/opt/bioql` shadows sibling modules, and importing it in another process
re-executes it and dies on signal handling (recorded in CLAUDE.md and in memory). BioQL already serves ~249 HTTP
routes on `127.0.0.1:5035`, and its own UI uses them — so Studio uses the same door, which is also the only door that
cannot change BioQL by accident.

**2. The entry list comes from BioQL itself, not from guesses.**
`/api/tools/registry` returns 63 pipelines with their typed schemas (required and optional parameters, cost tier,
whether confirmation is required). `python3 -m studio registry` prints them and marks each as *in allowlist*,
*allowable* or *not http*; `python3 -m studio allow NAME` copies one entry's schema verbatim. So the list is BioQL's
own, entry by entry, and the owner approves each one. Of the 63: 26 are HTTP entries with an endpoint, 37 are
dispatched by kind rather than by route (those need a route question answered before they can be allowed).

**3. "The agents never read BioQL because of Seatbelt" does not hold yet.**
The design rests this guarantee on Agtr's sandbox. Reading the profile (`agtrdesktop/guard.py:47-72`): it is
`(allow default)` — everything is readable except a fixed secrets list and the owner's protected folders — and it
allows outbound traffic to localhost even with the network off. The protected-folder exemption is also a substring
match (`dev.py:2194`), so a protected `/a/proj` is not closed for a project at `/a/proj2`. Three repairs must land in
the fork **before any agent runs** (they block Phase 3, not Phase 1):

- close Studio's own home folder (its tokens) and the adapter's port to agents;
- require a token on the adapter the moment it listens on a port;
- match protected paths by separator, and drop agent write access to `~/.claude*`, `~/.cargo` and `~/go` (which
  execute later outside the sandbox).

BioQL's folders are already in the owner's protected list (`protected.json`: `Spectrix_BioQL`, `bionics`,
`/opt/bioql`, `~/.bioql`, and two more), which is the right foundation — the repairs are what make it hold.

**4. The fork must change identity before it is first launched.**
Agtr's Swift wrapper reuses whatever already answers `agtr-desktop` on port 7790 and hard-codes its home path
(`Service.swift:7-8,28-42`). Launched unchanged, BioQL Studio would attach to the running Agtr Desktop and read its
token. Port, ping name, home folder, bundle id, log path and LaunchAgent label all change in the first commit of the
fork. Studio also never binds 3000 or 5035 and installs no `com.bioql.*` agent: it attaches to BioQL, it does not own
it.

## Phase 1 — adapter, contract, ledger · **done 2026-11-19**

Delivered: `studio/` (resolution, allowlist, adapter, guard, ledger, MCP server, CLI), 21 unit tests, and a live
end-to-end check that passed 9/9 against the running backend with the guard clean over 46,819 BioQL files.

Verified rather than asserted:
- the guards fail when they should — a deliberate `import external_fact` plus an `/opt/bioql` path made both contract
  tests fail; removing it returned the suite to green;
- a tampered ledger line is detected;
- a failed job comes back UNRESOLVABLE carrying BioQL's own cause, never as an empty result;
- `pipeline_job` on a job that does not exist is ABSENT (BioQL answered, it has no such job), while an unreachable
  backend is UNRESOLVABLE;
- an argument the entry does not declare is refused, and a route on the never-callable list stays refused even when
  written into the owner's own allowlist file.

Two claims were corrected during the phase, because a first version of each would have passed its own test while
being wrong about BioQL:

- **Hardware could not be recognised from the registry.** The first `from_registry` classed an entry as `hardware`
  when its `cost_tier` said "qpu" or "hardware" — and BioQL's tiers only ever say `high` (47) or `medium` (16). The
  rule would never have fired, and its test passed only because the fake registry used a tier BioQL does not emit.
  Now no entry is auto-classed hardware and **every** `compute` entry asks for a person; the owner relaxes one by
  hand. The real parameter is `real_qpu` (BioQL's own name), which the first hardware-name list also missed.
- **The job and streaming shapes were guesses.** They are now taken from BioQL's own UI
  (`src/frontend/src/components/chat/ChatInterface.tsx`): submit returns `{job_id, poll_url}`, polling
  `/api/pipeline/job/<id>` returns `{status, progress, result, result_url, error}` with `completed`, `failed` and
  `error`, and the body can carry a BOM and NUL bytes that must be stripped before parsing — Studio now strips them
  as the UI does. A state Studio does not recognise is UNRESOLVABLE, never a result.

Still unproven, and it blocks allowing the first heavy pipeline rather than Phase 1: no job has actually been
submitted. The shapes are read from the UI's code, not observed from a run. The first allowed pipeline is also the
first real test of them.

### The route question, answered from BioQL itself (not asked of the owner)

`tools/discover_routes.py` finds every pipeline's real route from four sources and then **proves each route exists
without running anything**: a GET on a POST-only Flask route answers `405` with an `Allow` header, so the route is
confirmed live while no pipeline body is ever sent.

| Source | What it gives | Authority |
|---|---|---|
| `/api/tools/registry` | 26 pipelines state their own endpoint | BioQL's own |
| `MCPClient.ts` + `VSIXService.ts` | 94 name → route pairs the UI uses | BioQL's own |
| `src/mcp/server.py` | 70 routes BioQL's MCP server posts to | BioQL's own |
| derivation | the nested family form, e.g. `quantum_ppi_binding_affinity` → `/api/quantum/ppi/binding_affinity` | inferred, marked |

**Result: 61 of 63 pipelines routed and verified live.** The two left are `replicon_workbench` and
`viral_replicon_design`: both registered with handlers, neither answering on any prefix tried. They stay out rather
than be guessed at.

18 of the 61 routes are *derived*: the route is proven to exist, but the binding of that name to that route is
inferred rather than stated by BioQL. Each entry records `route_confirmed_by_bioql: false`, and since every one needs
a person's approval, a wrong binding cannot fire unattended.

**A derivation rule had to be thrown away.** A first version also matched a route whose *last word* equalled the
pipeline's last word, and it sent `viral_replicon_design` to `/api/quantum/combination/design`. A wrong route is
worse than no route: it would run the wrong science under the right name. The rule now matches whole path segments
only — the same trap CLAUDE.md records under "match field names by token, not substring".

### How a heavy pipeline is called: BioQL's own async door

Every one of the 61 goes through `POST /api/pipeline/submit {endpoint, params}` and is then followed on
`/api/pipeline/job/<id>` — the path BioQL's own UI uses for long pipelines, because a held connection drops on a
proxy timeout and the result is lost. The `endpoint` sent is **the allowlist entry's own path**; a caller naming an
`endpoint` argument is refused. Tested both ways.

### One more gap the tests found

A hand-written `compute` entry with no `approval` field was callable without a person — the README claimed otherwise.
`approval` is now forced for every `compute` and `hardware` entry and cannot be turned off in the file: to make one
cheap, the owner changes its **kind** to `local`, which is a statement about that entry rather than a forgotten
field.

### Claude Code driving BioQL: proven, both ways · 2026-11-19

The design's central claim is that Claude can orchestrate BioQL without being able to touch it. Tested with the real
binary (`claude` 2.1.328, the owner's own account), `--mcp-config mcp.bioql.json --strict-mcp-config`, tools limited
to `mcp__bioql__*` and Bash/Edit/Write/Read disallowed:

- **It works.** Asked whether the engine answers and how many pipelines run, Claude called `backend_health` and
  `live_status` and reported: healthy, `BioQL Script Executor v2`, 0 active, last sessions `ewf_sqd` from
  2026-11-18 — which is exactly what the backend returned directly. Nothing invented.
- **The brake holds.** Asked to run ADMET on paracetamol, it was refused and said so, quoting the refusal verbatim
  and adding that it did not try to go around it with another tool or a direct call to the backend.
- **Both calls are on the record.** The ledger shows them as `by: claude`, two RESOLVED and one REFUSED, chain intact.

One operational note: the `claude` CLI warned that this sign-in can end at any time up to **2026-11-21**. Phase 2
onwards runs unattended work, so it needs `claude setup-token` (the same thing Agtr's notes say for the A11).

## Phase 2 — the app, Claude behind it, the signed notebook · 2 weeks

A fork of Agtr Desktop as `BioQL Studio.app`: its own service, its own window, Iris as the way to ask, and Claude Code
behind it reaching BioQL only through the adapter's MCP server. The ledger gains Ed25519 signatures (Agtr's
`brakes/log.py` chain), so each run carries its parameters, BioQL's job id, whether it was hardware or a simulator,
and the hash of the result.

Starts with: the identity changes above; `claude --mcp-config` pointed at `python3 -m studio mcp`; the owner's Claude
account, as in Agtr.

## Phase 3 — campaigns, verification, claim audit · 2 weeks

Agtr's agent engine, with the unit of work changed from a code task to a campaign: criteria, parallel BioQL runs each
in its own results folder, a verifier that checks the run was the kind asked for (hardware vs simulator, a job id
present when hardware was claimed), and an independent claim auditor. Blocked on the three sandbox repairs.

A specific guard this phase must carry, from BioQL's own history: a quantum result is not reported against a weak
classical arm, and `dim/dim_FCI` is published whenever determinant selection is involved (CLAUDE.md, 2026-10-07).

## Phase 4 — the scientific board · 1.5 weeks

Agtr's whiteboard canvas with scientific node types (target, mechanism, candidate, evidence, risk, plan). Each
evidence node links to its ledger line, so a conclusion reaches the raw data and the IBM job id in two clicks.

## Phase 5 — rails, evals, polish · 1.5 weeks

Each rejection that repeats becomes a hard rule that blocks the report. Evals are campaigns with known answers, run
after every change to the interface or the orchestrator — the UI and the orchestration are what is evaluated, never
the science.

## Standing constraints

- BioQL runs without Claude. Claude is the copilot; if the biology safeguards block again, the editor, the command
  line and recipes still reach BioQL.
- The owner's own Claude account, one login per person, as in Agtr. Selling it outside Molevera would need API keys
  instead.
- Studio never starts, stops or restarts BioQL's services, and never clears its `.pyc` files.
- IBM Quantum has ~153 s of hardware time left and Modal is suspended for an unpaid invoice: no phase spends either
  without the owner saying so, and the adapter refuses hardware without a person's approval by construction.
- `~/Documents` is iCloud-managed. Studio lives in `~/Developer/bioql-studio` and its data in
  `~/Library/Application Support/BioQL Studio`, so no launchd job has to read an iCloud path at login.

## Known limits of the guard, to close before the phase that needs them

- It skips folders named `runtime`, so it does not watch `~/.bioql/runtime/frontend-v2` (the copy actually served on
  :3000) or `~/.bioql/runtime/agents/launch.sh`. Those are rsynced by BioQL's own startup, so watching them needs a
  rule that separates BioQL's own sync from an outside edit. **Phase 2 touches the UI: close this first.**
- It fingerprints source extensions only. A change to a `.pdb`, `.sdf` or other data file is not seen.
- It reports the file, not the author. On its first day it caught another session's edit to the monolith (see
  `BASELINE-2026-11-19.txt`), which is the behaviour, not a fault.
