"""Rails and evals: Lauren Tan's third mechanism — a comment made twice is a code smell, so it becomes a hard rule.

Two halves, kept apart on purpose.

Rails (first half). A rail is a rule that BLOCKS A REPORT. It does not block a run, it never changes a
scientific number, and it never hides a finding: the findings travel beside the result exactly as `verify.check`
produced them, and the rail only says "this result does not get written up until a person deals with it". A rail is
born the way a reviewer's comment becomes a lint rule: `note_rejection` records that a person (or the auditor) turned
a result down for a reason, and when the SAME code has been turned down `threshold` times (2 by default) it is
promoted automatically. One rejection never promotes — one rejection is an opinion, two are a pattern. Every
promotion is recorded as an event with the rejections that caused it, so a person can read why a rail exists and
remove it; `remove_rail` is also recorded, and rejections from before a removal no longer count, or un-promoting
would re-promote on the next sentence.

Evals (second half). A stored case {name, result_fixture, expect_verdict, expect_codes} replayed through
`verify.check` / `verify.verdict`. These are unit tests for the ORCHESTRATION, not for the science: they check that
the verifier still reaches the same verdict on shapes BioQL really produced. They say nothing about whether a
number is right — no eval here can tell a correct energy from a wrong one. `run_evals` reports pass/fail per case
and a rate; it reports a failure, it does not raise, because a suite that falls over on the first bad case tells you
about one case instead of all of them.

The store is `home()/rails.json` (0600, atomic). It is read with the three states, like any other external fact: no
file is ABSENT — that IS information, it means no rails yet — while a file that cannot be read or parsed is
UNRESOLVABLE and raises. A corrupt rails file must never read as "no rails": that would turn a brake into a
silently open gate, which is the degradation CLAUDE.md's invariant of 2026-11-18 exists to prevent.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import home, write_private
from . import verify
from .adapter import provenance
from .resolution import Resolution, absent, resolved, unresolvable

SEVERITIES = (verify.HIGH, verify.WARN, verify.NOTE)
_ORDER = {verify.HIGH: 0, verify.WARN: 1, verify.NOTE: 2}
NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
SOURCE = "rails.json"
MIN_THRESHOLD = 2               # the whole point: one rejection is an opinion, two are a pattern


def rails_path() -> Path:
    return home() / "rails.json"


def _empty() -> dict:
    return {"version": 1, "rails": {}, "rejections": {}, "events": []}


def _check_rail(code: str, rail: Any) -> None:
    if not isinstance(rail, dict):
        raise ValueError(f"rail {code!r} is not an object")
    if rail.get("code") != code:
        raise ValueError(f"rail {code!r} carries code {rail.get('code')!r}")
    if rail.get("severity") not in SEVERITIES:
        raise ValueError(f"rail {code!r} has severity {rail.get('severity')!r}")
    if rail.get("pattern"):
        re.compile(rail["pattern"])             # a pattern that cannot compile is a corrupt store, not "no pattern"


def read_store(file: Path | None = None) -> Resolution:
    """RESOLVED the store, ABSENT when there is no rails file yet, UNRESOLVABLE when one exists and cannot be used."""
    f = file or rails_path()
    try:
        raw = f.read_bytes()
    except FileNotFoundError:
        return absent(SOURCE, f"no rails file at {f.name}: no rails have been created yet")
    except OSError as e:
        return unresolvable(SOURCE, f"cannot read {f.name}: {e}")
    try:
        store = json.loads(raw)
        if not isinstance(store, dict):
            raise ValueError("the rails file is not an object")
        for part in ("rails", "rejections"):
            if not isinstance(store.setdefault(part, {}), dict):
                raise ValueError(f"{part!r} is not an object")
        if not isinstance(store.setdefault("events", []), list):
            raise ValueError("'events' is not a list")
        for code, rail in store["rails"].items():
            _check_rail(code, rail)
    except (ValueError, re.error) as e:
        return unresolvable(SOURCE, f"{f.name} exists but cannot be trusted: {e}")
    store.setdefault("version", 1)
    return resolved(store, SOURCE, f"{len(store['rails'])} rail(s)")


def _store(file: Path | None = None) -> dict:
    """The store, or a fresh one when there is none. Raises on UNRESOLVABLE: a brake is never assumed to be absent."""
    return read_store(file).or_default(_empty())


def _save(store: dict, file: Path | None = None) -> None:
    write_private(file or rails_path(), json.dumps(store, indent=1, sort_keys=True))


def _next(store: dict) -> int:
    """A monotone number per store. Events are ordered by it rather than by their clock: a rejection recorded in the
    same millisecond as a removal must still be on the right side of it."""
    store["seq"] = int(store.get("seq") or 0) + 1
    return store["seq"]


def _event(store: dict, event: str, code: str, **extra: Any) -> dict:
    row = {"n": _next(store), "t": round(time.time(), 3), "event": event, "code": code, **extra}
    store["events"].append(row)
    return row


# --- rails: a rule that blocks a report


def add_rail(code: str, why: str, severity: str = verify.WARN, pattern: str | None = None,
             source: str = "a person", file: Path | None = None, **extra: Any) -> dict:
    """Create or replace a rail. `severity` is a FLOOR: a finding with this code blocks when it is at least this
    severe, so a rail on a `warn` code (the default) blocks that warning too — otherwise promoting a repeated
    rejection of `weak_arm_missing` would create a rail that can never fire."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, not {severity!r}")
    if not code or not isinstance(code, str):
        raise ValueError("a rail needs a code: the code `verify.check` reports")
    if not why:
        raise ValueError("a rail needs a why: a person has to be able to read why a report was blocked")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            raise ValueError(f"the rail's pattern does not compile: {e}") from e
    store = _store(file)                         # before the write, so a corrupt store is never overwritten
    old = store["rails"].get(code) or {}
    rail = {"code": code, "pattern": pattern, "severity": severity, "why": why, "source": source,
            "created": old.get("created") or round(time.time(), 3), "hits": int(old.get("hits") or 0), **extra}
    _check_rail(code, rail)
    store["rails"][code] = rail
    _event(store, "added" if not old else "replaced", code, severity=severity, source=source, why=why)
    _save(store, file)
    return rail


def list_rails(file: Path | None = None) -> list[dict]:
    return [store_rail for _, store_rail in sorted(_store(file)["rails"].items())]


def remove_rail(code: str, why: str = "", file: Path | None = None) -> bool:
    """Un-promote. The rejection history stays, but it stops counting: rejections from before a removal must not
    re-promote the rail on the next one."""
    store = _store(file)
    if code not in store["rails"]:
        return False
    store["rails"].pop(code)
    _event(store, "removed", code, why=why or "removed by hand")
    _save(store, file)
    return True


def rail_for(code: str, file: Path | None = None) -> dict | None:
    return _store(file)["rails"].get(code)


def block(findings: list[dict], rails: list[dict] | None = None, file: Path | None = None) -> list[dict]:
    """The findings that must stop a report being produced: a finding whose code has a rail, at or above the rail's
    severity floor, and matching the rail's pattern when it has one.

    Pure: it reads, it never writes, and it never mutates the findings handed to it. `rails=[]` means no rails (and
    so nothing blocks); `rails=None` means read the store."""
    known = {r["code"]: r for r in (list_rails() if rails is None else rails)}
    out = []
    for f in findings:
        rail = known.get(f.get("code"))
        if rail is None:
            continue
        if _ORDER.get(f.get("severity"), 9) > _ORDER[rail["severity"]]:
            continue
        if rail.get("pattern") and not re.search(rail["pattern"], f"{f.get('detail', '')} {f.get('path', '')}", re.I):
            continue
        out.append({**f, "rail": rail["code"], "rail_why": rail["why"]})
    return out


def gate(findings: list[dict], rails: list[dict] | None = None, file: Path | None = None) -> dict:
    """{allowed, blocked_by, verdict} — whether a REPORT may be produced from this result.

    A rail blocks a REPORT, never a run. Nothing here re-runs, re-scores or edits anything: the scientific numbers
    are untouched and every finding stays in `findings` whether it blocked or not. A `high` finding with no rail on
    its code leaves `allowed` True with verdict "contradicted" — the result is still contradicted and says so; the
    rail is only what makes that refusal automatic once a person has rejected the same thing twice.

    `hits` is counted only when this call read the store itself (`rails=None`), so passing rails in stays a pure
    query and a caller cannot inflate the count by re-checking."""
    blocked = block(findings, rails)
    if blocked and rails is None:
        store = _store(file)
        for b in blocked:
            if b["code"] in store["rails"]:
                store["rails"][b["code"]]["hits"] = int(store["rails"][b["code"]].get("hits") or 0) + 1
        _save(store, file)
    return {"allowed": not blocked, "blocked_by": blocked, "verdict": verify.verdict(findings)}


# --- promotion from repetition


def _countable(store: dict, code: str) -> list[dict]:
    """The rejections that still count: the ones after the last time this rail was removed."""
    cut = max([e.get("n") or 0 for e in store["events"] if e.get("code") == code and e.get("event") == "removed"] or [0])
    return [r for r in store["rejections"].get(code, []) if (r.get("n") or 0) > cut]


def note_rejection(code: str, detail: str, path: str = "$", by: str = "a person",
                   threshold: int = MIN_THRESHOLD, file: Path | None = None) -> dict:
    """Record that a result was turned down for `code`, then promote if that makes it the `threshold`-th time.

    {code, rejections, promoted, rail}: `rejections` counts only the ones that still count (see `_countable`)."""
    if not code or not isinstance(code, str):
        raise ValueError("a rejection needs the code that was rejected")
    if threshold < MIN_THRESHOLD:
        raise ValueError(f"threshold must be at least {MIN_THRESHOLD}: a single rejection never becomes a rail")
    store = _store(file)                         # a corrupt store raises here, before anything is written
    existed = code in store["rails"]
    store["rejections"].setdefault(code, []).append(
        {"n": _next(store), "t": round(time.time(), 3), "detail": detail, "path": path, "by": by})
    _event(store, "rejected", code, detail=detail, path=path, by=by)
    _save(store, file)
    rail = promote(code, threshold, file=file)
    return {"code": code, "rejections": len(_countable(_store(file), code)),
            "promoted": rail is not None and not existed, "rail": rail}


def promote(code: str, threshold: int = MIN_THRESHOLD, file: Path | None = None) -> dict | None:
    """Promote `code` to a rail once it has been rejected `threshold` times. Idempotent: an existing rail is
    returned untouched and no second event is written. Returns None while the rejections are not enough."""
    if threshold < MIN_THRESHOLD:
        raise ValueError(f"threshold must be at least {MIN_THRESHOLD}: a single rejection never becomes a rail")
    store = _store(file)
    if code in store["rails"]:
        return store["rails"][code]
    history = _countable(store, code)
    if len(history) < threshold:
        return None
    rail = {"code": code, "pattern": None, "severity": verify.WARN, "hits": 0,
            "created": round(time.time(), 3), "source": f"promoted after {len(history)} rejections",
            "why": f"rejected {len(history)} times by {', '.join(sorted({h.get('by', '?') for h in history}))}: "
                   f"{history[-1].get('detail') or code}",
            "history": history}
    _check_rail(code, rail)
    store["rails"][code] = rail
    _event(store, "promoted", code, rejections=len(history), threshold=threshold, why=rail["why"])
    _save(store, file)
    return rail


def history(code: str | None = None, file: Path | None = None) -> list[dict]:
    """Every recorded event, so a person can read why a rail exists before removing it."""
    return [e for e in _store(file)["events"] if code is None or e.get("code") == code]


# --- evals
#
# An eval is orchestration, not science. Each case is a shape BioQL really produced (or a defect a real audit found)
# plus the verdict the verifier is expected to reach on it. The suite proves the verifier still behaves; it proves
# nothing whatsoever about whether a number is correct.

REAL_ADMET: dict = {        # the real run of 2026-11-19, job 5a362e56-077 (ADMET on paracetamol), cut to the fields
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "ibm_job_ids": [], "molecules_analyzed": 1,
    "summary": {"classical_fallback": 0, "quantum_success": 1, "total_analyzed": 1},
    "results": [{"molecule_id": 1, "smiles": "CC(=O)Nc1ccc(O)cc1", "status": "SUCCESS",
                 "method": "bionics-admet-lgbm-ensemble",
                 "admet_classical": {"backend": "Bionics LightGBM (classical ML)",
                                     "raw_predictions": {"ames": 0.1337},
                                     "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this "
                                                       "prediction)"},
                 "admet_quantum": {"backend": "Bionics LightGBM (classical ML)",
                                   "raw_predictions": {"ames": 0.1337},
                                   "quantum_engine": "classical LightGBM ensemble (no quantum hardware in this "
                                                     "prediction)"}}]}

HONEST_ADMET: dict = {      # the same work, named for what it is
    "pipeline": "ADMET_STANDALONE", "quantum_backend": "NONE", "quantum_execution": "CLASSICAL_FALLBACK",
    "summary": {"classical_fallback": 1, "quantum_success": 0, "total_analyzed": 1},
    "results": [{"molecule_id": 1, "smiles": "CC(=O)Nc1ccc(O)cc1", "status": "SUCCESS",
                 "admet_classical": {"backend": "Bionics LightGBM (classical ML)",
                                     "raw_predictions": {"ames": 0.1337}}}]}

HARDWARE_NO_JOB: dict = {"energy": -1.2, "is_real_hardware": True, "quantum_backend": "ibm_fez",
                         "ibm_job_ids": []}


def owners_proposed_fix(base: dict | None = None) -> dict:
    """The owner's intended fix for `counter_vs_execution`, built by running THEIR expression, not a transcription:

        "quantum_success": sum(1 for r in results["results"] if r.get("status") == "SUCCESS")

    It counts a row that finished, whatever ran it, so on this run — one classical row with status SUCCESS — it
    produces the same 1 the envelope already claimed, and the contradiction survives untouched. That is why this
    case exists: the eval suite's job is to say so before the fix ships, not after."""
    results = dict(base or REAL_ADMET)
    fixed = json.loads(json.dumps(results))
    fixed["summary"] = {**fixed["summary"],
                        "quantum_success": sum(1 for r in results["results"] if r.get("status") == "SUCCESS")}
    return fixed


BUILTIN: list[dict] = [
    {"name": "real_admet_envelope_2026_11_19", "result_fixture": REAL_ADMET, "expect_verdict": "contradicted",
     "expect_codes": ["quantum_label_vs_execution", "quantum_twin", "counter_vs_execution"],
     "why": "the real run: an identical LightGBM payload under both _quantum and _classical, and a summary counting "
            "a quantum success while the run reports a classical fallback and no backend."},
    {"name": "honest_classical_admet", "result_fixture": HONEST_ADMET, "expect_verdict": "clean",
     "expect_codes": [],
     "why": "the same classical work named for what it is: the verifier must stay quiet, or it says nothing when it "
            "does fire."},
    {"name": "hardware_claim_without_a_job_id", "result_fixture": HARDWARE_NO_JOB, "expect_verdict": "contradicted",
     "expect_codes": ["hardware_without_job"],
     "why": "real-or-null: a hardware claim with no IBM job id to prove it."},
    {"name": "owners_proposed_counter_fix", "result_fixture": owners_proposed_fix(), "expect_verdict": "contradicted",
     "expect_codes": ["counter_vs_execution"],
     "why": "the fix the owner intends counts any SUCCESS row as a quantum success, so a classical row still counts: "
            "the verifier must STILL report counter_vs_execution. This is the case that earns the suite its keep."},
]


def evals_dir() -> Path:
    d = home() / "evals"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _case_path(name: str) -> Path:
    if not NAME.match(name or ""):
        raise ValueError(f"an eval name must be lowercase letters, digits and underscores: {name!r}")
    return evals_dir() / f"{name}.json"


def save_eval(case: dict) -> Path:
    """Store a case. A name that collides with a built-in is refused: two cases under one name means a run's result
    depends on which one won, which is not a test."""
    name = case.get("name")
    p = _case_path(name)
    if any(b["name"] == name for b in BUILTIN):
        raise ValueError(f"{name!r} is a built-in case; pick another name rather than shadow it")
    for key in ("result_fixture", "expect_verdict"):
        if key not in case:
            raise ValueError(f"an eval case needs {key!r}")
    write_private(p, json.dumps({**case, "expect_codes": list(case.get("expect_codes") or [])}, indent=1))
    return p


def read_eval(name: str) -> Resolution:
    """RESOLVED the case, ABSENT when no such file, UNRESOLVABLE when it exists and cannot be used."""
    p = _case_path(name)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return absent("evals", f"no stored case named {name!r}")
    except OSError as e:
        return unresolvable("evals", f"cannot read {name!r}: {e}")
    try:
        case = json.loads(raw)
        if not isinstance(case, dict) or "result_fixture" not in case or "expect_verdict" not in case:
            raise ValueError("a case needs result_fixture and expect_verdict")
    except ValueError as e:
        return unresolvable("evals", f"{name!r} exists but cannot be trusted: {e}")
    return resolved({**case, "name": name}, "evals", f"stored case {name!r}")


def list_evals(include_builtin: bool = True) -> list[dict]:
    """The built-in cases plus every stored one. A stored file that cannot be read becomes a case carrying `error`,
    so `run_evals` fails it instead of quietly running a smaller suite."""
    cases = [dict(b) for b in BUILTIN] if include_builtin else []
    names = {c["name"] for c in cases}
    for p in sorted(evals_dir().glob("*.json")):
        name = p.stem
        if not NAME.match(name):
            cases.append({"name": name, "error": f"{p.name} is not a usable case name"})
            continue
        res = read_eval(name)
        if res.resolved:
            case = res.value
            cases.append({**case, "error": "a stored case shadows a built-in of the same name"}
                         if name in names else case)
        elif res.unresolvable:
            cases.append({"name": name, "error": res.detail})
    return cases


def run_case(case: dict) -> dict:
    """Replay one case through `verify.check` / `verify.verdict`. Never raises: a broken case is a failed case."""
    row: dict = {"name": case.get("name") or "<unnamed>", "passed": False,
                 "expect_verdict": case.get("expect_verdict"), "expect_codes": list(case.get("expect_codes") or [])}
    if case.get("error"):
        return {**row, "error": case["error"]}
    try:
        fx = case["result_fixture"]
        findings = verify.check(fx, provenance(fx))
        row["verdict"] = verify.verdict(findings)
        row["codes"] = sorted({f["code"] for f in findings})
        row["missing_codes"] = sorted(set(row["expect_codes"]) - set(row["codes"]))
        row["passed"] = row["verdict"] == row["expect_verdict"] and not row["missing_codes"]
        if not row["passed"]:
            row["error"] = (f"expected {row['expect_verdict']!r}, got {row['verdict']!r}"
                            + (f"; missing {row['missing_codes']}" if row["missing_codes"] else ""))
    except Exception as e:                      # a case is data; data must not be able to take the suite down
        row["error"] = f"{type(e).__name__}: {e}"
    return row


def run_evals(cases: list[dict] | None = None) -> dict:
    """Replay the suite: {cases, passed, failed, total, rate, ok}.

    Unit tests for the ORCHESTRATION — that the verifier still reaches the same verdict on shapes BioQL really
    produced. Not for the science: no case here can tell a correct number from a wrong one.

    It reports failures rather than raising, and an empty suite is `ok: False` with rate 0.0 — a suite that ran
    nothing has not passed, and "0 of 0" must never read as 100%."""
    rows = [run_case(c) for c in (list_evals() if cases is None else cases)]
    passed = sum(1 for r in rows if r["passed"])
    total = len(rows)
    return {"cases": rows, "passed": passed, "failed": total - passed, "total": total,
            "rate": round(passed / total, 4) if total else 0.0,
            "ok": bool(total) and passed == total}
