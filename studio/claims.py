"""Claim consistency: every field that ASSERTS quantum execution must be derivable from the execution record that
sits in the same payload.

This is the twin of the three-state invariant, and it catches what that invariant cannot. `external_fact` governs
states of resolution — RESOLVED / ABSENT / UNRESOLVABLE — not NAMES. A payload can be perfectly resolved, carry no
fabricated number at all, and still tell a reader the opposite of what happened, because a counter called
`quantum_success` was derived from something that has nothing to do with quantum execution.

The real case, found on 2026-11-19 in `/api/pipeline/admet_standalone` (job 5a362e56-077): `quantum_success: 1` beside
`quantum_backend: "NONE"`, `quantum_execution: "CLASSICAL_FALLBACK"` and `ibm_job_ids: []`. The counter was counting
predictions that did not raise, never executions that reached IBM, while the backend and mode fields WERE derived
from the real job ids. The same payload carried an honest signal and an unrelated counter. Two lines below the field,
a comment read `# HONEST: real engine label derived from the actual model that ran`. Somebody took care of the leaf
and left the envelope alone.

THE RULE. Collect the execution record — `ibm_job_ids`, `quantum_backend`, `quantum_execution` / `mode` /
`execution_mode`, `is_real_hardware`, `ran_quantum`. Then every key containing `quantum`, `ibm`, `qpu` or `hardware`
whose value ASSERTS something positive must follow from that record. If the record says nothing quantum ran, the
claim is a contradiction. If there is NO record, the claim is not false — it is unverifiable, and that is reported as
its own state rather than quietly passed or quietly failed.

A denial is not a claim: `quantum_success: 0`, `ibm_job_ids: []`, `quantum_backend: "NONE"`,
`quantum_engine: "classical LightGBM ensemble (no quantum hardware in this prediction)"` all deny, and a reader is
not misled by them. The checker must stay quiet on those or it would punish the honest fields.
"""
from __future__ import annotations

import re
from typing import Any

HIGH, WARN = "high", "warn"

# A key that names quantum execution. Whole words only: `equantumx` is not a quantum field, and matching substrings
# is how an audit once went from 60% to 94% (CLAUDE.md).
CLAIM_KEY = re.compile(r"(?:^|_)(quantum|ibm|qpu|hardware|vqe|sqd|qaoa)(?:_|$)", re.I)

# The fields that ARE the execution record. They describe the run; they never count as claims about it.
RECORD_KEYS = {"ibm_job_id", "ibm_job_ids", "qpu_job_id", "hardware_job_id", "quantum_backend", "ibm_backend",
               "backend_name", "backend", "engine", "quantum_execution", "execution_mode", "mode",
               "is_real_hardware", "ran_quantum", "quantum_engine", "_quantum_backend_resolved",
               "quantum_hardware_used"}
# The plain key `backend`, nested inside a stage, is where several BioQL pipelines actually record the lane they ran
# on — `mrna_therapeutic` writes `cudaq_nvidia_A100` and `local qiskit-aer MPS` under it, with a note saying the
# label is derived from the lane each algorithm executed on. Reading only `quantum_backend` made an earlier version
# of this checker call 53 honest payloads contradicted. A guard that cries wolf gets switched off, and then it
# protects nothing.

# The WHOLE value is a denial. `NONE` is BioQL's own word for "no quantum backend".
DENIES = re.compile(r"^\s*(none|null|nil|n/?a|off|no|false|0|-|unknown|unspecified|not[ _]run|skipped)\s*$", re.I)
# The value MENTIONS classical work anywhere in it — unanchored on purpose: BioQL writes
# "Bionics LightGBM (classical ML)", where the honest words are not at the start. An anchored pattern missed it and
# read the field as an unrecognised backend, which made the checker call the real contradiction consistent.
SAYS_CLASSICAL = re.compile(r"\b(classical|classic|lightgbm|xgboost|sklearn|heuristic|rule[ _-]based|cpu[ _-]only|"
                            r"fallback|mock|stub|placeholder|no quantum)\b", re.I)
# A string value that itself asserts quantum execution even under an innocent key name.
ASSERTS = re.compile(r"\b(ibm_[a-z]+|ibmq|heron|real[ _]hardware|on[ _]hardware|qpu\b|quantum[ _]hardware|"
                     r"executed[ _]on[ _]ibm)\b", re.I)
# A backend string that names an actual quantum lane: a device, a quantum simulator, or the algorithm that ran on
# one. Matched against the TOKENISED value (see `tokens`), so "CUDA-Q", "cudaq" and "CUDA Q" all land the same.
REAL_BACKEND = re.compile(r"\b(ibm|ibmq|fez|kingston|marrakesh|torino|heron|garnet|ionq|quantinuum|rigetti|braket|"
                          r"aer|statevector|cudaq|cuda|cuquantum|mps|qasm|qiskit|vqe|qaoa|qsvm|qnn|grover|sqd|"
                          r"uccsd)\b", re.I)


def tokens(s: str) -> str:
    """The value with every non-alphanumeric run turned into a space, so `\\b` behaves.

    Without this, `CLASSICAL_FALLBACK` does not match `\\bclassical\\b` — `_` is a word character — and the honest
    denial reads as an unrecognised backend. That one detail decided whether the real ADMET contradiction was caught
    or called consistent.
    """
    return re.sub(r"[^A-Za-z0-9]+", " ", s)


def _walk(o: Any, path: str = "$", depth: int = 0, limit: int = 8):
    yield path, o
    if depth >= limit:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}", depth + 1, limit)
    elif isinstance(o, list):
        for i, v in enumerate(o[:20000]):   # a cap here hides a contradiction past it
            yield from _walk(v, f"{path}[{i}]", depth + 1, limit)


def _key(path: str) -> str:
    return path.rsplit(".", 1)[-1].split("[")[0]


# A subtree that holds what was ASKED FOR, not what happened. `input.run_quantum: true` is a request; reading it as
# evidence of execution is the reader's mistake to avoid, not the payload lying about itself — so it is reported
# under its own code instead of being counted as a contradiction.
REQUEST_SUBTREE = re.compile(r"\.(input|inputs|params|parameters|request|args|arguments|config|options|settings)\.")


def _in_request(path: str) -> bool:
    return bool(REQUEST_SUBTREE.search(path))


def execution_record(payload: Any) -> dict:
    """What the payload itself says about where the work ran, gathered from the record fields only.

    `ran`: True (something quantum ran), False (nothing did), or None — NOT KNOWN, because the payload carries no
    execution record at all. None is never read as False: a missing record is not a denial.
    """
    jobs: list[str] = []
    backends: list[str] = []
    modes: list[str] = []
    real: list[bool] = []
    flags: list[bool] = []
    for p, v in _walk(payload):
        k = _key(p).lower()
        if k in ("ibm_job_id", "ibm_job_ids", "qpu_job_id", "hardware_job_id"):
            for j in (v if isinstance(v, list) else [v]):
                if isinstance(j, str) and j.strip() and not DENIES.match(j):
                    jobs.append(j)
        elif k in ("quantum_backend", "ibm_backend", "backend_name", "backend", "engine", "_quantum_backend_resolved"):
            if isinstance(v, str) and v.strip():
                backends.append(v)
        elif k in ("quantum_execution", "execution_mode", "mode"):
            if isinstance(v, str) and v.strip():
                modes.append(v)
        elif k == "is_real_hardware" and isinstance(v, bool):
            real.append(v)
        elif k in ("ran_quantum", "quantum_hardware_used") and isinstance(v, bool):
            flags.append(v)

    # POSITIVE EVIDENCE is required. "This string is not a denial" is not evidence that quantum ran — reading it that
    # way is the same mistake as reading a missing record as a denial, pointed the other way.
    names_quantum = [s for s in backends + modes if REAL_BACKEND.search(tokens(s))]
    accounted = [s for s in backends + modes if DENIES.match(s) or SAYS_CLASSICAL.search(tokens(s))]
    record = backends + modes + [str(x) for x in real + flags]
    if jobs or any(real) or any(flags) or names_quantum:
        ran: bool | None = True
    elif not record:
        ran = None                        # no record at all: unknown, never False
    elif len(accounted) + len(real) + len(flags) == len(record):
        ran = False                       # a record exists and every part of it denies or says classical
    else:
        ran = None                        # a record exists but part of it is unrecognised: cannot tell
    return {"ran": ran, "ibm_job_ids": jobs, "backends": backends, "modes": modes,
            "unrecognised": [s for s in backends + modes if s not in accounted and s not in names_quantum],
            "is_real_hardware": (sorted(set(real))[0] if len(set(real)) == 1 else ("mixed" if real else None)),
            "flags": flags, "has_record": ran is not None}


# Fields that hold PROSE: notes, disclaimers, limitations, instructions. They discuss quantum execution, and the
# honest ones discuss its ABSENCE — BioQL's own ADMET report carries "No IBM hardware job id present; any
# quantum-hardware claim is NOT supported by this..." and a regulatory disclaimer naming quantum hardware. An earlier
# version of this checker flagged both: it read the warning as the thing being warned about. A guard must not read
# its own explanation (CLAUDE.md), and the same applies to someone else's.
PROSE_KEYS = re.compile(r"(?:^|_)(note|notes|reason|description|desc|disclaimer|regulatory_disclaimer|limitation|"
                        r"limitations|caveat|caveats|warning|warnings|comment|comments|remark|explanation|"
                        r"interpretation|how_to_reproduce|methodology|methods|summary_text|message|text|title|"
                        r"label|objective|scope|references)(?:_|$)", re.I)


def _asserts(path: str, value: Any) -> str | None:
    """Why this field asserts quantum execution, or None when it does not. A denial is not a claim."""
    k = _key(path)
    if k.lower() in RECORD_KEYS:
        return None                       # the record describes the run; it does not claim about it
    if PROSE_KEYS.search(k):
        return None                       # prose discusses the run; a warning about quantum is not a quantum claim
    named = bool(CLAIM_KEY.search(k))
    if isinstance(value, bool):
        return "a true flag" if (named and value) else None
    if isinstance(value, (int, float)):
        return "a count above zero" if (named and value > 0) else None
    if isinstance(value, str):
        if DENIES.match(value) or SAYS_CLASSICAL.search(tokens(value)):
            return None
        if ASSERTS.search(tokens(value)):
            return "a value naming quantum execution"
        return "a non-empty value" if named else None
    if isinstance(value, (dict, list)):
        return "a populated result" if (named and len(value) > 0) else None
    return None


def check_claims(payload: Any, label: str = "payload") -> list[dict]:
    """[{severity, code, field, why, detail, payload}] — every field that claims more than the record supports.

    Codes:
      claim_without_execution  the record says nothing quantum ran, and this field says it did (HIGH)
      claim_unverifiable       this field claims quantum execution and the payload carries no record at all (WARN)
      claim_in_request         the field sits in an input/params subtree: a request, not evidence of a run (WARN)
    """
    rec = execution_record(payload)
    if rec["ran"] is True:
        return []                         # something quantum ran: the claims are derivable
    out = []
    for p, v in _walk(payload):
        why = _asserts(p, v)
        if not why:
            continue
        shown = v if isinstance(v, (bool, int, float, str)) else f"{type(v).__name__}[{len(v)}]"
        if _in_request(p):
            out.append({"severity": WARN, "code": "claim_in_request", "field": p, "why": why,
                        "detail": f"{_key(p)}={shown!r} is what was ASKED FOR; the record "
                                  f"{'denies' if rec['ran'] is False else 'does not say'} that it ran, so this field "
                                  f"is not evidence of execution", "payload": label})
        elif rec["ran"] is False:
            out.append({"severity": HIGH, "code": "claim_without_execution", "field": p, "why": why,
                        "detail": f"{_key(p)}={shown!r} claims quantum execution, but the record in the same payload "
                                  f"denies it (backends={rec['backends'] or '-'}, modes={rec['modes'] or '-'}, "
                                  f"ibm_job_ids={rec['ibm_job_ids'] or '[]'})", "payload": label})
        else:
            out.append({"severity": WARN, "code": "claim_unverifiable", "field": p, "why": why,
                        "detail": f"{_key(p)}={shown!r} claims quantum execution and this payload carries no "
                                  f"execution record to derive it from — not false, unverifiable", "payload": label})
    order = {HIGH: 0, WARN: 1}
    return sorted(out, key=lambda f: (order[f["severity"]], f["field"]))


def verdict(findings: list[dict]) -> str:
    """"contradicted" | "unverifiable" | "requested_only" | "consistent". A request that was never shown to run is
    its own state: weaker than a contradiction, and not the same as nothing to report."""
    if any(f["severity"] == HIGH for f in findings):
        return "contradicted"
    codes = {f["code"] for f in findings}
    if "claim_unverifiable" in codes:
        return "unverifiable"
    return "requested_only" if codes else "consistent"
