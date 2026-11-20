# BioQL Studio

A layer **around** BioQL. It never changes BioQL.

BioQL — the algorithms, the pipelines, the IBM Quantum / Modal / GPU connections, the scientific models — stays
exactly as it is. Studio adds the app, the interface, the orchestration, the design boards and the lab notebook
around it, and reaches BioQL only through the HTTP entries BioQL already serves.

## The no-change contract, as code rather than a promise

| Rule | Where it is enforced |
|---|---|
| Studio never imports a BioQL module | `tests/test_guard_and_contract.py::test_studio_never_imports_bioql` — an AST walk over every file of `studio/`, docstrings stripped |
| Studio never writes inside BioQL's folders | `…::test_studio_never_writes_into_bioql` — no BioQL path appears as a string in code, and `guard.py` (which names them to read them) calls no write function |
| Only entries the owner approved are reachable | `studio/allowlist.py` — a closed list; the path comes from the list, never from the caller. Routes that run code, hold credentials or destroy state are refused even if the owner's own file lists them |
| Nothing expensive runs without a person | `studio/adapter.py` — every `compute` or `hardware` entry, anything the owner marked for approval, and any argument naming real hardware (`real_qpu`, a `backend` of `ibm_*`…) is refused unless the call carries `approved_by`. The MCP server always calls with no approval, so Claude cannot supply one. BioQL's registry never states whether a pipeline reaches a QPU — its tiers only say `high` or `medium` — so Studio assumes it may and asks |
| A failure never looks like a result | `studio/resolution.py` — RESOLVED / ABSENT / UNRESOLVABLE, the third never degrading into the second (BioQL's own invariant, re-implemented, not imported) |
| Provenance is BioQL's, never inferred | `adapter.provenance` — `is_real_hardware`, IBM job ids and backends are copied when BioQL states them and stay `null` when it does not |
| A session leaves BioQL as it found it | `studio/guard.py` — a sha256 fingerprint of BioQL's source trees before and after; the session fails if anything changed |
| A result is checked, not just received | `studio/verify.py` — every answer is read for labels that contradict the work (a quantum name over classical output, a counter that disagrees with the execution mode, `is_real_hardware` with no job id, determinant selection with no `dim/dim_FCI`, an advantage claim with no classical arm). Findings travel beside the science; nothing in the answer is altered |
| Every call is on the record | `studio/ledger.py` — one hash-chained line per call, refusals and findings included |

What the guard does **not** claim: it sees *that* a file changed, not *who* changed it. BioQL's own running services, or
another person working in the repository at the same time, show up too — so it lists every file by name.

## Try it (BioQL's backend must already be running; Studio never starts it)

```sh
python3 -m pytest -q                      # 563 tests
python3 tools/e2e_live.py                 # real calls to 127.0.0.1:5035, wrapped in the guard
python3 tools/discover_routes.py          # every pipeline's route, verified live without running one
python3 -m studio tools                   # the 68 entries Studio may call
python3 -m studio call smiles_to_3d '{"smiles":"CC(=O)Nc1ccc(O)cc1"}'
python3 -m studio ledger verify
```

The 61 pipelines all need a person. The first real run is one command, and only you can give it:

```sh
python3 -m studio call admet_standalone '{"smiles":"CC(=O)Nc1ccc(O)cc1"}' --approve "Heinz"
```

Measured on this Mac, 2026-11-19: 26 unit tests pass; the live check passes 9/9 with the guard clean over 46,819
BioQL files; and 61 of BioQL's 63 registered pipelines are routed and verified live (`docs/PLAN.md` says how, and
which two are not).

**The first real run, 2026-11-19 (job `5a362e56-077`, ADMET on paracetamol, approved by the owner).** It worked, and
it found two defects in Studio and one in BioQL's output:

- BioQL answers a submit with `status: "accepted"` — a word Studio's "still running" list did not have — so Studio
  reported the **receipt as the result**. The receipt holds no science at all. Fixed: a body carrying a job id is
  followed unless its state is already final, and a receipt with no job id is UNRESOLVABLE.
- A finished job keeps its result on its own route (`/api/pipeline/job/<id>/result`, 95,988 bytes here) and leaves
  `result` out of the status body. Studio now fetches it, from a path built out of the job id.
- The verifier, on that real result: **contradicted**, four high findings. `admet_quantum` and `admet_classical`
  carry an identical LightGBM payload, and `summary` counts `quantum_success: 1, classical_fallback: 0` while the run
  reports `CLASSICAL_FALLBACK` and `quantum_backend: NONE`. BioQL is honest at the leaf
  (`quantum_engine: "classical LightGBM ensemble (no quantum hardware in this prediction)"`); it is the envelope that
  reads as quantum. Claude Code, given only that tool, reached the same conclusion unprompted.

## Claude Code behind it

```sh
claude -p "..." --mcp-config mcp.bioql.json --strict-mcp-config \
       --allowedTools "mcp__bioql__backend_health,mcp__bioql__live_status" \
       --disallowedTools "Bash,Edit,Write,Read"
```

Proven on 2026-11-19 with the real CLI and the owner's own account: Claude read the engine's live state correctly and
was refused on the ADMET pipeline, quoting the refusal and noting it did not try to route around it. Both calls are in
the ledger as `by: claude`.

## Layout

```
studio/resolution.py   the three states of an external fact
studio/allowlist.py    the closed list of BioQL entries + the never-callable routes
studio/adapter.py      the only piece that talks to BioQL (plain HTTP, no language model in it)
studio/guard.py        the no-change guard
studio/ledger.py       the hash-chained run ledger (Phase 2 signs it)
studio/mcp_server.py   the adapter as seen by Claude Code (stdio MCP)
studio/__main__.py     the command line
tools/e2e_live.py      the live check
docs/PLAN.md           the five phases and what blocks each
docs/BASELINE-*.txt    BioQL's state when this work started
```

Phase 1 is here. `docs/PLAN.md` holds the rest, including the three security repairs that must land in the fork of
Agtr **before** any agent runs.
