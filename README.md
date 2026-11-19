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
| Every call is on the record | `studio/ledger.py` — one hash-chained line per call, refusals included |

What the guard does **not** claim: it sees *that* a file changed, not *who* changed it. BioQL's own running services, or
another person working in the repository at the same time, show up too — so it lists every file by name.

## Try it (BioQL's backend must already be running; Studio never starts it)

```sh
python3 -m pytest -q                      # 26 tests
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

What is **not** proven yet: no pipeline job has actually been submitted. The submit-and-poll path is tested against a
stand-in, and its shapes are read from BioQL's own UI, but the first real run needs an approval only the owner can
give — which is the design working, not a gap in it.

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
