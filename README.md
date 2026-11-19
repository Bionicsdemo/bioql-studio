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
| Nothing can reach a real QPU without a person | `studio/adapter.py` — a `hardware` entry, an entry marked for approval, or arguments naming real hardware are refused unless the call carries `approved_by`. The MCP server always calls with no approval, so Claude cannot supply one |
| A failure never looks like a result | `studio/resolution.py` — RESOLVED / ABSENT / UNRESOLVABLE, the third never degrading into the second (BioQL's own invariant, re-implemented, not imported) |
| Provenance is BioQL's, never inferred | `adapter.provenance` — `is_real_hardware`, IBM job ids and backends are copied when BioQL states them and stay `null` when it does not |
| A session leaves BioQL as it found it | `studio/guard.py` — a sha256 fingerprint of BioQL's source trees before and after; the session fails if anything changed |
| Every call is on the record | `studio/ledger.py` — one hash-chained line per call, refusals included |

What the guard does **not** claim: it sees *that* a file changed, not *who* changed it. BioQL's own running services, or
another person working in the repository at the same time, show up too — so it lists every file by name.

## Try it (BioQL's backend must already be running; Studio never starts it)

```sh
python3 -m pytest -q                 # 21 tests
python3 tools/e2e_live.py            # real calls to 127.0.0.1:5035, wrapped in the guard
python3 -m studio tools              # what Studio may call
python3 -m studio registry           # BioQL's own pipelines, and which could be allowed
python3 -m studio call smiles_to_3d '{"smiles":"CC(=O)Nc1ccc(O)cc1"}'
python3 -m studio ledger verify
```

Measured on this Mac, 2026-11-19: the live check passed 9/9 with the guard clean over 46,819 BioQL files (~35 s, most
of it the two fingerprints).

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
