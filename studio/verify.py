"""The verifier: a result is not finished when it arrives, only when it has been checked.

It reads a pipeline's answer and reports where the ENVELOPE contradicts what the work actually was. It never judges
the science: BioQL's numbers are BioQL's. It judges labels, counters and provenance against each other — the class of
defect the owner's own audits kept finding by hand (a simulator presented as real hardware, a field named `*_quantum`
holding a classical result, an affinity claim with no control).

Every finding names its JSON path, so a person can go to the exact field. Nothing here changes a result: the
findings travel beside it, in the ledger, and a report built on a flagged result says so.

Checks, each from a real case:
  quantum_label_vs_execution  a quantum word in the envelope while the run says it was classical
  quantum_twin               `x_quantum` and `x_classical` carrying the same payload
  counter_vs_execution       a summary counting a quantum success where execution says fallback
  hardware_without_job       is_real_hardware true and no job id to prove it (real-or-null, CLAUDE.md)
  job_without_hardware       an IBM job id present while is_real_hardware says false
  subspace_fraction_missing  determinant selection with no dim/dim_FCI published (CLAUDE.md 2026-10-07)
  weak_arm_missing           a quantum advantage claim with no classical arm beside it
"""
from __future__ import annotations

import re
from typing import Any

from .claims import tokens

HIGH, WARN, NOTE = "high", "warn", "note"

QUANTUM_WORD = re.compile(r"(?:^|_)(quantum|qpu|vqe|sqd|qaoa|uccsd)(?:$|_)", re.I)
CLASSICAL_SAYS = re.compile(r"\b(classical|classic|fallback|no quantum|simulator|aer|statevector|lightgbm|mock)\b", re.I)
# Matched against TOKENISED text, where `ibm_fez` has already become `ibm fez` — so the pattern names the words,
# never the underscore. Written as `ibm_[a-z]+` it matched nothing after tokenising, which is how this check went
# quiet the moment the tokeniser was added.
HARDWARE_SAYS = re.compile(r"\b(ibm|ibmq|heron|fez|kingston|marrakesh|torino|garnet|ionq|quantinuum|rigetti|"
                           r"real hardware|on hardware)\b", re.I)
SELECTION = re.compile(r"(?:^|_)(sqd|determinant|selected_determinants|cisd|subspace)(?:$|_)", re.I)
ADVANTAGE = re.compile(r"\b(quantum advantage|outperform|better than classical|speedup|supremacy)\b", re.I)
CLASSICAL_ARM = ("classical_baseline", "classical_arm", "baseline", "hdiag", "bare_coupling", "classical_reference",
                 "control", "negative_control")


def _walk(o: Any, path: str = "$", depth: int = 0, limit: int = 7):
    yield path, o
    if depth >= limit:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}", depth + 1, limit)
    elif isinstance(o, list):
        for i, v in enumerate(o[:200]):
            yield from _walk(v, f"{path}[{i}]", depth + 1, limit)


def _flat_text(o: Any, limit: int = 4000) -> str:
    out = []
    for _, v in _walk(o):
        if isinstance(v, str) and len(v) < 400:
            out.append(v)
        if len(out) > limit:
            break
    return " ".join(out)


def _find(o: Any, name: str) -> list[tuple[str, Any]]:
    """Every value whose KEY is exactly this name. By token, never by substring: `"kd" in path` also matches
    `score_breakdown`, which once inflated an audit from 60% to 94% (CLAUDE.md)."""
    return [(p, v) for p, v in _walk(o) if p.rsplit(".", 1)[-1].split("[")[0] == name]


def check(result: Any, provenance: dict | None = None) -> list[dict]:
    """[{severity, code, detail, path}], worst first. An empty list means nothing contradicted itself."""
    prov = provenance or {}
    out: list[dict] = []
    say = lambda sev, code, detail, path: out.append({"severity": sev, "code": code, "detail": detail, "path": path})

    # --- what the run says it was
    modes = [(p, str(v)) for p, v in _walk(result)
             if p.rsplit(".", 1)[-1] in ("quantum_execution", "execution_mode", "quantum_engine", "backend",
                                          "quantum_backend", "method") and isinstance(v, (str, int, float))]
    # Every comparison goes through `tokens` first. `\bclassical\b` cannot see into `CLASSICAL_FALLBACK` — the
    # underscore is a word character — so BioQL's own word for this very defect was invisible here, and the real
    # ADMET run was caught only through `quantum_backend: "NONE"` and some leaf prose. Same blind spot hid
    # `aer_simulator`.
    classical = [(p, v) for p, v in modes if CLASSICAL_SAYS.search(tokens(v)) or v.strip().upper() in ("NONE", "N/A")]
    hardware = [(p, v) for p, v in modes if HARDWARE_SAYS.search(tokens(v))]
    real = prov.get("is_real_hardware")
    jobs = list(prov.get("ibm_job_ids") or [])

    # --- provenance against itself (real-or-null)
    if real is True and not jobs:
        say(HIGH, "hardware_without_job", "the answer says is_real_hardware true but carries no job id to prove it", "$")
    if hardware and not jobs and real is not True:
        # A field naming a real device IS a hardware claim, even when no `is_real_hardware` flag was set. Reading
        # only the flag let `quantum_backend: "ibm_fez"` with no job id pass in silence.
        say(HIGH, "hardware_named_without_job",
            f"{hardware[0][1]!r} at {hardware[0][0]} names real quantum hardware, and the answer carries no job id "
            f"to prove anything ran there", hardware[0][0])
    if jobs and real is False:
        say(HIGH, "job_without_hardware", f"an IBM job id is present ({jobs[0]}) while is_real_hardware says false", "$")
    if real == "mixed":
        # Not a warning: a reader cannot tell what actually ran, which is the defect itself.
        say(HIGH, "hardware_mixed", "parts of this answer claim real hardware and parts deny it", "$")

    # --- a quantum-named field holding a classical result
    if classical and real is not True and not jobs:
        for p, v in _walk(result):
            key = p.rsplit(".", 1)[-1].split("[")[0]
            if QUANTUM_WORD.search(key) and isinstance(v, (dict, list)) and v:
                say(HIGH, "quantum_label_vs_execution",
                    f"{key!r} holds a result, but the run reports {classical[0][1]!r} at {classical[0][0]} "
                    f"and there is no job id: a quantum name over classical work", p)
                break

    # --- x_quantum and x_classical carrying the same thing
    for p, v in _walk(result):
        key = p.rsplit(".", 1)[-1].split("[")[0]
        if key.endswith("_quantum") and isinstance(v, (dict, list)):
            twin = p[: -len("_quantum")] + "_classical"
            for q, w in _walk(result):
                if q == twin and w == v:
                    say(HIGH, "quantum_twin",
                        f"{key!r} and its _classical twin carry an identical payload: the same work under two names", p)
                    break

    # --- counters that disagree with the execution mode
    for p, v in _walk(result):
        if not isinstance(v, dict):
            continue
        qs, cf = v.get("quantum_success"), v.get("classical_fallback")
        # The contradiction is `quantum_success > 0` on a run the envelope calls classical. Requiring
        # `classical_fallback == 0` as well gave two free escapes: omit the companion counter, or set it above zero
        # — so "5 quantum successes, 1 fallback" on a fully classical run passed in silence.
        if isinstance(qs, (int, float)) and not isinstance(qs, bool) and classical and qs > 0:
            seen = f"quantum_success={qs}" + (f" and classical_fallback={cf}" if isinstance(cf, (int, float)) else
                                              " with no classical_fallback beside it")
            say(HIGH, "counter_vs_execution",
                f"this summary counts {seen}, while the run reports {classical[0][1]!r} at {classical[0][0]}", p)

    # --- determinant selection with no subspace fraction (CLAUDE.md 2026-10-07)
    if any(SELECTION.search(p.rsplit(".", 1)[-1]) for p, _ in _walk(result)):
        if not (_find(result, "subspace_fraction") or _find(result, "dim_fci") or _find(result, "dim_FCI")):
            say(WARN, "subspace_fraction_missing",
                "a determinant-selection result with no dim/dim_FCI: without it the point cannot tell sampling "
                "from full CI", "$")

    # --- an advantage claim with no classical arm
    text = _flat_text(result)
    if ADVANTAGE.search(text) and not any(_find(result, k) for k in CLASSICAL_ARM):
        say(WARN, "weak_arm_missing",
            "this answer claims an advantage and carries no classical arm beside it (a cheap baseline is hard to "
            "beat and costs seconds)", "$")

    order = {HIGH: 0, WARN: 1, NOTE: 2}
    return sorted(out, key=lambda f: order[f["severity"]])


def verdict(findings: list[dict]) -> str:
    """"clean" | "concerns" | "contradicted". Never "pass": the verifier does not bless science, it reports labels."""
    if any(f["severity"] == HIGH for f in findings):
        return "contradicted"
    return "concerns" if findings else "clean"
